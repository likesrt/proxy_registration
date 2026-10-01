"""Unit tests for the email domain blacklist (auto-added when a trial is ineligible)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.email_blacklist import (
    AllDomainsBlacklisted,
    BlockedEmailRetriesExhausted,
    auto_block_enabled,
    block_domain,
    entries_text,
    format_entries,
    invalid_entries,
    is_blocked,
    is_public_suffix,
    load_blocked_entries,
    merge_entries_for_save,
    normalize_domain,
    parse_entries,
    psl_available,
    public_suffix,
    registrable_domain,
)
from src.email_service import EmailService
from src.gptmail_service import GPTMailService

PSL_SKIP = "tldextract 未安装（降级模式：原样记录完整域名）"


def _write_env(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestNormalizeDomain(unittest.TestCase):
    def test_email_address_is_reduced_to_domain(self):
        self.assertEqual(
            normalize_domain("lmorgan370@Ricardo0911.XYZ"), "ricardo0911.xyz"
        )
        self.assertEqual(normalize_domain("  alawson720@e.m.a.il.corbyrise.com. "),
                         "e.m.a.il.corbyrise.com")

    def test_idn_is_punycoded(self):
        self.assertEqual(normalize_domain("例子@例え.jp"), "xn--r8jz45g.jp")

    def test_invalid_input_returns_none(self):
        for bad in ("", "   ", "not a domain", "a..b.com", "localhost", "@", None):
            with self.subTest(bad=bad):
                self.assertIsNone(normalize_domain(bad))


@unittest.skipUnless(psl_available(), PSL_SKIP)
class TestRegistrableDomain(unittest.TestCase):
    def test_takes_registrable_root(self):
        self.assertEqual(registrable_domain("ricardo0911.xyz"), "ricardo0911.xyz")
        self.assertEqual(
            registrable_domain("e.m.a.il.corbyrise.com"), "corbyrise.com"
        )
        self.assertEqual(registrable_domain("a.b.co.uk"), "b.co.uk")

    def test_shared_suffixes_are_not_collapsed(self):
        """eu.org / github.io 是公共后缀，a.eu.org 的根就是 a.eu.org（否则误伤整个后缀）。"""
        self.assertEqual(registrable_domain("a.eu.org"), "a.eu.org")
        self.assertEqual(registrable_domain("a.github.io"), "a.github.io")

    def test_bare_public_suffix_has_no_registrable_domain(self):
        for suffix in ("eu.org", "co.uk", "github.io", "com"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(registrable_domain(suffix))

    def test_is_public_suffix(self):
        self.assertTrue(is_public_suffix("eu.org"))
        self.assertTrue(is_public_suffix("co.uk"))
        self.assertFalse(is_public_suffix("corbyrise.com"))
        self.assertEqual(public_suffix("a.b.co.uk"), "co.uk")


class TestParseEntries(unittest.TestCase):
    def test_splits_on_newlines_commas_semicolons_and_spaces(self):
        raw = "A.com\n b.com, c.com;d.com  e.com"
        self.assertEqual(
            parse_entries(raw), ["a.com", "b.com", "c.com", "d.com", "e.com"]
        )

    def test_dedupes_and_drops_invalid_tokens(self):
        raw = "a.com,A.COM,not a domain,,a.com"
        self.assertEqual(parse_entries(raw), ["a.com"])

    def test_accepts_full_email_and_reduces_it(self):
        self.assertEqual(parse_entries("x@a.com"), ["a.com"])

    def test_empty(self):
        self.assertEqual(parse_entries(""), [])
        self.assertEqual(parse_entries(None), [])

    def test_format_entries_is_env_safe_single_line(self):
        text = format_entries(["a.com", "b.com", "a.com", ""])
        self.assertEqual(text, "a.com,b.com")
        self.assertNotRegex(text, r"\s")


class TestBlockDomain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        _write_env(self.env, "# keep me\nWEB_PASSWORD=admin\nEMAIL_BLACKLIST=old.com\n")
        self.addCleanup(self.tmp.cleanup)

    def test_appends_root_domain_and_keeps_existing_content(self):
        result = block_domain("alawson720@e.m.a.il.corbyrise.com", path=self.env)
        self.assertTrue(result["ok"])
        self.assertTrue(result["added"])
        self.assertEqual(result["entry"], "corbyrise.com")
        text = open(self.env, encoding="utf-8").read()
        self.assertIn("# keep me", text)
        self.assertIn("WEB_PASSWORD=admin", text)
        self.assertEqual(
            load_blocked_entries(self.env), ["old.com", "corbyrise.com"]
        )

    def test_is_idempotent(self):
        block_domain("a@ricardo0911.xyz", path=self.env)
        second = block_domain("b@ricardo0911.xyz", path=self.env)
        self.assertFalse(second["added"])
        self.assertEqual(load_blocked_entries(self.env).count("ricardo0911.xyz"), 1)

    @unittest.skipUnless(psl_available(), PSL_SKIP)
    def test_refuses_public_suffix(self):
        for target in ("someone@eu.org", "someone@co.uk", "x@github.io"):
            with self.subTest(target=target):
                result = block_domain(target, path=self.env)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "public_suffix")
        self.assertEqual(load_blocked_entries(self.env), ["old.com"])

    def test_invalid_domain_is_rejected(self):
        result = block_domain("not an email", path=self.env)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_domain")


class TestIsBlocked(unittest.TestCase):
    ENTRIES = ["corbyrise.com", "ricardo0911.xyz"]

    def test_matches_exact_domain_and_subdomains(self):
        for target in (
            "corbyrise.com",
            "alawson720@e.m.a.il.corbyrise.com",
            "lmorgan370@ricardo0911.xyz",
        ):
            with self.subTest(target=target):
                self.assertTrue(is_blocked(target, entries=self.ENTRIES))

    def test_does_not_match_other_domains(self):
        for target in ("other.com", "x@notcorbyrise.com", "x@corbyrise.com.evil.net"):
            with self.subTest(target=target):
                self.assertFalse(is_blocked(target, entries=self.ENTRIES))

    @unittest.skipUnless(psl_available(), PSL_SKIP)
    def test_root_is_checked_even_when_entry_is_a_full_subdomain(self):
        """手填完整域名时，同一根域下的邮箱也应命中。"""
        entries = ["e.m.a.il.corbyrise.com"]
        self.assertTrue(is_blocked("x@other.corbyrise.com", entries=entries))

    def test_empty_list_never_blocks(self):
        self.assertFalse(is_blocked("a@corbyrise.com", entries=[]))


class TestConfigFileIntegration(unittest.TestCase):
    """黑名单读写走 .env：注册过程中自动加入的条目立刻生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        _write_env(self.env, "WEB_PASSWORD=admin\n")
        self.addCleanup(self.tmp.cleanup)

    def test_entries_are_read_from_the_env_file(self):
        with patch("src.email_blacklist.DEFAULT_ENV_PATH", self.env):
            block_domain("a@ricardo0911.xyz")
            self.assertEqual(load_blocked_entries(), ["ricardo0911.xyz"])
            self.assertTrue(is_blocked("b@sub.ricardo0911.xyz"))

    def test_env_var_fallback_only_when_file_lacks_the_key(self):
        with patch.dict(os.environ, {"EMAIL_BLACKLIST": "fallback.com"}):
            self.assertEqual(load_blocked_entries(self.env), ["fallback.com"])
        block_domain("a@file.com", path=self.env)
        with patch.dict(os.environ, {"EMAIL_BLACKLIST": "stale-env.com"}):
            # 文件里有了该键之后就不再读 os.environ（避免进程内旧值复活已删除的条目）
            self.assertEqual(load_blocked_entries(self.env), ["file.com"])


