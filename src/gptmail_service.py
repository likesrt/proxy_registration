"""
基于 mail.chatgpt.org.uk 的邮箱服务

已知可用域名时，本地拼接 前缀@域名 即可收信，无需调用 generate-email，节省 API 额度。
"""
import os
import random
import string
import threading
import time
from typing import Optional

import requests
from dotenv import load_dotenv

# 公开 key 接口：站点前端用「点击显示」按钮调用它拿到可用的 X-API-Key
DEFAULT_PUBLIC_KEY_URL = "https://mail.chatgpt.org.uk/api/public-key-status?reveal=1"
_PUBLIC_KEY_TTL = 3600
_public_key_lock = threading.Lock()
_public_key_cache: dict = {"key": None, "fetched_at": 0.0}


def fetch_public_api_key(
    url: Optional[str] = None,
    timeout: int = 10,
) -> Optional[str]:
    """GET 公开 key 接口并解析 data.key。任何失败都返回 None，绝不抛异常。"""
    target = (
        url
        or os.getenv("GPTMAIL_PUBLIC_KEY_URL")
        or DEFAULT_PUBLIC_KEY_URL
    )
    target = str(target).strip()
    if not target:
        return None
    try:
        res = requests.get(
            target,
            headers={
                "X-Public-Key-Reveal": "click",
                "Referer": "https://mail.chatgpt.org.uk/zh/api/",
                "Accept": "*/*",
            },
            timeout=timeout,
        )
        if res.status_code != 200:
            return None
        data = res.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    key = payload.get("key")
    if not key or not str(key).strip():
        return None
    return str(key).strip()


def get_public_api_key(force: bool = False) -> Optional[str]:
    """
    进程内缓存的公开 API key（TTL 1 小时），不回写 .env。

    成功才更新缓存；失败时回退到上一次已知的 key（可能为 None）。
    """
    now = time.time()
    with _public_key_lock:
        cached = _public_key_cache.get("key")
        fetched_at = float(_public_key_cache.get("fetched_at") or 0.0)
        if not force and cached and (now - fetched_at) < _PUBLIC_KEY_TTL:
            return cached

    key = fetch_public_api_key()
    with _public_key_lock:
        if key:
            _public_key_cache["key"] = key
            _public_key_cache["fetched_at"] = time.time()
        return _public_key_cache.get("key")


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
        # 优先 .env 的 GPTMAIL_API_KEY；留空则用公开 key 接口（进程内缓存）；
        # 都失败时沿用旧的 gpt-test 默认值，不中断注册。
        self.api_key = (
            (os.getenv("GPTMAIL_API_KEY") or "").strip()
            or get_public_api_key()
            or "gpt-test"
        )
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
