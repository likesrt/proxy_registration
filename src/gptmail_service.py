"""
基于 mail.chatgpt.org.uk 的邮箱服务

已知可用域名时，本地拼接 前缀@域名 即可收信，无需调用 generate-email，节省 API 额度。
"""
import os
import random
import string
import requests
from dotenv import load_dotenv


class GPTMailService:
    """GPTMail 临时邮箱服务"""

    BASE_URL = "https://mail.chatgpt.org.uk"

    # 域名后缀轮换计数器
    _domain_index = 0

    # 最近一次 API 响应里的 usage
    _last_usage = None

    def __init__(self, timeout=10):
        """初始化邮箱服务"""
        load_dotenv()

        raw_domains = os.getenv("GPTMAIL_DOMAIN", "")
        self.email_domains = [
            d.strip()
            for d in raw_domains.replace(";", ",").replace(" ", ",").split(",")
            if d.strip()
        ]
        self.api_key = os.getenv("GPTMAIL_API_KEY", "gpt-test")
        self.timeout = timeout

    def _next_domain(self) -> str:
        """轮换选择下一个邮箱域名后缀"""
        domain = self.email_domains[
            GPTMailService._domain_index % len(self.email_domains)
        ]
        GPTMailService._domain_index += 1
        return domain

    def _update_usage(self, data: dict):
        """从接口 JSON 中缓存 usage（含 remaining_total 等）"""
        usage = data.get("usage") if isinstance(data, dict) else None
        if not usage:
            return
        GPTMailService._last_usage = usage

    def get_remaining_total(self):
        """获取最近一次已知的 API 总剩余额度，未知则返回 None"""
        usage = GPTMailService._last_usage
        if not usage:
            return None
        remaining = usage.get("remaining_total")
        return remaining if remaining is not None else None

    def format_quota_log(self) -> str:
        """用于注册日志的额度片段，未知时返回空字符串"""
        remaining = self.get_remaining_total()
        if remaining is None:
            return ""
        return f" | GPTMail总剩余: {remaining}"

    def _generate_random_name(self):
        """生成随机邮箱前缀"""
        letters1 = "".join(
            random.choices(string.ascii_lowercase, k=random.randint(4, 6))
        )
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(
            random.choices(string.ascii_lowercase, k=random.randint(0, 5))
        )
        return letters1 + numbers + letters2

    def create_email(self):
        """
        创建临时邮箱。

        - GPTMAIL_DOMAIN 已配置：本地拼接 前缀@域名（多域名轮换），不调 generate-email
        - GPTMAIL_DOMAIN 为空：调用 /api/generate-email 由服务端分配

        返回 (None, email) 以兼容原接口（jwt 置为 None）。
        """
        if self.email_domains:
            domain = self._next_domain()
            prefix = self._generate_random_name()
            email = f"{prefix}@{domain}"
            return None, email
        return self._generate_email_via_api()

    def _generate_email_via_api(self):
        """调用 generate-email 接口随机生成临时邮箱"""
        url = f"{self.BASE_URL}/api/generate-email"
        try:
            res = requests.get(
                url,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self.api_key,
                },
                timeout=self.timeout,
            )
            if res.status_code == 200:
                data = res.json()
                self._update_usage(data)
                if data.get("success") and data.get("data"):
                    email = data["data"].get("email")
                    return None, email
            print(f"[-] 创建邮箱失败: {res.status_code} - {res.text}")
            return None, None
        except Exception as e:
            print(f"[-] 创建邮箱网络异常 ({url}): {e}")
            return None, None

    def fetch_first_email(self, jwt_unused, email=None):
        """
        获取邮箱的第一封邮件内容
        """
        if not email:
            print("[-] fetch_first_email 需要 email 参数")
            return None
        url = f"{self.BASE_URL}/api/emails"
        try:
            res = requests.get(
                url,
                params={"email": email},
                headers={"X-API-Key": self.api_key},
                timeout=self.timeout,
            )
            if res.status_code == 200:
                data = res.json()
                self._update_usage(data)
                emails = data.get("data", {}).get("emails", [])
                if emails:
                    first = emails[0]
                    content = first.get("content") or first.get("html_content")
                    if content:
                        return content
                    email_id = first.get("id")
                    if email_id:
                        return self._fetch_email_detail(email_id)
                return None
            else:
                print(f"[-] 获取邮件失败: {res.status_code} - {res.text}")
                return None
        except Exception as e:
            print(f"获取邮件失败: {e}")
            return None

    def _fetch_email_detail(self, email_id):
        """读取单封邮件详情（含正文）"""
        url = f"{self.BASE_URL}/api/email/{email_id}"
        try:
            res = requests.get(
                url,
                headers={"X-API-Key": self.api_key},
                timeout=self.timeout,
            )
            if res.status_code == 200:
                data = res.json()
                self._update_usage(data)
                detail = data.get("data") or {}
                return (
                    detail.get("content")
                    or detail.get("text")
                    or detail.get("html_content")
                    or detail.get("html")
                )
            print(f"[-] 获取邮件详情失败: {res.status_code} - {res.text}")
            return None
        except Exception as e:
            print(f"获取邮件详情失败: {e}")
            return None
