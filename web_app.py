"""
ProxyScrape Web 管理台

启动:
  python web_app.py
  # 默认 http://127.0.0.1:5080

功能:
  - 列表 keys/proxyscrape_accounts.txt 中的账号
  - 刷新各账号试用到期时间、流量剩余等 overview 详情
  - 从页面发起注册 N 个账号（复用 main.register_accounts）
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
import time
import threading
from typing import Optional
from urllib.parse import quote

from dotenv import load_dotenv
from quart import Quart, jsonify, request, Response, session, redirect

load_dotenv()

from src.proxyscrape_helpers import (  # noqa: E402
    ACCOUNTS_FILE,
    PROXIES_FILE,
    load_accounts_from_file,
    merge_account_with_details,
    pick_subaccount_id,
    normalize_overview_payload,
    apply_proxy_countries_to_details,
    delete_account_local_data,
    delete_all_accounts_local_data,
    format_countdown,
    load_proxies_file_content,
    load_account_details_cache,
    apply_cached_details_to_row,
    upsert_account_details_cache,
    remove_account_details_cache,
    clear_account_details_cache,
    is_access_token_expired,
    account_expiry_state,
    resolve_cached_account_id,
    collect_valid_proxy_lines,
)
from src.env_config import (  # noqa: E402
    DEFAULT_ENV_PATH,
    CONFIG_SCHEMA,
    get_config_for_ui,
    upsert_env_file,
    apply_updates_to_environ,
    reload_main_module_config,
    all_config_keys,
)
import main as reg  # noqa: E402

HOST = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.getenv("WEB_PORT", "5080") or "5080")

# Web 管理台登录密码（.env: WEB_PASSWORD，默认 admin）
DEFAULT_WEB_PASSWORD = "admin"

app = Quart(__name__)
# Cookie session signing key — stable across restarts when WEB_SECRET_KEY / WEB_PASSWORD set
_web_secret = (os.getenv("WEB_SECRET_KEY") or "").strip()
if not _web_secret:
    _web_secret = hashlib.sha256(
        f"proxyscrape-web:{os.getenv('WEB_PASSWORD', DEFAULT_WEB_PASSWORD)}".encode(
            "utf-8"
        )
    ).hexdigest()
app.secret_key = _web_secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7  # 7 days


def _web_password() -> str:
    """Current login password from env (updated when config is saved)."""
    return (os.getenv("WEB_PASSWORD") or DEFAULT_WEB_PASSWORD).strip() or DEFAULT_WEB_PASSWORD


def _feed_token() -> str:
    """Current proxy-feed token from env (read per request so /config applies at once).

    Empty means the feed is disabled — never serve an unauthenticated feed.
    """
    return (os.getenv("FEED_TOKEN") or "").strip()


def _is_authed() -> bool:
    return bool(session.get("web_auth"))


def _verify_password(password: str) -> bool:
    expected = _web_password()
    try:
        return hmac.compare_digest(
            (password or "").encode("utf-8"),
            expected.encode("utf-8"),
        )
    except (TypeError, ValueError):
        return False


# Paths that do not require login
_AUTH_PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/api/auth/login",
        "/api/auth/status",
        "/api/health",
        # Token-gated inside the route (remote feed pullers cannot log in)
        "/api/feed/proxies",
    }
)


@app.before_request
async def _require_web_auth():
    path = request.path or "/"
    if path in _AUTH_PUBLIC_PATHS:
        return None
    if _is_authed():
        return None
    if path.startswith("/api/"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "未登录",
                    "auth_required": True,
                }
            ),
            401,
        )
    next_q = quote(path, safe="/")
    return redirect(f"/login?next={next_q}")

_register_lock = threading.Lock()
_register_job: dict = {
    "running": False,
    "requested": 0,
    "started_at": None,
    "finished_at": None,
    "message": "idle",
    "success_at_start": 0,
}

_log_lock = threading.Lock()
_register_logs: list = []
_MAX_LOG_LINES = 3000


def _append_log(line: str):
    text = (line or "").rstrip("\n\r")
    if not text:
        return
    with _log_lock:
        _register_logs.append(text)
        overflow = len(_register_logs) - _MAX_LOG_LINES
        if overflow > 0:
            del _register_logs[:overflow]


def _get_logs(since: int = 0) -> dict:
    with _log_lock:
        total = len(_register_logs)
        since = max(0, min(int(since or 0), total))
        lines = list(_register_logs[since:])
        return {"lines": lines, "next": total, "total": total}


def _clear_logs():
    with _log_lock:
        _register_logs.clear()


class _StdoutLogTee:
    """Tee stdout into register log buffer (for web UI)."""

    def __init__(self, original):
        self.original = original
        self._buf = ""

    def write(self, s):
        if self.original is not None:
            self.original.write(s)
        if not s:
            return
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            _append_log(line)

    def flush(self):
        if self.original is not None:
            self.original.flush()
        if self._buf.strip():
            _append_log(self._buf)
            self._buf = ""


def _auth_session():
    return reg.create_session()


def _enrich_account(acc: dict, live: bool = False) -> dict:
    """Build UI row; optionally fetch /me + overview + country meta. Persist live details.

    Live path auto re-logins when JWT is expired/missing so bandwidth matches dashboard.
    """
    if not live:
        row = merge_account_with_details(acc, overview=None, me=None)
        # Restore last successful refresh from disk cache
        row = apply_cached_details_to_row(row, load_account_details_cache())
        return row

    # live=True: need a working token (re-login if expired)
    acc = dict(acc)
    me = None
    overview = None
    countries_payload = None
    err = None
    token_refreshed = False
    try:
        with _auth_session() as session:
            ensure = reg.ensure_fresh_access_token(session, acc, force=False)
            if not ensure.get("ok"):
                err = ensure.get("message") or "token 无效或已过期"
            else:
                token = ensure["access_token"]
                token_refreshed = bool(ensure.get("refreshed"))
                acc["access_token"] = token
                me = reg.fetch_account_me(session, token)
                # If /me still fails after JWT looked valid, force re-login once
                if me is None and not token_refreshed and acc.get("password"):
                    ensure2 = reg.ensure_fresh_access_token(
                        session, acc, force=True
                    )
                    if ensure2.get("ok"):
                        token = ensure2["access_token"]
                        token_refreshed = True
                        acc["access_token"] = token
                        me = reg.fetch_account_me(session, token)
                    else:
                        err = ensure2.get("message") or "重登失败"
                account_id = pick_subaccount_id(me) if me else None
                if account_id:
                    overview = reg.fetch_overview(session, token, account_id)
                    countries_payload = reg.fetch_proxy_list_meta(
                        session, token, account_id
                    )
                elif me is None and not err:
                    err = "token 无效或已过期"
    except Exception as e:
        err = str(e)[:120]

    row = merge_account_with_details(acc, overview=overview, me=me)
    if countries_payload is not None and isinstance(row.get("details"), dict):
        row["details"] = apply_proxy_countries_to_details(
            row["details"], countries_payload
        )
    row["token_refreshed"] = token_refreshed
    if err and not row["details"].get("ok"):
        row["details"]["error"] = err
        row["details"]["ok"] = False
        # Stale cache after failed live refresh is often wrong (esp. bandwidth).
        # Only restore cache when we never managed a live attempt with a valid token
        # and the failure is not clearly auth — still restore so offline list works,
        # but mark bandwidth as potentially stale in error text.
        row = apply_cached_details_to_row(row, load_account_details_cache())
        if row.get("details", {}).get("ok") and (
            "过期" in (err or "") or "重登" in (err or "") or "token" in (err or "").lower()
        ):
            # Keep cached numbers but surface that they may be outdated
            d = dict(row["details"])
            d["error"] = f"{err}（显示为缓存，可能不是最新流量）"
            d["stale"] = True
            # Keep ok True so numbers still show; UI shows warn tag via error/stale
            row["details"] = d
    elif row.get("details", {}).get("ok"):
        upsert_account_details_cache(row)
    return row


def _resolve_account_id(session, acc: dict, token: Optional[str] = None) -> Optional[str]:
    tok = (token if token is not None else acc.get("access_token")) or ""
    if not tok:
        return None
    me = reg.fetch_account_me(session, tok)
    return pick_subaccount_id(me) if me else None


def _download_proxies_for_account(acc: dict) -> dict:
    """Live-download proxies for one account store row (re-login if token expired)."""
    email = acc.get("email") or ""
    acc = dict(acc)
    try:
        with _auth_session() as session:
            ensure = reg.ensure_fresh_access_token(session, acc, force=False)
            if not ensure.get("ok"):
                return {
                    "ok": False,
                    "error": ensure.get("message")
                    or "无有效 access_token（可能已过期）",
                    "lines": [],
                    "email": email,
                }
            token = ensure["access_token"]
            acc["access_token"] = token
            account_id = _resolve_account_id(session, acc, token=token)
            if not account_id and acc.get("password"):
                # force re-login once if /me failed
                ensure2 = reg.ensure_fresh_access_token(session, acc, force=True)
                if ensure2.get("ok"):
                    token = ensure2["access_token"]
                    acc["access_token"] = token
                    account_id = _resolve_account_id(session, acc, token=token)
            if not account_id:
                return {
                    "ok": False,
                    "error": "无法解析 AccountID（token 可能过期）",
                    "lines": [],
                    "email": email,
                }
            dres = reg.download_premium_proxies(
                session,
                token,
                account_id,
                getattr(reg, "PROXY_DOWNLOAD_PROTOCOL", "http"),
            )
            if not dres.get("ok"):
                return {
                    "ok": False,
                    "error": dres.get("message") or "下载失败",
                    "lines": [],
                    "email": email,
                    "account_id": account_id,
                }
            return {
                "ok": True,
                "lines": dres["lines"],
                "email": email,
                "account_id": account_id,
                "count": len(dres["lines"]),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "lines": [], "email": email}


def _run_register_job(count: int):
    global _register_job
    old_out = sys.stdout
    tee = _StdoutLogTee(old_out)
    sys.stdout = tee
    try:
        _append_log(f"===== 开始注册任务 count={count} =====")
        reg.target_count = max(1, int(count))
        reg.stop_flag = False
        reg.success_count = 0
        reg.start_time = time.time()
        _register_job["success_at_start"] = 0
        _register_job["message"] = f"注册中 target={reg.target_count}"
        reg.register_accounts()
        _register_job["message"] = (
            f"完成 success={reg.success_count}/{reg.target_count}"
        )
        _append_log(
            f"===== 注册任务结束 success={reg.success_count}/{reg.target_count} ====="
        )
    except Exception as e:
        _register_job["message"] = f"异常: {str(e)[:160]}"
        _append_log(f"[ERROR] {e}")
    finally:
        try:
            tee.flush()
        except Exception:
            pass
        sys.stdout = old_out
        _register_job["running"] = False
        _register_job["finished_at"] = time.time()


# ---------------------------------------------------------------------------
# Auto top-up registration scheduler
#
# Single-process only: this is a daemon thread inside web_app.py, and it assumes
# one process owns keys/. Do not run web_app.py under multiple workers.
# ---------------------------------------------------------------------------

_AUTO_REGISTER_MAX_BACKOFF = 6
_AUTO_REGISTER_DISABLED_POLL = 60.0

_scheduler_stop = threading.Event()
_scheduler_started = False
_scheduler_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(float((os.getenv(name) or "").strip() or default))
    except (TypeError, ValueError):
        return default


def _auto_register_config() -> dict:
    """Read scheduler config from env on every tick (config page changes apply live)."""
    enabled = (os.getenv("AUTO_REGISTER_ENABLED", "false") or "false").strip().lower()
    return {
        "enabled": enabled in ("1", "true", "yes", "on"),
        "interval": max(30, _env_int("AUTO_REGISTER_INTERVAL", 1800)),
        "target": max(1, _env_int("AUTO_REGISTER_TARGET", 50)),
        "min_valid": max(0, _env_int("AUTO_REGISTER_MIN_VALID", 10)),
        "max_per_round": max(1, _env_int("AUTO_REGISTER_MAX_PER_ROUND", 20)),
    }


def _count_feed_eligible() -> Optional[int]:
    """Usable accounts (usable expiry + non-empty proxy file), or None on error."""
    try:
        return int(collect_valid_proxy_lines().get("feed_eligible_count") or 0)
    except Exception as e:
        _append_log(f"[-] [auto] 统计可供给账号失败: {str(e)[:160]}")
        return None


def _maybe_auto_register_once() -> dict:
    """One scheduler decision. Returns {action, have?, needed?}."""
    cfg = _auto_register_config()
    if not cfg["enabled"]:
        return {"action": "disabled"}

    have = _count_feed_eligible()
    if have is None:
        return {"action": "error"}
    if have >= cfg["min_valid"]:
        return {"action": "enough", "have": have}

    # "How many can I serve" is the metric, not reg.success_count (which only
    # counts API successes, regardless of whether a usable account resulted).
    needed = max(1, min(cfg["target"] - have, cfg["max_per_round"]))

    # Same critical section as POST /api/register — never run two rounds at once.
    with _register_lock:
        if _register_job["running"]:
            return {"action": "busy", "have": have}
        _register_job["running"] = True
        _register_job["requested"] = needed
        _register_job["started_at"] = time.time()
        _register_job["finished_at"] = None
        _register_job["message"] = f"auto starting count={needed}"

    _append_log(
        f"[*] [auto] 可供给账号 {have} < {cfg['min_valid']}，自动补齐 {needed} 个"
    )
    threading.Thread(target=_run_register_job, args=(needed,), daemon=True).start()
    return {"action": "started", "have": have, "needed": needed}


def _wait_for_register_job(timeout: float = 6 * 3600.0, poll: float = 2.0) -> None:
    """Wait until the running register round finishes (or stop/timeout)."""
    deadline = time.time() + timeout
    while _register_job.get("running"):
        if _scheduler_stop.is_set() or time.time() >= deadline:
            return
        if _scheduler_stop.wait(poll):
            return


def _auto_register_loop() -> None:
    backoff = 1
    while not _scheduler_stop.is_set():
        interval = _auto_register_config()["interval"]
        wait = float(interval)
        try:
            result = _maybe_auto_register_once()
        except Exception as e:
            _append_log(f"[-] [auto] 调度异常: {str(e)[:160]}")
            result = {"action": "error"}

        action = result.get("action")
        if action == "disabled":
            # Cheap no-op tick so flipping AUTO_REGISTER_ENABLED takes effect soon.
            wait = min(float(interval), _AUTO_REGISTER_DISABLED_POLL)
        elif action == "started":
            # Backoff: register_accounts() returns immediately when the Turnstile
            # solver is unavailable, so a round that does not raise the usable
            # account count must slow down instead of spinning every interval.
            before = result.get("have")
            _wait_for_register_job()
            after = _count_feed_eligible()
            if before is not None and after is not None and after > before:
                backoff = 1
            else:
                backoff = min(backoff * 2, _AUTO_REGISTER_MAX_BACKOFF)
                _append_log(
                    f"[!] [auto] 本轮未增加可供给账号 ({before} → {after})，"
                    f"下轮等待 ×{backoff}"
                )
            wait = float(interval) * backoff

        if _scheduler_stop.wait(wait):
            return


def _start_auto_register_scheduler() -> bool:
    """Start the daemon scheduler once (idempotent). Called from main() only."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True
    _scheduler_stop.clear()
    threading.Thread(
        target=_auto_register_loop, name="auto-register", daemon=True
    ).start()
    cfg = _auto_register_config()
    _append_log(
        f"[*] [auto] 调度器已启动 enabled={cfg['enabled']} "
        f"interval={cfg['interval']}s min_valid={cfg['min_valid']} "
        f"target={cfg['target']}"
    )
    return True


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ProxyScrape 账号管理</title>
  <style>
    :root {
      --bg: #0f1419;
      --panel: #1a2332;
      --border: #2d3a4d;
      --text: #e7ecf3;
      --muted: #8b9bb4;
      --accent: #3b82f6;
      --ok: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.45;
    }
    header {
      padding: 1rem 1.5rem; border-bottom: 1px solid var(--border);
      background: var(--panel); display: flex; flex-wrap: wrap;
      align-items: center; gap: 1rem; justify-content: space-between;
    }
    header h1 { margin: 0; font-size: 1.15rem; font-weight: 600; }
    header .sub { color: var(--muted); font-size: 0.85rem; }
    main { padding: 1.25rem 1.5rem; max-width: 1400px; margin: 0 auto; }
    .card {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 1rem;
    }
    .card h2 { margin: 0 0 0.75rem; font-size: 1rem; }
    .row { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; }
    label { color: var(--muted); font-size: 0.85rem; }
    input[type=number] {
      width: 5rem; padding: 0.4rem 0.55rem; border-radius: 6px;
      border: 1px solid var(--border); background: var(--bg); color: var(--text);
    }
    button {
      cursor: pointer; border: none; border-radius: 6px;
      padding: 0.45rem 0.9rem; font-weight: 600; font-size: 0.875rem;
      background: var(--accent); color: #fff;
    }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    button.secondary { background: #334155; }
    button.danger { background: var(--bad); }
    table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
    th, td {
      text-align: left; padding: 0.55rem 0.5rem;
      border-bottom: 1px solid var(--border); vertical-align: top;
    }
    th { color: var(--muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }
    tr:hover td { background: rgba(59,130,246,0.06); }
    .tag {
      display: inline-block; padding: 0.1rem 0.4rem; border-radius: 4px;
      font-size: 0.7rem; background: #243044; color: var(--muted); margin-right: 0.25rem;
    }
    .tag.ok { background: rgba(34,197,94,0.15); color: var(--ok); }
    .tag.bad { background: rgba(239,68,68,0.15); color: var(--bad); }
    .tag.warn { background: rgba(245,158,11,0.15); color: var(--warn); }
    .mono { font-family: ui-monospace, Consolas, monospace; font-size: 0.8rem; }
    .muted { color: var(--muted); }
    .status { font-size: 0.85rem; color: var(--muted); min-height: 1.2em; }
    #detailPanel pre {
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      padding: 0.75rem; overflow: auto; font-size: 0.8rem; max-height: 280px;
    }
    .empty { color: var(--muted); padding: 1.5rem; text-align: center; }
    #logPanel {
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      padding: 0.75rem; overflow: auto; font-size: 0.78rem; max-height: 360px;
      font-family: ui-monospace, Consolas, monospace; white-space: pre-wrap;
      line-height: 1.4; min-height: 160px;
    }
    #logPanel .log-err { color: var(--bad); }
    #logPanel .log-ok { color: var(--ok); }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>ProxyScrape 账号管理</h1>
      <div class="sub">注册账号 · 到期倒计时 · 流量剩余 · 删除本地数据</div>
    </div>
    <div class="row">
      <a href="/config" style="color:var(--accent);text-decoration:none;font-weight:600;padding:0.45rem 0.7rem">配置</a>
      <button type="button" class="secondary" id="btnReload">刷新列表</button>
      <button type="button" id="btnRefreshAll">刷新全部详情</button>
      <button type="button" id="btnDownloadAll">下载全部 proxies</button>
      <button type="button" class="danger" id="btnDeleteInvalid">删除过期账号</button>
      <button type="button" class="danger" id="btnDeleteAll">全部删除</button>
      <button type="button" class="secondary" id="btnLogout">退出</button>
    </div>
  </header>
  <main>
    <section class="card" id="registerSection">
      <h2>批量注册</h2>
      <div class="row">
        <label for="regCount">注册数量</label>
        <input type="number" id="regCount" min="1" value="1" />
        <button type="button" id="btnRegister">开始注册</button>
        <button type="button" class="danger" id="btnStop" disabled>停止</button>
      </div>
      <p class="status" id="regStatus">注册状态: idle</p>
      <p class="muted" style="margin:0.5rem 0 0;font-size:0.8rem">
        调用与 CLI 相同的 <code>main.register_accounts</code> 路径（需本地 Turnstile Solver）。
      </p>
    </section>

    <section class="card">
      <h2>账号列表 <span class="muted" id="accCount"></span></h2>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>邮箱</th>
              <th>剩余到期</th>
              <th>流量剩余</th>
              <th>代理数</th>
              <th>地区</th>
              <th>类型</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="accBody">
            <tr><td colspan="8" class="empty">加载中…</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card" id="logSection">
      <h2 class="row" style="justify-content:space-between">
        <span>注册日志</span>
        <span class="row">
          <button type="button" class="secondary" id="btnClearLog">清空日志</button>
          <button type="button" class="secondary" id="btnScrollLog">滚到底部</button>
        </span>
      </h2>
      <div id="logPanel" class="muted">等待注册任务输出…</div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let accountsCache = [];
    let logCursor = 0;
    let logAutoScroll = true;

    async function api(path, opts) {
      const r = await fetch(path, opts);
      if (r.status === 401) {
        location.href = "/login?next=" + encodeURIComponent(location.pathname);
        throw new Error("未登录");
      }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || j.message || r.statusText);
      return j;
    }

    function formatCountdownClient(unixSec) {
      if (unixSec == null || unixSec === "") return "—";
      const now = Math.floor(Date.now() / 1000);
      let sec = Math.floor(Number(unixSec) - now);
      if (Number.isNaN(sec)) return "—";
      if (sec <= 0) return "已过期";
      const days = Math.floor(sec / 86400);
      const h = Math.floor((sec % 86400) / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      const pad = (n) => String(n).padStart(2, "0");
      if (days > 0) return `${days}天 ${pad(h)}:${pad(m)}:${pad(s)}`;
      return `${pad(h)}:${pad(m)}:${pad(s)}`;
    }

    /** Hover: absolute expiry in Beijing time (UTC+8). */
    function formatBeijingExpiry(unixSec) {
      if (unixSec == null || unixSec === "") return "";
      const ms = Number(unixSec) * 1000;
      if (Number.isNaN(ms)) return "";
      try {
        const fmt = new Intl.DateTimeFormat("zh-CN", {
          timeZone: "Asia/Shanghai",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        });
        // zh-CN often yields "2026/07/26 15:59"
        const s = fmt.format(new Date(ms)).replace(/\//g, "-");
        return s;
      } catch (e) {
        return "";
      }
    }

    /** Hover: US:52, DE:11 (uppercase codes; no total). */
    function formatCountriesBreakdown(d) {
      if (d && d.countries_breakdown) {
        // Prefer server field; upper-case any legacy lowercase codes
        return String(d.countries_breakdown).replace(
          /\b([a-z]{2}):/g,
          (_, cc) => cc.toUpperCase() + ":"
        );
      }
      const countries = (d && d.countries) || {};
      const parts = Object.keys(countries)
        .map((cc) => [String(cc).toLowerCase(), Number(countries[cc]) || 0])
        .filter((kv) => kv[0])
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([cc, n]) => `${cc.toUpperCase()}:${n}`);
      return parts.join(", ");
    }

    function flagTags(row) {
      const tags = [];
      if (!row.has_token) tags.push('<span class="tag bad">无token</span>');
      else if (row.token_expired) tags.push('<span class="tag bad" title="JWT 已过期，点「刷新」会自动用密码重登">token过期</span>');
      else tags.push('<span class="tag ok">token</span>');
      if (row.token_refreshed) tags.push('<span class="tag ok">已续期</span>');
      (row.flags || []).forEach(f => tags.push(`<span class="tag warn">${f}</span>`));
      if (row.email_verified === true) tags.push('<span class="tag ok">已验证</span>');
      if (row.email_verified === false) tags.push('<span class="tag warn">未验证</span>');
      if (row.typeform_pending) tags.push('<span class="tag warn">问卷未完成</span>');
      const d = row.details || {};
      const expUnix = d.expires_at_unix;
      if (expUnix != null && Math.floor(Date.now()/1000) >= expUnix)
        tags.push('<span class="tag bad">试用到期</span>');
      if (d.is_trial === true) tags.push('<span class="tag">试用</span>');
      if (d.stale) tags.push(`<span class="tag warn" title="${esc(d.error || "缓存数据")}">流量缓存</span>`);
      if (d.ok === false && d.error) tags.push(`<span class="tag bad" title="${esc(d.error)}">详情不可用</span>`);
      return tags.join(" ");
    }

    function renderTable(list) {
      accountsCache = list || [];
      $("accCount").textContent = `(${accountsCache.length})`;
      const body = $("accBody");
      if (!accountsCache.length) {
        body.innerHTML = '<tr><td colspan="8" class="empty">暂无账号。请先注册或检查 keys/proxyscrape_accounts.txt</td></tr>';
        return;
      }
      body.innerHTML = accountsCache.map((row, i) => {
        const d = row.details || {};
        const expUnix = d.expires_at_unix;
        const countdown = expUnix != null
          ? formatCountdownClient(expUnix)
          : (d.expires_countdown || "—");
        const bjExpiry = expUnix != null ? formatBeijingExpiry(expUnix) : "";
        const absHint = bjExpiry
          ? ` title="到期: ${esc(bjExpiry)}"`
          : (d.expires_at_display && d.expires_at_display !== "—"
            ? ` title="到期: ${esc(d.expires_at_display)}"` : "");
        const bw = d.bandwidth_remaining_display || "—";
        let bwHint = "";
        if (d.bandwidth_used_display && d.bandwidth_total_display) {
          bwHint = ` title="已用 ${d.bandwidth_used_display} / 总量 ${d.bandwidth_total_display}${d.stale ? "（缓存，可能不是最新）" : ""}"`;
        } else if (row.token_expired) {
          bwHint = ` title="token 已过期，请点「刷新」自动重登后拉取最新流量"`;
        }
        const countryCount = d.country_count != null
          ? d.country_count
          : (d.countries_display && d.countries_display !== "—" ? d.countries_display : "—");
        const countryBd = formatCountriesBreakdown(d);
        const countryHint = countryBd ? ` title="${esc(countryBd)}"` : "";
        const cdClass = (countdown === "已过期") ? "tag bad" : "mono";
        return `<tr data-i="${i}">
          <td class="mono">${esc(row.email || "")}</td>
          <td${absHint}><span class="countdown ${cdClass}" data-expires-unix="${expUnix != null ? expUnix : ""}">${esc(countdown)}</span></td>
          <td${bwHint}>${esc(bw)}</td>
          <td>${d.proxy_amount != null ? d.proxy_amount : "—"}</td>
          <td${countryHint}>${esc(String(countryCount))}</td>
          <td>${esc(d.account_type || "—")}</td>
          <td>${flagTags(row)}</td>
          <td class="row">
            <button type="button" data-act="download" data-i="${i}">下载</button>
            <button type="button" class="secondary" data-act="refresh" data-i="${i}">刷新</button>
            <button type="button" class="danger" data-act="delete" data-i="${i}">删除</button>
          </td>
        </tr>`;
      }).join("");
    }

    function tickCountdowns() {
      document.querySelectorAll(".countdown[data-expires-unix]").forEach(el => {
        const u = el.getAttribute("data-expires-unix");
        if (!u) return;
        const text = formatCountdownClient(u);
        el.textContent = text;
        el.classList.toggle("bad", text === "已过期");
      });
    }

    function esc(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
      })[c]);
    }

    function appendLogLines(lines) {
      const panel = $("logPanel");
      if (!lines || !lines.length) return;
      if (panel.classList.contains("muted") && panel.textContent.includes("等待")) {
        panel.textContent = "";
        panel.classList.remove("muted");
      }
      const frag = document.createDocumentFragment();
      lines.forEach(line => {
        const div = document.createElement("div");
        const t = String(line);
        if (t.includes("[✓]") || t.includes("成功")) div.className = "log-ok";
        else if (t.includes("[-]") || t.includes("ERROR") || t.includes("失败")) div.className = "log-err";
        div.textContent = t;
        frag.appendChild(div);
      });
      panel.appendChild(frag);
      if (logAutoScroll) panel.scrollTop = panel.scrollHeight;
    }

    async function pollLogs() {
      try {
        const data = await api("/api/register/logs?since=" + logCursor);
        if (data.lines && data.lines.length) appendLogLines(data.lines);
        if (typeof data.next === "number") logCursor = data.next;
      } catch (_) {}
    }

    function downloadTextFile(filename, text) {
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    }

    async function downloadAccountProxies(email) {
      try {
        const r = await fetch("/api/accounts/download-proxies?email=" + encodeURIComponent(email));
        if (r.status === 401) {
          location.href = "/login?next=" + encodeURIComponent(location.pathname);
          return;
        }
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.error || j.message || r.statusText);
        }
        const text = await r.text();
        const safe = (email || "account").replace(/[^a-zA-Z0-9@._-]+/g, "_");
        downloadTextFile(`proxies_${safe}.txt`, text);
        appendLogLines([`[✓] 已下载 ${email} 的 proxy（${text.split(/\r?\n/).filter(Boolean).length} 行）`]);
      } catch (e) {
        alert(e.message);
        appendLogLines([`[-] 下载 ${email} proxy 失败: ${e.message}`]);
      }
    }

    async function downloadAllProxies() {
      try {
        const r = await fetch("/api/proxies/download-all");
        if (r.status === 401) {
          location.href = "/login?next=" + encodeURIComponent(location.pathname);
          return;
        }
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.error || j.message || r.statusText);
        }
        const text = await r.text();
        downloadTextFile("proxies_all.txt", text);
        appendLogLines([`[✓] 已下载全部 proxies（${text.split(/\r?\n/).filter(Boolean).length} 行）`]);
      } catch (e) {
        alert(e.message);
        appendLogLines([`[-] 下载全部 proxies 失败: ${e.message}`]);
      }
    }

    function mergePreservedDetails(freshList) {
      // Keep previously refreshed overview details when reloading without live=1
      const prev = {};
      (accountsCache || []).forEach(r => {
        if (r && r.email) prev[r.email.toLowerCase()] = r;
      });
      return (freshList || []).map(row => {
        const old = prev[(row.email || "").toLowerCase()];
        if (!old) return row;
        const oldOk = old.details && old.details.ok;
        const newOk = row.details && row.details.ok;
        if (oldOk && !newOk) {
          return {
            ...row,
            details: old.details,
            email_verified: row.email_verified != null ? row.email_verified : old.email_verified,
            typeform_pending: row.typeform_pending != null ? row.typeform_pending : old.typeform_pending,
            subaccount_id: row.subaccount_id || old.subaccount_id,
            has_token: row.has_token,
            flags: row.flags,
          };
        }
        return row;
      });
    }

    async function deleteAccount(email) {
      if (!confirm(`确定删除账号 ${email} 及其本地相关数据？\n（accounts 行、proxies_*.txt、proxies.txt 中对应代理）`))
        return;
      try {
        const row = accountsCache.find(a => a.email === email) || {};
        const d = row.details || {};
        const data = await api("/api/accounts/delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            email,
            account_id: row.subaccount_id || d.account_id || "",
            proxy_username: d.proxy_username || "",
          }),
        });
        // Do NOT loadList(false) — that would wipe other rows' refreshed details.
        // Remove only this account from the in-memory cache and re-render.
        accountsCache = accountsCache.filter(
          a => (a.email || "").toLowerCase() !== (email || "").toLowerCase()
        );
        renderTable(accountsCache);
        tickCountdowns();
        appendLogLines([
          `[✓] 已删除 ${email} · 账号行 ${data.removed?.account_lines || 0} · ` +
          `代理文件 ${(data.removed?.proxy_files || []).length} · ` +
          `proxies.txt ${data.removed?.proxy_lines || 0} 行`
        ]);
      } catch (e) { alert(e.message); }
    }

    async function deleteAllAccounts() {
      if (!confirm("确定删除全部账号及本地相关数据？\n将清空 accounts、proxies_*.txt、proxies.txt"))
        return;
      if (!confirm("再次确认：此操作不可恢复（仅本地文件）。"))
        return;
      try {
        const data = await api("/api/accounts/delete-all", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ clear_proxies: true }),
        });
        accountsCache = [];
        renderTable([]);
        appendLogLines([
          `[✓] 已全部删除 · 账号行 ${data.removed?.account_lines || 0} · ` +
          `代理文件 ${(data.removed?.proxy_files || []).length} · ` +
          `proxies.txt 已清空: ${data.removed?.proxies_txt_cleared ? "是" : "否"}`
        ]);
      } catch (e) { alert(e.message); }
    }

    async function deleteInvalidAccounts() {
      if (!confirm(
        "删除已过期账号及其本地数据？\n\n" +
        "默认只删除已过期（expired）账号，到期时间未知的账号会保留。\n" +
        "此操作不可恢复（仅本地文件）。"
      )) return;
      const includeUnknown = confirm(
        "是否同时删除「到期时间未知」的账号？\n\n" +
        "确定 = 一并删除（这些账号无法确认是否仍然有效）\n" +
        "取消 = 只删已过期账号"
      );
      const btn = $("btnDeleteInvalid");
      btn.disabled = true;
      try {
        const data = await api("/api/accounts/delete-invalid", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ include_unknown: includeUnknown }),
        });
        const gone = data.deleted || [];
        if (gone.length) {
          const goneSet = new Set(gone.map(e => (e || "").toLowerCase()));
          accountsCache = accountsCache.filter(
            a => !goneSet.has((a.email || "").toLowerCase())
          );
          renderTable(accountsCache);
          tickCountdowns();
        }
        appendLogLines([
          `[✓] 删除失效账号 ${gone.length} 个` +
          `（含 unknown: ${data.include_unknown ? "是" : "否"}）· ` +
          `账号行 ${data.removed?.account_lines || 0} · ` +
          `代理文件 ${data.removed?.proxy_files || 0} · ` +
          `proxies.txt ${data.removed?.proxy_lines || 0} 行`
        ]);
      } catch (e) { alert(e.message); }
      finally { btn.disabled = false; }
    }

    async function loadList(live) {
      const q = live ? "?live=1" : "";
      const data = await api("/api/accounts" + q);
      let list = data.accounts || [];
      // Preserve in-memory details unless this is a full live refresh
      if (!live) list = mergePreservedDetails(list);
      renderTable(list);
      tickCountdowns();
    }

    $("btnReload").onclick = () => loadList(false).catch(e => alert(e.message));
    $("btnRefreshAll").onclick = async () => {
      $("btnRefreshAll").disabled = true;
      try { await loadList(true); } catch (e) { alert(e.message); }
      $("btnRefreshAll").disabled = false;
    };
    $("btnDeleteAll").onclick = () => deleteAllAccounts();
    $("btnDeleteInvalid").onclick = () => deleteInvalidAccounts();
    $("btnDownloadAll").onclick = () => downloadAllProxies();
    $("btnLogout").onclick = async () => {
      try {
        await api("/api/auth/logout", { method: "POST" });
      } catch (_) {}
      location.href = "/login";
    };
    $("btnClearLog").onclick = async () => {
      try {
        await api("/api/register/logs/clear", { method: "POST" });
        logCursor = 0;
        $("logPanel").textContent = "日志已清空。";
        $("logPanel").classList.add("muted");
      } catch (e) { alert(e.message); }
    };
    $("btnScrollLog").onclick = () => {
      logAutoScroll = true;
      const panel = $("logPanel");
      panel.scrollTop = panel.scrollHeight;
    };
    $("logPanel").addEventListener("scroll", () => {
      const panel = $("logPanel");
      const nearBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 40;
      logAutoScroll = nearBottom;
    });

    $("accBody").onclick = async (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (!btn) return;
      const i = +btn.dataset.i;
      const row = accountsCache[i];
      if (!row) return;
      if (btn.dataset.act === "download") {
        btn.disabled = true;
        try { await downloadAccountProxies(row.email); }
        finally { btn.disabled = false; }
        return;
      }
      if (btn.dataset.act === "delete") {
        await deleteAccount(row.email);
        return;
      }
      if (btn.dataset.act === "refresh") {
        btn.disabled = true;
        try {
          const data = await api("/api/accounts/refresh", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ email: row.email }),
          });
          if (data.account) {
            accountsCache[i] = data.account;
            renderTable(accountsCache);
          }
        } catch (e) { alert(e.message); }
        btn.disabled = false;
      }
    };

    async function pollReg() {
      try {
        const s = await api("/api/register/status");
        $("regStatus").textContent =
          `注册状态: ${s.running ? "运行中" : "空闲"} · ${s.message || ""}` +
          (s.running ? ` · CLI success=${s.cli_success}/${s.cli_target}` : "");
        $("btnRegister").disabled = !!s.running;
        $("btnStop").disabled = !s.running;
      } catch (_) {}
    }

    $("btnRegister").onclick = async () => {
      const count = Math.max(1, parseInt($("regCount").value || "1", 10));
      try {
        const r = await api("/api/register", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ count }),
        });
        $("regStatus").textContent = r.message || "已启动";
        pollReg();
        pollLogs();
      } catch (e) { alert(e.message); }
    };
    $("btnStop").onclick = async () => {
      try { await api("/api/register/stop", { method: "POST" }); pollReg(); }
      catch (e) { alert(e.message); }
    };

    loadList(false).catch(e => {
      $("accBody").innerHTML = `<tr><td colspan="8" class="empty">${esc(e.message)}</td></tr>`;
    });
    setInterval(pollReg, 2000);
    setInterval(pollLogs, 1000);
    setInterval(tickCountdowns, 1000);
    pollReg();
    pollLogs();
  </script>
