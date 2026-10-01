"""
ProxyScrape 注册机

协议发现（chrome-devtools-mcp）：
  页面 https://dashboard.proxyscrape.com/v2/sign-up
  POST /v2/v4/account/auth/register  (email, password, cf_turnstile_token)
  POST /v2/v4/account/verify-email   (verificationCode + Bearer access_token)
  Turnstile sitekey: 0x4AAAAAAAFWUVCKyusT9T8r
"""
import os
import sys
import time
import re
from curl_cffi import requests
from dotenv import load_dotenv

load_dotenv()

from src import (
    EmailService,
    GPTMailService,
    TurnstileService,
    AllDomainsBlacklisted,
    BlockedEmailRetriesExhausted,
    UnsupportedEmailDomain,
    auto_block_enabled,
    block_domain,
)
from src.proxyscrape_helpers import (
    SIGNUP_URL,
    LOGIN_URL,
    REGISTER_ENDPOINT,
    LOGIN_ENDPOINT,
    VERIFY_EMAIL_ENDPOINT,
    RESEND_CODE_ENDPOINT,
    TYPEFORM_ENDPOINT,
    PREMIUM_TRIAL_CLAIM_ENDPOINT,
    ME_ENDPOINT,
    TYPEFORM_FORM_ID,
    TURNSTILE_SITEKEY,
    DEFAULT_PROXY_PROTOCOL,
    CREDENTIAL_FORMAT_PROTOCOL_URL,
    generate_register_password,
    password_meets_rules,
    parse_verification_code,
    save_account_credentials,
    save_proxy_lines,
    build_register_form_fields,
    build_login_form_fields,
    build_typeform_complete_fields,
    build_proxy_download_params,
    build_proxy_display_params,
    needs_typeform_onboarding,
    extract_register_success,
    pick_subaccount_id,
    proxy_list_download_url,
    proxy_list_page_url,
    overview_url,
    parse_proxy_download_text,
    is_protocol_url_proxy_line,
    is_access_token_expired,
    update_account_access_token,
    normalize_overview_payload,
    upsert_account_details_cache,
)

REGISTER_PASSWORD = os.getenv("REGISTER_PASSWORD", "random").strip()
try:
    REGISTER_INTERVAL = float(os.getenv("REGISTER_INTERVAL", "0") or "0")
except ValueError:
    REGISTER_INTERVAL = 0.0

DEFAULT_IMPERSONATE = "chrome120"
# Protocol for premium list download: trial is HTTP-only; paid may support socks5
PROXY_DOWNLOAD_PROTOCOL = (
    os.getenv("PROXY_DOWNLOAD_PROTOCOL", DEFAULT_PROXY_PROTOCOL) or "http"
).strip().lower()

PROXIES = (
    {
        "http": os.getenv("PROXY"),
        "https": os.getenv("PROXY"),
    }
    if os.getenv("PROXY")
    else {}
)

last_register_time = 0.0
success_count = 0
start_time = time.time()
target_count = 100
stop_flag = False


def wait_register_interval():
    """按 .env 中 REGISTER_INTERVAL 控制两次注册之间的最小间隔"""
    global last_register_time
    if REGISTER_INTERVAL <= 0:
        return
    now = time.time()
    wait = REGISTER_INTERVAL - (now - last_register_time)
    if wait > 0:
        print(f"[*] 注册间隔等待 {wait:.1f}s（间隔 {REGISTER_INTERVAL}s）...")
        end = time.time() + wait
        while time.time() < end:
            if stop_flag:
                return
            time.sleep(min(0.5, end - time.time()))
    last_register_time = time.time()


def get_register_password() -> str:
    pwd = generate_register_password(REGISTER_PASSWORD)
    if not password_meets_rules(pwd):
        # Force a compliant random password if env value is invalid
        print(
            f"[!] REGISTER_PASSWORD 不符合 ProxyScrape 规则（需≥8位、大写、数字、特殊字符），改用随机密码"
        )
        pwd = generate_register_password("random")
    return pwd


def create_session():
    return requests.Session(impersonate=DEFAULT_IMPERSONATE, proxies=PROXIES)


def solve_turnstile(
    turnstile_service: TurnstileService,
    page_url: str | None = None,
) -> str | None:
    """Solve Cloudflare Turnstile for ProxyScrape (sign-up or login page)."""
    url = page_url or SIGNUP_URL
    try:
        task_id = turnstile_service.create_task(url, TURNSTILE_SITEKEY)
        token = turnstile_service.get_response(task_id)
        if not token or token == "CAPTCHA_FAIL":
            return None
        return token
    except Exception as e:
        print(f"[-] Turnstile 求解异常: {e}")
        return None


def login_http(session, email: str, password: str, turnstile_token: str) -> dict:
    """
    POST /v2/v4/account/auth/login with email + password + Turnstile.
    Returns {ok, data, status, message} — data matches extract_register_success.
    """
    fields = build_login_form_fields(email, password, turnstile_token)
    try:
        res = session.post(
            LOGIN_ENDPOINT,
            data=fields,
            headers={
                "origin": "https://dashboard.proxyscrape.com",
                "referer": LOGIN_URL,
                "accept": "application/json, text/plain, */*",
            },
            timeout=30,
        )
    except Exception as e:
        return {"ok": False, "data": None, "status": 0, "message": str(e)}

    try:
        body = res.json()
    except Exception:
        body = {"raw": res.text[:500]}

    if res.status_code == 200:
        parsed = extract_register_success(body)
        if parsed:
            return {
                "ok": True,
                "data": parsed,
                "status": res.status_code,
                "message": "ok",
            }

    message = ""
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or str(body)[:200]
    else:
        message = str(body)[:200]
    return {
        "ok": False,
        "data": body,
        "status": res.status_code,
        "message": message or "login failed",
    }