class TestMergeEntriesForSave(unittest.TestCase):
    """配置页保存：不覆盖页面加载后自动加入的条目，同时尊重用户的删除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        self.addCleanup(self.tmp.cleanup)

    def test_replace_when_disk_unchanged(self):
        _write_env(self.env, "EMAIL_BLACKLIST=a.com,b.com\n")
        merged = merge_entries_for_save("a.com", "a.com,b.com", path=self.env)
        self.assertEqual(merged, "a.com")  # 用户删掉 b.com 是生效的

    def test_keeps_entries_auto_added_after_page_load(self):
        _write_env(self.env, "EMAIL_BLACKLIST=a.com,b.com,auto.com\n")
        merged = merge_entries_for_save("a.com", "a.com,b.com", path=self.env)
        self.assertEqual(merged, "a.com,auto.com")

    def test_without_base_snapshot_trusts_submitted(self):
        _write_env(self.env, "EMAIL_BLACKLIST=a.com\n")
        self.assertEqual(
            merge_entries_for_save("manual.com", None, path=self.env), "manual.com"
        )


class TestAutoBlockToggle(unittest.TestCase):
    """开关与黑名单同策略：文件优先、os.environ 兜底。"""

    def setUp(self):
        # 用不存在的 .env 隔离仓库里的真实配置，避免本机 .env 影响断言
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        self.addCleanup(self.tmp.cleanup)
        self._patch = patch("src.email_blacklist.DEFAULT_ENV_PATH", self.env)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_default_is_on(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(auto_block_enabled())

    def test_can_be_disabled(self):
        for raw in ("false", "FALSE", "0", "no", "off", ""):
            with self.subTest(raw=raw), patch.dict(
                os.environ, {"EMAIL_BLACKLIST_AUTO": raw}
            ):
                self.assertFalse(auto_block_enabled())
        with patch.dict(os.environ, {"EMAIL_BLACKLIST_AUTO": "true"}):
            self.assertTrue(auto_block_enabled())

    def test_env_file_wins_over_stale_process_env(self):
        """手改 .env 后无需重启即生效：文件值压过启动时 load_dotenv 的旧值。"""
        _write_env(self.env, "EMAIL_BLACKLIST_AUTO=false\n")
        with patch.dict(os.environ, {"EMAIL_BLACKLIST_AUTO": "true"}):
            self.assertFalse(auto_block_enabled())


class TestBlacklistReadPolicy(unittest.TestCase):
    """.env 文件为准：自动加入的条目要立刻可见、可被 is_blocked 拦到。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        self.addCleanup(self.tmp.cleanup)
        _write_env(self.env, "EMAIL_BLACKLIST=\n")

    def test_entries_text_ignores_stale_process_env(self):
        """配置页展示必须与磁盘同源：否则自动加入的条目在页面上看不见（删不掉）。"""
        with patch.dict(os.environ, {"EMAIL_BLACKLIST": ""}):
            block_domain("a@ricardo0911.xyz", path=self.env)
            self.assertEqual(entries_text(self.env), "ricardo0911.xyz")

    def test_new_entry_is_visible_immediately_after_write(self):
        """读缓存按文件指纹失效，写入后下一次读取必须拿到新条目。"""
        self.assertEqual(load_blocked_entries(self.env), [])
        block_domain("a@ricardo0911.xyz", path=self.env)
        self.assertEqual(load_blocked_entries(self.env), ["ricardo0911.xyz"])
        self.assertTrue(is_blocked("b@sub.ricardo0911.xyz", path=self.env))

    def test_removing_entry_unblocks_on_next_read(self):
        """删行解封必须立刻生效（缓存不能把已删除的条目留在内存里）。"""
        block_domain("a@ricardo0911.xyz", path=self.env)
        self.assertTrue(is_blocked("b@ricardo0911.xyz", path=self.env))
        _write_env(self.env, "EMAIL_BLACKLIST=\n")
        self.assertFalse(is_blocked("b@ricardo0911.xyz", path=self.env))

    def test_invalid_entries_are_reported(self):
        """填了但解析不出来的条目要能被回显，而不是静默丢弃。"""
        raw = "good.com\n*.foo.com, xyz; https://e.com/path"
        self.assertEqual(
            invalid_entries(raw),
            ["*.foo.com", "xyz", "https://e.com/path"],
        )
        self.assertEqual(invalid_entries("good.com,  \n b.com"), [])
        self.assertEqual(invalid_entries(None), [])


