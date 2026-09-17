"""Route + scheduler tests for the proxy feed and auto top-up registration."""
import os
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

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
    """Event stand-in that records wait() durations and stops after N of them.

    Patched over both _scheduler_stop and _scheduler_wake: the loop waits on the
    wake event and consults the stop event to exit, so one double covering both
    still captures the wait durations the backoff tests assert on.
    """

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
            web_app, "_scheduler_wake", stop
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


CFG = {
    "enabled": True,
    "interval": 21600,
    "target": 50,
    "min_valid": 10,
    "max_per_round": 20,
}


def _reset_auto_register_state():
    """Back to 'no tick yet' — the cache is module-global and shared by tests."""
    with web_app._auto_register_state_lock:
        for key in web_app._auto_register_state:
            web_app._auto_register_state[key] = None


class _StateMixin:
    def setUp(self):
        with web_app._auto_register_state_lock:
            self._saved_state = dict(web_app._auto_register_state)
        _reset_auto_register_state()

    def tearDown(self):
        with web_app._auto_register_state_lock:
            web_app._auto_register_state.update(self._saved_state)


class TestAutoRegisterStatusRoute(_StateMixin, unittest.IsolatedAsyncioTestCase):
    """GET /api/register/status must expose cached scheduler state, never scan."""

    def setUp(self):
        super().setUp()
        self.client = web_app.app.test_client()

    async def _status(self):
        with patch.object(web_app, "_is_authed", return_value=True):
            r = await self.client.get("/api/register/status")
            return r, await r.get_json()

    async def test_before_any_tick_fields_are_null_not_a_scan(self):
        with patch.object(
            web_app, "collect_valid_proxy_lines", side_effect=AssertionError("scanned")
        ), patch.object(
            web_app, "_count_feed_eligible", side_effect=AssertionError("scanned")
        ):
            r, body = await self._status()
        self.assertEqual(r.status_code, 200)
        ar = body["auto_register"]
        self.assertEqual(set(ar), set(web_app._AUTO_REGISTER_STATUS_KEYS))
        # no tick yet: unknown, not guessed and not measured
        self.assertTrue(all(v is None for v in ar.values()))

    async def test_reports_cached_values_without_rescanning(self):
        web_app._publish_auto_register_state(CFG, "enough", 99, 1_800_000_000.0)
        with patch.object(
            web_app, "collect_valid_proxy_lines", side_effect=AssertionError("scanned")
        ), patch.object(
            web_app, "_count_feed_eligible", side_effect=AssertionError("scanned")
        ):
            _, body = await self._status()
        ar = body["auto_register"]
        self.assertTrue(ar["enabled"])
        self.assertEqual(ar["interval"], 21600)
        self.assertEqual(ar["target"], 50)
        self.assertEqual(ar["min_valid"], 10)
        self.assertEqual(ar["next_run_at"], 1_800_000_000.0)
        self.assertEqual(ar["last_action"], "enough")
        self.assertEqual(ar["last_have"], 99)

    async def test_next_run_at_is_null_while_a_round_is_running(self):
        web_app._publish_auto_register_state(CFG, "started", 3, None)
        _, body = await self._status()
        ar = body["auto_register"]
        self.assertIsNone(ar["next_run_at"])
        self.assertEqual(ar["last_action"], "started")
        self.assertEqual(ar["last_have"], 3)

    async def test_status_route_requires_login(self):
        r = await self.client.get("/api/register/status")
        self.assertEqual(r.status_code, 401)


class TestAutoRegisterTransitionLog(_StateMixin, unittest.TestCase):
    """One line per meaningful change — never one per tick."""

    def setUp(self):
        super().setUp()
        web_app._clear_logs()

    def _lines(self):
        return web_app._get_logs(0)["lines"]

    def test_first_tick_logs_once_then_repeats_are_silent(self):
        web_app._log_auto_register_transition(dict(CFG), "enough", 99)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("interval=21600s", lines[0])
        self.assertIn("决定=enough(可供给=99)", lines[0])

        # A disabled scheduler ticks every 60s and an enabled one every
        # interval; neither may log while nothing changed.
        for _ in range(5):
            web_app._log_auto_register_transition(dict(CFG), "enough", 99)
        self.assertEqual(len(self._lines()), 1)

    def test_enabled_to_disabled_logs_both_sides_once(self):
        web_app._log_auto_register_transition(dict(CFG), "enough", 99)
        off = dict(CFG, enabled=False)
        web_app._log_auto_register_transition(off, "disabled", None)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("配置变更", lines[1])
        self.assertIn("enabled=true", lines[1])
        self.assertIn("enabled=false", lines[1])
        self.assertIn("决定=disabled", lines[1])

        web_app._log_auto_register_transition(dict(off), "disabled", None)
        self.assertEqual(len(self._lines()), 2)

    def test_disabled_to_enabled_logs(self):
        web_app._log_auto_register_transition(dict(CFG, enabled=False), "disabled", None)
        self.assertEqual(len(self._lines()), 1)
        web_app._log_auto_register_transition(dict(CFG), "enough", 5)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("enabled=false", lines[1])
        self.assertIn("enabled=true", lines[1])

    def test_each_tracked_field_logs_when_it_changes(self):
        for field, value in (
            ("interval", 3600),
            ("min_valid", 25),
            ("target", 80),
        ):
            with self.subTest(field=field):
                _reset_auto_register_state()
                web_app._clear_logs()
                web_app._log_auto_register_transition(dict(CFG), "enough", 1)
                web_app._log_auto_register_transition(
                    dict(CFG, **{field: value}), "enough", 1
                )
                self.assertEqual(len(self._lines()), 2)

    def test_decision_is_recorded_even_when_nothing_is_logged(self):
        web_app._log_auto_register_transition(dict(CFG), "enough", 7)
        web_app._publish_auto_register_state(CFG, "enough", 7, 1.0)
        # a different decision with an unchanged config: state only, no line
        web_app._publish_auto_register_state(CFG, "busy", 7, None)
        web_app._log_auto_register_transition(dict(CFG), "busy", 7)
        self.assertEqual(len(self._lines()), 1)
        st = web_app.get_auto_register_status()
        self.assertEqual(st["last_action"], "busy")
        self.assertEqual(st["last_have"], 7)

    def test_silent_loop_branches_do_not_spam_the_log(self):
        stop = _FakeStop(4)
        with patch.dict(os.environ, {"AUTO_REGISTER_INTERVAL": "21600"}), patch.object(
            web_app, "_scheduler_stop", stop
        ), patch.object(
            web_app, "_scheduler_wake", stop
        ), patch.object(
            web_app, "_maybe_auto_register_once", return_value={"action": "enough", "have": 99}
        ):
            web_app._auto_register_loop()
        auto_lines = [ln for ln in self._lines() if "[auto]" in ln]
        self.assertEqual(len(auto_lines), 1)
        self.assertEqual(stop.waits, [21600.0] * 4)


