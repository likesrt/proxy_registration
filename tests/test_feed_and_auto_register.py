"""Route + scheduler tests for the proxy feed and auto top-up registration."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_app


SNAP = {
    "lines": ["http://u:p@1.1.1.1:1", "https://u:p@2.2.2.2:2"],
    "accounts": [
        {
            "email": "a@x.com",
            "account_id": "a1",
            "proxy_file": "proxies_a1.txt",
            "state": "valid",
            "reason": "",
            "line_count": 2,
        },
        {
            "email": "b@x.com",
            "account_id": "b1",
            "proxy_file": None,
            "state": "unknown",
            "reason": "no_proxy_file",
            "line_count": 0,
        },
        {
            "email": "c@x.com",
            "account_id": "c1",
            "proxy_file": None,
            "state": "expired",
            "reason": "expired",
            "line_count": 0,
        },
    ],
    "valid_count": 1,
    "feed_eligible_count": 1,
    "expired_count": 1,
    "unknown_count": 1,
    "skipped_count": 1,
}

FEED_PATH = "/api/feed/proxies"
TOKEN_ENV = {"FEED_TOKEN": "s3cret-token"}


class TestFeedRoute(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = web_app.app.test_client()

    async def _get(self, path, headers=None):
        return await self.client.get(path, headers=headers or {})

    # --- auth ---------------------------------------------------------------

    async def test_empty_token_is_503(self):
        with patch.dict(os.environ, {"FEED_TOKEN": ""}):
            r = await self._get(FEED_PATH)
        self.assertEqual(r.status_code, 503)
        # must never fall through to an unauthenticated feed
        self.assertNotIn("http://", await r.get_data(as_text=True))

    async def test_unset_token_env_is_503(self):
        env = {k: v for k, v in os.environ.items() if k != "FEED_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            r = await self._get(FEED_PATH)
        self.assertEqual(r.status_code, 503)

    async def test_wrong_token_is_401(self):
        with patch.dict(os.environ, TOKEN_ENV), patch.object(
            web_app, "collect_valid_proxy_lines", return_value=SNAP
        ):
            r = await self._get(FEED_PATH, headers={"X-Feed-Token": "nope"})
        self.assertEqual(r.status_code, 401)

    async def test_missing_token_is_401_not_200(self):
        with patch.dict(os.environ, TOKEN_ENV), patch.object(
            web_app, "collect_valid_proxy_lines", return_value=SNAP
        ):
            r = await self._get(FEED_PATH)
        self.assertEqual(r.status_code, 401)

    # --- happy path ---------------------------------------------------------

    async def test_header_token_serves_plain_text_feed(self):
        with patch.dict(os.environ, TOKEN_ENV), patch.object(
            web_app, "collect_valid_proxy_lines", return_value=SNAP
        ):
            r = await self._get(FEED_PATH, headers={"X-Feed-Token": "s3cret-token"})
            body = await r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/plain", r.headers["Content-Type"])
        self.assertEqual(
            body, "http://u:p@1.1.1.1:1\nhttps://u:p@2.2.2.2:2\n"
        )
        self.assertEqual(r.headers["X-Proxy-Count"], "2")
        self.assertEqual(r.headers["X-Proxy-Accounts"], "1")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        # a feed is pulled, not downloaded as an attachment
        self.assertNotIn("Content-Disposition", r.headers)
        # expired accounts are not "errors"; the missing proxy file is
        self.assertEqual(r.headers["X-Proxy-Errors"], "b@x.com: no_proxy_file")

    async def test_query_token_serves_feed(self):
        with patch.dict(os.environ, TOKEN_ENV), patch.object(
            web_app, "collect_valid_proxy_lines", return_value=SNAP
        ):
            r = await self._get(FEED_PATH + "?token=s3cret-token")
        self.assertEqual(r.status_code, 200)

    async def test_empty_feed_is_200_with_empty_body(self):
        empty = dict(SNAP, lines=[], accounts=[], feed_eligible_count=0)
        with patch.dict(os.environ, TOKEN_ENV), patch.object(
            web_app, "collect_valid_proxy_lines", return_value=empty
        ):
            r = await self._get(FEED_PATH, headers={"X-Feed-Token": "s3cret-token"})
            body = await r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body, "")
        self.assertEqual(r.headers["X-Proxy-Count"], "0")

    async def test_reachable_without_a_login_session(self):
        """A remote puller has no cookie session; the path must bypass before_request."""
        self.assertIn(FEED_PATH, web_app._AUTH_PUBLIC_PATHS)
        with patch.dict(os.environ, TOKEN_ENV), patch.object(
            web_app, "collect_valid_proxy_lines", return_value=SNAP
        ):
            r = await self._get(FEED_PATH, headers={"X-Feed-Token": "s3cret-token"})
        # neither a login redirect nor the 401 auth error
        self.assertEqual(r.status_code, 200)

    async def test_protected_api_still_requires_login(self):
        """Contrast: the feed is the exception, not a hole in the auth check."""
        self.assertNotIn("/api/accounts", web_app._AUTH_PUBLIC_PATHS)
        r = await self._get("/api/accounts")
        self.assertEqual(r.status_code, 401)

    async def test_collect_failure_is_500_not_a_crash(self):
        with patch.dict(os.environ, TOKEN_ENV), patch.object(
            web_app, "collect_valid_proxy_lines", side_effect=OSError("disk gone")
        ):
            r = await self._get(FEED_PATH, headers={"X-Feed-Token": "s3cret-token"})
        self.assertEqual(r.status_code, 500)


class TestFeedEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Route -> the real collect_valid_proxy_lines -> a temp keys dir.

    The other route tests patch the collector; this one only patches its inputs,
    so a wiring mistake (wrong path, wrong helper, dead account leaking through)
    still fails here.
    """

    async def test_serves_real_files_and_drops_expired_account(self):
        import functools
        import tempfile

        from src.proxyscrape_helpers import collect_valid_proxy_lines as real_collect

        now = 1_700_000_000
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "proxies_live-1.txt"), "w", encoding="utf-8") as f:
                # duplicate + comment + non-http scheme all in one file
                f.write(
                    "# header\n"
                    "http://u:p@1.1.1.1:1\n"
                    "http://u:p@1.1.1.1:1\n"
                    "socks5://u:p@2.2.2.2:2\n"
                )
            with open(os.path.join(td, "proxies_dead-1.txt"), "w", encoding="utf-8") as f:
                f.write("http://dead:p@9.9.9.9:9\n")
            with open(os.path.join(td, "proxies_unknown-1.txt"), "w", encoding="utf-8") as f:
                f.write("https://u:p@3.3.3.3:3\n")

            accounts = [
                {"email": "live@x.com"},
                {"email": "dead@x.com"},
                {"email": "unknown@x.com"},
            ]
            cache = {
                "live@x.com": {
                    "subaccount_id": "live-1",
                    "details": {"ok": True, "expires_at_unix": now + 3600},
                },
                "dead@x.com": {
                    "subaccount_id": "dead-1",
                    "details": {"ok": True, "expires_at_unix": now - 1},
                },
                "unknown@x.com": {
                    "subaccount_id": "unknown-1",
                    "details": {"ok": True},
                },
            }
            bound = functools.partial(
                real_collect, accounts=accounts, cache=cache, keys_dir=td, now_ts=now
            )
            client = web_app.app.test_client()
            with patch.dict(os.environ, TOKEN_ENV), patch.object(
                web_app, "collect_valid_proxy_lines", new=bound
            ):
                r = await client.get(
                    FEED_PATH, headers={"X-Feed-Token": "s3cret-token"}
                )
                body = await r.get_data(as_text=True)

        self.assertEqual(r.status_code, 200)
        # de-duplicated, socks5 dropped, expired account excluded, unknown included
        self.assertEqual(body, "http://u:p@1.1.1.1:1\nhttps://u:p@3.3.3.3:3\n")
        self.assertEqual(r.headers["X-Proxy-Count"], "2")
        self.assertEqual(r.headers["X-Proxy-Accounts"], "2")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    async def test_shared_proxies_txt_is_never_served(self):
        import tempfile

        from src.proxyscrape_helpers import collect_valid_proxy_lines as real_collect

        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "proxies.txt"), "w", encoding="utf-8") as f:
                f.write("http://stale:p@6.6.6.6:6\n")
            import functools

            bound = functools.partial(real_collect, accounts=[], cache={}, keys_dir=td)
            client = web_app.app.test_client()
            with patch.dict(os.environ, TOKEN_ENV), patch.object(
                web_app, "collect_valid_proxy_lines", new=bound
            ):
                r = await client.get(FEED_PATH, headers={"X-Feed-Token": "s3cret-token"})
                body = await r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body, "")