</body>
</html>
"""


@app.get("/")
async def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>登录 · ProxyScrape 管理台</title>
  <style>
    :root {
      --bg: #0f1419; --panel: #1a2332; --border: #2d3a4d;
      --text: #e7ecf3; --muted: #8b9bb4; --accent: #3b82f6; --bad: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.45;
      padding: 1rem;
    }
    .card {
      width: 100%; max-width: 400px;
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 12px; padding: 1.5rem 1.6rem;
    }
    h1 { margin: 0 0 0.35rem; font-size: 1.2rem; }
    .sub { color: var(--muted); font-size: 0.85rem; margin-bottom: 1.25rem; }
    label { display: block; font-size: 0.85rem; color: var(--muted); margin-bottom: 0.35rem; }
    input[type=password], input[type=text] {
      width: 100%; padding: 0.55rem 0.7rem; border-radius: 6px;
      border: 1px solid var(--border); background: var(--bg); color: var(--text);
      font: inherit; margin-bottom: 1rem;
    }
    button {
      width: 100%; cursor: pointer; border: none; border-radius: 6px;
      padding: 0.55rem 1rem; font-weight: 600; font-size: 0.9rem;
      background: var(--accent); color: #fff;
    }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .err { color: var(--bad); font-size: 0.85rem; min-height: 1.2em; margin-top: 0.75rem; }
    .hint { color: var(--muted); font-size: 0.75rem; margin-top: 1rem; }
  </style>
</head>
<body>
  <div class="card">
    <h1>ProxyScrape 管理台</h1>
    <div class="sub">请输入管理密码后继续</div>
    <form id="loginForm" autocomplete="on">
      <label for="password">管理密码</label>
      <input type="password" id="password" name="password" autocomplete="current-password" autofocus required />
      <button type="submit" id="btnLogin">登录</button>
      <div class="err" id="errLine"></div>
    </form>
    <p class="hint">密码在 .env 的 <code>WEB_PASSWORD</code>（默认 <code>admin</code>），可在登录后于配置页修改。</p>
  </div>
  <script>
    const params = new URLSearchParams(location.search);
    const next = params.get("next") || "/";
    const form = document.getElementById("loginForm");
    const errLine = document.getElementById("errLine");
    const btn = document.getElementById("btnLogin");
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      errLine.textContent = "";
      btn.disabled = true;
      try {
        const r = await fetch("/api/auth/login", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ password: document.getElementById("password").value }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error || j.message || "登录失败");
        location.href = next.startsWith("/") ? next : "/";
      } catch (e) {
        errLine.textContent = e.message || "登录失败";
      } finally {
        btn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


CONFIG_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>配置 · ProxyScrape</title>
  <style>
    :root {
      --bg: #0f1419; --panel: #1a2332; --border: #2d3a4d;
      --text: #e7ecf3; --muted: #8b9bb4; --accent: #3b82f6;
      --ok: #22c55e; --bad: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.45;
    }
    header {
      padding: 1rem 1.5rem; border-bottom: 1px solid var(--border);
      background: var(--panel); display: flex; flex-wrap: wrap;
      align-items: center; gap: 1rem; justify-content: space-between;
    }
    header h1 { margin: 0; font-size: 1.15rem; }
    header a { color: var(--accent); text-decoration: none; font-weight: 600; }
    main { padding: 1.25rem 1.5rem; max-width: 900px; margin: 0 auto; }
    .card {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 1rem;
    }
    .card h2 {
      margin: 0 0 0.85rem; font-size: 0.95rem; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.04em;
    }
    .field { margin-bottom: 0.85rem; }
    .field label {
      display: block; font-size: 0.85rem; margin-bottom: 0.3rem; font-weight: 600;
    }
    .field .help { color: var(--muted); font-size: 0.75rem; margin-top: 0.25rem; }
    input[type=text], input[type=number], select, textarea {
      width: 100%; padding: 0.5rem 0.65rem; border-radius: 6px;
      border: 1px solid var(--border); background: var(--bg); color: var(--text);
      font: inherit;
    }
    textarea { min-height: 4.5rem; resize: vertical; font-family: ui-monospace, Consolas, monospace; font-size: 0.85rem; }
    .row { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; }
    button {
      cursor: pointer; border: none; border-radius: 6px;
      padding: 0.5rem 1rem; font-weight: 600; font-size: 0.875rem;
      background: var(--accent); color: #fff;
    }
    button.secondary { background: #334155; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .status { font-size: 0.85rem; color: var(--muted); min-height: 1.2em; margin-top: 0.5rem; }
    .status.ok { color: var(--ok); }
    .status.err { color: var(--bad); }
    .path { font-family: ui-monospace, Consolas, monospace; font-size: 0.8rem; color: var(--muted); }
    .secret-hint { color: var(--warn, #f59e0b); font-size: 0.75rem; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>配置</h1>
      <div class="path" id="envPath">加载中…</div>
    </div>
    <div class="row">
      <a href="/">← 返回管理台</a>
      <button type="button" class="secondary" id="btnReload">重新加载</button>
      <button type="button" id="btnSave">保存到 .env</button>
      <button type="button" class="secondary" id="btnLogout">退出</button>
    </div>
  </header>
  <main>
    <p class="status" id="statusLine">保存后写入项目 .env，并热更新当前进程（WEB_HOST/PORT 需重启）。登录后全部字段明文显示。</p>
    <div id="formRoot"></div>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let schema = [];
    let values = {};
    let secretsSet = {};

    async function api(path, opts) {
      const r = await fetch(path, opts);
      if (r.status === 401) {
        location.href = "/login?next=" + encodeURIComponent(location.pathname);
        throw new Error("未登录");
      }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || j.message || r.statusText);
      return j;
    }

    function esc(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
      })[c]);
    }

    function renderForm() {
      const root = $("formRoot");
      // Login-gated page: show every field in plaintext (including former password types).
      root.innerHTML = schema.map((g) => {
        const fields = g.keys.map((item) => {
          const k = item.key;
          const val = values[k] != null ? values[k] : (item.default || "");
          const help = item.help ? `<div class="help">${esc(item.help)}</div>` : "";
          const secretNote = (item.secret || item.type === "password")
            ? `<div class="help secret-hint">敏感字段 · 明文显示（已登录）</div>`
            : "";
          let control = "";
          if (item.type === "select") {
            const opts = (item.options || []).map(o =>
              `<option value="${esc(o)}" ${String(val)===String(o)?"selected":""}>${esc(o)}</option>`
            ).join("");
            control = `<select data-key="${esc(k)}">${opts}</select>`;
          } else if (item.type === "textarea") {
            control = `<textarea data-key="${esc(k)}" rows="3">${esc(val)}</textarea>`;
          } else if (item.type === "number") {
            control = `<input type="number" data-key="${esc(k)}" value="${esc(val)}" />`;
          } else {
            // text / password / secret → always plain text input after auth
            control = `<input type="text" data-key="${esc(k)}" value="${esc(val)}" autocomplete="off" spellcheck="false" />`;
          }
          return `<div class="field">
            <label for="">${esc(item.label || k)} <span class="help" style="font-weight:400">(${esc(k)})</span></label>
            ${control}${help}${secretNote}
          </div>`;
        }).join("");
        return `<section class="card"><h2>${esc(g.group)}</h2>${fields}</section>`;
      }).join("");
    }

    function collectUpdates() {
      const updates = {};
      document.querySelectorAll("[data-key]").forEach(el => {
        const k = el.getAttribute("data-key");
        updates[k] = el.value;
      });
      return updates;
    }

    async function loadConfig() {
      $("statusLine").textContent = "加载中…";
      $("statusLine").className = "status";
      const data = await api("/api/config");
      schema = data.schema || [];
      values = data.values || {};
      secretsSet = data.secrets_set || {};
      $("envPath").textContent = (data.exists ? "文件: " : "将创建: ") + (data.env_path || ".env");
      renderForm();
      $("statusLine").textContent = "已加载配置（全部明文）。修改后点「保存到 .env」。";
    }

    $("btnReload").onclick = () => loadConfig().catch(e => {
      $("statusLine").textContent = e.message;
      $("statusLine").className = "status err";
    });
    $("btnLogout").onclick = async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
      location.href = "/login";
    };
    $("btnSave").onclick = async () => {
      $("btnSave").disabled = true;
      try {
        const updates = collectUpdates();
        const data = await api("/api/config", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ values: updates }),
        });
        $("statusLine").textContent =
          `已保存 ${ (data.updated || []).length } 项到 .env` +
          (data.reloaded ? ` · 已热更新: ${data.reloaded.join(", ")}` : "") +
          (data.restart_hint ? ` · ${data.restart_hint}` : "");
        $("statusLine").className = "status ok";
        await loadConfig();
      } catch (e) {
        $("statusLine").textContent = e.message;
        $("statusLine").className = "status err";
      } finally {
        $("btnSave").disabled = false;
      }
    };

    loadConfig().catch(e => {
      $("statusLine").textContent = e.message;
      $("statusLine").className = "status err";
    });
  </script>
</body>
</html>
"""