class TestEmailServiceSkipsBlacklistedDomains(unittest.TestCase):
    ENV = {
        "WORKER_DOMAIN": "worker.test",
        "ADMIN_PASSWORD": "pw",
        "EMAIL_DOMAIN": "good.com,bad.com",
    }

    def setUp(self):
        EmailService._domain_index = 0

    def test_next_domain_skips_blocked(self):
        with patch.dict(os.environ, self.ENV), patch(
            "src.email_service.is_blocked", side_effect=lambda d: d == "bad.com"
        ):
            service = EmailService()
            self.assertEqual(service._next_domain(), "good.com")
            self.assertEqual(service._next_domain(), "good.com")

    def test_all_blocked_raises_instead_of_looping(self):
        with patch.dict(os.environ, self.ENV), patch(
            "src.email_service.is_blocked", return_value=True
        ):
            service = EmailService()
            self.assertIsNone(service._next_domain())
            with self.assertRaises(AllDomainsBlacklisted):
                service.create_email()

    def test_real_blacklist_file_is_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            env = os.path.join(td, ".env")
            _write_env(env, "EMAIL_BLACKLIST=bad.com\n")
            with patch.dict(os.environ, self.ENV, clear=False), patch(
                "src.email_blacklist.DEFAULT_ENV_PATH", env
            ):
                service = EmailService()
                self.assertEqual(service._next_domain(), "good.com")


