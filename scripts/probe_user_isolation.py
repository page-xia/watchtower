"""Safely smoke-test anonymous-principal isolation against a running API.

The probe deliberately uses fresh random client IDs, never prints them, and
removes only the watchlist entry that it created for the first client.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _new_client_id() -> str:
    # token_urlsafe uses only the server's accepted URL/header-safe alphabet.
    return secrets.token_urlsafe(18).replace("=", "")


def _validate_base_url(value: str) -> str:
    base_url = str(value or "").rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--base-url must be an absolute http(s) URL")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("non-local probes must use HTTPS")
    return base_url


def _request(
    base_url: str,
    method: str,
    path: str,
    client_id: str,
    *,
    payload: dict[str, Any] | None = None,
    expected_revision: int | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    headers = {"X-Client-ID": client_id, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if expected_revision is not None:
        headers["X-Expected-Revision"] = str(expected_revision)
    request = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is supplied by deployment operator.
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw) if raw else {}
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:200]}
        return int(error.code), body


def _expect(status: int, body: dict[str, Any], expected: int, operation: str) -> dict[str, Any]:
    if status != expected:
        raise RuntimeError(f"{operation} returned HTTP {status}: {body.get('error') or body.get('detail') or 'unexpected response'}")
    return body


def run_probe(base_url: str, code: str, timeout: float) -> None:
    alice = _new_client_id()
    bob = _new_client_id()
    if len(code) != 6 or not code.isdigit():
        raise ValueError("--code must be a six-digit A-share code")

    print(f"isolation probe: A={_digest(alice)} B={_digest(bob)}")
    created = False
    alice_revision: int | None = None
    try:
        status, alice_state = _request(base_url, "GET", "/api/watchlist", alice, timeout=timeout)
        _expect(status, alice_state, 200, "read A watchlist")
        status, bob_state = _request(base_url, "GET", "/api/watchlist", bob, timeout=timeout)
        _expect(status, bob_state, 200, "read B watchlist")
        if alice_state.get("items") or bob_state.get("items"):
            raise RuntimeError("fresh probe principals unexpectedly already contain personal state")

        status, added = _request(
            base_url,
            "POST",
            "/api/watchlist",
            alice,
            payload={"code": code, "name": "隔离验收探针"},
            expected_revision=int(alice_state.get("revision", 0)),
            timeout=timeout,
        )
        _expect(status, added, 200, "add A watchlist")
        created = True
        alice_revision = int(added.get("revision", 0))
        if code not in [str(item.get("code") or "") for item in added.get("items", [])]:
            raise RuntimeError("A did not receive its just-created watchlist item")

        status, bob_after = _request(base_url, "GET", "/api/watchlist", bob, timeout=timeout)
        _expect(status, bob_after, 200, "re-read B watchlist")
        if any(str(item.get("code") or "") == code for item in bob_after.get("items", [])):
            raise RuntimeError("cross-user watchlist leak: B received A's item")

        status, terminal_a = _request(base_url, "GET", "/api/dashboard?view=terminal&page_size=20", alice, timeout=timeout)
        _expect(status, terminal_a, 200, "read A terminal")
        status, terminal_b = _request(base_url, "GET", "/api/dashboard?view=terminal&page_size=20", bob, timeout=timeout)
        _expect(status, terminal_b, 200, "read B terminal")
        a_codes = [str(value) for value in terminal_a.get("watchlist_codes", [])]
        b_codes = [str(value) for value in terminal_b.get("watchlist_codes", [])]
        if code not in a_codes or code in b_codes:
            raise RuntimeError("terminal personalization is not isolated")

        print("isolation probe passed")
    finally:
        if created:
            status, body = _request(
                base_url,
                "DELETE",
                f"/api/watchlist/{code}",
                alice,
                expected_revision=alice_revision,
                timeout=timeout,
            )
            if status != 200:
                print(
                    f"warning: cleanup for A={_digest(alice)} returned HTTP {status}: "
                    f"{body.get('error') or body.get('detail') or 'unexpected response'}",
                    file=sys.stderr,
                )
            else:
                print("probe cleanup completed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify two-client watchlist isolation")
    parser.add_argument("--base-url", required=True, help="HTTPS production URL or local http://127.0.0.1 URL")
    parser.add_argument("--code", default="000001", help="six-digit temporary watchlist code (default: 000001)")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-request timeout seconds")
    parser.add_argument("--dry-run", action="store_true", help="validate arguments only; do not make requests or writes")
    args = parser.parse_args(argv)
    try:
        base_url = _validate_base_url(args.base_url)
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        if args.dry_run:
            print(f"dry run: validated {base_url}; no requests or writes were made")
            return 0
        run_probe(base_url, str(args.code).strip(), args.timeout)
        return 0
    except (ValueError, RuntimeError, URLError, OSError) as error:
        print(f"isolation probe failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - command-line entry point
    raise SystemExit(main())
