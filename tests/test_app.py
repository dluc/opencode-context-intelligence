from __future__ import annotations

import hashlib
import http.client
import json
import os
import io
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from opencode_ci.database import connect, list_sessions, load_sessions
from opencode_ci.exporter import canonical, export_sessions, map_session
from opencode_ci.server import make_server
from opencode_ci.upload import Uploader, build_payload, normalize_url
from opencode_ci.__main__ import main
from opencode_ci.settings import save_settings
from opencode_ci.local_exports import ExportBusy, delete_export

BASE = 1760000000000


def create_database(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, title TEXT,directory TEXT,parent_id TEXT,fork_session_id TEXT,
            time_created INTEGER,time_updated INTEGER,time_idle INTEGER,idle_outcome TEXT,
            time_archived INTEGER,agent TEXT);
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INTEGER,
            time_created INTEGER,time_updated INTEGER,data TEXT);
        """)
        for sid, parent in [("ses_root", None), ("ses_child", "ses_root"), ("ses_unselected", None)]:
            db.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                sid, "<script>alert('not HTML')</script>", "/work/example", parent, None,
                BASE, BASE + 3000, BASE + 3000, "succeeded", None, "build"))
        records = [
            ("msg_user", "user", {"time": {"created": BASE}, "text": "Hello"}),
            ("msg_assistant", "assistant", {
                "time": {"created": BASE + 100, "completed": BASE + 2500},
                "model": {"id": "model", "providerID": "test"}, "finish": "stop", "cost": 0.1,
                "tokens": {"input": 10, "output": 20, "cache": {"read": 0, "write": 0}},
                "native": {"secret": "NEVER_EXPORT_NATIVE"},
                "content": [
                    {"type": "reasoning", "text": "PRIVATE_REASONING", "time": {"created": BASE + 100, "completed": BASE + 200}},
                    {"type": "tool", "id": "call_stable", "name": "read", "time": {"created": BASE + 300, "ran": BASE + 300, "completed": BASE + 400},
                     "state": {"status": "completed", "input": {"path": "README.md"}, "content": [{"type": "text", "text": "Tool result"}]}},
                    {"type": "text", "text": "Response", "native": {"token": "NEVER_EXPORT_NATIVE"}},
                ]}),
            ("msg_idle", "idle", {"time": {"created": BASE + 3000}, "outcome": "succeeded"}),
        ]
        for sid in ("ses_root", "ses_child"):
            for seq, (mid, kind, data) in enumerate(records):
                db.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", (
                    f"{mid}_{sid}", sid, kind, seq, data["time"]["created"], BASE + 3000, json.dumps(data)))


class AppTests(unittest.TestCase):
    def setUp(self):
        # /tmp/opencode is not writable by this environment's shell user.
        self.tmp = tempfile.TemporaryDirectory(prefix="opencode-ci-", dir=Path.home() / ".cache")
        self.root = Path(self.tmp.name)
        self.db = self.root / "opencode.db"
        self.state = self.root / "state"
        create_database(self.db)
        self.environment = patch.dict(os.environ, {"OPENCODE_CI_TEST_KEY": "synthetic-test-key"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def export(self, **kwargs):
        return export_sessions(self.db, self.state, ["ses_root"], **kwargs)

    def test_list_metadata_and_filter(self):
        result = list_sessions(self.db)
        self.assertEqual(result["total"], 2)
        self.assertNotIn("data", result["sessions"][0])
        self.assertEqual(list_sessions(self.db, search="ses_root")["total"], 1)
        self.assertEqual(list_sessions(self.db, include_children=True)["total"], 3)
        self.assertEqual(list_sessions(self.db, search="%" )["total"], 0)
        self.assertEqual(len(list_sessions(self.db, limit=1, offset=1)["sessions"]), 1)

    def test_read_only(self):
        before = hashlib.sha256(self.db.read_bytes()).digest()
        with connect(self.db) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM session_v2")
        self.export()
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).digest())

    def test_wal_snapshot(self):
        writer = sqlite3.connect(self.db)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE session_v2 SET title='WAL title' WHERE id='ses_root'")
        writer.commit()
        try:
            with connect(self.db) as reader:
                title = reader.execute("SELECT title FROM session_v2 WHERE id='ses_root'").fetchone()[0]
                self.assertEqual(title, "WAL title")
                writer.execute("UPDATE session_v2 SET title='New title' WHERE id='ses_root'")
                writer.commit()
                self.assertEqual(reader.execute("SELECT title FROM session_v2 WHERE id='ses_root'").fetchone()[0], "WAL title")
        finally:
            writer.close()

    def test_children_explicit_and_unselected_omitted(self):
        self.assertEqual([s["id"] for s in load_sessions(self.db, ["ses_root"], True)], ["ses_root", "ses_child"])
        self.assertEqual(len(load_sessions(self.db, ["ses_root"], False)), 1)
        with self.assertRaises(ValueError):
            load_sessions(self.db, ["nonexistent"], False)

    def test_byte_stable_native_files_and_identity(self):
        a = self.export()
        b = self.export()
        self.assertEqual(a["fingerprint"], b["fingerprint"])
        self.assertNotEqual(a["id"], b["id"])
        for sa, sb in zip(a["sessions"], b["sessions"]):
            for name in ("events.jsonl", "metadata.json"):
                self.assertEqual((self.state / "exports" / a["id"] / sa["path"] / name).read_bytes(),
                                 (self.state / "exports" / b["id"] / sb["path"] / name).read_bytes())
            self.assertEqual(sa["id"], sb["id"])

    def test_native_shape_and_privacy(self):
        result = self.export()
        target = self.state / "exports" / result["id"]
        self.assertEqual(len(result["sessions"]), 2)
        self.assertTrue(result["include_children"])
        self.assertTrue(result["include_reasoning"])
        for item in result["sessions"]:
            raw = (target / item["path"] / "events.jsonl").read_text()
            self.assertNotIn("NEVER_EXPORT_NATIVE", raw)
            self.assertIn("PRIVATE_REASONING", raw)
            records = [json.loads(line) for line in raw.splitlines()]
            stamps = [r["timestamp"] for r in records]
            self.assertEqual(stamps, sorted(set(stamps)))
            for record in records:
                self.assertEqual(set(record), {"data", "event", "timestamp", "workspace"})
                self.assertEqual(record["timestamp"], record["data"]["timestamp"])
                self.assertEqual(record["data"]["session_id"], item["id"])
            self.assertNotIn("session:end", [r["event"] for r in records])
            meta = json.loads((target / item["path"] / "metadata.json").read_text())
            self.assertEqual(meta["format"], "context-intelligence")
            if item["id"] == "ses_child":
                self.assertEqual(meta["parent_id"], "ses_root")
        with zipfile.ZipFile(target / "export.zip") as archive:
            self.assertIn("manifest.json", archive.namelist())
            self.assertEqual(len([s for s in archive.namelist() if s.endswith("events.jsonl")]), 2)
        self.assertEqual((target / "export.zip").stat().st_mode & 0o777, 0o600)

    def test_delete_only_selected_local_artifact_and_keep_receipts(self):
        first, second = self.export(), self.export()
        root = self.state / "exports" / first["id"]
        other = self.state / "exports" / second["id"]
        receipt = self.state / "uploads.db"
        receipt.write_bytes(b"synthetic-receipt-marker")
        save_settings(self.state, {"include_children": True, "include_reasoning": True})
        settings_before = (self.state / "settings.json").read_bytes()
        source_before = self.db.read_bytes()
        self.assertEqual(delete_export(self.state, first["id"])["status"], "deleted")
        self.assertFalse(root.exists())
        self.assertTrue((other / "export.zip").exists())
        self.assertEqual(receipt.read_bytes(), b"synthetic-receipt-marker")
        self.assertEqual((self.state / "settings.json").read_bytes(), settings_before)
        self.assertEqual(self.db.read_bytes(), source_before)
        self.assertEqual(delete_export(self.state, first["id"])["status"], "already_deleted")

    def test_delete_rejects_traversal_and_root_symlinks(self):
        report = self.export()
        for value in ("", "..", "../uploads.db", "/", None, "a" * 31, report["id"] + "/.."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                delete_export(self.state, value)
        link_id = "a" * 32
        (self.state / "exports" / link_id).symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            delete_export(self.state, link_id)
        self.assertTrue(self.db.is_file())
        self.assertTrue((self.state / "exports" / report["id"] / "manifest.json").is_file())

    def test_delete_does_not_follow_child_symlink(self):
        report = self.export()
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep this unrelated file")
        root = self.state / "exports" / report["id"]
        (root / "outside-link").symlink_to(outside, target_is_directory=True)
        delete_export(self.state, report["id"])
        self.assertEqual(marker.read_text(), "keep this unrelated file")

    def test_delete_blocked_by_another_process_upload_lock(self):
        import fcntl
        report = self.export()
        with (self.state / "upload.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ExportBusy):
                delete_export(self.state, report["id"])
        self.assertTrue((self.state / "exports" / report["id"]).exists())
        self.assertEqual(delete_export(self.state, report["id"])["status"], "deleted")

    def test_delete_api_requires_auth_confirmation_and_valid_id(self):
        report = self.export()
        server, token = make_server(self.db, self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Content-Type": "application/json", "X-Local-Token": token}
        payload = {"export_id": report["id"], "confirm": True}
        try:
            for body, request_headers, expected in (
                (payload, {"Content-Type": "application/json"}, 403),
                ({"export_id": report["id"]}, headers, 400),
                ({"export_id": "../uploads.db", "confirm": True}, headers, 400),
                (payload, {**headers, "Origin": "https://attacker.example"}, 403),
            ):
                client.request("POST", "/api/delete-export", json.dumps(body), request_headers)
                response = client.getresponse(); self.assertEqual(response.status, expected); response.read()
                self.assertTrue((self.state / "exports" / report["id"]).exists())
            client.request("POST", "/api/delete-export", json.dumps(payload), headers)
            response = client.getresponse(); self.assertEqual(response.status, 202)
            jid = json.loads(response.read())["job"]
            import time
            for _ in range(100):
                client.request("GET", "/api/job/" + jid, headers={"X-Local-Token": token})
                response = client.getresponse(); job = json.loads(response.read())
                if job["status"] != "running": break
                time.sleep(.01)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["result"]["status"], "deleted")
            client.request("GET", "/api/exports", headers={"X-Local-Token": token})
            response = client.getresponse(); self.assertEqual(json.loads(response.read())["exports"], [])
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()

    def test_delete_api_blocked_during_active_archive_operation(self):
        report = self.export()
        server, token = make_server(self.db, self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Content-Type": "application/json", "X-Local-Token": token}
        started, release = threading.Event(), threading.Event()
        def working_export(*args, **kwargs):
            started.set(); release.wait(5)
            return {}
        try:
            with patch("opencode_ci.server.export_sessions", side_effect=working_export):
                client.request("POST", "/api/export", '{"sessions":["ses_root"]}', headers)
                response = client.getresponse(); self.assertEqual(response.status, 202); response.read()
                self.assertTrue(started.wait(2))
                client.request("POST", "/api/delete-export", json.dumps({"export_id": report["id"], "confirm": True}), headers)
                response = client.getresponse(); self.assertEqual(response.status, 409); response.read()
                self.assertTrue((self.state / "exports" / report["id"]).exists())
        finally:
            release.set(); client.close(); server.shutdown(); server.server_close(); thread.join()

    def test_reasoning_toggle_does_not_change_other_events(self):
        session = load_sessions(self.db, ["ses_root"], False)[0]
        without = map_session(session, include_reasoning=False)["events"]
        with_reason = map_session(session)["events"]
        indexed = {e["data"]["event_id"]: e for e in with_reason}
        for record in without:
            self.assertEqual(record, indexed[record["data"]["event_id"]])
        self.assertGreater(len(with_reason), len(without))

    def test_explicit_export_opt_outs(self):
        report = self.export(include_children=False, include_reasoning=False)
        self.assertEqual(len(report["sessions"]), 1)
        self.assertFalse(report["include_children"])
        self.assertFalse(report["include_reasoning"])
        path = self.state / "exports" / report["id"] / report["sessions"][0]["path"] / "events.jsonl"
        self.assertNotIn("PRIVATE_REASONING", path.read_text())

    def test_cli_export_defaults_and_opt_outs(self):
        base = ["opencode-ci", "--db", str(self.db), "--state", str(self.state), "export", "--session", "ses_root"]
        for flags, included in [([], True), (["--no-children", "--no-reasoning"], False), (["--include-reasoning"], True)]:
            with self.subTest(flags=flags), patch("sys.argv", base + flags), patch("sys.stdout", new_callable=io.StringIO), patch("opencode_ci.__main__.export_sessions", return_value={}) as export:
                # main's umask change is process-global; restore it for other tests.
                previous = os.umask(0o077)
                try:
                    main()
                finally:
                    os.umask(previous)
                self.assertEqual(export.call_args.kwargs["include_children"], included)
                self.assertEqual(export.call_args.kwargs["include_reasoning"], included)

    def test_cli_uses_saved_preferences_with_explicit_overrides(self):
        save_settings(self.state, {"include_children": False, "include_reasoning": False})
        base = ["opencode-ci", "--db", str(self.db), "--state", str(self.state), "export", "--session", "ses_root"]
        for flags, included in [([], False), (["--include-children", "--include-reasoning"], True)]:
            with self.subTest(flags=flags), patch("sys.argv", base + flags), patch("sys.stdout", new_callable=io.StringIO), patch("opencode_ci.__main__.export_sessions", return_value={}) as export:
                previous = os.umask(0o077)
                try:
                    main()
                finally:
                    os.umask(previous)
                self.assertEqual(export.call_args.kwargs["include_children"], included)
                self.assertEqual(export.call_args.kwargs["include_reasoning"], included)

    def test_append_idle_run_does_not_change_existing_events(self):
        before = map_session(load_sessions(self.db, ["ses_root"], False)[0])["events"]
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", (
                "msg_next", "ses_root", "user", 3, BASE+4000, BASE+4000,
                json.dumps({"time": {"created": BASE+4000}, "text": "Next prompt"})))
        after = map_session(load_sessions(self.db, ["ses_root"], False)[0])["events"]
        self.assertEqual(before, after[:len(before)])

    def test_unfinished_message_is_not_fabricated(self):
        session = load_sessions(self.db, ["ses_root"], False)[0]
        session["messages"] = session["messages"][:2]
        del session["messages"][1]["data"]["time"]["completed"]
        result = map_session(session)
        self.assertEqual([e["event"] for e in result["events"]], ["session:start", "prompt:submit", "execution:start"])
        self.assertTrue(result["warnings"])
        self.assertEqual(result["metadata"]["status"], "running")

    def test_no_wall_clock_fallback(self):
        session = load_sessions(self.db, ["ses_root"], False)[0]
        session["time_created"] = None
        with self.assertRaises(ValueError):
            map_session(session)

    def test_error_tool(self):
        session = load_sessions(self.db, ["ses_root"], False)[0]
        tool = session["messages"][1]["data"]["content"][1]
        tool["state"] = {"status": "error", "input": {}, "error": "failed"}
        events = map_session(session)["events"]
        error = next(e for e in events if e["event"] == "tool:error")
        self.assertEqual(error["data"]["tool_call_id"], "call_stable")
        self.assertEqual(error["data"]["error"], "failed")

    def test_unsupported_schema_rejected(self):
        bad = self.root / "bad.db"
        sqlite3.connect(bad).close()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            list_sessions(bad)

    def test_url_validation(self):
        self.assertEqual(normalize_url("https://CI.EXAMPLE.com/base/"), "https://ci.example.com/base")
        self.assertEqual(normalize_url("http://127.0.0.1:1234"), "http://127.0.0.1:1234")
        for url in ("http://remote.example", "https://u:secret@ci.example", "https://ci.example/events", "https://ci.example?key=secret", "file:///x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_upload_receipt_skips_repeat_and_refuses_mutation(self):
        report = self.export()
        root = self.state / "exports" / report["id"]
        uploader = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.object(uploader, "_send") as send:
            result = uploader.upload(root, report)
            self.assertEqual(result["status"], "accepted")
            count = sum(s["events"] for s in report["sessions"])
            self.assertEqual(send.call_count, count)
            self.assertTrue(all(c.args[0]["idempotency_key"].startswith("aci-event-v1:") for c in send.call_args_list))
            self.assertEqual(uploader.upload(root, report)["status"], "already_accepted")
            self.assertEqual(send.call_count, count)
        again = self.export()
        self.assertEqual(uploader.upload(self.state / "exports" / again["id"], again)["status"], "already_accepted")
        with closing(sqlite3.connect(self.db)) as db, db:
            (raw,) = db.execute("SELECT data FROM session_message WHERE id='msg_user_ses_root'").fetchone()
            data = json.loads(raw); data["text"] = "Changed old prompt"
            db.execute("UPDATE session_message SET data=? WHERE id='msg_user_ses_root'", (json.dumps(data),))
        changed = self.export()
        with self.assertRaisesRegex(ValueError, "Previously attempted event changed"):
            uploader.upload(self.state / "exports" / changed["id"], changed)

    def test_failed_upload_has_no_receipt_and_sanitizes_errors(self):
        report = self.export()
        root = self.state / "exports" / report["id"]
        uploader = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.object(uploader, "_send", side_effect=ValueError("synthetic failure")) as send:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    uploader.upload(root, report)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args_list[0], send.call_args_list[1])

    def test_partial_failure_resumes_only_unaccepted_events(self):
        report = self.export()
        root = self.state / "exports" / report["id"]
        uploader = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.object(uploader, "_send", side_effect=[None, None, ValueError("failure")]), self.assertRaises(ValueError):
            uploader.upload(root, report)
        # A fresh object simulates restarting the app.
        restarted = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.object(restarted, "_send") as send:
            result = restarted.upload(root, report)
            self.assertEqual(result["events_skipped"], 2)
            self.assertEqual(send.call_count, sum(s["events"] for s in report["sessions"]) - 2)

    def test_native_export_tampering_refused(self):
        report = self.export()
        root = self.state / "exports" / report["id"]
        path = root / report["sessions"][0]["path"] / "events.jsonl"
        path.write_text(path.read_text().replace('"source"', '"modified"'))
        uploader = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.object(uploader, "_send") as send, self.assertRaisesRegex(ValueError, "Export files changed"):
            uploader.upload(root, report)
        send.assert_not_called()

    def test_incremental_new_run_sends_only_new_events(self):
        a = self.export(include_children=False)
        uploader = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.object(uploader, "_send"):
            uploader.upload(self.state / "exports" / a["id"], a)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", (
                "msg_next", "ses_root", "user", 3, BASE+4000, BASE+4000,
                json.dumps({"time": {"created": BASE+4000}, "text": "Next prompt"})))
        b = self.export(include_children=False)
        with patch.object(uploader, "_send") as send:
            result = uploader.upload(self.state / "exports" / b["id"], b)
        self.assertEqual(result["events_skipped"], a["sessions"][0]["events"])
        self.assertEqual(send.call_count, 2)

    def test_historical_reasoning_backfill_refused(self):
        a = self.export(include_children=False, include_reasoning=False)
        uploader = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.object(uploader, "_send"):
            uploader.upload(self.state / "exports" / a["id"], a)
        b = self.export(include_children=False, include_reasoning=True)
        with patch.object(uploader, "_send") as send, self.assertRaisesRegex(ValueError, "historical events precede"):
            uploader.upload(self.state / "exports" / b["id"], b)
        send.assert_not_called()

    def test_expired_uncertain_receipt_refused(self):
        a = self.export(include_children=False)
        uploader = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.object(uploader, "_send", side_effect=ValueError("failure")), self.assertRaises(ValueError):
            uploader.upload(self.state / "exports" / a["id"], a)
        with closing(sqlite3.connect(self.state / "uploads.db")) as db, db:
            db.execute("UPDATE delivery SET attempted_at=1 WHERE status='sending'")
        with patch.object(uploader, "_send") as send, self.assertRaisesRegex(ValueError, "older than six days"):
            uploader.upload(self.state / "exports" / a["id"], a)
        send.assert_not_called()

    def test_credential_error_does_not_expose_secret(self):
        uploader = Uploader(self.state, "https://ci.example", api_key_env="OPENCODE_CI_TEST_KEY")
        with patch.dict(os.environ, {"OPENCODE_CI_TEST_KEY": "SECRET_VALUE\nwrong"}), self.assertRaises(ValueError) as error:
            uploader._send({})
        self.assertNotIn("SECRET_VALUE", str(error.exception))

    def test_wire_payload_unicode_matches_official_formula(self):
        record = {"event": "prompt:submit", "workspace": "日本語", "data": {"prompt": "café ☕"}}
        payload = build_payload(record, "/work/日本語")
        serialized = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self.assertEqual(payload["idempotency_key"], "aci-event-v1:" + hashlib.sha256(serialized.encode()).hexdigest())

    def test_redirect_is_refused_and_retry_keeps_key(self):
        received = []

        class MockCI(BaseHTTPRequestHandler):
            mode = "retry"

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, value))
                if self.mode == "redirect":
                    self.send_response(307)
                    self.send_header("Location", "/redirected")
                elif len(received) == 1:
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                else:
                    self.send_response(202)
                self.end_headers()

            def log_message(self, *_): pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), MockCI)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        uploader = Uploader(self.state, f"http://127.0.0.1:{server.server_port}", api_key_env="OPENCODE_CI_TEST_KEY")
        try:
            uploader._send({"idempotency_key": "fixed-key"})
            self.assertEqual(len(received), 2)
            self.assertEqual(received[0], received[1])
            received.clear(); MockCI.mode = "redirect"
            with self.assertRaisesRegex(ValueError, "HTTP 307"):
                uploader._send({"idempotency_key": "fixed-key"})
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], "/events")
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_wire_payload_and_http_only_use_loopback_synthetic_data(self):
        received = []

        class MockCI(BaseHTTPRequestHandler):
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, value, self.headers.get("Authorization")))
                self.send_response(202); self.end_headers(); self.wfile.write(b'{"status":"queued"}')

            def log_message(self, *_): pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), MockCI)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            report = self.export(include_children=False)
            uploader = Uploader(self.state, f"http://127.0.0.1:{server.server_port}", api_key_env="OPENCODE_CI_TEST_KEY")
            result = uploader.upload(self.state / "exports" / report["id"], report)
            self.assertEqual(result["events_sent"], len(received))
            for path, payload, bearer in received:
                self.assertEqual(path, "/events")  # no replay=true
                self.assertEqual(bearer, "Bearer synthetic-test-key")
                envelope = {k: payload[k] for k in ("event", "workspace", "data")}
                digest = hashlib.sha256(json.dumps(envelope,sort_keys=True,separators=(",", ":")).encode()).hexdigest()
                self.assertEqual(payload["idempotency_key"], "aci-event-v1:"+digest)
                self.assertEqual(payload["working_dir"], "/work/example")
            uploader.upload(self.state / "exports" / report["id"], report)
            self.assertEqual(result["events_sent"], len(received))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_http_auth_csrf_and_export_job(self):
        server, token = make_server(self.db, self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            client.request("GET", "/api/sessions")
            response = client.getresponse(); self.assertEqual(response.status, 401); response.read()
            client.request("GET", "/api/sessions", headers={"X-Local-Token": token})
            response = client.getresponse(); self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["total"], 2)
            client.request("GET", "/api/config", headers={"X-Local-Token": token, "Host": "attacker.example"})
            response = client.getresponse(); self.assertEqual(response.status, 403); response.read()
            headers = {"X-Local-Token": token, "Content-Type": "application/json", "Origin": "https://attacker.example"}
            client.request("POST", "/api/export", '{"sessions":["ses_root"]}', headers)
            response = client.getresponse(); self.assertEqual(response.status, 403); response.read()
            headers.pop("Origin")
            client.request("POST", "/api/export", '{"sessions":["ses_root"]}', headers)
            response = client.getresponse(); self.assertEqual(response.status, 202)
            jid = json.loads(response.read())["job"]
            import time
            for _ in range(100):
                client.request("GET", f"/api/job/{jid}", headers={"X-Local-Token": token})
                response = client.getresponse(); job = json.loads(response.read())
                if job["status"] != "running": break
                time.sleep(.01)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(len(job["result"]["sessions"]), 2)
            self.assertTrue(job["result"]["include_children"])
            self.assertTrue(job["result"]["include_reasoning"])
            client.request("POST", "/api/upload", json.dumps({"export_id": job["result"]["id"], "confirm": True}), headers)
            response = client.getresponse(); self.assertEqual(response.status, 400); response.read()
            client.request("GET", "/", headers={})
            response = client.getresponse(); self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy")); response.read()
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