def re_login_account(
    session,
    email: str,
    password: str,
    turnstile_service: TurnstileService | None = None,
    persist: bool = True,
) -> dict:
    """
    Solve Turnstile + login; optionally write new access_token to accounts file.
    Returns {ok, access_token, message, status}.
    """
    email = (email or "").strip()
    password = (password or "").strip()
    if not email or not password:
        return {
            "ok": False,
            "access_token": "",
            "message": "缺少 email 或 password",
            "status": 0,
        }

    svc = turnstile_service or TurnstileService()
    ts_token = None
    for captcha_try in range(3):
        print(f"[*] {email} 登录 Turnstile ({captcha_try + 1}/3)...")
        ts_token = solve_turnstile(svc, LOGIN_URL)
        if ts_token:
            break
        time.sleep(1)
    if not ts_token:
        return {
            "ok": False,
            "access_token": "",
            "message": "Turnstile 求解失败（请确认 api_solver 已启动）",
            "status": 0,
        }

    try:
        session.get(LOGIN_URL, timeout=15)
    except Exception:
        pass

    result = login_http(session, email, password, ts_token)
    if not result.get("ok"):
        return {
            "ok": False,
            "access_token": "",
            "message": result.get("message") or "登录失败",
            "status": result.get("status") or 0,
        }

    access_token = (result.get("data") or {}).get("access_token") or ""
    if not access_token:
        return {
            "ok": False,
            "access_token": "",
            "message": "登录响应无 access_token",
            "status": result.get("status") or 200,
        }

    if persist:
        try:
            update_account_access_token(email, access_token)
            print(f"[+] {email} token 已续期并写入 accounts 文件")
        except Exception as e:
            print(f"[!] {email} token 写入失败: {e}")

    return {
        "ok": True,
        "access_token": access_token,
        "message": "ok",
        "status": result.get("status") or 200,
        "data": result.get("data"),
    }


def ensure_fresh_access_token(
    session,
    acc: dict,
    turnstile_service: TurnstileService | None = None,
    force: bool = False,
) -> dict:
    """
    Return a usable access_token for the account row.
    Re-logins when force=True, token missing/expired, or JWT past exp.
    """
    email = (acc.get("email") or "").strip()
    password = (acc.get("password") or "").strip()
    token = (acc.get("access_token") or "").strip()
    if not force and token and not is_access_token_expired(token):
        return {
            "ok": True,
            "access_token": token,
            "refreshed": False,
            "message": "ok",
        }
    if not password:
        return {
            "ok": False,
            "access_token": token,
            "refreshed": False,
            "message": "token 过期且无 password，无法重登",
        }
    lr = re_login_account(
        session, email, password, turnstile_service=turnstile_service, persist=True
    )
    if not lr.get("ok"):
        return {
            "ok": False,
            "access_token": token,
            "refreshed": False,
            "message": lr.get("message") or "重登失败",
        }
    return {
        "ok": True,
        "access_token": lr["access_token"],
        "refreshed": True,
        "message": "ok",
    }


def register_http(session, email: str, password: str, turnstile_token: str) -> dict:
    """
    POST ProxyScrape register endpoint with discovered form fields.
    Returns {ok, data, status, message}.
    """
    fields = build_register_form_fields(email, password, turnstile_token)
    try:
        # Browser uses FormData; multipart is accepted (MCP probe confirmed endpoint)
        res = session.post(
            REGISTER_ENDPOINT,
            data=fields,
            headers={
                "origin": "https://dashboard.proxyscrape.com",
                "referer": SIGNUP_URL,
                "accept": "application/json, text/plain, */*",
            },
            timeout=30,
        )
    except Exception as e:
        return {"ok": False, "data": None, "status": 0, "message": str(e)}

    try:
        body = res.json()
    except Exception:
        body = {"raw": res.text[:500]}

    if res.status_code == 200:
        parsed = extract_register_success(body)
        if parsed:
            return {
                "ok": True,
                "data": parsed,
                "status": res.status_code,
                "message": "ok",
            }

    message = ""
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or str(body)[:200]
    else:
        message = str(body)[:200]
    return {
        "ok": False,
        "data": body,
        "status": res.status_code,
        "message": message,
    }


def _auth_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "origin": "https://dashboard.proxyscrape.com",
        "referer": "https://dashboard.proxyscrape.com/v2/verify",
        "accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def verify_email_http(session, access_token: str, code: str) -> dict:
    """POST /v2/v4/account/verify-email with Bearer token."""
    try:
        res = session.post(
            VERIFY_EMAIL_ENDPOINT,
            data={"verificationCode": code},
            headers=_auth_headers(access_token),
            timeout=30,
        )
    except Exception as e:
        return {"ok": False, "status": 0, "message": str(e)}

    try:
        body = res.json()
    except Exception:
        body = {"raw": res.text[:500]}

    ok = res.status_code == 200 and (
        body.get("success") is not False if isinstance(body, dict) else True
    )
    message = ""
    if isinstance(body, dict):
        message = body.get("message") or ("ok" if ok else str(body)[:200])
    return {"ok": ok, "status": res.status_code, "message": message, "data": body}


def resend_verification_code(session, access_token: str) -> dict:
    """
    POST /v2/v4/account/reset-verification-code

    Critical: register API does NOT always auto-send the verification email.
    Frontend verify page relies on this (or an initial server-side send).
    """
    try:
        res = session.post(
            RESEND_CODE_ENDPOINT,
            data={},
            headers=_auth_headers(access_token),
            timeout=30,
        )
    except Exception as e:
        return {"ok": False, "status": 0, "message": str(e)}

    try:
        body = res.json()
    except Exception:
        # Cloudflare 429 HTML etc.
        return {
            "ok": False,
            "status": res.status_code,
            "message": res.text[:200],
            "data": None,
        }

    ok = res.status_code == 200 and (
        body.get("success") is not False if isinstance(body, dict) else True
    )
    message = ""
    if isinstance(body, dict):
        message = body.get("message") or ("ok" if ok else str(body)[:200])
    # Rate-limit message is still "sent recently" — treat as soft-ok for polling
    if (
        isinstance(body, dict)
        and "once every 2 minutes" in str(body.get("message", "")).lower()
    ):
        ok = True
    return {"ok": ok, "status": res.status_code, "message": message, "data": body}


