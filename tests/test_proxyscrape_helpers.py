"""Unit tests for pure ProxyScrape registration helpers (shipped code path)."""
import os
import sys
import tempfile
import unittest

# Project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.proxyscrape_helpers import (
    SIGNUP_URL,
    REGISTER_ENDPOINT,
    TURNSTILE_SITEKEY,
    TYPEFORM_FORM_ID,
    TYPEFORM_ENDPOINT,
    PREMIUM_TRIAL_CLAIM_ENDPOINT,
    CREDENTIAL_FORMAT_PROTOCOL_URL,
    generate_register_password,
    password_meets_rules,
    parse_verification_code,
    save_account_credentials,
    save_proxy_lines,
    load_proxies_file_content,
    format_account_line,
    format_proxy_line,
    build_register_form_fields,
    build_typeform_complete_fields,
    build_proxy_download_params,
    build_proxy_display_params,
    normalize_proxy_countries_payload,
    needs_typeform_onboarding,
    extract_register_success,
    parse_proxy_download_text,
    is_protocol_url_proxy_line,
    pick_subaccount_id,
    proxy_list_download_url,
    collect_valid_proxy_lines,
)


class TestPasswordHelpers(unittest.TestCase):
    def test_random_password_meets_rules(self):
        for _ in range(20):
            pwd = generate_register_password("random")
            self.assertTrue(password_meets_rules(pwd), f"failed: {pwd}")
            self.assertGreaterEqual(len(pwd), 8)

    def test_configured_password_passthrough(self):
        pwd = generate_register_password("Dz1nn3f3@@@@")
        self.assertEqual(pwd, "Dz1nn3f3@@@@")
        self.assertTrue(password_meets_rules(pwd))

    def test_weak_password_rejected_by_rules(self):
        # Site rules (MCP discovery): min8, ≥1 upper, ≥1 digit, ≥1 special — no lower required
        self.assertFalse(password_meets_rules("short"))
        self.assertFalse(password_meets_rules("alllowercase1!"))  # no uppercase
        self.assertFalse(password_meets_rules("NoDigits!!!!"))
        self.assertFalse(password_meets_rules("NoSpecial1"))
        self.assertTrue(password_meets_rules("ALLUPPERCASE1!"))  # valid per site


class TestParseVerificationCode(unittest.TestCase):
    def test_proxyscrape_hex_code_phrase(self):
        # Live format: "Here is your email verification code: 94168d64e3"
        html = (
            '<p style="margin: 0;">Here is your email verification code: '
            "94168d64e3</p></div>"
        )
        self.assertEqual(parse_verification_code(html), "94168d64e3")

    def test_proxyscrape_quoted_printable(self):
        raw = (
            "Subject: ProxyScrape - Email Verification\r\n\r\n"
            "Here is your email verification code: ab12cd34ef\r\n"
        )
        self.assertEqual(parse_verification_code(raw), "ab12cd34ef")

    def test_does_not_grab_header_hex(self):
        # Must not return random Message-ID / bounce hex as the code
        raw = (
            "Message-ID: <082c5bc1deadbeef@proxyscrape.com>\r\n"
            "Subject: hello\r\n\r\n"
            "Here is your email verification code: deadbeef01\r\n"
        )
        self.assertEqual(parse_verification_code(raw), "deadbeef01")

    def test_empty(self):
        self.assertIsNone(parse_verification_code(""))
        self.assertIsNone(parse_verification_code(None))  # type: ignore


class TestPersistence(unittest.TestCase):
    def test_format_account_line_not_grok_sso(self):
        line = format_account_line("a@b.com", "Pass1!", "tok123")
        self.assertIn("a@b.com----Pass1!----tok123", line)
        # Must not look like bare SSO-only Grok lines
        self.assertNotEqual(line.strip(), "tok123")

    def test_save_account_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            paths = save_account_credentials(
                "user@example.com",
                "TestPass1!",
                "access_token_abc",
                keys_dir=td,
            )
            self.assertTrue(os.path.isfile(paths["accounts"]))
            self.assertNotIn("tokens", paths)
            with open(paths["accounts"], encoding="utf-8") as f:
                content = f.read()
            self.assertIn("user@example.com----TestPass1!----access_token_abc", content)
            # No separate tokens file
            self.assertFalse(
                os.path.isfile(os.path.join(td, "proxyscrape_tokens.txt"))
            )


