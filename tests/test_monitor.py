import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tradingx_monitor.client import Client, FetchError, normalize_post, read_token, retry_after_seconds
from tradingx_monitor.config import Config
from tradingx_monitor.runner import collect, lock, next_delay
from tradingx_monitor.store import Store, now
from tradingx_monitor.web import make_server


def post(post_id=1, text=None, **kwargs):
    record = {"id": post_id, "createdAt": "2026-10-23T03:57:25Z", "editedAt": None,
              "likes": 8, "views": 55, "locked": text is None, "textLength": len(text) if text else 94,
              "images": [{"id": "image-1", "thumb": "data:image/jpeg;base64,example"}]}
    if text is not None:
        record["text"] = text
    record.update(kwargs)
    return normalize_post(record)


def snapshot(posts, **kwargs):
    value = {"posts": posts, "viewer": "guest", "total": len(posts), "pages": 1,
             "truncated": False, "auth_configured": False}
    value.update(kwargs)
    return value


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), port=0)
        self.store = Store(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, records, **kwargs):
        return self.store.success(snapshot(records, **kwargs), now())

    def test_baseline_then_duplicates_and_counters_are_quiet(self):
        self.assertTrue(self.ingest([post()])["baseline"])
        result = self.ingest([post(likes=99, views=999)])
        self.assertEqual((result["new"], result["changed"]), (0, 0))
        self.assertEqual(self.store.status()["stored_posts"], 1)
        self.assertEqual(len(self.store.revisions(1)), 1)
        self.assertEqual([e["type"] for e in self.store.events()], ["baseline"])
        self.assertEqual(self.store.posts()[0]["views"], 999)

    def test_new_and_edited_posts_generate_distinct_events(self):
        self.ingest([post(1, "原始正文")])
        self.assertEqual(self.ingest([post(2), post(1, "原始正文")])["new"], 1)
        self.assertEqual(self.ingest([post(2), post(1, "编辑正文")])["changed"], 1)
        self.assertEqual([e["type"] for e in self.store.events()], ["updated", "new_post", "baseline"])

    def test_empty_baseline_does_not_hide_first_new_post(self):
        self.ingest([])
        self.assertEqual(self.ingest([post()])["new"], 1)

    def test_unlock_and_relock_preserve_historical_text(self):
        self.ingest([post()])
        self.ingest([post(text="会员正文")], viewer="member", auth_configured=True)
        self.assertIn("unlocked", [e["type"] for e in self.store.events()])
        self.ingest([post(textLength=4)])
        stored = self.store.posts()[0]
        self.assertTrue(stored["locked"])
        self.assertIsNone(stored["text"])
        self.assertEqual(stored["cachedText"], "会员正文")
        self.assertIsNotNone(stored["cachedAt"])
        self.assertEqual(self.store.status()["readable_archived_posts"], 1)

    def test_failure_keeps_previous_success_and_recovery_is_recorded(self):
        self.ingest([post()])
        success = self.store.status()["last_success"]
        error = FetchError("rate_limited", "网站限制请求频率", 600)
        self.store.failure(error, now())
        self.store.failure(error, now())
        state = self.store.status()
        self.assertFalse(state["healthy"])
        self.assertEqual(state["last_success"], success)
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertEqual([e["type"] for e in self.store.events()].count("monitor_error"), 1)
        self.ingest([post()])
        self.assertTrue(self.store.status()["healthy"])
        self.assertIn("recovered", [e["type"] for e in self.store.events()])

    def test_event_cursor_never_skips_a_burst(self):
        self.ingest([])
        self.ingest([post(i) for i in range(1, 8)])
        first = self.store.events(limit=3, after=0)
        second = self.store.events(limit=3, after=first[-1]["id"])
        third = self.store.events(limit=3, after=second[-1]["id"])
        ids = [e["id"] for e in first + second + third]
        self.assertEqual(ids, list(range(1, 9)))

    def test_partial_fetch_failure_does_not_commit_a_baseline(self):
        with patch.object(Client, "fetch", side_effect=FetchError("pagination_changed", "分页格式变化")):
            _, error = collect(self.config, self.store)
        self.assertEqual(error.code, "pagination_changed")
        self.assertEqual(self.store.status()["stored_posts"], 0)
        self.assertFalse(self.store.status().get("initialized", False))

    def test_token_permissions_and_single_instance_lock(self):
        self.config.token_file.write_text("fake-test-token")
        self.config.token_file.chmod(0o644)
        with patch.dict(os.environ):
            os.environ.pop("TRADINGX_TOKEN", None)
            with self.assertRaises(FetchError) as error:
                read_token(self.config)
            self.assertEqual(error.exception.code, "token_permissions")
        with lock(self.config, "run.lock"):
            with self.assertRaises(FetchError):
                with lock(self.config, "run.lock"):
                    self.fail("second instance acquired lock")

    def test_health_is_stale_even_when_last_request_had_no_error(self):
        self.ingest([post()])
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with self.store.connect() as db:
            self.store._update(db, last_success=old)
        self.assertFalse(self.store.status()["healthy"])

    def test_read_only_dashboard_and_host_guard(self):
        self.ingest([post()])
        server = make_server(self.config, self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/api/posts") as response:
                self.assertEqual(json.load(response)["posts"][0]["id"], 1)
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
            for route, status in (("/healthz", 200), ("/", 200), ("/app.js", 200), ("/style.css", 200)):
                with urlopen(base + route) as response:
                    self.assertEqual(response.status, status)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/posts", headers={"Host": "attacker.example"}))
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/api/events?after=bad")
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class ClientTests(unittest.TestCase):
    def make_client(self, pages, max_pages=10):
        config = Config(root=Path("/nonexistent-test-root"), max_pages=max_pages)
        client = Client(config)
        client._get = unittest.mock.Mock(side_effect=pages)
        return client

    def test_pagination_uses_before_and_reports_incomplete_coverage(self):
        pages = [{"posts": [post(3), post(2)], "hasMore": True, "viewer": "guest", "total": 3},
                 {"posts": [post(1)], "hasMore": False, "viewer": "guest", "total": 3}]
        with patch("tradingx_monitor.client.read_token", return_value=""):
            client = self.make_client(pages)
            result = client.fetch()
            self.assertEqual([p["id"] for p in result["posts"]], [3, 2, 1])
            self.assertTrue(client._get.call_args_list[1].args[0].endswith("?before=2"))
            self.assertFalse(result["truncated"])
            self.assertTrue(self.make_client(pages, max_pages=1).fetch()["truncated"])

    def test_repeated_pages_and_access_change_fail_closed(self):
        first = {"posts": [post(3)], "hasMore": True, "viewer": "guest"}
        with patch("tradingx_monitor.client.read_token", return_value=""):
            with self.assertRaises(FetchError) as error:
                self.make_client([first, first]).fetch()
            self.assertEqual(error.exception.code, "pagination_changed")
            second = {"posts": [post(2)], "hasMore": False, "viewer": "member"}
            with self.assertRaises(FetchError) as error:
                self.make_client([first, second]).fetch()
            self.assertEqual(error.exception.code, "access_changed_mid_fetch")

    def test_locked_post_never_saves_supplied_body_or_original_image(self):
        raw = copy.deepcopy(post())
        raw["text"] = "must not be saved"
        raw["images"][0]["url"] = "/api/private-image"
        cleaned = normalize_post(raw)
        self.assertIsNone(cleaned["text"])
        self.assertNotIn("url", cleaned["images"][0])
        for broken in ({"id": 1}, {**raw, "createdAt": "yesterday"}, {**raw, "id": True}):
            with self.assertRaises(FetchError):
                normalize_post(broken)

    def test_backoff_respects_retry_after_and_regular_jitter(self):
        config = Config()
        self.assertEqual(next_delay(config, 1, FetchError("rate_limited", "wait", 3600)), 3600)
        self.assertEqual(next_delay(config, 100), config.max_backoff_seconds)
        self.assertTrue(108 <= next_delay(config, 0) <= 132)
        self.assertEqual(retry_after_seconds("600"), 600)
        self.assertEqual(retry_after_seconds("garbage"), 0)

    def test_http_errors_do_not_echo_remote_body_or_token(self):
        secret = "fake-sensitive-test-token"
        for code, expected in ((401, "auth_required"), (403, "auth_required"), (429, "rate_limited"), (503, "http_503")):
            client = Client(Config())
            with patch.object(client.opener, "open", side_effect=HTTPError("http://example", code, secret, {"Retry-After": "600"}, None)):
                with self.assertRaises(FetchError) as error:
                    client._get("http://example", secret)
            self.assertEqual(error.exception.code, expected)
            self.assertNotIn(secret, str(error.exception))
            self.assertEqual(error.exception.retry_after, 600)

    def test_redirect_does_not_forward_bearer_credentials(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append(self.path)
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/destination")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(FetchError) as error:
                Client(Config())._get(f"http://127.0.0.1:{server.server_port}/source", "fake-token")
            self.assertEqual(error.exception.code, "redirect_blocked")
            self.assertEqual(received, ["/source"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