@app.get("/login")
async def login_page():
    if _is_authed():
        nxt = request.args.get("next") or "/"
        if not nxt.startswith("/"):
            nxt = "/"
        return redirect(nxt)
    return Response(LOGIN_HTML, mimetype="text/html; charset=utf-8")


@app.post("/api/auth/login")
async def api_auth_login():
    body = await request.get_json(force=True, silent=True) or {}
    password = body.get("password")
    if password is None:
        password = ""
    if not _verify_password(str(password)):
        return jsonify({"ok": False, "error": "密码错误"}), 401
    session.permanent = True
    session["web_auth"] = True
    session["web_auth_at"] = time.time()
    return jsonify({"ok": True, "message": "登录成功"})


@app.post("/api/auth/logout")
async def api_auth_logout():
    session.clear()
    return jsonify({"ok": True, "message": "已退出"})


@app.get("/api/auth/status")
async def api_auth_status():
    return jsonify({"ok": True, "authenticated": _is_authed()})


@app.get("/config")
async def config_page():
    return Response(CONFIG_HTML, mimetype="text/html; charset=utf-8")


@app.get("/api/config")
async def api_config_get():
    # Authenticated only (before_request). Return full plaintext values.
    data = get_config_for_ui(DEFAULT_ENV_PATH, include_secrets=True)
    data["ok"] = True
    return jsonify(data)