class TestRegisterProtocolHelpers(unittest.TestCase):
    def test_signup_url_is_proxyscrape(self):
        self.assertIn("dashboard.proxyscrape.com", SIGNUP_URL)
        self.assertIn("sign-up", SIGNUP_URL)
        self.assertNotIn("accounts.x.ai", SIGNUP_URL)
        self.assertNotIn("x.ai", REGISTER_ENDPOINT)

    def test_turnstile_sitekey_discovered(self):
        self.assertTrue(TURNSTILE_SITEKEY.startswith("0x4"))

    def test_build_register_form_fields(self):
        fields = build_register_form_fields("e@x.com", "Pass1!", "cf_tok")
        self.assertEqual(
            fields,
            {
                "email": "e@x.com",
                "password": "Pass1!",
                "cf_turnstile_token": "cf_tok",
            },
        )

    def test_extract_register_success(self):
        body = {
            "access_token": "jwt_here",
            "userData": {
                "email": "e@x.com",
                "emailId": 12,
                "EmailVerified": False,
                "associatedSubaccounts": [],
            },
        }
        parsed = extract_register_success(body)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["access_token"], "jwt_here")
        self.assertEqual(parsed["email"], "e@x.com")
        self.assertFalse(parsed["email_verified"])

    def test_extract_register_failure(self):
        self.assertIsNone(
            extract_register_success(
                {
                    "success": False,
                    "message": "Unable to validate Cloudflare Turnstile token.",
                }
            )
        )

    def test_typeform_complete_fields(self):
        fields = build_typeform_complete_fields()
        self.assertEqual(fields["form_id"], TYPEFORM_FORM_ID)
        self.assertEqual(TYPEFORM_FORM_ID, "vnCgUn0n")
        self.assertTrue(fields["response_id"])
        self.assertIn("typeform", TYPEFORM_ENDPOINT)
        self.assertEqual(
            PREMIUM_TRIAL_CLAIM_ENDPOINT,
            "https://dashboard.proxyscrape.com/v2/v4/account/premium/claim-trial",
        )
        fixed = build_typeform_complete_fields(response_id="abc123")
        self.assertEqual(fixed["response_id"], "abc123")

    def test_needs_typeform_onboarding(self):
        self.assertTrue(needs_typeform_onboarding({"typeform": True}))
        self.assertFalse(needs_typeform_onboarding({"typeform": False}))
        self.assertFalse(needs_typeform_onboarding({}))


