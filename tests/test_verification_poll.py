"""
邮箱验证流程的单测。

覆盖三件事（对应真实日志暴露的问题）：
1. GPTMail 返回 `Unsupported email domain` → 立刻拉黑该根域并结束本轮，不再空转轮询；
2. 收件箱连续为空达到阈值 → 提前放弃本轮；
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
FAST = {"interval": 0.01, "window": 0.3, "resend_after": 0.05, "empty_abort": 3}


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
        long_cfg = {"interval": 0.01, "window": 30.0, "resend_after": 20.0, "empty_abort": 0}
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


class TestPollEmptyInboxAbort(unittest.TestCase):
    """连续空收件箱达阈值即放弃本轮，避免白等整个窗口。"""

    def _poll(self, config, fetch):
        with patch.object(reg, "fetch_inbox_content", side_effect=fetch):
            return reg.poll_verification_code(
                None, None, "a@x.com", "gptmail", config=config
            )

    def test_aborts_after_threshold(self):
        cfg = {"interval": 0.01, "window": 5.0, "resend_after": 4.0, "empty_abort": 3}
        calls = []

        def empty(*a, **k):
            calls.append(1)
            return None

        started = time.monotonic()
        out = self._poll(cfg, empty)
        self.assertEqual(out["reason"], "empty_inbox")
        self.assertEqual(len(calls), 3, "到达阈值应立即停止取信")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_zero_disables_early_abort(self):
        cfg = {"interval": 0.01, "window": 0.1, "resend_after": 5.0, "empty_abort": 0}
        out = self._poll(cfg, lambda *a, **k: None)
        self.assertEqual(out["reason"], "timeout")

    def test_single_mail_resets_the_streak(self):
        """收到过邮件（哪怕解不出码）就不算"连续为空"。"""
        # 窗口要留出首轮 1.0s 的初始等待，否则循环一次都没跑就超时
        cfg = {"interval": 0.01, "window": 3.0, "resend_after": 2.5, "empty_abort": 3}
        seq = [None, None, "some text without a code", None, None, None]

        def fetch(*a, **k):
            return seq.pop(0) if seq else None

        out = self._poll(cfg, fetch)
        self.assertEqual(out["reason"], "empty_inbox")


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
        """锁定出厂默认：空箱 10 次（约 20s）换号、窗口 180s、重发 120s。"""
        with patch.dict(os.environ, {}, clear=True):
            cfg = reg._verify_poll_config()
        self.assertEqual(cfg["empty_abort"], 10)
        self.assertEqual(cfg["window"], 180.0)
        self.assertEqual(cfg["resend_after"], 120.0)
        self.assertEqual(cfg["interval"], 2.0)
        self.assertLess(
            1.0 + (cfg["empty_abort"] - 1) * cfg["interval"],
            cfg["resend_after"],
            "空箱提前放弃必须早于重发时刻，否则等于没提前",
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
            {"interval": 0.01, "window": 2.0, "resend_after": 0.05, "empty_abort": 0}
        )
        self.assertEqual(out["reason"], "timeout")
        self.assertEqual(first_sends, 1, "开始时请求发信一次")
        self.assertEqual(len(resends), 1, "冷却满足后应重发一次")

    def test_no_resend_before_delay(self):
        out, first_sends, resends = self._run(
            {"interval": 0.01, "window": 2.0, "resend_after": 60.0, "empty_abort": 0}
        )
        self.assertEqual(out["reason"], "timeout")
        self.assertEqual(first_sends, 1)
        self.assertEqual(resends, [], "未到冷却时间不得重发（否则必然被站点 400 拒绝）")

    def test_resend_only_once(self):
        _out, _first, resends = self._run(
            {"interval": 0.01, "window": 2.0, "resend_after": 0.02, "empty_abort": 0}
        )
        self.assertEqual(len(resends), 1, "整轮只重发一次，避免撞 2 分钟限流")


class TestPollSuccessAndStop(unittest.TestCase):
    def test_returns_code_when_parsed(self):
        cfg = {"interval": 0.01, "window": 1.0, "resend_after": 0.5, "empty_abort": 0}
        mail = "Here is your email verification code: 94168d64e3"
        with patch.object(reg, "fetch_inbox_content", return_value=mail):
            out = reg.poll_verification_code(None, None, "a@x.com", "gptmail", config=cfg)
        self.assertEqual(out["code"], "94168d64e3")
        self.assertEqual(out["reason"], "ok")

    def test_stop_flag_ends_loop(self):
        cfg = {"interval": 0.01, "window": 30.0, "resend_after": 20.0, "empty_abort": 0}
        with patch.object(reg, "fetch_inbox_content", return_value=None), patch.object(
            reg, "stop_flag", True
        ):
            out = reg.poll_verification_code(None, None, "a@x.com", "gptmail", config=cfg)
        self.assertEqual(out["reason"], "stopped")


if __name__ == "__main__":
    unittest.main()
