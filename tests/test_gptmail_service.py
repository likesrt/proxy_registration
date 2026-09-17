"""Unit tests for GPTMail public API key fetching + in-process caching."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.gptmail_service as g
from src.gptmail_service import (
    DEFAULT_PUBLIC_KEY_URL,
    GPTMailService,
    fetch_public_api_key,
    get_public_api_key,
)


class _Resp:
    def __init__(self, status_code=200, body=None, bad_json=False):
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json
        self.text = "" if body is None else str(body)

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._body


def _reset_cache():
    g._public_key_cache["key"] = None
    g._public_key_cache["fetched_at"] = 0.0


class TestPublicKeyFetch(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_parses_data_key_and_sends_expected_headers(self):
        ok = _Resp(200, {"data": {"key": "pk-123"}})
        with patch.object(g.requests, "get", return_value=ok) as m:
            self.assertEqual(fetch_public_api_key(), "pk-123")
        self.assertEqual(m.call_args.args[0], DEFAULT_PUBLIC_KEY_URL)
        headers = m.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Public-Key-Reveal"], "click")
        self.assertEqual(headers["Referer"], "https://mail.chatgpt.org.uk/zh/api/")
        self.assertEqual(headers["Accept"], "*/*")

    def test_accepts_key_at_top_level_too(self):
        with patch.object(g.requests, "get", return_value=_Resp(200, {"key": "flat"})):
            self.assertEqual(fetch_public_api_key(), "flat")

    def test_failures_return_none_and_never_raise(self):
        cases = [
            _Resp(500, {"data": {"key": "x"}}),
            _Resp(200, {"data": {}}),
            _Resp(200, {"data": {"key": "  "}}),
            _Resp(200, None, bad_json=True),
            _Resp(200, ["not", "a", "dict"]),
        ]
        for resp in cases:
            with self.subTest(resp=resp.status_code):
                with patch.object(g.requests, "get", return_value=resp):
                    self.assertIsNone(fetch_public_api_key())
        with patch.object(g.requests, "get", side_effect=Exception("offline")):
            self.assertIsNone(fetch_public_api_key())

    def test_url_override_from_env(self):
        ok = _Resp(200, {"data": {"key": "k"}})
        with patch.dict(os.environ, {"GPTMAIL_PUBLIC_KEY_URL": "https://example.test/k"}), patch.object(
            g.requests, "get", return_value=ok
        ) as m:
            fetch_public_api_key()
        self.assertEqual(m.call_args.args[0], "https://example.test/k")

    def test_explicit_url_argument_wins(self):
        ok = _Resp(200, {"data": {"key": "k"}})
        with patch.dict(os.environ, {"GPTMAIL_PUBLIC_KEY_URL": "https://example.test/k"}), patch.object(
            g.requests, "get", return_value=ok
        ) as m:
            fetch_public_api_key(url="https://explicit.test/x")
        self.assertEqual(m.call_args.args[0], "https://explicit.test/x")


class TestPublicKeyCache(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_ttl_window_requests_once(self):
        ok = _Resp(200, {"data": {"key": "pk-1"}})
        with patch.object(g.requests, "get", return_value=ok) as m:
            self.assertEqual(get_public_api_key(), "pk-1")
            self.assertEqual(get_public_api_key(), "pk-1")
            self.assertEqual(m.call_count, 1)
            self.assertEqual(get_public_api_key(force=True), "pk-1")
            self.assertEqual(m.call_count, 2)

    def test_expired_ttl_refetches(self):
        ok = _Resp(200, {"data": {"key": "pk-1"}})
        with patch.object(g.requests, "get", return_value=ok) as m:
            get_public_api_key()
            g._public_key_cache["fetched_at"] = 0.0  # pretend the hour elapsed
            get_public_api_key()
            self.assertEqual(m.call_count, 2)

    def test_failure_falls_back_to_last_known_key(self):
        with patch.object(g.requests, "get", return_value=_Resp(200, {"data": {"key": "pk-1"}})):
            get_public_api_key()
        g._public_key_cache["fetched_at"] = 0.0
        with patch.object(g.requests, "get", side_effect=Exception("down")):
            self.assertEqual(get_public_api_key(), "pk-1")
        # a failed fetch must not poison the cache
        self.assertEqual(g._public_key_cache["key"], "pk-1")

    def test_failure_with_cold_cache_returns_none(self):
        with patch.object(g.requests, "get", side_effect=Exception("down")):
            self.assertIsNone(get_public_api_key())


class TestServiceApiKeyResolution(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_configured_env_key_wins(self):
        with patch.dict(os.environ, {"GPTMAIL_API_KEY": "env-key"}), patch.object(
            g.requests, "get", side_effect=AssertionError("must not call the API")
        ):
            self.assertEqual(GPTMailService().api_key, "env-key")

    def test_blank_env_key_uses_public_endpoint(self):
        with patch.dict(os.environ, {"GPTMAIL_API_KEY": ""}), patch.object(
            g.requests, "get", return_value=_Resp(200, {"data": {"key": "pk-9"}})
        ):
            self.assertEqual(GPTMailService().api_key, "pk-9")

    def test_falls_back_to_gpt_test_when_nothing_works(self):
        with patch.dict(os.environ, {"GPTMAIL_API_KEY": ""}), patch.object(
            g.requests, "get", side_effect=Exception("offline")
        ):
            self.assertEqual(GPTMailService().api_key, "gpt-test")

    def test_never_written_back_to_env_file(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "src", "gptmail_service.py"), encoding="utf-8") as f:
            src = f.read()
        # the cache is process-local on purpose: no dotenv write / set_key anywhere
        self.assertNotIn("set_key", src)
        self.assertNotIn("dotenv.set_key", src)


if __name__ == "__main__":
    unittest.main()