@app.post("/api/config")
async def api_config_save():
    body = await request.get_json(force=True, silent=True) or {}
    values = body.get("values") or body
    if not isinstance(values, dict):
        return jsonify({"ok": False, "error": "values must be object"}), 400

    allow = set(all_config_keys())
    updates = {}
    for k, v in values.items():
        if k not in allow:
            continue
        if v is None:
            continue
        updates[k] = str(v)

    if not updates:
        return jsonify({"ok": False, "error": "没有可保存的变更"}), 400

    result = upsert_env_file(updates, path=DEFAULT_ENV_PATH, keys_allowlist=list(allow))
    apply_updates_to_environ(updates)
    reloaded = reload_main_module_config(reg)

    restart_hint = None
    if "WEB_HOST" in updates or "WEB_PORT" in updates:
        restart_hint = "WEB_HOST/WEB_PORT 修改后请重启 web_app.py 生效"

    return jsonify(
        {
            "ok": True,
            "updated": result.get("updated") or list(updates.keys()),
            "path": result.get("path"),
            "created": result.get("created"),
            "reloaded": reloaded,
            "restart_hint": restart_hint,
        }
    )


@app.get("/api/health")
async def health():
    return jsonify(
        {
            "ok": True,
            "service": "ProxyScrape Web 管理",
            "accounts_file": ACCOUNTS_FILE,
            "register_running": _register_job["running"],
            "config_page": "/config",
        }
    )