class TestGPTMailServiceSkipsBlacklistedDomains(unittest.TestCase):
    ENV = {"GPTMAIL_DOMAIN": "good.com,bad.com", "GPTMAIL_API_KEY": "k"}

    def setUp(self):
        GPTMailService._domain_index = 0

    def test_next_domain_skips_blocked(self):
        with patch.dict(os.environ, self.ENV), patch(
            "src.gptmail_service.is_blocked", side_effect=lambda d: d == "bad.com"
        ):
            service = GPTMailService()
            self.assertEqual(service._next_domain(), "good.com")

    def test_all_blocked_raises(self):
        with patch.dict(os.environ, self.ENV), patch(
            "src.gptmail_service.is_blocked", return_value=True
        ):
            service = GPTMailService()
            with self.assertRaises(AllDomainsBlacklisted):
                service.create_email()

    def test_server_assigned_email_is_retried_when_blocked(self):
        with patch.dict(
            os.environ, {"GPTMAIL_DOMAIN": "", "GPTMAIL_API_KEY": "k"}
        ), patch(
            "src.gptmail_service.is_blocked",
            side_effect=lambda v: str(v).endswith("@bad.com"),
        ):
            service = GPTMailService()
            with patch.object(
                service,
                "_generate_email_once",
                side_effect=["p1@bad.com", "p2@bad.com", "p3@good.com"],
            ):
                self.assertEqual(service.create_email(), (None, "p3@good.com"))

    def test_server_assigned_email_all_blocked_raises(self):
        """随机域名池连续命中的异常类型必须能区分：换一轮可能抽到别的域名。"""
        with patch.dict(
            os.environ, {"GPTMAIL_DOMAIN": "", "GPTMAIL_API_KEY": "k"}
        ), patch("src.gptmail_service.is_blocked", return_value=True):
            service = GPTMailService()
            with patch.object(service, "_generate_email_once", return_value="p@bad.com"):
                with self.assertRaises(BlockedEmailRetriesExhausted):
                    service.create_email()


