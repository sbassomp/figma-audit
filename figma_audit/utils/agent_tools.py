"""Tool definitions and implementations for the agentic loop.

Each tool is a small, sandboxed primitive the agent can call. Tools must:
- accept (params: dict, context: AgentContext) and return a JSON-serializable value
- never raise on bad input — return an "error" key the model can read and adapt to
- enforce sandbox limits from AgentContext (file size, hit count, http calls)
- never write outside the harness's own bookkeeping (no write_file, no shell)

The Anthropic tool-use protocol expects each tool to ship a JSON Schema. We
keep the schema next to the implementation so they evolve together.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from figma_audit.utils.agent_context import AgentContext

# Maximum size of a single tool result string handed back to the model.
# Larger results are truncated with a clear marker so the model knows.
MAX_TOOL_RESULT_BYTES = 20_000

# Directories we never enter when listing/grepping (noise + secret risk).
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "build",
        ".dart_tool",
        ".gradle",
        ".idea",
        ".vscode",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        ".next",
        "target",
    }
)

# Keys whose values we redact in JSON bodies returned from http_request,
# so the model never sees a real bearer token / password / cookie.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(authorization|password|token|secret|api[_-]?key|cookie|set[_-]?cookie)",
    re.IGNORECASE,
)


@dataclass
class Tool:
    """One tool the agent can call. Schema follows Anthropic's tool-use spec."""

    name: str
    description: str
    input_schema: dict
    run: Callable[[dict, AgentContext], Any]

    def to_anthropic(self) -> dict:
        """Serialize for the `tools=[...]` parameter of messages.create."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _truncate(text: str, marker: str = "... [truncated]") -> str:
    """Cap a string at MAX_TOOL_RESULT_BYTES with a visible marker."""
    if len(text) <= MAX_TOOL_RESULT_BYTES:
        return text
    return text[: MAX_TOOL_RESULT_BYTES - len(marker)] + marker


def _redact_sensitive(obj: Any) -> Any:
    """Recursively redact values whose keys look sensitive.

    Keeps the structure intact so the model can reason about shape/length but
    cannot exfiltrate secret material via tool result echoing.
    """
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if SENSITIVE_KEY_PATTERN.search(str(k)) else _redact_sensitive(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_sensitive(item) for item in obj]
    return obj


# ─────────────────────────────────────────────────────────────────────
# read_file
# ─────────────────────────────────────────────────────────────────────

def _run_read_file(params: dict, ctx: AgentContext) -> dict:
    rel_path = params.get("path", "")
    offset = int(params.get("offset", 0) or 0)
    max_bytes = min(int(params.get("max_bytes", ctx.max_file_bytes) or ctx.max_file_bytes),
                    ctx.max_file_bytes)

    if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
        return {"error": f"invalid path: {rel_path!r} (must be relative, no '..')"}

    target = ctx.project_dir / rel_path
    if not ctx.is_inside_sandbox(target):
        return {"error": f"path escapes sandbox: {rel_path}"}
    if not target.exists():
        return {"error": f"file not found: {rel_path}"}
    if not target.is_file():
        return {"error": f"not a regular file: {rel_path}"}

    try:
        with open(target, "rb") as f:
            if offset:
                f.seek(offset)
            raw = f.read(max_bytes + 1)  # +1 to detect truncation
    except OSError as e:
        return {"error": f"read failed: {e}"}

    # Reject binary files (heuristic: NUL byte in first 512)
    if b"\x00" in raw[:512]:
        return {"error": f"binary file refused: {rel_path}"}

    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    return {
        "path": rel_path,
        "size_bytes": len(raw[:max_bytes]),
        "truncated": truncated,
        "content": text,
    }


READ_FILE = Tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file from the project under audit. The path is "
        "relative to the project root and may not contain '..'. Reads are "
        "capped at 50KB by default; pass `max_bytes` to lower the cap. "
        "Binary files are rejected. Use this to inspect specific source files "
        "you have already located via grep_code or list_files."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to the project root "
                    "(e.g. 'lib/features/auth/auth_repository.dart')."
                ),
            },
            "offset": {
                "type": "integer",
                "description": "Byte offset to start reading from. Default 0.",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Max bytes to return (capped at 50000).",
            },
        },
        "required": ["path"],
    },
    run=_run_read_file,
)


# ─────────────────────────────────────────────────────────────────────
# grep_code
# ─────────────────────────────────────────────────────────────────────

def _grep_with_rg(pattern: str, glob: str, case_insensitive: bool, max_results: int,
                  cwd: Path) -> tuple[list[str], str | None]:
    """Try ripgrep first; return (lines, error_or_None)."""
    cmd = [
        "rg",
        "--no-heading",
        "--line-number",
        "--max-count",
        str(max_results),
        "--max-filesize",
        "1M",
    ]
    if case_insensitive:
        cmd.append("-i")
    # Always exclude noise directories regardless of .gitignore presence,
    # otherwise tests and projects without a .gitignore see false positives.
    for excluded in EXCLUDED_DIRS:
        cmd.extend(["-g", f"!{excluded}"])
    if glob and glob != "**/*":
        cmd.extend(["-g", glob])
    cmd.extend(["-e", pattern])
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        return [], "rg not installed"
    except subprocess.TimeoutExpired:
        return [], "rg timeout"
    # rg exit code 1 = no matches; 2 = error
    if result.returncode == 2:
        return [], result.stderr.strip() or "rg error"
    return result.stdout.splitlines()[:max_results], None


def _grep_with_python(pattern: str, glob: str, case_insensitive: bool, max_results: int,
                      cwd: Path) -> list[str]:
    """Pure-Python fallback when ripgrep is unavailable."""
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return [f"<regex error: {e}>"]

    glob_pattern = glob if glob else "**/*"
    hits: list[str] = []
    for path in cwd.glob(glob_pattern):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            with open(path, "rb") as f:
                if b"\x00" in f.read(512):
                    continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = path.relative_to(cwd)
                hits.append(f"{rel}:{lineno}:{line.rstrip()}")
                if len(hits) >= max_results:
                    return hits
    return hits


def _run_grep_code(params: dict, ctx: AgentContext) -> dict:
    pattern = params.get("pattern", "")
    glob = params.get("file_glob", "**/*") or "**/*"
    case_insensitive = bool(params.get("case_insensitive", False))
    max_results = min(int(params.get("max_results", ctx.max_grep_hits) or ctx.max_grep_hits),
                      ctx.max_grep_hits)

    if not pattern:
        return {"error": "pattern is required"}
    if glob.startswith("/") or ".." in glob:
        return {"error": f"invalid glob: {glob!r}"}

    lines, rg_error = _grep_with_rg(pattern, glob, case_insensitive, max_results,
                                    ctx.project_dir)
    used = "rg"
    if rg_error == "rg not installed":
        lines = _grep_with_python(pattern, glob, case_insensitive, max_results,
                                  ctx.project_dir)
        used = "python"
    elif rg_error:
        return {"error": rg_error, "tool": "rg"}

    capped = len(lines) >= max_results
    return {
        "pattern": pattern,
        "glob": glob,
        "matches": lines,
        "match_count": len(lines),
        "capped": capped,
        "engine": used,
    }


GREP_CODE = Tool(
    name="grep_code",
    description=(
        "Search the project codebase with a regex pattern (ripgrep semantics, "
        "ECMAScript-like). Returns matching lines prefixed with `path:line:`. "
        "Use a narrow file_glob (e.g. '**/*.dart', '**/*_repository.dart') to "
        "stay focused — broad searches waste tokens. Hits are capped at 200; "
        "if `capped` is true, refine your pattern."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for.",
            },
            "file_glob": {
                "type": "string",
                "description": (
                    "Glob limiting which files to search, e.g. '**/*.dart'. "
                    "Default '**/*'."
                ),
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive search. Default false.",
            },
            "max_results": {
                "type": "integer",
                "description": "Max hits to return (capped at 200).",
            },
        },
        "required": ["pattern"],
    },
    run=_run_grep_code,
)


# ─────────────────────────────────────────────────────────────────────
# list_files
# ─────────────────────────────────────────────────────────────────────

def _run_list_files(params: dict, ctx: AgentContext) -> dict:
    rel_dir = params.get("directory", ".") or "."
    recursive = bool(params.get("recursive", False))
    max_entries = min(int(params.get("max_entries", ctx.max_list_entries)
                          or ctx.max_list_entries),
                      ctx.max_list_entries)

    if rel_dir.startswith("/") or ".." in Path(rel_dir).parts:
        return {"error": f"invalid directory: {rel_dir!r}"}

    target = ctx.project_dir / rel_dir
    if not ctx.is_inside_sandbox(target):
        return {"error": f"directory escapes sandbox: {rel_dir}"}
    if not target.exists():
        return {"error": f"directory not found: {rel_dir}"}
    if not target.is_dir():
        return {"error": f"not a directory: {rel_dir}"}

    entries: list[dict] = []
    iterator = target.rglob("*") if recursive else target.iterdir()
    for entry in iterator:
        if any(part in EXCLUDED_DIRS for part in entry.parts):
            continue
        try:
            rel = entry.relative_to(ctx.project_dir)
        except ValueError:
            continue
        entries.append(
            {
                "path": str(rel),
                "type": "dir" if entry.is_dir() else "file",
            }
        )
        if len(entries) >= max_entries:
            break
    capped = len(entries) >= max_entries
    return {
        "directory": rel_dir,
        "recursive": recursive,
        "entries": entries,
        "entry_count": len(entries),
        "capped": capped,
    }


LIST_FILES = Tool(
    name="list_files",
    description=(
        "List files and directories under a relative path. Non-recursive by "
        "default. Excludes common noise (node_modules, .git, build, "
        ".dart_tool, etc). Use this to discover the project structure when "
        "you don't yet know specific filenames; prefer grep_code if you "
        "already have a hint."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Relative path from project root. Default '.'.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Recurse into subdirectories. Default false.",
            },
            "max_entries": {
                "type": "integer",
                "description": "Max entries to return (capped at 200).",
            },
        },
        "required": ["directory"],
    },
    run=_run_list_files,
)


# ─────────────────────────────────────────────────────────────────────
# http_request (Feature A only — never registered for Phase 1 agent)
# ─────────────────────────────────────────────────────────────────────

def _run_http_request(params: dict, ctx: AgentContext) -> dict:
    if ctx.app_url is None:
        return {"error": "http_request not available in this context (no app_url configured)"}

    if ctx._http_count >= ctx.max_http_calls:
        return {"error": f"http_request budget exceeded ({ctx.max_http_calls} calls)"}

    method = (params.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return {"error": f"invalid method: {method}"}

    path = params.get("path", "")
    if not path or path.startswith(("http://", "https://")):
        return {"error": "path must be a relative path (no scheme)"}
    if ".." in path:
        return {"error": "path may not contain '..'"}
    if not path.startswith("/"):
        path = "/" + path

    body = params.get("body")
    use_auth = bool(params.get("use_auth", True))

    # Anti-loop: refuse the 3rd identical call.
    body_repr = json.dumps(body, sort_keys=True) if body is not None else ""
    sig = hashlib.md5(f"{method}|{path}|{body_repr}".encode()).hexdigest()
    seen = ctx._http_seen.get(sig, 0)
    if seen >= 2:
        return {
            "status": 0,
            "error": (
                "REFUSED: this exact request was already attempted twice. "
                "Read the previous responses, change the payload meaningfully, "
                "or call ask_user."
            ),
        }
    ctx._http_seen[sig] = seen + 1
    ctx._http_count += 1

    import requests  # local import — only Feature A pulls this in

    url = ctx.app_url.rstrip("/") + path
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if use_auth and ctx.auth_token:
        headers["Authorization"] = f"Bearer {ctx.auth_token}"

    try:
        resp = requests.request(
            method,
            url,
            json=body if body is not None else None,
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        return {"status": 0, "error": f"network error: {e}"}

    # Parse JSON if possible, otherwise return text (truncated)
    parsed_body: Any
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            parsed_body = _redact_sensitive(resp.json())
        except ValueError:
            parsed_body = resp.text[:8000]
    else:
        parsed_body = resp.text[:8000]

    # Only echo a few headers, all redacted
    safe_headers = {
        k: v for k, v in resp.headers.items()
        if not SENSITIVE_KEY_PATTERN.search(k)
    }

    return {
        "status": resp.status_code,
        "headers": dict(list(safe_headers.items())[:10]),
        "body": parsed_body,
    }


HTTP_REQUEST = Tool(
    name="http_request",
    description=(
        "Send an HTTP request to the app under audit. The base URL and bearer "
        "token are configured by the harness — supply only `method`, `path`, "
        "and optional JSON `body`. **400 responses typically contain validation "
        "errors listing the missing or invalid fields — read the body carefully "
        "and retry with a corrected payload.** Calling the same exact request "
        "more than twice in a row is refused; change the payload meaningfully "
        "between attempts. Total budget per session: 40 calls."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
            },
            "path": {
                "type": "string",
                "description": "Path only (no scheme), e.g. '/api/items'. May not contain '..'.",
            },
            "body": {
                "type": "object",
                "description": "JSON body for POST/PUT/PATCH. Omit for GET/DELETE.",
            },
            "use_auth": {
                "type": "boolean",
                "description": "Include the bearer token. Default true.",
            },
        },
        "required": ["method", "path"],
    },
    run=_run_http_request,
)


# ─────────────────────────────────────────────────────────────────────
# ask_user
# ─────────────────────────────────────────────────────────────────────

def _run_ask_user(params: dict, ctx: AgentContext) -> dict:
    question = params.get("question", "").strip()
    if not question:
        return {"error": "question is required"}
    choices = params.get("choices") or None

    if not ctx.interactive:
        return {
            "answer": None,
            "note": (
                "non-interactive mode: cannot prompt the user. "
                "Proceed with your best guess."
            ),
        }

    # Anti-begging: refuse if the same question was asked recently.
    if question in ctx._ask_history[-3:]:
        return {
            "answer": None,
            "note": "you already asked this exact question recently; do not repeat.",
        }
    ctx._ask_history.append(question)

    import click

    ctx.console.print()
    ctx.console.print(f"[bold cyan]Agent question:[/bold cyan] {question}")
    if choices and isinstance(choices, list):
        for i, ch in enumerate(choices, start=1):
            ctx.console.print(f"  [{i}] {ch}")
        try:
            answer = click.prompt(
                "Your choice (number or free text)", default="", show_default=False
            )
        except (click.Abort, EOFError):
            return {"answer": None, "note": "user aborted"}
        # Map numeric answers to the corresponding choice text
        if answer.strip().isdigit():
            idx = int(answer.strip()) - 1
            if 0 <= idx < len(choices):
                answer = choices[idx]
    else:
        try:
            answer = click.prompt("Your answer", default="", show_default=False)
        except (click.Abort, EOFError):
            return {"answer": None, "note": "user aborted"}
    return {"answer": answer}


ASK_USER = Tool(
    name="ask_user",
    description=(
        "Ask the human operator a clarification question when the source code "
        "is genuinely ambiguous and reading more would not help. Use sparingly "
        "— prefer reading more code first. If `choices` is provided, the user "
        "picks one. In non-interactive environments (CI, daemons) the call "
        "returns no answer and you must proceed with your best guess. The same "
        "exact question will not be asked twice in a row."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to display to the user.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of pre-defined choices.",
            },
        },
        "required": ["question"],
    },
    run=_run_ask_user,
)


# ─────────────────────────────────────────────────────────────────────
# submit_result — the canonical "I am done" signal
# ─────────────────────────────────────────────────────────────────────

def _run_submit_result(params: dict, ctx: AgentContext) -> dict:
    """Marker tool: the loop runner intercepts this call before invoking _run."""
    return {"received": True}


SUBMIT_RESULT = Tool(
    name="submit_result",
    description=(
        "Call this exactly ONCE when you have a complete, validated final "
        "answer. The harness stops as soon as this is called. The `result` "
        "argument must be the full JSON object the user asked for — do not "
        "wrap it in extra prose or markdown."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "description": "The final result payload. Schema is task-specific.",
            },
        },
        "required": ["result"],
    },
    run=_run_submit_result,
)


# ─────────────────────────────────────────────────────────────────────
# Tool registries — one per feature
# ─────────────────────────────────────────────────────────────────────

READONLY_TOOLS: list[Tool] = [READ_FILE, GREP_CODE, LIST_FILES, ASK_USER, SUBMIT_RESULT]
"""Tools available to read-only agents (Phase 1 agentic mode)."""

LIVE_BACKEND_TOOLS: list[Tool] = [
    READ_FILE,
    GREP_CODE,
    LIST_FILES,
    HTTP_REQUEST,
    ASK_USER,
    SUBMIT_RESULT,
]
"""Tools available to agents that may probe the live backend (setup-test-data)."""


def serialize_tools(tools: list[Tool]) -> list[dict]:
    """Convert a tool list to the JSON form expected by Anthropic's tools= parameter."""
    return [t.to_anthropic() for t in tools]


def find_tool(tools: list[Tool], name: str) -> Tool | None:
    """Look up a tool by name (exact match)."""
    for t in tools:
        if t.name == name:
            return t
    return None


def format_tool_result(value: Any) -> str:
    """Convert a tool's return value to the string the model receives, capped."""
    if isinstance(value, str):
        return _truncate(value)
    return _truncate(json.dumps(value, ensure_ascii=False, default=str))
