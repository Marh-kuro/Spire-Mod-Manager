from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import webview

from .backup import BackupManager
from .logger import get_logger
from .scanner import ModInfo, mod_version_key, norm_mod_source
from .store import (
    AUTO_TAG_LIB_KEYWORDS,
    AUTO_TAG_RELIC,
    AUTO_TAG_NEW_CARD,
    AUTO_TAG_EVENT,
    AUTO_TAG_POTION,
    AUTO_TAG_ENEMY,
    AUTO_TAG_POWER,
    AUTO_TAG_MAP,
    ModManager,
)

_log = get_logger("api")

# 全局线程池：所有涉及文件 IO 的 API 都丢这里跑，不阻塞 pywebview 主线程
_IO_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="spire-io")
# pywebview 主线程锁（evaluate_js 可能需要）
_WINDOW_LOCK = threading.Lock()

# 后端返回给前端的少量内置文案（按界面语言返回）。mod 内容/工坊描述不在此列。
_UI_TEXT = {
    "zh": {
        "not_installed": "（未安装）",
        "not_enabled": "（未启用）",
        "default_pack": "整合包",
        "skin_config_missing": "未找到皮肤配置",
        "unknown_role": "未知角色",
        "skin_unavailable": "该皮肤不在可用列表",
        "write_failed": "写回失败",
        "card_unavailable": "该卡面不可用",
        "pack_missing": "整合包不存在",
        "fetch_failed": "获取失败",
        "folder_missing": "未找到可打开的目录",
        "not_workshop": "该 mod 不是创意工坊来源",
        "mod_missing": "未找到该模组",
        "backup_missing": "备份不存在",
    },
    "en": {
        "not_installed": " (not installed)",
        "not_enabled": " (not enabled)",
        "default_pack": "Pack",
        "skin_config_missing": "Skin config not found",
        "unknown_role": "Unknown role",
        "skin_unavailable": "This skin is not available",
        "write_failed": "Write-back failed",
        "card_unavailable": "This card art is not available",
        "pack_missing": "Pack not found",
        "fetch_failed": "Failed to fetch",
        "folder_missing": "No folder to open",
        "not_workshop": "This mod is not from the Workshop",
        "mod_missing": "Mod not found",
        "backup_missing": "Backup not found",
    },
}