class TestProxyDownloadHelpers(unittest.TestCase):
    def test_credential_format_is_protocol_url(self):
        self.assertEqual(CREDENTIAL_FORMAT_PROTOCOL_URL, "3")
        params = build_proxy_download_params()
        self.assertEqual(params["type"], "getproxies")
        self.assertEqual(params["format"], "credentials")
        self.assertEqual(params["credential_format"], "3")
        self.assertEqual(params["protocol"], "http")

    def test_format_and_validate_line(self):
        line = format_proxy_line(
            "http", "s04xvtwkxqsv", "3f0gsnlwbbsonwm", "209.50.163.168", 3129
        )
        self.assertEqual(
            line,
            "http://s04xvtwkxqsv:3f0gsnlwbbsonwm@209.50.163.168:3129",
        )
        self.assertTrue(is_protocol_url_proxy_line(line))
        self.assertFalse(is_protocol_url_proxy_line("host:port"))
        self.assertFalse(is_protocol_url_proxy_line("user:pass@host:port"))

    def test_parse_proxy_download_text(self):
        body = (
            "http://u:p@1.2.3.4:3129\r\n"
            "\n"
            "http://u:p@5.6.7.8:3129\n"
            "<html>nope</html>\n"
        )
        lines = parse_proxy_download_text(body)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(is_protocol_url_proxy_line(x) for x in lines))

    def test_pick_subaccount_and_url(self):
        me = {
            "associatedSubaccounts": [
                {"AccountID": "74c2bfa2-a600-49e1-8d55-c42b6b878548"}
            ]
        }
        aid = pick_subaccount_id(me)
        self.assertEqual(aid, "74c2bfa2-a600-49e1-8d55-c42b6b878548")
        self.assertIn(
            "/datacenter_shared/proxy-list",
            proxy_list_download_url(aid),
        )

    def test_proxy_display_params_and_countries(self):
        params = build_proxy_display_params("HTTP")
        self.assertEqual(params["type"], "displayproxies")
        self.assertEqual(params["format"], "data")
        self.assertEqual(params["protocol"], "http")

        sample = {
            "countries": {
                "it": 3,
                "us": 52,
                "br": 10,
                "de": 11,
                "ca": 5,
                "gb": 8,
                "es": 4,
                "th": 4,
                "fr": 3,
            },
            "recordsTotal": 100,
        }
        meta = normalize_proxy_countries_payload(sample)
        self.assertEqual(meta["country_count"], 9)
        self.assertEqual(meta["records_total"], 100)
        self.assertEqual(meta["countries"]["us"], 52)
        self.assertEqual(meta["countries_display"], "9")
        # highest count first; display codes uppercase
        self.assertTrue(meta["countries_breakdown"].startswith("US:52"))
        self.assertIn("DE:11", meta["countries_breakdown"])
        self.assertNotIn("us:52", meta["countries_breakdown"])

        empty = normalize_proxy_countries_payload(None)
        self.assertEqual(empty["countries_display"], "—")
        self.assertIsNone(empty["country_count"])

    def test_save_proxy_lines(self):
        with tempfile.TemporaryDirectory() as td:
            paths = save_proxy_lines(
                ["http://u:p@1.2.3.4:1", "http://u:p@5.6.7.8:2"],
                keys_dir=td,
                account_id="abc-123",
                email="a@b.com",
            )
            self.assertEqual(paths["count"], 2)
            with open(paths["proxies"], encoding="utf-8") as f:
                content = f.read()
            self.assertIn("http://u:p@1.2.3.4:1", content)
            self.assertTrue(os.path.isfile(paths["account_proxies"]))

    def test_save_proxy_lines_appends_not_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            save_proxy_lines(["http://u:p@1.1.1.1:1"], keys_dir=td)
            save_proxy_lines(["http://u:p@2.2.2.2:2"], keys_dir=td)
            path = os.path.join(td, "proxies.txt")
            with open(path, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("http://u:p@1.1.1.1:1", body)
            self.assertIn("http://u:p@2.2.2.2:2", body)
            self.assertEqual(len([ln for ln in body.splitlines() if ln.strip()]), 2)

    def test_load_proxies_file_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "proxies.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# comment\nhttp://u:p@1.2.3.4:3129\nhttp://u:p@5.6.7.8:3129\n")
            content = load_proxies_file_content(path)
            self.assertNotIn("#", content)
            self.assertTrue(content.endswith("\n"))
            self.assertIn("http://u:p@1.2.3.4:3129", content)
            self.assertIn("http://u:p@5.6.7.8:3129", content)
            # missing file → empty string, no exception
            self.assertEqual(
                load_proxies_file_content(os.path.join(td, "nope.txt")), ""
            )

    def test_load_proxies_file_content_empty_or_comment_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "proxies.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# only a comment\n\n")
            self.assertEqual(load_proxies_file_content(path), "")