class TestSchedulerWake(unittest.TestCase):
    """Change C': a config save must cut the sleep short, without polling slices."""

    def test_long_interval_is_waited_in_one_piece(self):
        stop = _FakeStop(1)
        env = {"AUTO_REGISTER_ENABLED": "true", "AUTO_REGISTER_INTERVAL": "21600"}
        with patch.dict(os.environ, env), patch.object(
            web_app, "_scheduler_stop", stop
        ), patch.object(
            web_app, "_scheduler_wake", stop
        ), patch.object(
            web_app, "_maybe_auto_register_once",
            return_value={"action": "enough", "have": 99},
        ):
            web_app._auto_register_loop()
        # one 6h wait, not 360 x 60s polls
        self.assertEqual(stop.waits, [21600.0])

    def test_wake_cuts_a_six_hour_wait_short(self):
        with web_app._auto_register_state_lock:
            saved_state = dict(web_app._auto_register_state)
        _reset_auto_register_state()
        web_app._scheduler_stop.clear()
        web_app._scheduler_wake.clear()
        decision = Mock(return_value={"action": "enough", "have": 99})
        worker = None
        try:
            with patch.dict(
                os.environ,
                {"AUTO_REGISTER_ENABLED": "true", "AUTO_REGISTER_INTERVAL": "21600"},
            ), patch.object(web_app, "_maybe_auto_register_once", decision):
                worker = threading.Thread(
                    target=web_app._auto_register_loop, name="test-auto-register", daemon=True
                )
                worker.start()
                self.assertTrue(self._wait_until(lambda: decision.call_count >= 1))
                self.assertTrue(
                    self._wait_until(
                        lambda: web_app.get_auto_register_status()["next_run_at"] is not None
                    )
                )
                next_run_at = web_app.get_auto_register_status()["next_run_at"]
                self.assertGreater(next_run_at - time.time(), 3600 * 5)

                # this is what POST /api/config does
                started = time.time()
                web_app._scheduler_wake.set()
                self.assertTrue(self._wait_until(lambda: decision.call_count >= 2))
                elapsed = time.time() - started
            self.assertLess(elapsed, 2.0)
        finally:
            web_app._scheduler_stop.set()
            web_app._scheduler_wake.set()
            if worker is not None:
                worker.join(timeout=5)
            web_app._scheduler_stop.clear()
            web_app._scheduler_wake.clear()
            with web_app._auto_register_state_lock:
                web_app._auto_register_state.update(saved_state)

    def test_config_save_sets_the_wake_event(self):
        web_app._scheduler_wake.clear()
        with patch.object(web_app, "upsert_env_file", return_value={"updated": ["AUTO_REGISTER_ENABLED"]}), patch.object(
            web_app, "apply_updates_to_environ"
        ), patch.object(web_app, "reload_main_module_config", return_value=[]), patch.object(
            web_app, "_is_authed", return_value=True
        ):
            client = web_app.app.test_client()
            r = await_result(
                client.post(
                    "/api/config",
                    json={"values": {"AUTO_REGISTER_ENABLED": "true"}},
                )
            )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(web_app._scheduler_wake.is_set())
        web_app._scheduler_wake.clear()

    def test_check_now_route_sets_the_wake_event(self):
        web_app._scheduler_wake.clear()
        with patch.object(web_app, "_is_authed", return_value=True):
            client = web_app.app.test_client()
            r = await_result(client.post("/api/register/check-now"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(web_app._scheduler_wake.is_set())
        web_app._scheduler_wake.clear()

    def test_check_now_route_requires_login(self):
        client = web_app.app.test_client()
        r = await_result(client.post("/api/register/check-now"))
        self.assertEqual(r.status_code, 401)

    @staticmethod
    def _wait_until(predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()


def await_result(awaitable):
    """Run a Quart test-client coroutine from a plain unittest.TestCase."""
    import asyncio

    return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
