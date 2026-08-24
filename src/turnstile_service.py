"""
Turnstile 验证服务

可对接本机或远程 api_solver：
  默认 http://127.0.0.1:5072
  环境变量 TURNSTILE_SOLVER_URL=http://solver-host:5072
"""
import os
import time
import requests


class TurnstileService:
    """Turnstile 验证服务类"""

    def __init__(self, solver_url=None):
        """
        对接 Turnstile Solver（本机或远程 HTTP）。
        solver_url 优先；否则读 TURNSTILE_SOLVER_URL；再默认本机 5072。
        """
        url = (
            solver_url
            or os.getenv("TURNSTILE_SOLVER_URL", "").strip()
            or "http://127.0.0.1:5072"
        )
        self.solver_url = url.rstrip("/")

    def create_task(self, siteurl, sitekey):
        """
        创建 Turnstile 验证任务
        """
        url = f"{self.solver_url}/turnstile?url={siteurl}&sitekey={sitekey}"
        response = requests.get(url)
        response.raise_for_status()
        return response.json()["taskId"]

    def get_response(self, task_id, max_retries=30, initial_delay=5, retry_delay=2):
        """
        轮询本地 Solver 获取 Turnstile token
        """
        time.sleep(initial_delay)

        for _ in range(max_retries):
            try:
                url = f"{self.solver_url}/result?id={task_id}"
                response = requests.get(url)
                response.raise_for_status()
                data = response.json()
                captcha = data.get("solution", {}).get("token", None)

                if captcha:
                    if captcha != "CAPTCHA_FAIL":
                        return captcha
                    return None
                time.sleep(retry_delay)
            except Exception as e:
                print(f"获取 Turnstile 响应异常: {e}")
                time.sleep(retry_delay)

        return None
