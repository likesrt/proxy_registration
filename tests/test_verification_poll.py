"""
邮箱验证流程的单测。

覆盖三件事（对应真实日志暴露的问题）：
1. GPTMail 返回 `Unsupported email domain` → 立刻拉黑该根域并结束本轮，不再空转轮询；
2. 收件箱为空不构成放弃条件 → 必须等满窗口，因为邮件可能只是投递慢；
3. 重发验证码必须等到站点冷却（>=120s）之后，且窗口留出重发后的收信时间。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as reg
from src.email_blacklist import load_blocked_entries
from src.gptmail_service import GPTMailService, UnsupportedEmailDomain

# 测试用的极短参数：不等于生产默认值，只为让循环秒级跑完
FAST = {"interval": 0.01, "window": 0.3, "resend_after": 0.05}


class _Resp:
    """最小 HTTP 响应替身（requirements: status_code / text / json）。"""

    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else ("" if body is None else str(body))

    def json(self):
        return self._body


class TestUnsupportedDomainDetection(unittest.TestCase):
    """取件接口要把「域名不被支持」识别成硬失败，而不是普通错误。"""

    def _service(self):
        with patch.dict(os.environ, {"GPTMAIL_DOMAIN": "", "GPTMAIL_API_KEY": "k"}):
            return GPTMailService()

    def test_400_unsupported_domain_raises(self):
        body = {"success": False, "error": "Unsupported email domain"}
        with patch("src.gptmail_service.requests.get", return_value=_Resp(400, body)):
            with self.assertRaises(UnsupportedEmailDomain):
                self._service().fetch_first_email(None, email="a@charityrcao.org.uk")

    def test_case_and_spacing_insensitive(self):
        for text in (
            '{"error":"UNSUPPORTED EMAIL DOMAIN"}',
            '{"error":"unsupported  email  domain"}',
        ):
            with self.subTest(text=text), patch(
                "src.gptmail_service.requests.get",
                return_value=_Resp(400, None, text=text),
            ):
                with self.assertRaises(UnsupportedEmailDomain):
                    self._service().fetch_first_email(None, email="a@x.org.uk")

    def test_other_errors_stay_soft(self):
        """普通 400/500 不该被升级成硬失败（否则会误拉黑可用域名）。"""
        for status, text in ((400, '{"error":"invalid email"}'), (500, "boom")):
            with self.subTest(status=status), patch(
                "src.gptmail_service.requests.get",
                return_value=_Resp(status, None, text=text),
            ):
                self.assertIsNone(
                    self._service().fetch_first_email(None, email="a@good.com")
                )

    def test_empty_inbox_returns_none_not_raise(self):
        body = {"success": True, "data": {"emails": []}}
        with patch("src.gptmail_service.requests.get", return_value=_Resp(200, body)):
            self.assertIsNone(
                self._service().fetch_first_email(None, email="a@good.com")
            )


class TestNewestEmailWins(unittest.TestCase):
    """重发验证码后箱内会有新旧两封，必须取最新那封（接口顺序没有文档保证）。"""

    def _service(self):
        with patch.dict(os.environ, {"GPTMAIL_API_KEY": "sk-test"}):
            return GPTMailService()

    def _body(self, emails):
        return {"success": True, "data": {"emails": emails, "count": len(emails)}}

    def test_picks_newest_regardless_of_list_order(self):
        """接口把旧邮件排在前面时，也要取到新的那封（旧码已被站点作废）。"""
        old = {"id": "old", "content": "Here is your email verification code: aaaa1111bb", "timestamp": 100}
        new = {"id": "new", "content": "Here is your email verification code: bbbb2222aa", "timestamp": 200}
        with patch(
            "src.gptmail_service.requests.get",
            return_value=_Resp(200, self._body([old, new])),
        ):
            got = self._service().fetch_first_email(None, email="a@good.com")
        self.assertIn("bbbb2222aa", got, "必须取较新那封，而不是列表第一封")

    def test_skips_newest_without_content(self):
        """最新那封没正文时，退回更早那封有正文的邮件，而不是直接返回 None。"""
        newest = {"id": "n", "timestamp": 300}  # 无 content / html_content
        older = {"id": "o", "content": "Here is your email verification code: cccc3333cc", "timestamp": 200}
        with patch(
            "src.gptmail_service.requests.get",
            return_value=_Resp(200, self._body([newest, older])),
        ):
            got = self._service().fetch_first_email(None, email="a@good.com")
        self.assertIn("cccc3333cc", got)

    def test_missing_timestamp_does_not_crash(self):
        """缺 timestamp 的邮件按 0 处理，不能因排序抛异常。"""
        no_ts = {"id": "x", "content": "Here is your email verification code: dddd4444dd"}
        with patch(
            "src.gptmail_service.requests.get",
            return_value=_Resp(200, self._body([no_ts])),
        ):
            got = self._service().fetch_first_email(None, email="a@good.com")
        self.assertIn("dddd4444dd", got)

    def test_inbox_contents_returns_all_newest_first(self):
        """列表接口要把全部正文都返回，且最新在前（供换码重试使用）。"""
        old = {"id": "old", "content": "Here is your email verification code: aaaa1111bb", "timestamp": 100}
        new = {"id": "new", "content": "Here is your email verification code: bbbb2222aa", "timestamp": 200}
        with patch(
            "src.gptmail_service.requests.get",
            return_value=_Resp(200, self._body([old, new])),
        ):
            got = self._service().fetch_inbox_contents(email="a@good.com")
        self.assertEqual(len(got), 2)
        self.assertIn("bbbb2222aa", got[0], "最新在前")


class TestOtherVerificationCodes(unittest.TestCase):
    """验证失败时，应能改用箱内其他验证码（新旧码并存时的兜底）。"""

    class _Svc:
        """替身服务：返回固定的几封邮件正文，最新在前。"""

        def __init__(self, contents):
            self.contents = contents

        def fetch_inbox_contents(self, email=None):
            return list(self.contents)

    def test_excludes_tried_code_and_dedupes(self):
        """已试过的码要排除，重复的码只保留一次，且保持最新在前的顺序。"""
        svc = self._Svc(
            [
                "Here is your email verification code: bbbb2222aa",
                "Here is your email verification code: aaaa1111bb",
                "Here is your email verification code: aaaa1111bb",  # 重复
                "no code here",
            ]
        )
        got = reg._other_verification_codes(svc, None, "a@x.com", "gptmail", "bbbb2222aa")
        self.assertEqual(got, ["aaaa1111bb"], "排除已试过的码并去重")

    def test_returns_empty_when_no_other_code(self):
        """箱内只有已试过的那一个码时，没有可改试的候选。"""
        svc = self._Svc(["Here is your email verification code: bbbb2222aa"])
        self.assertEqual(
            reg._other_verification_codes(svc, None, "a@x.com", "gptmail", "bbbb2222aa"),
            [],
        )

    def test_non_gptmail_service_returns_empty(self):
        """Worker 版服务没有列表接口，兜底应安静返回空列表而不是报错。"""
        self.assertEqual(reg.fetch_inbox_contents(object(), None, "a@x.com", "cloudflare"), [])

    def test_swallows_errors(self):
        """兜底路径不能让异常冒泡——此时调用点已经拿到验证码，不该打断注册。"""
        class _Boom:
            def fetch_inbox_contents(self, email=None):
                raise RuntimeError("网络炸了")

        self.assertEqual(
            reg.fetch_inbox_contents(_Boom(), None, "a@x.com", "gptmail"), []
        )


class TestFetchInboxReraises(unittest.TestCase):
    """main.fetch_inbox_content 的兜底 except 不能吞掉域名级硬失败。"""

    class _Svc:
        def fetch_first_email(self, jwt, email=None):
            raise UnsupportedEmailDomain("HTTP 400 Unsupported email domain")

    def test_reraises_unsupported_domain(self):
        with self.assertRaises(UnsupportedEmailDomain):
            reg.fetch_inbox_content(self._Svc(), None, "a@x.com", "gptmail")

    def test_swallows_generic_errors(self):
        class _Boom:
            def fetch_first_email(self, jwt, email=None):
                raise RuntimeError("网络炸了")

        self.assertIsNone(reg.fetch_inbox_content(_Boom(), None, "a@x.com", "gptmail"))


class TestPollUnsupportedDomain(unittest.TestCase):
    """命中 Unsupported email domain：拉黑根域 + 立刻返回，不再耗完窗口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        with open(self.env, "w", encoding="utf-8") as f:
            f.write("EMAIL_BLACKLIST=\n")
        self.addCleanup(self.tmp.cleanup)

    def _poll(self, email, config=None):
        with patch("src.email_blacklist.DEFAULT_ENV_PATH", self.env), patch.dict(
            os.environ, {"EMAIL_BLACKLIST": "", "EMAIL_BLACKLIST_AUTO": "true"}
        ), patch.object(
            reg,
            "fetch_inbox_content",
            side_effect=UnsupportedEmailDomain("HTTP 400 Unsupported email domain"),
        ):
            return reg.poll_verification_code(
                None, None, email, "gptmail", config=dict(config or FAST)
            )

    def test_blacklists_root_domain_recursively(self):
        """brtfsbht.xjcnsdevg.de5.net 与 bwm.de5.net 同根域：一次拉黑救下两个。"""
        out = self._poll("a@brtfsbht.xjcnsdevg.de5.net")
        self.assertEqual(out["reason"], "unsupported_domain")
        self.assertTrue(out["domain_blocked"])
        self.assertEqual(load_blocked_entries(self.env), ["de5.net"])

    def test_returns_immediately_instead_of_burning_the_window(self):
        long_cfg = {"interval": 0.01, "window": 30.0, "resend_after": 20.0}
        started = time.monotonic()
        out = self._poll("a@charityrcao.org.uk", config=long_cfg)
        elapsed = time.monotonic() - started
        self.assertEqual(out["reason"], "unsupported_domain")
        self.assertLess(elapsed, 5.0, "命中域名不支持后应立即返回，而不是等满 30s 窗口")

    def test_public_suffix_is_refused_and_reason_still_reported(self):
        with patch("src.email_blacklist.psl_available", return_value=True):
            out = self._poll("a@eu.org")
        self.assertEqual(out["reason"], "unsupported_domain")
        self.assertFalse(out["domain_blocked"])  # 公共后缀被拒绝，不应写入
        self.assertEqual(load_blocked_entries(self.env), [])

    def test_toggle_off_keeps_blacklist_empty(self):
        with patch("src.email_blacklist.DEFAULT_ENV_PATH", self.env), patch.dict(
            os.environ, {"EMAIL_BLACKLIST": "", "EMAIL_BLACKLIST_AUTO": "false"}
        ), patch.object(
            reg, "fetch_inbox_content", side_effect=UnsupportedEmailDomain("x")
        ):
            out = reg.poll_verification_code(
                None, None, "a@de5.net", "gptmail", config=dict(FAST)
            )
        self.assertEqual(out["reason"], "unsupported_domain")
        self.assertFalse(out["domain_blocked"])
        self.assertEqual(load_blocked_entries(self.env), [])