@app.get("/api/accounts")
async def api_accounts():
    live = request.args.get("live", "").lower() in ("1", "true", "yes")
    accounts = load_accounts_from_file()
    cache = load_account_details_cache()
    # live=all can be slow; allow ?live=1 for full refresh
    rows = []
    for acc in accounts:
        if live:
            rows.append(_enrich_account(acc, live=True))
        else:
            row = merge_account_with_details(acc)
            row = apply_cached_details_to_row(row, cache)
            rows.append(row)
    return jsonify(
        {
            "ok": True,
            "count": len(rows),
            "accounts": rows,
            "live": live,
            "details_from_cache": not live,
        }
    )


@app.post("/api/accounts/refresh")
async def api_accounts_refresh():
    body = await request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    accounts = load_accounts_from_file()
    acc = next((a for a in accounts if a["email"].lower() == email), None)
    if not acc:
        return jsonify({"ok": False, "error": "account not found"}), 404
    row = _enrich_account(acc, live=True)
    return jsonify({"ok": True, "account": row})


@app.post("/api/accounts/delete")
async def api_accounts_delete():
    """Delete one account and related local files/proxy lines."""
    body = await request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    account_id = (body.get("account_id") or body.get("accountId") or "").strip()
    proxy_username = (body.get("proxy_username") or "").strip()
    # If ids missing, try a live refresh first (best-effort)
    if not account_id or not proxy_username:
        accounts = load_accounts_from_file()
        acc = next(
            (a for a in accounts if a["email"].lower() == email.lower()), None
        )
        if acc and acc.get("access_token"):
            try:
                row = _enrich_account(acc, live=True)
                d = row.get("details") or {}
                account_id = account_id or row.get("subaccount_id") or d.get(
                    "account_id"
                ) or ""
                proxy_username = proxy_username or d.get("proxy_username") or ""
            except Exception:
                pass
    result = delete_account_local_data(
        email,
        account_id=account_id or "",
        proxy_username=proxy_username or "",
    )
    if not result.get("ok"):
        return jsonify(result), 400
    remove_account_details_cache(email)
    return jsonify(result)