class TestAcquireEmailAction(unittest.TestCase):
    """黑名单失败要翻译成「重试 / 停止」：随机域名池不能被单轮命中判死。"""

    class _Service:
        """替身邮箱服务：按需抛异常或返回固定结果。"""

        def __init__(self, exc=None, result=(None, "a@good.com")):
            self.exc = exc
            self.result = result

        def create_email(self):
            if self.exc is not None:
                raise self.exc
            return self.result

    def _acquire(self, service, rounds=0):
        from main import acquire_email

        return acquire_email(service, rounds)

    def test_success_returns_ok_and_resets_counter(self):
        out = self._acquire(self._Service(), rounds=3)
        self.assertEqual(out["action"], "ok")
        self.assertEqual(out["email"], "a@good.com")
        self.assertEqual(out["blocked_rounds"], 0)

    def test_configured_list_all_blocked_stops_immediately(self):
        """配置里的域名列表是确定性的：全被拉黑就没有可用的了。"""
        out = self._acquire(self._Service(exc=AllDomainsBlacklisted("全封")))
        self.assertEqual(out["action"], "stop")

    def test_random_pool_retries_before_stopping(self):
        from main import MAX_BLOCKED_EMAIL_ROUNDS

        service = self._Service(exc=BlockedEmailRetriesExhausted("运气差"))
        rounds = 0
        for _ in range(MAX_BLOCKED_EMAIL_ROUNDS - 1):
            out = self._acquire(service, rounds)
            rounds = out["blocked_rounds"]
            self.assertEqual(out["action"], "retry")
        out = self._acquire(service, rounds)
        self.assertEqual(out["action"], "stop")
        self.assertEqual(out["blocked_rounds"], MAX_BLOCKED_EMAIL_ROUNDS)

    def test_transient_failures_retry(self):
        for service in (
            self._Service(exc=RuntimeError("网络炸了")),
            self._Service(result=(None, None)),
        ):
            with self.subTest(service=service.exc or "空邮箱"):
                out = self._acquire(service, rounds=2)
                self.assertEqual(out["action"], "retry")
                self.assertEqual(out["blocked_rounds"], 2)


class TestConfigRouteBlacklist(unittest.IsolatedAsyncioTestCase):
    """配置页与磁盘同源：自动加入的条目要看得见，保存时无效项要回显。"""

    def setUp(self):
        import web_app

        self.web_app = web_app
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        self.addCleanup(self.tmp.cleanup)
        self.client = web_app.app.test_client()

    def _ctx(self):
        return (
            patch.object(self.web_app, "_is_authed", return_value=True),
            patch.object(self.web_app, "DEFAULT_ENV_PATH", self.env),
        )

    async def test_get_returns_file_entries_despite_stale_process_env(self):
        """回归：页面曾按 os.environ 取值，导致自动拉黑的后缀在页面上看不见（删不掉）。"""
        _write_env(self.env, "WEB_PASSWORD=admin\nEMAIL_BLACKLIST=\n")
        block_domain("a@ricardo0911.xyz", path=self.env)
        auth, path = self._ctx()
        with auth, path, patch.dict(os.environ, {"EMAIL_BLACKLIST": ""}):
            r = await self.client.get("/api/config")
            data = await r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["values"]["EMAIL_BLACKLIST"], "ricardo0911.xyz")

    async def test_post_reports_invalid_entries_and_keeps_auto_added(self):
        _write_env(self.env, "WEB_PASSWORD=admin\nEMAIL_BLACKLIST=\n")
        block_domain("a@auto.com", path=self.env)  # 模拟页面打开后自动加入的条目
        auth, path = self._ctx()
        with auth, path, patch.object(
            self.web_app, "reload_main_module_config", return_value=[]
        ), patch.object(self.web_app, "apply_updates_to_environ"):
            r = await self.client.post(
                "/api/config",
                json={
                    "values": {"EMAIL_BLACKLIST": "manual.com, *.bad"},
                    "blacklist_base": "",
                },
            )
            data = await r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["invalid_blacklist"], ["*.bad"])
        # 手工条目 + 页面打开后自动加入的条目都在；无效项被丢弃且已回显
        self.assertEqual(load_blocked_entries(self.env), ["manual.com", "auto.com"])


