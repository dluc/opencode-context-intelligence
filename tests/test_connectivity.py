"""Read-only probes against a synthetic loopback server, never a real endpoint."""

import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from opencode_ci.connectivity import test_connection as check_connection
from opencode_ci.server import make_server
from opencode_ci.settings import save_settings


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.health_code = 200
        self.identity_code = 200
        self.health_requires_token = False
        self.identity = {"contributor_id": "synthetic-contributor"}
        owner = self

        class MockCI(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.command, self.path, self.headers.get("Authorization"), self.headers.get("Content-Length")))
                if self.path == "/ci/status":
                    code = 401 if owner.health_requires_token and self.headers.get("Authorization") != "Bearer synthetic-test-token" else owner.health_code
                    body = {"status": "ok"}
                else:
                    code, body = owner.identity_code, owner.identity
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                if code == 307:
                    self.send_header("Location", "/redirected")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                owner.requests.append((self.command, self.path, None, None))
                self.send_response(405); self.end_headers()

            def log_message(self, *_): pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockCI)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/ci"
        self.credential = Mock()
        self.credential.get_token.return_value = SimpleNamespace(token="synthetic-test-token", expires_on=time.time() + 3600)
        self.create = patch("opencode_ci.auth._create_credential", return_value=self.credential)
        self.create.start()

    def tearDown(self):
        self.create.stop()
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def check(self):
        return check_connection(self.url, "api://example-app")

    def test_only_read_only_routes_and_no_body_or_identity_leak(self):
        result = self.check()
        self.assertTrue(result["success"])
        self.assertTrue(result["server"]["ok"])
        self.assertTrue(result["authentication"]["ok"])
        self.assertEqual(self.requests, [
            ("GET", "/ci/status", "Bearer synthetic-test-token", None),
            ("GET", "/ci/whoami", "Bearer synthetic-test-token", None),
        ])
        self.credential.get_token.assert_called_once_with("api://example-app/.default")
        self.credential.close.assert_called_once()
        self.assertNotIn("synthetic-test-token", json.dumps(result))
        self.assertNotIn("synthetic-contributor", json.dumps(result))

    def test_missing_health_route_does_not_hide_verified_authentication(self):
        self.health_code = 404
        result = self.check()
        self.assertFalse(result["success"])
        self.assertFalse(result["server"]["ok"])
        self.assertTrue(result["server"]["reachable"])
        self.assertIn("404", result["server"]["message"])

    def test_gateway_protected_health_uses_bearer_from_first_request(self):
        self.health_requires_token = True
        result = self.check()
        self.assertTrue(result["success"])
        self.assertEqual(result["server"]["http_status"], 200)
        self.assertTrue(result["server"]["authenticated_request"])
        self.assertEqual([r[1] for r in self.requests], ["/ci/status", "/ci/whoami"])
        self.assertTrue(all(r[2] == "Bearer synthetic-test-token" for r in self.requests))

    def test_health_401_refreshes_token_before_retry(self):
        self.health_requires_token = True
        self.credential.get_token.side_effect = [
            SimpleNamespace(token="expired-synthetic-token", expires_on=time.time() + 3600),
            SimpleNamespace(token="synthetic-test-token", expires_on=time.time() + 3600),
        ]
        result = self.check()
        self.assertTrue(result["success"])
        self.assertEqual([r[1] for r in self.requests], ["/ci/status", "/ci/status", "/ci/whoami"])
        self.assertEqual(self.credential.get_token.call_count, 2)

    def test_persistent_authenticated_health_401_is_not_overall_success(self):
        self.health_code = 401
        result = self.check()
        self.assertFalse(result["success"])
        self.assertTrue(result["authentication"]["ok"])
        self.assertFalse(result["server"]["ok"])
        self.assertIn("rejected the authenticated request", result["server"]["message"])

    def test_missing_identity_route_is_not_reported_as_auth_rejection(self):
        self.identity_code = 404
        result = self.check()
        self.assertFalse(result["success"])
        self.assertTrue(result["server"]["ok"])
        self.assertIn("not exposed", result["authentication"]["message"])

    def test_rejected_token_refreshes_once_and_redacts_response(self):
        self.identity_code = 401
        self.identity = {"error": "SENSITIVE_SERVER_RESPONSE"}
        result = self.check()
        self.assertFalse(result["success"])
        self.assertEqual(self.credential.get_token.call_count, 2)
        self.assertEqual([r[1] for r in self.requests], ["/ci/status", "/ci/whoami", "/ci/whoami"])
        self.assertIn("401", result["authentication"]["message"])
        self.assertNotIn("SENSITIVE_SERVER_RESPONSE", json.dumps(result))

    def test_missing_azure_credentials_keeps_health_result(self):
        self.credential.get_token.side_effect = RuntimeError("SENSITIVE_ACCOUNT_DETAILS")
        result = self.check()
        self.assertTrue(result["server"]["ok"])
        self.assertFalse(result["authentication"]["ok"])
        self.assertIn("az login", result["authentication"]["message"])
        self.assertNotIn("SENSITIVE_ACCOUNT_DETAILS", json.dumps(result))
        self.assertEqual([r[1] for r in self.requests], ["/ci/status"])

    def test_anonymous_or_html_success_is_not_authentication(self):
        for identity in ({"contributor_id": None}, {"status": "ok"}, "HTML landing page"):
            with self.subTest(identity=identity):
                self.identity = identity
                self.assertFalse(self.check()["authentication"]["ok"])

    def test_redirect_is_never_followed_with_credentials(self):
        self.identity_code = 307
        result = self.check()
        self.assertFalse(result["success"])
        self.assertEqual([r[1] for r in self.requests], ["/ci/status", "/ci/whoami"])

    def test_invalid_input_does_not_acquire_token_or_make_requests(self):
        with self.assertRaises(ValueError):
            check_connection(self.url, "")
        self.assertEqual(self.requests, [])
        self.credential.get_token.assert_not_called()