@app.post("/api/accounts/delete-all")
async def api_accounts_delete_all():
    """Delete all accounts and related local proxy files."""
    body = await request.get_json(force=True, silent=True) or {}
    clear_proxies = body.get("clear_proxies", True)
    if isinstance(clear_proxies, str):
        clear_proxies = clear_proxies.lower() not in ("0", "false", "no")
    result = delete_all_accounts_local_data(clear_proxies_txt=bool(clear_proxies))
    clear_account_details_cache()
    return jsonify(result)


@app.post("/api/accounts/delete-invalid")
async def api_accounts_delete_invalid():
    """
    Delete accounts whose cached expiry says they are dead (manual action only —
    the feed never deletes anything).

    body: { "include_unknown": false }
      default: only state == "expired"
      include_unknown: also drop accounts with no usable expiry (kept by default,
      because "unknown" is treated as valid by the feed).
    """
    body = await request.get_json(force=True, silent=True) or {}
    include_unknown = body.get("include_unknown", False)
    if isinstance(include_unknown, str):
        include_unknown = include_unknown.lower() not in ("0", "false", "no")

    wanted = {"expired"}
    if include_unknown:
        wanted.add("unknown")

    accounts = load_accounts_from_file()
    cache = load_account_details_cache()
    deleted: list = []
    removed = {"account_lines": 0, "proxy_files": 0, "proxy_lines": 0}

    for acc in accounts:
        email = (acc.get("email") or "").strip()
        if not email:
            continue
        entry = cache.get(email.lower())
        if account_expiry_state(entry) not in wanted:
            continue
        details = entry.get("details") if isinstance(entry, dict) else {}
        details = details if isinstance(details, dict) else {}
        result = delete_account_local_data(
            email,
            account_id=resolve_cached_account_id(entry),
            proxy_username=details.get("proxy_username") or "",
        )
        if not result.get("ok"):
            continue
        remove_account_details_cache(email)
        deleted.append(email)
        rm = result.get("removed") or {}
        removed["account_lines"] += rm.get("account_lines") or 0
        removed["proxy_files"] += len(rm.get("proxy_files") or [])
        removed["proxy_lines"] += rm.get("proxy_lines") or 0

    if deleted:
        _append_log(
            f"[✓] 删除失效账号 {len(deleted)} 个 "
            f"(include_unknown={bool(include_unknown)})"
        )
    return jsonify(
        {
            "ok": True,
            "deleted": deleted,
            "count": len(deleted),
            "include_unknown": bool(include_unknown),
            "removed": removed,
        }
    )


