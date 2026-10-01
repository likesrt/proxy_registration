"""
邮箱域名黑名单

- 数据存在 .env 的 ``EMAIL_BLACKLIST``（Web 配置页可直接查看 / 增删），
  支持换行、逗号、分号或空格分隔。
- **自动加入**时统一取「可注册根域」(eTLD+1)：
  ``e.m.a.il.corbyrise.com`` → ``corbyrise.com``，这样临时邮箱换子域也拦得住。
- **公共后缀**（``eu.org`` / ``co.uk`` / ``github.io`` 这类被 PSL 视为后缀的域名）
  会被拒绝自动加入：它们不是某一个人的域名，封掉会误伤整个后缀。
- 匹配时同时比较完整域名、根域和各级父域，所以 ``corbyrise.com`` 能拦住
  ``alawson720@e.m.a.il.corbyrise.com``。
- 匹配是**双向**的：名单里手填 ``mail.example.com`` 会连 ``example.com`` 以及同根域的
  兄弟子域一起拦住（见 ``is_blocked``）。想只拦某一个子域请写完整域名并自行确认影响范围。

读取策略统一为「``.env`` 文件优先，文件里没有该键时才回退 ``os.environ``」，
所以注册过程中自动加入的条目立刻生效，而 Web 配置页看到的就是真正生效的名单。

没装 ``tldextract`` 时退化为「原样记录完整域名」——宁可漏拦，也不误伤。
"""
from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional, Tuple

from .env_config import (
    DEFAULT_ENV_PATH,
    ENV_FILE_LOCK,
    parse_env_file,
    upsert_env_file,
)

# .env 键名（Web 配置页的配置项）
ENV_KEY = "EMAIL_BLACKLIST"
ENV_AUTO_KEY = "EMAIL_BLACKLIST_AUTO"

_SPLIT_RE = re.compile(r"[\s,;]+")
_LABEL_RE = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^{_LABEL_RE}(?:\.{_LABEL_RE})+$")
_FALSE_VALUES = {"0", "false", "no", "off", ""}

# 解析结果缓存：只留最新一份，键为 (路径, 文件指纹)，见 load_blocked_entries
_ENTRIES_CACHE: dict = {}

try:  # PSL 快照随包分发，suffix_list_urls=() 表示完全离线、不联网取列表
    import tldextract

    _EXTRACTOR = tldextract.TLDExtract(
        suffix_list_urls=(),
        cache_dir=False,
        include_psl_private_domains=True,
    )
except Exception:  # pragma: no cover - 依赖缺失时的降级路径
    _EXTRACTOR = None


class AllDomainsBlacklisted(RuntimeError):
    """配置好的邮箱域名全都在黑名单里：确定性失败，注册应停止而不是空转重试。"""


class BlockedEmailRetriesExhausted(AllDomainsBlacklisted):
    """
    服务端随机分配的邮箱连续命中黑名单（GPTMAIL_DOMAIN 为空时的 generate-email）。

    继承 ``AllDomainsBlacklisted`` 是为了兼容只认父类的调用方；但语义不同：
    这只说明「这一轮抽到的域名都被拉黑了」，换个时间可能抽到别的域名，
    调用方应先重试，连续多轮都如此再判定为停止。
    """


# --------------------------------------------------------------------------
# 域名规范化 / PSL
# --------------------------------------------------------------------------
def normalize_domain(raw) -> Optional[str]:
    """把 ``user@Example.COM`` / ``Example.com.`` 规范成 ``example.com``；非法返回 None。"""
    s = str(raw or "").strip().lower()
    if not s:
        return None
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    s = s.strip().strip("[]<>").strip().strip(".")
    if not s:
        return None
    try:
        s = s.encode("idna").decode("ascii")
    except Exception:
        pass
    if len(s) > 253 or not _DOMAIN_RE.match(s):
        return None
    return s


def psl_available() -> bool:
    """是否装好了 tldextract（没有时只能原样记录完整域名，无法收敛到根域）。"""
    return _EXTRACTOR is not None


def public_suffix(domain) -> Optional[str]:
    """返回域名所属公共后缀（``a.b.co.uk`` → ``co.uk``）；无 PSL 时返回 None。"""
    d = normalize_domain(domain)
    if not d or _EXTRACTOR is None:
        return None
    try:
        ext = _EXTRACTOR(d)
    except Exception:
        return None
    return ext.suffix or None


def is_public_suffix(domain) -> bool:
    """域名本身就是一个公共后缀（``eu.org`` / ``co.uk`` / ``com``）时为 True。"""
    return public_suffix(domain) == normalize_domain(domain)