def fetch_account_me(session, access_token: str) -> dict | None:
    """POST /v2/v4/account/auth/me — EmailVerified + typeform onboarding flag."""
    try:
        res = session.post(
            ME_ENDPOINT,
            data={},
            headers=_auth_headers(access_token),
            timeout=20,
        )
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[-] /me 查询异常: {e}")
    return None


def download_premium_proxies(
    session,
    access_token: str,
    account_id: str,
    protocol: str | None = None,
) -> dict:
    """
    Download premium proxy list as protocol://user:pass@host:port lines.

    Dashboard page: /v2/services/premium/proxy-list/{accountId}
    API (MCP + live probe):
      GET /v2/v4/account/{accountId}/datacenter_shared/proxy-list
        ?type=getproxies&protocol=http&format=credentials&credential_format=3
    """
    proto = (protocol or PROXY_DOWNLOAD_PROTOCOL or "http").lower()
    params = build_proxy_download_params(
        protocol=proto,
        credential_format=CREDENTIAL_FORMAT_PROTOCOL_URL,
    )
    headers = _auth_headers(access_token)
    headers["referer"] = proxy_list_page_url(account_id)
    headers["accept"] = "*/*"
    url = proxy_list_download_url(account_id)
    try:
        res = session.get(url, params=params, headers=headers, timeout=60)
    except Exception as e:
        return {"ok": False, "status": 0, "message": str(e), "lines": []}

    if res.status_code != 200:
        msg = res.text[:300] if res.text else f"HTTP {res.status_code}"
        return {
            "ok": False,
            "status": res.status_code,
            "message": msg,
            "lines": [],
        }

    # curl_cffi Response: .text or .content
    try:
        body = res.text if isinstance(res.text, str) else res.content.decode(
            "utf-8", errors="replace"
        )
    except Exception:
        body = res.content.decode("utf-8", errors="replace")

    lines = parse_proxy_download_text(body)
    if not lines:
        return {
            "ok": False,
            "status": res.status_code,
            "message": f"empty proxy list body: {body[:200]!r}",
            "lines": [],
        }

    valid = [ln for ln in lines if is_protocol_url_proxy_line(ln)]
    if not valid:
        # still accept lines if format slightly differs
        valid = lines

    return {
        "ok": True,
        "status": res.status_code,
        "message": f"{len(valid)} proxies",
        "lines": valid,
        "sample": valid[0] if valid else "",
    }


def fetch_overview(session, access_token: str, account_id: str) -> dict | None:
    """GET services/overview — includes proxy_username / proxy_password."""
    try:
        res = session.get(
            overview_url(account_id),
            headers={
                **_auth_headers(access_token),
                "referer": proxy_list_page_url(account_id),
                "accept": "application/json",
            },
            timeout=30,
        )
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[-] overview 查询异常: {e}")
    return None


