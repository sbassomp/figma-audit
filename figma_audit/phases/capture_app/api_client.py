"""HTTP client used by Phase 4 to authenticate and seed test data.

The Phase 1 AI is unreliable on two fronts: it sometimes omits the API
prefix (e.g. emits ``/auth/login`` when the real endpoint is
``/api/auth/login``), and it sometimes guesses field names that do not
match the actual request DTO. This module compensates for both:

- :func:`_endpoint_variants` and :func:`_api_request_with_prefix_fallback`
  retry each call with common API prefix variants on 404/405.
- :func:`_setup_test_data` reports validation errors verbatim so the user
  can either fix the manifest or override ``test_setup`` in the YAML.
"""

from __future__ import annotations

import base64
import json

from rich.console import Console

from figma_audit.config import Account, TestSetup
from figma_audit.phases.capture_app.templates import (
    _extract_path,
    _resolve_payload,
    _resolve_template,
)

console = Console()


def _extract_jwt_sub(token: str) -> str | None:
    """Return the ``sub`` claim of a JWT without verifying its signature.

    Used after ``_api_login`` to expose each authenticated account's
    stable user identifier (typically the Keycloak user UUID) to
    template substitution. The resulting value lands in ``test_data``
    as ``<role>_user_id`` so navigation URLs like ``/profile/:userId``
    can template ``/profile/${driver_user_id}`` instead of leaving a
    hallucinated placeholder that leaks into captures.

    Parsing is intentionally minimal and never raises: an invalid or
    unparsable token just returns ``None`` and the caller continues.
    """
    if not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        # Base64url decoding requires padding to a multiple of 4.
        padded = payload + "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    sub = data.get("sub")
    return str(sub) if sub else None


def _pre_auth_accounts(
    base_url: str,
    test_setup_dict: dict,
    accounts: dict[str, Account],
) -> dict[str, str]:
    """Authenticate every declared account once and return role → token.

    The shared ``test_setup_dict`` is mutated so the API prefix hint
    learned during the first successful login is reused for the others.
    Accounts whose login fails are omitted from the returned map; the
    caller decides what to do (skip steps tagged with that role, or
    abort the run).
    """
    tokens: dict[str, str] = {}
    for role, account in accounts.items():
        if not account.email:
            console.print(f"    [yellow]Account '{role}' has no email, skipping[/yellow]")
            continue
        creds = {"email": account.email, "otp": account.otp}
        token = _api_login(base_url, test_setup_dict, creds)
        if token:
            tokens[role] = token
            console.print(f"    [green]'{role}' authenticated[/green] ({account.email})")
        else:
            console.print(f"    [red]'{role}' login failed[/red] ({account.email})")
    return tokens


def _run_setup_dag(
    base_url: str,
    setup: TestSetup,
    tokens: dict[str, str],
    test_data: dict,
    test_setup_dict: dict,
) -> list[str]:
    """Execute the multi-actor seed DAG before capture.

    Runs :meth:`TestSetup.topological_order` and for each step:

    - Skips steps whose ``as_role`` has no registered token (the account
      failed to log in upstream). A warning is printed and the step
      becomes a no-op.
    - Resolves ``${key}`` templates in the endpoint and payload against
      the current ``test_data`` (which contains credentials plus every
      value saved by earlier steps).
    - Calls the endpoint via :func:`_api_request_with_prefix_fallback`
      with the step's bearer token.
    - On success, extracts response values into ``test_data`` per the
      step's ``save`` map so later steps and page URLs can template
      them.

    Returns the list of step names that completed successfully, for
    reporting and for deciding whether downstream captures can run.
    """
    completed: list[str] = []
    prefix = test_setup_dict.get("_api_prefix_hint", "")

    ordered = setup.topological_order()
    if not ordered:
        return completed

    console.print(f"  [bold]Running {len(ordered)} seed step(s) via API...[/bold]")
    for step in ordered:
        token = tokens.get(step.as_role)
        if not token:
            console.print(
                f"    [yellow]Skipping '{step.name}': no token for role '{step.as_role}'[/yellow]"
            )
            continue

        endpoint = _resolve_template(step.endpoint, test_data)
        payload = _resolve_payload(step.payload, test_data) if step.payload else None
        headers = {"Authorization": f"Bearer {token}"}

        try:
            resp, _used = _api_request_with_prefix_fallback(
                step.method,
                base_url,
                f"{prefix}{endpoint}" if prefix else endpoint,
                json=payload,
                headers=headers,
                timeout=15,
            )
        except Exception as e:
            console.print(f"    [yellow]Step '{step.name}' error: {e}[/yellow]")
            continue

        if resp.status_code not in (200, 201, 202, 204):
            console.print(
                f"    [yellow]Step '{step.name}' failed ({resp.status_code}): "
                f"{resp.text[:150]}[/yellow]"
            )
            continue

        body: dict = {}
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        for key, json_path in step.save.items():
            value = _extract_path(body, json_path) if isinstance(body, dict) else ""
            if value:
                test_data[key] = value

        completed.append(step.name)
        saved = ", ".join(f"{k}={test_data.get(k, '?')}" for k in step.save) or "no save"
        console.print(f"    [green]'{step.name}' ({step.as_role}): OK[/green] — {saved}")

    console.print(f"  [green]{len(completed)}/{len(ordered)} seed step(s) completed[/green]")
    return completed


