"""
Pure helpers for ProxyScrape registration (unit-testable, no I/O).
Discovered via chrome-devtools-mcp against https://dashboard.proxyscrape.com/v2/sign-up
"""
import os
import random
import re
import string
import time
from typing import Optional


# Live site constants (from MCP discovery)
SIGNUP_URL = "https://dashboard.proxyscrape.com/v2/sign-up"
LOGIN_URL = "https://dashboard.proxyscrape.com/v2/login"
REGISTER_ENDPOINT = "https://dashboard.proxyscrape.com/v2/v4/account/auth/register"
LOGIN_ENDPOINT = "https://dashboard.proxyscrape.com/v2/v4/account/auth/login"
VERIFY_EMAIL_ENDPOINT = "https://dashboard.proxyscrape.com/v2/v4/account/verify-email"
RESEND_CODE_ENDPOINT = "https://dashboard.proxyscrape.com/v2/v4/account/reset-verification-code"
TYPEFORM_ENDPOINT = "https://dashboard.proxyscrape.com/v2/v4/account/typeform"
PREMIUM_TRIAL_CLAIM_ENDPOINT = "https://dashboard.proxyscrape.com/v2/v4/account/premium/claim-trial"
ME_ENDPOINT = "https://dashboard.proxyscrape.com/v2/v4/account/auth/me"
# Post-verify onboarding (typeform embed vnCgUn0n → then dashboard)
TYPEFORM_PAGE = "https://dashboard.proxyscrape.com/v2/typeform"
TYPEFORM_FORM_ID = "vnCgUn0n"
TURNSTILE_SITEKEY = "0x4AAAAAAAFWUVCKyusT9T8r"
KEYS_DIR = "keys"
ACCOUNTS_FILE = os.path.join(KEYS_DIR, "proxyscrape_accounts.txt")
# Primary deliverable: premium proxy list lines
PROXIES_FILE = os.path.join(KEYS_DIR, "proxies.txt")
# Cached overview/details so web UI survives page refresh
ACCOUNT_DETAILS_CACHE_FILE = os.path.join(KEYS_DIR, "account_details_cache.json")

# Dashboard: /v2/services/premium/proxy-list/{accountId}
# Download format value "3" = protocol://user:pass@host:port (MCP discovery)
CREDENTIAL_FORMAT_PROTOCOL_URL = "3"
DEFAULT_PROXY_PROTOCOL = "http"

# Password must include special character matching site rule
_SPECIAL = "!@#$%^&*(),.?\":{}|<>"
_PASSWORD_SPECIAL_RE = re.compile(r'[!@#$%^&*(),.?":{}|<>]')