class TestCollectValidProxyLines(unittest.TestCase):
    """Feed source: local per-account files, expiry-gated, no network."""

    NOW = 1_700_000_000

    def _make_keys(self, td):
        """valid account file with a duplicate, a comment, and a socks5 line."""
        with open(os.path.join(td, "proxies_aid-valid.txt"), "w", encoding="utf-8") as f:
            f.write(
                "# email=v@x.com accountId=aid-valid\n"
                "http://u:p@1.1.1.1:1\n"
                "socks5://u:p@2.2.2.2:2\n"
                "http://u:p@1.1.1.1:1\n"
                "not-a-proxy-line\n"
                "\n"
            )
        with open(os.path.join(td, "proxies_aid-unknown.txt"), "w", encoding="utf-8") as f:
            f.write("https://u:p@3.3.3.3:3\n")
        # empty file → usable account but nothing to serve
        open(os.path.join(td, "proxies_aid-empty.txt"), "w", encoding="utf-8").close()

    def _snap(self, td):
        accounts = [
            {"email": "v@x.com"},       # valid + file
            {"email": "e@x.com"},       # expired + file
            {"email": "u@x.com"},       # unknown (no expires_at_unix) + file
            {"email": "m@x.com"},       # no cache entry at all
            {"email": "f@x.com"},       # valid but file missing
            {"email": "z@x.com"},       # valid but file empty
        ]
        cache = {
            "v@x.com": {
                "subaccount_id": "aid-valid",
                "details": {"ok": True, "expires_at_unix": self.NOW + 3600},
            },
            "e@x.com": {
                "subaccount_id": "aid-expired",
                "details": {"ok": True, "expires_at_unix": self.NOW - 10},
            },
            "u@x.com": {
                "subaccount_id": "aid-unknown",
                "details": {"ok": True},
            },
            "f@x.com": {
                "subaccount_id": "aid-missing",
                "details": {"ok": True, "expires_at_unix": self.NOW + 3600},
            },
            "z@x.com": {
                "subaccount_id": "aid-empty",
                "details": {"ok": True, "expires_at_unix": self.NOW + 3600},
            },
        }
        return collect_valid_proxy_lines(
            accounts=accounts, cache=cache, keys_dir=td, now_ts=self.NOW
        )

    def test_expired_excluded_unknown_included(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_keys(td)
            with open(
                os.path.join(td, "proxies_aid-expired.txt"), "w", encoding="utf-8"
            ) as f:
                f.write("http://u:p@9.9.9.9:9\n")
            snap = self._snap(td)

            self.assertNotIn("http://u:p@9.9.9.9:9", snap["lines"])
            self.assertIn("http://u:p@1.1.1.1:1", snap["lines"])
            # BOTH unknown flavours are served: no expires_at_unix, and no cache entry
            self.assertIn("https://u:p@3.3.3.3:3", snap["lines"])

            by_email = {r["email"]: r for r in snap["accounts"]}
            self.assertEqual(by_email["e@x.com"]["state"], "expired")
            self.assertEqual(by_email["e@x.com"]["reason"], "expired")
            self.assertEqual(by_email["u@x.com"]["state"], "unknown")
            self.assertEqual(by_email["m@x.com"]["state"], "unknown")
            self.assertEqual(by_email["v@x.com"]["state"], "valid")

    def test_counts(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_keys(td)
            snap = self._snap(td)
            # valid: v, f, z — expired: e — unknown: u, m
            self.assertEqual(snap["valid_count"], 3)
            self.assertEqual(snap["expired_count"], 1)
            self.assertEqual(snap["unknown_count"], 2)
            # served: v (file) + u (file); m has no cache → no account_id → no file
            self.assertEqual(snap["feed_eligible_count"], 2)
            # usable but unservable: f (missing file), z (empty file), m (no file)
            self.assertEqual(snap["skipped_count"], 3)

    def test_line_cleaning_and_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_keys(td)
            snap = self._snap(td)
            self.assertNotIn("socks5://u:p@2.2.2.2:2", snap["lines"])
            self.assertNotIn("not-a-proxy-line", snap["lines"])
            self.assertEqual(snap["lines"].count("http://u:p@1.1.1.1:1"), 1)
            by_email = {r["email"]: r for r in snap["accounts"]}
            self.assertEqual(by_email["v@x.com"]["line_count"], 1)
            self.assertEqual(by_email["v@x.com"]["proxy_file"], "proxies_aid-valid.txt")

    def test_never_reads_shared_proxies_txt(self):
        """proxies.txt is append-only and holds dead accounts — feed must ignore it."""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "proxies.txt"), "w", encoding="utf-8") as f:
                f.write("http://dead:p@6.6.6.6:6\n")
            snap = collect_valid_proxy_lines(
                accounts=[{"email": "n@x.com"}],
                cache={},
                keys_dir=td,
                now_ts=self.NOW,
            )
            self.assertEqual(snap["lines"], [])
            self.assertEqual(snap["feed_eligible_count"], 0)

    def test_account_id_prefers_subaccount_id(self):
        from src.proxyscrape_helpers import resolve_cached_account_id

        # subaccount_id (from /me) wins — it is what the filename was built from
        self.assertEqual(
            resolve_cached_account_id(
                {"subaccount_id": "from-me", "details": {"account_id": "from-overview"}}
            ),
            "from-me",
        )
        self.assertEqual(
            resolve_cached_account_id({"details": {"account_id": "from-overview"}}),
            "from-overview",
        )
        self.assertEqual(resolve_cached_account_id(None), "")
        self.assertEqual(resolve_cached_account_id({}), "")

    def test_expiry_state_recomputes_and_ignores_stale_cache(self):
        from src.proxyscrape_helpers import account_expiry_state

        # stale cached is_expired must not be trusted; expires_at_unix is authoritative
        stale_ok = {"details": {"expires_at_unix": self.NOW + 10, "is_expired": True}}
        self.assertEqual(account_expiry_state(stale_ok, now_ts=self.NOW), "valid")
        stale_dead = {"details": {"expires_at_unix": self.NOW - 10, "is_expired": False}}
        self.assertEqual(account_expiry_state(stale_dead, now_ts=self.NOW), "expired")
        # milliseconds are normalised
        self.assertEqual(
            account_expiry_state(
                {"details": {"expires_at_unix": (self.NOW + 10) * 1000}},
                now_ts=self.NOW,
            ),
            "valid",
        )
        self.assertEqual(account_expiry_state({}, now_ts=self.NOW), "unknown")
        self.assertEqual(
            account_expiry_state({"details": {"expires_at_unix": "junk"}}, now_ts=self.NOW),
            "unknown",
        )