@app.post("/api/register")
async def api_register():
    body = await request.get_json(force=True, silent=True) or {}
    try:
        count = max(1, int(body.get("count") or 1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid count"}), 400

    with _register_lock:
        if _register_job["running"]:
            return jsonify({"ok": False, "error": "注册任务已在运行"}), 409
        _register_job["running"] = True
        _register_job["requested"] = count
        _register_job["started_at"] = time.time()
        _register_job["finished_at"] = None
        _register_job["message"] = f"starting count={count}"

    t = threading.Thread(target=_run_register_job, args=(count,), daemon=True)
    t.start()
    return jsonify(
        {
            "ok": True,
            "message": f"已启动注册 {count} 个账号（与 CLI 相同路径）",
            "count": count,
        }
    )


@app.post("/api/register/stop")
async def api_register_stop():
    reg.stop_flag = True
    _register_job["message"] = "停止请求已发送"
    _append_log("[!] 用户请求停止注册")
    return jsonify({"ok": True, "message": "stop_flag set"})


@app.get("/api/register/status")
async def api_register_status():
    return jsonify(
        {
            "ok": True,
            "running": _register_job["running"],
            "requested": _register_job["requested"],
            "message": _register_job["message"],
            "started_at": _register_job["started_at"],
            "finished_at": _register_job["finished_at"],
            "cli_success": getattr(reg, "success_count", 0),
            "cli_target": getattr(reg, "target_count", 0),
        }
    )


@app.get("/api/register/logs")
async def api_register_logs():
    since = request.args.get("since", "0")
    try:
        since_i = int(since)
    except ValueError:
        since_i = 0
    data = _get_logs(since_i)
    data["ok"] = True
    return jsonify(data)


@app.post("/api/register/logs/clear")
async def api_register_logs_clear():
    _clear_logs()
    return jsonify({"ok": True})


@app.get("/api/accounts/download-proxies")
async def api_download_account_proxies():
    """Download live proxies for one account as text/plain file."""
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    accounts = load_accounts_from_file()
    acc = next((a for a in accounts if a["email"].lower() == email.lower()), None)
    if not acc:
        return jsonify({"ok": False, "error": "account not found"}), 404
    result = _download_proxies_for_account(acc)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error") or "download failed"}), 400
    body = "\n".join(result["lines"]) + ("\n" if result["lines"] else "")
    safe = re.sub(r"[^a-zA-Z0-9@._-]+", "_", email)[:80]
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="proxies_{safe}.txt"',
        },
    )


