"""Loopback-only UI, authenticated per-process and protected against cross-site POSTs."""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .database import list_sessions
from .exporter import export_sessions
from .upload import Uploader
from .settings import load_settings, save_settings, validate_settings
from .connectivity import test_connection
from .local_exports import ExportBusy, delete_export


def make_server(database, state, *, port=8766, server_url=None, api_key_env="AMPLIFIER_CONTEXT_INTELLIGENCE_API_KEY", auth_resource=None, auth_mode="entra"):
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(32)
    jobs = {}
    lock = threading.Lock()
    settings = load_settings(state)
    config_lock = threading.Lock()
    test_lock = threading.Lock()
    archive_lock = threading.Lock()
    export_options = {name: settings[name] for name in ("include_children", "include_reasoning")}
    uploader = Uploader(state, settings["url"] if server_url is None else server_url,
                        auth_resource=settings["auth_resource"] if auth_resource is None else auth_resource,
                        auth_mode=auth_mode, api_key_env=api_key_env)
    assets = Path(__file__).parent / "static"

    def archive_operation(function):
        # Reserve before starting the background job so deletion cannot race a
        # queued export/upload. Downloads reserve the same lock while streaming.
        if not archive_lock.acquire(blocking=False):
            raise ExportBusy("Local exports are in use. Try again after the current operation finishes")

        def run():
            try:
                return function()
            finally:
                archive_lock.release()

        return run

    def configuration():
        with config_lock:
            return {"database": str(database), "state": str(state), "destination": uploader.server_url,
                    "url": uploader.server_url, "auth_resource": uploader.auth_resource,
                    "auth_mode": uploader.auth_mode, "upload_available": uploader.available,
                    "settings_file": str(state / "settings.json"), **export_options}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # URLs, transcripts and auth tokens must not enter access logs.

        def headers_for(self, status, content_type="application/json", length=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def reply(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.headers_for(status, length=len(body))
            self.wfile.write(body)

        def trusted_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def authenticated(self):
            return hmac.compare_digest(self.headers.get("X-Local-Token", ""), token)

        def do_GET(self):
            if not self.trusted_host():
                self.reply({"error": "Invalid host"}, 403)
                return
            parsed = urlsplit(self.path)
            if parsed.path in ("/", "/app.js", "/style.css"):
                path = assets / ("index.html" if parsed.path == "/" else parsed.path[1:])
                body = path.read_bytes()
                self.headers_for(200, mimetypes.guess_type(path)[0] or "text/plain", len(body))
                self.wfile.write(body)
                return
            if not self.authenticated():
                self.reply({"error": "Open the launch URL (with its #token fragment)"}, 401)
                return
            try:
                if parsed.path == "/api/config":
                    self.reply(configuration())
                elif parsed.path == "/api/sessions":
                    args = parse_qs(parsed.query)
                    since = args.get("since", [""])[0]
                    since_ms = int(datetime.fromisoformat(since).timestamp() * 1000) if since else 0
                    self.reply(list_sessions(database, search=args.get("search", [""])[0], since=since_ms,
                                             include_children=args.get("children", [""])[0] == "true",
                                             offset=int(args.get("offset", ["0"])[0])))
                elif parsed.path == "/api/exports":
                    reports = []
                    for path in (state / "exports").glob("*/manifest.json"):
                        try:
                            reports.append((path.stat().st_mtime, json.loads(path.read_text())))
                        except FileNotFoundError:
                            continue  # Another tab may just have deleted this artifact.
                    reports.sort(key=lambda entry: entry[0], reverse=True)
                    self.reply({"exports": [report for _, report in reports[:50]]})
                elif parsed.path.startswith("/api/job/"):
                    with lock:
                        job = jobs.get(parsed.path.rsplit("/", 1)[-1])
                        self.reply(dict(job) if job else {"error": "Job not found"}, 200 if job else 404)
                elif re.fullmatch(r"/api/download/[a-f0-9]{32}", parsed.path):
                    path = state / "exports" / parsed.path.rsplit("/", 1)[-1] / "export.zip"
                    if not path.is_file():
                        self.reply({"error": "Export not found"}, 404)
                        return
                    def download():
                        with path.open("rb") as f:
                            self.headers_for(200, "application/zip", os.fstat(f.fileno()).st_size)
                            while chunk := f.read(65536):
                                self.wfile.write(chunk)
                    archive_operation(download)()
                else:
                    self.reply({"error": "Not found"}, 404)
            except ExportBusy as exc:
                self.reply({"error": str(exc)}, 409)
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.reply({"error": str(exc)}, 400)

        def do_POST(self):
            origin = self.headers.get("Origin")
            allowed = (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
            if not self.trusted_host() or not self.authenticated() or (origin and origin not in allowed):
                self.reply({"error": "Unauthorized or cross-site request"}, 403)
                return
            if self.headers.get("Content-Type") != "application/json":
                self.reply({"error": "Expected application/json"}, 415)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 100_000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("Expected an object")
                if self.path == "/api/config":
                    with config_lock:
                        submitted = validate_settings({**export_options, **body})
                        uploader.configure(submitted["url"], submitted["auth_resource"],
                                           persist=lambda value: save_settings(state, {**submitted, **value}))
                        export_options.update({name: submitted[name] for name in export_options})
                    self.reply(configuration())
                    return
                elif self.path == "/api/test-connection":
                    if set(body) - {"url", "auth_resource"}:
                        raise ValueError("A connection test accepts only url and auth_resource")
                    candidate = validate_settings(body)
                    if not candidate["url"] or (auth_mode == "entra" and not candidate["auth_resource"]):
                        raise ValueError("Enter url and auth_resource before testing")
                    if not test_lock.acquire(blocking=False):
                        raise ValueError("A connection test is already running")

                    def check():
                        try:
                            return test_connection(candidate["url"], candidate["auth_resource"],
                                                   auth_mode=auth_mode, api_key_env=api_key_env)
                        finally:
                            test_lock.release()

                    action = check
                elif self.path == "/api/export":
                    ids = body.get("sessions")
                    if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(s, str) for s in ids):
                        raise ValueError("Select 1–500 session IDs")
                    with config_lock:
                        options = {name: body.get(name, default) for name, default in export_options.items()}
                    if any(not isinstance(value, bool) for value in options.values()):
                        raise ValueError("Export options must be booleans")
                    action = archive_operation(lambda: export_sessions(database, state, ids, **options))
                elif self.path == "/api/delete-export":
                    eid = body.get("export_id", "")
                    if not isinstance(eid, str) or not re.fullmatch(r"[a-f0-9]{32}", eid) or body.get("confirm") is not True:
                        raise ValueError("Choose a local export and explicitly confirm deletion")
                    action = archive_operation(lambda: delete_export(state, eid))
                elif self.path == "/api/upload":
                    eid = body.get("export_id", "")
                    if not isinstance(eid, str) or not re.fullmatch(r"[a-f0-9]{32}", eid) or body.get("confirm") is not True:
                        raise ValueError("Choose an export and explicitly confirm upload")
                    if not uploader.available:
                        raise ValueError("Upload is not configured")
                    connection = (uploader.server_url, uploader.auth_resource)
                    if (body.get("url"), body.get("auth_resource")) != connection:
                        raise ValueError("Connection settings changed; reload them and confirm the upload again")
                    root = state / "exports" / eid
                    report = json.loads((root / "manifest.json").read_text())
                    action = archive_operation(lambda: uploader.upload(root, report, progress=update_progress, expected_connection=connection))
                else:
                    self.reply({"error": "Not found"}, 404)
                    return
                jid = secrets.token_hex(16)
                with lock:
                    jobs[jid] = {"status": "running"}

                def update_progress(value):
                    with lock:
                        jobs[jid].update(value)

                def work():
                    try:
                        result = action()
                        update = {"status": "completed", "result": result}
                    except Exception as exc:
                        # The upload adapter already sanitizes credential-bearing errors.
                        update = {"status": "failed", "error": str(exc)}
                    with lock:
                        jobs[jid] = update

                threading.Thread(target=work, daemon=True).start()
                self.reply({"job": jid}, 202)
            except ExportBusy as exc:
                self.reply({"error": str(exc)}, 409)
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.reply({"error": str(exc)}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler), token