class TestTrialIneligibleDetection(unittest.TestCase):
    def test_matches_the_site_message(self):
        from main import is_trial_ineligible

        for message in (
            "This email address is not eligible for the free trial.",
            "this email address is NOT ELIGIBLE FOR THE TRIAL",
            "Not eligible for free trial",
            "not  eligible  for  the  free  trial",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_trial_ineligible(message))

    def test_ignores_transient_and_vague_failures(self):
        from main import is_trial_ineligible

        for message in (
            "",
            None,
            "Not eligible",
            "Premium free trial activated.",
            "HTTP 500",
            "rate limit exceeded",
            "Invalid or expired access token",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_trial_ineligible(message))


class TestBlacklistTrialDomain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        _write_env(self.env, "WEB_PASSWORD=admin\n")
        self.addCleanup(self.tmp.cleanup)

    def _run(self, email, message, auto="true"):
        from main import blacklist_trial_domain

        with patch("src.email_blacklist.DEFAULT_ENV_PATH", self.env), patch.dict(
            os.environ, {"EMAIL_BLACKLIST": "", "EMAIL_BLACKLIST_AUTO": auto}
        ):
            return blacklist_trial_domain(email, message)

    def test_one_ineligible_result_burns_the_suffix(self):
        result = self._run(
            "lmorgan370@ricardo0911.xyz",
            "This email address is not eligible for the free trial.",
        )
        self.assertTrue(result["added"])
        self.assertEqual(result["entry"], "ricardo0911.xyz")
        self.assertEqual(load_blocked_entries(self.env), ["ricardo0911.xyz"])

    def test_other_failures_do_not_blacklist(self):
        result = self._run("a@corbyrise.com", "HTTP 500")
        self.assertFalse(result["added"])
        self.assertEqual(load_blocked_entries(self.env), [])

    def test_toggle_off_skips_auto_blacklist(self):
        result = self._run(
            "a@corbyrise.com",
            "This email address is not eligible for the free trial.",
            auto="false",
        )
        self.assertEqual(result["error"], "disabled")
        self.assertEqual(load_blocked_entries(self.env), [])

    def test_public_suffix_is_refused_end_to_end(self):
        if not psl_available():
            self.skipTest(PSL_SKIP)
        result = self._run(
            "a@eu.org", "This email address is not eligible for the free trial."
        )
        self.assertEqual(result["error"], "public_suffix")
        self.assertEqual(load_blocked_entries(self.env), [])