class ConnectionAPItests(unittest.TestCase):
    def test_uses_unsaved_values_without_saving_reading_sessions_or_uploading(self):
        with tempfile.TemporaryDirectory(prefix="opencode-ci-probe-", dir=Path.home() / ".cache") as directory:
            state = Path(directory)
            save_settings(state, {"url": "https://saved.example.com", "auth_resource": "api://saved-example"})
            before = (state / "settings.json").read_bytes()
            server, token = make_server(Path("must-not-open.db"), state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            candidate = {"url": "https://draft.example.com", "auth_resource": "api://draft-example"}
            result = {"success": True, "server": {"ok": True, "message": "Reachable"},
                      "authentication": {"ok": True, "message": "Verified"}}
            try:
                with patch("opencode_ci.server.test_connection", return_value=result) as probe, patch("opencode_ci.server.list_sessions") as listing, patch("opencode_ci.server.export_sessions") as export, patch("opencode_ci.upload.Uploader._send") as upload, patch("opencode_ci.server.save_settings") as save:
                    client.request("POST", "/api/test-connection", json.dumps(candidate), {"Content-Type": "application/json"})
                    response = client.getresponse(); self.assertEqual(response.status, 403); response.read()
                    probe.assert_not_called()
                    client.request("POST", "/api/test-connection", json.dumps(candidate), {"Content-Type": "application/json", "X-Local-Token": token})
                    response = client.getresponse(); self.assertEqual(response.status, 202)
                    job_id = json.loads(response.read())["job"]
                    job = {}
                    for _ in range(100):
                        client.request("GET", "/api/job/" + job_id, headers={"X-Local-Token": token})
                        response = client.getresponse(); job = json.loads(response.read())
                        if job["status"] != "running": break
                        time.sleep(.01)
                    self.assertEqual(job["status"], "completed")
                    self.assertEqual(job["result"], result)
                    probe.assert_called_once_with(candidate["url"], candidate["auth_resource"], auth_mode="entra", api_key_env="AMPLIFIER_CONTEXT_INTELLIGENCE_API_KEY")
                    listing.assert_not_called(); export.assert_not_called(); upload.assert_not_called(); save.assert_not_called()
                self.assertEqual((state / "settings.json").read_bytes(), before)
                self.assertFalse((state / "uploads.db").exists())
                self.assertFalse((state / "exports").exists())
            finally:
                client.close(); server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