def registrable_domain(domain) -> Optional[str]:
    """
    返回可注册根域 (eTLD+1)；域名本身就是公共后缀、裸 TLD 或无法解析时返回 None。

    ``e.m.a.il.corbyrise.com`` → ``corbyrise.com``
    ``a.eu.org``              → ``a.eu.org``（eu.org 是公共后缀，不能退成 eu.org）
    ``a.b.co.uk``             → ``b.co.uk``
    ``eu.org``                → None（公共后缀，拒绝）
    """
    d = normalize_domain(domain)
    if not d or _EXTRACTOR is None:
        return None
    try:
        ext = _EXTRACTOR(d)
    except Exception:
        return None
    if not ext.suffix or not ext.domain:
        return None
    return f"{ext.domain}.{ext.suffix}"


# --------------------------------------------------------------------------
# 名单解析 / 读取
# --------------------------------------------------------------------------
def _file_stamp(path: str) -> Optional[Tuple[int, int]]:
    """
    取文件指纹 ``(mtime_ns, size)``，用于缓存失效判断；文件不存在或不可读返回 None。

    无副作用；只读 ``os.stat``，不会打开文件。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_file_first(
    key: str, path: str
) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """
    按「``.env`` 文件优先、``os.environ`` 兜底」读一个配置项。

    参数:
        key: 配置项名。
        path: ``.env`` 路径（调用方已解析默认值）。

    返回:
        ``(raw, stamp)``。raw 为 None 表示文件与进程环境都没有该键；
        stamp 是文件指纹，值来自 ``os.environ`` 时为 None（供调用方跳过缓存）。

    副作用: 读文件；文件不可读时按「没有该键」处理，不抛异常。
    """
    stamp = _file_stamp(path)
    values = {}
    if stamp is not None:
        try:
            values = parse_env_file(path)
        except OSError:
            values = {}
    if key in values:
        return values.get(key), stamp
    return os.getenv(key), None


def parse_entries(raw) -> List[str]:
    """把配置项文本切成去重后的域名列表（保持出现顺序）。"""
    out: List[str] = []
    seen = set()
    for token in _SPLIT_RE.split(str(raw or "")):
        d = normalize_domain(token)
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def format_entries(entries: Iterable[str]) -> str:
    """序列化成 .env 安全的单行逗号分隔文本（不含空白，避免被引号包裹）。"""
    out: List[str] = []
    seen = set()
    for item in entries or []:
        d = normalize_domain(item)
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return ",".join(out)


def load_blocked_entries(path: Optional[str] = None) -> List[str]:
    """
    读取黑名单（``.env`` 文件优先；文件里没有该键时才回退 ``os.environ``）。

    参数:
        path: ``.env`` 路径；None 时用模块级 ``DEFAULT_ENV_PATH``（便于测试替换）。

    返回:
        规范化去重后的域名列表（保持文件中的出现顺序）。任何一侧都没有该键时返回 []。

    实现要点:
        - 读与写共用 ``ENV_FILE_LOCK``，避免在写入方 ``open(w)`` 截断的瞬间读到空文件；
        - 以 ``(路径, mtime_ns, size)`` 为缓存键：``_next_domain`` 每轮要对每个候选域名
          查一次黑名单，不能每次都重新解析文件；而任何一次写入都会改变 mtime/size，
          所以「运行期自动加入立即生效」的语义不受影响（缓存只留最新一份）。
    """
    p = path or DEFAULT_ENV_PATH
    with ENV_FILE_LOCK:
        raw, stamp = _read_file_first(ENV_KEY, p)
        if raw is None:
            return []
        if stamp is None:  # 值来自 os.environ：环境变量变化无法用文件指纹感知，不缓存
            return parse_entries(raw)
        cache_key = (p, stamp)
        entries = _ENTRIES_CACHE.get(cache_key)
        if entries is None:
            entries = parse_entries(raw)
            _ENTRIES_CACHE.clear()
            _ENTRIES_CACHE[cache_key] = entries
        return list(entries)  # 返回副本：调用方（block_domain）会 append


def entries_text(path: Optional[str] = None) -> str:
    """
    配置页展示用的黑名单文本，始终以 ``.env`` 文件为准。

    ``get_config_for_ui`` 的优先级是 ``os.environ`` > 文件，而注册线程自动拉黑只写文件、
    不刷新进程环境；若此处沿用它的结果，页面会显示启动时的旧名单（自动加入的条目看不见，
    也就删不掉）。这里直接按 ``load_blocked_entries`` 的优先级取，保证三处同源：
    页面显示、``is_blocked`` 实际拦截、保存时的 ``blacklist_base``。
    """
    return format_entries(load_blocked_entries(path))


def invalid_entries(raw) -> List[str]:
    """
    挑出切分后仍无法解析成域名的条目（配置页保存时回显，避免被静默丢弃）。

    参数:
        raw: 配置页提交的原始文本（可含换行 / 逗号 / 分号 / 空格分隔）。

    返回:
        无法规范化的原始 token 列表（保持出现顺序，可能含重复）；空 token 不算无效。
    """
    out: List[str] = []
    for token in _SPLIT_RE.split(str(raw or "")):
        token = token.strip()
        if token and normalize_domain(token) is None:
            out.append(token)
    return out


def auto_block_enabled(path: Optional[str] = None) -> bool:
    """
    是否允许自动加入黑名单（配置页 ``EMAIL_BLACKLIST_AUTO``，缺省为开）。

    参数:
        path: ``.env`` 路径；None 时用模块级 ``DEFAULT_ENV_PATH``。

    返回:
        开启返回 True；值为 0/false/no/off/空 时返回 False；未配置时返回 True。

    与黑名单本身同策略：``.env`` 文件优先、``os.environ`` 兜底，
    这样手改配置文件后无需重启即可生效（Docker 注入环境变量时仍按环境变量走）。
    """
    p = path or DEFAULT_ENV_PATH
    raw, _ = _read_file_first(ENV_AUTO_KEY, p)
    if raw is None:
        return True
    return str(raw).strip().lower() not in _FALSE_VALUES


# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------
def is_blocked(
    target,
    entries: Optional[Iterable[str]] = None,
    path: Optional[str] = None,
) -> bool:
    """邮箱或域名是否命中黑名单（命中完整域名 / 根域 / 各级父域任一即算）。

    参数:
        target: 邮箱地址或域名。
        entries: 直接给定的名单；None 时按 ``path`` 从 ``.env`` 读取。
        path: ``.env`` 路径，仅当 ``entries`` 为 None 时使用。

    匹配是双向的：名单里的父域拦住其所有子域；名单里手填的子域（如
    ``mail.example.com``）也会把它所属的根域与其兄弟子域一起拦住——否则同一批临时邮箱
    换个前缀或子域就绕过去了。只想拦单个子域时不要手填子域，改用完整域名 + 自担影响。
    """
    domain = normalize_domain(target)
    if not domain:
        return False
    known = set(entries) if entries is not None else set(load_blocked_entries(path))
    if not known:
        return False

    candidates = {domain}
    root = registrable_domain(domain)
    if root:
        candidates.add(root)

    for cand in candidates:
        if cand in known:
            return True
        # 父域方向：a.b.example.com 命中 example.com
        parts = cand.split(".")
        for i in range(1, len(parts)):
            if ".".join(parts[i:]) in known:
                return True
        # 子域方向：手填了完整域名 e.m.a.il.corbyrise.com 时，corbyrise.com 也应命中
        if any(entry.endswith("." + cand) for entry in known):
            return True
    return False


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------
def block_domain(
    target,
    path: Optional[str] = None,
) -> dict:
    """
    把一个邮箱 / 域名加入黑名单（幂等）。

    返回 ``{"ok": bool, "added": bool, "entry": str, "error": str}``；
    公共后缀会被拒绝（ok=False, error="public_suffix"）。
    """
    domain = normalize_domain(target)
    if not domain:
        return {"ok": False, "added": False, "entry": "", "error": "invalid_domain"}

    root = registrable_domain(domain)
    entry = root or domain  # 没有 PSL 库时保守处理：不改写用户给的域名
    if root is None and is_public_suffix(domain):
        return {
            "ok": False,
            "added": False,
            "entry": domain,
            "error": "public_suffix",
        }

    p = path or DEFAULT_ENV_PATH
    with ENV_FILE_LOCK:
        entries = load_blocked_entries(p)
        if entry in entries:
            return {"ok": True, "added": False, "entry": entry, "error": ""}
        entries.append(entry)
        upsert_env_file(
            {ENV_KEY: format_entries(entries)},
            path=p,
            keys_allowlist=[ENV_KEY],
        )
        return {"ok": True, "added": True, "entry": entry, "error": ""}


def merge_entries_for_save(
    submitted_raw,
    base_raw,
    path: Optional[str] = None,
) -> str:
    """
    配置页保存时的合并：保留页面加载之后自动加入的条目，同时尊重用户的删除。

    - 磁盘值 == 页面加载时的值 → 完全按提交值保存（用户删掉的条目就是删掉了）
    - 磁盘值变了（注册线程自动加过） → 提交值 + 期间新增的条目
    """
    submitted = parse_entries(submitted_raw)
    if base_raw is None:
        return format_entries(submitted)

    base = set(parse_entries(base_raw))
    disk = load_blocked_entries(path)
    if set(disk) == base:
        return format_entries(submitted)

    keep = list(submitted)
    seen = set(submitted)
    for entry in disk:
        if entry not in base and entry not in seen:
            seen.add(entry)
            keep.append(entry)
    return format_entries(keep)
