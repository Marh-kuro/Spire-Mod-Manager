from __future__ import annotations

import html
import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .logger import get_logger
from .steam_webapi import _to_steam_lang

_log = get_logger("workshop")

# RimCrow 式：Steam Web API (language=schinese) 直接返回中文 title/description/tags
# → 砍掉整套社区页面 HTML 爬取 + 反爬风控 + 签名索引（2026-11-03 重构）
# → 唯一保留的 HTML 爬取是 fetch_workshop_dependencies（依赖链 API 无 key 不返回）
# → 依赖链下次 build_steamdb.py 加 children 字段后也能零 HTTP

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

_CACHE_VERSION = 3  # v3 缓存；HTML fallback 在缓存命中后按需触发，不需要 version bump 强制重抓

# 缓存策略（7 天、1 天）
_DEPS_CACHE_TTL = 7 * 24 * 3600    # 依赖缓存 7 天（workshop 依赖极少变化）
_META_CACHE_TTL = 24 * 3600        # 元数据缓存 1 天（标题/描述/标签偶尔更新）
_TITLE_RETRY_TTL = 7 * 24 * 3600   # 标题轻量缓存失败时 7 天后重试


# ═══════════════════════════════════════════════════════════════════════════
# 社区页 HTML 爬取节流（两种场景共用一个节流器）：
#   A. fetch_workshop_dependencies   —— Steam Web API 无 key 不返回 dependencies
#   B. fetch_workshop_preview desc 回退 —— zh 界面 + API desc 无中文 → 社区翻译补中文
# 标题/tags/preview_url 全走 Web API 零风控。
#
# 串行间隔可配置：将来如果 mod 数量 > 500 且批量补全慢，可切换为
# asyncio.Semaphore(3~5) 并发 + 保留这个节流器做最小间隔兜底。
# ═══════════════════════════════════════════════════════════════════════════

_HTML_LAST_TS = 0.0
_HTML_MIN_INTERVAL = 2.0   # 社区页爬取最小间隔（秒）—— Steam 社区页单 IP 限速阈值较严，2.0s 更安全
_HTML_MAX_RECHECK_DAYS = 30  # 预判层最大重检间隔 —— 社区翻译是其他用户贡献的，
                              # 不触发 mod time_updated 变化，30 天强制重检一次

# ── HTML 熔断：检测到 429/403 → 冷却期内所有 HTML 请求直接返回 None ──
_HTML_CIRCUIT_OPEN_TS = 0.0   # 熔断开启的时间戳
_HTML_CIRCUIT_COOLDOWN = 600.0  # 冷却 10 分钟
_html_inflight: set[str] = set()  # 正在爬的 wid（去重）
_html_lock = threading.Lock()
_html_sem = threading.Semaphore(1)  # 并发上限：desc 回退路径同一时刻只有 1 个后台爬


def _html_circuit_open() -> bool:
    """HTML 熔断是否开启（冷却期内）。"""
    if _HTML_CIRCUIT_OPEN_TS <= 0:
        return False
    if time.monotonic() < _HTML_CIRCUIT_OPEN_TS + _HTML_CIRCUIT_COOLDOWN:
        return True
    # 冷却期过了 → 自动关闭熔断
    _log.info("[workshop] HTML 熔断冷却结束，恢复请求")
    globals()['_HTML_CIRCUIT_OPEN_TS'] = 0.0
    return False


def _html_circuit_trigger() -> None:
    """触发 HTML 熔断（检测到 429/403）。"""
    globals()['_HTML_CIRCUIT_OPEN_TS'] = time.monotonic()
    _log.warning("[workshop] ⚠️ HTML 熔断触发！冷却 %.0fs（社区页被风控）", _HTML_CIRCUIT_COOLDOWN)


def _html_throttle() -> None:
    """社区页 HTML 爬取统一节流（2.0s 间隔）。deps 和 desc 共用。"""
    if _html_circuit_open():
        raise RuntimeError("HTML circuit breaker open — cooldown in effect")
    global _HTML_LAST_TS
    now = time.monotonic()
    delay = _HTML_LAST_TS + _HTML_MIN_INTERVAL - now
    if delay > 0:
        _log.debug("[workshop] throttle wait %.2fs", delay)
        time.sleep(delay)
    _HTML_LAST_TS = time.monotonic()


# 兼容旧名字
_deps_throttle = _html_throttle


# ──────────────────────────────────────────────────────────────── 辅助函数 ──
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_TOTAL_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")  # CJK 统一符号（中文标点等）

_HTML_ZH_MIN_RATIO = 0.10  # desc 中文占比低于这个阈值 → 主体不是中文，触发 HTML 补全
                           # 作者可能在英文 desc 里混了几个字（QQ群号、昵称），
                           # 让简单的 _has_chinese() 误判为"已有中文"


def _has_enough_chinese(s: str) -> bool:
    """判断 desc 的中文占比是否"足够多"（≥ 10%）。

    作者可能在全英文 desc 里混了几个中文（QQ群号、B 站昵称 / 版本兼容性），
    简单的 _has_chinese() 会误判为"已有中文，跳过 HTML 补全"。
    这个函数看的是**主体语言**：如果中文占比 < 10%，说明 desc 主体还是英文。
    """
    text = (s or "").strip()
    if len(text) < 50:  # 太短不判断（可能是标题或空壳）
        return bool(_CHINESE_RE.search(text))  # 退回简单判断
    zh_chars = len(_CHINESE_RE.findall(text))
    cjk_chars = len(_CJK_TOTAL_RE.findall(text))
    # 用 CJK 字符数（中文 + 标点）除以总字符数
    # 标点占比一般不高，主要反映主体是不是中文
    ratio = cjk_chars / len(text)
    return ratio >= _HTML_ZH_MIN_RATIO


# 兼容旧名字（只有一处调用）
_has_chinese = _has_enough_chinese


def _bbcode_to_html(text: str) -> str:
    """Steam BBCode → 安全 HTML。支持 h1/h2/h3、b/i/u/s、list/*、img、url、quote。

    核心：先 html.escape() 转义所有内容（防 XSS），再替换 BBCode。
    所有 href/src 都来自 Steam/作者原始内容，保持 escape 状态不会引入安全问题。
    """
    import html as _html
    text = _html.escape(text)

    # 图片
    text = re.sub(
        r'\[img\](.*?)\[/img\]',
        lambda m: f'<img class="bb-img" src="{m.group(1).strip()}" onerror="this.style.display=\'none\'"/>',
        text, flags=re.S,
    )
    # URL [url=href]text[/url]
    text = re.sub(
        r'\[url=([^\]]+)\](.*?)\[/url\]',
        lambda m: f'<a href="{m.group(1).strip()}" target="_blank" rel="noopener">{m.group(2).strip()}</a>',
        text, flags=re.S,
    )
    # URL [url]href[/url]
    text = re.sub(
        r'\[url\](.*?)\[/url\]',
        lambda m: f'<a href="{m.group(1).strip()}" target="_blank" rel="noopener">{m.group(1).strip()}</a>',
        text, flags=re.S,
    )
    # 标题 h3 → h2 → h1（顺序重要：先换深层级）
    for lvl in (3, 2, 1):
        text = re.sub(
            rf'\[h{lvl}\](.*?)\[/h{lvl}\]',
            lambda m, l=lvl: f'<h{l} class="bb-h{l}">{m.group(1).strip()}</h{l}>',
            text, flags=re.S,
        )
    # 行内样式
    text = re.sub(r'\[b\](.*?)\[/b\]', r'<strong>\1</strong>', text, flags=re.S)
    text = re.sub(r'\[i\](.*?)\[/i\]', r'<em>\1</em>', text, flags=re.S)
    text = re.sub(r'\[u\](.*?)\[/u\]', r'<u>\1</u>', text, flags=re.S)
    text = re.sub(r'\[s\](.*?)\[/s\]', r'<s>\1</s>', text, flags=re.S)
    # 列表
    text = re.sub(r'\[list\]', '<ul class="bb-list">', text, flags=re.I)
    text = re.sub(r'\[/list\]', '</ul>', text, flags=re.I)
    text = re.sub(r'\[\*\]', '<li>', text)
    # quote
    text = re.sub(r'\[quote\](.*?)\[/quote\]', r'<blockquote class="bb-quote">\1</blockquote>', text, flags=re.S)
    # 换行
    text = text.replace('\n', '<br/>')
    return text


