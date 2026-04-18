#!/usr/bin/env bash
#
# Sync GitLab master to GitHub main, with secret-leak checks.
#
# Runs inside a workspace that has both `origin` (GitLab) and `github`
# remotes. If started from anywhere else, falls back to ~/dev/.figma-audit-github.
#
# Usage:
#   scripts/sync_to_github.sh           # interactive confirm before push
#   scripts/sync_to_github.sh --yes     # skip confirm (for CI / automation)
#
set -euo pipefail

# --- Locate the dual-remote mirror -----------------------------------------

if ! git remote 2>/dev/null | grep -qx github; then
    MIRROR="${HOME}/dev/.figma-audit-github"
    if [[ ! -d "${MIRROR}/.git" ]]; then
        echo "error: cannot find mirror repo (${MIRROR})" >&2
        exit 1
    fi
    cd "${MIRROR}"
fi

for r in origin github; do
    git remote get-url "$r" >/dev/null 2>&1 || {
        echo "error: remote '$r' not configured in $(pwd)" >&2
        exit 1
    }
done

# --- Fetch both sides -------------------------------------------------------

echo ">> fetching origin (GitLab)"
git fetch --tags origin master
echo ">> fetching github"
git fetch --tags github main

BEFORE=$(git rev-parse github/main)
AFTER=$(git rev-parse origin/master)

if [[ "$BEFORE" == "$AFTER" ]]; then
    echo ">> already in sync at ${BEFORE:0:8}. nothing to do."
    exit 0
fi

COUNT=$(git rev-list --count "${BEFORE}..${AFTER}")
echo ">> ${COUNT} commit(s) to sync:"
git log --oneline "${BEFORE}..${AFTER}"
echo

# --- Secret-leak checks -----------------------------------------------------

# Patterns: GitLab PAT, GitHub classic + fine-grained, PyPI, Anthropic/OpenAI, AWS.
PATTERN='(glpat-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|pypi-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})'

if command -v gitleaks >/dev/null 2>&1; then
    echo ">> gitleaks scan on ${BEFORE:0:8}..${AFTER:0:8}"
    gitleaks detect --no-banner --redact \
        --log-opts="${BEFORE}..${AFTER}"
else
    echo ">> gitleaks not installed — relying on pattern grep"
    echo "   install with: go install github.com/gitleaks/gitleaks/v8@latest"
fi

echo ">> pattern grep on diff of ${BEFORE:0:8}..${AFTER:0:8}"
DIFF_HITS=$(git log -p --no-color "${BEFORE}..${AFTER}" \
    | grep -nE "$PATTERN" || true)
if [[ -n "$DIFF_HITS" ]]; then
    echo "error: suspicious token pattern in commit range:" >&2
    echo "$DIFF_HITS" | head -20 >&2
    exit 2
fi

echo ">> pattern grep on tracked files at origin/master"
TREE_HITS=$(git grep -nE "$PATTERN" origin/master -- \
    || true)
if [[ -n "$TREE_HITS" ]]; then
    echo "error: suspicious token pattern in tracked files:" >&2
    echo "$TREE_HITS" | head -20 >&2
    exit 2
fi

# The GitLab origin URL legitimately stores an oauth2 token. Strip that single
# line before scanning so the scan stays meaningful.
echo ">> .git/config scan (ignoring legit GitLab oauth2 URL)"
CFG_HITS=$(grep -vE '^\s*url\s*=\s*https://oauth2:glpat-[^@]+@git\.gardendwarf\.org/' .git/config \
    | grep -nE "$PATTERN" || true)
if [[ -n "$CFG_HITS" ]]; then
    echo "error: suspicious pattern in .git/config:" >&2
    echo "$CFG_HITS" >&2
    exit 2
fi

# --- Push -------------------------------------------------------------------

echo
echo ">> all secret-leak checks passed."

if [[ "${1:-}" != "--yes" ]]; then
    read -rp ">> push ${BEFORE:0:8}..${AFTER:0:8} to github/main? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || { echo "aborted."; exit 0; }
fi

echo ">> fast-forward main to origin/master"
git checkout main
git merge --ff-only origin/master

echo ">> pushing main + tags to github"
git push github main
git push github --tags

echo ">> done. github/main is now at ${AFTER:0:8}."
