"""
邮箱服务类（Cloudflare Worker 临时邮箱）
"""
import os
import requests
import random
import string
from dotenv import load_dotenv


class EmailService:
    """邮箱服务类"""

    # 域名后缀轮换计数器
    _domain_index = 0

    def __init__(self, timeout=30):
        """初始化邮箱服务"""
        load_dotenv()

        self.worker_domain = os.getenv("WORKER_DOMAIN")
        raw_domains = os.getenv("EMAIL_DOMAIN", "")
        self.email_domains = [
            d.strip()
            for d in raw_domains.replace(";", ",").replace(" ", ",").split(",")
            if d.strip()
        ]
        self.admin_password = os.getenv("ADMIN_PASSWORD")
        self.timeout = timeout

        if not all([self.worker_domain, self.email_domains, self.admin_password]):
            raise ValueError(
                "Missing required environment variables: WORKER_DOMAIN, EMAIL_DOMAIN, ADMIN_PASSWORD"
            )

    def _next_domain(self) -> str:
        """轮换选择下一个邮箱域名后缀"""
        domain = self.email_domains[
            EmailService._domain_index % len(self.email_domains)
        ]
        EmailService._domain_index += 1
        return domain

    def _generate_random_name(self):
        """生成随机邮箱名称"""
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
        创建临时邮箱（域名后缀在配置列表中轮换选择）
        """
        url = f"https://{self.worker_domain}/admin/new_address"
        domain = self._next_domain()
        try:
            random_name = self._generate_random_name()
            res = requests.post(
                url,
                json={
                    "enablePrefix": True,
                    "name": random_name,
                    "domain": domain,
                },
                headers={
                    "x-admin-auth": self.admin_password,
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("jwt"), data.get("address")
            else:
                print(
                    f"[-] 创建邮箱接口返回错误 ({domain}): {res.status_code} - {res.text}"
                )
                return None, None
        except Exception as e:
            print(f"[-] 创建邮箱网络异常 ({url}, domain={domain}): {e}")
            return None, None

    def fetch_first_email(self, jwt, max_retries=3):
        """
        获取邮件内容
        """
        last_error = None
        for _ in range(max_retries):
            try:
                limit = 10
                offset = 0
                res = requests.get(
                    f"https://{self.worker_domain}/api/mails",
                    params={
                        "limit": limit,
                        "offset": offset,
                    },
                    headers={
                        "Authorization": f"Bearer {jwt}",
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout,
                )

                if res.status_code == 200:
                    data = res.json()
                    if data["results"]:
                        raw_email_content = data["results"][0]["raw"]
                        return raw_email_content
                    return None
                else:
                    print(f"获取邮件失败: {res.text}")
                    return None
            except Exception as e:
                print(f"获取邮件失败: {e}")
                last_error = e

        raise last_error if last_error else RuntimeError("获取邮件失败: 未知错误")
