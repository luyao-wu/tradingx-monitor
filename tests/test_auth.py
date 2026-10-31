import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tradingx_monitor.auth import connect_account, login
from tradingx_monitor.client import FetchError
from tradingx_monitor.config import Config
from tradingx_monitor.store import Store
from tradingx_monitor.web import make_server


class AccountLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), port=0)
        self.store = Store(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_password_only_goes_to_original_https_login_endpoint(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"token":"fake-test-token"}'
        token = login(self.config, "test@example.com", "fake-test-password", opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://wall-street-skill.com/api/login")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"method": "password", "email": "test@example.com", "password": "fake-test-password"})
        self.assertEqual(token, "fake-test-token")
        self.assertFalse(self.config.token_file.exists())

    def test_failed_login_preserves_existing_token_and_hides_remote_error(self):
        self.config.token_file.write_text("previous-token")
        self.config.token_file.chmod(0o600)
        opener = Mock()
        opener.open.side_effect = HTTPError("https://wall-street-skill.com/api/login", 401, "fake-test-password", {}, io.BytesIO(b"private response"))
        with self.assertRaises(FetchError) as error:
            login(self.config, "test@example.com", "fake-test-password", opener)
        self.assertNotIn("fake-test-password", str(error.exception))
        self.assertEqual(self.config.token_file.read_text(), "previous-token")

    def test_connection_saves_token_only_and_verifies_full_content(self):
        post = {"id": 1, "createdAt": "2026-10-23T03:57:25Z", "editedAt": None, "likes": 0, "views": 0,
                "locked": False, "text": "模拟会员正文", "textLength": 6, "images": [], "url": "https://wall-street-skill.com/tradingx/1"}
        snapshot = {"posts": [post], "viewer": "member", "total": 1, "pages": 1, "truncated": False, "auth_configured": True}
        with patch.dict(os.environ):
            os.environ.pop("TRADINGX_TOKEN", None)
            with patch("tradingx_monitor.auth.login", return_value="fake-test-token"), patch("tradingx_monitor.client.Client.fetch", return_value=snapshot):
                result = connect_account(self.config, self.store, "test@example.com", "fake-test-password")
        self.assertTrue(result["verified"])
        self.assertEqual(result["readable_posts"], 1)
        self.assertNotIn("fake-test-token", json.dumps(result))
        self.assertEqual(self.config.token_file.read_text(), "fake-test-token\n")
        self.assertEqual(self.config.token_file.stat().st_mode & 0o777, 0o600)
        for filename in Path(self.temp.name).rglob("*"):
            if filename.is_file():
                self.assertNotIn(b"fake-test-password", filename.read_bytes())

    def test_web_login_requires_same_origin_and_csrf_before_authentication(self):
        server = make_server(self.config, self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        body = json.dumps({"email": "test@example.com", "password": "fake-test-password"}).encode()
        try:
            with urlopen(base + "/api/auth/session") as response:
                csrf = json.load(response)["csrf"]
            with patch("tradingx_monitor.auth.connect_account", return_value={"connected": True, "message": "已接入"}) as connect:
                for origin, nonce in (("https://attacker.example", csrf), (base, "bad-nonce")):
                    request = Request(base + "/api/auth/login", body, {"Content-Type": "application/json", "Origin": origin, "X-CSRF-Token": nonce})
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request)
                    self.assertEqual(error.exception.code, 403)
                connect.assert_not_called()
                request = Request(base + "/api/auth/login", body, {"Content-Type": "application/json", "Origin": base, "X-CSRF-Token": csrf})
                with urlopen(request) as response:
                    self.assertTrue(json.load(response)["connected"])
                connect.assert_called_once_with(self.config, self.store, "test@example.com", "fake-test-password")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