def fetch_proxy_list_meta(
    session,
    access_token: str,
    account_id: str,
    protocol: str | None = None,
) -> dict | None:
    """
    GET datacenter_shared/proxy-list?format=data&type=displayproxies&protocol=http

    Dashboard country breakdown, e.g.:
      {"countries":{"us":52,"de":11,...},"recordsTotal":100}
    """
    proto = (protocol or PROXY_DOWNLOAD_PROTOCOL or "http").lower()
    params = build_proxy_display_params(protocol=proto)
    try:
        res = session.get(
            proxy_list_download_url(account_id),
            params=params,
            headers={
                **_auth_headers(access_token),
                "referer": proxy_list_page_url(account_id),
                "accept": "application/json",
            },
            timeout=30,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[-] proxy-list meta 查询异常: {e}")
    return None


def complete_typeform_onboarding(session, access_token: str) -> dict:
    """
    Complete post-verify Typeform questionnaire gate.

    Live flow (chrome-devtools, account qmubgz71q@yjb.me):
      /me typeform=true → redirect /v2/typeform (embed form vnCgUn0n)
      Q1: Individual / Company
      Q2: What do you use proxies for?
      onSubmit → POST /v2/v4/account/typeform {form_id, response_id}
      typeform=false → real dashboard (e.g. /services/premium/overview/...)

    API accepts a synthetic response_id without filling the embed UI.
    """
    fields = build_typeform_complete_fields()
    headers = _auth_headers(access_token)
    headers["referer"] = "https://dashboard.proxyscrape.com/v2/typeform"
    try:
        res = session.post(
            TYPEFORM_ENDPOINT,
            data=fields,
            headers=headers,
            timeout=30,
        )
    except Exception as e:
        return {"ok": False, "status": 0, "message": str(e), "fields": fields}

    try:
        body = res.json()
    except Exception:
        body = {"raw": res.text[:500]}

    ok = res.status_code == 200 and (
        body.get("success") is not False if isinstance(body, dict) else True
    )
    # Idempotent: already submitted is success for our purposes
    if (
        isinstance(body, dict)
        and "already submitted" in str(body.get("error") or body.get("message") or "").lower()
    ):
        ok = True
    message = ""
    if isinstance(body, dict):
        message = (
            body.get("message")
            or body.get("error")
            or ("ok" if ok else str(body)[:200])
        )
    return {
        "ok": ok,
        "status": res.status_code,
        "message": message,
        "data": body,
        "fields": fields,
    }


def claim_premium_trial(session, access_token: str) -> dict:
    """Activate the eligible account's Premium free trial after onboarding."""
    headers = _auth_headers(access_token)
    headers.pop("Content-Type", None)
    headers["referer"] = "https://dashboard.proxyscrape.com/v2/overview"
    try:
        res = session.post(
            PREMIUM_TRIAL_CLAIM_ENDPOINT,
            headers=headers,
            timeout=30,
        )
    except Exception as e:
        return {"ok": False, "status": 0, "message": str(e), "data": None}

    try:
        body = res.json()
    except Exception:
        body = {"raw": res.text[:500]}

    ok = (
        200 <= res.status_code < 300
        and isinstance(body, dict)
        and body.get("success") is True
    )
    message = ""
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or (
            "ok" if ok else str(body)[:200]
        )
    return {
        "ok": ok,
        "status": res.status_code,
        "message": message or f"HTTP {res.status_code}",
        "data": body,
    }


# 站点只在邮箱域名不可用时返回这句；这是唯一可靠的「该后缀已废」信号
TRIAL_INELIGIBLE_RE = re.compile(
    r"not\s+eligible\s+for\s+(?:the\s+)?(?:free\s+)?trial", re.IGNORECASE
)


def is_trial_ineligible(message) -> bool:
    """免费试用失败信息是否表示该邮箱地址不具备试用资格。"""
    return bool(TRIAL_INELIGIBLE_RE.search(str(message or "")))


def auto_blacklist_domain(email: str) -> dict:
    """
    按配置把该邮箱的域名加入黑名单（自动拉黑的公共入口，一次即封）。

    参数:
        email: 邮箱地址；内部取可注册根域（eTLD+1）后写入 ``EMAIL_BLACKLIST``。

    返回:
        ``block_domain`` 的结果 ``{"ok", "added", "entry", "error"}``，
        以及自动拉黑被关闭时的 ``error="disabled"``。

    边界条件:
        - ``EMAIL_BLACKLIST_AUTO=false`` 时只打印日志、不写名单；
        - 公共后缀（eu.org 等）由 ``block_domain`` 拒绝，日志说明原因；
        - 已在名单中时不重复写入（幂等），仅提示。

    副作用: 写 ``.env``；打印一行状态日志。
    """
    if not auto_block_enabled():
        print("[*] 邮箱黑名单自动加入已关闭（EMAIL_BLACKLIST_AUTO=false），跳过")
        return {"ok": False, "added": False, "entry": "", "error": "disabled"}

    result = block_domain(email)
    if result.get("added"):
        print(
            f"[!] {email} 域名已加入黑名单: {result['entry']}"
            "（该后缀不再使用，可在 Web 配置页移除）"
        )
    elif result.get("ok"):
        print(f"[*] {email} 域名 {result['entry']} 已在黑名单中")
    elif result.get("error") == "public_suffix":
        print(
            f"[!] {email} 域名为公共后缀 {result.get('entry')}，"
            "拒绝加入黑名单（避免误伤同后缀用户）"
        )
    else:
        print(f"[!] {email} 域名无法加入黑名单: {result.get('error')}")
    return result


def blacklist_trial_domain(email: str, message) -> dict:
    """
    试用失败且文案确认是「地址无资格」时，把该邮箱的域名加入黑名单（一次即封）。

    只有站点明确返回 not eligible for the free trial 才触发；
    401/429/5xx 之类的瞬时失败不会拉黑整个后缀。
    """
    if not is_trial_ineligible(message):
        return {"ok": False, "added": False, "entry": "", "error": "not_ineligible"}
    return auto_blacklist_domain(email)


def blacklist_unsupported_domain(email: str) -> dict:
    """
    邮件服务商声明不支持该收件域名时，把域名加入黑名单（一次即封）。

    触发源是 GPTMail 收件接口的 400 ``Unsupported email domain``：该域名 MX 失效后
    被停用，属于域名级确定性失败——重发验证码、继续轮询都不会有信。
    拉黑规则与试用不合格完全一致（开关 / 根域 / 公共后缀保护 / 幂等）。
    """
    print(f"[!] {email} 邮件服务商不支持该收件域名（Unsupported email domain）")
    return auto_blacklist_domain(email)


# 服务端随机分配域名时（GPTMAIL_DOMAIN 为空），单轮命中的只是「这次抽到的域名」，
# 换个时间可能抽到别的域名；连续这么多轮都只抽到黑名单域名，才判定整个池子不可用。
MAX_BLOCKED_EMAIL_ROUNDS = 5


def acquire_email(email_service, blocked_rounds: int = 0) -> dict:
    """
    创建临时邮箱，并把「黑名单」类失败翻译成注册循环的控制动作。

    参数:
        email_service: EmailService / GPTMailService 实例。
        blocked_rounds: 之前连续「服务端只给出黑名单域名」的轮数，用于判断是否放弃。

    返回:
        ``{"action", "jwt", "email", "blocked_rounds"}``，action 取值：
        - ``"ok"``    ：拿到可用邮箱，jwt/email 有效；
        - ``"retry"`` ：本轮拿不到邮箱（邮箱服务异常 / 返回空 / 服务端连续给黑名单域名），
                        调用方 sleep 后进下一轮；
        - ``"stop"``  ：配置里的域名全被拉黑，或服务端连续 ``MAX_BLOCKED_EMAIL_ROUNDS``
                        轮只给黑名单域名，调用方应结束注册（继续跑只会空转）。

    副作用: 打印失败原因；不修改 ``blocked_rounds`` 之外的状态。
    """
    try:
        jwt, email = email_service.create_email()
    except BlockedEmailRetriesExhausted as e:
        # 随机分配的域名：换一轮可能抽到别的域名，先重试而不是立刻判死
        blocked_rounds += 1
        print(f"[-] {e}（连续 {blocked_rounds}/{MAX_BLOCKED_EMAIL_ROUNDS} 轮）")
        action = "stop" if blocked_rounds >= MAX_BLOCKED_EMAIL_ROUNDS else "retry"
        return {"action": action, "jwt": None, "email": None,
                "blocked_rounds": blocked_rounds}
    except AllDomainsBlacklisted as e:
        # 配置里的域名列表是确定性的：全被拉黑就再也拿不到邮箱
        print(f"[-] {e}")
        return {"action": "stop", "jwt": None, "email": None,
                "blocked_rounds": blocked_rounds}
    except Exception as e:
        print(f"[-] 邮箱服务抛出异常: {e}")
        return {"action": "retry", "jwt": None, "email": None,
                "blocked_rounds": blocked_rounds}

    if not email:
        print("[-] 邮箱创建返回空，可能接口挂了或超时")
        return {"action": "retry", "jwt": None, "email": None,
                "blocked_rounds": blocked_rounds}
    return {"action": "ok", "jwt": jwt, "email": email, "blocked_rounds": 0}


def fetch_inbox_content(email_service, jwt, email: str, email_service_type: str):
    """
    拉取临时邮箱的邮件内容，屏蔽普通异常。

    参数:
        email_service: EmailService / GPTMailService 实例。
        jwt: Cloudflare Worker 服务的 jwt；GPTMail 不使用。
        email: 邮箱地址（GPTMail 必需）。
        email_service_type: "gptmail" 或其它（走 Worker 分支）。

    返回:
        邮件正文；收件箱为空或一般异常时返回 None。

    异常:
        UnsupportedEmailDomain 原样抛出——它是域名级硬失败，必须让调用方
        拉黑该域名并结束本轮，不能被这里的兜底 except 吞掉。
    """
    try:
        if email_service_type == "gptmail":
            return email_service.fetch_first_email(jwt, email=email)
        return email_service.fetch_first_email(jwt)
    except UnsupportedEmailDomain:
        raise
    except Exception as e:
        print(f"[-] 拉取邮件异常: {e}")
        return None


# 邮箱验证轮询默认参数（可用环境变量覆盖，配置页可直接改）
# 站点限制验证码每 2 分钟只能重发一次，所以窗口必须留出"重发之后还能收信"的余量，
# 否则重发等于白做（旧实现窗口仅 ~89s，重发必然撞 400，已修正）。
_VERIFY_DEFAULT_INTERVAL = 2.0
_VERIFY_DEFAULT_WINDOW = 180.0
_VERIFY_DEFAULT_RESEND_AFTER = 120.0
# 连续空收件箱 10 次（约 20s）即换号：兼顾"邮件延迟几十秒"与"坏域名别白等"
_VERIFY_DEFAULT_EMPTY_ABORT = 10

# 失败原因 → 日志文案（供注册主循环复用）
_VERIFY_FAIL_TEXT = {
    "timeout": "轮询窗口内未解析到验证码",
    "empty_inbox": "收件箱持续为空，提前放弃本轮",
    "unsupported_domain": "邮件服务商不支持该收件域名（已拉黑）",
    "stopped": "用户请求停止",
}


def _verify_env_number(name: str, default: float) -> float:
    """
    读取数值型环境变量。

    参数:
        name: 环境变量名。
        default: 未设置或非法时的回退值。

    返回:
        float 值；配置页允许留空，留空或非数字时回退默认（并打印一次提示），不抛异常。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[!] {name}={raw!r} 不是数字，按默认 {default} 处理")
        return default


def _verify_poll_config() -> dict:
    """
    组装验证码轮询参数（每次轮询都重读，配置页改完即生效）。

    返回:
        ``{"interval", "window", "resend_after", "empty_abort"}``：
        - interval: 取信间隔（秒），下限 1；
        - resend_after: 首次发信后多久重发，下限 120（站点冷却要求）；
        - window: 整个轮询窗口（秒），下限 ``resend_after + 30``，保证重发后还有收信时间；
        - empty_abort: 连续取到空收件箱多少次放弃本轮，<=0 表示不启用该提前退出。
    """
    interval = max(
        1.0, _verify_env_number("VERIFY_POLL_INTERVAL", _VERIFY_DEFAULT_INTERVAL)
    )
    resend_after = max(
        120.0, _verify_env_number("VERIFY_RESEND_AFTER", _VERIFY_DEFAULT_RESEND_AFTER)
    )
    window = max(
        resend_after + 30.0,
        _verify_env_number("VERIFY_POLL_WINDOW", _VERIFY_DEFAULT_WINDOW),
    )
    empty_abort = int(
        _verify_env_number("VERIFY_EMPTY_ABORT_FETCHES", _VERIFY_DEFAULT_EMPTY_ABORT)
    )
    return {
        "interval": interval,
        "window": window,
        "resend_after": resend_after,
        "empty_abort": empty_abort,
    }


def _verify_result(reason: str, code=None, domain_blocked: bool = False) -> dict:
    """组装轮询返回结构 ``{"code", "reason", "domain_blocked"}``。"""
    return {"code": code, "reason": reason, "domain_blocked": domain_blocked}


def request_verification_mail(session, access_token, email: str, label: str) -> bool:
    """
    请求站点发送验证邮件（首次发信 / 冷却满足后的重发共用）。

    参数:
        session: 站点会话；None 时不做任何事。
        access_token: 站点 access_token；为空时不请求。
        email: 仅用于日志的邮箱地址。
        label: 日志动作文案（如 "请求发送邮箱验证码" / "再次请求发信"）。

    返回:
        请求是否被站点接受。站点对 2 分钟内的重复请求返回 400，
        调用方只在冷却满足后重发，正常不会触发该分支。

    副作用: 发起一次 HTTP 请求并打印结果。
    """
    if session is None or not access_token:
        return False
    print(f"[*] {email} {label} (reset-verification-code)...")
    rr = resend_verification_code(session, access_token)
    print(f"[*] {email} 发信结果 ({rr['status']}): {rr['message']}")
    return bool(rr.get("ok"))


def _poll_verification_loop(
    email_service, jwt, email, email_service_type, cfg, session, access_token
):
    """
    轮询收件箱直到拿到验证码、判定失败或超时（``poll_verification_code`` 的主循环）。

    参数:
        email_service / jwt / email / email_service_type: 见 ``poll_verification_code``。
        cfg: ``_verify_poll_config()`` 的结果。
        session / access_token: 站点会话与令牌，用于冷却满足后重发。

    返回:
        ``_verify_result(...)``；reason 为 ok / timeout / empty_inbox /
        unsupported_domain / stopped。

    副作用: 网络取信、按需重发、打印进度；unsupported_domain 时写黑名单。
    """
    started = time.monotonic()
    deadline = started + cfg["window"]
    resend_at = started + cfg["resend_after"]
    resend_done = False
    empty_streak = 0
    last_raw_len = 0
    attempt = 0

    while time.monotonic() < deadline:
        if stop_flag:
            return _verify_result("stopped")
        time.sleep(cfg["interval"] if attempt else 1.0)
        attempt += 1
        try:
            content = fetch_inbox_content(email_service, jwt, email, email_service_type)
        except UnsupportedEmailDomain as e:
            print(f"[-] {email} 取信被拒 ({e})")
            blocked = blacklist_unsupported_domain(email)
            return _verify_result(
                "unsupported_domain", domain_blocked=bool(blocked.get("added"))
            )

        if content:
            empty_streak = 0
            if len(content) != last_raw_len:
                last_raw_len = len(content)
                print(f"[*] {email} 收到邮件 (len={len(content)})，解析验证码...")
            code = parse_verification_code(content)
            if code:
                return _verify_result("ok", code=code)
            if attempt in (1, 6, 16, 31):
                snippet = re.sub(r"\s+", " ", content)[:180]
                print(f"[!] {email} 邮件未能解析验证码，snippet: {snippet!r}")
        else:
            empty_streak += 1
            if attempt % 5 == 1:
                print(f"[*] {email} 等待验证邮件... (第 {attempt} 次)")
            if cfg["empty_abort"] > 0 and empty_streak >= cfg["empty_abort"]:
                print(f"[-] {email} 连续 {empty_streak} 次收件箱为空，提前放弃本轮")
                return _verify_result("empty_inbox")

        # 重发只在冷却（默认 120s）满足后做一次；提前重发会被站点 400 拒绝
        if not resend_done and time.monotonic() >= resend_at:
            resend_done = True
            request_verification_mail(session, access_token, email, "仍未收到验证码，再次请求发信")

    return _verify_result("timeout")


def poll_verification_code(
    email_service,
    jwt,
    email: str,
    email_service_type: str,
    session=None,
    access_token: str | None = None,
    config: dict | None = None,
) -> dict:
    """
    轮询临时邮箱获取 ProxyScrape 验证码。

    参数:
        email_service: 邮箱服务实例（Worker / GPTMail）。
        jwt: Worker jwt；GPTMail 不使用。
        email: 邮箱地址。
        email_service_type: "gptmail" 或其它。
        session: 站点会话；提供时先请求发信，并在冷却满足后重发一次。
        access_token: 站点 access_token，配合 session 使用。
        config: 覆盖 ``_verify_poll_config()``（测试注入用，避免真等 180s）。

    返回:
        ``{"code", "reason", "domain_blocked"}``，reason 取值：
        - "ok"                 拿到验证码；
        - "timeout"            窗口内始终无可解析验证码；
        - "empty_inbox"        连续空收件箱达阈值，提前放弃；
        - "unsupported_domain" 服务商不支持该域名，已加入黑名单（domain_blocked=True）；
        - "stopped"            用户请求停止。

    副作用: 网络请求、打印进度；reason=unsupported_domain 时写 ``.env`` 黑名单。
    """
    cfg = config or _verify_poll_config()
    request_verification_mail(session, access_token, email, "请求发送邮箱验证码")
    return _poll_verification_loop(
        email_service, jwt, email, email_service_type, cfg, session, access_token
    )


def register_accounts():
    """单线程 ProxyScrape 注册循环。"""
    global success_count, stop_flag

    EMAIL_SERVICE_TYPE = os.getenv("EMAIL_SERVICE_TYPE", "cloudflare")
    try:
        if EMAIL_SERVICE_TYPE == "gptmail":
            email_service = GPTMailService()
        else:
            email_service = EmailService()
        turnstile_service = TurnstileService()
    except Exception as e:
        print(f"[-] 服务初始化失败: {e}")
        return

    print(f"[*] 注册目标: {SIGNUP_URL}")
    print(f"[*] API: {REGISTER_ENDPOINT}")
    print(f"[*] Turnstile sitekey: {TURNSTILE_SITEKEY}")

    consecutive_captcha_fails = 0
    max_captcha_rounds = int(os.getenv("MAX_CAPTCHA_FAIL_ROUNDS", "3") or "3")
    # 连续「服务端只给黑名单域名」的轮数，见 acquire_email
    blocked_email_rounds = 0

    while not stop_flag and success_count < target_count:
        try:
            with create_session() as session:
                try:
                    session.get(SIGNUP_URL, timeout=15)
                except Exception:
                    pass

                password = get_register_password()

                acq = acquire_email(email_service, blocked_email_rounds)
                blocked_email_rounds = acq["blocked_rounds"]
                if acq["action"] == "stop":
                    print(
                        "[-] 没有可用邮箱域名，停止注册"
                        "（可在 Web 配置页「邮箱黑名单」检查或解封）"
                    )
                    return
                jwt, email = acq["jwt"], acq["email"]
                if acq["action"] != "ok":
                    print("[-] 本轮未拿到可用邮箱，等待 5s 后重试...")
                    time.sleep(5)
                    continue

                if stop_flag:
                    return

                wait_register_interval()
                if stop_flag:
                    return

                if EMAIL_SERVICE_TYPE == "gptmail":
                    quota_log = email_service.format_quota_log()
                    print(f"[*] 开始 ProxyScrape 注册: {email}{quota_log}")
                else:
                    print(f"[*] 开始 ProxyScrape 注册: {email}")

                # Step 1: Turnstile
                token = None
                for captcha_try in range(3):
                    if stop_flag:
                        return
                    print(f"[*] {email} 求解 Turnstile ({captcha_try + 1}/3)...")
                    token = solve_turnstile(turnstile_service)
                    if token:
                        break
                    print(f"[-] {email} CAPTCHA 失败，重试...")
                    time.sleep(1)

                if not token:
                    consecutive_captcha_fails += 1
                    print(
                        f"[-] {email} CAPTCHA 连续失败，换号 "
                        f"({consecutive_captcha_fails}/{max_captcha_rounds})"
                    )
                    if consecutive_captcha_fails >= max_captcha_rounds:
                        print(
                            "[-] 本地 Turnstile Solver 不可用或持续失败，"
                            "停止注册（请启动 api_solver 后重试）"
                        )
                        return
                    time.sleep(2)
                    continue

                consecutive_captcha_fails = 0

                # Step 2: Register
                result = register_http(session, email, password, token)
                if not result["ok"]:
                    print(
                        f"[-] {email} 注册失败 ({result['status']}): {result['message']}"
                    )
                    # Site-side reasons (captcha/email) are logged; not Grok config errors
                    time.sleep(3)
                    continue

                parsed = result["data"]
                access_token = parsed["access_token"]
                email_verified = bool(parsed.get("email_verified", False))
                print(
                    f"[+] {email} 注册 API 成功 | token={access_token[:16]}... | "
                    f"EmailVerified={email_verified}"
                )

                # Confirm with /me (register payload can lag; source of truth)
                me = fetch_account_me(session, access_token)
                if me is not None:
                    email_verified = bool(me.get("EmailVerified") or me.get("emailVerified"))
                    print(f"[*] {email} /me EmailVerified={email_verified}")

                # Step 3: Email verification — required when not verified.
                # Register does NOT reliably auto-send mail; we call resend endpoint.
                if not email_verified:
                    print(f"[*] {email} 开始邮箱验证流程...")
                    vpoll = poll_verification_code(
                        email_service,
                        jwt,
                        email,
                        EMAIL_SERVICE_TYPE,
                        session=session,
                        access_token=access_token,
                    )
                    code = vpoll["code"]
                    if not code:
                        reason = _VERIFY_FAIL_TEXT.get(
                            vpoll["reason"], "未解析到验证码"
                        )
                        print(f"[-] {email} {reason}；已保存未验证账号（不计入成功）")
                        paths = save_account_credentials(
                            email, password, access_token, extra="UNVERIFIED"
                        )
                        print(f"[~] 未验证账号 -> {paths['accounts']}")
                        # 域名不支持 / 收件箱长期为空 / 用户停止：本轮已无意义，直接换号
                        time.sleep(3)
                        continue

                    print(f"[*] {email} 验证码: {code}")
                    vres = verify_email_http(session, access_token, code)
                    if not vres["ok"]:
                        print(
                            f"[-] {email} 邮箱验证失败 ({vres['status']}): {vres['message']}"
                        )
                        paths = save_account_credentials(
                            email, password, access_token, extra="UNVERIFIED"
                        )
                        print(f"[~] 验证失败账号 -> {paths['accounts']}")
                        time.sleep(3)
                        continue
                    print(f"[+] {email} 邮箱验证成功")

                    me2 = fetch_account_me(session, access_token)
                    if me2 is not None:
                        print(
                            f"[*] {email} 验证后 /me EmailVerified="
                            f"{me2.get('EmailVerified')} typeform="
                            f"{me2.get('typeform')}"
                        )

                # Step 4: Typeform onboarding ("Let us know a little more about you")
                # Until typeform=false, dashboard redirects to /v2/typeform and blocks
                # real backend overview. Complete via API (same as embed onSubmit).
                me3 = fetch_account_me(session, access_token)
                if me3 is None or needs_typeform_onboarding(me3):
                    print(
                        f"[*] {email} 开始 onboarding 问卷 "
                        f"(typeform form_id={TYPEFORM_FORM_ID})..."
                    )
                    tres = complete_typeform_onboarding(session, access_token)
                    if not tres["ok"]:
                        print(
                            f"[-] {email} 问卷提交失败 ({tres['status']}): "
                            f"{tres['message']}"
                        )
                        paths = save_account_credentials(
                            email, password, access_token, extra="NO_TYPEFORM"
                        )
                        print(f"[~] 未完成问卷账号 -> {paths['accounts']}")
                        time.sleep(3)
                        continue
                    print(
                        f"[+] {email} 问卷完成: {tres['message']} "
                        f"(response_id={tres['fields'].get('response_id')})"
                    )
                    me4 = fetch_account_me(session, access_token)
                    if me4 is not None and needs_typeform_onboarding(me4):
                        print(
                            f"[-] {email} 问卷 API 成功但 /me typeform 仍为 true"
                        )
                        paths = save_account_credentials(
                            email, password, access_token, extra="NO_TYPEFORM"
                        )
                        print(f"[~] 问卷未生效账号 -> {paths['accounts']}")
                        time.sleep(3)
                        continue
                    if me4 is not None:
                        print(
                            f"[*] {email} 问卷后 /me typeform={me4.get('typeform')}"
                        )
                else:
                    print(f"[*] {email} onboarding 问卷已完成，跳过")

                # Step 5: Activate the supported Premium free-trial API.
                # This only runs after /me has confirmed onboarding is complete.
                print(f"[*] {email} 激活 Premium 免费试用...")
                cres = claim_premium_trial(session, access_token)
                if not cres["ok"]:
                    print(
                        f"[-] {email} 免费试用激活失败 ({cres['status']}): "
                        f"{cres['message']}"
                    )
                    blacklist_trial_domain(email, cres.get("message"))
                    paths = save_account_credentials(
                        email, password, access_token, extra="NO_PREMIUM_TRIAL"
                    )
                    print(f"[~] 未激活免费试用账号 -> {paths['accounts']}")
                    time.sleep(3)
                    continue
                print(f"[+] {email} Premium 免费试用已激活: {cres['message']}")

                # Step 6: Download premium proxy list (primary deliverable)
                # Page: /v2/services/premium/proxy-list/{accountId}
                # Format: protocol://user:pass@host:port  (credential_format=3)
                me_final = fetch_account_me(session, access_token) or {}
                account_id = pick_subaccount_id(me_final)
                if not account_id:
                    # fallback to register userData
                    account_id = pick_subaccount_id(parsed.get("user_data") or {})
                if not account_id:
                    print(f"[-] {email} 无 AccountID，无法下载 proxy-list")
                    save_account_credentials(
                        email, password, access_token, extra="NO_ACCOUNT_ID"
                    )
                    time.sleep(3)
                    continue

                print(
                    f"[*] {email} 下载 proxy-list | accountId={account_id} | "
                    f"protocol={PROXY_DOWNLOAD_PROTOCOL} | "
                    f"format=protocol://user:pass@host:port"
                )
                print(f"[*] 页面: {proxy_list_page_url(account_id)}")

                ov = fetch_overview(session, access_token, account_id)
                if ov and isinstance(ov.get("data"), dict):
                    dc = (
                        (ov["data"].get("services") or {})
                        .get("datacenter_shared")
                        or {}
                    )
                    pu = dc.get("proxy_username")
                    pp = dc.get("proxy_password")
                    n_proxies = dc.get("proxy_amount")
                    print(
                        f"[*] credentials user={pu} pass="
                        f"{(pp[:3] + '***') if pp else None} "
                        f"proxy_amount={n_proxies}"
                    )
                    # Cache expiry/bandwidth now: the feed reads this later, and
                    # re-using the overview we already fetched costs no extra request.
                    # overview failure writes nothing → account stays 'unknown'.
                    try:
                        details = normalize_overview_payload(ov)
                        if details.get("ok"):
                            upsert_account_details_cache(
                                {
                                    "email": email,
                                    "details": details,
                                    # /me-derived id — same value used for the filename
                                    "subaccount_id": account_id,
                                    "email_verified": True,
                                    "typeform_pending": False,
                                    "flags": [],
                                }
                            )
                    except Exception as e:
                        print(f"[-] {email} 详情缓存写入失败: {e}")

                dres = download_premium_proxies(
                    session, access_token, account_id, PROXY_DOWNLOAD_PROTOCOL
                )
                if not dres["ok"] or not dres["lines"]:
                    print(
                        f"[-] {email} 代理下载失败 ({dres['status']}): "
                        f"{dres['message']}"
                    )
                    save_account_credentials(
                        email, password, access_token, extra="NO_PROXIES"
                    )
                    time.sleep(3)
                    continue

                proxy_paths = save_proxy_lines(
                    dres["lines"],
                    account_id=account_id,
                    email=email,
                )
                # Optional side log of account (not primary output)
                save_account_credentials(email, password, access_token)

                success_count += 1
                avg = (time.time() - start_time) / success_count
                print(
                    f"[✓] 代理下载成功: {email} | {proxy_paths['count']} 条 | "
                    f"样例: {dres['sample'][:60]}... | "
                    f"{success_count}/{target_count} | 平均: {avg:.1f}s"
                )
                print(
                    f"[*] 已写入 {proxy_paths['proxies']}"
                    + (
                        f" 与 {proxy_paths['account_proxies']}"
                        if proxy_paths.get("account_proxies")
                        else ""
                    )
                )
                if success_count >= target_count:
                    print(f"[*] 已达到目标数量: {success_count}/{target_count}")
                    return

        except KeyboardInterrupt:
            stop_flag = True
            print("\n[!] 检测到中断，正在停止...")
            return
        except Exception as e:
            print(f"[-] 异常: {str(e)[:120]}")
            time.sleep(5)


def _read_target_count() -> int:
    """Read registration count from env REGISTER_COUNT or stdin (TTY-friendly)."""
    env_count = os.getenv("REGISTER_COUNT", "").strip()
    if env_count:
        try:
            return max(1, int(env_count))
        except ValueError:
            pass
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if line.strip():
            try:
                return max(1, int(line.strip()))
            except ValueError:
                return 100
        return 100
    try:
        total = int(input("注册数量 (默认100): ").strip() or 100)
        return max(1, total)
    except Exception:
        return 100


def main():
    print("=" * 60)
    print("ProxyScrape 注册 + Premium 代理下载")
    print(f"注册: {SIGNUP_URL}")
    print("下载: /v2/services/premium/proxy-list/{accountId}")
    print(f"格式: protocol://user:pass@host:port  (protocol={PROXY_DOWNLOAD_PROTOCOL})")
    print("=" * 60)

    print("[*] 正在初始化...")
    print(f"[+] Register: {REGISTER_ENDPOINT}")
    print(f"[+] Turnstile sitekey: {TURNSTILE_SITEKEY}")
    print(f"[+] Proxy download credential_format={CREDENTIAL_FORMAT_PROTOCOL_URL}")

    global target_count, stop_flag, success_count, start_time
    target_count = _read_target_count()
    stop_flag = False
    success_count = 0
    start_time = time.time()

    interval_info = (
        f"，注册间隔 {REGISTER_INTERVAL}s" if REGISTER_INTERVAL > 0 else ""
    )
    email_type = os.getenv("EMAIL_SERVICE_TYPE", "cloudflare")
    print(f"[*] 邮箱服务: {email_type}")
    print(f"[*] Turnstile: 本地 Solver ({TURNSTILE_SITEKEY[:12]}...)")
    print(f"[*] 目标 {target_count} 个账号（注册→验证→问卷→下载代理）{interval_info}")
    print(f"[*] 主产物: keys/proxies.txt  (追加写入，不覆盖)")

    try:
        register_accounts()
    except KeyboardInterrupt:
        stop_flag = True
        print("\n[!] 检测到 Ctrl+C，已停止")

    print(f"[*] 结束，成功注册 {success_count}/{target_count}")
    print("[*] 代理列表由 Resin 反向拉取本机 feed: GET /api/feed/proxies")


if __name__ == "__main__":
    main()