class Api:
    """pywebview js_api：把 ModManager 的方法封装成 JSON 可序列化的返回给前端 Vue。"""

    def __init__(self, manager: ModManager):
        self.manager = manager
        # 上次扫描快照（lower mod_id -> version）。None 表示尚无基线（首次扫描）。
        self._last_snapshot: Optional[dict] = None
        # refresh 启动互斥：避免两次 API 调用在 running 标志写入前同时通过检查
        self._refresh_start_lock = threading.Lock()
        # 从 godot.log 解析出的 mod 子内容缓存：{mod_id: {"initializers": [...], "creatings": [...]}}
        self._cached_subcontents: dict = {}
        # 备份管理器（按备份根目录懒创建；目录变了自动重建）
        self._backups: Optional[BackupManager] = None
        self._backups_root: Optional[str] = None
        # 本地 mod 预览图扫描结果缓存：{install_dir(str): (dir_mtime, url|None)}
        # 避免 get_state 每次对每个 mod 重复做 O(预览名×子目录) 的磁盘探测
        self._local_preview_cache: dict[str, tuple[float, str | None]] = {}
        # 工坊标题后台补全：线程句柄 + 待补全数量缓存（按语言）
        self._title_fill_thread: Optional[threading.Thread] = None
        self._title_fill_lock = threading.Lock()
        self._titles_pending_cache: dict[str, int] = {}
        # 后台两阶段完整补全：阶段1 Web API快速预览图 → 阶段2慢速HTML补中文
        self._bg_fill_thread: Optional[threading.Thread] = None
        # 全量备份进度（线程安全，供前端轮询）
        self._backup_progress = {
            "running": False, "total": 0, "processed": 0,
            "done": 0, "failed": 0, "skipped": 0,
        }
        self._backup_progress_lock = threading.Lock()

    def _ui(self, key: str) -> str:
        """按当前界面语言返回内置文案。"""
        lang = self.manager.get_language()
        return _UI_TEXT.get(lang, _UI_TEXT["zh"]).get(key, key)

    # ------------------------------------------------------------------ 异步派发
    def _dispatch_async(self, fn, op_label: str = "") -> dict:
        """把耗时操作（文件 IO、磁盘写入）丢到线程池，立即返回 async_id。

        线程完成后通过 webview.windows[0].evaluate_js 调前端：
          window.__onBackendDone(async_id, ok, result_json)
        前端用 async_id 匹配 pending Promise。
        """
        async_id = uuid.uuid4().hex[:12]

        def _run():
            try:
                result = fn()
                ok = True
            except Exception as e:
                _log.error("[async:%s] 失败: %s", op_label or async_id, e, exc_info=True)
                ok = False
                result = {"error": str(e)}
            # 不管成功失败，回调前端
            try:
                js_result = json.dumps(result, ensure_ascii=False, default=str)
                js = f"window.__onBackendDone({json.dumps(async_id)}, {str(ok).lower()}, {js_result});"
                with _WINDOW_LOCK:
                    for w in webview.windows:
                        try:
                            w.evaluate_js(js)
                        except Exception:
                            pass
            except Exception as e:
                _log.error("[async:%s] 回调前端失败: %s", async_id, e)

        _IO_POOL.submit(_run)
        return {"ok": True, "async_id": async_id, "op": op_label}

    # ------------------------------------------------------------------ 序列化
    def _mod_dict(self, info: ModInfo) -> dict:
        hv = self.manager.highest_version(info.mod_id)
        is_highest = self.manager.is_active_highest(info.mod_id)
        version_hint = None if is_highest else hv
        dep_flags = list(self.manager.mod_warning_flags(info.mod_id))
        # B2 修复：本地 mod 预览图扫描（install_dir 下常见图片名）
        local_preview = self._scan_local_preview(info.install_dir)
        version_compat = self._calc_mod_version_compat(info)
        # B1 修复：依赖检查一次遍历出 dep_statuses + missing_deps
        dep_statuses, missing_deps = self._dependency_checks(info)

        # ===== 显示名优先级：Steam workshop title > mod.json name =====
        display_name = info.display_name
        if info.source == "workshop" and info.workshop_id:
            ws_title = self._try_get_workshop_title(info.workshop_id)
            if ws_title:
                display_name = ws_title

        # 2026-10-13 修复：last_activity 兜底用 install_dir mtime（新扫描/从未 toggle 的 mod 不会是 0）
        _la = self.manager._state.last_activity.get(info.mod_id.lower(), 0)
        if _la == 0 and info.install_dir is not None:
            try:
                _la = int(os.path.getmtime(str(info.install_dir)))
            except Exception:
                pass
        return {
            "id": info.mod_id,
            "name": display_name,
            "author": info.manifest.author,
            "version": info.manifest.version,
            "description": info.manifest.description,
            "dependencies": info.manifest.dependencies,
            "tags": info.manifest.tags,
            "manager_tags": self.manager.get_mod_tags(info.mod_id),
            "tag_reason": self.manager.get_mod_tags_reason(info.mod_id),
            "tag_hits": self.manager.get_mod_tags_hits(info.mod_id),
            "tag_source": self.manager.get_mod_tags_with_source(info.mod_id)[1],
            "source": info.external_source or info.source,  # workshop mod 用 external_source="steam" 确保颜色不因启停变化
            "workshop_id": info.workshop_id,
            "folder": info.folder_name,
            "path": str(info.install_dir) if info.install_dir else None,
            "local_preview": local_preview,
            "version_hint": version_hint,
            "missing_deps": missing_deps,
            # ===== 依赖检查 =====
            "has_dep_warning": bool(dep_flags),
            "dep_flags": dep_flags,
            "dep_statuses": dep_statuses,  # 每个依赖的状态详情
            # ===== mod 版本兼容 =====
            "min_game_version": info.manifest.min_game_version,
            "max_game_version": getattr(info.manifest, "max_game_version", None),
            "version_compat": version_compat,  # {status, min_game, max_game, current}
            # ===== P0 新增：mod 类型推断 =====
            "mod_type": info.mod_type,           # framework / utility / content / skin
            "mod_type_label": info.mod_type_label,
            "mod_type_icon": info.mod_type_icon,
            # ===== P0 新增：source 中文标签 =====
            "external_source": info.external_source,  # nexusmods / github / curseforge / modrinth / steam / None
            "source_url": info.manifest.url,           # manifest 里读的 url
            "source_label": info.source_label,
            "source_icon": info.source_icon,           # 🎭 / 📁 / 🟠 / 🐙
            # ===== P0 新增：个人备注/颜色 =====
            "mod_meta": self.manager.get_mod_meta(info.mod_id),
            "mod_color": self.manager.get_mod_color(info.mod_id),
            # ===== P1 新增：Pinned 锁定 =====
            "pinned": self.manager.get_pinned(info.mod_id),  # {version, enabled} or None
            # ===== 最近活动时间（停用列表排序用） =====
            "last_activity": _la,
            # ===== godot.log 解析：大型 mod 的子内容（子角色/子模块/子卡牌池）=====
            # 比如 Downfall → {"initializers": ["Snecko","SlimeBoss",...], "creatings": ["Snecko",...]}
            # 来自游戏启动时 BaseLib 注册日志，比 DLL 反编译准得多
            "subcontents": self._cached_subcontents.get(info.mod_id, None),
            # ===== 同一 mod 的全部物理副本（工坊 / 本地 / 停用）=====
            # 默认只有 1 条 = 自身；有多个时前端展开显示
            "all_versions": self._all_versions_dict(info),
            # ===== 2026-10 新增：每个 mod_id 下各 source 的独立启停状态 =====
            # 格式：{ "steam_workshop": bool, "mods_directory": bool }
            # 同 mod_id 的工坊副本和本地副本各自独立启停
            "source_states": self.manager._source_states.get(info.mod_id.lower(), {}),
        }

    def _calc_mod_version_compat(self, info: ModInfo) -> dict:
        """计算 mod 版本兼容性，返回 status + 具体字段。

        status 枚举：
          "ok"             - 兼容（min ≤ current ≤ max 或没 max）
          "needs_update"   - 游戏版本太旧（current < min），mod 需要更高版本游戏
          "too_new"        - mod 要求的最大版本 < 当前游戏（max < current，极少见）
          "unknown"        - 无法判断（游戏版本未检测到 或 mod 没写 min_game_version）
        """
        current = ""
        try:
            game = self.manager.game
            if game:
                current = game.version
        except Exception:
            pass

        min_v = info.manifest.min_game_version or ""
        max_v = getattr(info.manifest, "max_game_version", None) or ""

        base = {
            "status": "unknown",
            "min_game": min_v or None,
            "max_game": max_v or None,
            "current": current or None,
        }

        if not current or not min_v:
            # 没检测到游戏版本或 mod 没写 min_game_version → 未知
            return base

        try:
            cur = self._parse_version(current)
            mn = self._parse_version(min_v)
        except Exception:
            return base

        if cur < mn:
            base["status"] = "needs_update"
            return base

        if max_v:
            try:
                mx = self._parse_version(max_v)
                if cur > mx:
                    base["status"] = "too_new"
                    return base
            except Exception:
                pass

        base["status"] = "ok"
        return base

    @staticmethod
    def _parse_version(version_str: str) -> tuple:
        """把 "0.111.0" 或 "v0.107.1" 拆成可比较的元组 (0, 111, 0)。

        遇到非数字部分时停止，取前缀。简单但够用——STS2 的版本号全是纯数字点分格式。
        """
        s = version_str.strip().lstrip("vV")
        parts = []
        for chunk in s.split("."):
            num = ""
            for ch in chunk:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                parts.append(int(num))
            else:
                break
        # 补齐到至少 3 位，方便比较
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)

    # ------------------------------------------------------------------ 本地 mod 预览图扫描
    _PREVIEW_NAMES = (
        "banner.png", "banner.jpg", "banner.jpeg",
        "preview.png", "preview.jpg", "preview.jpeg",
        "thumbnail.png", "thumbnail.jpg", "thumbnail.jpeg",
        "icon.png", "icon.jpg", "icon.jpeg",
        "cover.png", "cover.jpg", "cover.jpeg",
    )

    def _try_get_workshop_title(self, workshop_id: str) -> str | None:
        """从缓存里取 Steam workshop 标题。找不到返回 None。

        优先级说明（重要）：
          - meta.<lang>.json 来自社区页面，会按语言本地化标题；当前语言的优先。
          - data/cache/steam_workshop/{wid}.json 来自 Steam Web API，其 language 参数
            对 title 无效（永远返回基础语言），只在 meta 完全缺失时兜底。

        顺序：当前语言本地化标题（预览缓存或轻量标题缓存）→ API 缓存。
        """
        try:
            import json
            from .steam_webapi import _get_cache_dir as _api_cache_dir
            from .workshop import get_localized_title

            lang = self.manager.get_language()

            # 1) 当前语言的本地化标题（来自社区页面，已含 title.<lang>.json 轻量缓存）
            t = get_localized_title(str(workshop_id), lang)
            if t:
                return t

            # 2) Steam Web API 缓存（不本地化，但至少是有效标题）
            api_cache = _api_cache_dir()
            if api_cache:
                f = api_cache / f"{workshop_id}.json"
                if f.is_file():
                    raw = json.loads(f.read_text(encoding="utf-8"))
                    title = raw.get("title")
                    if title and title.strip():
                        return title.strip()
        except Exception:
            return None
        return None

    def _scan_local_preview(self, install_dir) -> str | None:
        """扫描本地 mod 文件夹里的预览图，返回 file:// URL 或 None。

        结果按 install_dir 的 mtime 缓存：目录未变化时直接复用，避免 get_state
        每次对每个 mod 重复做 O(预览名×子目录) 的磁盘探测。
        """
        if install_dir is None:
            return None
        try:
            d = Path(install_dir)
            if not d.is_dir():
                return None
            key = str(d)
            try:
                mtime = d.stat().st_mtime
            except OSError:
                mtime = 0.0
            cached = self._local_preview_cache.get(key)
            if cached is not None and cached[0] == mtime:
                return cached[1]
            result: str | None = None
            for name in self._PREVIEW_NAMES:
                for sub in ("", "assets", "icons", "images", "img"):
                    candidate = d / sub / name
                    if candidate.is_file():
                        import urllib.parse
                        abs_path = str(candidate.resolve()).replace("\\", "/")
                        result = "file:///" + urllib.parse.quote(abs_path)
                        break
                if result:
                    break
            self._local_preview_cache[key] = (mtime, result)
            return result
        except Exception:
            return None

    def _dependency_checks(self, info: ModInfo) -> tuple[list[dict], list[str]]:
        """一次遍历返回 (dep_statuses, missing_deps)，消除原 _dep_statuses / _missing_dependencies 的重复匹配。

        dep_statuses: [{id, status}]  status ∈ {ok, missing_install, missing_enable} — 前端按语言 I18N
        missing_deps: 同检查结果但格式化为字符串（含后端 UI 文案）
        """
        all_ids = {k.lower() for k in self.manager.all_mods}
        enabled_ids = {i.mod_id.lower() for i in self.manager.ordered_enabled()}
        statuses: list[dict] = []
        missing: list[str] = []
        for dep_raw in (info.manifest.dependencies or []):
            dep = self.manager._dep_id(dep_raw)
            dl = dep.lower()
            matched_all = self.manager._match_dep(dl, all_ids)
            matched_enabled = self.manager._match_dep(dl, enabled_ids)
            if matched_all is None:
                statuses.append({"id": dep, "status": "missing_install"})
                missing.append(f"{dep_raw}{self._ui('not_installed')}")
            elif matched_enabled is None:
                statuses.append({"id": dep, "status": "missing_enable"})
                missing.append(f"{dep_raw}{self._ui('not_enabled')}")
            else:
                statuses.append({"id": dep, "status": "ok"})
        return statuses, missing

    # _available_versions 已于 2026-10 删除（活动/非活动副本选择逻辑废弃）
    # 多副本展示统一由 _all_versions_dict 给 get_state 的 all_versions 字段提供

    def _all_versions_dict(self, active: ModInfo) -> list[dict]:
        """返回该 mod 的全部物理副本详情（不过滤来源）。

        格式: [{"key", "version", "source", "source_label", "path", "workshop_id", "is_enabled"}, ...]
        每个磁盘上的独立副本目录都返回一条。
        2026-10 删除 is_active（活动/非活动副本选择逻辑废弃）；保留 is_enabled 用于多 source 独立启停。

        2026-11-01 多副本修复：workshop 副本的 state_key 改成 ws_{workshop_id}，
        不再用统一的 "steam_workshop"——因为同 mod_id 可能有多个 workshop 副本
        （如圣主历险记原版 vs 修复版，workshop_id 不同），需要独立记录启停状态。
        """
        src_states = self.manager._source_states.get(active.mod_id.lower(), {})
        src_toggled = self.manager._source_last_toggled.get(active.mod_id.lower(), {})
        results: list[dict] = []
        versions_list = self.manager.available_versions(active.mod_id)
        for v in versions_list:
            v_state_key = mod_version_key(v)
            # 每个副本独立的显示名：Steam workshop title > mod.json name
            v_display_name = v.display_name
            if v.source == "workshop" and v.workshop_id:
                ws_title = self._try_get_workshop_title(v.workshop_id)
                if ws_title:
                    v_display_name = ws_title
            results.append({
                "key": mod_version_key(v),
                "name": v_display_name,  # 独立显示名，前端 expandModList 会用这个覆盖 canonical.name
                "version": v.manifest.version,
                "source": v.external_source or v.source,  # 同 _mod_dict：优先 external_source，颜色不因启停变
                "external_source": v.external_source,
                "source_label": v.source_label,
                "path": str(v.install_dir) if v.install_dir else None,
                "workshop_id": v.workshop_id,
                # 该副本在 source_states 里的独立启停状态
                # 有记录就用记录，没有的话：workshop 副本默认真（物理恒 ACTIVE），local 副本看 source
                "is_enabled": src_states.get(v_state_key, bool(v.source == "workshop" or v.source == "local")),
                # 前端 displayList 停用侧排序用——刚停的副本排顶部
                "last_toggled_at": src_toggled.get(v_state_key, 0.0),
            })
        return results

    def _workshop_ids(self) -> list[str]:
        """全部工坊副本的 workshop_id（含同 mod_id 的多副本），按出现顺序去重。"""
        out: list[str] = []
        seen: set[str] = set()
        for info in self.manager.all_mods.values():
            wid = str(info.workshop_id) if info.workshop_id else ""
            if wid and wid not in seen:
                seen.add(wid)
                out.append(wid)
        return out

    def _titles_pending_count(self) -> int:
        """还有多少个工坊 mod 缺当前语言的本地化标题（结果按语言缓存，避免每次 state 都扫盘）。"""
        lang = self.manager.get_language()
        cached = self._titles_pending_cache.get(lang)
        if cached is None:
            from .workshop import needs_title_fetch
            cached = sum(1 for w in self._workshop_ids() if needs_title_fetch(w, lang))
            self._titles_pending_cache[lang] = cached
        return cached

    def _start_title_fill(self) -> None:
        """后台补全缺失的工坊本地化标题（不阻塞扫描，同一时间只跑一个线程）。

        Steam Web API 的 language 参数对工坊标题无效（永远返回基础语言），只有社区
        页面才会按语言返回作者填写的本地化标题。这里为「还没打开过预览」的 mod 抓一次
        标题并落盘，让 mod 列表也能显示工坊原名。已抓过的自动跳过（幂等），请求全走
        全局节流，所以不会额外增加风控压力。
        """
        with self._title_fill_lock:
            if self._title_fill_thread and self._title_fill_thread.is_alive():
                return
            from .workshop import fill_missing_titles

            if not self._workshop_ids():
                return

            def _run():
                try:
                    # 以「当前语言」为准：补全途中用户切了语言，就再补一轮新语言
                    # （最多两轮，避免任何异常情况下无限循环）
                    for _ in range(2):
                        lang = self.manager.get_language()
                        ids = self._workshop_ids()
                        if not ids:
                            break
                        filled = fill_missing_titles(ids, lang)
                        _log.info("[api] 工坊标题补全(%s)完成：新增 %d 个", lang, filled)
                        self._titles_pending_cache.clear()
                        if not self._titles_pending_count():
                            break
                except Exception as e:
                    _log.warning("[api] 工坊标题补全失败: %s", e)
                finally:
                    with self._title_fill_lock:
                        self._title_fill_thread = None
                        self._titles_pending_cache.clear()

            t = threading.Thread(target=_run, daemon=True, name="ws-title-fill")
            self._title_fill_thread = t
            t.start()

    def _start_bg_fill(self) -> None:
        """后台两阶段完整补全（替代 _start_title_fill）。

        阶段 1（快速）：Web API 批量 → 下载预览图（秒级，不碰 HTML）
        阶段 2（慢速）：爬社区页面 HTML 补中文标题/screenshots/stats
        """
        with self._title_fill_lock:  # 复用 title_lock，保证同一时间只跑一个后台线程
            if self._bg_fill_thread and self._bg_fill_thread.is_alive():
                return
            from .workshop import run_bg_fill

            ids = self._workshop_ids()
            if not ids:
                return

            def _run():
                try:
                    lang = self.manager.get_language()
                    run_bg_fill(ids, lang)
                except Exception as e:
                    _log.warning("[api] bg-fill 失败: %s", e)
                finally:
                    with self._title_fill_lock:
                        self._bg_fill_thread = None
                        self._title_fill_thread = None  # 旧的 title_fill 也一起清理
                        self._titles_pending_cache.clear()

            t = threading.Thread(target=_run, daemon=True, name="ws-bg-fill")
            self._bg_fill_thread = t
            self._title_fill_thread = t  # 前端还查 _title_fill_thread.is_alive()，指向同一个
            t.start()

    # ------------------------------------------------------------------ 核心
    def refresh(self) -> dict:
        """启动后台线程执行扫描，立刻返回。前端通过 get_scan_progress 轮询进度和结果。"""

        # 防止用户连续点两次 refresh（或上次异常退出 running 残留）
        from .scanner import _scan_progress, _scan_progress_lock

        def _run():
            from .scanner import finish_scan_progress, set_scan_stage
            try:
                _log.info("[api] refresh (async) start")
                set_scan_stage("scan_mods")
                self._titles_pending_cache.clear()
                # A6：旧格式快照 {mod_id: version} 检测 → 直接重建
                if self._is_old_snapshot_format(self._last_snapshot):
                    _log.info("[api] 检测到旧格式快照，强制重建为 {(mod_id, install_dir): version}")
                    self._last_snapshot = self._rebuild_snapshot()
                before = self._last_snapshot
                self.manager.refresh()
                # 启动后台两阶段补全（Web API 快速预览图 → 慢速 HTML 补中文）
                self._start_bg_fill()
                # 扫 godot.log 拿大型 mod 子内容（角色/敌人/卡牌池），和 store 刷新并行无冲突
                try:
                    from .scanner import scan_mod_subcontents_from_log
                    self._cached_subcontents = scan_mod_subcontents_from_log()
                except Exception:
                    self._cached_subcontents = {}
                set_scan_stage("finalize")
                after = self._snapshot()
                state = self.get_state()
                state["scan_report"] = self._scan_report(before, after)
                # 先返回让 UI 立即渲染！workshop_updates 和 auto_backup 都丢后台
                state["workshop_updates"] = {"updates": [], "checked": 0, "errors": 0, "pending": True}
                self._last_snapshot = after
                finish_scan_progress(result=state)
                _log.info("[api] refresh (async) UI-ready — 后端补充任务继续跑")

                # 后台跑：Steam 工坊更新检测（批量 API + 24h 缓存，首次 ~1s，后续 0s）
                def _post_check():
                    try:
                        upd = self.check_workshop_updates()
                        try:
                            with _WINDOW_LOCK:
                                for w in webview.windows:
                                    try:
                                        w.evaluate_js(
                                            "window.__refreshWorkshopUpdates && window.__refreshWorkshopUpdates()"
                                        )
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    except Exception as e:
                        _log.warning("[api] workshop update check failed: %s", e)
                    # auto_backup 也放这里——所有非 UI 关键的后处理
                    self._auto_backup_updated()
                    _log.info("[api] refresh (async) done — 后处理完成")

                _IO_POOL.submit(_post_check)
            except Exception as e:
                _log.exception("[api] refresh (async) error: %s", e)
                finish_scan_progress(error=str(e))

        # 同一时刻只允许一个 refresh 启动；running 在起线程前写入，关闭检查/写入之间的竞态
        with self._refresh_start_lock:
            if self.get_scan_progress().get("running"):
                return {"ok": False, "error": "scan_already_running"}
            with _scan_progress_lock:
                _scan_progress["running"] = True
            try:
                threading.Thread(target=_run, daemon=True).start()
            except Exception:
                with _scan_progress_lock:
                    _scan_progress["running"] = False
                raise
        return {"running": True}

    def _snapshot(self) -> dict:
        """当前所有物理副本的 {(lower mod_id, version_key): version} 快照。

        2026-11-04：从 {mod_id: version} 改为 {(mod_id, version_key): version}。
        多副本 mod（如同时有 workshop + local 版本）删一个副本后，
        旧逻辑因为 mod_id 还存在所以算不出 deleted；
        新逻辑按物理副本粒度跟踪，每个 install_dir 独立统计。
        """
        snap: dict = {}
        for low, info in self.manager.all_mods.items():
            versions = self.manager._mod_versions.get(low, [])
            if versions:
                for v in versions:
                    vk = str(v.install_dir) if v.install_dir else ''
                    if vk:
                        snap[(low, vk)] = v.manifest.version or ''
            # fallback：没 _mod_versions 时用 canonical info
            vk = str(info.install_dir) if info.install_dir else ''
            if vk and (low, vk) not in snap:
                snap[(low, vk)] = info.manifest.version or ''
        return snap

    def _is_old_snapshot_format(self, snap) -> bool:
        """判断快照是否为旧格式 {mod_id: version}（字符串键，单值）。"""
        if not snap:
            return False
        for k in snap.keys():
            if isinstance(k, tuple) and len(k) == 2:
                return False   # 新格式: (mod_id, install_dir)
            return True        # 旧格式: "mod_id"
        return False

    def _rebuild_snapshot(self) -> dict:
        """强制重建快照为新格式（丢弃旧格式的 in-memory _last_snapshot）。"""
        return self._snapshot()

    def _scan_report(self, before: Optional[dict], after: dict) -> dict:
        """对比前后两次扫描，统计新增 / 更新 / 删除（物理副本粒度）。

        注意：
        - total = len(after)，和 diff 同粒度（物理副本数，非 mod_id 数）
        - "更新"只比 version 字段；内容变了但 version 没变检测不到
        - mod_deleted 是内部统计，UI 不直接消费（定论 #8）
        """
        total = len(after)

        # 2026-10-15：ghost_removed（残留目录清理）也算 deleted
        ghost_deleted = getattr(self.manager, '_last_ghost_removed', 0) or 0

        if before is None:
            # 首次扫描：没有基线，不算现有 mod 为 new，但 ghost 要算 deleted
            return {"total": total, "new": 0, "updated": 0,
                    "deleted": ghost_deleted, "known": total + ghost_deleted}

        new = sum(1 for k in after if k not in before)
        deleted = sum(1 for k in before if k not in after)
        updated = sum(
            1 for k in after
            if k in before and (after.get(k) or "") != (before.get(k) or "")
        )
        # mod_id 级别的聚合（内部用，UI 不直接暴露）
        before_ids = {k[0] for k in before}
        after_ids = {k[0] for k in after}
        mod_deleted = len(before_ids - after_ids)

        return {
            "total": total,
            "new": new,           # 物理副本级新增
            "updated": updated,   # 物理副本级版本变化
            "deleted": deleted + ghost_deleted,  # 物理副本级删除 + ghost
            "mod_deleted": mod_deleted,   # 整个 mod_id 消失（内部用）
            "known": total + deleted + ghost_deleted,
        }

    def clear_workshop_cache(self) -> dict:
        """智能清理缓存：删除已删除/取消订阅 mod 的孤儿缓存，并为工坊有更新的 mod
        增量重下预览缓存。返回 {removed, checked, refreshing, errors}。

        设置页「清理缓存」按钮调用（前端 clearWorkshopCache）。
        """

        from .steam_webapi import get_workshop_batch_info
        from .workshop import (
            cleanup_workshop_cache,
            fetch_workshop_time_updated,
            get_cached_mtime,
        )

        ws_mods = []
        for info in self.manager.all_mods.values():
            if info.source == "workshop" and info.workshop_id:
                ws_mods.append(info)

        # 1) 删除孤儿缓存（已删除/取消订阅的 mod）
        valid_ids = {info.workshop_id for info in ws_mods}
        removed = cleanup_workshop_cache(valid_ids)

        # 2) 对比工坊更新：工坊 time_updated 比本地缓存时间新则重新抓取
        def _check(info):
            local = get_cached_mtime(info.workshop_id)
            remote = fetch_workshop_time_updated(info.workshop_id)
            return info, local, remote

        to_refresh: list[str] = []
        checked = 0
        errors = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            for info, local, remote in ex.map(_check, ws_mods):
                checked += 1
                if remote is None:
                    errors += 1
                    continue
                # 无本地缓存 或 工坊比本地新（5 分钟容差）
                if local == 0 or remote > local + 300:
                    to_refresh.append(info.workshop_id)

        def _run():
            lang = self.manager.get_language()
            batch = list({str(w).strip() for w in to_refresh if str(w).strip()})
            if batch:
                try:
                    get_workshop_batch_info(batch, force_refresh=True, language=lang)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()
        return {
            "removed": removed,
            "checked": checked,
            "refreshing": len(to_refresh),
            "errors": errors,
        }

    def clear_dll_cache(self) -> dict:
        """清空 mod 分析缓存（DLL 元数据 + PCK 资源路径）。设置页「清空缓存」按钮调用。"""
        from .dllinfo import clear_dll_cache as _clear_dll
        from .pckinfo import clear_pck_cache as _clear_pck

        _clear_dll()
        _clear_pck()
        return {"cleared": True}

    def clear_workshop_cache_all(self) -> dict:
        """清理全部工坊预览缓存，并立刻重新下载所有工坊 mod 的预览。"""

        from .workshop import (
            clear_workshop_cache as _clear,
            fetch_workshop_preview,
            reset_workshop_progress,
            tick_workshop_progress,
        )

        ws_ids = [
            info.workshop_id
            for info in self.manager.all_mods.values()
            if info.source == "workshop" and info.workshop_id
        ]
        _clear()
        language = self.manager.get_language()

        def _run():

            reset_workshop_progress(len(ws_ids))
            for ws_id in ws_ids:
                try:
                    fetch_workshop_preview(ws_id, language, _skip_html=True)
                except Exception:
                    pass
                finally:
                    tick_workshop_progress()
                # 串行抓取 + 小延迟，降低被 Steam 限流的概率
                time.sleep(0.4)

        threading.Thread(target=_run, daemon=True).start()
        return {"cleared": True, "refreshing": len(ws_ids)}

    def get_scan_progress(self) -> dict:
        """返回扫描进度（done/total/stage/current/result），供前端轮询。"""
        from .scanner import get_scan_progress as _gsp
        return _gsp()

    def get_backup_progress(self) -> dict:
        """返回全量备份进度（total/processed/done/failed/skipped），供前端轮询。"""
        with self._backup_progress_lock:
            return dict(self._backup_progress)

    def get_workshop_cache_progress(self) -> dict:
        """返回批量重新下载预览的进度（done/total），供正下方进度条轮询。"""
        from .workshop import get_workshop_progress

        return get_workshop_progress()

    # ------------------------------------------------------------ 皮肤/卡面 --

    def _is_character(self, info: ModInfo) -> bool:
        """角色 mod（新增可游玩角色）判断：名称/描述关键词 + DLL 元数据 + 持久化结果。"""
        from .scanner import is_character_mod

        # 角色 mod 是玩法内容（affects_gameplay=true）；皮肤/卡面/语音替换是 affects=false，
        # 它们常提到「角色/character」或替换角色选择界面，但并不是「新增角色」。
        if not info.manifest.affects_gameplay:
            return False

        # 持久化结果优先：之前已识别为角色 mod，清除工坊缓存后仍有效。
        # 但若当前标签已明确归为其它玩法细分（新遗物/新卡牌/…），说明此前是误判，纠正之。
        if self.manager.is_known_character(info.mod_id):
            auto_tags = set(self.manager.get_mod_tags(info.mod_id))
            non_char_gameplay = {
                AUTO_TAG_RELIC, AUTO_TAG_NEW_CARD, AUTO_TAG_EVENT,
                AUTO_TAG_POTION, AUTO_TAG_ENEMY, AUTO_TAG_POWER, AUTO_TAG_MAP,
            }
            if auto_tags & non_char_gameplay:
                self.manager.unmark_character(info.mod_id)
                return False
            return True

        if is_character_mod(info):
            self.manager.mark_character(info.mod_id)
            return True

        # DLL 元数据：含 CharacterSelect / AllCharacters 相关类型 => 角色 mod。
        # 但库/依赖类 mod（如 BaseLib）的 DLL 也常含 CharacterSelect 相关类型，
        # 却不是「新增角色」，所以库类 mod 不走 DLL 信号。
        if self._looks_like_library(info):
            return False

        sig = self.manager._dll_signals(info)
        if sig and sig.get("ok") and sig.get("has_character"):
            self.manager.mark_character(info.mod_id)
            return True

        # 工坊 tag：作者明确标注 Character/角色 的角色 mod（如 PenanceMod 通过皮肤替换机制
        # 实现新角色，DLL 里没有 charselect/characterselect/allcharacters 等标准信号）。
        if info.workshop_id:
            from .workshop import get_cached_tags
            if any(
                t.strip().lower() == "character" or t.strip() == "角色"
                for t in get_cached_tags(info.workshop_id)
            ):
                self.manager.mark_character(info.mod_id)
                return True
        return False

    def _looks_like_library(self, info: ModInfo) -> bool:
        """按名称/描述关键词粗判「库/依赖类 mod」（用于排除角色误判）。"""
        hay = " ".join([
            info.display_name or "",
            info.manifest.description or "",
        ]).lower()
        return any(k.lower() in hay for k in AUTO_TAG_LIB_KEYWORDS)

    def _tag_role(self, info: ModInfo, character_roles: Optional[list[dict]] = None) -> Optional[str]:
        """标签关联：mod 打了「外观」+ 某个角色标签（官方角色或新增角色 mod），返回该角色 id。

        官方角色标签表示「这个 mod 属于某个官方角色」；角色 mod 标签表示「这个 mod 是某个
        新增角色的皮肤/卡面」。角色 mod 自身也可能有额外皮肤/卡面（由其它 mod 提供），靠此关联识别。
        """
        from .scanner import SKIN_ROLE_LABELS

        tags = set(self.manager.get_mod_tags(info.mod_id))
        if "外观" not in tags:
            return None
        tags_lower = {t.lower() for t in tags}
        for rid, label in SKIN_ROLE_LABELS.items():
            if label.lower() in tags_lower:
                return rid
        if character_roles is None:
            character_roles = self._character_roles()
        for c in character_roles:
            if c["id"].lower() in tags_lower or c["label"].lower() in tags_lower:
                return c["id"]
        return None

    def _character_roles(self) -> list[dict]:
        """返回启用列表里的「角色 mod」作为额外角色：{id, label, is_character}。"""
        roles: list[dict] = []
        for info in self.manager.ordered_enabled():
            if not self._is_character(info):
                continue
            roles.append({
                "id": info.mod_id,
                "label": info.display_name,
                "is_character": True,
            })
        return roles

    def _variant_display(self, variant: str, mod_by_id_lower: dict) -> str:
        """把皮肤变体名映射成显示名：优先 mod 显示名，其次原变体名。"""
        low = variant.lower()
        info = mod_by_id_lower.get(low)
        if info is not None:
            return info.display_name
        return variant

    def _is_mod_enabled(self, mod_id: str) -> bool:
        """判断 mod 是否物理启用（store 没暴露 is_enabled，内部集合判断）。"""
        low = mod_id.lower()
        for info in self.manager.ordered_enabled():
            if info.mod_id.lower() == low:
                return True
        return False

    # ------------------------------------------------------------------ 辅助：从已启用 mod 收集皮肤/卡面 mod
    def _collect_appearance_mods(self, kind: str) -> list[tuple["ModInfo", str]]:
        """从所有 mod（启用+停用）里识别皮肤/卡面 mod，返回 [(ModInfo, role_id), ...]。

        kind: "skin" 或 "card"。
        识别信号（按优先级叠加，命中任一即纳入）：
          1) mod_tags 里有 AUTO_TAG_SKIN / AUTO_TAG_CARD_ART（精确细分）
          2) _tag_role 标签关联（mod 打了「外观」+ 角色 tag）
          3) 名字关键词自动识别（复用 CARD_ROLE_KEYWORDS，皮肤/卡面通用）

        去重规则：有精确细分标签的 mod 只进对应 kind，不跨类。
        """
        from .store import AUTO_TAG_SKIN, AUTO_TAG_CARD_ART

        results: list[tuple["ModInfo", str]] = []
        seen_ids: set[str] = set()

        all_infos = list(self.manager.all_mods.values())
        character_roles = self._character_roles()

        for info in all_infos:
            low = info.mod_id.lower()
            if low in seen_ids:
                continue
            tags = set(self.manager.get_mod_tags(info.mod_id))

            is_target = False
            has_skin_tag = AUTO_TAG_SKIN in tags
            has_card_tag = AUTO_TAG_CARD_ART in tags

            # 精确细分：有皮肤/卡面细分标签的，只进对应 kind
            if kind == "skin":
                if has_skin_tag:
                    is_target = True
            else:  # card
                if has_card_tag:
                    is_target = True

            # 标签关联：有角色归属的外观 mod（不受精确细分限制——皮肤 mod 打了角色 tag 也进）
            role_id = self._tag_role(info, character_roles)
            if role_id and "外观" in tags:
                # 但如果有精确细分标签且和当前 kind 不匹配，排除
                if kind == "skin" and has_card_tag and not has_skin_tag:
                    pass  # 卡面精确标签 → 不进皮肤
                elif kind == "card" and has_skin_tag and not has_card_tag:
                    pass  # 皮肤精确标签 → 不进卡面
                else:
                    is_target = True

            # 名字关键词自动识别（复用卡面关键词，皮肤/卡面通用）
            # 条件：名字必须有细分外观信号（skin/卡面/portrait 等）——纯特效/动画 mod 名字里没这些
            # 关键词，虽然 tag 是「外观」但不该进皮肤/卡面列表
            if not is_target:
                kr = self._card_role(info.mod_id)
                if kr and kr != "other" and "外观" in tags:
                    low_name = info.mod_id.lower()
                    # 名字必须有细分外观信号才进
                    SKIN_CARD_HINT = ("skin", "portrait", "card", "皮肤", "卡面", "卡图", "立绘")
                    has_hint = any(h in low_name for h in SKIN_CARD_HINT)
                    if has_hint:
                        is_target = True

            if not is_target:
                continue

            # 确定角色归属（皮肤和卡面都用同一套关键词识别）
            if role_id is None:
                role_id = self._card_role(info.mod_id)
                # 没匹配到官方角色关键词 → 查角色 mod id
                if role_id == "other":
                    for c in character_roles:
                        if c["id"].lower() in low or c["label"].lower() in low:
                            role_id = c["id"]
                            break

            results.append((info, role_id))
            seen_ids.add(low)

        return results

    def get_skin_state(self) -> dict:
        """返回皮肤管理状态。

        移除了对 Sts2SkinManager skin_choices.json 的强依赖：
          - 无论配置存在与否，都从已启用 mod 按角色标签/关键词/标签关联识别皮肤 mod
          - 按角色分组，已启用的皮肤 mod 即为当前激活变体（互斥）
          - 若 skin_choices.json 存在，额外读取其中的 active / available_variants 做补充
        返回的 roles[i].variants[j].active 直接反映 mod 的物理启用状态。
        """
        from .scanner import SKIN_ROLE_LABELS, load_skin_choices

        config_data = load_skin_choices()  # 可能为 None
        config_found = config_data is not None
        enabled_lower = {i.mod_id.lower() for i in self.manager.ordered_enabled()}
        mod_by_id_lower = {}
        for mid, info in self.manager.all_mods.items():
            mod_by_id_lower[mid.lower()] = info

        # 收集所有皮肤 mod（含启用+停用），按角色分组
        all_skins = self._collect_appearance_mods("skin")
        character_roles = self._character_roles()

        # 合并：config 里的 available_variants 也纳入（兼容 Sts2SkinManager 用户）
        if config_found and isinstance(config_data, dict):
            for role_id, role_data in config_data.items():
                if role_id.startswith("_") or not isinstance(role_data, dict):
                    continue
                for v in role_data.get("available_variants", []) or []:
                    if not isinstance(v, str) or v == "default":
                        continue
                    low = v.lower()
                    if low in {info.mod_id.lower() for info, _ in all_skins}:
                        continue  # 已通过标签/关键词识别过
                    info = mod_by_id_lower.get(low)
                    if info is None:
                        continue
                    # 推断角色：config 里已经给了 role_id
                    all_skins.append((info, role_id))

        # 角色顺序：SKIN_ROLE_LABELS → 配置里额外的 → 角色 mod
        role_order: list[str] = []
        for rid in SKIN_ROLE_LABELS:
            role_order.append(rid)
        if config_found:
            for rid in config_data:
                if rid.startswith("_") or rid in role_order:
                    continue
                role_order.append(rid)
        for c in character_roles:
            if c["id"] not in role_order:
                role_order.append(c["id"])

        # 按角色分组（过滤 other 角色）
        from collections import defaultdict
        grouped: dict[str, list[ModInfo]] = defaultdict(list)
        seen_per_role: dict[str, set[str]] = defaultdict(set)
        for info, role_id in all_skins:
            if role_id == "other":
                continue
            low = info.mod_id.lower()
            if low in seen_per_role[role_id]:
                continue
            # 角色 mod 的「自己」不是皮肤变体，排除
            if any(c["id"].lower() == low for c in character_roles):
                continue
            grouped[role_id].append(info)
            seen_per_role[role_id].add(low)

        # 同角色内去重：相似名字/同功能的多个变体，优先保留 workshop 来源的
        # 场景：workshop item 平铺扫到多 manifest（正确的一个），同时游戏 mods_disabled/ 里有旧副本
        def _dedup_variants(mods: list[ModInfo]) -> list[ModInfo]:
            buckets: dict[str, list[ModInfo]] = defaultdict(list)
            for m in mods:
                if m.install_dir is not None:
                    try:
                        parent = str(m.install_dir.parent).lower()
                    except Exception:
                        parent = ""
                else:
                    parent = ""
                # 名字简化：去掉 _Public / _数字后缀等
                short = m.mod_id.lower()
                short = short.replace("public", "").replace("_public", "")
                short = re.sub(r"_\d+$", "", short)
                short = short.strip("_")
                buckets[(parent, short)].append(m)

            result: list[ModInfo] = []
            for _key, group in buckets.items():
                if len(group) == 1:
                    result.append(group[0])
                else:
                    def _score(m: ModInfo):
                        ws = 1 if m.workshop_id and m.source == "workshop" else 0
                        en = 1 if m.mod_id.lower() in enabled_lower else 0
                        dirty = 1 if re.search(r"_\d+$", m.mod_id) or "public" in m.mod_id.lower() else 0
                        return (ws, en, -dirty, len(m.mod_id))
                    group.sort(key=_score, reverse=True)
                    result.append(group[0])
            return result

        roles = []
        for role_id in role_order:
            infos = _dedup_variants(grouped.get(role_id, []))
            if not infos and role_id not in SKIN_ROLE_LABELS and not any(c["id"] == role_id for c in character_roles):
                continue  # 跳过 other 和空角色
            variants = [{"id": "default", "label": "默认皮肤", "is_default": True, "active": False}]
            current_active = "default"
            for info in infos:
                low = info.mod_id.lower()
                is_active = low in enabled_lower
                if is_active:
                    current_active = info.mod_id
                variants.append({
                    "id": info.mod_id,
                    "label": info.display_name,
                    "is_default": False,
                    "active": is_active,
                })
            # 没皮肤 mod 的官方角色也展示（让用户看到「该角色没装皮肤」）
            if role_id in SKIN_ROLE_LABELS or any(c["id"] == role_id for c in character_roles):
                if current_active == "default" and len(variants) == 1:
                    # 角色 mod 没额外皮肤 → 不显示（避免空条目）
                    if any(c["id"] == role_id for c in character_roles):
                        continue
                label = SKIN_ROLE_LABELS.get(role_id, role_id)
                is_character_role = any(c["id"] == role_id for c in character_roles)
                if is_character_role:
                    for c in character_roles:
                        if c["id"] == role_id:
                            label = c["label"]
                            break
                roles.append({
                    "id": role_id,
                    "label": label,
                    "active": current_active,
                    "variants": variants,
                    "is_character": is_character_role,
                })

        return {
            "available": True,
            "roles": roles,
            "config_found": config_found,
            "manager": "sts2skinmanager" if config_found else "native",
        }

    def _set_appearance_enabled(self, kind: str, role: str, variant: str) -> tuple[list, Optional[ModInfo]]:
        """皮肤/卡面互斥启停的核心逻辑（被 set_skin_active / set_card_active 共享）。

        kind: "skin" | "card"
        variant: "default" 表示全停
        返回 (role_mods, target_info) —— 供调用方做各自的兼容 JSON 写回。
        找不到 target 时返回 ([], None) 让调用方输出错误。
        """
        # 1) 收该角色下所有外观 mod
        all_matches = self._collect_appearance_mods(kind)
        role_mods = [info for info, rid in all_matches if rid == role]

        if variant == "default":
            # 全部停用
            for info in role_mods:
                if self._is_mod_enabled(info.mod_id):
                    self.manager.set_enabled(info.mod_id, False)
            return role_mods, None

        # 2) 找到 target_info
        target_info = next((i for i in role_mods if i.mod_id.lower() == variant.lower()), None)
        if target_info is None:
            ti = self.manager.active_version(variant)
            if ti is None:
                return [], None
            target_info = ti

        # 3) 互斥：先停同角色其他 → 再启目标（避免游戏加载冲突）
        for info in role_mods:
            if info.mod_id.lower() != variant.lower() and self._is_mod_enabled(info.mod_id):
                self.manager.set_enabled(info.mod_id, False)
        if not self._is_mod_enabled(target_info.mod_id):
            self.manager.set_enabled(target_info.mod_id, True)

        return role_mods, target_info

    def set_skin_active(self, role: str, variant: str) -> dict:
        """切换某角色的激活皮肤：互斥启停 + Sts2SkinManager 兼容写回。"""
        from .scanner import load_skin_choices, save_skin_choices

        role_mods, target = self._set_appearance_enabled("skin", role, variant)
        if not role_mods and target is None:
            return {"ok": False, "error": self._ui("skin_unavailable"), **self.get_skin_state()}

        # 兼容 skin_choices.json：写回 role[active] + available_variants
        data = load_skin_choices()
        if data is not None and isinstance(data, dict):
            role_data = data.get(role)
            if not isinstance(role_data, dict):
                role_data = {}
                data[role] = role_data
            variants_list = role_data.get("available_variants")
            if not isinstance(variants_list, list):
                variants_list = []
                role_data["available_variants"] = variants_list
            if variant != "default" and variant not in variants_list:
                variants_list.append(variant)
            role_data["active"] = variant
            save_skin_choices(data)

        return {"ok": True, "error": "", **self.get_skin_state()}

    # ------------------------------------------------------------ 卡面管理 --

    def _card_role(self, card_id: str) -> str:
        """确定卡面 mod 的角色：手动指定优先，其次按名字关键词自动识别，再匹配角色 mod，最后归「其它」。"""
        manual = self.manager.get_card_role(card_id)
        if manual:
            return manual
        from .scanner import CARD_ROLE_KEYWORDS

        low = card_id.lower()
        for role, kws in CARD_ROLE_KEYWORDS.items():
            for kw in kws:
                if kw.lower() in low:
                    return role
        # 角色 mod 匹配：卡面 mod 名里包含某角色 mod 的 id 时归到该角色
        for info in self.manager.ordered_enabled():
            if not self._is_character(info):
                continue
            cid = info.mod_id.lower()
            if cid and cid in low:
                return info.mod_id
        return "other"

    def get_card_state(self) -> dict:
        """返回卡面管理状态（同皮肤，不依赖 skin_choices.json）。"""
        from .scanner import SKIN_ROLE_LABELS, load_skin_choices

        config_data = load_skin_choices()
        config_found = config_data is not None
        enabled_lower = {i.mod_id.lower() for i in self.manager.ordered_enabled()}

        # 收集所有卡面 mod（含启用+停用），按角色分组
        all_cards = self._collect_appearance_mods("card")
        character_roles = self._character_roles()
        char_role_ids = {c["id"].lower() for c in character_roles}

        # 合并 config 里的 _card_packs（兼容 Sts2SkinManager 用户）
        if config_found and isinstance(config_data, dict):
            packs = config_data.get("_card_packs")
            if isinstance(packs, dict):
                ordering = packs.get("ordering") or []
                existing_ids = {info.mod_id.lower() for info, _ in all_cards}
                for cid in ordering:
                    if not isinstance(cid, str) or cid.lower() in existing_ids:
                        continue
                    info = self.manager.active_version(cid)
                    if info is None:
                        continue
                    role_id = self._card_role(cid)
                    all_cards.append((info, role_id))

        # 角色顺序：SKIN_ROLE_LABELS → other → 角色 mod
        role_order: list[str] = list(SKIN_ROLE_LABELS.keys()) + ["other"]
        for c in character_roles:
            if c["id"] not in role_order:
                role_order.append(c["id"])

        # 按角色分组（过滤 other）
        from collections import defaultdict
        grouped: dict[str, list[ModInfo]] = defaultdict(list)
        seen_per_role: dict[str, set[str]] = defaultdict(set)
        for info, role_id in all_cards:
            if role_id == "other":
                continue
            low = info.mod_id.lower()
            if low in seen_per_role[role_id]:
                continue
            # 角色 mod 自己不是卡面变体
            if low in char_role_ids:
                continue
            grouped[role_id].append(info)
            seen_per_role[role_id].add(low)

        roles = []
        for rid in role_order:
            infos = grouped.get(rid, [])
            if not infos and rid not in SKIN_ROLE_LABELS and rid not in char_role_ids:
                continue  # 跳过 other 和空角色

            current_active = "default"
            variants = [{"id": "default", "label": "默认卡面", "is_default": True, "active": False}]
            for info in infos:
                low = info.mod_id.lower()
                is_active = low in enabled_lower
                if is_active:
                    current_active = info.mod_id
                variants.append({
                    "id": info.mod_id,
                    "label": info.display_name,
                    "is_default": False,
                    "active": is_active,
                })

            # 角色 mod：排除「自己」只保留额外卡面
            if rid in char_role_ids:
                infos = [i for i in infos if i.mod_id.lower() != rid.lower()]

            is_character_role = rid in char_role_ids
            label = SKIN_ROLE_LABELS.get(rid, rid)
            if is_character_role:
                for c in character_roles:
                    if c["id"] == rid:
                        label = c["label"]
                        break
            roles.append({
                "id": rid,
                "label": label,
                "active": current_active,
                "variants": variants,
                "is_character": is_character_role,
            })

        return {
            "available": True,
            "roles": roles,
            "config_found": config_found,
            "manager": "sts2skinmanager" if config_found else "native",
        }

    def set_card_active(self, role: str, card_id: str) -> dict:
        """切换某角色的激活卡面：互斥启停 + _card_packs 兼容写回。"""
        from .scanner import load_skin_choices, save_skin_choices

        role_mods, target = self._set_appearance_enabled("card", role, card_id)
        if not role_mods and target is None:
            return {"ok": False, "error": self._ui("card_unavailable"), **self.get_card_state()}

        # 兼容 skin_choices.json 的 _card_packs
        data = load_skin_choices()
        if data is not None and isinstance(data, dict):
            packs = data.get("_card_packs")
            if not isinstance(packs, dict):
                packs = {}
            enabled_map = packs.get("enabled")
            if not isinstance(enabled_map, dict):
                enabled_map = {}
            ordering = packs.get("ordering")
            if not isinstance(ordering, list):
                ordering = []

            role_mod_ids = {i.mod_id.lower() for i in role_mods}
            for cid in list(enabled_map.keys()):
                if isinstance(cid, str) and cid.lower() in role_mod_ids:
                    del enabled_map[cid]
            if card_id != "default":
                enabled_map[card_id] = True
                if card_id not in ordering:
                    ordering.append(card_id)
            packs["enabled"] = enabled_map
            packs["ordering"] = ordering
            data["_card_packs"] = packs
            save_skin_choices(data)

        return {"ok": True, "error": "", **self.get_card_state()}

    def set_card_role(self, card_id: str, role: str) -> dict:
        """手动指定卡面 mod 的角色（空字符串表示清除手动指定）。"""
        self.manager.set_card_role(str(card_id), str(role) if role else None)
        return {"ok": True, **self.get_card_state()}

    # ---------------------------------------------------------- 管理器标签 --

    def get_manager_tags(self) -> dict:
        """返回全局标签池 + 当前所有 mod 的标签映射。"""
        return {
            "tags": self.manager.list_manager_tags(),
            "mod_tags": {
                mid.lower(): self.manager.get_mod_tags(mid)
                for mid in self.manager.all_mods
            },
        }

    def add_manager_tag(self, name: str) -> dict:
        ok = self.manager.add_manager_tag(str(name))
        return {"ok": ok, **self.get_manager_tags()}

    def delete_manager_tag(self, name: str) -> dict:
        ok = self.manager.delete_manager_tag(str(name))
        return {"ok": ok, **self.get_manager_tags()}

    def set_mod_tags(self, mod_id: str, tags: list) -> dict:
        def _do():
            self.manager.set_mod_tags(str(mod_id), [str(t) for t in tags])
            return {"ok": True, **self.get_manager_tags()}

        return self._dispatch_async(_do, f"set_mod_tags({mod_id})")

    # ------------------------------------------------------------ P0 mod_meta/color --

    def get_mod_meta(self, mod_id: str) -> dict:
        return {"ok": True, "meta": self.manager.get_mod_meta(str(mod_id))}

    def set_mod_meta(self, mod_id: str, alias: str = "", note: str = "") -> dict:
        self.manager.set_mod_meta(str(mod_id), alias=str(alias), note=str(note))
        return {"ok": True, "meta": self.manager.get_mod_meta(str(mod_id))}

    def set_mod_color(self, mod_id: str, color: str = "") -> dict:
        c = str(color).strip() if color else ""
        self.manager.set_mod_color(str(mod_id), c or None)
        return {"ok": True, "color": self.manager.get_mod_color(str(mod_id))}

    # ------------------------------------------------------------ P0 自定义分组 --

    def get_groups(self) -> dict:
        groups = self.manager.get_groups()
        order = self.manager.get_group_order()
        return {"ok": True, "groups": groups, "group_order": order}

    def create_group(self, name: str) -> dict:
        ok = self.manager.create_group(str(name))
        return {"ok": ok, **self.get_groups()}

    def delete_group(self, name: str) -> dict:
        ok = self.manager.delete_group(str(name))
        return {"ok": ok, **self.get_groups()}

    def rename_group(self, old_name: str, new_name: str) -> dict:
        ok = self.manager.rename_group(str(old_name), str(new_name))
        return {"ok": ok, **self.get_groups()}

    def add_mod_to_group(self, mod_id: str, group_name: str) -> dict:
        ok = self.manager.add_mod_to_group(str(mod_id), str(group_name))
        return {"ok": ok, **self.get_groups()}

    def remove_mod_from_group(self, mod_id: str, group_name: str) -> dict:
        ok = self.manager.remove_mod_from_group(str(mod_id), str(group_name))
        return {"ok": ok, **self.get_groups()}

    def set_group_mods(self, group_name: str, mod_ids: list) -> dict:
        ok = self.manager.set_group_mods(str(group_name), [str(m) for m in mod_ids])
        return {"ok": ok, **self.get_groups()}

    def reorder_group(self, mod_id: str, group_name: str, new_index: int) -> dict:
        ok = self.manager.reorder_group(str(mod_id), str(group_name), int(new_index))
        return {"ok": ok, **self.get_groups()}

    def get_mod_groups(self, mod_id: str) -> dict:
        return {"ok": True, "groups": self.manager.get_mod_groups(str(mod_id))}

    # ------------------------------------------------------------ P1 Pinned --

    def pin_mod(self, mod_id: str, pin_version: bool = True, pin_enabled: bool = False) -> dict:
        """锁定一个 mod。pin_version=True 锁当前版本号；pin_enabled=True 锁启停状态。"""
        mod_id = str(mod_id)
        info = self.manager._mods.get(mod_id.lower())
        version = info.manifest.version if (pin_version and info) else None
        enabled_status = None
        if pin_enabled:
            enabled_status = mod_id.lower() in self.manager._state.enabled_order
        self.manager.set_pinned(mod_id, version=version, enabled=enabled_status)
        pinned = self.manager.get_pinned(mod_id)
        return {"ok": True, "mod_id": mod_id, "pinned": pinned}

    def unpin_mod(self, mod_id: str) -> dict:
        """解除锁定。"""
        self.manager.unpin(str(mod_id))
        return {"ok": True, "mod_id": str(mod_id), "pinned": None}

    def list_pinned(self) -> dict:
        """返回所有锁定的 mods。"""
        return {"ok": True, "pinned": self.manager.all_pinned()}

    # ------------------------------------------------------------ P0 导出 --

    def export_profile(self, format: str = "csv") -> dict:
        """导出当前 profile。format: csv / markdown。返回包含 data 的 dict（前端可触发下载）。"""
        rows = []
        for info in self.manager.ordered_enabled():
            meta = self.manager.get_mod_meta(info.mod_id)
            groups = self.manager.get_mod_groups(info.mod_id)
            rows.append({
                "mod_id": info.mod_id,
                "name": info.display_name,
                "author": info.manifest.author or "",
                "version": info.manifest.version or "",
                "source": info.source_label,
                "workshop_id": info.workshop_id or "",
                "enabled": True,
                "type": info.mod_type_label,
                "alias": meta.get("alias", ""),
                "note": meta.get("note", ""),
                "groups": ";".join(groups),
                "workshop_url": f"https://steamcommunity.com/workshop/filedetails/?id={info.workshop_id}" if info.workshop_id else "",
            })
        for info in self.manager.ordered_disabled():
            meta = self.manager.get_mod_meta(info.mod_id)
            groups = self.manager.get_mod_groups(info.mod_id)
            rows.append({
                "mod_id": info.mod_id,
                "name": info.display_name,
                "author": info.manifest.author or "",
                "version": info.manifest.version or "",
                "source": info.source_label,
                "workshop_id": info.workshop_id or "",
                "enabled": False,
                "type": info.mod_type_label,
                "alias": meta.get("alias", ""),
                "note": meta.get("note", ""),
                "groups": ";".join(groups),
                "workshop_url": f"https://steamcommunity.com/workshop/filedetails/?id={info.workshop_id}" if info.workshop_id else "",
            })

        fmt = str(format).lower()
        if fmt == "markdown":
            lines = ["| 启用 | 类型 | 名称 | 版本 | 作者 | 来源 | 工坊ID | 分组 | 别名 | 备注 |",
                     "|------|------|------|------|------|------|--------|------|------|------|"]
            for r in rows:
                lines.append(f"| {'✅' if r['enabled'] else '❌'} | {r['type']} | {r['name']} | {r['version']} | {r['author']} | {r['source']} | {r['workshop_id']} | {r['groups']} | {r['alias']} | {r['note']} |")
            return {"ok": True, "format": "markdown", "data": "\n".join(lines), "filename": f"modlist_{self.manager.active_profile_name}.md"}
        else:  # csv
            headers = ["启用", "类型", "名称", "版本", "作者", "来源", "工坊ID", "分组", "别名", "备注", "ModID", "工坊链接"]
            lines = [",".join(headers)]
            for r in rows:
                cells = [
                    "Yes" if r["enabled"] else "No",
                    r["type"], r["name"], r["version"], r["author"],
                    r["source"], r["workshop_id"], r["groups"],
                    r["alias"], r["note"], r["mod_id"], r["workshop_url"],
                ]
                lines.append(",".join('"'+str(c).replace('"','""')+'"' for c in cells))
            return {"ok": True, "format": "csv", "data": "\n".join(lines), "filename": f"modlist_{self.manager.active_profile_name}.csv"}

    # ------------------------------------------------------------ 游戏日志 --

    def get_game_log(self, file_name: str = "", level: str = "all",
                     keyword: str = "", use_regex: bool = False) -> dict:
        """读取 STS2 游戏日志（当前 godot.log 或其历史轮转文件），逐行返回供日志面板展示。

        BaseLib 的日志也写在同一个 godot.log 里（行首带 [BaseLib] 前缀），
        所以这里相当于复用 BaseLib 的日志源，只是把显示搬到管理器里。
        只读，不改动任何文件。
        """
        from .scanner import read_game_log

        try:
            return read_game_log(
                file_name=file_name or None,
                level=level or "all",
                keyword=keyword or "",
                use_regex=bool(use_regex),
            )
        except Exception as e:
            _log.warning("[api] get_game_log 失败: %s", e)
            return {
                "available": False, "files": [], "lines": [],
                "name": file_name or "", "path": "",
                "counts": {"info": 0, "warn": 0, "error": 0, "other": 0},
                "total": 0, "shown": 0, "truncated": False,
            }

    # ------------------------------------------------------------ 应用信息 --

    def get_app_info(self) -> dict:
        from . import __version__

        return {"version": __version__}

    def log_js_error(self, msg: str = "", url: str = "", line: int = 0, col: int = 0, stack: str = "") -> None:
        """前端 window.onerror / unhandledrejection 回写的 JS 错误日志。"""
        _log.error("[JS ERROR] %s (at %s:%s:%s)\n%s", msg, url, line, col, stack or "")

    # ------------------------------------------------------------ DLL 分析 --

    def analyze_dlls(self) -> dict:
        """后台线程分析所有含 DLL / PCK 的 mod，前端通过 get_dll_progress 轮询进度。"""

        from .dllinfo import (
            cleanup_dll_cache,
            find_mod_dll,
            inspect_dll,
            reset_dll_progress,
        )
        from .pckinfo import cleanup_pck_cache, find_mod_pck, inspect_pck

        dll_targets = []
        pck_targets = []
        skipped = 0
        for info in self.manager.all_mods.values():
            if info.install_dir is None:
                continue
            # 跳过用户手动设置过标签的 mod：手动标签优先级最高，分析结果不会覆盖它
            if self.manager.has_manual_tags(info.mod_id):
                skipped += 1
                continue
            if info.manifest.has_dll:
                dll = find_mod_dll(info.install_dir, [info.mod_id, info.folder_name])
                if dll is not None:
                    dll_targets.append((info.display_name, dll))
            if info.manifest.has_pck:
                pck = find_mod_pck(info.install_dir, [info.mod_id, info.folder_name])
                if pck is not None:
                    pck_targets.append((info.display_name, pck))

        # 缓存淘汰：删除不属于当前 DLL/PCK 的孤儿缓存（已删除/已更新 mod 的旧缓存）
        valid_dlls = []
        for _name, dll in dll_targets:
            try:
                valid_dlls.append((dll, int(dll.stat().st_mtime)))
            except Exception:
                continue
        removed_dll = cleanup_dll_cache(valid_dlls)

        valid_pcks = []
        for _name, pck in pck_targets:
            try:
                valid_pcks.append((pck, int(pck.stat().st_mtime)))
            except Exception:
                continue
        removed_pck = cleanup_pck_cache(valid_pcks)

        total = len(dll_targets) + len(pck_targets)
        reset_dll_progress(total)

        def _run():
            for name, dll in dll_targets:
                inspect_dll(dll, progress_name=name)
            for name, pck in pck_targets:
                inspect_pck(pck, progress_name=name)

        threading.Thread(target=_run, daemon=True).start()
        return {
            "started": True,
            "total": total,
            "dll": len(dll_targets),
            "pck": len(pck_targets),
            "skipped": skipped,
            "removed_cache": removed_dll + removed_pck,
        }

    def get_dll_progress(self) -> dict:
        from .dllinfo import get_dll_progress as _get

        return _get()

    def get_tag_diagnostics(self) -> dict:
        """返回所有 mod 的标签诊断结果（标签 + 判定依据）。"""
        return {"items": self.manager.diagnose_all_tags()}

    def run_tag_selftest(self) -> dict:
        """对内置样例跑标签识别自检，返回实际 vs 期望（用于发现识别规则回归）。"""
        return {"items": self.manager.run_tag_selftest()}

    # ------------------------------------------------------------ 工坊更新 --

    def _local_mod_mtime(self, mod_dir) -> int:
        """返回 mod 目录下顶层文件的最新修改时间（Unix 时间戳）。"""
        try:
            return max(p.stat().st_mtime for p in mod_dir.iterdir() if p.is_file())
        except Exception:
            return 0

    def check_workshop_updates(self) -> dict:
        """批量检查工坊 mod 是否有更新。

        数据源优先级（零 HTTP 优先）：
          1. steamDB.json 快照（全量本地，有 revision_change_number + time_updated）
          2. meta.zh.json 缓存（之前 bg-fill 写的）
          3. Steam Web API 批量拉（只有缺的才走网络）
        """
        from .steam_webapi import get_workshop_batch_info, query_steamdb, load_steamdb
        from .workshop import _load_cached, _migrate_legacy_meta

        ws_lang = self.manager.get_language()
        mods = []
        for info in self.manager.ordered_enabled():
            if info.source != "workshop" or not info.workshop_id or info.install_dir is None:
                continue
            mods.append(info)

        if not mods:
            return {"updates": [], "checked": 0, "errors": 0}

        # 先触发 steamDB 懒加载（启动时一次性）
        load_steamdb()

        # 1) 优先查 steamDB 快照（零 HTTP，有 revision_change_number）
        time_updated_map: dict[str, int] = {}
        revision_map: dict[str, int] = {}
        missing_meta: list[str] = []
        for info in mods:
            wid = str(info.workshop_id)
            snap = query_steamdb(wid)
            if snap:
                tu = int(snap.get("time_updated") or 0)
                rev = int(snap.get("revision_change_number") or 0)
                if tu > 0:
                    time_updated_map[wid] = tu
                if rev > 0:
                    revision_map[wid] = rev
            # 2) 再补 meta.zh.json 里的（作为 steamDB 缺失时的降级）
            _migrate_legacy_meta(wid)
            cached = _load_cached(wid, ws_lang)
            if cached:
                meta_tu = int(cached.get("time_updated") or 0)
                meta_rev = int(cached.get("revision_change_number") or 0)
                # steamDB 优先，但 meta 有值就补进 map
                if wid not in time_updated_map and meta_tu > 0:
                    time_updated_map[wid] = meta_tu
                if wid not in revision_map and meta_rev > 0:
                    revision_map[wid] = meta_rev
            if wid not in time_updated_map:
                missing_meta.append(wid)

        # 3) 还缺的才批量 API 拉（走网络，最慢）
        if missing_meta:
            _log.info("[api] check_workshop_updates: %d/%d 本地都缺 time_updated 需在线拉",
                      len(missing_meta), len(mods))
            steam_infos = get_workshop_batch_info(missing_meta, language=ws_lang)
            for wid, info in steam_infos.items():
                time_updated_map[wid] = int(getattr(info, "time_updated", 0) or 0)
                # ★ steamDB 补 rev（无 key Web API 不返回）
                snap = query_steamdb(wid)
                if snap and not revision_map.get(wid):
                    rev = int(snap.get("revision_change_number") or 0)
                    if rev > 0:
                        revision_map[wid] = rev

        updates: list[dict] = []
        checked = 0
        errors = 0

        for info in mods:
            checked += 1
            wid = str(info.workshop_id)
            remote_tu = time_updated_map.get(wid, 0)
            remote_rev = revision_map.get(wid, 0)
            if not remote_tu:
                errors += 1
                continue
            local_mtime = self._local_mod_mtime(info.install_dir)
            if local_mtime <= 0:
                errors += 1
                continue
            # ★ 双源判断：revision_change_number（Steam 官方版本号，最准）+ time_updated（改描述也会变）
            # rev > 0 表示 steamDB 有这个 mod 的快照 → 说明它是活跃的 workshop mod
            # rev 不变 + time_updated 小范围波动可能是作者改了截图/描述，也算"有更新"
            is_update = remote_tu > local_mtime + 300  # 5 分钟容差
            if is_update:
                reason = "time_updated"
                if remote_rev > 0:
                    reason = f"rev={remote_rev}"
                updates.append({
                    "id": info.mod_id,
                    "name": info.display_name,
                    "workshop_id": wid,
                    "remote_updated": remote_tu,
                    "remote_revision": remote_rev,
                    "local_mtime": local_mtime,
                    "reason": reason,
                })

        _log.info("[api] check_workshop_updates: %d checked, %d updates, %d errors (steamDB rev hits=%d)",
                  checked, len(updates), errors, len(revision_map))
        return {"updates": updates, "checked": checked, "errors": errors}

    # ---------------------------------------------------- #2 替代 mod 建议 --

    REPLACEMENT_IDLE_DAYS = 180       # mod 停更超过 180 天视为可能停更
    REPLACEMENT_MIN_SUBS_RATIO = 2.0  # 建议替代的 mod 订阅数至少是原 mod 的 2 倍
    REPLACEMENT_MIN_TAG_OVERLAP = 2   # 至少 2 个标签重叠才考虑是同类替代

    def find_replacement_mods(self) -> dict:
        """纯本地替代 mod 建议（零 HTTP 请求，只分析已缓存的数据）。

        逻辑：
          1) 扫已装的 workshop mod，找停更超过 REPLACEMENT_IDLE_DAYS 天 + 订阅数不高的
          2) 在已装 mod 里找同标签 + 更新 + 订阅数高的作为替代建议
          3) 返回建议列表（可能为空）
        """
        from .steam_webapi import _get_cache_dir

        cache_dir = _get_cache_dir()
        if cache_dir is None:
            return {"suggestions": [], "note": "no_cache"}

        # 1) 收集已装 mod 的 SteamWebAPI 缓存数据
        installed: dict[str, dict] = {}  # wid → {tags, subscriptions, time_updated, title, mod_id}
        for info in self.manager.all_mods.values():
            if info.source != "workshop" or not info.workshop_id:
                continue
            wid = str(info.workshop_id)
            f = cache_dir / f"{wid}.json"
            if not f.is_file():
                continue
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            installed[wid] = {
                "mod_id": info.mod_id,
                "display_name": info.display_name or info.mod_id,
                "title": raw.get("title", info.display_name or info.mod_id),
                "tags": set(raw.get("tags") or []),
                "subscriptions": int(raw.get("subscriptions") or 0),
                "time_updated": int(raw.get("time_updated") or 0),
                "workshop_id": wid,
                "enabled": info.enabled,
            }

        if len(installed) < 2:
            return {"suggestions": [], "note": "too_few_mods"}

        now = int(time.time())
        idle_threshold = now - self.REPLACEMENT_IDLE_DAYS * 86400

        # 2) 标记停更候选
        idle: list[dict] = []
        for wid, m in installed.items():
            if m["time_updated"] > 0 and m["time_updated"] < idle_threshold:
                idle.append(m)
        # 按订阅数升序（订阅少的更可能停更且有替代）
        idle.sort(key=lambda m: m["subscriptions"])

        suggestions: list[dict] = []

        # 3) 对每个停更 mod，找本地同标签热门替代
        for old in idle:
            old_tags = old["tags"]
            if not old_tags:
                continue
            best_match = None
            best_score = 0
            for wid_new, new in installed.items():
                if wid_new == old["workshop_id"]:
                    continue
                # 订阅数不够多 → 不可能替代
                if new["subscriptions"] < old["subscriptions"] * self.REPLACEMENT_MIN_SUBS_RATIO:
                    continue
                # 必须更新（time_updated 更大）
                if new["time_updated"] <= old["time_updated"]:
                    continue
                # 标签重叠
                overlap = len(old_tags & new["tags"])
                if overlap < self.REPLACEMENT_MIN_TAG_OVERLAP:
                    continue
                # 综合评分：标签重叠 × 订阅倍数 × 新鲜度
                ratio = new["subscriptions"] / max(old["subscriptions"], 1)
                freshness = (new["time_updated"] - old["time_updated"]) / 86400  # 差多少天
                score = overlap * 10 + min(ratio, 10) * 2 + min(freshness / 30, 6)
                if score > best_score:
                    best_score = score
                    best_match = new

            if best_match:
                suggestions.append({
                    "replace": {
                        "mod_id": old["mod_id"],
                        "name": old["display_name"],
                        "workshop_id": old["workshop_id"],
                        "idle_days": (now - old["time_updated"]) // 86400,
                        "subscriptions": old["subscriptions"],
                        "last_updated": old["time_updated"],
                    },
                    "with": {
                        "mod_id": best_match["mod_id"],
                        "name": best_match["display_name"],
                        "workshop_id": best_match["workshop_id"],
                        "subscriptions": best_match["subscriptions"],
                        "last_updated": best_match["time_updated"],
                        "tags_overlap": len(old_tags & best_match["tags"]),
                    },
                })

        suggestions.sort(key=lambda s: s["replace"]["idle_days"], reverse=True)

        _log.info("[api] find_replacement_mods: %d idle candidates, %d suggestions",
                  len(idle), len(suggestions))
        return {
            "suggestions": suggestions,
            "idle_count": len(idle),
            "scanned_count": len(installed),
            "note": "ok" if suggestions else "no_local_replacements",
        }

    # ---------------------------------------------------- #4 steamDB 导出 --

    def export_steamdb(self) -> dict:
        """导出 RimCrow 格式的 steamDB.json（把本地缓存组织成静态数据库）。

        位置：<项目>/tools/steamDB.json
        ⚠️ 旧函数——推荐用 tools/build_steamdb.py（走 QueryFiles/v1/ 全量拉 API）
        """
        from pathlib import Path as _Path
        tools_root = _Path(__file__).resolve().parent.parent / "tools"
        db_path = tools_root / "steamDB.json"

        database: dict[str, dict] = {}

        # 1) 遍历 SteamWebAPI 缓存
        if api_cache and api_cache.is_dir():
            for f in api_cache.glob("*.json"):
                try:
                    raw = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                wid = str(raw.get("workshop_id") or f.stem)
                if not wid or not wid.isdigit():
                    continue
                entry: dict = {
                    "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
                    "steamName": raw.get("title", ""),
                    "name": "",
                    "dependencies": {},
                    "tags": raw.get("tags") or [],
                    "subscriptions": raw.get("subscriptions", 0),
                    "time_updated": raw.get("time_updated", 0),
                    "cache_ts": raw.get("_cache_ts", 0),
                }
                database[wid] = entry

        # 2) 补充 workshop 本地缓存（deps.json 依赖 + meta.<lang>.json 标题/标签）
        if ws_cache_root and ws_cache_root.is_dir():
            from .workshop import load_cached_any, read_deps_meta as _read_deps_meta

            lang = self.manager.get_language()
            for d in ws_cache_root.iterdir():
                if not d.is_dir():
                    continue
                wid = d.name
                deps_meta = _read_deps_meta(wid)
                cached_meta = load_cached_any(wid, lang) or {}
                if not deps_meta and not cached_meta:
                    continue
                if wid not in database:
                    # 没有 WebAPI 缓存但有本地缓存 → 也记录一条
                    database[wid] = {
                        "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
                        "steamName": cached_meta.get("title", ""),
                        "name": "",
                        "dependencies": {},
                        "tags": cached_meta.get("tags") or [],
                        "subscriptions": 0,
                        "time_updated": 0,
                        "cache_ts": deps_meta.get("deps_fetched_at", 0),
                    }
                # 合并依赖
                deps = deps_meta.get("dependencies") or []
                dep_map: dict[str, str] = {}
                for dep in deps:
                    if isinstance(dep, dict):
                        dwid = str(dep.get("workshop_id") or "")
                        dname = dep.get("name", "")
                        if dwid:
                            dep_map[dwid] = dname
                if dep_map:
                    database[wid]["dependencies"].update(dep_map)

        # 3) 补充本地 mod name（如果 wid 在已装列表里）
        for info in self.manager.all_mods.values():
            if info.source != "workshop" or not info.workshop_id:
                continue
            wid = str(info.workshop_id)
            if wid in database:
                database[wid]["name"] = info.display_name or info.mod_id

        # 4) 写文件
        payload = {
            "version": int(time.time()),
            "database": database,
            "_source": "SpireModManager local export",
            "_mod_count": len(database),
        }
        data_root.mkdir(parents=True, exist_ok=True)
        try:
            db_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": str(e), "entries": len(database)}

        dep_total = sum(1 for v in database.values() if v["dependencies"])
        _log.info("[api] export_steamdb: %d entries (%d with deps) → %s",
                  len(database), dep_total, db_path)
        return {
            "ok": True,
            "path": str(db_path),
            "entries": len(database),
            "with_deps": dep_total,
        }

    # ----------------------------------------------- #3 多人联机兼容性检测 --

    # 关键词库（用于扫描 mod 名称/描述/标签）
    _MP_TAGS_FULL = {"Multiplayer", "multiplayer", "Online", "online", "Co-op", "co-op", "coop"}
    _MP_DESC_FULL = [
        "multiplayer is fully supported", "multiplayer fully supported",
        "fully supports multiplayer", "supports multiplayer",
        "works in multiplayer", "works with multiplayer",
        "compatible with multiplayer", "multiplayer compatible",
        "multiplayer support", "online support",
        "co-op compatible", "coop compatible",
        "支持多人联机", "支持联机", "多人联机兼容", "联机兼容",
    ]
    _MP_DESC_PARTIAL = [
        "some multiplayer", "partial multiplayer",
        "not fully supported", "not fully compatible",
        "may not work in multiplayer", "might not work in multiplayer",
        "部分联机功能", "部分多人",
    ]
    _MP_DESC_INCOMPATIBLE = [
        "not compatible with multiplayer", "not multiplayer compatible",
        "single player only", "single-player only",
        "multiplayer: n/a", "multiplayer: no",
        "联机不兼容", "不支持联机", "仅单人",
    ]
    _MP_FRAMEWORK_HINTS = [
        "together in spire", "multiplayer mod", "multiplayer framework",
        "sts2-multiplayer", "multiplayer limit break",
    ]

    def _mp_overrides_path(self) -> Path:
        return self.manager._profile_dir / "mp_overrides.json"

    def _load_mp_overrides(self) -> dict[str, str]:
        """加载用户手动覆盖的联机兼容状态：{mod_id: status}。"""
        p = self._mp_overrides_path()
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_mp_overrides(self, overrides: dict[str, str]) -> None:
        p = self._mp_overrides_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_mp_compat_override(self, mod_id: str, status: str) -> dict:
        """用户手动覆盖某 mod 的联机状态。status ∈ {full, partial, incompatible, mp-required, unknown, auto}
        传 'auto' 清除覆盖（恢复自动检测）。"""
        overrides = self._load_mp_overrides()
        mid = str(mod_id).lower()
        if status == "auto":
            overrides.pop(mid, None)
        elif status in ("full", "partial", "incompatible", "mp-required", "unknown"):
            overrides[mid] = status
        else:
            return {"ok": False, "error": f"unknown status: {status}"}
        self._save_mp_overrides(overrides)
        return {"ok": True, "mod_id": mod_id, "status": status}

    def check_mp_compatibility(self) -> dict:
        """扫描所有已装 mod 的联机兼容性（纯本地，零 HTTP）。

        返回 {results: {mod_id: {status, source, reason}}, stats: {...}}
        status: full | partial | incompatible | mp-required | unknown
        source: auto | user-override
        """
        from .steam_webapi import _get_cache_dir as _api_cache_dir

        overrides = self._load_mp_overrides()
        api_cache = _api_cache_dir()

        def _scan_text(text: str) -> Optional[str]:
            t = (text or "").lower()
            # 先查 framework（mod 本身是联机框架）
            for kw in self._MP_FRAMEWORK_HINTS:
                if kw in t:
                    return "mp-required"
            # 明确不兼容
            for kw in self._MP_DESC_INCOMPATIBLE:
                if kw in t:
                    return "incompatible"
            # 完全支持
            for kw in self._MP_DESC_FULL:
                if kw in t:
                    return "full"
            # 部分支持
            for kw in self._MP_DESC_PARTIAL:
                if kw in t:
                    return "partial"
            return None

        def _scan_tags(tags: list[str]) -> Optional[str]:
            tag_set = set(tags or [])
            if not tag_set:
                return None
            # 不兼容标签优先
            if tag_set & {"SingleplayerOnly", "singleplayer_only", "Single Player Only"}:
                return "incompatible"
            if tag_set & self._MP_TAGS_FULL:
                return "full"
            return None

        results: dict[str, dict] = {}

        for info in self.manager.all_mods.values():
            mid = info.mod_id.lower()
            display = info.display_name or info.mod_id

            # 1) 用户覆盖优先
            if mid in overrides:
                status = overrides[mid]
                results[mid] = {
                    "mod_id": mid,
                    "name": display,
                    "status": status,
                    "source": "user-override",
                    "reason": f"用户手动标记为 {status}",
                    "workshop_id": str(info.workshop_id) if info.workshop_id else None,
                }
                continue

            # 2) 自动检测：先查标签，再查描述
            status = None
            reason_parts: list[str] = []

            # 从 SteamWebAPI 缓存取 tags + description
            tags: list[str] = []
            description = ""
            if info.source == "workshop" and info.workshop_id and api_cache:
                f = api_cache / f"{info.workshop_id}.json"
                if f.is_file():
                    try:
                        raw = json.loads(f.read_text(encoding="utf-8"))
                        tags = raw.get("tags") or []
                        description = raw.get("description") or ""
                    except Exception:
                        pass

            # 从 workshop 预览缓存补 description（HTML 抓取的更全，按当前语言优先）
            if not description and info.source == "workshop" and info.workshop_id:
                from .workshop import load_cached_any

                cached = load_cached_any(str(info.workshop_id), self.manager.get_language())
                if cached:
                    description = cached.get("description") or ""
                    if not tags:
                        tags = cached.get("tags") or []

            # 标签扫描
            tag_result = _scan_tags(tags)
            if tag_result:
                status = tag_result
                reason_parts.append(f"标签命中: {tags}")

            # 描述扫描
            desc_text = f"{display} {info.mod_id} {description}"
            desc_result = _scan_text(desc_text)
            if desc_result:
                if status is None or desc_result == "mp-required":
                    status = desc_result
                reason_parts.append(f"描述命中关键词")

            if status is None:
                status = "unknown"
                reason_parts.append("无联机相关关键词")

            results[mid] = {
                "mod_id": mid,
                "name": display,
                "status": status,
                "source": "auto",
                "reason": "; ".join(reason_parts),
                "workshop_id": str(info.workshop_id) if info.workshop_id else None,
                "has_tags": bool(tags),
                "has_desc": bool(description),
            }

        # 统计
        stats = {"total": len(results)}
        for r in results.values():
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        stats["user_overrides"] = sum(1 for r in results.values() if r["source"] == "user-override")

        _log.info("[api] check_mp_compatibility: %d mods, %s", len(results), stats)
        return {"results": results, "stats": stats}

    def get_state(self) -> dict:
        game = self.manager.game
        # P0: 读取当前 Steam ID（settings.save 所在目录名，如 "76561198308863395"）
        steam_id = None
        try:
            save = self.manager._native_save_path
            if save and save.is_file():
                steam_id = save.parent.name
        except Exception:
            pass
        # 游戏版本信息（从 GameInstall 填充）
        game_info = {
            "root": str(game.game_root) if game else None,
            "exe": str(game.exe_path) if game else None,
            "version": game.version if game else "",
            "version_detected": game.version_detected if game else False,
            "beta_key": game.beta_key if game else "",
            "beta_label": game.beta_label if game else "未知分支",
            "workshop_dir": str(game.workshop_dir) if game and game.workshop_dir else None,
            "mods_dir": str(game.mods_dir) if game else None,
            "disabled_dirs": [str(d) for d in (game.disabled_dirs or [])] if game else [],
        }
        # 启动兜底：main.py 的开机扫描直接调 manager.refresh()，不经过 Api.refresh()。
        # 这里在拿到 state 时确保后台标题补全线程已启动——幂等，已在跑会立即返回。
        titles_pending = self._titles_pending_count()
        if titles_pending:
            self._start_title_fill()
        return {
            "game_root": str(game.game_root) if game else None,
            "game_info": game_info,
            "enabled": [self._mod_dict(i) for i in self.manager.ordered_enabled()],
            "disabled": [self._mod_dict(i) for i in self.manager.ordered_disabled()],
            "packs": self.manager.list_packs(),
            "backups": self._backup_list(),
            "backup_stats": self._backup_stats(),
            "errors": self.manager.errors,
            "subscribed_known": bool(getattr(self.manager, "subscribed_known", True)),
            "mod_count_breakdown": self._build_mod_count_breakdown(),
            "steam_id": steam_id,
            # 还有多少工坊 mod 缺本地化标题（前端据此轮询，补全后自动刷新列表名）
            "titles_pending": titles_pending,
        }

    def get_workshop_titles_pending(self) -> dict:
        """工坊标题补全进度：{pending: n, running: bool}。前端轮询用（只读，无网络请求）。

        pending 归零且 running=False 表示本次补全已经结束，前端可以重载一次 state 刷新列表名。
        """
        with self._title_fill_lock:
            running = bool(self._title_fill_thread and self._title_fill_thread.is_alive())
        return {"ok": True, "pending": self._titles_pending_count(), "running": running}

    def _build_mod_count_breakdown(self) -> dict:
        """构建 mod 数量分解说明（供前端在列表标题下方显示"扫描 N 个，其中 M 个因为 ... 没显示"）。

        典型场景：
          workshop 目录总数 = 196（含 3 个未订阅残留目录 → 被 skip）
          settings.save mod_list = 200（含 4 个占位符）
          最终展示 = 199（合并重复源后 unique canonical）

        返回:
          {
            "final_total": 199,           # 前端展示的总数（enabled + disabled）
            "final_enabled": 18,
            "final_disabled": 181,
            "workshop_dirs_total": 196,    # workshop 目录实际数（含未订阅残留）
            "unsubscribed_skipped": 3,     # 跳过的未订阅残留目录
            "native_list_total": 200,      # settings.save mod_list 条目数（含占位符）
            "native_placeholders": 0,      # settings.save 里的占位符条目
            "merged_duplicate_sources": 6,  # 多个物理副本合并成 1 个 canonical 的数量
            "extra_mods_dirs": 0,           # 额外扫描的本地 mods 目录里的 mod 数
          }
        """
        mgr = self.manager
        game = getattr(mgr, "_game", None)
        final_total = len(getattr(mgr, "_mods", {}))
        _state = getattr(mgr, "_state", None)
        final_enabled = len(getattr(_state, "enabled_order", [])) if _state else 0
        final_disabled = len(getattr(_state, "disabled_order", [])) if _state else 0

        # workshop 目录总数（实际文件系统）
        workshop_dirs_total = 0
        if game and game.workshop_dir and Path(game.workshop_dir).is_dir():
            try:
                workshop_dirs_total = sum(
                    1 for d in Path(game.workshop_dir).iterdir() if d.is_dir()
                )
            except Exception:
                workshop_dirs_total = 0

        # 跳过的未订阅残留目录
        unsub_skipped = len(getattr(mgr, "_subscribed_mod_ids", set()))
        try:
            # 从 scanner 的跳过日志反推：workshop 目录数 - 扫描进 _mods 的 workshop mod 数
            scanned_ws = sum(
                1 for info in getattr(mgr, "_mods", {}).values()
                if info.source in ("workshop", "disabled")
            )
            if workshop_dirs_total > scanned_ws:
                unsub_skipped = max(unsub_skipped, workshop_dirs_total - scanned_ws)
        except Exception:
            pass

        # settings.save mod_list 条目数
        native_total = 0
        native_placeholders = 0
        native_data = getattr(mgr, "_native_full_data", None)
        if native_data and isinstance(native_data, dict):
            ml = native_data.get("mod_settings", {}).get("mod_list", [])
            if isinstance(ml, list):
                native_total = len(ml)
                try:
                    placeholders_added = sum(1 for s in (
                        (getattr(_state, "enabled_order", []) if _state else []) +
                        (getattr(_state, "disabled_order", []) if _state else [])
                    ) if s.startswith("__stub_") or s.endswith(".placeholder"))
                    native_placeholders = placeholders_added
                except Exception:
                    native_placeholders = 0

        # 合并重复源：多个物理副本 → 1 个 canonical
        merged = 0
        versions = getattr(mgr, "_mod_versions", {})
        for mid, vlist in versions.items():
            ws_count = sum(1 for v in vlist if v.workshop_id)
            local_count = sum(1 for v in vlist if not v.workshop_id and v.source == "local")
            disabled_count = sum(1 for v in vlist if not v.workshop_id and v.source == "disabled")
            # 如果有 ≥2 个不同 source → 被合并
            if (ws_count > 0 and local_count > 0) or (ws_count > 0 and disabled_count > 0) \
                    or (local_count > 0 and disabled_count > 0) \
                    or ws_count >= 2:
                merged += 1

        # 额外 mods 目录
        extra_count = sum(
            1 for info in getattr(mgr, "_mods", {}).values()
            if info.source not in ("workshop", "disabled", "local", "builtin")
        )

        return {
            "final_total": final_total,
            "final_enabled": final_enabled,
            "final_disabled": final_disabled,
            "workshop_dirs_total": workshop_dirs_total,
            "unsubscribed_skipped": unsub_skipped,
            "native_list_total": native_total,
            "native_placeholders": native_placeholders,
            "merged_duplicate_sources": merged,
            "extra_mods_dirs": extra_count,
        }

    def get_workshop_bg_progress(self) -> dict:
        """后台两阶段补全进度（供前端预览面板下方进度条用）。返回：
        {
          phase1: {done, total, running, ok},  # 快速预览图（Web API 批量 + 下载）
          phase2: {done, total, running, ok},  # 慢速 HTML 补中文标题
          running: bool,                        # 任一阶段在跑
        }

        running 只看 _bg_progress 的 running 标志（后端精确控制），
        不看 daemon 线程是否活着——线程可能刚启动还没设 running=False。
        """
        from .workshop import get_bg_progress
        p = get_bg_progress()
        running = p.get("phase1_running") or p.get("phase2_running")
        return {
            "ok": True,
            "phase1": {
                "done": p.get("phase1_done", 0),
                "total": p.get("phase1_total", 0),
                "running": p.get("phase1_running", False),
                "ok": p.get("phase1_ok", False),
            },
            "phase2": {
                "done": p.get("phase2_done", 0),
                "total": p.get("phase2_total", 0),
                "running": p.get("phase2_running", False),
                "ok": p.get("phase2_ok", False),
            },
            "running": running,
        }

    def get_html_tasks(self) -> dict:
        """统一 HTML 队列的任务状态（供前端轻量轮询）。

        返回 {items: {wid: {status, reason, seq, updated_at}}, pending: [wid], seq, circuit_open}
        status: queued / running / done / failed
        """
        from .workshop import get_html_tasks
        return get_html_tasks()

    def request_workshop_translation(self, mod_id: str) -> dict:
        """用户手动触发：绕过中文占比门控，抓一次社区页中文描述。

        只对当前选中 mod 生效，不自动重试；完成后前端轮询 get_html_tasks 刷新预览。
        """
        from .workshop import ensure_html

        info = self.manager.active_version(str(mod_id))
        if info is None or info.source != "workshop" or not info.workshop_id:
            return {"ok": False, "error": "not_workshop"}
        lang = self.manager.get_language()
        res = ensure_html(str(info.workshop_id), lang, reason="manual", force=True)
        status = res.get("status")
        return {"ok": status in ("queued", "running", "cached"), **res}

    def set_enabled(
        self, mod_id: str, enable: bool,
        source: Optional[str] = None, version_key: Optional[str] = None,
    ) -> dict:
        """启用/停用 mod。

        2026-10 改为 version_key 精确版本（推荐）：
        version_key 是每个物理副本唯一的标识（= install_dir 路径），
        因为 norm_mod_source("local") 和 norm_mod_source("disabled") 都映射到 "mods_directory"，
        source 参数无法区分同一个 mod_id 下 Mods/ vs .mods_disabled/ 的两份本地副本。

        前端传 flat 副本的 m.__version_key 即可。向后兼容保留 source 参数。

        2026-10-13 改为手动保存：set_enabled 只改内存+物理层，不自动写 settings.save。
        前端需调用 save_all() 一次性提交所有变更。
        """
        _log.info("[api] set_enabled %s -> %s (source=%s, version_key=%s)", mod_id, enable, source, version_key)

        def _do():
            ok = self.manager.set_enabled(
                str(mod_id), bool(enable),
                source=str(source) if source else None,
                version_key=str(version_key) if version_key else None,
                auto_save=False,
            )
            if not ok:
                _log.warning("[api] set_enabled %s → %s 后端拒绝（可能被 pinned 或找不到），前端将收到未变更状态",
                             mod_id, enable)
            self.manager.recheck_dependencies()
            # 启用入口：把该工坊 mod 的 HTML 补全（依赖 + 中文描述）放进统一队列，
            # 不阻塞当前返回；失败只记 debug，不影响启停结果
            if ok and enable:
                try:
                    from .workshop import ensure_html
                    if version_key:
                        info = self.manager._find_by_key(str(version_key))
                    else:
                        info = self.manager._find(
                            str(mod_id), source=str(source) if source else None
                        )
                    if info is not None and info.workshop_id:
                        ensure_html(str(info.workshop_id), reason="enable")
                except Exception as e:
                    _log.debug("[api] set_enabled 入队 HTML 任务失败: %s", e)
            state = self.get_state()
            state["toggle_ok"] = ok
            return state

        # set_enabled 改同步：避免 async dispatch race condition
        # 前端 toggle 依赖及时 applyState，async 回调延迟可能导致状态不一致
        return _do()

    def save_all(self) -> dict:
        """手动保存：把当前内存的启停/排序状态写回 settings.save + BMM settings。
        前端 toggle/drag 后需要调这个才持久化。
        """
        def _do():
            saved_native = False
            saved_bmm = False
            errors = []
            if self.manager.uses_native_mod_list:
                try:
                    self.manager._do_save_native_mod_list()
                    saved_native = True
                except Exception as e:
                    errors.append(f"settings.save: {e}")
                    _log.error("[save_all] settings.save 写回失败: %s", e)
            if self.manager.uses_better_mod_menu:
                try:
                    self.manager._do_save_bmm_settings()
                    saved_bmm = True
                except Exception as e:
                    errors.append(f"BMM: {e}")
                    _log.error("[save_all] BMM settings 写回失败: %s", e)
            # 关键：把当前 enabled_order / disabled_order 同步写进 profile.json！
            # 之前只写 settings.save，profile.json 还存着旧顺序 → 下次 refresh
            # _merge_keep(prev=profile.json 里的) 把 settings.save 的顺序覆盖掉 → 顺序回退
            try:
                self.manager.save_state()
            except Exception as e:
                errors.append(f"profile.json: {e}")
                _log.error("[save_all] profile.json 写回失败: %s", e)
            return {
                "ok": not errors,
                "saved_native": saved_native,
                "saved_bmm": saved_bmm,
                "errors": errors,
            }
        # 手动保存 = 用户主动点击按钮，必须同步执行完写盘才返回
        # 不走 _dispatch_async 线程池（之前异步丢线程池会导致：
        #   CLI 测试里立刻读 settings.save → 线程还没写完 → 旧值
        #   GUI 里用户保存后立刻关应用 → 线程可能还没写完 → 丢数据）
        return _do()

    def _insert_newly_enabled(self, mod_id: str) -> None:
        """新启用 mod 的插入式排序：插到所有依赖它的子 mod 前面。

        逻辑：
          1. 找当前 enabled_order 里所有依赖 mod_id 的子 mod
          2. 如果有子 mod，插到位置最小的子 mod 前面
          3. 如果没有子 mod，插到末尾
          4. 如果新启用的 mod 依赖其他 mod（那些 mod 已启用），确保它在所有依赖之后
        """
        info = self.manager._find(mod_id)
        if info is None:
            return
        new_id_lower = mod_id.lower()
        enabled = self.manager._state.enabled_order

        if new_id_lower not in [m.lower() for m in enabled]:
            return  # set_enabled 可能没成功（比如被 Pinned 保护），不强行插入

        # 先从 enabled 里移除（如果 set_enabled 没自动处理顺序，这里兜底）
        enabled_list = list(enabled)
        if new_id_lower in [m.lower() for m in enabled_list]:
            enabled_list = [m for m in enabled_list if m.lower() != new_id_lower]

        # 找依赖此 mod 的子 mod 在 enabled_list 里的位置
        min_child_idx = None
        for i, mid in enumerate(enabled_list):
            child_info = self.manager._mods.get(mid.lower())
            if child_info is None:
                continue
            for dep_raw in (child_info.manifest.dependencies or []):
                dep = self.manager._dep_id(dep_raw).lower()
                if dep == new_id_lower:
                    min_child_idx = i if (min_child_idx is None or i < min_child_idx) else min_child_idx
                    break

        # 找此 mod 依赖的前置 mod 在 enabled_list 里的位置 → 插在最后一个前置之后
        max_parent_idx = -1
        for dep_raw in (info.manifest.dependencies or []):
            dep = self.manager._dep_id(dep_raw).lower()
            for i, mid in enumerate(enabled_list):
                if mid.lower() == dep:
                    max_parent_idx = i if i > max_parent_idx else max_parent_idx
                    break

        # 决定最终插入位置
        if min_child_idx is not None and max_parent_idx >= 0:
            insert_at = max(min_child_idx, max_parent_idx + 1)
        elif min_child_idx is not None:
            insert_at = min_child_idx
        elif max_parent_idx >= 0:
            insert_at = max_parent_idx + 1
        else:
            insert_at = len(enabled_list)

        enabled_list.insert(insert_at, mod_id)
        # 直接写回 state（不调 reorder_enabled，避免触发 settings.save 写回）
        self.manager._state.enabled_order = enabled_list

        # 同步落盘：原生清单（如果 LOM 启用）
        try:
            if self.manager.uses_native_mod_list:
                self.manager.save_native_mod_list()
        except Exception:
            pass
        self.manager.save_state()

    def reorder_enabled(self, new_order: list) -> dict:
        def _do():
            # 手动保存模式：拖拽只改内存 state.enabled_order
            # settings.save 写入等 save_all 统一做
            self.manager.reorder_enabled([str(x) for x in new_order], auto_save=False)
            self.manager.recheck_dependencies()
            return self.get_state()
        # 拖拽重排 = 用户即时操作，必须同步返回结果
        return _do()

    def auto_sort(self) -> dict:
        def _do():
            # 直接调游戏原生排序（Kahn + PriorityQueue 按 settings.save 平级）
            self.manager._apply_game_sort_to_enabled()
            self.manager.recheck_dependencies()
            return self.get_state()

        return self._dispatch_async(_do, "auto_sort")

    def _topo_sort(self, ids: list, deps_map: dict) -> list:
        import heapq

        n = len(ids)
        id_to_index = {mid.lower(): i for i, mid in enumerate(ids)}
        edges: dict[int, list[int]] = {}
        indegree = [0] * n
        for i, mid in enumerate(ids):
            for dep in deps_map.get(mid.lower(), []):
                dep_index = id_to_index.get(dep.lower())
                if dep_index is not None and dep_index != i:
                    edges.setdefault(dep_index, []).append(i)
                    indegree[i] += 1
        heap = [i for i in range(n) if indegree[i] == 0]
        heapq.heapify(heap)
        result: list = []
        while heap:
            u = heapq.heappop(heap)
            result.append(ids[u])
            for v in edges.get(u, []):
                indegree[v] -= 1
                if indegree[v] == 0:
                    heapq.heappush(heap, v)
        return ids if len(result) != n else result

    # select_version / available_versions 公开 API 已于 2026-10 删除（活动/非活动副本选择逻辑废弃）
    # 多副本改由 source_states 多 source 独立启停；_all_versions_dict 仍保留给 get_state 展示

    def get_workshop_details(self, mod_id: str) -> dict:
        """返回创意工坊 mod 的详情（主图/轮播图/标签/奖励/简介等）。
        无论启用/停用/本地，只要有 workshop_id 就查 Steam。"""
        from .workshop import fetch_workshop_preview as _fetch

        # 2026-11-04: 从 _mod_versions 找有 workshop_id 的副本（多副本场景下 active_version 可能返回 local 副本）
        mid_lower = str(mod_id).lower()
        info = None
        for v in self.manager._mod_versions.get(mid_lower, []):
            if v.workshop_id:
                info = v
                break
        if info is None:
            return {}
        result = _fetch(info.workshop_id, self.manager.get_language())
        if result is None:
            return {"workshop_id": info.workshop_id, "error": self._ui("fetch_failed")}
        # 合并工坊标签 + mod 本地文件自带的标签（去重，本地标签优先靠前）
        local_tags = list(info.manifest.tags or [])
        merged: list[str] = []
        for t in local_tags + list(result.get("tags") or []):
            if t and t not in merged:
                merged.append(t)
        result["tags"] = merged
        return result

    def is_workshop_cached(self, mod_id: str) -> bool:
        """该工坊 mod 的预览是否已有缓存（前端据此决定是否显示加载进度条）。"""
        from .workshop import is_workshop_cached as _check

        info = self.manager.active_version(str(mod_id))
        if info is None or info.source != "workshop" or not info.workshop_id:
            return True  # 非工坊 mod 视为「已缓存」，不显示进度条
        return _check(info.workshop_id, self.manager.get_language())

    def clear_workshop_cache_force(self) -> dict:
        """全量清除所有 Steam 工坊预览缓存（meta.json + preview.jpg 等），不触发重新下载。

        工坊详情面板的「清除缓存」按钮调用（前端 clearWsCache）；设置页的「清理缓存」
        按钮走 clear_workshop_cache（智能清理：仅删孤儿 + 为有更新的 mod 增量重下）。
        """
        from .workshop import clear_all_cache
        count = clear_all_cache()
        _log.info("[api] clear_workshop_cache_force: 删除 %d 个缓存目录", count)
        return {"ok": True, "cleared": count}

    def refresh_workshop_preview(self, mod_id: str) -> dict:
        """右键菜单：强制删除单个 mod 的两层缓存（steam_webapi 元数据 + workshop 预览），
        然后立刻重抓一次。返回 ok + deleted + title。

        通道说明：社区页面（steamcommunity.com）和 Web API（api.steampowered.com）
        独立节流/风控——社区页面被封时只影响 HTML 步骤，Web API 仍能拿到标题/预览 URL。
        """
        from .steam_webapi import clear_single_cache as _clear_sw
        from .workshop import clear_single_cache as _clear_ws
        from .workshop import fetch_workshop_preview as _fetch
        from .workshop import _clear_api_fail

        _log.info("[api] refresh_workshop_preview 入口: mod_id=%s", mod_id)
        # 2026-11-04: 从 _mod_versions 里找有 workshop_id 的副本（多副本场景下 active_version 可能返回 local 副本）
        mid_lower = str(mod_id).lower()
        info = None
        for v in self.manager._mod_versions.get(mid_lower, []):
            if v.workshop_id:
                info = v
                break
        if info is None:
            _log.debug("[api] refresh_workshop_preview 跳过: mod_id=%s（无 workshop 副本）", mod_id)
            return {"ok": False, "error": "no_workshop_id"}

        wid = str(info.workshop_id)

        # 删缓存前先备份旧标题——防止 HTML 通道被封时，重抓结果用 Web API 英文覆盖掉之前正确的本地化标题
        old_title = None
        try:
            from .workshop import _load_cached
            lang = self.manager.get_language()
            old_cache = _load_cached(wid, lang)
            if old_cache:
                old_title = old_cache.get("title")
        except Exception:
            pass

        sw_del = _clear_sw(wid)
        ws_del = _clear_ws(wid)

        # 清掉永久失败标记 → 允许这次重抓
        _clear_api_fail(wid)

        # 立刻重抓（用当前用户语言）
        lang = self.manager.get_language()
        try:
            preview = _fetch(wid, lang, _fallback_title=old_title)
        except Exception as e:
            _log.warning("[api] refresh_workshop_preview 重抓失败 wid=%s: %s", wid, e)
            preview = None

        title = preview.get("title") if preview else None
        _log.info("[api] refresh_workshop_preview: mod=%s wid=%s sw_del=%s ws_del=%s title=%s",
                  mod_id, wid, sw_del, ws_del, title)
        return {"ok": True, "deleted": {"steam_webapi": sw_del, "workshop": ws_del},
                "title": title, "workshop_id": wid}

    def delete_mod(self, mod_id: str, version_key: Optional[str] = None) -> dict:
        """删除 mod 的某个物理副本（或整个 mod_id）。

        2026-11-04：定论方案。
        - partial = 删之前同 mod_id 的副本数 > 1（**先算后 pop**，定论细节 1）
        - 局部更新 _last_snapshot：pop((mod_id, install_dir), None)
        - 不动 set_enabled 类的启停（定论：启停不动快照）
        - 返回 {ok, partial, state}，前端用 partial 区分"全删/部分变更"
        """
        def _do():
            mod_id_low = str(mod_id).lower()
            vk = str(version_key) if version_key else None
            # 1) 先算 partial：删之前，同 mod_id 在 _last_snapshot 里有几个物理副本
            #    必须在 pop 之前算，否则逻辑反了（定论细节 1）
            remaining_before = [k for k in (self._last_snapshot or {}) if k[0] == mod_id_low]
            partial = len(remaining_before) > 1  # 2+ 副本 → 删一个还剩其他

            ok = self.manager.delete_mod(str(mod_id), version_key=vk)
            if ok:
                # 2) 局部更新 _last_snapshot（只 pop 被删的那个副本，不跑全量 diff）
                if self._last_snapshot is not None and vk:
                    self._last_snapshot.pop((mod_id_low, vk), None)
                self.manager.refresh()  # store 重建，清理 delete_mod 后的残留引用
                _log.info("[api] delete_mod 成功: mod_id=%s, version_key=%s, partial=%s, snapshot 已同步",
                          mod_id_low, vk, partial)
            else:
                _log.warning("[api] delete_mod 后端拒绝: mod_id=%s, version_key=%s", mod_id_low, vk)
            return {"ok": ok, "partial": partial, **self.get_state()}
        return _do()

    def clear_enabled(self) -> dict:
        def _do():
            for info in list(self.manager.ordered_enabled()):
                try:
                    self.manager.set_enabled(info.mod_id, False)
                except Exception as e:
                    _log.warning("[api] clear_enabled 停用 %s 失败: %s", info.mod_id, e)
            return self.get_state()

        return self._dispatch_async(_do, "clear_enabled")

    def save_pack(self) -> dict:
        """用当前启用的 mod + 排序创建一个整合包。"""
        def _do():
            import datetime
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            existing = self.manager.list_packs()
            n = len(existing) + 1
            base = self._ui("default_pack")
            if n == 1:
                name = base
            elif self.manager.get_language() == "en":
                name = f"{base} ({n})"
            else:
                name = f"{base}（{n}）"
            pack = self.manager.save_pack(name, now)
            return {"ok": True, "pack": pack, **self.get_state()}

        return self._dispatch_async(_do, "save_pack")

    def rename_pack(self, pack_id: str, new_name: str) -> dict:
        ok = self.manager.rename_pack(str(pack_id), str(new_name))
        return {"ok": ok, **self.get_state()}

    def delete_pack(self, pack_id: str) -> dict:
        ok = self.manager.delete_pack(str(pack_id))
        return {"ok": ok, **self.get_state()}

    def load_pack(self, pack_id: str) -> dict:
        """加载整合包：恢复其记录的启用 mod 与排序。"""
        def _do():
            pack = self.manager.load_pack(str(pack_id))
            if pack is None:
                return {"ok": False, "error": self._ui("pack_missing"), **self.get_state()}
            return {"ok": True, "pack": pack, **self.get_state()}

        return self._dispatch_async(_do, f"load_pack({pack_id})")

    # ============================================================ mod 备份 --
    def _bkmgr(self) -> BackupManager:
        """获取（或按当前备份目录重建）BackupManager。"""
        root = str(self.manager.backup_root)
        if self._backups is None or self._backups_root != root:
            self._backups = BackupManager(self.manager.backup_root, data_root=self.manager.data_dir)
            self._backups_root = root
        self._backups.keep_versions = int(self.manager.state().keep_versions)
        return self._backups

    def _backup_list(self) -> list[dict]:
        try:
            return self._bkmgr().list()
        except Exception as e:
            _log.warning("[api] 列出备份失败: %s", e)
            return []

    def _backup_stats(self) -> dict:
        try:
            return self._bkmgr().stats()
        except Exception:
            return {"count": 0, "mod_count": 0, "archive_bytes": 0, "original_bytes": 0, "saved_bytes": 0, "root": str(self.manager.backup_root)}

    def list_backups(self) -> dict:
        """返回全部备份记录 + 统计。"""
        return {"ok": True, "backups": self._backup_list(), "stats": self._backup_stats(), "settings": self.manager.get_backup_settings()}

    def backup_mod(self, mod_id: str, kind: str = "manual") -> dict:
        """为单个 mod 创建备份（异步）。"""
        def _do():
            info = self.manager.active_version(str(mod_id)) or self.manager._find(str(mod_id))
            if info is None:
                return {"ok": False, "error": self._ui("mod_missing"), **self.list_backups()}
            try:
                rec = self._bkmgr().create(info, kind=kind)
            except Exception as e:
                return {"ok": False, "error": str(e), **self.list_backups()}
            return {"ok": True, "backup": rec, **self.list_backups()}

        return self._dispatch_async(_do, f"backup_mod({mod_id})")

    def backup_all(self, scope: str = "all") -> dict:
        """备份全部 mod 或仅当前启用的 mod（异步）。scope: all | enabled。"""
        def _do():
            if scope == "enabled":
                infos = self.manager.ordered_enabled()
            else:
                infos = list(self.manager.ordered_enabled()) + list(self.manager.ordered_disabled())
            candidates = [i for i in infos if i.install_dir is not None]
            skipped = len(infos) - len(candidates)
            mgr = self._bkmgr()
            done, failed = 0, 0
            errors: list[str] = []
            with self._backup_progress_lock:
                self._backup_progress.update({
                    "running": True, "total": len(candidates), "processed": 0,
                    "done": 0, "failed": 0, "skipped": skipped,
                })
            try:
                for info in candidates:
                    try:
                        mgr.create(info, kind="manual")
                        done += 1
                    except Exception as e:
                        failed += 1
                        errors.append(f"{info.display_name}: {e}")
                        _log.warning("[api] backup_all 失败 %s: %s", info.mod_id, e)
                    finally:
                        with self._backup_progress_lock:
                            self._backup_progress.update({
                                "processed": done + failed, "done": done, "failed": failed,
                            })
            finally:
                with self._backup_progress_lock:
                    self._backup_progress["running"] = False
            return {
                "ok": True, "done": done, "failed": failed, "skipped": skipped,
                "errors": errors[:10], **self.list_backups(),
            }

        with self._backup_progress_lock:
            self._backup_progress.update({
                "running": False, "total": 0, "processed": 0,
                "done": 0, "failed": 0, "skipped": 0,
            })
        return self._dispatch_async(_do, f"backup_all({scope})")

    def delete_backup(self, backup_id: str) -> dict:
        """删除一份备份（异步）。"""
        def _do():
            ok = self._bkmgr().delete(str(backup_id))
            return {"ok": ok, **self.list_backups()}

        return self._dispatch_async(_do, f"delete_backup({backup_id})")

    def verify_backup(self, backup_id: str) -> dict:
        """校验备份归档完整性（异步）。"""
        def _do():
            return {**self._bkmgr().verify(str(backup_id)), **self.list_backups()}

        return self._dispatch_async(_do, f"verify_backup({backup_id})")

    def restore_backup(self, backup_id: str, target: str = "workshop") -> dict:
        """还原备份（异步）。target: workshop（回工坊目录）/ local（解包到 .mods_disabled/ 停用文件夹）。

        2026-11-04：定论方案。
        - before 必须是拷贝（dict(_last_snapshot)），防止可变对象被后续修改导致 diff 失真
        - 首次启动 _last_snapshot 为空时，先跑一次基线快照作为 before，避免把所有已有 mod 算成新增
        - 还原成功后：store 刷新 → after = _snapshot() → 算 scan_report → 同步 _last_snapshot = after
        - 返回 {restore_result, scan_report, state}，前端直接消费，不调全量 refresh
        """
        def _do():
            rec = self._bkmgr().get(str(backup_id))
            if rec is None:
                return {"ok": False, "error": self._ui("backup_missing"), **self.list_backups()}
            game = self.manager.game
            if str(target) == "local":
                if game is None or game.disabled_dir is None:
                    return {"ok": False, "error": self._ui("folder_missing"), **self.list_backups()}
                dest = game.disabled_dir / str(rec.get("name") or rec.get("mod_id") or "mod")
            else:
                ws_id = rec.get("workshop_id")
                if game is not None and game.workshop_dir is not None and ws_id:
                    dest = game.workshop_dir / str(ws_id)
                elif rec.get("origin_dir"):
                    dest = Path(str(rec["origin_dir"]))
                else:
                    return {"ok": False, "error": self._ui("folder_missing"), **self.list_backups()}
            # 1) before 用拷贝（定论：before 不能是引用，否则 after 赋值时会污染 diff）
            #    首次启动 _last_snapshot 为空 → 先跑一次 store.refresh() 拿基线快照，
            #    避免还原后把所有已有 mod 都算成 "新增"
            if self._last_snapshot is None:
                self.manager.refresh()   # 跑 store 初始化
                before = dict(self._snapshot())
                first_baseline = True
            else:
                before = dict(self._last_snapshot)
                first_baseline = False

            res = self._bkmgr().restore(str(backup_id), Path(dest))
            if not res.get("ok"):
                return {**res, **self.list_backups()}

            # 2) 还原成功 → store 刷新 + 算报告 + 同步快照
            self.manager.refresh()
            after = self._snapshot()
            scan_report = self._scan_report(before, after)
            if first_baseline:
                scan_report["first_baseline"] = True  # 标注：首次基线，未对比历史变化
            self._last_snapshot = after
            _log.info("[api] restore_backup 完成: scan_report=%s", scan_report)
            # 把 get_state() 的字段展开到顶层，前端 applyState(s) 直接读 s.enabled/disabled
            state = self.get_state()
            return {**res, "scan_report": scan_report, **self.list_backups(), **state}

        return self._dispatch_async(_do, f"restore_backup({backup_id},{target})")

    def open_backup_folder(self, backup_id: str = "") -> dict:
        """在资源管理器中打开备份归档所在文件夹（或备份根目录）。"""
        try:
            if backup_id:
                folder = self._bkmgr().folder_of(str(backup_id))
            else:
                folder = self.manager.backup_root
        except Exception:
            folder = self.manager.backup_root
        if folder is None:
            return {"ok": False, "error": self._ui("folder_missing")}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
            return {"ok": True, "path": str(folder)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _auto_backup_updated(self) -> None:
        """已禁用：改为前端弹窗让用户手动选择是否备份更新的 mod。"""
        return
        # refresh 后检测被 Steam 更新过的 mod，后台自动补一份备份（不阻塞 UI）。
        try:
            if False and not self.manager.state().auto_backup:
                return
            import os as _os
            targets = []
            skipped_large = []
            for info in list(self.manager.ordered_enabled()) + list(self.manager.ordered_disabled()):
                if info.install_dir is None:
                    continue
                try:
                    if self._bkmgr().needs_update(info):
                        # 跳过 >100MB 的超大 mod——自动压缩 1GB 级别目录要几十秒，
                        # 会把 _IO_POOL 占死导致进程退出时也卡住（ThreadPoolExecutor 等任务完才退）
                        try:
                            _size = sum(
                                (_os.path.getsize(_os.path.join(dp, f)) for dp, dn, fn in _os.walk(str(info.install_dir)) for f in fn)
                            )
                        except Exception:
                            _size = 0
                        if _size > 100 * 1024 * 1024:
                            skipped_large.append(f"{info.display_name}({_size // 1024 // 1024}MB)")
                        else:
                            targets.append(info)
                except Exception:
                    continue
            if skipped_large:
                _log.info("[api] auto_backup 跳过 %d 个超大 mod(>100MB): %s", len(skipped_large), skipped_large)
            if not targets:
                return
            _log.info("[api] 检测到 %d 个 mod 已更新，自动备份", len(targets))

            def _run():
                mgr = self._bkmgr()
                names = []
                for info in targets:
                    try:
                        mgr.create(info, kind="auto")
                        names.append(info.display_name)
                    except Exception as e:
                        _log.warning("[api] 自动备份失败 %s: %s", info.mod_id, e)
                if names:
                    try:
                        js = "window.__onAutoBackup && window.__onAutoBackup(%s);" % json.dumps(
                            json.dumps(names, ensure_ascii=False)
                        )
                        with _WINDOW_LOCK:
                            for w in webview.windows:
                                try:
                                    w.evaluate_js(js)
                                except Exception:
                                    pass
                    except Exception:
                        pass

            _IO_POOL.submit(_run)
        except Exception as e:
            _log.debug("[api] 自动备份检查失败: %s", e)

    def open_folder(self, mod_id: str, install_path: str = None) -> dict:

        if install_path:
            # 直接用前端传来的具体副本路径
            target = Path(install_path)
        else:
            info = self.manager.active_version(str(mod_id))
            target = info.install_dir if info else None
        if target is None or not Path(str(target)).exists():
            return {"ok": False, "error": self._ui("folder_missing")}
        try:
            os.startfile(str(target))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_workshop(self, mod_id: str, mode: str = "browser") -> dict:
        """打开创意工坊详情页。mode: "browser" 浏览器 / "steam" Steam 客户端。"""
        import webbrowser

        info = self.manager.active_version(str(mod_id))
        if info is None or not info.workshop_id:
            return {"ok": False, "error": self._ui("not_workshop")}
        url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={info.workshop_id}"
        try:
            if mode == "steam":
                # steam://openurl/ 让 Steam 客户端打开，而非系统浏览器
                os.startfile(f"steam://openurl/{url}")
            else:
                webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def launch_game(self, force: bool = False) -> dict:

        def _run() -> None:
            try:
                ok, reason = self.manager.launch_and_wait(
                    timeout_seconds=None,
                    should_stop=lambda: False,
                    on_tick=lambda a, b: None,
                    force=bool(force),
                )
                _log.info("[api] launch result: ok=%s reason=%s", ok, reason)
            except Exception as e:
                _log.exception("[api] launch_game 异常")

        threading.Thread(target=_run, daemon=True).start()
        return {"launching": True}

    def is_running(self) -> bool:
        game = self.manager.game
        if game is None:
            return False
        try:
            return bool(self.manager.is_process_running(expected_game_root=game.game_root))
        except Exception:
            return False

    def get_settings(self) -> dict:
        state = self.manager.state()
        game = self.manager.game
        return {
            "manual_game_root": state.manual_game_root,
            "manual_mods_dirs": state.manual_mods_dirs,
            "manual_workshop_dir": state.manual_workshop_dir,
            "manual_mods_dir": state.manual_mods_dir,
            "manual_save_dir": state.manual_save_dir,
            "detected_game": str(game.game_root) if game else None,
            "detected_workshop": str(game.workshop_dir) if game and game.workshop_dir else None,
            "detected_mods": str(game.mods_dir) if game else None,
            "language": self.manager.get_language(),
            "workshop_html_fill": self.manager.get_workshop_html_fill(),
            "backup": self.manager.get_backup_settings(),
            "tag_i18n": self.manager.tag_i18n(),
            "tag_i18n_en2zh": self.manager.tag_i18n_en2zh(),
        }

    def set_backup_settings(
        self,
        auto_backup: Optional[bool] = None,
        keep_versions: Optional[int] = None,
        backup_dir: Optional[str] = None,
    ) -> dict:
        """更新备份设置（异步落盘）。backup_dir 传空字符串表示恢复默认目录。"""
        def _do():
            self.manager.set_backup_settings(
                auto_backup=auto_backup,
                keep_versions=keep_versions,
                backup_dir=backup_dir,
            )
            return {"ok": True, **self.list_backups()}

        return self._dispatch_async(_do, "set_backup_settings")

    def set_settings(
        self,
        manual_game_root: Optional[str] = None,
        manual_mods_dirs: Optional[str] = None,
        manual_workshop_dir: Optional[str] = None,
        manual_mods_dir: Optional[str] = None,
        manual_save_dir: Optional[str] = None,
        language: Optional[str] = None,
        workshop_html_fill: Optional[bool] = None,
    ) -> dict:
        def _do():
            # 先记录旧值，判断路径有没有变
            s = self.manager._state
            old_paths = (
                s.manual_game_root, s.manual_mods_dirs, s.manual_workshop_dir,
                s.manual_mods_dir, s.manual_save_dir,
            )
            new_paths = (
                manual_game_root, manual_mods_dirs, manual_workshop_dir,
                manual_mods_dir, manual_save_dir,
            )
            paths_changed = (str(old_paths) != str(new_paths))

            self.manager.set_manual_paths(
                manual_game_root=manual_game_root,
                manual_mods_dirs=manual_mods_dirs,
                manual_workshop_dir=manual_workshop_dir,
                manual_mods_dir=manual_mods_dir,
                manual_save_dir=manual_save_dir,
            )
            if language:
                self.manager.set_language(str(language))
            if workshop_html_fill is not None:
                self.manager.set_workshop_html_fill(bool(workshop_html_fill))

            # 只有**路径变化**才全量重扫——改语言/只改空路径都是纯内存操作，
            # 扫一遍游戏目录 + 所有 mod + DLL/PCK 完全没必要（RimCrow 也不做）。
            if paths_changed:
                self.manager.refresh(manual_game_root=manual_game_root if manual_game_root else None)
            return self.get_state()

        return self._dispatch_async(_do, "set_settings")

    def pick_folder(self) -> dict:
        """打开系统文件夹选择对话框，返回选中的路径（取消返回空）。"""
        import webview

        try:
            result = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        except Exception as e:
            _log.warning("[api] pick_folder 失败: %s", e)
            return {"path": None}
        return {"path": result[0] if result else None}

    def detect_paths(self) -> dict:
        """自动识别游戏根目录、创意工坊目录、本地 Mods 目录和存档目录。"""
        from .scanner import detect_game_install, locate_settings_save

        game = detect_game_install(None)
        save = locate_settings_save(None)
        return {
            "game_root": str(game.game_root) if game else None,
            "workshop_dir": str(game.workshop_dir) if game and game.workshop_dir else None,
            "mods_dir": str(game.mods_dir) if game else None,
            "save_path": str(save) if save else None,
            "version": game.version if game else "",
            "beta_key": game.beta_key if game else "",
            "beta_label": game.beta_label if game else "未知分支",
        }

    # ------------------------------------------------------------------ 游戏版本 & Steam Beta
    def get_game_version_info(self) -> dict:
        """返回当前游戏版本号和 Steam Beta 分支（强制重新检测一次）。"""
        from .scanner import detect_game_version_from_log, detect_steam_beta_key

        version = ""
        version_detected = False
        beta_key = ""
        beta_label = "未知分支"

        vd = detect_game_version_from_log()
        if vd:
            version = vd["version"]
            version_detected = True

        game_root = self.manager.game.game_root if self.manager.game else None
        bd = detect_steam_beta_key(game_root)
        if bd:
            beta_key = bd["beta_key"]
            beta_label = bd["beta_label"]

        return {
            "version": version,
            "version_detected": version_detected,
            "beta_key": beta_key,
            "beta_label": beta_label,
        }

    def set_app_beta(self, branch: str) -> dict:
        """切换 STS2 的 Steam Beta 分支（修改 appmanifest.acf + 通知 Steam 刷新）。"""
        from .scanner import switch_steam_beta
        _log.info("[set_app_beta] 收到请求: branch=%r", branch)

        result = switch_steam_beta(branch)
        _log.info("[set_app_beta] 返回: ok=%s keys_updated=%s",
                 result.get("ok") if result else None,
                 result.get("beta_keys_updated") if result else None)
        if result is None:
            return {"ok": False, "error": "switch_failed"}
        return result

    def get_app_update_status(self) -> dict:
        """读取 appmanifest 的 StateFlags / BytesDownloaded 等，返回 Steam 下载进度。"""
        from .scanner import get_steam_app_update_status
        status = get_steam_app_update_status()
        if status is None:
            return {"ok": False, "error": "manifest_not_found"}
        return {"ok": True, **status}

    # ------------------------------------------------------------------ 存档管理
    def list_save_slots(self) -> dict:
        """扫描 STS2 所有存档槽位，返回 profiles 列表。"""
        from .scanner import list_game_save_slots
        return list_game_save_slots()

    def backup_save_slot(self, profile_num: int, kind: str) -> dict:
        """备份某个存档槽位到 data/saves_backups/。kind: 'official' | 'modded'。"""
        from .scanner import get_save_slot_dir, _find_running_game_process

        running = _find_running_game_process()
        if running:
            return {"ok": False, "error": "game_running", "message": f"游戏正在运行中（PID {running['pid']}），无法备份存档。"}

        src = get_save_slot_dir(int(profile_num), str(kind))
        if src is None:
            return {"ok": False, "error": "slot_not_found"}

        data_dir = Path(__file__).parent.parent / "data"
        backup_root = data_dir / "saves_backups" / f"profile{profile_num}"
        backup_root.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        zip_path = backup_root / f"{kind}_{ts}.zip"

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_LZMA) as zf:
                for child in src.rglob("*"):
                    if child.is_file():
                        rel = child.relative_to(src.parent)
                        zf.write(child, rel)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {"ok": True, "path": str(zip_path), "size": zip_path.stat().st_size}

    def restore_save_slot(self, profile_num: int, kind: str, backup_path: str) -> dict:
        """从 ZIP 备份还原存档槽位。会先自动把当前存档再备份一份。"""
        from .scanner import get_save_slot_dir, _find_running_game_process

        running = _find_running_game_process()
        if running:
            return {"ok": False, "error": "game_running", "message": f"游戏正在运行中（PID {running['pid']}），无法还原存档。"}

        src = Path(str(backup_path))
        if not src.is_file():
            return {"ok": False, "error": "backup_not_found"}
        if not src.suffix.lower() == ".zip":
            return {"ok": False, "error": "not_zip"}

        dst = get_save_slot_dir(int(profile_num), str(kind))
        if dst is None:
            return {"ok": False, "error": "slot_not_found"}

        bak_dir = dst.parent / f"saves_bak_{time.strftime('%Y%m%d_%H%M%S')}"
        try:
            if dst.is_dir():
                shutil.copytree(dst, bak_dir)
                shutil.rmtree(dst)
            dst.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"prebackup_failed: {e}"}

        try:
            with zipfile.ZipFile(src, "r") as zf:
                zf.extractall(dst.parent)
        except Exception as e:
            try:
                if dst.is_dir():
                    shutil.rmtree(dst)
                shutil.copytree(bak_dir, dst)
            except Exception:
                pass
            return {"ok": False, "error": str(e)}

        return {"ok": True, "path": str(dst), "backup": str(bak_dir)}

    def list_save_backups(self) -> dict:
        """列出所有存档备份。返回 {profiles: [{profile, official_backups, modded_backups}]}。"""
        data_dir = Path(__file__).parent.parent / "data"
        root = data_dir / "saves_backups"
        out: list[dict] = []
        for slot in (1, 2, 3):
            slot_dir = root / f"profile{slot}"
            backups: dict = {"official": [], "modded": []}
            if slot_dir.is_dir():
                for child in sorted(slot_dir.iterdir()):
                    if child.is_file() and child.suffix.lower() == ".zip":
                        st = child.stat()
                        entry = {
                            "name": child.name,
                            "path": str(child),
                            "size": st.st_size,
                            "mtime": int(st.st_mtime),
                        }
                        if child.name.startswith("official_"):
                            backups["official"].append(entry)
                        elif child.name.startswith("modded_"):
                            backups["modded"].append(entry)
            if backups["official"] or backups["modded"]:
                out.append({"profile": slot, **backups})
        return {"ok": True, "profiles": out}

    def close_window(self) -> None:
        """关闭窗口（退出应用）。"""
        import webview

        webview.windows[0].destroy()
