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
    CREDENTIAL_FORMAT_PROTOCOL_URL,
    generate_register_password,
    password_meets_rules,
    parse_verification_code,
    save_account_credentials,
    save_proxy_lines,
    load_proxies_file_content,
    build_resin_subscription_payload,
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

    def test_load_and_resin_payload(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "proxies.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# comment\nhttp://u:p@1.2.3.4:3129\nhttp://u:p@5.6.7.8:3129\n")
            content = load_proxies_file_content(path)
            self.assertNotIn("#", content)
            self.assertTrue(content.endswith("\n"))
            self.assertIn("http://u:p@1.2.3.4:3129", content)
            payload = build_resin_subscription_payload(content, name="test")
            self.assertEqual(payload["name"], "test")
            self.assertEqual(payload["update_interval"], "12h")
            self.assertTrue(payload["enabled"])
            self.assertFalse(payload["ephemeral"])
            self.assertEqual(payload["content"], content)


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
        self.assertIn("download_premium_proxies", src)
        self.assertIn("LOGIN_ENDPOINT", src)
        self.assertIn("login_http", src)
        self.assertIn("re_login_account", src)
        self.assertIn("ensure_fresh_access_token", src)
        self.assertIn("upload_proxies_to_resin", src)
        self.assertIn("protocol://user:pass@host:port", src)
        self.assertNotIn("accounts.x.ai", src)
        self.assertNotIn("auth_mgmt.AuthManagement", src)
        self.assertNotIn("keys/grok.txt", src)
        self.assertNotIn("Grok 注册机", src)
        self.assertNotIn("redirect=grok-com", src)


if __name__ == "__main__":
    unittest.main()
