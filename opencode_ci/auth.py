"""Azure bearer authentication. Credentials and cached tokens never reach disk/UI."""

from __future__ import annotations

import threading
import time


def normalize_resource(value):
    if not isinstance(value, str):
        raise ValueError("auth_resource must be a string")
    value = value.strip().rstrip("/")
    if not value:
        return ""
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    if (parsed.scheme not in ("api", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(c.isspace() for c in value) or value.endswith("/.default")):
        raise ValueError("Enter an api:// or https:// resource URI, without credentials, query, or /.default scope suffix")
    return value


def _create_credential():
    # Same credential chain used by Amplifier, including the existing az login
    # session. Construct lazily: saving settings must never initiate auth.
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential()


class EntraAuth:
    def __init__(self, resource, *, credential=None):
        self.resource = normalize_resource(resource)
        self._credential = credential
        self._token = ""
        self._expires_on = 0
        self._lock = threading.Lock()

    def headers(self):
        if not self.resource:
            raise ValueError("Configure auth_resource before uploading")
        with self._lock:
            if not self._token or time.time() >= self._expires_on - 300:
                try:
                    if self._credential is None:
                        self._credential = _create_credential()
                    token = self._credential.get_token(self.resource + "/.default")
                    if not token.token or float(token.expires_on) <= time.time():
                        raise ValueError("Unusable token")
                    self._token = token.token
                    self._expires_on = float(token.expires_on)
                except Exception:
                    # Credential exceptions may include tenant, account, endpoint,
                    # or token details. Never expose them to the UI or logs here.
                    raise ValueError("Azure authentication failed. Run az login on the machine running this app and verify the configured auth_resource and endpoint permissions.") from None
            return {"Authorization": "Bearer " + self._token}

    def invalidate(self):
        with self._lock:
            self._token = ""
            self._expires_on = 0

    def close(self):
        with self._lock:
            self._token = ""
            self._expires_on = 0
            if self._credential is not None:
                try:
                    self._credential.close()
                except Exception:
                    pass  # Cleanup must not expose credential internals or prevent a settings change.
                finally:
                    self._credential = None
