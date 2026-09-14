"""Unit tests for account store parse + overview normalization (web management)."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.proxyscrape_helpers import (
    parse_account_line,
    parse_accounts_file_text,
    format_bytes_human,
    format_unix_expiry,
    format_countdown,
    normalize_overview_payload,
    apply_proxy_countries_to_details,
    merge_account_with_details,
    filter_proxy_lines_excluding,
    delete_account_local_data,
    delete_all_accounts_local_data,
    cache_entry_from_ui_row,
    apply_cached_details_to_row,
    save_account_details_cache,
    load_account_details_cache,
    upsert_account_details_cache,
    remove_account_details_cache,
    build_login_form_fields,
    decode_jwt_payload,
    access_token_exp_unix,
    is_access_token_expired,
    update_account_access_token,
)


# Representative overview shape from live ProxyScrape services/overview
SAMPLE_OVERVIEW = {
    "status": "valid",
    "account_type": "premium",
    "data": {
        "id": "74c2bfa2-a600-49e1-8d55-c42b6b878548",
        "bandwidth": 10_000_000_000,
        "bandwidth_used": 1_500_000_000,
        "is_trial": True,
        "proxy_credentials_enabled": True,
        "services": {
            "datacenter_shared": {
                "expiration_time": 1785048996,
                "proxy_amount": 100,
                "proxy_username": "s04xvtwkxqsv",
                "proxy_password": "secret",
            }
        },
    },
}


class TestParseAccountLine(unittest.TestCase):
    def test_basic_line(self):
        line = "user@example.com----Pass1!----tok_abc"
        acc = parse_account_line(line)
        self.assertIsNotNone(acc)
        self.assertEqual(acc["email"], "user@example.com")
        self.assertEqual(acc["password"], "Pass1!")
        self.assertEqual(acc["access_token"], "tok_abc")
        self.assertEqual(acc["flags"], [])

    def test_flag_on_token(self):
        line = "a@b.com----p----tok----UNVERIFIED"
        acc = parse_account_line(line)
        self.assertIn("UNVERIFIED", acc["flags"])
        self.assertEqual(acc["access_token"], "tok")

    def test_premium_trial_flag_on_token(self):
        line = "a@b.com----p----tok----NO_PREMIUM_TRIAL"
        acc = parse_account_line(line)
        self.assertIn("NO_PREMIUM_TRIAL", acc["flags"])
        self.assertEqual(acc["access_token"], "tok")

    def test_jwt_ending_with_hyphen_plus_unverified_roundtrip(self):
        """Markers must not strip('-') — base64url JWT may end with '-'."""
        # Synthetic JWT-shaped token ending with '-' (base64url)
        token = (
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjMifQ."
            "signature_padding_ends_with-"
        )
        self.assertTrue(token.endswith("-"))
        line = f"user@ex.com----Pass1!----{token}----UNVERIFIED"
        acc = parse_account_line(line)
        self.assertIsNotNone(acc)
        self.assertEqual(acc["access_token"], token)
        self.assertIn("UNVERIFIED", acc["flags"])
        # leading hyphen must also survive
        token2 = "-" + token
        line2 = f"user@ex.com----Pass1!----{token2}----NO_TYPEFORM"
        acc2 = parse_account_line(line2)
        self.assertEqual(acc2["access_token"], token2)
        self.assertIn("NO_TYPEFORM", acc2["flags"])

    def test_skip_comment_and_empty(self):
        self.assertIsNone(parse_account_line(""))
        self.assertIsNone(parse_account_line("# comment"))
        self.assertIsNone(parse_account_line("noconcat"))

    def test_file_dedupe_last_wins(self):
        text = "\n".join(
            [
                "a@x.com----p----t1",
                "b@x.com----p----t2",
                "a@x.com----p----t3",
            ]
        )
        rows = parse_accounts_file_text(text)
        self.assertEqual(len(rows), 2)
        a = next(r for r in rows if r["email"] == "a@x.com")
        self.assertEqual(a["access_token"], "t3")


class TestNormalizeOverview(unittest.TestCase):
    def test_bandwidth_remaining_from_sample(self):
        d = normalize_overview_payload(SAMPLE_OVERVIEW)
        self.assertTrue(d["ok"])
        self.assertEqual(d["bandwidth_total"], 10_000_000_000)
        self.assertEqual(d["bandwidth_used"], 1_500_000_000)
        # remaining = total - used — must come from the function, not a pre-baked string alone
        self.assertEqual(d["bandwidth_remaining"], 8_500_000_000)
        self.assertEqual(
            d["bandwidth_remaining"],
            d["bandwidth_total"] - d["bandwidth_used"],
        )
        self.assertIn("GB", d["bandwidth_remaining_display"])
        self.assertEqual(d["proxy_amount"], 100)
        self.assertEqual(d["proxy_username"], "s04xvtwkxqsv")
        self.assertEqual(d["account_id"], "74c2bfa2-a600-49e1-8d55-c42b6b878548")
        self.assertTrue(d["is_trial"])
        self.assertEqual(d["account_type"], "premium")
        # expiry from unix
        self.assertEqual(d["expires_at_unix"], 1785048996)
        self.assertIsNotNone(d["expires_at_iso"])
        self.assertNotEqual(d["expires_at_display"], "—")
        self.assertIn("proxy-list", d["proxy_list_page"] or "")

    def test_remaining_zero_when_over_quota(self):
        ov = {
            "data": {
                "bandwidth": 1000,
                "bandwidth_used": 5000,
                "services": {"datacenter_shared": {"expiration_time": 2000000000}},
            }
        }
        d = normalize_overview_payload(ov)
        self.assertEqual(d["bandwidth_remaining"], 0)

    def test_missing_overview(self):
        d = normalize_overview_payload(None)
        self.assertFalse(d["ok"])
        self.assertEqual(d["expires_at_display"], "—")
        self.assertEqual(d["bandwidth_remaining_display"], "—")

    def test_format_bytes_and_expiry_helpers(self):
        # Decimal (SI) units: ProxyScrape 10e9 quota must read as 10.00 GB, not 9.31 GiB
        self.assertEqual(format_bytes_human(1000), "1.00 KB")
        self.assertEqual(format_bytes_human(1024), "1.02 KB")
        self.assertEqual(format_bytes_human(10_000_000_000), "10.00 GB")
        # Small usage still visible when remaining is near full quota
        self.assertEqual(format_bytes_human(9_993_424_770), "9.99 GB")
        exp = format_unix_expiry(1785048996)
        dt = datetime.fromtimestamp(1785048996, tz=timezone.utc)
        self.assertEqual(exp["expires_at_iso"], dt.isoformat())
        # display is Beijing (UTC+8), not bare UTC label
        self.assertIn("北京时间", exp["expires_at_display"])
        self.assertNotIn("UTC", exp["expires_at_display"])
        self.assertIn("expires_countdown", exp)
        self.assertIn("expires_in_seconds", exp)
        # 1785048996 UTC → +8 hours Beijing
        from datetime import timedelta
        bj = dt.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(
            exp["expires_at_display"],
            bj.strftime("%Y-%m-%d %H:%M 北京时间"),
        )

    def test_countdown_format(self):
        self.assertEqual(format_countdown(0), "已过期")
        self.assertEqual(format_countdown(-10), "已过期")
        self.assertEqual(format_countdown(None), "—")
        # 1 day + 1h + 2m + 3s
        self.assertEqual(format_countdown(86400 + 3600 + 120 + 3), "1天 01:02:03")
        self.assertEqual(format_countdown(3661), "01:01:01")
        # fixed now: expiry in exactly 90 seconds
        now = 1_700_000_000.0
        exp = format_unix_expiry(now + 90, now_ts=now)
        self.assertEqual(exp["expires_in_seconds"], 90)
        self.assertEqual(exp["expires_countdown"], "00:01:30")
        self.assertFalse(exp["is_expired"])

    def test_bandwidth_remaining_includes_countdown_fields(self):
        d = normalize_overview_payload(SAMPLE_OVERVIEW)
        self.assertIsNotNone(d["expires_in_seconds"])
        self.assertTrue(isinstance(d["expires_countdown"], str))
        self.assertNotEqual(d["expires_countdown"], "")

    def test_apply_proxy_countries_to_details(self):
        d = normalize_overview_payload(SAMPLE_OVERVIEW)
        self.assertIsNone(d["country_count"])
        merged = apply_proxy_countries_to_details(
            d,
            {
                "countries": {"us": 52, "de": 11, "br": 10},
                "recordsTotal": 73,
            },
        )
        self.assertEqual(merged["country_count"], 3)
        self.assertEqual(merged["records_total"], 73)
        self.assertEqual(merged["countries"]["us"], 52)
        self.assertEqual(merged["countries_display"], "3")
        # overview fields preserved
        self.assertEqual(merged["proxy_amount"], 100)
        self.assertTrue(merged["ok"])


class TestMergeAccount(unittest.TestCase):
    def test_merge_with_overview(self):
        acc = parse_account_line("u@e.com----pw----token123")
        me = {
            "EmailVerified": True,
            "typeform": False,
            "associatedSubaccounts": [
                {"AccountID": "74c2bfa2-a600-49e1-8d55-c42b6b878548"}
            ],
        }
        row = merge_account_with_details(acc, overview=SAMPLE_OVERVIEW, me=me)
        self.assertEqual(row["email"], "u@e.com")
        self.assertTrue(row["has_token"])
        self.assertTrue(row["email_verified"])
        self.assertFalse(row["typeform_pending"])
        self.assertEqual(
            row["details"]["bandwidth_remaining"],
            8_500_000_000,
        )
        self.assertEqual(row["subaccount_id"], "74c2bfa2-a600-49e1-8d55-c42b6b878548")


class TestDeleteAccountData(unittest.TestCase):
    def test_filter_proxy_lines(self):
        content = "\n".join(
            [
                "http://keep:p@1.1.1.1:1",
                "http://dropme:p@2.2.2.2:2",
                "http://other:x@3.3.3.3:3",
                "",
            ]
        )
        new, n = filter_proxy_lines_excluding(
            content,
            drop_lines={"http://other:x@3.3.3.3:3"},
            drop_usernames={"dropme"},
        )
        self.assertEqual(n, 2)
        self.assertIn("keep", new)
        self.assertNotIn("dropme", new)
        self.assertNotIn("other:x", new)

    def test_delete_account_local_data(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            acc_path = os.path.join(td, "proxyscrape_accounts.txt")
            proxies_path = os.path.join(td, "proxies.txt")
            aid = "abc-123-def"
            per = os.path.join(td, "proxies_abc-123-def.txt")
            with open(acc_path, "w", encoding="utf-8") as f:
                f.write("keep@x.com----p----t1\n")
                f.write("del@x.com----p----t2\n")
            with open(per, "w", encoding="utf-8") as f:
                f.write("# email=del@x.com accountId=abc-123-def\n")
                f.write("http://u1:pw@9.9.9.9:3129\n")
            with open(proxies_path, "w", encoding="utf-8") as f:
                f.write("http://u1:pw@9.9.9.9:3129\n")
                f.write("http://keepu:p@1.1.1.1:1\n")
            result = delete_account_local_data(
                "del@x.com",
                keys_dir=td,
                accounts_file=acc_path,
                proxies_file=proxies_path,
                account_id=aid,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["removed"]["account_lines"], 1)
            self.assertFalse(os.path.isfile(per))
            with open(acc_path, encoding="utf-8") as f:
                left = f.read()
            self.assertIn("keep@x.com", left)
            self.assertNotIn("del@x.com", left)
            with open(proxies_path, encoding="utf-8") as f:
                px = f.read()
            self.assertIn("keepu", px)
            self.assertNotIn("u1:pw", px)

    def test_delete_all_accounts_local_data(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            acc_path = os.path.join(td, "proxyscrape_accounts.txt")
            proxies_path = os.path.join(td, "proxies.txt")
            per = os.path.join(td, "proxies_zzz.txt")
            with open(acc_path, "w", encoding="utf-8") as f:
                f.write("a@x.com----p----t1\nb@x.com----p----t2\n")
            with open(per, "w", encoding="utf-8") as f:
                f.write("http://u:p@1.1.1.1:1\n")
            with open(proxies_path, "w", encoding="utf-8") as f:
                f.write("http://u:p@1.1.1.1:1\n")
            result = delete_all_accounts_local_data(
                keys_dir=td,
                accounts_file=acc_path,
                proxies_file=proxies_path,
                clear_proxies_txt=True,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["removed"]["account_lines"], 2)
            self.assertIn("proxies_zzz.txt", result["removed"]["proxy_files"])
            self.assertFalse(os.path.isfile(per))
            with open(acc_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "")
            with open(proxies_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "")


class TestDetailsCache(unittest.TestCase):
    def test_cache_roundtrip_and_apply(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "account_details_cache.json")
            row = {
                "email": "u@e.com",
                "password": "secret",
                "has_token": True,
                "email_verified": True,
                "typeform_pending": False,
                "subaccount_id": "aid-1",
                "details": {
                    "ok": True,
                    "expires_at_unix": 1785048996,
                    "expires_countdown": "1天 00:00:00",
                    "bandwidth_remaining": 100,
                    "bandwidth_remaining_display": "100 B",
                    "proxy_amount": 50,
                },
            }
            upsert_account_details_cache(row, path=path)
            # password must not be stored
            raw = load_account_details_cache(path)
            self.assertIn("u@e.com", raw)
            self.assertNotIn("password", raw["u@e.com"])
            self.assertNotIn("access_token", raw["u@e.com"])

            bare = {
                "email": "u@e.com",
                "has_token": True,
                "details": {
                    "ok": False,
                    "error": "未刷新详情",
                    "expires_at_display": "—",
                    "bandwidth_remaining_display": "—",
                },
            }
            merged = apply_cached_details_to_row(bare, raw)
            self.assertTrue(merged["details"]["ok"])
            self.assertEqual(merged["details"]["proxy_amount"], 50)
            self.assertEqual(merged["subaccount_id"], "aid-1")
            self.assertTrue(merged["email_verified"])

            remove_account_details_cache("u@e.com", path=path)
            self.assertEqual(load_account_details_cache(path), {})

    def test_apply_cache_reformats_stale_bandwidth_display(self):
        """Old cache stored binary '9.31 GB'; reload must show SI from raw bytes."""
        bare = {
            "email": "old@e.com",
            "details": {"ok": False, "error": "未刷新详情", "bandwidth_remaining_display": "—"},
        }
        cache = {
            "old@e.com": {
                "details": {
                    "ok": True,
                    "bandwidth_total": 10_000_000_000,
                    "bandwidth_used": 6_575_230,
                    "bandwidth_remaining": 9_993_424_770,
                    "bandwidth_total_display": "9.31 GB",
                    "bandwidth_used_display": "6.27 MB",
                    "bandwidth_remaining_display": "9.31 GB",
                }
            }
        }
        merged = apply_cached_details_to_row(bare, cache)
        self.assertEqual(merged["details"]["bandwidth_total_display"], "10.00 GB")
        self.assertEqual(merged["details"]["bandwidth_remaining_display"], "9.99 GB")
        self.assertEqual(merged["details"]["bandwidth_used_display"], "6.58 MB")


class TestJwtAndLoginHelpers(unittest.TestCase):
    def _make_jwt(self, exp: int, iat: int = 1) -> str:
        import base64
        import json

        def b64(obj):
            raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        return f"{b64({'alg':'none'})}.{b64({'exp': exp, 'iat': iat})}.sig"

    def test_build_login_form_fields(self):
        f = build_login_form_fields("a@b.com", "Pass1!", "ts_tok")
        self.assertEqual(f["email"], "a@b.com")
        self.assertEqual(f["password"], "Pass1!")
        self.assertEqual(f["cf_turnstile_token"], "ts_tok")

    def test_jwt_expired_detection(self):
        now = 1_700_000_000
        fresh = self._make_jwt(now + 3600, now)
        dead = self._make_jwt(now - 10, now - 3600)
        self.assertFalse(is_access_token_expired(fresh, now=now))
        self.assertTrue(is_access_token_expired(dead, now=now))
        self.assertTrue(is_access_token_expired("", now=now))
        self.assertTrue(is_access_token_expired(None, now=now))
        self.assertEqual(access_token_exp_unix(fresh), now + 3600)
        self.assertIsNotNone(decode_jwt_payload(fresh))

    def test_merge_exposes_token_expired(self):
        dead2 = self._make_jwt(1000, 1)
        row2 = merge_account_with_details(
            {"email": "u@e.com", "password": "p", "access_token": dead2}
        )
        self.assertTrue(row2["token_expired"])
        self.assertTrue(row2["has_token"])
        self.assertEqual(row2["token_expires_at_unix"], 1000)

    def test_update_account_access_token_rewrites_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "accounts.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("a@x.com----p1----oldtok----UNVERIFIED\n")
                f.write("b@x.com----p2----tokb\n")
            ok = update_account_access_token("a@x.com", "newtok", accounts_file=path)
            self.assertTrue(ok)
            with open(path, encoding="utf-8") as f:
                accs = parse_accounts_file_text(f.read())
            by = {a["email"]: a for a in accs}
            self.assertEqual(by["a@x.com"]["access_token"], "newtok")
            self.assertIn("UNVERIFIED", by["a@x.com"]["flags"])
            self.assertEqual(by["b@x.com"]["access_token"], "tokb")
            self.assertFalse(
                update_account_access_token("missing@x.com", "x", accounts_file=path)
            )


class TestWebAppSource(unittest.TestCase):
    def test_web_app_exists_and_has_ui(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "web_app.py")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("ProxyScrape", src)
        self.assertIn("流量剩余", src)
        self.assertIn("剩余到期", src)
        self.assertIn("formatCountdownClient", src)
        self.assertIn("mergePreservedDetails", src)
        self.assertIn("/api/accounts", src)
        self.assertIn("/api/accounts/delete", src)
        self.assertIn("/api/accounts/delete-all", src)
        self.assertIn("/api/accounts/download-proxies", src)
        self.assertIn("/api/proxies/download-all", src)
        self.assertIn("/api/proxies/upload-resin", src)
        self.assertIn("btnUploadResin", src)
        self.assertIn("uploadAllToResin", src)
        self.assertIn("/api/register/logs", src)
        self.assertIn("注册日志", src)
        self.assertIn("downloadAccountProxies", src)
        self.assertIn("downloadAllProxies", src)
        self.assertIn("/api/register", src)
        self.assertIn("register_accounts", src)
        self.assertIn("INDEX_HTML", src)
        self.assertIn("LOGIN_HTML", src)
        self.assertIn("/api/auth/login", src)
        self.assertIn("btnLogout", src)
        self.assertIn("load_accounts_from_file", src)
        self.assertIn("deleteAccount", src)
        self.assertIn("deleteAllAccounts", src)
        self.assertIn("token过期", src)
        self.assertIn("ensure_fresh_access_token", src)
        self.assertIn("fetch_proxy_list_meta", src)
        self.assertIn(">地区<", src)
        self.assertIn("data-act=\"download\"", src)
        self.assertIn(">下载</button>", src)
        self.assertIn(">刷新</button>", src)
        self.assertNotIn("下载 proxy", src)
        self.assertNotIn("刷新详情", src)
        self.assertNotIn("账号详情", src)


if __name__ == "__main__":
    unittest.main()