def generate_random_string(length: int = 12) -> str:
    """Lowercase + digits random string (not a full password)."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def generate_register_password(configured: Optional[str] = None) -> str:
    """
    Build a password that satisfies ProxyScrape sign-up rules:
    min 8 chars, ≥1 uppercase, ≥1 digit, ≥1 special character.
    If configured is set and not 'random', use it as-is (caller should meet rules).
    """
    if configured and configured.strip() and configured.strip().lower() != "random":
        return configured.strip()

    # Ensure all required character classes
    upper = random.choice(string.ascii_uppercase)
    lower = "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    digit = random.choice(string.digits)
    special = random.choice(_SPECIAL)
    extra = "".join(
        random.choice(string.ascii_letters + string.digits) for _ in range(4)
    )
    chars = list(upper + lower + digit + special + extra)
    random.shuffle(chars)
    return "".join(chars)


def password_meets_rules(password: str) -> bool:
    """Validate against ProxyScrape client-side rules."""
    if not password or len(password) < 8:
        return False
    if not re.search(r"[A-Z]", password):
        return False
    if not re.search(r"\d", password):
        return False
    if not _PASSWORD_SPECIAL_RE.search(password):
        return False
    return True


def _decode_quoted_printableish(content: str) -> str:
    """Light cleanup for QP soft breaks and HTML so codes are easier to match."""
    text = content.replace("=\r\n", "").replace("=\n", "")
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def parse_verification_code(email_content: str) -> Optional[str]:
    """
    Extract ProxyScrape email verification code.

    Live mail format (discovered 2026-07-19):
      "Here is your email verification code: 94168d64e3"
    Codes are 8–16 char lowercase hex (not pure 6-digit OTP).
    """
    if not email_content:
        return None

    # Prefer body-ish text (skip matching random hex in headers)
    body = email_content
    if "\r\n\r\n" in email_content:
        body = email_content.split("\r\n\r\n", 1)[-1]
    elif "\n\n" in email_content:
        body = email_content.split("\n\n", 1)[-1]
    plain = _decode_quoted_printableish(body)

    # ProxyScrape exact phrase (best)
    m = re.search(
        r"email\s+verification\s+code\s*[:：]\s*([0-9a-fA-F]{8,16})\b",
        plain,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    m = re.search(
        r"verification\s+code\s*[:：]\s*([0-9a-fA-F]{8,16})\b",
        plain,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    # Same patterns on raw body (HTML may not strip cleanly)
    m = re.search(
        r"verification\s+code\s*[:：]\s*([0-9a-fA-F]{8,16})\b",
        body,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    # HTML: code right after the phrase inside tags
    m = re.search(
        r"verification\s+code\s*[:：]\s*</[^>]+>\s*([0-9a-fA-F]{8,16})\b",
        body,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    # Last resort: hex token near "verification" (avoid bare "code" — matches Message-ID etc.)
    m = re.search(
        r"verification[^0-9a-fA-F]{0,40}([0-9a-fA-F]{8,16})\b",
        plain,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    return None


def format_account_line(
    email: str,
    password: str,
    access_token: str = "",
    extra: str = "",
) -> str:
    """
    ProxyScrape-oriented credential line (not Grok SSO).
    email----password----access_token[----extra]
    """
    parts = [email, password, access_token or ""]
    if extra:
        parts.append(extra)
    return "----".join(parts) + "\n"


def save_account_credentials(
    email: str,
    password: str,
    access_token: str = "",
    keys_dir: str = KEYS_DIR,
    accounts_file: Optional[str] = None,
    extra: str = "",
) -> dict:
    """
    Persist account side-log under keys/proxyscrape_accounts.txt only
    (email----password----access_token[----extra]). No separate tokens file.

    Prefer extra= for status markers (UNVERIFIED, …) instead of appending them
    onto access_token — keeps JWT intact for parse_account_line.
    """
    os.makedirs(keys_dir, exist_ok=True)
    accounts_path = accounts_file or os.path.join(keys_dir, "proxyscrape_accounts.txt")

    # Peel markers wrongly glued onto token by older call sites
    token = access_token or ""
    status_extra = (extra or "").strip()
    for m in _ACCOUNT_LINE_MARKERS:
        suffix = "----" + m
        if token.endswith(suffix):
            token = token[: -len(suffix)]
            if m not in status_extra:
                status_extra = (status_extra + "----" + m).strip("-")
        elif token.endswith(m) and not status_extra:
            # avoid eating JWT that merely ends with those letters — require ---- glue
            pass

    line = format_account_line(email, password, token, extra=status_extra)
    with open(accounts_path, "a", encoding="utf-8") as f:
        f.write(line)

    return {"accounts": accounts_path}


def build_register_form_fields(
    email: str,
    password: str,
    turnstile_token: str,
) -> dict:
    """Form fields for POST /v2/v4/account/auth/register (discovered)."""
    return {
        "email": email,
        "password": password,
        "cf_turnstile_token": turnstile_token,
    }


def build_login_form_fields(
    email: str,
    password: str,
    turnstile_token: str,
) -> dict:
    """
    Form fields for POST /v2/v4/account/auth/login (discovered from login JS).

    Password path: {email, password, cf_turnstile_token}
    Response shape matches register: {access_token, userData}.
    """
    return {
        "email": email,
        "password": password,
        "cf_turnstile_token": turnstile_token,
    }


def decode_jwt_payload(token: str) -> Optional[dict]:
    """
    Decode JWT payload without verifying signature (client-side exp check only).
    Returns dict or None if not a JWT / unreadable.
    """
    import base64
    import json

    if not token or not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    pad = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def access_token_exp_unix(token: str) -> Optional[int]:
    """Return JWT exp as unix seconds, or None if unknown."""
    data = decode_jwt_payload(token or "")
    if not data or data.get("exp") is None:
        return None
    try:
        n = int(float(data["exp"]))
    except (TypeError, ValueError):
        return None
    if n > 10_000_000_000:  # ms
        n = n // 1000
    return n


def is_access_token_expired(
    token: Optional[str],
    now: Optional[float] = None,
    skew_seconds: int = 60,
) -> bool:
    """
    True when token is missing or JWT exp is past (with skew).

    Non-JWT tokens without exp: treated as not expired locally (API decides).
    """
    import time as _time

    if not token or not str(token).strip():
        return True
    exp = access_token_exp_unix(token)
    if exp is None:
        return False
    if now is None:
        now = _time.time()
    return float(exp) <= float(now) + float(skew_seconds)


def build_typeform_complete_fields(
    form_id: str = TYPEFORM_FORM_ID,
    response_id: Optional[str] = None,
) -> dict:
    """
    Fields for POST /v2/v4/account/typeform.

    After email verify, user.typeform=true forces /v2/typeform (embedded Typeform
    form id vnCgUn0n: individual/company + proxy use). Frontend onSubmit posts
    {form_id, response_id}; API accepts a client-generated response_id and sets
    typeform=false so the user can enter the real dashboard.
    """
    rid = response_id or (
        "auto-"
        + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(16))
    )
    return {"form_id": form_id, "response_id": rid}


def needs_typeform_onboarding(user_or_me: dict) -> bool:
    """True when /me (or userData) still requires the post-signup questionnaire."""
    if not isinstance(user_or_me, dict):
        return False
    # API uses lowercase `typeform`: true = incomplete, false = done
    if "typeform" in user_or_me:
        return bool(user_or_me.get("typeform"))
    if "Typeform" in user_or_me:
        return bool(user_or_me.get("Typeform"))
    return False


def pick_subaccount_id(me_or_user: dict) -> Optional[str]:
    """First associatedSubaccounts[].AccountID from /me or register userData."""
    if not isinstance(me_or_user, dict):
        return None
    subs = me_or_user.get("associatedSubaccounts") or []
    if not subs:
        return None
    first = subs[0] or {}
    return first.get("AccountID") or first.get("accountId") or first.get("id")


def proxy_list_download_url(account_id: str) -> str:
    """API for GET premium proxy list blob (same data as dashboard Download)."""
    return (
        f"https://dashboard.proxyscrape.com/v2/v4/account/"
        f"{account_id}/datacenter_shared/proxy-list"
    )


def proxy_list_page_url(account_id: str) -> str:
    return (
        f"https://dashboard.proxyscrape.com/v2/services/premium/"
        f"proxy-list/{account_id}"
    )


def overview_url(account_id: str) -> str:
    return (
        f"https://dashboard.proxyscrape.com/v2/v4/account/"
        f"{account_id}/services/overview"
    )


def build_proxy_download_params(
    protocol: str = DEFAULT_PROXY_PROTOCOL,
    credential_format: str = CREDENTIAL_FORMAT_PROTOCOL_URL,
    country: Optional[str] = None,
) -> dict:
    """
    Query params for type=getproxies download.

    credential_format (from dashboard Download format menu):
      1 = host:port:user:pass
      2 = user:pass@host:port
      3 = protocol://user:pass@host:port   ← default desired output
      (IP:port only also exists in UI)
    """
    params = {
        "type": "getproxies",
        "protocol": (protocol or "http").lower(),
        "format": "credentials",
        "credential_format": str(credential_format),
    }
    if country:
        # UI may pass country filter; leave flexible
        params["country"] = country
    return params


def build_proxy_display_params(
    protocol: str = DEFAULT_PROXY_PROTOCOL,
) -> dict:
    """
    Query params for type=displayproxies (dashboard country breakdown).

    Live GET:
      /v2/v4/account/{id}/datacenter_shared/proxy-list
        ?format=data&type=displayproxies&protocol=http
      → {"countries":{"us":52,"de":11,...},"recordsTotal":100}
    """
    return {
        "format": "data",
        "type": "displayproxies",
        "protocol": (protocol or "http").lower(),
    }


def normalize_proxy_countries_payload(payload: Optional[dict]) -> dict:
    """
    Map displayproxies JSON to UI fields.

    Returns:
      countries: {cc: count, ...} lower-case ISO keys
      country_count: number of distinct regions
      records_total: total proxy rows (recordsTotal)
      countries_display: short label e.g. "9"
      countries_breakdown: "US:52, DE:11, ..." (uppercase codes, count desc)
    """
    empty = {
        "countries": {},
        "country_count": None,
        "records_total": None,
        "countries_display": "—",
        "countries_breakdown": "",
    }
    if not isinstance(payload, dict):
        return empty

    raw = payload.get("countries")
    if not isinstance(raw, dict):
        raw = {}

    countries = {}
    for k, v in raw.items():
        if k is None:
            continue
        cc = str(k).strip().lower()
        if not cc:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n < 0:
            continue
        countries[cc] = n

    records_total = payload.get("recordsTotal")
    if records_total is None:
        records_total = payload.get("records_total")
    try:
        records_total = int(records_total) if records_total is not None else None
    except (TypeError, ValueError):
        records_total = None

    country_count = len(countries)
    # tooltip: uppercase ISO codes, sorted by count desc then code asc
    parts = [
        f"{cc.upper()}:{n}"
        for cc, n in sorted(countries.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "countries": countries,
        "country_count": country_count,
        "records_total": records_total,
        "countries_display": str(country_count) if countries else "—",
        "countries_breakdown": ", ".join(parts),
    }


def parse_proxy_download_text(body: str) -> list:
    """
    Split download body into non-empty proxy lines.
    Expected: protocol://user:pass@host:port
    """
    if not body:
        return []
    lines = []
    for raw in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        # skip HTML/error payloads
        if line.startswith("<") or line.startswith("{"):
            continue
        lines.append(line)
    return lines


def is_protocol_url_proxy_line(line: str) -> bool:
    """Validate protocol://user:pass@host:port shape (loose)."""
    if not line:
        return False
    return bool(
        re.match(
            r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^:@\s]+:[^@\s]+@[^:\s]+:\d+$",
            line.strip(),
        )
    )


