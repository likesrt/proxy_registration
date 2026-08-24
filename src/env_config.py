"""
.env read/write helpers for Web 配置页.

- Preserves comments and unknown keys where possible
- Upserts known keys without rewriting unrelated content blindly
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

# Project root .env (relative to cwd when running web_app/main)
DEFAULT_ENV_PATH = ".env"

# Fields editable from Web UI (covers project .env)
# secret=True: 标记敏感配置（登录鉴权后配置页仍以明文展示，便于核对）
# type 可为 text/password/number/select/textarea；Web 配置页统一明文输入
CONFIG_SCHEMA = [
    {
        "group": "邮箱 · Cloudflare",
        "keys": [
            {
                "key": "EMAIL_SERVICE_TYPE",
                "label": "邮箱服务类型",
                "type": "select",
                "options": ["cloudflare", "gptmail"],
                "default": "cloudflare",
                "help": "cloudflare 或 gptmail",
            },
            {
                "key": "WORKER_DOMAIN",
                "label": "Worker 域名",
                "type": "text",
                "default": "",
                "secret": True,
                "help": "Cloudflare 临时邮箱 Worker 域名",
            },
            {
                "key": "ADMIN_PASSWORD",
                "label": "Admin 密码",
                "type": "password",
                "default": "",
                "secret": True,
                "help": "Worker x-admin-auth",
            },
            {
                "key": "EMAIL_DOMAIN",
                "label": "邮箱域名列表",
                "type": "textarea",
                "default": "",
                "help": "多个域名逗号分隔，注册时轮换",
            },
        ],
    },
    {
        "group": "邮箱 · GPTMail",
        "keys": [
            {
                "key": "GPTMAIL_DOMAIN",
                "label": "GPTMail 域名",
                "type": "text",
                "default": "",
            },
            {
                "key": "GPTMAIL_API_KEY",
                "label": "GPTMail API Key",
                "type": "password",
                "default": "",
                "secret": True,
            },
        ],
    },
    {
        "group": "注册",
        "keys": [
            {
                "key": "REGISTER_PASSWORD",
                "label": "注册密码",
                "type": "text",
                "default": "random",
                "secret": True,
                "help": "固定密码，或 random 每次随机",
            },
            {
                "key": "REGISTER_INTERVAL",
                "label": "注册间隔(秒)",
                "type": "number",
                "default": "0",
            },
            {
                "key": "REGISTER_COUNT",
                "label": "默认注册数量",
                "type": "number",
                "default": "1",
                "help": "CLI/非交互默认数量；Web 以页面输入为准",
            },
            {
                "key": "MAX_CAPTCHA_FAIL_ROUNDS",
                "label": "验证码失败轮数上限",
                "type": "number",
                "default": "3",
            },
            {
                "key": "PROXY",
                "label": "注册用 HTTP 代理",
                "type": "text",
                "default": "",
                "secret": True,
                "help": "可选，给 curl_cffi 会话",
            },
            {
                "key": "PROXY_DOWNLOAD_PROTOCOL",
                "label": "代理下载协议",
                "type": "select",
                "options": ["http"],
                "default": "http",
            },
        ],
    },
    {
        "group": "Turnstile Solver",
        "keys": [
            {
                "key": "TURNSTILE_SOLVER_URL",
                "label": "Solver URL",
                "type": "text",
                "default": "http://127.0.0.1:5072",
                "secret": True,
                "help": "本机或远程 api_solver 地址",
            },
        ],
    },
    {
        "group": "Resin 上传",
        "keys": [
            {
                "key": "RESIN_SUBSCRIPTION_URL",
                "label": "订阅 URL",
                "type": "text",
                "default": "",
            },
            {
                "key": "RESIN_API_TOKEN",
                "label": "API Token",
                "type": "password",
                "default": "",
                "secret": True,
            },
            {
                "key": "RESIN_NAME",
                "label": "订阅名称",
                "type": "text",
                "default": "proxyscrape",
            },
            {
                "key": "RESIN_UPDATE_INTERVAL",
                "label": "更新间隔",
                "type": "text",
                "default": "12h",
            },
            {
                "key": "RESIN_EPHEMERAL_NODE_EVICT_DELAY",
                "label": "节点驱逐延迟",
                "type": "text",
                "default": "72h0m0s",
            },
            {
                "key": "RESIN_ENABLED",
                "label": "Enabled",
                "type": "select",
                "options": ["true", "false"],
                "default": "true",
            },
            {
                "key": "RESIN_EPHEMERAL",
                "label": "Ephemeral",
                "type": "select",
                "options": ["true", "false"],
                "default": "false",
            },
            {
                "key": "RESIN_INCREMENTAL_ALIVE_NODES",
                "label": "Incremental alive nodes",
                "type": "select",
                "options": ["true", "false"],
                "default": "false",
            },
        ],
    },
    {
        "group": "Web 服务",
        "keys": [
            {
                "key": "WEB_HOST",
                "label": "监听地址",
                "type": "text",
                "default": "127.0.0.1",
                "help": "修改后需重启 web_app.py",
            },
            {
                "key": "WEB_PORT",
                "label": "端口",
                "type": "number",
                "default": "5080",
                "help": "修改后需重启 web_app.py",
            },
            {
                "key": "WEB_PASSWORD",
                "label": "管理台登录密码",
                "type": "text",
                "default": "admin",
                "secret": True,
                "help": "访问 Web 管理台 / 配置页的登录密码；修改后立即生效，下次登录用新密码",
            },
        ],
    },
]


def all_config_keys() -> list:
    keys = []
    for g in CONFIG_SCHEMA:
        for item in g["keys"]:
            keys.append(item["key"])
    return keys


def config_schema_public() -> list:
    """Schema for frontend (no secrets content)."""
    return CONFIG_SCHEMA


_ENV_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)


def _strip_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    # inline comment for unquoted
    if " #" in v and not (v.startswith('"') or v.startswith("'")):
        v = v.split(" #", 1)[0].rstrip()
    return v


def parse_env_file(path: str = DEFAULT_ENV_PATH) -> dict:
    """Parse .env into {key: value} (last assignment wins)."""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = _ENV_LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            out[m.group(1)] = _strip_value(m.group(2))
    return out


def _format_env_value(value: str) -> str:
    """Quote value if needed for .env safety."""
    if value is None:
        return ""
    s = str(value)
    if s == "":
        return ""
    if re.search(r'[\s#"\'\\]', s) or s.startswith("#"):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def upsert_env_file(
    updates: dict,
    path: str = DEFAULT_ENV_PATH,
    keys_allowlist: Optional[list] = None,
) -> dict:
    """
    Update keys in .env file. Preserves comments and other keys.
    Empty string values are written as KEY=
    Returns {path, updated: [keys], created: bool}
    """
    allow = set(keys_allowlist or all_config_keys())
    clean = {}
    for k, v in (updates or {}).items():
        if k not in allow:
            continue
        if v is None:
            continue
        clean[str(k)] = str(v)

    created = not os.path.isfile(path)
    lines: list = []
    if not created:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()

    seen = set()
    new_lines = []
    for line in lines:
        m = _ENV_LINE_RE.match(line)
        if not m:
            new_lines.append(line)
            continue
        key = m.group(1)
        if key in clean:
            new_lines.append(f"{key}={_format_env_value(clean[key])}")
            seen.add(key)
        else:
            new_lines.append(line)

    # append missing keys
    pending = [k for k in clean if k not in seen]
    if pending:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# Updated via Web 配置页")
        for k in pending:
            new_lines.append(f"{k}={_format_env_value(clean[k])}")
            seen.add(k)

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(new_lines))
        if new_lines:
            f.write("\n")

    return {
        "path": path,
        "updated": sorted(seen),
        "created": created,
    }


def get_config_for_ui(
    path: str = DEFAULT_ENV_PATH,
    include_secrets: bool = True,
) -> dict:
    """
    Build config payload for Web UI: schema + current values + os.environ overlay.
    File values override defaults; process env overrides file (runtime).
    """
    file_vals = parse_env_file(path)
    values = {}
    secrets_set = {}
    for g in CONFIG_SCHEMA:
        for item in g["keys"]:
            k = item["key"]
            # priority: os.environ (if set) > file > default
            if k in os.environ and os.environ.get(k) is not None:
                val = os.environ.get(k, "")
            elif k in file_vals:
                val = file_vals[k]
            else:
                val = item.get("default", "")
            is_secret = bool(item.get("secret") or item.get("type") == "password")
            secrets_set[k] = is_secret and bool(val)
            # Mask secrets only when explicitly requested (e.g. unauthenticated paths).
            # Authenticated config page uses include_secrets=True and shows plaintext.
            if is_secret and not include_secrets:
                values[k] = ""
            else:
                values[k] = val
    return {
        "schema": CONFIG_SCHEMA,
        "values": values,
        "secrets_set": secrets_set,
        "env_path": os.path.abspath(path),
        "exists": os.path.isfile(path),
    }


def apply_updates_to_environ(updates: dict) -> None:
    """Write updates into os.environ for current process."""
    for k, v in (updates or {}).items():
        if v is None:
            continue
        os.environ[str(k)] = str(v)


def reload_main_module_config(main_mod) -> list:
    """
    Re-read env into main.py module globals used at runtime.
    Returns list of reloaded attribute names.
    """
    from dotenv import load_dotenv

    load_dotenv(override=True)
    reloaded = []

    def setg(name, value):
        setattr(main_mod, name, value)
        reloaded.append(name)

    setg(
        "REGISTER_PASSWORD",
        os.getenv("REGISTER_PASSWORD", "random").strip(),
    )
    try:
        setg(
            "REGISTER_INTERVAL",
            float(os.getenv("REGISTER_INTERVAL", "0") or "0"),
        )
    except ValueError:
        setg("REGISTER_INTERVAL", 0.0)

    setg(
        "PROXY_DOWNLOAD_PROTOCOL",
        (os.getenv("PROXY_DOWNLOAD_PROTOCOL", "http") or "http").strip().lower(),
    )
    setg("RESIN_SUBSCRIPTION_URL", os.getenv("RESIN_SUBSCRIPTION_URL", "").strip())
    setg("RESIN_API_TOKEN", os.getenv("RESIN_API_TOKEN", "").strip())
    setg(
        "RESIN_NAME",
        os.getenv("RESIN_NAME", "proxyscrape").strip() or "proxyscrape",
    )
    setg(
        "RESIN_UPDATE_INTERVAL",
        os.getenv("RESIN_UPDATE_INTERVAL", "12h").strip() or "12h",
    )
    setg(
        "RESIN_EPHEMERAL_NODE_EVICT_DELAY",
        os.getenv("RESIN_EPHEMERAL_NODE_EVICT_DELAY", "72h0m0s").strip() or "72h0m0s",
    )
    setg(
        "RESIN_ENABLED",
        os.getenv("RESIN_ENABLED", "true").strip().lower()
        not in ("0", "false", "no", "off"),
    )
    setg(
        "RESIN_EPHEMERAL",
        os.getenv("RESIN_EPHEMERAL", "false").strip().lower()
        in ("1", "true", "yes", "on"),
    )
    setg(
        "RESIN_INCREMENTAL_ALIVE_NODES",
        os.getenv("RESIN_INCREMENTAL_ALIVE_NODES", "false").strip().lower()
        in ("1", "true", "yes", "on"),
    )
    proxy = os.getenv("PROXY")
    setg(
        "PROXIES",
        {"http": proxy, "https": proxy} if proxy else {},
    )
    return reloaded
