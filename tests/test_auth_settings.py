"""Synthetic auth/settings fixtures only: never use a real tenant or endpoint."""

import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from opencode_ci.auth import EntraAuth, normalize_resource
from opencode_ci.server import make_server
from opencode_ci.settings import load_settings, save_settings
from opencode_ci.upload import Uploader


class AuthTests(unittest.TestCase):
    def test_scopes_cache_and_expiry_refresh(self):
        credential = Mock()
        credential.get_token.side_effect = [
            SimpleNamespace(token="first-test-token", expires_on=2000),
            SimpleNamespace(token="second-test-token", expires_on=3000),
        ]
        auth = EntraAuth("api://example-app", credential=credential)
        with patch("opencode_ci.auth.time.time", return_value=1000):
            self.assertEqual(auth.headers(), {"Authorization": "Bearer first-test-token"})
            self.assertEqual(auth.headers(), {"Authorization": "Bearer first-test-token"})
            credential.get_token.assert_called_once_with("api://example-app/.default")
        with patch("opencode_ci.auth.time.time", return_value=1800):
            self.assertEqual(auth.headers(), {"Authorization": "Bearer second-test-token"})
        self.assertEqual(credential.get_token.call_count, 2)

    def test_lazy_acquisition_and_explicit_invalidation(self):
        credential = Mock()
        credential.get_token.return_value = SimpleNamespace(token="synthetic", expires_on=5000)
        with patch("opencode_ci.auth._create_credential", return_value=credential) as create:
            auth = EntraAuth("api://example-app")
            create.assert_not_called()
            with patch("opencode_ci.auth.time.time", return_value=1000):
                auth.headers()
                auth.invalidate()
                auth.headers()
            create.assert_called_once()
            self.assertEqual(credential.get_token.call_count, 2)
            auth.close()
            credential.close.assert_called_once()

    def test_auth_failure_does_not_expose_sensitive_error(self):
        credential = Mock()
        credential.get_token.side_effect = RuntimeError("SENSITIVE_TOKEN_AND_ACCOUNT")
        auth = EntraAuth("api://example-app", credential=credential)
        with self.assertRaises(ValueError) as error:
            auth.headers()
        self.assertNotIn("SENSITIVE_TOKEN_AND_ACCOUNT", str(error.exception))
        self.assertIn("az login", str(error.exception))

    def test_invalid_and_empty_resources(self):
        self.assertEqual(normalize_resource(" api://example-app/ "), "api://example-app")
        self.assertEqual(normalize_resource(""), "")
        for value in (None, "not-a-resource", "api://example-app/.default", "https://user:password@example.com", "api://bad app"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_resource(value)

    def test_entra_401_refresh_uses_same_payload_and_never_static_fallback(self):
        uploader = Uploader(Path("."), "https://ci.example.com", auth_resource="api://example-app")
        uploader.auth = Mock()
        uploader.auth.headers.side_effect = [{"Authorization": "Bearer first"}, {"Authorization": "Bearer second"}]
        response = Mock()
        response.__enter__ = Mock(return_value=SimpleNamespace(status=202))
        response.__exit__ = Mock(return_value=False)
        uploader.opener.open = Mock(side_effect=[HTTPError("https://ci.example.com/events", 401, "Unauthorized", {}, io.BytesIO(b"sensitive-server-body")), response])
        uploader._send({"idempotency_key": "stable-synthetic-id"})
        self.assertEqual(uploader.auth.headers.call_count, 2)
        uploader.auth.invalidate.assert_called_once()
        first, second = [call.args[0] for call in uploader.opener.open.call_args_list]
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.get_header("Authorization"), "Bearer first")
        self.assertEqual(second.get_header("Authorization"), "Bearer second")
        self.assertEqual(second.get_header("Content-type"), "application/json")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-ci-settings-", dir=Path.home() / ".cache")
        self.state = Path(self.temp.name) / "state"

    def tearDown(self):
        self.temp.cleanup()

    def test_blank_public_defaults_and_private_round_trip(self):
        defaults = {"url": "", "auth_resource": "", "include_children": True, "include_reasoning": True}
        self.assertEqual(load_settings(self.state), defaults)
        expected = {"url": "https://ci.example.com", "auth_resource": "api://example-app", "include_children": False, "include_reasoning": False}
        with patch("opencode_ci.auth._create_credential") as credential:
            save_settings(self.state, expected)
            credential.assert_not_called()
        self.assertEqual(load_settings(self.state), expected)
        self.assertEqual((self.state / "settings.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(self.state.glob(".settings-*")))
        save_settings(self.state, {"url": "", "auth_resource": ""})
        self.assertEqual(load_settings(self.state), defaults)

    def test_existing_connection_only_settings_keep_enabled_defaults(self):
        self.state.mkdir()
        (self.state / "settings.json").write_text(json.dumps({"url": "https://ci.example.com", "auth_resource": "api://example-app"}))
        loaded = load_settings(self.state)
        self.assertTrue(loaded["include_children"])
        self.assertTrue(loaded["include_reasoning"])

    def test_rejects_credentials_and_invalid_settings(self):
        for value in ({"url": 42}, {"api_key": "must-not-be-saved"}, {"url": "https://user:password@ci.example.com"}, {"auth_resource": None}, {"include_children": "false"}, {"include_reasoning": None}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                save_settings(self.state, value)
        self.assertFalse((self.state / "settings.json").exists())

    def test_settings_change_blocked_during_upload(self):
        uploader = Uploader(self.state, auth_mode="entra")
        uploader.lock.acquire()
        try:
            persist = Mock()
            with self.assertRaisesRegex(ValueError, "current upload"):
                uploader.configure("https://ci.example.com", "api://example-app", persist=persist)
            persist.assert_not_called()
        finally:
            uploader.lock.release()

    def test_confirmed_destination_cannot_change_before_upload_starts(self):
        uploader = Uploader(self.state, "https://ci.example.com", auth_resource="api://example-app")
        with patch.object(uploader, "_upload") as send, self.assertRaisesRegex(ValueError, "settings changed"):
            uploader.upload(Path("unused"), {}, expected_connection=("https://other.example.com", "api://example-app"))
        send.assert_not_called()

    def test_configuration_api_persistence_auth_and_no_network(self):
        server, token = make_server(Path("unused.db"), self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        expected = {"url": "https://ci.example.com", "auth_resource": "api://example-app", "include_children": False, "include_reasoning": False}
        headers = {"Content-Type": "application/json", "X-Local-Token": token}
        try:
            with patch("opencode_ci.auth._create_credential") as create, patch("opencode_ci.upload.Uploader._send") as send:
                client.request("POST", "/api/config", json.dumps(expected), {"Content-Type": "application/json"})
                response = client.getresponse(); self.assertEqual(response.status, 403); response.read()
                client.request("POST", "/api/config", json.dumps(expected), headers)
                response = client.getresponse(); self.assertEqual(response.status, 200)
                saved = json.loads(response.read())
                self.assertEqual(saved["url"], expected["url"])
                self.assertEqual(saved["auth_resource"], expected["auth_resource"])
                self.assertEqual(saved["auth_mode"], "entra")
                self.assertFalse(saved["include_children"])
                self.assertFalse(saved["include_reasoning"])
                self.assertTrue(saved["upload_available"])
                self.assertNotIn("api_key", saved)
                self.assertNotIn("token", saved)
                client.request("GET", "/api/config", headers={"X-Local-Token": token})
                response = client.getresponse(); self.assertEqual(json.loads(response.read())["auth_resource"], expected["auth_resource"])
                # Older clients saving only connection fields must not reset preferences.
                client.request("POST", "/api/config", json.dumps({key: expected[key] for key in ("url", "auth_resource")}), headers)
                response = client.getresponse(); saved = json.loads(response.read())
                self.assertFalse(saved["include_children"])
                self.assertFalse(saved["include_reasoning"])
                called = threading.Event()
                def export(*args, **kwargs):
                    called.set()
                    return {}
                with patch("opencode_ci.server.export_sessions", side_effect=export) as export_mock:
                    client.request("POST", "/api/export", '{"sessions":["ses_synthetic"]}', headers)
                    response = client.getresponse(); self.assertEqual(response.status, 202); response.read()
                    self.assertTrue(called.wait(2))
                    self.assertFalse(export_mock.call_args.kwargs["include_children"])
                    self.assertFalse(export_mock.call_args.kwargs["include_reasoning"])
                create.assert_not_called(); send.assert_not_called()
            self.assertEqual(load_settings(self.state), expected)
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()
        # A fresh app reads the same private values without acquiring a token.
        with patch("opencode_ci.auth._create_credential") as create:
            restarted, _ = make_server(Path("unused.db"), self.state, port=0)
            restarted.server_close()
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