class TestMainModuleNoGrok(unittest.TestCase):
    def test_main_source_is_proxyscrape(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_path = os.path.join(root, "main.py")
        with open(main_path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("ProxyScrape", src)
        self.assertIn("dashboard.proxyscrape.com", src)
        self.assertIn("/v2/v4/account/auth/register", src)
        self.assertIn("typeform", src)
        self.assertIn("complete_typeform_onboarding", src)
        self.assertIn("claim_premium_trial", src)
        self.assertIn("PREMIUM_TRIAL_CLAIM_ENDPOINT", src)
        self.assertIn("NO_PREMIUM_TRIAL", src)
        self.assertIn("download_premium_proxies", src)
        self.assertIn("LOGIN_ENDPOINT", src)
        self.assertIn("login_http", src)
        self.assertIn("re_login_account", src)
        self.assertIn("ensure_fresh_access_token", src)
        self.assertNotIn("upload_proxies_to_resin", src)
        self.assertNotIn("build_resin_subscription_payload", src)
        self.assertNotIn("RESIN_", src)
        self.assertIn("protocol://user:pass@host:port", src)
        self.assertNotIn("accounts.x.ai", src)
        self.assertNotIn("auth_mgmt.AuthManagement", src)
        self.assertNotIn("keys/grok.txt", src)
        self.assertNotIn("Grok 注册机", src)
        self.assertNotIn("redirect=grok-com", src)


if __name__ == "__main__":
    unittest.main()