class TestWebConfigWiring(unittest.TestCase):
    def test_schema_exposes_blacklist_config_items(self):
        from src.env_config import CONFIG_SCHEMA, all_config_keys

        keys = set(all_config_keys())
        self.assertIn("EMAIL_BLACKLIST", keys)
        self.assertIn("EMAIL_BLACKLIST_AUTO", keys)

        item = next(
            i
            for g in CONFIG_SCHEMA
            for i in g["keys"]
            if i["key"] == "EMAIL_BLACKLIST"
        )
        self.assertEqual(item["type"], "textarea")

        auto = next(
            i
            for g in CONFIG_SCHEMA
            for i in g["keys"]
            if i["key"] == "EMAIL_BLACKLIST_AUTO"
        )
        self.assertEqual(auto["default"], "true")
        self.assertEqual(auto["options"], ["true", "false"])

    def test_config_page_sends_blacklist_base_for_safe_merge(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web_app.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("merge_entries_for_save", src)
        self.assertIn("blacklist_base", src)


class TestRegisterAccountsAutoBlacklist(unittest.TestCase):
    """跑一遍 register_accounts 主循环：免费试用不合格 → 域名立刻进黑名单。"""

    class _Session:
        def get(self, *args, **kwargs):
            return None

    def test_ineligible_trial_blacklists_domain_and_stops(self):
        import contextlib

        import main as reg

        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        env = os.path.join(tmp, ".env")
        _write_env(env, "WEB_PASSWORD=admin\nEMAIL_BLACKLIST=\n")

        GPTMailService._domain_index = 0  # 固定选到第一个域名
        saved = []

        def fake_save(email, password, access_token, extra=""):
            saved.append(extra)
            reg.stop_flag = True  # 失败分支会 continue，用它结束循环
            return {"accounts": "keys/proxyscrape_accounts.txt"}

        def fake_claim(session, access_token):
            return {
                "ok": False,
                "status": 400,
                "message": "This email address is not eligible for the free trial.",
                "data": {},
            }

        with patch.dict(
            os.environ,
            {
                "EMAIL_SERVICE_TYPE": "gptmail",
                "GPTMAIL_DOMAIN": "ricardo0911.xyz,good.com",
                "GPTMAIL_API_KEY": "k",
            },
            clear=False,
        ), patch("src.email_blacklist.DEFAULT_ENV_PATH", env), patch.object(
            reg, "create_session", lambda: contextlib.nullcontext(self._Session())
        ), patch.object(reg, "solve_turnstile", lambda *a, **k: "turnstile-token"), patch.object(
            reg,
            "register_http",
            lambda *a, **k: {
                "ok": True,
                "status": 200,
                "message": "",
                "data": {
                    "access_token": "access-token-0123456789",
                    "email_verified": True,
                    "user_data": {},
                },
            },
        ), patch.object(
            reg, "fetch_account_me", lambda *a, **k: {"EmailVerified": True, "typeform": False}
        ), patch.object(reg, "claim_premium_trial", fake_claim), patch.object(
            reg, "save_account_credentials", fake_save
        ), patch.object(
            reg, "stop_flag", False
        ), patch.object(
            reg, "success_count", 0
        ), patch.object(
            reg, "target_count", 1
        ):
            reg.register_accounts()

        self.assertEqual(saved, ["NO_PREMIUM_TRIAL"])
        self.assertEqual(load_blocked_entries(env), ["ricardo0911.xyz"])
        self.assertTrue(is_blocked("next@e.m.a.il.ricardo0911.xyz", path=env))


class TestAllDomainsBlacklistedStopsRegistration(unittest.TestCase):
    """所有域名都被拉黑时必须立刻停下——否则会 5s 一轮地空转。"""

    class _Session:
        def get(self, *args, **kwargs):
            return None

    def test_stops_instead_of_looping(self):
        import contextlib
        import threading

        import main as reg

        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        env = os.path.join(tmp, ".env")
        _write_env(env, "EMAIL_BLACKLIST=ricardo0911.xyz,good.com\n")

        done = threading.Event()

        def run():
            reg.register_accounts()
            done.set()

        with patch.dict(
            os.environ,
            {
                "EMAIL_SERVICE_TYPE": "gptmail",
                "GPTMAIL_DOMAIN": "ricardo0911.xyz,good.com",
                "GPTMAIL_API_KEY": "k",
            },
            clear=False,
        ), patch("src.email_blacklist.DEFAULT_ENV_PATH", env), patch.object(
            reg, "create_session", lambda: contextlib.nullcontext(self._Session())
        ), patch.object(
            reg, "stop_flag", False
        ), patch.object(
            reg, "target_count", 5
        ):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(
                done.wait(5), "全部域名被拉黑时 register_accounts 应立即返回"
            )


if __name__ == "__main__":
    unittest.main()