def _endpoint_variants(endpoint: str) -> list[str]:
    """Return candidate paths to try when an endpoint may be missing the API prefix.

    Phase 1's AI often reads endpoint strings from request handlers but
    misses the ``baseUrl`` prefix set on the HTTP client (dio
    ``BaseOptions``, Retrofit ``@BaseUrl``, etc.). Rather than re-running
    Phase 1, we transparently retry each endpoint with common API prefixes
    when the server returns 404/405.
    """
    if not endpoint:
        return [endpoint]
    variants = [endpoint]
    for prefix in ("/api", "/v1", "/api/v1"):
        if not endpoint.startswith(prefix + "/"):
            variants.append(f"{prefix}{endpoint}")
    return variants


def _api_request_with_prefix_fallback(
    method: str,
    base_url: str,
    endpoint: str,
    **kwargs,
):
    """POST/PUT/GET the endpoint, retrying with API prefix variants on 404/405.

    Returns the first successful response (non-404/405), or the last
    response if all variants failed. The caller still checks the final
    status code.
    """
    import requests

    last_resp = None
    for variant in _endpoint_variants(endpoint):
        resp = requests.request(method, f"{base_url}{variant}", **kwargs)
        last_resp = resp
        if resp.status_code not in (404, 405):
            return resp, variant
    return last_resp, endpoint


def _api_login(base_url: str, test_setup: dict, credentials: dict) -> str | None:
    """Authenticate via the app API using manifest config. Returns bearer token."""
    auth_endpoint = test_setup.get("auth_endpoint", "")
    if not auth_endpoint:
        return None

    payload = _resolve_payload(test_setup.get("auth_payload", {}), credentials)

    try:
        otp_endpoint = test_setup.get("auth_otp_request_endpoint")
        if otp_endpoint:
            _api_request_with_prefix_fallback(
                "POST",
                base_url,
                otp_endpoint,
                json={"email": credentials.get("email")},
                timeout=10,
            )
        resp, used_path = _api_request_with_prefix_fallback(
            "POST", base_url, auth_endpoint, json=payload, timeout=10
        )
        if resp.status_code != 200:
            console.print(
                f"    [yellow]API login failed ({resp.status_code}) on {used_path}: "
                f"{resp.text[:100]}[/yellow]"
            )
            return None
        if used_path != auth_endpoint:
            # Persist the working prefix on the test_setup dict so subsequent
            # seed_items calls use it directly without re-probing.
            test_setup["_api_prefix_hint"] = used_path[: len(used_path) - len(auth_endpoint)]
            console.print(
                f"    [dim]Using API prefix '{test_setup['_api_prefix_hint']}' "
                f"(manifest endpoints missing it)[/dim]"
            )
        token_path = test_setup.get("auth_token_path", "accessToken")
        token = _extract_path(resp.json(), token_path)
        if not token:
            console.print(f"    [yellow]No token at '{token_path}'[/yellow]")
            return None
        return token
    except Exception as e:
        console.print(f"    [yellow]API login error: {e}[/yellow]")
        return None