class TestFeedSource(unittest.TestCase):
    """The route must stay read-only and token-gated in the source, too."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web_app.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_token_compared_with_compare_digest(self):
        self.assertIn("hmac.compare_digest", self.src)
        self.assertIn('"/api/feed/proxies"', self.src)

    def test_feed_token_read_per_request(self):
        # config page changes must apply without a restart
        self.assertIn('os.getenv("FEED_TOKEN")', self.src)

    def test_no_store_header(self):
        self.assertIn("Cache-Control", self.src)
        self.assertIn("no-store", self.src)

    def test_removed_resin_chain(self):
        self.assertNotIn("upload-resin", self.src)
        self.assertNotIn("btnUploadResin", self.src)
        self.assertNotIn("uploadAllToResin", self.src)
        self.assertNotIn("upload_proxies_to_resin", self.src)


class _FakeStop:
    """Event stand-in that records wait() durations and stops after N of them."""

    def __init__(self, max_waits):
        self.waits = []
        self._max = max_waits

    def is_set(self):
        return len(self.waits) >= self._max

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return len(self.waits) >= self._max

    def set(self):
        pass

    def clear(self):
        pass


class TestAutoRegisterDecision(unittest.TestCase):
    def setUp(self):
        self._was_running = web_app._register_job.get("running")
        web_app._register_job["running"] = False

    def tearDown(self):
        web_app._register_job["running"] = bool(self._was_running)

    def test_disabled_does_nothing(self):
        with patch.dict(os.environ, {"AUTO_REGISTER_ENABLED": "false"}), patch.object(
            web_app, "collect_valid_proxy_lines"
        ) as collect:
            res = web_app._maybe_auto_register_once()
        self.assertEqual(res["action"], "disabled")
        collect.assert_not_called()

    def test_enough_usable_accounts_does_not_register(self):
        env = {"AUTO_REGISTER_ENABLED": "true", "AUTO_REGISTER_MIN_VALID": "10"}
        with patch.dict(os.environ, env), patch.object(
            web_app, "collect_valid_proxy_lines", return_value={"feed_eligible_count": 25}
        ), patch.object(web_app.threading, "Thread") as thread:
            res = web_app._maybe_auto_register_once()
        self.assertEqual(res, {"action": "enough", "have": 25})
        thread.assert_not_called()

    def test_below_min_starts_capped_round(self):
        env = {
            "AUTO_REGISTER_ENABLED": "true",
            "AUTO_REGISTER_MIN_VALID": "10",
            "AUTO_REGISTER_TARGET": "50",
            "AUTO_REGISTER_MAX_PER_ROUND": "20",
        }
        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), **kwargs):
                started.append((target, args, kwargs.get("daemon")))

            def start(self):
                pass

        with patch.dict(os.environ, env), patch.object(
            web_app, "collect_valid_proxy_lines", return_value={"feed_eligible_count": 3}
        ), patch.object(web_app.threading, "Thread", FakeThread):
            res = web_app._maybe_auto_register_once()

        self.assertEqual(res["action"], "started")
        # target - have = 47, capped to max_per_round = 20
        self.assertEqual(res["needed"], 20)
        self.assertEqual(len(started), 1)
        target, args, daemon = started[0]
        self.assertEqual(args, (20,))
        self.assertIs(target, web_app._run_register_job)
        self.assertTrue(daemon)
        self.assertTrue(web_app._register_job["running"])
        self.assertEqual(web_app._register_job["requested"], 20)

    def test_never_interleaves_with_a_running_manual_job(self):
        web_app._register_job["running"] = True
        env = {"AUTO_REGISTER_ENABLED": "true", "AUTO_REGISTER_MIN_VALID": "10"}
        with patch.dict(os.environ, env), patch.object(
            web_app, "collect_valid_proxy_lines", return_value={"feed_eligible_count": 0}
        ), patch.object(web_app.threading, "Thread") as thread:
            res = web_app._maybe_auto_register_once()
        self.assertEqual(res["action"], "busy")
        thread.assert_not_called()
        self.assertTrue(web_app._register_job["running"])

    def test_count_error_does_not_register(self):
        with patch.dict(os.environ, {"AUTO_REGISTER_ENABLED": "true"}), patch.object(
            web_app, "collect_valid_proxy_lines", side_effect=OSError("boom")
        ):
            res = web_app._maybe_auto_register_once()
        self.assertEqual(res["action"], "error")


class TestAutoRegisterBackoff(unittest.TestCase):
    def setUp(self):
        self._was_running = web_app._register_job.get("running")
        web_app._register_job["running"] = False

    def tearDown(self):
        web_app._register_job["running"] = bool(self._was_running)

    def _run_loop(self, decisions, counts, max_waits):
        stop = _FakeStop(max_waits)
        env = {"AUTO_REGISTER_INTERVAL": "100"}
        with patch.dict(os.environ, env), patch.object(
            web_app, "_scheduler_stop", stop
        ), patch.object(
            web_app, "_maybe_auto_register_once", side_effect=list(decisions)
        ), patch.object(
            web_app, "collect_valid_proxy_lines", side_effect=list(counts)
        ):
            web_app._auto_register_loop()
        return stop.waits

    def test_no_gain_doubles_wait_up_to_6x(self):
        started = {"action": "started", "have": 0, "needed": 1}
        waits = self._run_loop(
            [dict(started)] * 3, [{"feed_eligible_count": 0}] * 3, max_waits=3
        )
        self.assertEqual(waits, [200.0, 400.0, 600.0])

    def test_gain_resets_backoff(self):
        waits = self._run_loop(
            [
                {"action": "started", "have": 0, "needed": 1},
                {"action": "started", "have": 0, "needed": 1},
                {"action": "started", "have": 5, "needed": 1},
            ],
            [
                {"feed_eligible_count": 0},  # no gain -> x2
                {"feed_eligible_count": 5},  # gain -> reset
                {"feed_eligible_count": 5},  # no gain -> x2 again
            ],
            max_waits=3,
        )
        self.assertEqual(waits, [200.0, 100.0, 200.0])

    def test_disabled_polls_soon(self):
        waits = self._run_loop([{"action": "disabled"}] * 2, [], max_waits=2)
        # interval is 100 but a disabled scheduler should re-check sooner
        self.assertEqual(waits, [60.0, 60.0])

    def test_enough_keeps_normal_interval(self):
        waits = self._run_loop(
            [{"action": "enough", "have": 99}] * 2, [], max_waits=2
        )
        self.assertEqual(waits, [100.0, 100.0])


class TestSchedulerWiring(unittest.TestCase):
    def test_import_does_not_start_the_scheduler(self):
        self.assertFalse(web_app._scheduler_started)

    def test_started_from_main_before_app_run(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web_app.py"), encoding="utf-8") as f:
            src = f.read()
        main_body = src[src.index("def main():"):]
        self.assertIn("_start_auto_register_scheduler()", main_body)
        self.assertLess(
            main_body.index("_start_auto_register_scheduler()"),
            main_body.index("app.run("),
        )

    def test_start_is_idempotent(self):
        with patch.object(web_app.threading, "Thread") as thread:
            first = web_app._start_auto_register_scheduler()
            second = web_app._start_auto_register_scheduler()
        try:
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(thread.call_count, 1)
        finally:
            web_app._scheduler_started = False


if __name__ == "__main__":
    unittest.main()