def _extract_html_description(html_text: str) -> Optional[str]:
    """从社区页 HTML 里抽 workshopItemDescription 的 BBCode 正文。

    关键：workshopItemDescription 内部嵌套了很多 <div class="bb_h1/h2/h3"> 等，
    不能用 (.*?)</div> 非贪婪匹配——它会在第一个子 div 就停，只抓到开头几行。
    必须数 <div> 配对找到真正的闭合标签。

    注意：从 HTML 里抽出来的内容是作者填的 BBCode 渲染后的 HTML（含 <br/>、<div class="bb_h1"> 等），
    我们要把它还原回 BBCode（去掉 div/span/br 等 HTML 包装），再交给 _bbcode_to_html 统一转换。
    """
    # 找到开始标签的位置
    start_pat = re.compile(r'<div[^>]*class="workshopItemDescription"[^>]*>', re.I)
    start_m = start_pat.search(html_text)
    if not start_m:
        return None
    # 从开始标签后面开始，数 div 配对
    pos = start_m.end()
    depth = 1
    div_open = re.compile(r'<div[\s/>]', re.I)
    div_close = re.compile(r'</div\s*>', re.I)
    while depth > 0 and pos < len(html_text):
        next_open = div_open.search(html_text, pos)
        next_close = div_close.search(html_text, pos)
        if not next_close:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            pos = next_close.end()
    content = html_text[start_m.end():pos]
    if not content:
        return None

    # ── 把 Steam 渲染后的 HTML 还原回 BBCode ──
    # Steam 把 [h1]xxx[/h1] 渲染成 <div class="bb_h1">xxx</div>
    # 把 <div class="bb_h1">...</div> 转回 [h1]...[/h1]
    for lvl in (3, 2, 1):
        content = re.sub(
            rf'<div\s+class="bb_h{lvl}"[^>]*>(.*?)</div>',
            lambda m, l=lvl: f'[h{lvl}]{m.group(1).strip()}[/h{lvl}]',
            content, flags=re.S + re.I,
        )
    # <br> / <br/> → \n
    content = re.sub(r'<br\s*/?>', '\n', content, flags=re.I)
    # <img src="..."/> → [img]...[/img]
    content = re.sub(
        r'<img[^>]*src="([^"]+)"[^>]*/?>',
        lambda m: f'[img]{m.group(1).strip()}[/img]',
        content, flags=re.I,
    )
    # <a href="...">text</a> → [url=...]text[/url]
    content = re.sub(
        r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
        lambda m: f'[url={m.group(1).strip()}]{m.group(2).strip()}[/url]',
        content, flags=re.S + re.I,
    )
    # <ul class="bb_list">...</ul> → [list]...[/list]
    content = re.sub(r'<ul[^>]*>', '[list]', content, flags=re.I)
    content = re.sub(r'</ul>', '[/list]', content, flags=re.I)
    # <li> → [*]
    content = re.sub(r'<li[^>]*>', '[*]', content, flags=re.I)
    content = re.sub(r'</li>', '', content, flags=re.I)
    # <strong>/<b> → [b]
    content = re.sub(r'<(?:strong|b)[^>]*>', '[b]', content, flags=re.I)
    content = re.sub(r'</(?:strong|b)>', '[/b]', content, flags=re.I)
    # <em>/<i> → [i]
    content = re.sub(r'<(?:em|i)[^>]*>', '[i]', content, flags=re.I)
    content = re.sub(r'</(?:em|i)>', '[/i]', content, flags=re.I)
    # <blockquote> → [quote]
    content = re.sub(r'<blockquote[^>]*>', '[quote]', content, flags=re.I)
    content = re.sub(r'</blockquote>', '[/quote]', content, flags=re.I)

    # 去剩余的 HTML tag（杂项 div/span 等）
    content = re.sub(r'<[^>]+>', '', content)
    # unescape &amp; &nbsp; 等
    content = html.unescape(content)
    content = content.strip()
    return content or None


# 保留旧封禁文件兼容读取（不再写新的）
_RATE_LIMITED_FILE = Path(__file__).resolve().parent.parent / "data" / "rate_limited.json"


def _load_rate_limited() -> None:
    """一次性清理旧 rate_limited.json（重构后废弃，静默删除）。"""
    try:
        if _RATE_LIMITED_FILE.is_file():
            _RATE_LIMITED_FILE.unlink()
    except Exception:
        pass


# 砍签名索引（2026-11-03 重构）——签名索引用 HTML 对比版本号完全多余，
# steamDB.json 的 revision_change_number + time_updated 更准且零 HTTP。
# 旧索引文件已移到 archive/2026-11-03_html_crawl_removal/workshop_html_signatures.json

# 启动时加载封禁状态（import 时触发）
_load_rate_limited()

# ------------------------------------------------------ 永久失败标记 --
# 某些 mod 可能真的被 Steam 下架 / API 不再返回 title。
# 记录连续 N 次 API 拿不到 title 的 wid → 永久跳过，防止死循环重跑。
# 用户手动右键"重新获取"时可清掉失败计数强制重试。

_API_PERMA_FAIL_FILE = Path(__file__).resolve().parent.parent / "data" / "api_perma_fail.json"
_API_PERMA_FAIL_THRESHOLD = 3  # 连续 3 次 API 拿不到 title → 永久跳过
_api_perma_fail: dict[str, int] = {}  # wid → 失败次数