class TestEmptyInboxNoEarlyAbort(unittest.TestCase):
    """空收件箱不再是放弃条件：邮件可能只是投递慢，必须等满窗口。

    背景（2026-10 线上日志）：旧实现按「连续空箱 N 次」提前放弃，在 76s 就结束了
    180s 的窗口，而该邮箱的验证邮件确实到达了——慢邮件被连同账号一起丢掉。
    """

    def _poll(self, config, fetch):
        """用给定的取信序列跑一轮轮询，返回 ``poll_verification_code`` 的结果。"""
        with patch.object(reg, "fetch_inbox_content", side_effect=fetch):
            return reg.poll_verification_code(
                None, None, "a@x.com", "gptmail", config=config
            )

    def test_keeps_polling_until_window_ends(self):
        """持续空箱也要取信到窗口耗尽，返回 timeout（不再是 empty_inbox）。"""
        # 窗口须大于首轮固定的 1.0s 初始等待，否则循环一次都没跑就超时
        cfg = {"interval": 0.01, "window": 3.0, "resend_after": 5.0}
        calls = []

        def empty(*a, **k):
            calls.append(1)
            return None

        out = self._poll(cfg, empty)
        self.assertEqual(out["reason"], "timeout")
        self.assertGreater(len(calls), 5, "不应在少量空箱后就停止取信")

    def test_late_mail_is_still_caught(self):
        """回归：连续空箱 20 次后才到达的邮件仍能被取到（旧逻辑会提前放弃）。"""
        cfg = {"interval": 0.01, "window": 3.0, "resend_after": 2.5}
        seq = [None] * 20 + ["Here is your email verification code: abc123def4"]

        def fetch(*a, **k):
            return seq.pop(0) if seq else None

        out = self._poll(cfg, fetch)
        self.assertEqual(out["reason"], "ok")
        self.assertEqual(out["code"], "abc123def4", "迟到的邮件必须还能被取到")