def format_proxy_line(
    protocol: str,
    username: str,
    password: str,
    host: str,
    port,
) -> str:
    """Build protocol://user:pass@host:port."""
    proto = (protocol or "http").lower().split()[0]
    return f"{proto}://{username}:{password}@{host}:{port}"


def save_proxy_lines(
    lines: list,
    keys_dir: str = KEYS_DIR,
    proxies_file: Optional[str] = None,
    account_file: Optional[str] = None,
    account_id: str = "",
    email: str = "",
) -> dict:
    """
    Append proxy lines to keys/proxies.txt (primary deliverable, never overwrite).
    Optionally also append a per-account file.
    """
    os.makedirs(keys_dir, exist_ok=True)
    main_path = proxies_file or os.path.join(keys_dir, "proxies.txt")
    # Always append for batch registration accumulation
    with open(main_path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line.rstrip() + "\n")

    per_path = None
    if account_file:
        per_path = account_file
    elif account_id:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", account_id)[:36]
        per_path = os.path.join(keys_dir, f"proxies_{safe}.txt")
    if per_path:
        # Append so re-downloads do not wipe prior lines for same file path
        with open(per_path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line.rstrip() + "\n")

    return {"proxies": main_path, "account_proxies": per_path, "count": len(lines)}


def load_proxies_file_content(
    proxies_file: Optional[str] = None,
    keys_dir: str = KEYS_DIR,
    skip_comments: bool = True,
) -> str:
    """
    Read full proxies.txt for remote upload.
    Returns content with trailing newline; empty string if missing/empty.
    """
    path = proxies_file or os.path.join(keys_dir, "proxies.txt")
    if not os.path.isfile(path):
        return ""
    lines = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if skip_comments and line.startswith("#"):
                continue
            lines.append(line)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def extract_register_success(response_json: dict) -> Optional[dict]:
    """
    Parse successful register JSON.
    Returns {access_token, email, email_id, email_verified, user_data} or None.
    """
    if not isinstance(response_json, dict):
        return None
    token = response_json.get("access_token")
    user = response_json.get("userData") or response_json.get("user") or {}
    if not token and response_json.get("success") is False:
        return None
    if not token:
        return None
    return {
        "access_token": token,
        "email": user.get("email") or user.get("Email"),
        "email_id": user.get("emailId") or user.get("EmailId"),
        "email_verified": bool(
            user.get("EmailVerified") or user.get("emailVerified")
        ),
        "user_data": user,
        "raw": response_json,
    }


# ---------------------------------------------------------------------------
# Web management: account store + overview display fields
# ---------------------------------------------------------------------------