def _load_api_perma_fail() -> None:
    global _api_perma_fail
    try:
        if _API_PERMA_FAIL_FILE.is_file():
            _api_perma_fail = json.loads(_API_PERMA_FAIL_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass


def _persist_api_perma_fail() -> None:
    try:
        _API_PERMA_FAIL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _API_PERMA_FAIL_FILE.write_text(
            json.dumps(_api_perma_fail, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        _log.warning("[workshop] 持久化 API 永久失败标记失败: %s", e)


def _is_api_perma_failed(wid: str) -> bool:
    return _api_perma_fail.get(wid, 0) >= _API_PERMA_FAIL_THRESHOLD


def _mark_api_fail(wid: str) -> int:
    """API 对这个 wid 没返回 title 时调一次。返回新的失败计数。"""
    _api_perma_fail[wid] = _api_perma_fail.get(wid, 0) + 1
    _persist_api_perma_fail()
    return _api_perma_fail[wid]


def _clear_api_fail(wid: str) -> None:
    """右键刷新等场景手动清掉失败计数，允许重试。"""
    if wid in _api_perma_fail:
        del _api_perma_fail[wid]
        _persist_api_perma_fail()


# ═══════════════════════════════════════════════════════════════════════════
# 砍 HTML 签名索引（2026-11-03 重构）
# 旧索引文件已移到 archive/2026-11-03_html_crawl_removal/workshop_html_signatures.json
# 旧索引函数：_html_sig_is_fetched / _html_sig_mark_fetched / _html_sig_seed_version
# 旧持久化：_HTML_SIG_FILE / _html_signatures / _load_html_signatures / _persist_html_signatures
# 原因：签名索引用 HTML 对比版本号完全多余，steamDB.json 的 revision_change_number 更准
# ═══════════════════════════════════════════════════════════════════════════

_load_api_perma_fail()

# 磁盘缓存根目录：<项目>/data/cache/workshop/<workshop_id>/
# 每个 mod 存：
#   preview.<ext>      宣传图原始二进制（各语言共用，同一张图）
#   meta.<lang>.json   按语言分开的文本元数据（标题/描述/标签/统计）
#   deps.json          依赖列表（语言无关）
# 双语并存的好处：切换界面语言时直接命中另一语言缓存，无需重新请求 Steam。
_CACHE_ROOT = Path(__file__).resolve().parent.parent / "data" / "cache" / "workshop"

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# 批量重新下载预览的进度状态（供前端右上角进度条轮询）
_progress: dict = {"done": 0, "total": 0}
_progress_lock = threading.Lock()


def reset_workshop_progress(total: int) -> None:
    with _progress_lock:
        _progress["done"] = 0
        _progress["total"] = max(0, int(total))


def get_workshop_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def tick_workshop_progress() -> None:
    with _progress_lock:
        _progress["done"] += 1


# ---- 后台两阶段补全进度 ----
# 阶段 2（慢速）：爬 HTML 补中文标题/screenshots/stats（3s 间隔，反爬风险）
_bg_progress: dict = {
    "phase1_done": 0, "phase1_total": -1, "phase1_running": False, "phase1_ok": False,
    "phase2_done": 0, "phase2_total": -1, "phase2_running": False, "phase2_ok": False,
}
_bg_progress_lock = threading.Lock()


def get_bg_progress() -> dict:
    """返回后台两阶段补全进度（供前端进度条轮询）。"""
    with _bg_progress_lock:
        return dict(_bg_progress)


def _set_bg_progress(**kwargs) -> None:
    with _bg_progress_lock:
        for k, v in kwargs.items():
            _bg_progress[k] = v


def reset_bg_progress() -> None:
    """完全重置两阶段进度（每次启动后台补全时调用）。
    phase1_total/phase2_total 设为 -1 表示「正在计算需要多少个」，前端据此显示模糊文案。
    """
    with _bg_progress_lock:
        _bg_progress.update({
            "phase1_done": 0, "phase1_total": -1,
            "phase1_running": True, "phase1_ok": False,
            "phase2_done": 0, "phase2_total": -1,
            "phase2_running": False, "phase2_ok": False,
        })


# ═══════════════════════════════════════════════════════════════════════════
# 砍 HTML 入队队列（2026-11-03 重构）
# 旧代码：enqueue_html_fill / _html_fill_worker / _merge_html_into_cache / needs_title_fetch
# 旧数据：_html_fill_queue / _html_fill_pending / _html_fill_lock / _html_fill_cv / queue import
# 原因：Web API (language=schinese) 直接返回中文 title/desc/tags/preview_url — 零 HTML 爬取
#       唯一保留的 HTML 爬取是 fetch_workshop_dependencies
# ═══════════════════════════════════════════════════════════════════════════


def _fetch_html(workshop_id: str, language: str = "zh") -> tuple[Optional[str], str]:
    """爬 Steam 社区页 HTML（desc fallback / deps 共用）。带节流 + 429 熔断 + 分级退避。

    返回 (html_text, reason):
      - 成功: (HTML 字符串, "ok")
      - 失败: (None, 原因码)
        原因码:
          "circuit_open"    — 熔断已开（入口或循环中被别的 mod 触发）
          "429_circuit"     — 429 重试耗尽 → 本 mod 触发了熔断
          "403_circuit"     — 403 → 本 mod 触发了熔断
          "http_<code>"     — 其他 HTTP 错误
          "connection"      — 连接重置重试耗尽
          "exception"       — 其他异常

    调用方靠 reason 判断"本次失败算不算真没中文"，不靠事后查全局熔断——
    避免"别的 mod 触发熔断导致这个 mod 被误判"的竞态。
    """
    try:
        _deps_throttle()  # 内部会检查熔断
    except RuntimeError:
        _log.debug("[workshop] wid=%s 熔断开启，跳过 HTML 爬取", workshop_id)
        return None, "circuit_open"

    steam_lang = _to_steam_lang(language)
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}&l={steam_lang}"
    headers = {
        "User-Agent": _UA,
        # Accept-Encoding 不加 — urllib 默认处理 Content-Encoding，
        # 加了反而可能遇到解压兼容问题，收益（15KB→3KB）不值得
    }

    max_retries = 2
    for attempt in range(max_retries + 1):
        if _html_circuit_open():
            return None, "circuit_open"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "ignore"), "ok"
        except urllib.error.HTTPError as he:
            if he.code == 429:
                # 指数退避：0.5s → 1s
                if attempt < max_retries:
                    wait = 0.5 * (2 ** attempt)
                    _log.warning("[workshop] wid=%s HTTP 429，%.1fs 后重试 (%d/%d)",
                                 workshop_id, wait, attempt + 1, max_retries)
                    time.sleep(wait)
                    _deps_throttle()
                    continue
                _html_circuit_trigger()
                _log.warning("[workshop] wid=%s HTTP 429 重试耗尽，触发熔断", workshop_id)
                return None, "429_circuit"
            if he.code == 403:
                _html_circuit_trigger()
                _log.warning("[workshop] wid=%s HTTP 403，触发熔断", workshop_id)
                return None, "403_circuit"
            _log.warning("[workshop] wid=%s HTML 爬取失败: HTTP %d", workshop_id, he.code)
            return None, f"http_{he.code}"
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as ce:
            if attempt < max_retries:
                time.sleep(0.5)
                _log.debug("[workshop] wid=%s 连接重置，0.5s 后重试 (%d/%d)",
                           workshop_id, attempt + 1, max_retries)
                continue
            _log.warning("[workshop] wid=%s HTML 连接失败: %s", workshop_id, ce)
            return None, "connection"
        except Exception as e:
            _log.warning("[workshop] wid=%s HTML 爬取异常: %s", workshop_id, e)
            return None, "exception"


def _launch_async_html_fill(workshop_id: str, language: str, *,
                           api_time_updated: int = 0) -> None:
    """后台 daemon 线程：爬 HTML 社区页补中文 desc，落盘后前端轮询自动刷新。

    预判层（零网络成本）：
      - 缓存已有中文 desc → 不爬
      - html_checked=True 且 html_time_updated 和 Web API/steamDB 一致 → mod 没更新过
        额外检查：距上次 HTML 检查 < 30 天 → 真跳过
        （30 天过了强制重检 —— 社区翻译是其他用户贡献的，不触发 mod time_updated）
      - steamDB 查询零 HTTP（1.4MB 本地 JSON）

    风控层：
      - 熔断开启 → 不启动（冷却 10 分钟）
      - 同一个 wid 正在爬 → 不重复启动（去重）
      - _html_throttle 1.5s 间隔 → 多线程串行
      - _fetch_html 分级退避（429 指数重试 → 连接重置短延时 → 超时 30s）

    失败不永久放弃：
      - 429/403 熔断耗尽 → 不标记 html_checked=True，只记 html_last_checked_ts
        （下次预览或批量补全还能再试）
      - 其他失败（社区真没中文、HTML 解析失败）→ 正常标记 html_checked=True
    """
    if _html_circuit_open():
        _log.debug("[workshop] wid=%s 熔断开启，跳过后台 HTML 补全", workshop_id)
        return
    with _html_lock:
        if workshop_id in _html_inflight:
            _log.debug("[workshop] wid=%s 后台 HTML 已在进行中，跳过", workshop_id)
            return
        _html_inflight.add(workshop_id)

    def _do():
        with _html_sem:
            try:
                # ── 预判层：time_updated 没变 + 30 天内检查过 → 不爬 ──
                check_time = int(api_time_updated or 0)
                if check_time <= 0:
                    # 没传 Web API 的值 → 查 steamDB（零网络成本）
                    try:
                        from .steam_webapi import query_steamdb
                        snap = query_steamdb(workshop_id)
                        if snap:
                            check_time = int(snap.get("time_updated") or 0)
                    except Exception:
                        pass

                cached_before = _load_cached(workshop_id, language)
                if cached_before and check_time > 0 and cached_before.get("html_checked"):
                    cached_time = cached_before.get("html_time_updated")
                    if cached_time == check_time:
                        # mod 没更新过，再看距上次检查多久
                        last_checked = float(cached_before.get("html_last_checked_ts") or 0)
                        days_since = max(0, (time.time() - last_checked) / 86400) if last_checked > 0 else 0
                        if days_since < _HTML_MAX_RECHECK_DAYS:
                            from datetime import datetime, timezone
                            ts_str = datetime.fromtimestamp(last_checked, tz=timezone.utc).strftime("%Y-%m-%d") if last_checked > 0 else "never"
                            _log.debug("[workshop] wid=%s time_updated 没变 (%d) 且 %.1f 天内检查过 (上次=%s) → 跳过",
                                       workshop_id, check_time, days_since, ts_str)
                            return
                        _log.debug("[workshop] wid=%s time_updated 没变 (%d) 但已过 %.0f 天 → 强制重检",
                                   workshop_id, check_time, days_since)

                # ── 正式爬取 ──
                html_text, fetch_reason = _fetch_html(workshop_id, language)

                # 只有 fetch_reason=="ok" 且提取到中文 desc 才算成功
                # 任何其他情况都走到下面的"判断是否临时失败"分支
                if fetch_reason == "ok" and html_text is not None:
                    html_desc = _extract_html_description(html_text)
                    if html_desc and _has_chinese(html_desc):
                        cached = _load_cached(workshop_id, language)
                        if cached:
                            cached["description"] = html_desc
                            cached["desc_src"] = "html"
                            cached["html_checked"] = True
                            cached["html_time_updated"] = check_time
                            cached["html_last_checked_ts"] = time.time()
                            _save_cache(workshop_id, cached, None, "image/jpeg")
                            _log.info("[workshop] wid=%s 后台 HTML 补中文 desc 完成", workshop_id)
                            return

                # 到这里: 要么 fetch_reason!="ok"（临时失败），要么 fetch_reason=="ok"
                # 但社区 HTML 里确实没有中文 desc（作者没填 + 社区翻译系统也没覆盖）
                cached = _load_cached(workshop_id, language)
                if not cached:
                    return

                # 判断是不是临时失败（值得保留重试机会，不标记 html_checked=True）
                # 临时: 熔断类、HTTP 错误、连接抖动、任意 exception（偶发异常按临时处理）
                # 只有 fetch_reason=="ok" 但 HTML 里真没中文 → 唯一确认"没翻译"的路径
                _TEMP_FAILURES = {"429_circuit", "403_circuit", "circuit_open", "connection", "exception"}
                is_temp_failure = (
                    fetch_reason in _TEMP_FAILURES
                    or fetch_reason.startswith("http_")
                )

                if is_temp_failure:
                    cached["html_last_checked_ts"] = time.time()
                    cached.pop("html_checked", None)  # 删掉 → 下次还能进预判层
                    _save_cache(workshop_id, cached, None, "image/jpeg")
                    _log.warning("[workshop] wid=%s HTML 临时失败 (reason=%s) → 不标记已检查，保留重试机会",
                                 workshop_id, fetch_reason)
                else:
                    # 正常情况：社区真没中文 desc 或 HTML 解析失败 → 标记已检查
                    cached["html_checked"] = True
                    cached["html_time_updated"] = check_time
                    cached["html_last_checked_ts"] = time.time()
                    _save_cache(workshop_id, cached, None, "image/jpeg")
                    _log.debug("[workshop] wid=%s 后台 HTML 未拿到中文 desc（已标记跳过, reason=%s）",
                               workshop_id, fetch_reason)
            except Exception as e:
                _log.debug("[workshop] wid=%s 后台 HTML 补全失败: %s", workshop_id, e)
            finally:
                with _html_lock:
                    _html_inflight.discard(workshop_id)

    threading.Thread(target=_do, daemon=True).start()


def _download_image_bytes(url: str, max_bytes: int = 2_000_000, retries: int = 2) -> Optional[tuple[bytes, str]]:
    """下载图片，返回 (原始字节, content-type)。用原始二进制存储，最小占用。

    CDN（images.steamusercontent.com）无反爬，**不走社区页面节流器**。
    国内访问 Steam CDN 偶发超时，内置重试（默认 2 次）+ 30s 超时。

    max_bytes 默认 2MB：Steam 工坊预览图大多 200KB-1.2MB，但也有少数正好卡在旧阈值
    800KB 以上（比如 BetterSpire2 恰好 800,001 字节），旧阈值判成超大丢掉。
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Referer": "https://steamcommunity.com/"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read(max_bytes + 1)
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                if len(data) > max_bytes:
                    _log.debug("[workshop] 图片过大跳过 (%d bytes) %s", len(data), url)
                    return None
                return data, ctype
        except urllib.error.HTTPError as he:
            # 注意：CDN（images.steamusercontent.com）的 429 不触发全局封禁！
            # 那是社区页面（steamcommunity.com）反爬墙才应该停全局。
            # CDN 429 通常是临时节点波动，让这个 wid 的图片失败就行，
            # 不要连累后续 HTML 补全通道。
            _log.warning("[workshop] 图片下载失败 %s: HTTP %d", url, he.code)
            return None  # HTTP 错误不重试
        except Exception as e:
            if attempt < retries:
                _log.debug("[workshop] 图片下载重试 (%d/%d) %s: %s", attempt + 1, retries, url, e)
                time.sleep(1.0 + attempt)  # 退避 1s, 2s
                continue
            _log.warning("[workshop] 图片下载失败 %s: %s", url, e)
            return None


def fetch_workshop_dependencies(workshop_id: str, timeout: int = 15) -> Optional[list[dict]]:
    """爬 Steam 工坊页面拿依赖列表（HTML 里服务端直接渲染，不需要执行 JS）。

    2026-11-03 重构：这是**唯一**保留的 HTML 爬取场景——Steam Web API 无 key 不返回 dependencies。
    下次 build_steamdb.py 加 children 字段后 → 优先 steamDB 零 HTTP。

    返回 list[{"workshop_id": "...", "name": "..."}]。失败或没依赖返回 None。
    缓存 7 天（依赖极少变化）。
    """
    wid = str(workshop_id)

    # 优先 steamDB（如果下次重拉加了 children）
    try:
        from .steam_webapi import query_steamdb
        snap = query_steamdb(wid)
        if snap and snap.get("children"):
            children = snap["children"]
            if isinstance(children, list) and children:
                deps = []
                for c in children:
                    if isinstance(c, dict):
                        cid = str(c.get("publishedfileid") or c.get("workshop_id") or "")
                        cname = c.get("title") or c.get("name") or ""
                        if cid:
                            deps.append({"workshop_id": cid, "name": cname})
                if deps:
                    _log.debug("[deps] steamDB 命中 wid=%s → %d 个依赖", wid, len(deps))
                    return deps
    except Exception:
        pass

    # 缓存（7 天内新鲜 → 直接返回）
    _migrate_legacy_meta(wid)
    cached = _read_deps(wid)
    if cached and "dependencies" in cached:
        fetched_at = cached.get("deps_fetched_at", 0)
        if time.time() - fetched_at < _DEPS_CACHE_TTL:
            return cached["dependencies"]

    # HTML 爬取（唯一保留场景）—— throttle 统一在 _fetch_html 内部
    html_text, _ = _fetch_html(wid)
    if html_text is None:
        return None

    # 4) 从 HTML 里解析 requiredItems 区域的链接
    #    同一依赖在页面里可能出现多次（缩略图 + 标题链接），按 workshop_id 去重
    deps: list[dict] = []
    seen_ids: set[str] = set()
    container_match = re.search(
        r'id="RequiredItems"[^>]*>(.*?)(?:</div>\s*</div>\s*</div>\s*<!--\s*created by|$)',
        html_text, re.DOTALL | re.IGNORECASE
    )
    if container_match:
        container_html = container_match.group(1)
        for link in re.finditer(
            r'href="https?://steamcommunity\.com/(?:workshop/)?filedetails/\?id=(\d+)"[^>]*>(.*?)</a>',
            container_html, re.DOTALL | re.IGNORECASE
        ):
            dep_wid = link.group(1)
            dep_name = html.unescape(re.sub(r'<[^>]+>', '', link.group(2))).strip()
            if dep_wid and dep_wid not in seen_ids:
                seen_ids.add(dep_wid)
                deps.append({"workshop_id": dep_wid, "name": dep_name})

    # 5) 写缓存
    try:
        d = _cache_dir(wid)
        d.mkdir(parents=True, exist_ok=True)
        _deps_path(wid).write_text(json.dumps({
            "dependencies": deps,
            "deps_fetched_at": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log.warning("[workshop] 依赖缓存写失败: %s", e)

    return deps


def _cache_dir(workshop_id: str) -> Path:
    return _CACHE_ROOT / workshop_id


def _meta_path(workshop_id: str, language: str) -> Path:
    """某语言的 meta 文件路径：meta.<lang>.json"""
    return _cache_dir(workshop_id) / f"meta.{language}.json"


def _deps_path(workshop_id: str) -> Path:
    """依赖缓存文件路径（语言无关）：deps.json"""
    return _cache_dir(workshop_id) / "deps.json"


def _read_deps(workshop_id: str) -> Optional[dict]:
    p = _deps_path(workshop_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_deps_meta(workshop_id: str) -> dict:
    """读取依赖缓存原始结构（含 dependencies / deps_fetched_at）；无缓存返回 {}。"""
    return _read_deps(workshop_id) or {}


def _migrate_legacy_meta(workshop_id: str) -> None:
    """把旧版单文件 meta.json（v3 及以前）拆分迁移到新布局。

    - 文本元数据 → meta.<原language>.json（v3 缓存已带 language 字段）
    - 依赖数据   → deps.json
    迁移后删除旧文件，避免与新布局重复。
    """
    d = _cache_dir(workshop_id)
    legacy = d / "meta.json"
    if not legacy.is_file():
        return
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception:
        return
    try:
        lang = str(raw.get("language") or "zh")
        deps = raw.get("dependencies")
        if deps:
            deps_file = _deps_path(workshop_id)
            if not deps_file.is_file():
                deps_file.write_text(json.dumps({
                    "dependencies": deps,
                    "deps_fetched_at": raw.get("deps_fetched_at", 0),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
        text_meta = {k: v for k, v in raw.items() if k not in ("dependencies", "deps_fetched_at")}
        target = _meta_path(workshop_id, lang)
        if not target.is_file():
            target.write_text(json.dumps(text_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        legacy.unlink()
        _log.info("[workshop] 迁移旧缓存布局 id=%s → meta.%s.json", workshop_id, lang)
    except Exception as e:
        _log.warning("[workshop] 迁移旧缓存失败 id=%s: %s", workshop_id, e)


def clear_all_cache() -> int:
    """删除所有 Steam 工坊预览缓存。返回删除的目录数。

    与 clear_workshop_cache 完全同效（都删 _CACHE_ROOT），保留此名兼容调用方。
    """
    return clear_workshop_cache()


def _load_cached(workshop_id: str, language: str = "zh") -> Optional[dict]:
    """读取指定语言的预览缓存；不存在返回 None。

    设计：
      - 标题/描述/标签 → 按语言分 meta.<lang>.json
      - 预览图 → 多语言共用 preview.jpg/png，存在目录根
    """
    d = _cache_dir(workshop_id)
    if not d.is_dir():
        return None
    _migrate_legacy_meta(workshop_id)
    meta_path = _meta_path(workshop_id, language)
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for ext in (".jpg", ".png", ".gif", ".webp"):
        p = d / f"preview{ext}"
        if p.is_file():
            meta["preview"] = p.as_uri()
            break
    else:
        meta["preview"] = None
    return meta


def load_cached_any(workshop_id: str, prefer_language: str = "") -> Optional[dict]:
    """读取缓存，优先 prefer_language；该语言没有缓存时退到任意可用语言。

    用于「标题/描述/标签」这类只要有内容就够用的场景（如 mod 列表显示名、
    风控扫描），避免为了一个语言未缓存就去请求 Steam。
    """
    if prefer_language:
        cached = _load_cached(workshop_id, prefer_language)
        if cached is not None:
            return cached
    d = _cache_dir(workshop_id)
    if not d.is_dir():
        return None
    _migrate_legacy_meta(workshop_id)
    for p in sorted(d.glob("meta.*.json")):
        lang = p.name[len("meta."):-len(".json")]
        cached = _load_cached(workshop_id, lang)
        if cached is not None:
            return cached
    return None


# ---------------------------------------------------------------- 标题补全 --
# 背景：Steam Web API 的 language 参数对工坊标题无效（永远返回基础语言），
# 只有社区页面会按 ?l= 返回本地化标题。为了「没打开过预览的 mod 也显示中文名」，
# 这里做一个轻量的标题补全：只抓 HTML 解析标题，落到 title.<lang>.json。
_TITLE_RETRY_TTL = 7 * 86400   # 补抓失败后 7 天内不重试，避免反复撞风控


def _title_path(workshop_id: str, language: str) -> Path:
    return _cache_dir(workshop_id) / f"title.{language}.json"


def _read_title_cache(workshop_id: str, language: str) -> Optional[dict]:
    p = _title_path(workshop_id, language)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_localized_title(workshop_id: str, language: str) -> Optional[str]:
    """取本地化标题：完整预览缓存优先，其次是只抓标题的轻量缓存；都没有返回 None。

    只信任 cache_version 达标（v3 起 HTML 标题优先）的预览缓存——旧版 API 优先的
    缓存里存的是基础语言标题（英文），继续沿用会让列表名一直是英文。
    """
    cached = _load_cached(workshop_id, language)
    if cached and cached.get("cache_version", 0) >= _CACHE_VERSION:
        t = cached.get("title")
        if t and str(t).strip():
            return str(t).strip()
    raw = _read_title_cache(workshop_id, language)
    if raw and raw.get("ok"):
        t = raw.get("title")
        if t and str(t).strip():
            return str(t).strip()
    return None


def needs_title_fetch(workshop_id: str, language: str) -> bool:
    """当前是否还需要补抓本地化标题（幂等判断）。

    2026-11-03 重构：标题现在全走 Web API (language=schinese) — 零 HTML 爬取。
    这个函数只判断是否已有缓存，不再检查风控（Web API 通道无风控）。
    """
    if get_localized_title(workshop_id, language):
        return False
    raw = _read_title_cache(workshop_id, language)
    if raw:
        if raw.get("ok"):
            return False
        if time.time() - float(raw.get("fetched_at") or 0) < _TITLE_RETRY_TTL:
            return False
    return True


def fill_localized_title(workshop_id: str, language: str) -> Optional[str]:
    """2026-11-03 重构：不再爬 HTML 拿标题，全走 Web API 批量。
    这个函数保留给 store_mods.py 调用，但内部改调 Web API（fill_missing_titles 批量化更好）。
    """
    from .steam_webapi import fetch_workshop_details
    details = fetch_workshop_details([str(workshop_id)], language=language)
    info = details.get(str(workshop_id))
    title = info.title if info and info.title else None
    if title:
        try:
            d = _cache_dir(workshop_id)
            d.mkdir(parents=True, exist_ok=True)
            _title_path(workshop_id, language).write_text(
                json.dumps({"title": title, "ok": True, "fetched_at": int(time.time())},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            _log.warning("[workshop] 标题缓存写失败 id=%s: %s", workshop_id, e)
    return title


def fill_missing_titles(workshop_ids: list[str], language: str, on_progress=None) -> int:
    """批量补全缺失的本地化标题，返回本次成功抓到标题的数量。

    优先级：steamDB.json 本地快照（零 HTTP）→ Steam Web API（兜底）。
    steamDB 有 4745/4747 个 mod 的英文 title，毫秒级覆盖绝大多数订阅。
    """
    from .steam_webapi import fetch_workshop_details, query_steamdb

    pending = [
        w for w in dict.fromkeys(str(x) for x in workshop_ids)
        if needs_title_fetch(w, language)
    ]
    if not pending:
        return 0

    # ★ 优先从 steamDB 快照拿 title（零 HTTP）
    filled = 0
    api_fallback: list[str] = []
    for wid in pending:
        snap = query_steamdb(wid)
        title = snap.get("name") if snap else None
        if title:
            try:
                d = _cache_dir(wid)
                d.mkdir(parents=True, exist_ok=True)
                _title_path(wid, language).write_text(json.dumps({
                    "title": title, "ok": True, "fetched_at": int(time.time()),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                filled += 1
            except Exception as e:
                _log.warning("[workshop] 标题缓存写失败 id=%s: %s", wid, e)
        else:
            api_fallback.append(wid)

    # 还缺的才批量 Web API 兜底
    if api_fallback:
        _log.info("[workshop] fill_missing_titles: steamDB 命中 %d，%d 个需 Web API 兜底",
                  filled, len(api_fallback))
        for i in range(0, len(api_fallback), 100):
            batch = api_fallback[i : i + 100]
            details = fetch_workshop_details(batch, language=language)
            for wid in batch:
                info = details.get(wid)
                title = info.title if info and info.title else None
                if title:
                    try:
                        d = _cache_dir(wid)
                        d.mkdir(parents=True, exist_ok=True)
                        _title_path(wid, language).write_text(json.dumps({
                            "title": title, "ok": True, "fetched_at": int(time.time()),
                        }, ensure_ascii=False, indent=2), encoding="utf-8")
                        filled += 1
                    except Exception as e:
                        _log.warning("[workshop] 标题缓存写失败 id=%s: %s", wid, e)
                if on_progress:
                    try:
                        on_progress(api_fallback.index(wid)+1, len(api_fallback), wid)
                    except Exception:
                        pass

    _log.info("[workshop] 标题补全完成: %d/%d 个 mod 获得标题 (steamDB+API)", filled, len(pending))
    return filled


def get_cached_tags(workshop_id: str, language: str = "") -> list[str]:
    """读取已缓存的创意工坊标签（不触发网络请求）；未缓存或无效返回 []。"""
    cached = load_cached_any(workshop_id, language)
    if not cached:
        return []
    tags = cached.get("tags")
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, str)]


def is_workshop_cached(workshop_id: str, language: str = "zh") -> bool:
    """该语言是否已有可复用的预览缓存（只查缓存，不触发网络请求）。

    每个语言独立判断：中文看过、切到英文时英文没有缓存 → 返回 False，
    前端会显示进度条并只补抓英文，中文缓存保留不丢。
    """
    cached = _load_cached(workshop_id, language)
    if cached is None:
        return False
    return _cache_valid(cached, language)


def _save_cache(workshop_id: str, meta: dict, preview_bytes: Optional[bytes], ctype: str) -> None:
    """写某语言的 meta 缓存。宣传图多语言共用一张，抓取时覆盖写入（作者换图后能及时更新）。"""
    d = _cache_dir(workshop_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        if preview_bytes is not None:
            ext = _CONTENT_TYPE_EXT.get(ctype, ".jpg")
            (d / f"preview{ext}").write_bytes(preview_bytes)
        language = str(meta.get("language") or "zh")
        _meta_path(workshop_id, language).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        _log.warning("[workshop] 写缓存失败 id=%s: %s", workshop_id, e)


def clear_workshop_cache() -> int:
    """清空创意工坊预览磁盘缓存。返回被删除的缓存目录数（供调用方展示）。"""
    try:
        if _CACHE_ROOT.is_dir():
            count = sum(1 for c in _CACHE_ROOT.iterdir() if c.is_dir())
            shutil.rmtree(_CACHE_ROOT)
            _log.info("[workshop] 已清空预览缓存: %s（%d 个目录）", _CACHE_ROOT, count)
            return count
        return 0
    except Exception as e:
        _log.warning("[workshop] 清空缓存失败: %s", e)
        return 0


def clear_single_cache(workshop_id: str) -> bool:
    """删除单个 workshop 的预览缓存目录。返回是否实际删除了东西。"""
    wid = str(workshop_id or "").strip()
    if not wid:
        return False
    d = _cache_dir(wid)
    if not d.is_dir():
        return False
    try:
        shutil.rmtree(d)
        _log.info("[workshop] 已删除单个预览缓存: %s", d)
        return True
    except Exception as e:
        _log.warning("[workshop] 删除单个缓存失败 wid=%s: %s", wid, e)
        return False


def cleanup_workshop_cache(valid_ids) -> int:
    """删除缓存目录里不属于 valid_ids 的孤儿缓存（已删除/取消订阅的 mod）。返回删除数量。"""
    removed = 0
    try:
        if not _CACHE_ROOT.is_dir():
            return 0
        for d in _CACHE_ROOT.iterdir():
            if d.is_dir() and d.name not in valid_ids:
                try:
                    shutil.rmtree(d)
                    removed += 1
                except Exception:
                    pass
    except Exception as e:
        _log.warning("[workshop] 清理孤儿缓存失败: %s", e)
    return removed


def get_cached_mtime(workshop_id: str) -> int:
    """返回本地预览缓存的修改时间（各语言 meta 里最新的一次；无缓存返回 0）。"""
    try:
        d = _cache_dir(workshop_id)
        if not d.is_dir():
            return 0
        _migrate_legacy_meta(workshop_id)
        stamps = [p.stat().st_mtime for p in d.glob("meta.*.json")]
        return int(max(stamps)) if stamps else 0
    except Exception:
        return 0


def _strip_html(raw: str) -> str:
    """把 HTML 片段转成纯文本：<br> 转换行，其余标签去掉。"""
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</p\s*>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).strip()


def _extract_desc(html_text: str) -> Optional[str]:
    """提取 workshopItemDescription div 的完整内容。

    注意：description div 内部有嵌套的子 div（如 <div class="bb_h1"> 标题、<div class="bb_h2"> 小节），
    用 .*? 非贪婪匹配会在第一个子 div 的结束标签就截断，导致只取到开头几个字符。
    这里改成深度计数法，找到真正的闭合 </div>。
    """
    m = re.search(
        r'<div\s+class="workshopItemDescription"(?![Tt])[^>]*>', html_text
    )
    if not m:
        return None
    open_end = html_text.find(">", m.start()) + 1
    depth = 1
    pos = open_end
    close_pos = -1
    while depth > 0 and pos < len(html_text):
        next_open = html_text.find("<div", pos)
        next_close = html_text.find("</div>", pos)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = html_text.find(">", next_open) + 1
        else:
            depth -= 1
            pos = next_close + len("</div>")
            if depth == 0:
                close_pos = next_close
    if close_pos < 0:
        return None
    raw = html_text[open_end:close_pos]
    text = _strip_html(raw)
    # 有效性过滤：description 不能只是标题（比如只有 "MinionLib" 这种 3-10 个字符的短文本）
    if not text or len(text.strip()) < 10:
        return None
    return text


def _extract_preview(html_text: str) -> Optional[str]:
    # 优先取预览大图 img#previewImage，其次 og:image
    m = re.search(r'(?s)<img id="previewImage"[^>]+src="([^"]+)"', html_text)
    if m:
        return m.group(1).replace("&amp;", "&")
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html_text)
    if m:
        return m.group(1).replace("&amp;", "&")
    return None


def _extract_screenshots(html_text: str) -> list[str]:
    """轮播图：取 rgFullScreenshotURLs 数组；没有则退回 rgScreenshotURLs 对象。"""
    urls: list[str] = []
    m = re.search(r"(?s)var\s+rgFullScreenshotURLs\s*=\s*\[(.*?)\];", html_text)
    if m:
        urls = re.findall(r'"([^"]+)"', m.group(1))
    if not urls:
        m = re.search(r"(?s)var\s+rgScreenshotURLs\s*=\s*\{(.*?)\};", html_text)
        if m:
            urls = re.findall(r'"([^"]+)"', m.group(1))
    return [u.replace("&amp;", "&") for u in urls]


def _extract_tags(html_text: str) -> list[str]:
    tags: list[str] = []
    # Steam 标签在 rightDetailsBlock 里，链接形式为 requiredtags%5B%5D=XXX
    # 直接搜所有包含 requiredtags 的 <a> 链接文字
    for m in re.finditer(
        r'<a[^>]*href="[^"]*requiredtags%5B%5D=[^"]*"[^>]*>([^<]+)</a>',
        html_text, re.IGNORECASE
    ):
        tag = html.unescape(m.group(1)).strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _extract_awards(html_text: str) -> list[dict]:
    """社区奖励物品：Steam 页面上的 reaction 图标 + 数量 + 名称。"""
    awards: list[dict] = []
    # 每个 review_award 块（内部无嵌套 div，data-tooltip-html 里的引号均为 &quot; 转义）
    for seg in re.findall(r'(?s)<div[^>]*class="[^"]*review_award[^"]*"[^>]*>.*?</div>', html_text):
        count_m = re.search(r'data-reactioncount="(\d+)"', seg)
        icon_m = re.search(r'(?s)<img class="review_award_icon" src="([^"]+)"', seg)
        tooltip_m = re.search(r'data-tooltip-html="([^"]*)"', seg)
        name = ""
        if tooltip_m:
            # tooltip 内容是 HTML 实体（&lt;div ...&gt;），先反转义再抓 reaction_award_name
            tip = html.unescape(tooltip_m.group(1))
            name_m = re.search(r'<div class="reaction_award_name">([^<]+)</div>', tip)
            if name_m:
                name = html.unescape(name_m.group(1).strip())
        if not icon_m:
            continue
        awards.append({
            "name": name,
            "count": int(count_m.group(1)) if count_m else 0,
            "icon": icon_m.group(1).replace("&amp;", "&"),
        })
    return awards


def _extract_author(html_text: str) -> Optional[str]:
    m = re.search(r'(?s)Created by.*?<div class="friendBlockContent">\s*(.*?)<br', html_text)
    if m:
        return _strip_html(m.group(1))
    return None


def _extract_stats(html_text: str) -> dict:
    """订阅 / 收藏 / 访问数等侧栏统计。"""
    stats: dict = {}
    table = re.search(r'(?s)<table class="stats_table">(.*?)</table>', html_text)
    if not table:
        return stats
    rows = re.findall(r"(?s)<tr>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>", table.group(1))
    for value, label in rows:
        key = label.strip().lower().replace(" ", "_")
        stats[key] = value.strip()
    return stats


def _cache_valid(cached: dict, language: str) -> bool:
    """判断磁盘缓存是否足够有效可以复用。

    缓存可能因为以下原因不可复用：
      - cache_version 缺失或低于当前版本（强制重抓）
      - language 字段缺失/不匹配
      - title 缺失（空壳，API 都没返回）

    完整缓存（_html_fetched=True）只要 language + title 对就有效——
    说明 HTML 爬过了，就算 description 短/screenshots 空也不应该丢。
    不完整缓存也跳过 description 检查——API 给多少先用多少，按需补。
    """
    if cached.get("cache_version", 0) < _CACHE_VERSION:
        return False
    if not cached.get("title"):
        return False
    if cached.get("language") != language:
        return False
    return True


def _convert_desc_to_html(result: Optional[dict]) -> Optional[dict]:
    """给前端的最终返回：把 BBCode desc 转成安全 HTML。
    缓存里存 BBCode（统一格式），只有 API 边界做一次转换。
    """
    if not result or not result.get("description"):
        return result
    desc = result["description"]
    # 如果已经是 HTML（以 < 开头）就不转（兼容旧缓存和新转换后的缓存）
    if desc.strip().startswith("<"):
        return result
    result["description"] = _bbcode_to_html(desc)
    return result


def fetch_workshop_preview(workshop_id: str, language: str = "zh", *,
                            _fallback_title: Optional[str] = None,
                            _skip_html: bool = False) -> Optional[dict]:
    """获取创意工坊 mod 的完整预览详情（单份）。优先读磁盘缓存，无缓存则 Web API 抓取并落盘。失败返回 None。

    2026-11-03 重构 v3：纯同步秒出 + 异步后台 HTML desc 补全。
      - 缓存命中 → 直接返回（不管 title/desc 有没有中文），**毫秒级**
      - 缓存无效 → Web API 抓 title/desc/tags/preview_url → 落盘 → 返回（也是秒级）
      - zh 界面 + desc 无中文 + html_checked 未标记 → 启动后台 daemon 线程爬 HTML 补中文
        后台不阻塞当前返回，前端 _pollHtmlFill 每 5s 轮询自动刷新 desc
      - desc 补中文才值得花 3s（长文本）；title 不补（就几个词）

    返回值里带 `cached` 字段：True=命中缓存，False=首次抓取。
    """
    if not workshop_id:
        return None

    cached = _load_cached(workshop_id, language)
    if cached is not None and _cache_valid(cached, language):
        # ── 缓存命中 → 直接返回（毫秒级）──
        # 但 zh 界面 + desc 无中文 + 未爬过 HTML → 异步后台补
        if (language == "zh" and not _skip_html
                and not _has_chinese(cached.get("description") or "")
                and not cached.get("html_checked")):
            _launch_async_html_fill(workshop_id, language,
                                    api_time_updated=int(cached.get("time_updated") or 0))
            _log.debug("[workshop] wid=%s 缓存返回，后台线程补中文 desc", workshop_id)
        cached.pop("_html_fetched", None)
        cached["cached"] = True
        return _convert_desc_to_html(cached)

    # ── 目标语言 meta 不存在 → 先看共享目录有没有本地图 ──
    # （切语言时：meta.en.json 不存在但 preview.jpg 已在共享目录，
    #   后面 Web API 抓完 title/desc/tags 后 _save_cache + _load_cached 会自动把 file://
    #   本地图路径设进 preview 字段，前端直接秒出，不走 CDN）
    # 这个 fallback 不需要提前 return，只要确保后续流程不会因为 meta 不存在就重下 CDN 图
    # （实际上 Web API 本身不下载图，_save_cache(preview_bytes=None) 写的 meta 里 preview=None，
    #  但 _load_cached 会自动从共享目录找图填上）

    # ── 缓存真的不存在 → Web API 抓取（毫秒级）──
    api_item = _fetch_api_details(workshop_id, language)
    if not api_item:
        _log.warning("[workshop] fetch_workshop_preview: Web API 失败 wid=%s", workshop_id)
        return None

    api_title = api_item.get("title") or ""
    api_preview_url = api_item.get("preview_url") or ""
    api_desc = api_item.get("description") or ""
    api_tags_raw = api_item.get("tags") or []
    api_tags = [t.get("tag") for t in api_tags_raw if isinstance(t, dict) and t.get("tag")]

    final_title = html.unescape(api_title.strip()) if api_title else ""
    final_desc = api_desc

    if not final_title:
        final_title = (_fallback_title or "").strip()
    if not final_title:
        _log.warning("[workshop] 无法获取 title wid=%s", workshop_id)
        return None

    def _fmt(v) -> str:
        if not v:
            return "0"
        try:
            return f"{int(v):,}"
        except (TypeError, ValueError):
            return str(v)
    stats = {
        "当前订阅者": _fmt(api_item.get("subscriptions")),
        "当前收藏人数": _fmt(api_item.get("favorited")),
        "总浏览数": _fmt(api_item.get("views")),
        "总收藏数": _fmt(api_item.get("lifetime_favorited")),
        "总订阅数": _fmt(api_item.get("lifetime_subscriptions")),
    }

    result: dict = {
        "cache_version": _CACHE_VERSION,
        "workshop_id": workshop_id,
        "language": language,
        "title": final_title,
        "title_src": "api",
        "preview": None,
        "preview_url": api_preview_url,
        "screenshots": [],
        "tags": api_tags,
        "awards": [],
        # 缓存里存 BBCode（Web API 返回的就是 BBCode）
        "description": final_desc or None,
        "desc_src": "api",
        "author": None,
        "stats": stats,
        "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}",
        "time_updated": int(api_item.get("time_updated") or 0),
    }

    _save_cache(workshop_id, result, None, "image/jpeg")

    # 后台线程异步补中文 desc（如果需要）
    if (language == "zh" and not _skip_html
            and not _has_chinese(final_desc)):
        _launch_async_html_fill(workshop_id, language, api_time_updated=int(api_item.get("time_updated") or 0))

    fresh = _load_cached(workshop_id, language)
    if fresh is not None:
        fresh["cached"] = False
    return _convert_desc_to_html(fresh)


# --------------------------------------------------------------- 后台两阶段批量补全 --

def run_bg_fill(workshop_ids: list[str], language: str = "zh") -> None:
    """后台两阶段补全：RimCrow 式快速预览图 + 慢速中文 HTML 补全。

    设计：
      阶段 1（快速）：Steam Web API 批量 100/次 → 拿所有 mod 的 preview_url → 下载预览图。
        RimCrow 式：不碰社区页面 HTML，只走反爬墙外的 Web API + CDN 图床。
        100 个 mod 约 0.3s × 2 批 + 每个图 ~0.1s → **100 个约 15 秒**完成。
        启动后列表秒出所有预览图 + 英文标题。
      阶段 2（慢速）：爬社区页面 HTML 补中文标题/screenshots/stats。
        约 3s/个（社区页面节流），20 个窗口/10min。
        只有阶段 1 完成后才开始，撞风控自动中止。

    撞风控时两个阶段都会自动中止，下次重启或手动点刷新再继续。
    """
    from .steam_webapi import fetch_workshop_details

    wids = [str(w) for w in workshop_ids if w]
    if not wids:
        return

    lang = language or "zh"
    _log.info("[bg-fill] 启动：%d 个 workshop mod", len(wids))

    # 先把进度状态重置到"不可见"（phase1_total=-1 + phase1_running=False）
    # 这样 daemon 线程刚启动时前端看不到 running=True 的瞬时闪烁
    _set_bg_progress(
        phase1_done=0, phase1_total=-1,
        phase1_running=False, phase1_ok=False,
        phase2_done=0, phase2_total=-1,
        phase2_running=False, phase2_ok=False,
    )

    # --- 阶段 1：批量 Web API 拿数据写 meta.json（不爬 HTML、不下载预览图）---
    # RimCrow 设计：预览图 URL 存 preview_url 字段，前端直接 <img src="CDN URL"> 渲染
    # 不做本地下载（Python urllib 批量下载失败率 30-50%，浏览器自己拉稳定得多）
    # 筛选：有 meta.json + title 非空 → 跳过
    #      永久失败标记（API 连续 3 次拿不到 title）→ 也跳过
    phase1_ids: list[str] = []
    skipped_perma = 0
    for wid in wids:
        if _is_api_perma_failed(wid):
            skipped_perma += 1
            continue
        cached = _load_cached(wid, lang)
        # 有 title 且有 time_updated → 完全跳过（统一缓存已完整）
        # 有 title 但缺 time_updated → 也要跑（补写统一缓存）
        if cached is not None and cached.get("title") and cached.get("time_updated"):
            continue
        phase1_ids.append(wid)

    if skipped_perma:
        _log.warning("[bg-fill] %d 个 mod 因 API 永久失败被跳过（连续 %d 次无 title）",
                     skipped_perma, _API_PERMA_FAIL_THRESHOLD)

    if not phase1_ids:
        _log.info("[bg-fill] 阶段1 所有 mod 已有 API 数据，全部跳过 (%d)", len(wids))
        # phase1_total=-1 + phase1_running=False → 前端不显示进度条
        # 但后面还有"下载本地图"步骤要跑（可能很多 mod 有 meta 但没本地图）
    else:
        _log.info("[bg-fill] 阶段1：%d 个 mod 需补 API 数据", len(phase1_ids))

    if phase1_ids:
        # phase1_ids 不为空才设置 running=True + total（前端看到才显示进度条）
        _set_bg_progress(phase1_total=len(phase1_ids), phase1_done=0, phase1_running=True, phase1_ok=False)
        phase1_ok = True

        # 批量 Web API 拿 title + preview_url + description + tags + creator
        # 100/批，独立节流 0.3s + 200/10min 窗口（反爬墙外）
        all_details = fetch_workshop_details(phase1_ids, language=lang)

        # ★ 同时批量查 steamDB 快照（有 revision_change_number + time_updated）
        # Web API 无 key 不返回 revision_change_number — steamDB 是唯一来源
        from .steam_webapi import load_steamdb, query_steamdb
        _steamdb = load_steamdb()  # 懒加载，整个进程只 load 一次
        _steamdb_rev_hits = 0

        # ★ 批量解析 steamid → 实际作者名
        # Web API / steamDB 都只返回 steamid（如 "76561199186957585"），不返回玩家名字
        # ISteamUser/GetPlayerSummaries/v2/ 无 key 也能用，每批 100 个
        from .steam_webapi import resolve_steamids_to_names
        all_steamids: set[str] = set()
        for wid in phase1_ids:
            detail = all_details.get(wid)
            sid = (getattr(detail, "creator", None) if detail else None) or ""
            if sid and sid.isdigit():
                all_steamids.add(str(sid))
            if _steamdb:
                snap = query_steamdb(wid)
                if snap:
                    sid2 = snap.get("authors") or ""
                    if sid2 and str(sid2).isdigit():
                        all_steamids.add(str(sid2))
        _author_names = resolve_steamids_to_names(list(all_steamids)) if all_steamids else {}
        if _author_names:
            _log.info("[bg-fill] 解析 %d 个 steamid → 作者名（命中 %d）", len(all_steamids), len(_author_names))

        # 串行写 meta.json（不下载预览图）
        for i, wid in enumerate(phase1_ids):
            detail = all_details.get(wid)
            old_cache = _load_cached(wid, lang)

            # API 对这个 wid 没返回 + 旧缓存 title 也空 → 永久失败计数
            if detail is None and not (old_cache and old_cache.get("title")):
                count = _mark_api_fail(wid)
                if count >= _API_PERMA_FAIL_THRESHOLD:
                    _log.warning("[bg-fill] wid=%s API 连续 %d 次无 title → 永久跳过", wid, count)
                else:
                    _log.debug("[bg-fill] wid=%s API 无返回且无旧标题，失败计数 %d/%d",
                              wid, count, _API_PERMA_FAIL_THRESHOLD)
                _set_bg_progress(phase1_done=i + 1)
                continue

            api_title = getattr(detail, "title", None) if detail else None
            if not api_title and old_cache and old_cache.get("title"):
                api_title = old_cache.get("title")

            # 拿到 title → 清掉之前的失败计数
            if api_title:
                _clear_api_fail(wid)

            try:
                # getattr 安全访问：SteamModInfo 属性可能和预期不完全一致
                api_preview_url = getattr(detail, "preview_url", None) if detail else None
                api_tags = getattr(detail, "tags", None) or []
                api_desc = getattr(detail, "description", None) if detail else None
                api_author = (getattr(detail, "creator", None) or getattr(detail, "author", None)
                             or getattr(detail, "author_steam_id", None)) if detail else None
                api_time_updated = int(getattr(detail, "time_updated", 0) or 0) if detail else 0
                api_subscriptions = int(getattr(detail, "subscriptions", 0) or 0) if detail else 0
                api_rev = int(getattr(detail, "revision_change_number", 0) or 0) if detail else 0
                api_file_size = int(getattr(detail, "file_size", 0) or 0) if detail else 0

                # ★ steamDB 补：revision_change_number 只能从快照拿（无 key API 不返回）
                # file_size / time_updated 也从快照补（两者都有时对比取更可信的）
                if _steamdb:
                    snap = query_steamdb(wid)
                    if snap:
                        snap_rev = int(snap.get("revision_change_number") or 0)
                        snap_tu = int(snap.get("time_updated") or 0)
                        snap_fs = int(snap.get("file_size") or 0)
                        if snap_rev > 0 and api_rev == 0:
                            api_rev = snap_rev
                            _steamdb_rev_hits += 1
                        if snap_tu > api_time_updated:  # 快照更新
                            api_time_updated = snap_tu
                        if snap_fs > 0 and api_file_size == 0:
                            api_file_size = snap_fs

                result = {
                    "cache_version": _CACHE_VERSION,
                    "workshop_id": wid,
                    "language": lang,
                    "title": api_title or "",
                    "preview": None,  # 磁盘上始终为 None
                    # 关键：preview_url 存 CDN URL，前端直接 <img src=CDN> 渲染
                    "preview_url": api_preview_url or (old_cache.get("preview_url") if old_cache else None) or "",
                    "screenshots": [],
                    "tags": ([t.tag if hasattr(t, 'tag') else str(t) for t in api_tags]) if api_tags else (old_cache.get("tags") if old_cache else []),
                    "awards": [],
                    "description": api_desc or (old_cache.get("description") if old_cache else None) or "",
                    "author": (_author_names.get(str(api_author)) if api_author and str(api_author).isdigit() else None)
                              or api_author or (old_cache.get("author") if old_cache else None) or "",
                    # 统一缓存：RimCrow 式完整版本号追踪
                    # time_updated（时间戳） + revision_change_number（Steam 官方版本号，每次文件变更 +1）
                    # + file_size（当前总大小） → 三个字段对比精准检测 mod 本体/描述/文件的任何变更
                    "time_updated": api_time_updated or int(old_cache.get("time_updated") or 0),
                    "revision_change_number": api_rev or int(old_cache.get("revision_change_number") or 0),
                    "file_size": api_file_size or int(old_cache.get("file_size") or 0),
                    "subscriptions": api_subscriptions or int(old_cache.get("subscriptions") or 0),
                    "stats": {},
                    "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
                }
                # 只写 meta.json，不传 preview_bytes（不下载图）
                _save_cache(wid, result, None, None)
            except Exception as e:
                _log.warning("[bg-fill] 阶段1 缓存写入 wid=%s 失败: %s", wid, e, exc_info=True)

            _set_bg_progress(phase1_done=i + 1)

        _set_bg_progress(phase1_running=False, phase1_ok=phase1_ok)
        _log.info("[bg-fill] 阶段1 完成: %s (%d/%d, steamDB rev 补充=%d)",
                  "ok" if phase1_ok else "被风控中断",
                  _bg_progress["phase1_done"], len(phase1_ids), _steamdb_rev_hits)
    else:
        phase1_ok = True

    # ── 阶段1 收尾：HTML 签名索引已废弃（steamDB.revision_change_number 更准） ──
    _log.info("[bg-fill] 阶段1 完成（纯 Web API + steamDB）")

    # 阶段 2（慢速 HTML 补中文）已移除以用户按需触发：
    # fetch_workshop_preview 检测到不完整缓存(_html_fetched=False)时，
    # 立即返回缓存给前端（秒开），同时后台 daemon 线程异步补 HTML。
    # 这样既防止全量爬 HTML 触发 429 风控，又保证用户点选时能尽快看到内容。
    _set_bg_progress(phase2_total=0, phase2_done=0, phase2_running=False, phase2_ok=True)

    # --- 后台串行下载 preview_url 到本地（方案 B，RimCrow 方案的取长补短版）---
    #
    # 双重持久化保障，互不干扰：
    #   方案 A（webview_app.py 层）：private_mode=False + storage_path → WebView2 持久化
    #       CDN 资源的 HTTP 磁盘缓存。前端 fallback 到 preview_url（CDN URL）时，
    #       第二次加载浏览器直接命中缓存秒出，和 RimCrow 行为一致。
    #   方案 B（本阶段）：urllib 串行下载 → cache/workshop/<wid>/preview.jpg → 前端
    #       effectivePreview 优先 file:// 本地图，根本不发 CDN 请求。
    #
    # 为什么需要 B：WebView2 的 HTTP 缓存可能被系统/浏览器自动清理、版本升级时丢失；
    # 本地磁盘缓存是我们完全控制的，永不丢失。A 兜底 B 的失败场景，B 秒出常见场景。
    #
    # 防重复：
    #   - 下载前检查 preview.jpg/png 是否存在（L1404/L1416/L1428 三处 continue）
    #   - 前端优先本地 file:// → 本地图存在时浏览器零 CDN 请求 → A 不会和 B 抢活
    #
    # 风控安全：
    #   - 走 images.steamusercontent.com（Steam CDN 通道），与 api.steampowered.com
    #     （Web API）/ steamcommunity.com（社区页面）完全隔离
    #   - 串行下载（0.5s/张，无并发），429 只丢这张图，不触发全局封禁
    #   - run_bg_fill 本身就在 daemon 线程里，这里直接在当前线程继续跑
    #     （阶段 1 已瞬间完成设 phase1_running=False）

    # 收集所有需要下载 preview 的 wid（包括已在筛选阶段有 preview_url 的）
    all_preview_wids = []
    seen_preview = set()
    for wid in wids:
        cached = _load_cached(wid, lang)
        if not cached or not cached.get("preview_url"):
            continue
        cd = _cache_dir(wid)
        if (cd / "preview.jpg").exists() or (cd / "preview.png").exists():
            continue  # 已有本地图
        all_preview_wids.append((wid, cached["preview_url"]))
        seen_preview.add(wid)

    # 也把 phase1_ids 里刚写完 meta.json 的加上（preview_url 刚写入，可能还没下载）
    for wid in phase1_ids:
        if wid in seen_preview:
            continue
        cached = _load_cached(wid, lang)
        if cached and cached.get("preview_url"):
            cd = _cache_dir(wid)
            if (cd / "preview.jpg").exists() or (cd / "preview.png").exists():
                continue
            all_preview_wids.append((wid, cached["preview_url"]))

    if not all_preview_wids:
        _log.info("[bg-fill] 所有预览图已下载到本地")
        # 这里不 return！后面还有 HTML 补全入队要跑

    _log.info("[bg-fill] 后台下载 %d 张预览图到本地", len(all_preview_wids))
    downloaded = 0
    for wid, purl in all_preview_wids:
        cd = _cache_dir(wid)
        if (cd / "preview.jpg").exists() or (cd / "preview.png").exists():
            continue
        try:
            result = _download_image_bytes(purl)
            if result:
                data, ctype = result
                ext = ".png" if "png" in ctype else ".jpg"
                cd.mkdir(parents=True, exist_ok=True)
                (cd / f"preview{ext}").write_bytes(data)
                downloaded += 1
            # 下载失败不计数，也不阻塞——下次启动或用户点选时还能重试
        except Exception as e:
            _log.debug("[bg-fill] 预览图下载 wid=%s 失败: %s", wid, e)
    _log.info("[bg-fill] 预览图下载完成: %d/%d 成功", downloaded, len(all_preview_wids))

    # 2026-11-03 重构：enqueue_html_fill 已砍
    # Web API (language=schinese) 直接返回中文 title/description/tags — 零 HTML 补全


def _fetch_api_details(workshop_id: str, language: str = "zh") -> Optional[dict]:
    """通过 Steam Web API GetPublishedFileDetails 拿基础元数据。

    这个 API 不受 Steam 反爬墙影响，比 HTML 解析可靠得多。返回 dict 含
    title / preview_url / description / num_subs / num_favs / num_views /
    creator / tags，失败返回 None。

    走 Web API 独立节流（0.3s 间隔、200/10min 窗口），与社区页面通道完全解耦。

    language: zh/en。决定 API 返回的 title/description 是什么语言。
      不传 language Steam 默认返回英文！
    """
    import urllib.parse
    from .steam_webapi import _throttle
    _throttle()
    steam_lang = _to_steam_lang(language)
    url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    data = urllib.parse.urlencode({
        "itemcount": "1",
        "publishedfileids[0]": workshop_id,
        "language": steam_lang,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": _UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        _log.warning("[workshop] API 详情抓取失败 id=%s: %s", workshop_id, e)
        return None
    items = (payload.get("response") or {}).get("publishedfiledetails") or []
    if not items:
        return None
    return items[0]


def fetch_workshop_time_updated(workshop_id: str) -> Optional[int]:
    """获取工坊条目的 time_updated（Unix 时间戳）。

    优先级：steamDB.json 本地快照（零 HTTP）→ Steam Web API（无 key 兜底）。
    steamDB 是 1.4MB 本地 JSON，加载一次后 O(1) dict 查找，比 HTTP 快 1000x。
    """
    # ★ 优先查 steamDB 快照（零网络）
    try:
        from .steam_webapi import query_steamdb
        snap = query_steamdb(str(workshop_id))
        if snap:
            tu = int(snap.get("time_updated") or 0)
            if tu > 0:
                return tu
    except Exception:
        pass

    import urllib.parse
    from .steam_webapi import _throttle

    _throttle()
    url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    data = urllib.parse.urlencode({
        "itemcount": "1",
        "publishedfileids[0]": workshop_id,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        _log.warning("[workshop] 获取 time_updated 失败 id=%s: %s", workshop_id, e)
        return None
    details = (payload.get("response") or {}).get("publishedfiledetails") or []
    if not details:
        return None
    ts = details[0].get("time_updated")
    return int(ts) if ts else None
