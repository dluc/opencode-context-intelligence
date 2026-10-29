"""Incremental CI /events uploader with per-event durable local receipts.

Uses the official aci-event-v1 wire contract, never replay=true. Accepted events
are never resent by this installation, including after app restarts. A lost HTTP
response is inherently ambiguous: retries reuse exactly the same idempotency key.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import ssl
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .exporter import canonical
from .auth import EntraAuth, normalize_resource


def normalize_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("Destination must be an HTTPS base URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Destination must not contain credentials, a query, or a fragment")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Plain HTTP is permitted only for a loopback test server")
    path = parsed.path.rstrip("/")
    if path.endswith("/events"):
        raise ValueError("Configure the base URL, not the /events endpoint")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def build_payload(record, working_dir):
    envelope = {"event": record["event"], "workspace": record["workspace"], "data": record["data"]}
    # Match the official hook's JSON defaults EXACTLY (including ensure_ascii=True).
    serialized = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    return {**envelope, "working_dir": working_dir,
            "idempotency_key": "aci-event-v1:" + hashlib.sha256(serialized.encode()).hexdigest()}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # Never forward a bearer credential to a redirected endpoint.


class Uploader:
    def __init__(self, state: Path, server_url="", *, api_key_env="AMPLIFIER_CONTEXT_INTELLIGENCE_API_KEY", auth_resource="", auth_mode=None):
        self.state = state
        self.server_url = normalize_url(server_url) if server_url else ""
        self.api_key_env = api_key_env
        self.auth_resource = normalize_resource(auth_resource)
        self.auth_mode = auth_mode or ("entra" if self.auth_resource else "static")
        if self.auth_mode not in ("entra", "static"):
            raise ValueError("auth_mode must be entra or static")
        self.auth = EntraAuth(self.auth_resource)
        self.lock = threading.Lock()
        self.opener = build_opener(NoRedirect())

    @property
    def available(self):
        # Configured is not proof of successful authentication. Tokens are only
        # acquired on an explicit upload, never on a settings GET/save.
        return bool(self.server_url and (self.auth_resource if self.auth_mode == "entra" else os.environ.get(self.api_key_env)))

    def configure(self, url, auth_resource, *, persist):
        url = normalize_url(url) if url else ""
        resource = normalize_resource(auth_resource)
        if not self.lock.acquire(blocking=False):
            raise ValueError("Wait for the current upload to finish before changing connection settings")
        try:
            persist({"url": url, "auth_resource": resource})
            self.auth.close()
            self.server_url = url
            self.auth_resource = resource
            self.auth = EntraAuth(resource)
        finally:
            self.lock.release()

    def _ledger(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.state / "uploads.db"
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        db = sqlite3.connect(path, timeout=5)
        # A separate POSIX file lock prevents interleaved deliveries by two apps;
        # this connection commits each accepted receipt independently.
        db.execute("""CREATE TABLE IF NOT EXISTS delivery (
            endpoint TEXT,event_id TEXT,digest TEXT,status TEXT,attempted_at REAL,
            PRIMARY KEY(endpoint,event_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS checkpoint (
            endpoint TEXT,session_id TEXT,event_timestamp TEXT,
            PRIMARY KEY(endpoint,session_id))""")
        db.commit()
        return db

    def upload(self, root: Path, report, *, progress=None, expected_connection=None):
        if not self.lock.acquire(blocking=False):
            raise ValueError("Another upload is running; wait for it to finish")
        try:
            if expected_connection is not None and expected_connection != (self.server_url, self.auth_resource):
                raise ValueError("Connection settings changed; review the destination and confirm the upload again")
            if not self.available:
                raise ValueError("Upload not configured: enter url and auth_resource in Connection settings")
            # Inter-process lock: never rely on threading.Lock alone for receipts.
            import fcntl
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(self.state / "upload.lock", "a", opener=lambda p, f: os.open(p, f, 0o600)) as lock_file:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("Another app process is uploading; wait for it to finish") from None
                return self._upload(root, report, progress=progress)
        finally:
            self.lock.release()

    def _upload(self, root, report, *, progress):
        pending = []
        skipped = 0
        # Export order is already parent-before-child. Native files are read, not
        # re-exported, so clicking Upload later cannot pick up new private messages.
        with closing(self._ledger()) as ledger:
            for session in report["sessions"]:
                directory = root / session["path"]
                metadata = json.loads((directory / "metadata.json").read_text())
                checkpoint = ledger.execute("SELECT event_timestamp FROM checkpoint WHERE endpoint=? AND session_id=?", (self.server_url, session["id"])).fetchone()
                lines = (directory / "events.jsonl").read_text(encoding="utf-8")
                expected = hashlib.sha256((lines + canonical(metadata) + "\n").encode()).hexdigest()
                if expected != session["sha256"]:
                    raise ValueError("Export files changed after creation; make a fresh export before uploading")
                for line in lines.splitlines():
                    record = json.loads(line)
                    eid = record["data"]["event_id"]
                    payload = build_payload(record, metadata["working_dir"])
                    digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
                    old = ledger.execute("SELECT digest,status,attempted_at FROM delivery WHERE endpoint=? AND event_id=?", (self.server_url, eid)).fetchone()
                    if old and old[0] != digest:
                        raise ValueError("Previously attempted event changed. Refusing upload to preserve identity; retain the earlier export and review source edits/mapping changes.")
                    if old and old[1] == "accepted":
                        skipped += 1
                        continue
                    if checkpoint and record["timestamp"] <= checkpoint[0]:
                        raise ValueError("Newly included historical events precede this session's upload checkpoint. Refusing out-of-order backfill; choose reasoning/options before the first upload and reconcile changed history separately.")
                    if old and old[2] and time.time() - old[2] > 6 * 86400:
                        raise ValueError("An unconfirmed delivery is older than six days. Refusing automatic retry: the server's deduplication window may have expired. Reconcile this session on the server first.")
                    pending.append((eid, digest, payload))
            total = len(pending) + skipped
            if progress:
                progress({"events_total": total, "events_sent": 0, "events_skipped": skipped})
            # Reserve all identities before networking. Even a partially failed
            # upload cannot later replace the source content under an old identity.
            ledger.executemany("INSERT OR IGNORE INTO delivery VALUES (?,?,?,'reserved',NULL)", [
                (self.server_url, eid, digest) for eid, digest, _ in pending
            ])
            ledger.commit()
            sent = 0
            for eid, _digest, payload in pending:
                ledger.execute("UPDATE delivery SET status='sending',attempted_at=coalesce(attempted_at,?) WHERE endpoint=? AND event_id=?", (time.time(), self.server_url, eid))
                ledger.commit()
                self._send(payload)
                ledger.execute("UPDATE delivery SET status='accepted' WHERE endpoint=? AND event_id=?", (self.server_url, eid))
                ledger.execute("INSERT INTO checkpoint VALUES (?,?,?) ON CONFLICT(endpoint,session_id) DO UPDATE SET event_timestamp=max(event_timestamp,excluded.event_timestamp)",
                               (self.server_url, payload["data"]["session_id"], payload["data"]["timestamp"]))
                ledger.commit()
                sent += 1
                if progress:
                    progress({"events_total": total, "events_sent": sent, "events_skipped": skipped})
            return {"status": "accepted" if sent else "already_accepted", "sessions": len(report["sessions"]),
                    "events_sent": sent, "events_skipped": skipped,
                    "note": "HTTP acceptance is not graph-ingestion completion. Check the CI server status/queues."}

    def _send(self, payload):
        body = canonical(payload).encode()
        refreshed = False
        for attempt in range(4):
            # Ask for headers on EVERY attempt so long uploads refresh tokens.
            if self.auth_mode == "entra":
                headers = self.auth.headers()
            else:
                token = os.environ.get(self.api_key_env, "")
                if not token.strip() or any(ord(c) < 33 or ord(c) > 126 for c in token):
                    raise ValueError("The bearer credential is empty or contains invalid whitespace/non-ASCII characters; check its environment variable")
                headers = {"Authorization": "Bearer " + token}
            request = Request(self.server_url + "/events", data=body,
                              headers={**headers, "Content-Type": "application/json"}, method="POST")
            wait = min(2 ** attempt, 8)
            try:
                with self.opener.open(request, timeout=30) as response:
                    code = response.status
                if 200 <= code < 300:
                    return
                raise ValueError(f"Unexpected HTTP {code}")
            except HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get("Retry-After", "")
                exc.close()
                if code == 401 and self.auth_mode == "entra" and not refreshed and attempt < 3:
                    self.auth.invalidate()
                    refreshed = True
                    continue
                if code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise ValueError(f"Upload failed (HTTP {code}). Previously accepted events are saved; retry this export after fixing the endpoint/authentication.") from None
                try:
                    wait = min(max(float(retry_after), 0), 30)
                except ValueError:
                    pass
            except (URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, (ssl.SSLError, socket.gaierror)) or attempt == 3:
                    raise ValueError("Upload transport failed. Acceptance of the last event may be ambiguous; retries reuse its identical idempotency key. No credential/server response has been logged.") from None
            except (ValueError, UnicodeError):
                raise ValueError("Invalid upload URL, credential header, or HTTP response; credential details are suppressed") from None
            time.sleep(wait)