# Status markers main.py may append after the token as extra ----segments
_ACCOUNT_LINE_MARKERS = frozenset(
    {
        "UNVERIFIED",
        "NO_TYPEFORM",
        "NO_PREMIUM_TRIAL",
        "NO_ACCOUNT_ID",
        "NO_PROXIES",
    }
)
_MARKER_WITH_LEADING_HYPHENS = re.compile(
    r"^(-+)(" + "|".join(sorted(_ACCOUNT_LINE_MARKERS, key=len, reverse=True)) + r")$"
)


def _split_account_line_parts(line: str) -> list:
    """
    Split on ---- but re-attach hyphens stolen from JWT ends.

    Because base64url tokens may end with '-', joining token + '----' + MARKER
    produces five hyphens; str.split('----') then yields
    [..., 'token_without_trailing_hyphen', '-MARKER']. Reattach those hyphens
    to the previous segment and normalize the marker name.
    """
    raw = str(line).strip().split("----")
    fixed: list = []
    for seg in raw:
        m = _MARKER_WITH_LEADING_HYPHENS.match(seg)
        if m and fixed:
            hyphens, marker = m.group(1), m.group(2)
            fixed[-1] = fixed[-1] + hyphens
            fixed.append(marker)
        else:
            fixed.append(seg)
    return fixed


def parse_account_line(line: str) -> Optional[dict]:
    """
    Parse one keys/proxyscrape_accounts.txt line:
      email----password----access_token[----extra...]
    Returns {email, password, access_token, extra, flags} or None.

    Status markers (UNVERIFIED, NO_TYPEFORM, …) are whole ----segments only.
    Never strip('-') from the token — JWTs are base64url and may start/end with '-'.
    """
    if not line or not str(line).strip() or str(line).strip().startswith("#"):
        return None
    parts = _split_account_line_parts(line)
    if len(parts) < 2:
        return None
    email = parts[0].strip()
    password = parts[1].strip() if len(parts) > 1 else ""
    # Remaining segments: non-markers form the token (usually one); markers are flags
    tail = [p.strip() for p in parts[2:] if p.strip()]
    flags = [p for p in tail if p in _ACCOUNT_LINE_MARKERS]
    token_parts = [p for p in tail if p not in _ACCOUNT_LINE_MARKERS]
    # JWT is a single segment. Do not strip any '-' from it.
    clean_token = token_parts[0] if token_parts else ""
    extra = "----".join(flags)
    if not email:
        return None
    return {
        "email": email,
        "password": password,
        "access_token": clean_token,
        "extra": extra,
        "flags": flags,
        "raw_line": str(line).strip(),
    }


def parse_accounts_file_text(text: str) -> list:
    """Parse full accounts file content into list of account dicts (last line wins per email)."""
    by_email = {}
    order = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        acc = parse_account_line(raw)
        if not acc:
            continue
        email = acc["email"].lower()
        if email not in by_email:
            order.append(email)
        by_email[email] = acc
    return [by_email[e] for e in order]


def load_accounts_from_file(path: Optional[str] = None) -> list:
    """Load and parse project account store (default keys/proxyscrape_accounts.txt)."""
    p = path or ACCOUNTS_FILE
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return parse_accounts_file_text(f.read())