def _collect_all_proxy_lines(live: bool = True) -> dict:
    """
    Gather proxy lines from live accounts and/or local proxies.txt.
    Returns {lines, source, errors}.
    """
    lines: list = []
    errors: list = []
    source = "file"

    if live:
        accounts = load_accounts_from_file()
        for acc in accounts:
            if not acc.get("access_token"):
                errors.append(f"{acc.get('email')}: no token")
                continue
            result = _download_proxies_for_account(acc)
            if result.get("ok") and result.get("lines"):
                lines.extend(result["lines"])
            else:
                errors.append(
                    f"{acc.get('email')}: {result.get('error') or 'fail'}"
                )
        if lines:
            source = "live"

    if not lines:
        content = load_proxies_file_content(PROXIES_FILE)
        file_lines = [
            ln.strip()
            for ln in (content or "").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if file_lines:
            lines = file_lines
            source = "file"

    # de-dupe preserve order
    seen = set()
    unique = []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            unique.append(ln)
    return {"lines": unique, "source": source, "errors": errors}


@app.get("/api/proxies/download-all")
async def api_download_all_proxies():
    """
    One combined proxies file.
    Prefer live download of all accounts with tokens; fallback to keys/proxies.txt.
    """
    live = request.args.get("live", "1").lower() not in ("0", "false", "no")
    collected = _collect_all_proxy_lines(live=live)
    unique = collected["lines"]
    errors = collected["errors"]
    source = collected["source"]

    if not unique:
        msg = "无可用代理"
        if errors:
            msg += ": " + "; ".join(errors[:5])
        return jsonify({"ok": False, "error": msg, "errors": errors}), 404

    body = "\n".join(unique) + "\n"
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="proxies_all.txt"',
            "X-Proxy-Source": source,
            "X-Proxy-Count": str(len(unique)),
            "X-Proxy-Errors": "; ".join(errors)[:500] if errors else "",
        },
    )


@app.get("/api/feed/proxies")
async def api_feed_proxies():
    """
    Read-only proxy feed for remote pullers (e.g. a subscription client).

    Auth: FEED_TOKEN via `X-Feed-Token` header or `?token=` query param.
      - token unset  -> 503 (feed disabled; never serve an open feed)
      - token wrong  -> 401
    Data: local per-account files only (collect_valid_proxy_lines). No network
    calls, no re-login, no side effects. Empty result is 200 + empty body.
    """
    expected = _feed_token()
    if not expected:
        return Response(
            "feed 未开启\n",
            status=503,
            mimetype="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    provided = (request.headers.get("X-Feed-Token") or "").strip()
    if not provided:
        provided = (request.args.get("token") or "").strip()
    try:
        token_ok = hmac.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        )
    except (TypeError, ValueError):
        token_ok = False
    if not token_ok:
        return Response(
            "unauthorized\n",
            status=401,
            mimetype="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    try:
        snap = collect_valid_proxy_lines()
    except Exception as e:
        _append_log(f"[-] feed 收集失败: {str(e)[:160]}")
        return Response(
            f"feed error: {str(e)[:160]}\n",
            status=500,
            mimetype="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    lines = snap["lines"]
    errors = [
        f"{row['email']}: {row['reason']}"
        for row in snap["accounts"]
        if row.get("reason") and row.get("reason") != "expired"
    ]
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        body,
        status=200,
        mimetype="text/plain",
        headers={
            "Cache-Control": "no-store",
            "X-Proxy-Count": str(len(lines)),
            "X-Proxy-Accounts": str(snap["feed_eligible_count"]),
            "X-Proxy-Errors": "; ".join(errors)[:500] if errors else "",
        },
    )


def main():
    pwd = _web_password()
    print("=" * 60)
    print("ProxyScrape Web 管理台")
    print(f"打开: http://{HOST}:{PORT}/")
    print(f"登录: /login  （WEB_PASSWORD={'(默认 admin)' if pwd == DEFAULT_WEB_PASSWORD else '已配置'}）")
    print(f"账号文件: {ACCOUNTS_FILE}")
    print(
        "API: /api/accounts  /api/register  /api/register/logs  "
        "/api/proxies/download-all  /api/feed/proxies"
    )
    cfg = _auto_register_config()
    print(
        f"[*] 自动补齐: enabled={cfg['enabled']} interval={cfg['interval']}s "
        f"min_valid={cfg['min_valid']} target={cfg['target']} "
        f"max_per_round={cfg['max_per_round']}"
    )
    _start_auto_register_scheduler()
    print("=" * 60)
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
