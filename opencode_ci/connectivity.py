"""Read-only connection checks. Never open a session database or call /events."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .auth import EntraAuth, normalize_resource
from .upload import NoRedirect, normalize_url


def _get(opener, url, *, headers=None, identity=False):
    request = Request(url, headers={"Accept": "application/json", **(headers or {})}, method="GET")
    try:
        with opener.open(request, timeout=10) as response:
            code = response.status
            data = None
            if identity and 200 <= code < 300:
                # Identity bodies should be tiny. Never return arbitrary server
                # response text, account details, or headers to the browser.
                raw = response.read(65537)
                if len(raw) <= 65536:
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeError):
                        pass
        return {"http_status": code, "reachable": True, "data": data}
    except HTTPError as exc:
        code = exc.code
        exc.close()
        return {"http_status": code, "reachable": True, "data": None}
    except (URLError, OSError, ValueError):
        return {"http_status": None, "reachable": False, "data": None}


def _health_result(health, *, authenticated):
    code = health["http_status"]
    ok = code is not None and 200 <= code < 300
    if ok:
        message = f"Reachable (HTTP {code})"
    elif authenticated and code in (401, 403):
        message = f"/status rejected the authenticated request (HTTP {code})"
    elif health["reachable"]:
        message = f"Reachable; /status returned HTTP {code}"
    else:
        message = "Could not reach /status (network, DNS, or TLS failure)"
    return {"ok": ok, "reachable": health["reachable"], "http_status": code,
            "authenticated_request": authenticated, "message": message}


def test_connection(url, auth_resource, *, auth_mode="entra", api_key_env="AMPLIFIER_CONTEXT_INTELLIGENCE_API_KEY"):
    url = normalize_url(url)
    resource = normalize_resource(auth_resource)
    if auth_mode == "entra" and not resource:
        raise ValueError("Enter auth_resource before testing the connection")
    if auth_mode not in ("entra", "static"):
        raise ValueError("Unsupported authentication mode")
    opener = build_opener(NoRedirect())
    health = None
    auth = EntraAuth(resource) if auth_mode == "entra" else None
    try:
        def headers():
            if auth is not None:
                return auth.headers()
            token = os.environ.get(api_key_env, "")
            if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
                raise ValueError("Static bearer credential unavailable")
            return {"Authorization": "Bearer " + token}

        try:
            request_headers = headers()
            # Gateways may protect /status even if the CI server itself doesn't.
            # Use the same bearer authentication as the actual identity check.
            health = _get(opener, url + "/status", headers=request_headers)
            if health["http_status"] == 401 and auth is not None:
                auth.invalidate()
                request_headers = headers()
                health = _get(opener, url + "/status", headers=request_headers)
            identity = _get(opener, url + "/whoami", headers=request_headers, identity=True)
            if identity["http_status"] == 401 and auth is not None:
                auth.invalidate()
                identity = _get(opener, url + "/whoami", headers=headers(), identity=True)
        except ValueError:
            # If no token can be obtained, an unauthenticated GET can still
            # distinguish an unreachable host from a credential acquisition fault.
            authenticated = health is not None
            if health is None:
                health = _get(opener, url + "/status")
            return {"success": False, "server": _health_result(health, authenticated=authenticated), "authentication": {
                "ok": False, "http_status": None,
                "message": "Could not acquire an Azure token; run az login and check auth_resource" if auth is not None else "Static bearer credential unavailable",
            }}
        code = identity["http_status"]
        data = identity["data"]
        verified = (code is not None and 200 <= code < 300 and isinstance(data, dict)
                    and isinstance(data.get("contributor_id"), str) and bool(data["contributor_id"].strip()))
        if verified:
            message = f"Verified (HTTP {code})"
        elif code in (404, 405):
            message = f"Not verified; /whoami is not exposed (HTTP {code})"
        elif code in (401, 403):
            message = f"Rejected (HTTP {code}); check identity, audience, and API access"
        elif code is not None and 200 <= code < 300:
            message = "Not verified; /whoami did not confirm an authenticated identity"
        elif code is not None:
            message = f"Not verified; /whoami returned HTTP {code}"
        else:
            message = "Could not reach /whoami (network, DNS, or TLS failure)"
        server = _health_result(health, authenticated=True)
        return {"success": server["ok"] and verified, "server": server,
                "authentication": {"ok": verified, "http_status": code, "message": message}}
    finally:
        if auth is not None:
            auth.close()