def update_account_access_token(
    email: str,
    access_token: str,
    accounts_file: Optional[str] = None,
) -> bool:
    """
    Replace access_token for one email in the accounts file (password/flags kept).
    Returns True if the email was found and the file rewritten.
    """
    path = accounts_file or ACCOUNTS_FILE
    accounts = load_accounts_from_file(path)
    if not accounts:
        return False
    found = False
    lines = []
    for acc in accounts:
        token = acc.get("access_token") or ""
        if (acc.get("email") or "").lower() == (email or "").lower():
            token = access_token or ""
            found = True
        flags = list(acc.get("flags") or [])
        extra = "----".join(flags) if flags else ""
        line = format_account_line(
            acc.get("email") or "",
            acc.get("password") or "",
            token,
            extra=extra,
        ).rstrip("\n")
        lines.append(line)
    if not found:
        return False
    os.makedirs(os.path.dirname(path) or KEYS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return True


def load_account_details_cache(
    path: Optional[str] = None,
) -> dict:
    """
    Load email -> cached UI row fields (details / subaccount / verification).
    File: keys/account_details_cache.json
    """
    import json

    p = path or ACCOUNT_DETAILS_CACHE_FILE
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # normalize keys to lower email
        out = {}
        for k, v in data.items():
            if isinstance(v, dict) and k:
                out[str(k).lower()] = v
        return out
    except (OSError, ValueError, TypeError):
        return {}


def save_account_details_cache(
    cache: dict,
    path: Optional[str] = None,
) -> str:
    """Persist details cache dict to JSON. Returns path written."""
    import json

    p = path or ACCOUNT_DETAILS_CACHE_FILE
    os.makedirs(os.path.dirname(p) or KEYS_DIR, exist_ok=True)
    # only keep dict values
    clean = {}
    for k, v in (cache or {}).items():
        if k and isinstance(v, dict):
            clean[str(k).lower()] = v
    with open(p, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return p


def cache_entry_from_ui_row(row: dict) -> dict:
    """Strip secrets; keep fields needed to restore list/detail after refresh."""
    if not isinstance(row, dict):
        return {}
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    # never store password or full token in cache
    return {
        "email": row.get("email"),
        "details": details,
        "email_verified": row.get("email_verified"),
        "typeform_pending": row.get("typeform_pending"),
        "subaccount_id": row.get("subaccount_id"),
        "flags": list(row.get("flags") or []),
        "cached_at": __import__("time").time(),
    }


def refresh_bandwidth_display_fields(details: dict) -> dict:
    """
    Recompute bandwidth_*_display from raw byte fields.

    Keeps disk cache usable after format_bytes_human changes (e.g. SI vs binary).
    """
    if not isinstance(details, dict):
        return details
    out = dict(details)
    for raw_key, display_key in (
        ("bandwidth_total", "bandwidth_total_display"),
        ("bandwidth_used", "bandwidth_used_display"),
        ("bandwidth_remaining", "bandwidth_remaining_display"),
    ):
        raw = out.get(raw_key)
        if raw is not None:
            out[display_key] = format_bytes_human(raw)
    return out


def apply_cached_details_to_row(row: dict, cache: dict) -> dict:
    """
    Merge disk cache into a UI row when live details are missing.
    Only fills when row.details is not ok / not live-refreshed.
    """
    if not isinstance(row, dict):
        return row
    email = (row.get("email") or "").lower()
    if not email or not isinstance(cache, dict):
        return row
    entry = cache.get(email)
    if not isinstance(entry, dict):
        return row
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    cached_details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    # Prefer live ok details; else restore cache if it was successful
    if not details.get("ok") and cached_details.get("ok"):
        row = dict(row)
        row["details"] = refresh_bandwidth_display_fields(cached_details)
        if row.get("email_verified") is None and entry.get("email_verified") is not None:
            row["email_verified"] = entry.get("email_verified")
        if row.get("typeform_pending") is None and entry.get("typeform_pending") is not None:
            row["typeform_pending"] = entry.get("typeform_pending")
        if not row.get("subaccount_id") and entry.get("subaccount_id"):
            row["subaccount_id"] = entry.get("subaccount_id")
    return row


def upsert_account_details_cache(
    row: dict,
    path: Optional[str] = None,
) -> dict:
    """Update one email's cache entry after a successful live refresh."""
    if not isinstance(row, dict) or not row.get("email"):
        return load_account_details_cache(path)
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    if not details.get("ok"):
        # don't overwrite good cache with a failed refresh
        return load_account_details_cache(path)
    cache = load_account_details_cache(path)
    cache[(row["email"] or "").lower()] = cache_entry_from_ui_row(row)
    save_account_details_cache(cache, path)
    return cache


def remove_account_details_cache(
    email: str,
    path: Optional[str] = None,
) -> dict:
    """Drop one email from details cache (on account delete)."""
    cache = load_account_details_cache(path)
    key = (email or "").lower()
    if key in cache:
        del cache[key]
        save_account_details_cache(cache, path)
    return cache


def clear_account_details_cache(path: Optional[str] = None) -> None:
    """Clear entire details cache (on delete-all)."""
    p = path or ACCOUNT_DETAILS_CACHE_FILE
    if os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            save_account_details_cache({}, path)


def format_bytes_human(n) -> str:
    """
    Human-readable byte size using decimal (1000) units.

    ProxyScrape quota is sold in SI bytes (e.g. 10_000_000_000 → 10.00 GB).
    Binary (1024) would show that same quota as misleading "9.31 GB".
    """
    try:
        val = float(n)
    except (TypeError, ValueError):
        return "—"
    if val < 0:
        val = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while val >= 1000 and i < len(units) - 1:
        val /= 1000.0
        i += 1
    if i == 0:
        return f"{int(val)} {units[i]}"
    return f"{val:.2f} {units[i]}"


def format_countdown(seconds_remaining) -> str:
    """
    Human countdown string for remaining trial time.
    e.g. "6天 12:30:05", "03:15:09", "已过期", "—".
    """
    if seconds_remaining is None:
        return "—"
    try:
        sec = int(seconds_remaining)
    except (TypeError, ValueError):
        return "—"
    if sec <= 0:
        return "已过期"
    days = sec // 86400
    h = (sec % 86400) // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if days > 0:
        return f"{days}天 {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_unix_expiry(ts, now_ts: Optional[float] = None) -> dict:
    """
    Normalize unix timestamp to display + countdown fields.
    Returns expires_at_*, expires_in_seconds, expires_countdown, is_expired.

    expires_at_display uses Beijing time (UTC+8).
    expires_at_iso remains UTC ISO-8601.
    """
    from datetime import datetime, timedelta, timezone

    beijing = timezone(timedelta(hours=8))
    empty = {
        "expires_at_unix": None,
        "expires_at_iso": None,
        "expires_at_display": "—",
        "expires_in_seconds": None,
        "expires_countdown": "—",
        "is_expired": None,
    }
    if ts is None or ts == "":
        return empty
    try:
        n = int(float(ts))
    except (TypeError, ValueError):
        return empty
    # ms vs s heuristic
    if n > 10_000_000_000:
        n = n // 1000
    dt_utc = datetime.fromtimestamp(n, tz=timezone.utc)
    dt_bj = dt_utc.astimezone(beijing)
    if now_ts is None:
        now_ts = datetime.now(tz=timezone.utc).timestamp()
    remaining = int(n - now_ts)
    return {
        "expires_at_unix": n,
        "expires_at_iso": dt_utc.isoformat(),
        "expires_at_display": dt_bj.strftime("%Y-%m-%d %H:%M") + " 北京时间",
        "expires_in_seconds": remaining,
        "expires_countdown": format_countdown(remaining),
        "is_expired": remaining <= 0,
    }


def normalize_overview_payload(overview: Optional[dict]) -> dict:
    """
    Map ProxyScrape services/overview JSON to web UI detail fields.

    Known shape (live):
      status, account_type,
      data.bandwidth, data.bandwidth_used, data.is_trial,
      data.services.datacenter_shared.expiration_time,
      data.services.datacenter_shared.proxy_amount / proxy_username / ...
    """
    base = {
        "ok": False,
        "error": None,
        "account_type": None,
        "is_trial": None,
        "account_id": None,
        "proxy_amount": None,
        "proxy_username": None,
        "proxy_credentials_enabled": None,
        "bandwidth_total": None,
        "bandwidth_used": None,
        "bandwidth_remaining": None,
        "bandwidth_total_display": "—",
        "bandwidth_used_display": "—",
        "bandwidth_remaining_display": "—",
        "expires_at_unix": None,
        "expires_at_iso": None,
        "expires_at_display": "—",
        "expires_in_seconds": None,
        "expires_countdown": "—",
        "is_expired": None,
        "proxy_list_page": None,
        # displayproxies country breakdown (filled by live refresh)
        "countries": {},
        "country_count": None,
        "records_total": None,
        "countries_display": "—",
        "countries_breakdown": "",
    }
    if not isinstance(overview, dict):
        base["error"] = "详情暂不可用"
        return base

    data = overview.get("data") if isinstance(overview.get("data"), dict) else {}
    services = data.get("services") if isinstance(data.get("services"), dict) else {}
    dc = (
        services.get("datacenter_shared")
        if isinstance(services.get("datacenter_shared"), dict)
        else {}
    )

    total = data.get("bandwidth")
    used = data.get("bandwidth_used")
    if used is None:
        used = 0
    remaining = None
    try:
        if total is not None:
            remaining = max(0, float(total) - float(used or 0))
    except (TypeError, ValueError):
        remaining = None

    exp = format_unix_expiry(dc.get("expiration_time"))
    account_id = data.get("id")

    base.update(
        {
            "ok": True,
            "error": None,
            "account_type": overview.get("account_type") or data.get("account_type"),
            "is_trial": data.get("is_trial"),
            "account_id": account_id,
            "proxy_amount": dc.get("proxy_amount"),
            "proxy_username": dc.get("proxy_username"),
            "proxy_credentials_enabled": data.get("proxy_credentials_enabled"),
            "bandwidth_total": total,
            "bandwidth_used": used,
            "bandwidth_remaining": remaining,
            "bandwidth_total_display": format_bytes_human(total)
            if total is not None
            else "—",
            "bandwidth_used_display": format_bytes_human(used)
            if used is not None
            else "—",
            "bandwidth_remaining_display": format_bytes_human(remaining)
            if remaining is not None
            else "—",
            **exp,
            "proxy_list_page": proxy_list_page_url(account_id) if account_id else None,
            "countries": {},
            "country_count": None,
            "records_total": None,
            "countries_display": "—",
            "countries_breakdown": "",
        }
    )
    return base


def apply_proxy_countries_to_details(
    details: dict,
    countries_payload: Optional[dict],
) -> dict:
    """Merge normalize_proxy_countries_payload() into overview details dict."""
    if not isinstance(details, dict):
        details = {}
    out = dict(details)
    meta = normalize_proxy_countries_payload(countries_payload)
    out.update(meta)
    return out


def merge_account_with_details(account: dict, overview: Optional[dict] = None, me: Optional[dict] = None) -> dict:
    """Combine store row + optional live overview/me for one UI row."""
    token = account.get("access_token") or ""
    token_exp = access_token_exp_unix(token)
    token_expired = is_access_token_expired(token)
    row = {
        "email": account.get("email"),
        "password": account.get("password"),
        "has_token": bool(token),
        "token_expired": token_expired,
        "token_expires_at_unix": token_exp,
        "flags": list(account.get("flags") or []),
        "details": normalize_overview_payload(overview) if overview is not None else {
            "ok": False,
            "error": "未刷新详情",
            "expires_at_display": "—",
            "expires_countdown": "—",
            "expires_in_seconds": None,
            "expires_at_unix": None,
            "bandwidth_remaining_display": "—",
        },
        "email_verified": None,
        "typeform_pending": None,
        "subaccount_id": None,
    }
    if isinstance(me, dict):
        row["email_verified"] = bool(
            me.get("EmailVerified") or me.get("emailVerified")
        )
        row["typeform_pending"] = needs_typeform_onboarding(me)
        row["subaccount_id"] = pick_subaccount_id(me)
    if row["details"].get("account_id") and not row["subaccount_id"]:
        row["subaccount_id"] = row["details"]["account_id"]
    return row


def _safe_account_id_filename(account_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", account_id or "")[:36]


def collect_related_proxy_files(
    email: str,
    account_id: str = "",
    keys_dir: str = KEYS_DIR,
) -> list:
    """List per-account proxy files that belong to this email/accountId."""
    paths = []
    if not os.path.isdir(keys_dir):
        return paths
    email_l = (email or "").lower()
    want_id = _safe_account_id_filename(account_id) if account_id else ""
    for name in os.listdir(keys_dir):
        if not name.startswith("proxies_") or not name.endswith(".txt"):
            continue
        full = os.path.join(keys_dir, name)
        if want_id and name == f"proxies_{want_id}.txt":
            paths.append(full)
            continue
        try:
            with open(full, encoding="utf-8") as f:
                head = f.read(400)
            # header: # email=xxx accountId=yyy
            if email and re.search(
                rf"#\s*email\s*=\s*{re.escape(email)}\b",
                head,
                re.I,
            ):
                paths.append(full)
        except OSError:
            continue
    return paths


# ---------------------------------------------------------------------------
# Read-only proxy feed: expiry state + local per-account proxy collection
# ---------------------------------------------------------------------------

EXPIRY_STATE_VALID = "valid"
EXPIRY_STATE_EXPIRED = "expired"
EXPIRY_STATE_UNKNOWN = "unknown"

# Feed only serves http(s) lines; is_protocol_url_proxy_line would also accept socks5://
FEED_PROTOCOLS = ("http", "https")


def account_expiry_state(entry, now_ts: Optional[float] = None) -> str:
    """
    Classify a cached details entry as 'expired' | 'valid' | 'unknown'.

    Recomputed from details.expires_at_unix against `now_ts` — the cached
    is_expired / expires_in_seconds fields are never trusted (they go stale in
    the on-disk cache). No cache entry, or no expires_at_unix, is 'unknown'
    (unknown counts as usable: we cannot prove the account is dead).
    """
    if not isinstance(entry, dict):
        return EXPIRY_STATE_UNKNOWN
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    raw = details.get("expires_at_unix")
    if raw is None or raw == "":
        return EXPIRY_STATE_UNKNOWN
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        return EXPIRY_STATE_UNKNOWN
    if n > 10_000_000_000:  # ms
        n = n // 1000
    if now_ts is None:
        now_ts = time.time()
    try:
        now = float(now_ts)
    except (TypeError, ValueError):
        now = time.time()
    return EXPIRY_STATE_EXPIRED if n <= now else EXPIRY_STATE_VALID


def resolve_cached_account_id(entry) -> str:
    """
    Account id used for keys/proxies_{id}.txt naming.

    Prefer entry['subaccount_id'] — it comes from /me, the same source main.py
    uses when writing the per-account file. details['account_id'] comes from
    overview data.id and is NOT provably the same value.
    """
    if not isinstance(entry, dict):
        return ""
    sid = entry.get("subaccount_id")
    if sid and str(sid).strip():
        return str(sid).strip()
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    aid = details.get("account_id")
    return str(aid).strip() if aid else ""


def _read_feed_proxy_lines(path: str, protocols) -> list:
    """Cleaned proxy lines from one file: skip blanks/#, require protocol://…@…:port."""
    allowed = {str(p).strip().lower() for p in (protocols or ()) if str(p).strip()}
    lines = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if not is_protocol_url_proxy_line(line):
                    continue
                scheme = line.split("://", 1)[0].lower()
                if allowed and scheme not in allowed:
                    continue
                lines.append(line)
    except OSError:
        return []
    return lines


def _find_account_proxy_file(
    email: str,
    account_id: str,
    keys_dir: str,
) -> Optional[str]:
    """Path of the per-account proxy file for this account, or None."""
    if account_id:
        direct = os.path.join(
            keys_dir, f"proxies_{_safe_account_id_filename(account_id)}.txt"
        )
        if os.path.isfile(direct):
            return direct
    related = collect_related_proxy_files(email, account_id=account_id, keys_dir=keys_dir)
    return related[0] if related else None


def collect_valid_proxy_lines(
    accounts: Optional[list] = None,
    cache: Optional[dict] = None,
    keys_dir: str = KEYS_DIR,
    now_ts: Optional[float] = None,
    protocols=FEED_PROTOCOLS,
) -> dict:
    """
    Read-only, side-effect-free proxy feed source.

    Reads ONLY keys/proxies_{accountId}.txt per account — never keys/proxies.txt
    (that file is append-only and never pruned, so it holds dead accounts too).
    Makes no network requests.

    Returns:
      lines: ordered, de-duplicated proxy lines from non-expired accounts
      accounts: per-account rows {email, account_id, proxy_file, state,
                 reason, line_count}
      valid_count / expired_count / unknown_count: accounts by expiry state
      feed_eligible_count: accounts that are usable AND have a non-empty
                 per-account proxy file (this is the "how many can I serve" number)
      skipped_count: usable accounts whose proxy file is missing or empty
    """
    if accounts is None:
        accounts = load_accounts_from_file()
    if cache is None:
        cache = load_account_details_cache()
    cache = cache if isinstance(cache, dict) else {}

    feed_lines: list = []
    seen: set = set()
    rows: list = []
    valid_count = 0
    expired_count = 0
    unknown_count = 0
    feed_eligible_count = 0
    skipped_count = 0

    for acc in accounts or []:
        if not isinstance(acc, dict):
            continue
        email = (acc.get("email") or "").strip()
        if not email:
            continue
        entry = cache.get(email.lower())
        state = account_expiry_state(entry, now_ts=now_ts)
        account_id = resolve_cached_account_id(entry)

        if state == EXPIRY_STATE_VALID:
            valid_count += 1
        elif state == EXPIRY_STATE_EXPIRED:
            expired_count += 1
        else:
            unknown_count += 1

        proxy_file = None
        line_count = 0
        reason = ""

        if state == EXPIRY_STATE_EXPIRED:
            reason = "expired"
        else:
            path = _find_account_proxy_file(email, account_id, keys_dir)
            if not path:
                reason = "no_proxy_file"
            else:
                proxy_file = path
                found = _read_feed_proxy_lines(path, protocols)
                if not found:
                    reason = "empty_proxy_file"
                else:
                    for line in found:
                        if line in seen:
                            continue
                        seen.add(line)
                        feed_lines.append(line)
                        line_count += 1

        if reason == "":
            feed_eligible_count += 1
        elif state != EXPIRY_STATE_EXPIRED:
            skipped_count += 1

        rows.append(
            {
                "email": email,
                "account_id": account_id or None,
                "proxy_file": os.path.basename(proxy_file) if proxy_file else None,
                "state": state,
                "reason": reason,
                "line_count": line_count,
            }
        )

    return {
        "lines": feed_lines,
        "accounts": rows,
        "valid_count": valid_count,
        "feed_eligible_count": feed_eligible_count,
        "expired_count": expired_count,
        "unknown_count": unknown_count,
        "skipped_count": skipped_count,
    }


def filter_proxy_lines_excluding(
    content: str,
    drop_lines: set,
    drop_usernames: Optional[set] = None,
) -> tuple:
    """
    Remove exact proxy lines and/or lines matching user@host auth for usernames.
    Returns (new_content, removed_count).
    """
    drop_lines = {ln.strip() for ln in (drop_lines or set()) if ln and ln.strip()}
    drop_usernames = {u for u in (drop_usernames or set()) if u}
    kept = []
    removed = 0
    for raw in (content or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            if line.startswith("#"):
                kept.append(raw.rstrip("\n"))
            continue
        if line in drop_lines:
            removed += 1
            continue
        # protocol://user:pass@host:port
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^:@/]+):", line)
        if m and m.group(1) in drop_usernames:
            removed += 1
            continue
        kept.append(line)
    body = "\n".join(kept)
    if body and not body.endswith("\n"):
        body += "\n"
    return body, removed


def delete_account_local_data(
    email: str,
    keys_dir: str = KEYS_DIR,
    accounts_file: Optional[str] = None,
    proxies_file: Optional[str] = None,
    account_id: str = "",
    proxy_username: str = "",
) -> dict:
    """
    Delete one account and related local artifacts:
      - remove lines for email from proxyscrape_accounts.txt
      - delete keys/proxies_{accountId}.txt (and email-matched per-account files)
      - strip matching proxy lines from keys/proxies.txt
    Does not call remote ProxyScrape APIs (local store only).
    """
    email = (email or "").strip()
    if not email:
        return {"ok": False, "error": "email required", "removed": {}}

    accounts_path = accounts_file or os.path.join(keys_dir, "proxyscrape_accounts.txt")
    proxies_path = proxies_file or os.path.join(keys_dir, "proxies.txt")
    removed = {
        "account_lines": 0,
        "proxy_files": [],
        "proxy_lines": 0,
    }

    # 1) accounts file
    if os.path.isfile(accounts_path):
        with open(accounts_path, encoding="utf-8") as f:
            lines = f.readlines()
        kept = []
        for ln in lines:
            acc = parse_account_line(ln)
            if acc and acc["email"].lower() == email.lower():
                removed["account_lines"] += 1
                if not account_id and acc.get("extra"):
                    pass
                continue
            kept.append(ln if ln.endswith("\n") else ln + "\n")
        with open(accounts_path, "w", encoding="utf-8") as f:
            f.writelines(kept)

    # 2) per-account proxy files + collect lines/usernames to strip
    drop_lines: set = set()
    drop_users: set = set()
    if proxy_username:
        drop_users.add(proxy_username)

    related = collect_related_proxy_files(email, account_id=account_id, keys_dir=keys_dir)
    for path in related:
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        # parse accountId from header if missing
                        if line.startswith("#") and "accountId=" in line and not account_id:
                            m = re.search(r"accountId\s*=\s*(\S+)", line)
                            if m:
                                account_id = m.group(1)
                        continue
                    drop_lines.add(line)
                    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^:@/]+):", line)
                    if m:
                        drop_users.add(m.group(1))
            os.remove(path)
            removed["proxy_files"].append(os.path.basename(path))
        except OSError:
            continue

    # also try direct name if account_id known but file not matched
    if account_id:
        direct = os.path.join(
            keys_dir, f"proxies_{_safe_account_id_filename(account_id)}.txt"
        )
        if os.path.isfile(direct) and direct not in [
            os.path.join(keys_dir, x) for x in removed["proxy_files"]
        ]:
            try:
                with open(direct, encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if line and not line.startswith("#"):
                            drop_lines.add(line)
                            m = re.match(
                                r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^:@/]+):", line
                            )
                            if m:
                                drop_users.add(m.group(1))
                os.remove(direct)
                removed["proxy_files"].append(os.path.basename(direct))
            except OSError:
                pass

    # 3) main proxies.txt
    if os.path.isfile(proxies_path) and (drop_lines or drop_users):
        with open(proxies_path, encoding="utf-8") as f:
            content = f.read()
        new_content, n_rm = filter_proxy_lines_excluding(
            content, drop_lines, drop_users
        )
        removed["proxy_lines"] = n_rm
        with open(proxies_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    return {
        "ok": True,
        "email": email,
        "account_id": account_id or None,
        "removed": removed,
    }


def delete_all_accounts_local_data(
    keys_dir: str = KEYS_DIR,
    accounts_file: Optional[str] = None,
    proxies_file: Optional[str] = None,
    clear_proxies_txt: bool = True,
) -> dict:
    """
    Delete ALL local account rows and related proxy artifacts.
      - empty/clear proxyscrape_accounts.txt
      - delete all keys/proxies_*.txt per-account files
      - optionally empty keys/proxies.txt
    """
    accounts_path = accounts_file or os.path.join(keys_dir, "proxyscrape_accounts.txt")
    proxies_path = proxies_file or os.path.join(keys_dir, "proxies.txt")
    removed = {
        "account_lines": 0,
        "proxy_files": [],
        "proxies_txt_cleared": False,
    }

    if os.path.isfile(accounts_path):
        with open(accounts_path, encoding="utf-8") as f:
            for ln in f:
                if parse_account_line(ln):
                    removed["account_lines"] += 1
        with open(accounts_path, "w", encoding="utf-8") as f:
            f.write("")

    if os.path.isdir(keys_dir):
        for name in os.listdir(keys_dir):
            if name.startswith("proxies_") and name.endswith(".txt"):
                full = os.path.join(keys_dir, name)
                try:
                    os.remove(full)
                    removed["proxy_files"].append(name)
                except OSError:
                    continue

    if clear_proxies_txt and os.path.isfile(proxies_path):
        with open(proxies_path, "w", encoding="utf-8") as f:
            f.write("")
        removed["proxies_txt_cleared"] = True

    return {"ok": True, "removed": removed}