def _setup_test_data(
    app_url: str,
    test_data: dict,
    test_setup: dict,
    seed_account: dict | None = None,
) -> tuple[list[str], str | None]:
    """Create test data via manifest-driven API calls.

    Reads ``test_setup`` from the manifest (or YAML override) to know
    which endpoints to call. Returns ``(created_item_ids, taken_item_id)``.
    """
    base = app_url.rstrip("/")

    if not test_setup or not test_setup.get("seed_items"):
        return [], None

    console.print("  [bold]Setting up test data via API...[/bold]")

    # Login with seed account (items created by seed appear as "available" for main user)
    seed_creds = seed_account or test_data
    token = _api_login(base, test_setup, seed_creds)
    if not token:
        return [], None
    console.print("    API login OK (seed)")
    headers = {"Authorization": f"Bearer {token}"}

    # Create seed items from manifest config.
    # Apply the API prefix hint learned during login (if any) before trying.
    api_prefix_hint = test_setup.get("_api_prefix_hint", "")
    created_ids: list[str] = []
    for i, item_spec in enumerate(test_setup["seed_items"]):
        endpoint = _resolve_template(item_spec.get("endpoint", ""), test_data)
        method = item_spec.get("method", "POST").upper()
        payload = _resolve_payload(item_spec.get("payload", {}), test_data)
        id_path = item_spec.get("id_path", "id")
        td_key = item_spec.get("test_data_key", f"item_{i}")

        try:
            resp, _used = _api_request_with_prefix_fallback(
                method,
                base,
                f"{api_prefix_hint}{endpoint}" if api_prefix_hint else endpoint,
                json=payload,
                headers=headers,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                item_id = _extract_path(resp.json(), id_path)
                if item_id:
                    created_ids.append(item_id)
                    test_data[td_key] = item_id
                console.print(f"    Item {i + 1} created ({td_key}={item_id})")
            else:
                console.print(
                    f"    [yellow]Item {i + 1} failed ({resp.status_code}): "
                    f"{resp.text[:100]}[/yellow]"
                )
        except Exception as e:
            console.print(f"    [yellow]Item {i + 1} error: {e}[/yellow]")

    console.print(f"  [green]{len(created_ids)} test item(s) created[/green]")

    # Take the first item with the MAIN user (if configured in manifest)
    taken_id = None
    take_spec = test_setup.get("take_item")
    if take_spec and created_ids:
        main_email = test_data.get("email")
        seed_email = (seed_account or {}).get("email")
        if main_email and main_email != seed_email:
            main_token = _api_login(base, test_setup, test_data)
            if main_token:
                cid = created_ids[0]
                merged = {**test_data, "item_id": cid}
                endpoint = _resolve_template(take_spec.get("endpoint", ""), merged)
                td_key = take_spec.get("test_data_key", "item_taken_id")
                prefix = test_setup.get("_api_prefix_hint", "")
                try:
                    resp, _used = _api_request_with_prefix_fallback(
                        take_spec.get("method", "POST"),
                        base,
                        f"{prefix}{endpoint}" if prefix else endpoint,
                        headers={"Authorization": f"Bearer {main_token}"},
                        timeout=10,
                    )
                    if resp.status_code in (200, 201):
                        taken_id = cid
                        test_data[td_key] = cid
                        console.print(f"    Item {cid} taken ({td_key})")
                    else:
                        console.print(f"    [yellow]Take failed ({resp.status_code})[/yellow]")
                except Exception as e:
                    console.print(f"    [yellow]Take error: {e}[/yellow]")

    return created_ids, taken_id


def _cleanup_test_data(
    app_url: str,
    test_data: dict,
    test_setup: dict,
    item_ids: list[str],
) -> None:
    """Clean up test items via manifest-configured API endpoint."""
    if not item_ids or not test_setup:
        return

    import requests

    base = app_url.rstrip("/")
    cleanup_endpoint = test_setup.get("cleanup_endpoint")
    if not cleanup_endpoint:
        return

    token = _api_login(base, test_setup, test_data)
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}"}

    for item_id in item_ids:
        try:
            endpoint = _resolve_template(cleanup_endpoint, {"item_id": item_id})
            requests.post(f"{base}{endpoint}", headers=headers, timeout=10)
        except Exception as e:
            console.print(f"  [dim]Cleanup {item_id} failed: {e}[/dim]")

    console.print(f"  [dim]Cleaned up {len(item_ids)} test item(s)[/dim]")
