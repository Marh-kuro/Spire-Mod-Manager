"""P0: Steam Web API 轻量客户端（HTTP REST，不需要 SteamworksPy、不需要 API Key）。

用途：给磁盘上的 workshop mod 补充 Steam 在线信息
  - 标题/作者（比磁盘 manifest 更准）
  - 最后更新时间（对比本地 mod 判断是否有新版本）
  - 有效性状态（result=1 有效；!=1 表示已下架/不可访问）
  - 订阅数/评分/标签

核心接口：POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/
  - 零配置、零依赖（只需要 Python 标准库 urllib）
  - 每批最多 100 个 ID

⚠️ 注意：本模块走的是 api.steampowered.com 通道，与 workshop.py 走的 steamcommunity.com
   社区页面通道是两条独立的节流/风控通道。社区页面通道对 IP 限流很敏感，Web API
   通道宽容得多——因此两套通道各自独立节流，互不阻断。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .logger import get_logger

_log = get_logger("steam_webapi")

# =============== Web API 通道专属节流 ===============
# Steam Web API 对 IP 限流比社区页面宽松得多，这里用更短的间隔和更大的窗口。
_API_MIN_INTERVAL = 0.3
_API_PER_WINDOW = 200
_API_LAST_TS = 0.0
_API_WINDOW_TOKENS = 0
_API_WINDOW_RESET_TS = 0.0
_API_THROTTLE_LOCK = threading.Lock()


def _throttle():
    """Web API 通道专属节流（与社区页面通道独立）。"""
    global _API_LAST_TS, _API_WINDOW_TOKENS, _API_WINDOW_RESET_TS
    with _API_THROTTLE_LOCK:
        now = time.monotonic()
        if now - _API_WINDOW_RESET_TS > 600:
            _API_WINDOW_TOKENS = 0
            _API_WINDOW_RESET_TS = now
        if _API_WINDOW_TOKENS >= _API_PER_WINDOW:
            wait = max(0.0, 600 - (now - _API_WINDOW_RESET_TS))
            if wait > 0:
                _log.info("[steam_webapi] API 窗口上限，%.1fs 后恢复", wait)
            _API_WINDOW_TOKENS = 0
            _API_WINDOW_RESET_TS = now + wait
        earliest = max(_API_LAST_TS + _API_MIN_INTERVAL,
                       _API_WINDOW_RESET_TS if _API_WINDOW_TOKENS == 0 else now)
        _API_LAST_TS = earliest
        _API_WINDOW_TOKENS += 1
        delay = earliest - time.monotonic()
    if delay > 0:
        time.sleep(delay)

# 用户语言 → Steam API 语言 的唯一转换入口。
# 上层全部传 manager.get_language() 的 "zh"/"en"，底层统一在这里转 Steam 格式。
# 以后加新语言（日语/韩语/繁体）只用改这张表。
_STEAM_LANG_MAP = {
    "zh": "schinese",
    "en": "english",
}


def _to_steam_lang(user_lang: str) -> str:
    """把用户界面语言（zh/en）转换成 Steam API 语言代码。

    不在映射表里的值原样返回——调用方直接传 "schinese" / "english" 这种也能兼容。
    默认 fallback 到 "schinese"。
    """
    return _STEAM_LANG_MAP.get(str(user_lang).strip().lower(), str(user_lang or "schinese"))

PUBLISHED_FILE_DETAILS_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
)
CACHE_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 天缓存——避免隔天重启就全 miss 触发 429 风控


@dataclass
class SteamModInfo:
    """Steam 工坊上某个 mod 的在线信息。"""
    workshop_id: str
    result: int = 0  # 1=有效，其他值=已下架/不可访问/未找到
    title: str = ""
    description: str = ""
    author_steam_id: str = ""
    subscriptions: int = 0
    votes_up: int = 0
    votes_down: int = 0
    time_updated: int = 0  # Steam 服务器上该项目的最后更新时间戳
    revision_change_number: int = 0  # Steam 官方版本号，每次作者推新内容 +1（比 time_updated 更精准）
    file_size: int = 0  # 当前总文件大小（Steam 服务器端）
    visibility: int = 0
    tags: list[str] = field(default_factory=list)
    preview_url: str = ""
    # 本地 mod 的 mtime（用于对比是否有更新）
    local_mtime: Optional[int] = None
    # 对比结果："up-to-date" / "has-update" / "unknown"
    update_status: str = "unknown"

    @property
    def is_valid(self) -> bool:
        """Steam 上这个 mod 是否还有效（未下架）。"""
        return self.result == 1

    @property
    def has_update(self) -> bool:
        """Steam 服务器上的版本是否比本地新。"""
        if not self.is_valid or not self.time_updated or not self.local_mtime:
            return False
        return self.time_updated > self.local_mtime

    @property
    def url(self) -> str:
        return f"https://steamcommunity.com/sharedfiles/filedetails/?id={self.workshop_id}"


def _http_post_form(url: str, data: dict, timeout: int = 15) -> Optional[dict]:
    """发送 application/x-www-form-urlencoded POST 请求，返回 JSON dict。"""
    try:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "STS2ModManager/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return json.loads(body)
    except urllib.error.HTTPError as he:
        # Web API 是独立通道，429 不影响社区页面通道；这里只记日志让调用方自行处理
        _log.warning("[steam_webapi] 请求失败 → HTTP %d: %s", he.code, url)
        return None
    except Exception as e:
        _log.debug("Steam Web API 请求失败: %s → %s", url, e)
        return None


def _normalize_result(raw: Optional[dict]) -> dict[str, SteamModInfo]:
    """把 Steam 原始响应解析成 {workshop_id: SteamModInfo}。"""
    result: dict[str, SteamModInfo] = {}
    if not raw or not isinstance(raw, dict):
        return result
    resp = raw.get("response")
    if not isinstance(resp, dict):
        return result
    items = resp.get("publishedfiledetails")
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        wid = str(item.get("publishedfileid") or "").strip()
        if not wid:
            continue
        tags: list[str] = []
        for t in item.get("tags") or []:
            if isinstance(t, dict):
                name = str(t.get("tag") or t.get("display_name") or "").strip()
                if name:
                    tags.append(name)
        result[wid] = SteamModInfo(
            workshop_id=wid,
            result=int(item.get("result") or 0),
            title=str(item.get("title") or "").strip(),
            description=str(item.get("file_description") or item.get("description") or "").strip(),
            author_steam_id=str(item.get("creator") or "").strip(),
            subscriptions=int(item.get("subscriptions") or 0),
            votes_up=int(item.get("votes_up") or 0),
            votes_down=int(item.get("votes_down") or 0),
            time_updated=int(item.get("time_updated") or 0),
            revision_change_number=int(item.get("revision_change_number") or 0),
            file_size=int(item.get("file_size") or 0),
            visibility=int(item.get("visibility") or 0),
            tags=tags,
            preview_url=str(item.get("preview_url") or "").strip(),
        )
    return result


# ── steamDB.json 本地快照（RimCrow 模式） ──────────────────────────────────
# 全量 STS2 workshop 快照，由 tools/build_steamdb.py 生成（API key 全量拉 QueryFiles）
# 目的：revision_change_number（Web API 不返回）+ children 依赖链 + 快速本地查找
#
# 加载优先级（三级降级）：
#   1) tools/steamDB.json    ← 开发者用 build_steamdb.py 本地拉的
#   2) data/steamDB.json     ← 旧位置兼容（用户之前可能手动放过）
#   3) Git Release 下载      ← 后续发布：GitHub raw URL 拉最新快照
#
import threading as _threading

_GIT_STEAMDB_URL = ""  # TODO: 发布时填 GitHub raw URL（例如 "https://raw.githubusercontent.com/.../steamDB.json"）

_steamdb_cache: Optional[dict] = None
_steamdb_lock = _threading.Lock()


def _try_load_steamdb_from(path: Path) -> Optional[dict]:
    """尝试从指定路径加载 steamDB.json。成功返回 dict，否则 None。"""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)
        n = len(db.get("database", {}))
        _log.info(f"[steamdb] 加载成功: {path} ({n} 条, version={db.get('version')})")
        return db
    except Exception as e:
        _log.warning(f"[steamdb] 加载失败 {path}: {e}")
        return None


def _try_download_steamdb() -> Optional[dict]:
    """从 Git URL 下载 steamDB.json（第三级降级）。URL 为空或失败返回 None。"""
    if not _GIT_STEAMDB_URL:
        return None
    try:
        import urllib.request as _ur
        _log.info(f"[steamdb] 尝试从 Git 下载 {_GIT_STEAMDB_URL}")
        req = _ur.Request(_GIT_STEAMDB_URL, headers={"User-Agent": "SpireModManager"})
        with _ur.urlopen(req, timeout=10) as resp:
            db = json.loads(resp.read())
        # 缓存到 tools/ 下次不用再下载
        local_path = Path(__file__).parent.parent / "tools" / "steamDB.json"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
        _log.info(f"[steamdb] Git 下载成功，已缓存到 {local_path}")
        return db
    except Exception as e:
        _log.warning(f"[steamdb] Git 下载失败: {e}")
        return None


def load_steamdb() -> Optional[dict]:
    """懒加载 steamDB.json。三级降级：tools/ → data/ → Git 下载。全失败返回 None。"""
    global _steamdb_cache
    if _steamdb_cache is not None:
        return _steamdb_cache

    with _steamdb_lock:
        # double-check after acquiring lock
        if _steamdb_cache is not None:
            return _steamdb_cache

        project_root = Path(__file__).parent.parent
        candidates = [
            project_root / "tools" / "steamDB.json",   # 1) 开发者产物（新位置）
            project_root / "data" / "steamDB.json",    # 2) 旧位置兼容
        ]

        for p in candidates:
            db = _try_load_steamdb_from(p)
            if db:
                _steamdb_cache = db
                return db

        # 3) Git 下载（URL 先留空，发布时填）
        db = _try_download_steamdb()
        if db:
            _steamdb_cache = db
            return db

        _log.info("[steamdb] 未找到任何快照（本地无 + Git URL 未配置）")
        _steamdb_cache = {}  # 标记已检查过，不再重试
        return None

def query_steamdb(wid: str) -> Optional[dict]:
    """查单个 wid。返回 {"time_updated", "revision_change_number", "file_size", "tags", ...} 或 None。"""
    db = load_steamdb()
    if not db:
        return None
    return db.get("database", {}).get(str(wid))

def steamdb_version() -> int:
    """快照版本（Unix 时间戳），用于日志。没加载返回 0。"""
    db = load_steamdb()
    return int(db.get("version", 0)) if db else 0


def resolve_steamids_to_names(steamids: list[str]) -> dict[str, str]:
    """批量把 SteamID64 → 实际玩家名字。走 ISteamUser/GetPlayerSummaries/v2/。

    无 key 也能用，每批最多 100 个 steamid。失败静默跳过。
    用途：Web API 和 steamDB 都只返回 steamid，不返回作者名字。
    """
    if not steamids:
        return {}
    unique = list(dict.fromkeys(str(s) for s in steamids if s))
    result: dict[str, str] = {}

    for i in range(0, len(unique), 100):
        batch = unique[i:i + 100]
        url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?steamids={','.join(batch)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SpireModManager/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for p in data.get("response", {}).get("players", []):
                sid = str(p.get("steamid", ""))
                name = p.get("personaname", "")
                if sid and name:
                    result[sid] = name
        except Exception as e:
            _log.debug("[steamapi] resolve_steamids batch %d failed: %s", i // 100, e)
    return result


def fetch_workshop_details(workshop_ids: list[str], *, timeout: int = 15, language: str = "zh") -> dict[str, SteamModInfo]:
    """批量查询 Steam 工坊详情。每 100 个一批，失败静默跳过。

    language: 用户界面语言（"zh"/"en"），也直接传 Steam 格式（"schinese"/"english"）兼容。
      内部统一经 _to_steam_lang() 转成 Steam API 格式后再 POST。

    返回 {workshop_id: SteamModInfo}。完全没查到的 workshop_id 不在结果里。
    """
    steam_lang = _to_steam_lang(language)

    # 去重 + 保序
    seen: set[str] = set()
    unique: list[str] = []
    for wid in workshop_ids:
        wid = str(wid or "").strip()
        if not wid or wid in seen:
            continue
        seen.add(wid)
        unique.append(wid)
    if not unique:
        return {}

    result: dict[str, SteamModInfo] = {}
    batch_size = 100
    for i in range(0, len(unique), batch_size):
        batch = unique[i : i + batch_size]
        _throttle()  # 复用 workshop 的全局节流（1.2s + 窗口 60 次）
        data = {"itemcount": len(batch), "language": steam_lang}
        for idx, wid in enumerate(batch):
            data[f"publishedfileids[{idx}]"] = wid
        raw = _http_post_form(PUBLISHED_FILE_DETAILS_URL, data, timeout=timeout)
        batch_result = _normalize_result(raw)
        result.update(batch_result)
    _log.info("[P0] fetch_workshop_details: lang=%s 请求 %d 个, 有效返回 %d 个",
              steam_lang, len(unique), len(result))
    return result


# ----------------------------------------------------------- 磁盘缓存层 --


_CACHE_DIR_NAME = Path("cache") / "steam_workshop"


def _get_cache_dir() -> Optional[Path]:
    try:
        # 相对本文件的 ../../data/cache/steam_workshop
        root = Path(__file__).resolve().parent.parent / "data" / _CACHE_DIR_NAME
        root.mkdir(parents=True, exist_ok=True)
        return root
    except Exception:
        return None


def clear_single_cache(workshop_id: str) -> bool:
    """删除单个 workshop 的 steam_webapi 缓存文件（json）。返回是否实际删除。"""
    wid = str(workshop_id or "").strip()
    if not wid:
        return False
    root = _get_cache_dir()
    if root is None:
        return False
    f = root / f"{wid}.json"
    if not f.is_file():
        return False
    try:
        f.unlink()
        _log.info("[steam_webapi] 已删除单个缓存: %s", f)
        return True
    except Exception as e:
        _log.warning("[steam_webapi] 删除单个缓存失败 wid=%s: %s", wid, e)
        return False


def read_cached_details(workshop_ids: list[str], language: str = "zh") -> tuple[dict[str, SteamModInfo], list[str]]:
    """从磁盘缓存读；返回 (命中, 待在线查询)。

    language: 用户界面语言（"zh"/"en"），内部经 _to_steam_lang() 转成 Steam 格式后再和缓存 _lang 字段比。
    2026-11-01: 缓存 key 加 language 检查——旧缓存没 _lang 字段或语言不匹配 → 当 miss 重抓。
    2026-11-02: 缓存文件存在且 result=1 就算超 TTL 也先用（降级命中），避免每次重启全量 miss 触发 429。
    """
    steam_lang = _to_steam_lang(language)
    cache_dir = _get_cache_dir()
    hit: dict[str, SteamModInfo] = {}
    miss: list[str] = []
    for wid in workshop_ids:
        wid = str(wid or "").strip()
        if not wid:
            continue
        if cache_dir is None:
            miss.append(wid)
            continue
        f = cache_dir / f"{wid}.json"
        if f.is_file():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                # 缓存里的 _lang 必须匹配当前请求的 Steam 格式语言
                cached_lang = raw.get("_lang")
                if cached_lang and cached_lang != steam_lang:
                    miss.append(wid)
                    continue
                ts = int(raw.get("_cache_ts") or 0)
                # 正常命中：TTL 内
                if time.time() - ts < CACHE_TTL_SECONDS:
                    info = SteamModInfo(**{k: v for k, v in raw.items() if k not in ("_cache_ts", "_lang")})
                    hit[wid] = info
                    continue
                # 降级命中：缓存超 TTL 但文件存在且 result=1（比全量 miss 好）
                if raw.get("result") == 1:
                    info = SteamModInfo(**{k: v for k, v in raw.items() if k not in ("_cache_ts", "_lang")})
                    hit[wid] = info
                    continue
            except Exception:
                pass
        miss.append(wid)
    return hit, miss


def write_cached_details(infos: dict[str, SteamModInfo], language: str = "zh") -> None:
    """写入磁盘缓存（每个 workshop_id 一个 json 文件）。

    language: 用户界面语言（"zh"/"en"），内部经 _to_steam_lang() 转成 Steam 格式后存入 _lang 字段。
    读缓存时语言不匹配会自动重抓。
    """
    steam_lang = _to_steam_lang(language)
    cache_dir = _get_cache_dir()
    if cache_dir is None:
        return
    now = int(time.time())
    for wid, info in infos.items():
        try:
            payload = {
                "workshop_id": wid,
                "result": info.result,
                "title": info.title,
                "description": info.description,
                "author_steam_id": info.author_steam_id,
                "subscriptions": info.subscriptions,
                "votes_up": info.votes_up,
                "votes_down": info.votes_down,
                "time_updated": info.time_updated,
                "visibility": info.visibility,
                "tags": info.tags,
                "preview_url": info.preview_url,
                "_cache_ts": now,
                "_lang": steam_lang,
            }
            (cache_dir / f"{wid}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            _log.debug("写 Steam 缓存失败 %s: %s", wid, e)


def get_workshop_batch_info(
    workshop_ids: list[str],
    *,
    force_refresh: bool = False,
    language: str = "zh",
) -> dict[str, SteamModInfo]:
    """带缓存的批量入口：查多个 workshop 的元数据（time_updated / title / subs 等）。

    与 workshop.fetch_workshop_preview 的区别：
      - 批量（list[str]）vs 单个（str）
      - 轻量元数据 vs 完整预览（多图/标签/奖励/描述）
      - 24h 缓存 vs 长期磁盘缓存

    language: 用户界面语言（"zh"/"en"），内部统一经 _to_steam_lang() 转 Steam 格式。
    force_refresh=True 忽略所有缓存直接在线拉。
    """
    if force_refresh:
        missing = list({str(w or "").strip() for w in workshop_ids if str(w or "").strip()})
        hit = {}
    else:
        hit, missing = read_cached_details(workshop_ids, language=language)
    if missing:
        online = fetch_workshop_details(missing, language=language)
        write_cached_details(online, language=language)
        hit.update(online)
    return hit


# ------------------------------------------------------ 本地 mtime 对比 --


def enrich_with_local_mtime(
    infos: dict[str, SteamModInfo],
    mods_dir_map: dict[str, Optional[Path]],
) -> dict[str, SteamModInfo]:
    """给 SteamModInfo 补上本地 mod 目录的 mtime，让 update_status 生效。

    mods_dir_map: {workshop_id: 本地路径}，路径为 None 表示 mod 不在磁盘上。
    """
    for wid, info in infos.items():
        local_path = mods_dir_map.get(wid)
        if local_path is None:
            info.local_mtime = None
            info.update_status = "unknown"
            continue
        try:
            info.local_mtime = int(local_path.stat().st_mtime)
        except Exception:
            info.local_mtime = None
        # 判定更新状态
        if not info.is_valid:
            info.update_status = "offline"
        elif info.has_update:
            info.update_status = "has-update"
        else:
            info.update_status = "up-to-date"
    return infos