class TestPollResendTiming(unittest.TestCase):
    """重发必须等冷却满足；窗口要留出重发后收信的时间。"""

    RESEND_LABEL = "仍未收到验证码，再次请求发信"

    def test_config_enforces_site_cooldown_and_window(self):
        with patch.dict(
            os.environ,
            {"VERIFY_RESEND_AFTER": "10", "VERIFY_POLL_WINDOW": "60"},
        ):
            cfg = reg._verify_poll_config()
        self.assertEqual(cfg["resend_after"], 120.0, "低于站点冷却需抬到 120s")
        self.assertGreaterEqual(cfg["window"], cfg["resend_after"] + 30.0)

    def test_default_window_exceeds_cooldown(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = reg._verify_poll_config()
        self.assertGreaterEqual(cfg["window"], 150.0)

    def test_defaults_match_documented_values(self):
        """锁定出厂默认：窗口 240s（唯一的放弃条件）、重发 120s、间隔 2s。"""
        with patch.dict(os.environ, {}, clear=True):
            cfg = reg._verify_poll_config()
        self.assertEqual(cfg["window"], 240.0)
        self.assertEqual(cfg["resend_after"], 120.0)
        self.assertEqual(cfg["interval"], 2.0)
        self.assertNotIn("empty_abort", cfg, "空箱提前放弃已移除，配置只剩时间参数")

    def test_window_leaves_time_for_the_resent_mail(self):
        """窗口必须给重发那封留出投递时间，否则重发等于白做。

        实测邮件延迟超过 76s（2026-10 线上日志），所以重发后至少要能再等 120s。
        """
        with patch.dict(os.environ, {}, clear=True):
            cfg = reg._verify_poll_config()
        after_resend = cfg["window"] - cfg["resend_after"]
        self.assertGreaterEqual(
            after_resend, 120.0, "重发后应留出与实测延迟同量级的收信时间"
        )

    def _run(self, cfg):
        """
        跑一轮轮询，返回 (结果, 首次发信次数, 重发次数)。

        request_verification_mail 被整体替换，所以必须按 label 区分
        「开始时的首次发信」与「循环里的重发」，否则计数会混在一起。
        """
        calls = []
        with patch.object(reg, "fetch_inbox_content", return_value=None), patch.object(
            reg,
            "request_verification_mail",
            side_effect=lambda *a: calls.append(a) or True,
        ):
            out = reg.poll_verification_code(
                None,
                None,
                "a@x.com",
                "gptmail",
                session=object(),
                access_token="t",
                config=dict(cfg),
            )
        resends = [c for c in calls if c[3] == self.RESEND_LABEL]
        return out, len(calls) - len(resends), resends

    def test_resend_happens_after_delay(self):
        # 首轮 sleep 固定 1.0s，窗口须大于它循环才会继续
        out, first_sends, resends = self._run(
            {"interval": 0.01, "window": 2.0, "resend_after": 0.05}
        )
        self.assertEqual(out["reason"], "timeout")
        self.assertEqual(first_sends, 1, "开始时请求发信一次")
        self.assertEqual(len(resends), 1, "冷却满足后应重发一次")

    def test_no_resend_before_delay(self):
        out, first_sends, resends = self._run(
            {"interval": 0.01, "window": 2.0, "resend_after": 60.0}
        )
        self.assertEqual(out["reason"], "timeout")
        self.assertEqual(first_sends, 1)
        self.assertEqual(resends, [], "未到冷却时间不得重发（否则必然被站点 400 拒绝）")

    def test_resend_only_once(self):
        _out, _first, resends = self._run(
            {"interval": 0.01, "window": 2.0, "resend_after": 0.02}
        )
        self.assertEqual(len(resends), 1, "整轮只重发一次，避免撞 2 分钟限流")


class TestPollSuccessAndStop(unittest.TestCase):
    def test_returns_code_when_parsed(self):
        cfg = {"interval": 0.01, "window": 1.0, "resend_after": 0.5}
        mail = "Here is your email verification code: 94168d64e3"
        with patch.object(reg, "fetch_inbox_content", return_value=mail):
            out = reg.poll_verification_code(None, None, "a@x.com", "gptmail", config=cfg)
        self.assertEqual(out["code"], "94168d64e3")
        self.assertEqual(out["reason"], "ok")

    def test_stop_flag_ends_loop(self):
        cfg = {"interval": 0.01, "window": 30.0, "resend_after": 20.0}
        with patch.object(reg, "fetch_inbox_content", return_value=None), patch.object(
            reg, "stop_flag", True
        ):
            out = reg.poll_verification_code(None, None, "a@x.com", "gptmail", config=cfg)
        self.assertEqual(out["reason"], "stopped")


if __name__ == "__main__":
    unittest.main()
