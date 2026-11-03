"""store_mods —— ModManager 类体。从 store.py 拆分（2026-10-14 O1-3 重构）。"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .logger import get_logger
from .scanner import (
    GAME_EXE_NAME,
    BmmPaths,
    GameInstall,
    ModManifest,
    ModInfo,
    ModScanResult,
    bmm_extract_enabled_order_ids,
    bmm_settings_reorder,
    bmm_settings_set_enabled,
    ensure_workshop_stub_disabled,
    ensure_workshop_stub_enabled,
    load_bmm_profiles,
    load_bmm_settings,
    locate_better_mod_menu,
    locate_mod_by_id,
    move_mod_between,
    save_bmm_profiles,
    save_bmm_settings,
    scan_all_mods,
    set_workshop_mod_disabled,
    mod_version_key,
    version_gt,
    norm_mod_source,
    locate_settings_save,
    load_native_mod_list,
    save_native_mod_list,
    nat_mod_list_set_enabled,
    nat_mod_list_reorder,
    nat_mod_list_find_entry,
    nat_extract_enabled_order_ids,
    nat_extract_disabled_order_ids,
    nat_extract_source_states,
)
from .store_core import *  # noqa: F403  re-export 常量 + ManagerState + _serialized

_log = get_logger("store")


class ModManager:
    """封装持久化 + 启用/停用/排序操作。对外的工作单位是 mod_id（不区分大小写）。

    启用/停用/排序 权威源优先级（从高到低，命中即生效）：
      1) STS2 原生清单  settings.save → mod_settings.mod_list
            真·游戏源：只要 settings.save 里有 mod_settings.mod_list，就认为这是 LOM / 游戏
            写入的权威启用清单；管理器直接改这里，游戏下次启动直接读取。
      2) BetterModMenu（显式启用 prefer_better_mod_menu=True 且已安装 BMM 时）
      3) 物理目录驱动（fallback）：Local 搬目录 Mods/ <-> .mods_disabled/；Workshop 种 stub
    """

    def __init__(self, profile_path: Path, prefer_better_mod_menu: Optional[bool] = None):
        raw = Path(profile_path)
        # ====== P1 多 Profile 兼容层 ======
        # 两种传入方式都支持：
        #   1) 具体 .json 文件 → legacy 模式，直接加载
        #   2) 目录（通常是 data/）→ 新 profiles 管理模式
        if raw.is_dir() or raw.suffix.lower() != ".json":
            self.data_dir = raw
            self.profiles_dir = raw / "profiles"
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
            # 迁移：如果只有旧的 manager.state.json，自动复制到 profiles/default.json
            legacy = raw / "manager.state.json"
            default_json = self.profiles_dir / "default.json"
            if legacy.is_file() and not default_json.is_file():
                try:
                    shutil.copy2(str(legacy), str(default_json))
                    _log.info("[P1] 已迁移 legacy manager.state.json → profiles/default.json")
                except Exception as e:
                    _log.warning("[P1] legacy 迁移失败: %s", e)
            # 读取/初始化 .active 文件
            active_file = self.profiles_dir / ".active"
            if active_file.is_file():
                try:
                    active_name = active_file.read_text(encoding="utf-8").strip()
                    if not active_name:
                        active_name = "default"
                except Exception:
                    active_name = "default"
            else:
                active_name = "default"
            # 默认 profile 文件不存在就创建空的
            active_json = self.profiles_dir / f"{active_name}.json"
            if not active_json.is_file():
                active_name = "default"
                active_json = self.profiles_dir / "default.json"
            self._active_file = active_file
            self._active_name = active_name
            self.profile_path = active_json
        else:
            # legacy 直接加载具体 JSON 文件
            self.data_dir = raw.parent
            self.profiles_dir = raw.parent / "profiles"
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
            self._active_file = self.profiles_dir / ".active"
            self._active_name = raw.stem
            self.profile_path = raw
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        _log.debug("ModManager 初始化, profile=%s, data_dir=%s, active=%s, prefer_bmm_arg=%s",
                   self.profile_path, self.data_dir, self._active_name, prefer_better_mod_menu)

        self._game: Optional[GameInstall] = None
        self._mods: dict[str, ModInfo] = {}
        # 每个 mod_id 的全部物理副本（key=lower mod_id），供「选择版本」使用
        self._mod_versions: dict[str, list[ModInfo]] = {}
        # 每个 mod_id 下各 source 的启停状态（2026-10 新增，settings.save 双字段粒度）
        # 格式：{ lower_mod_id: { "steam_workshop": bool, "mods_directory": bool } }
        # 同 mod_id 的工坊副本和本地副本各自独立启停，互不影响
        self._source_states: dict[str, dict[str, bool]] = {}
        # 各 source 副本最近一次 toggle 的时间戳（前端 displayList 停用侧排序用）
        # 格式：{ lower_mod_id: { "steam_workshop": timestamp, "mods_directory": timestamp } }
        self._source_last_toggled: dict[str, dict[str, float]] = {}
        self._state = ManagerState.default()
        self._errors: list[str] = []
        # P0: Steam 工坊在线信息缓存 {workshop_id: SteamModInfo}
        self._steam_info: dict[str, object] = {}
        # 全局依赖/配置检查警告（和 _errors 区分：errors 是硬错误，warnings 是可运行但有风险的情况）
        self._warnings: list[str] = []
        # 单 mod 警告映射 {mod_id.lower(): [警告类型, ...]}
        self._mod_warnings_map: dict[str, list[str]] = {}

        # ---------------------------------------------------------------
        # STS2 原生清单（settings.save → mod_settings.mod_list）—— 最高优先级
        # ---------------------------------------------------------------
        self._native_save_path: Optional[Path] = None
        self._native_full_data: Optional[dict] = None  # 整个 settings.save（含 controller_mapping 等顶层字段）
        # 缓存：refresh 时一次性算出 uses_native_mod_list，避免每次 property 都重新解析
        self._uses_native_mod_list_cache: Optional[bool] = None

        # BetterModMenu 支持（默认完全关闭，只在 prefer_better_mod_menu=True 时启用）
        self._bmm: Optional[BmmPaths] = None
        self._bmm_settings: list[dict] = []
        self._bmm_profiles: dict = {}

        # 构造器显式传 prefer_better_mod_menu 优先级最高；
        # 否则等 load_state() 从 manager.state.json 读上一次的设置
        self._override_prefer_bmm = prefer_better_mod_menu
        self.load_state()
        if prefer_better_mod_menu is not None:
            # 构造器指定 → 覆盖 state
            self._state.prefer_better_mod_menu = bool(prefer_better_mod_menu)

        # 文件写入锁：防止后台 refresh 线程与主线程操作并发写同一个 .tmp 文件
        self._save_lock = threading.Lock()
        # 粗粒度操作锁：串行化 refresh / delete / set_enabled 等改写内存状态的操作
        # （见 _serialized 装饰器），避免多线程派发时并发改 self._mods / self._state
        self._op_lock = threading.RLock()
        # ── O2: 启用/停用侧缓存（避免每次 ordered_enabled() 重建 list + O(N) 遍历） ──
        # 预计算 enabled 侧的 list/set/id-index，所有依赖检查、状态读取直接查缓存；
        # 任何会改 enabled_order/disabled_order/_mods 的操作（refresh/set_enabled/delete/
        # reorder/toggle）必须调用 self._invalidate_mod_caches() 重置这三个字段为 None。
        self._cache_enabled_list: Optional[list[ModInfo]] = None
        self._cache_enabled_ids_set: Optional[set[str]] = None
        self._cache_disabled_list: Optional[list[ModInfo]] = None
        self._cache_disabled_ids_set: Optional[set[str]] = None

        # 工坊订阅登记（appworkshop_*.acf）本次扫描是否成功读取；False 表示可能漏显/误判
        self._subscribed_known: bool = True
        # 上一次扫描识别到的工坊 mod 数（用于 settings.save 写回健康度门禁：骤降即跳过写回）
        self._last_ws_mod_count: int = 0

        # 角色 mod 延迟持久化：mark_character / unmark_character 在 get_skin_state 读路径上
        # 被频繁调用，每次都 save_state 会与 refresh 后台线程并发抢文件锁。
        # 改为设 dirty flag + 500ms 防抖 Timer，多次 mark 只触发一次 save_state。
        self._character_dirty: bool = False
        self._character_flush_timer: Optional[threading.Timer] = None

        # A. dirty gate（2026-10-14）：高频轻量路径（set_mod_tags / set_mod_meta /
        # set_mod_color / set_auto_check_steam / set_backup_settings / set_language /
        # add_manager_tag / set_manager_tags）不再直接调 save_state()，而是设 _state_dirty
        # + 500ms 防抖 Timer，避免文件锁竞争。
        # 核心操作末尾（refresh / set_enabled / delete_mod / reorder_enabled）仍立即
        # save_state()，但 save_state 成功后会顺便清掉 _state_dirty（因为已经把所有变更
        # 都落盘了）。
        self._state_dirty: bool = False
        self._state_flush_timer: Optional[threading.Timer] = None

    @property
    def uses_native_mod_list(self) -> bool:
        """是否使用 STS2 原生清单（settings.save → mod_settings.mod_list）。

        判定标准：settings.save 存在且含合法 mod_settings.mod_list 数组即可。
        设计前提：管理器自己读写 settings.save，STS2 原生读它来决定启用/停用。
        不再需要检查 LoadOrderManager mod 是否物理启用（管理器直接写原生清单即可）。

        交叉验证：_sync_state_orders 会对比原生 is_enabled 与物理 source 字段，
        发现矛盾时打 warning 日志，但仍以 settings.save 为准（管理器自己写的，
        理论上应该一致；物理不一致说明有外部修改）。

        结果在 refresh() 时缓存。
        """
        if self._uses_native_mod_list_cache is not None:
            return self._uses_native_mod_list_cache
        basic_ok = (
            self._native_save_path is not None
            and self._native_full_data is not None
            and isinstance(self._native_full_data.get("mod_settings"), dict)
            and isinstance(self._native_full_data["mod_settings"].get("mod_list"), list)
        )
        self._uses_native_mod_list_cache = bool(basic_ok)
        return bool(basic_ok)

    @property
    def uses_better_mod_menu(self) -> bool:
        """是否真的使用 BetterModMenu（次优先于原生清单）。
        ① 管理器允许启用 BMM（state.prefer_better_mod_menu=True）
        ② 且 STS2 环境确实已安装 BetterModMenu 且能定位到配置（_bmm is not None）
        同时满足才算。默认=False → 优先走原生清单，再 fallback 目录驱动。
        """
        if not self._state.prefer_better_mod_menu:
            return False
        return self._bmm is not None

    # ------------------------------------------------------------------ I/O --

    @property
    def game(self) -> Optional[GameInstall]:
        return self._game

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def subscribed_known(self) -> bool:
        """本次扫描是否成功读取 Steam 订阅登记（appworkshop_*.acf）。

        False 表示 acf 缺失/解析失败，工坊列表只能回退为「遍历目录不过滤」，
        可能包含已取消订阅的孤儿项，前端据此显示提示。
        """
        return self._subscribed_known

    @property
    def all_mods(self) -> dict[str, ModInfo]:
        return dict(self._mods)

    def state(self) -> ManagerState:
        # 返回副本
        return ManagerState(
            enabled_order=list(self._state.enabled_order),
            disabled_order=list(self._state.disabled_order),
            # selected_versions 已于 2026-10 删除（改由 source_states 多 source 独立启停）
            manual_game_root=self._state.manual_game_root,
            manual_mods_dirs=self._state.manual_mods_dirs,
            manual_workshop_dir=self._state.manual_workshop_dir,
            manual_mods_dir=self._state.manual_mods_dir,
            manual_save_dir=self._state.manual_save_dir,
            prefer_better_mod_menu=self._state.prefer_better_mod_menu,
            packs=[dict(p) for p in self._state.packs],
            manager_tags=list(self._state.manager_tags),
            mod_tags={k: list(v) for k, v in self._state.mod_tags.items()},
            card_roles=dict(self._state.card_roles),
            character_mods=list(self._state.character_mods),
            language=self._state.language,
            auto_check_steam=self._state.auto_check_steam,
            groups={k: list(v) for k, v in self._state.groups.items()},
            group_order=list(self._state.group_order),
            mod_meta={k: dict(v) for k, v in self._state.mod_meta.items()},
            mod_colors=dict(self._state.mod_colors),
            pinned={k: dict(v) for k, v in self._state.pinned.items()},
            version=self._state.version,
        )

    def load_state(self) -> None:
        if not self.profile_path.is_file():
            self._state = ManagerState.default()
            # 构造器显式传了就以构造器为准
            if self._override_prefer_bmm is not None:
                self._state.prefer_better_mod_menu = bool(self._override_prefer_bmm)
            return
        try:
            raw = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except Exception as e:
            self._errors.append(f"读取 {self.profile_path} 失败，使用空状态: {e}")
            self._state = ManagerState.default()
            if self._override_prefer_bmm is not None:
                self._state.prefer_better_mod_menu = bool(self._override_prefer_bmm)
            return
        state = ManagerState.default()
        if isinstance(raw, dict):
            state.enabled_order = [str(x) for x in raw.get("enabled_order", [])]
            state.disabled_order = [str(x) for x in raw.get("disabled_order", [])]
            state.manual_game_root = (
                str(raw["manual_game_root"]) if raw.get("manual_game_root") else None
            )
            state.manual_mods_dirs = (
                str(raw["manual_mods_dirs"]) if raw.get("manual_mods_dirs") else None
            )
            state.manual_workshop_dir = (
                str(raw["manual_workshop_dir"]) if raw.get("manual_workshop_dir") else None
            )
            state.manual_mods_dir = (
                str(raw["manual_mods_dir"]) if raw.get("manual_mods_dir") else None
            )
            state.manual_save_dir = (
                str(raw["manual_save_dir"]) if raw.get("manual_save_dir") else None
            )
            # selected_versions 已于 2026-10 删除（活动/非活动副本选择逻辑废弃）
            # 老版本 JSON 里的 selected_versions 字段会被忽略，不会报错
            # 读之前保存的 prefer_better_mod_menu 开关（没有就默认 False = 游戏源逻辑）
            if isinstance(raw.get("prefer_better_mod_menu"), bool):
                state.prefer_better_mod_menu = bool(raw["prefer_better_mod_menu"])
            # 读整合包列表
            raw_packs = raw.get("packs")
            if isinstance(raw_packs, list):
                for p in raw_packs:
                    if isinstance(p, dict) and isinstance(p.get("mods"), list):
                        state.packs.append({
                            "id": str(p.get("id") or ""),
                            "name": str(p.get("name") or ""),
                            "created_at": str(p.get("created_at") or ""),
                            "mods": [str(x) for x in p["mods"]],
                        })
            # 读管理器标签
            raw_tags = raw.get("manager_tags")
            if isinstance(raw_tags, list):
                state.manager_tags = [str(t) for t in raw_tags if str(t).strip()]
            # 迁移：确保官方角色标签始终在池里（老配置没有则补上）
            for t in OFFICIAL_CHARACTER_TAGS:
                if t not in state.manager_tags:
                    state.manager_tags.append(t)
            # 迁移：老配置补上所有内置细分标签
            for t in BUILTIN_TAGS:
                if t not in state.manager_tags:
                    state.manager_tags.append(t)
            raw_mod_tags = raw.get("mod_tags")
            if isinstance(raw_mod_tags, dict):
                for k, v in raw_mod_tags.items():
                    if isinstance(v, list):
                        cleaned = [str(t) for t in v if str(t).strip()]
                        if cleaned:
                            state.mod_tags[str(k)] = cleaned
                        # 空 list → 跳过（老 bug 遗留，不存空 list）
            # 读卡面手动角色归属
            raw_card_roles = raw.get("card_roles")
            if isinstance(raw_card_roles, dict):
                for k, v in raw_card_roles.items():
                    if isinstance(v, str):
                        state.card_roles[str(k)] = v
            # 读持久化的角色 mod 识别结果
            raw_char_mods = raw.get("character_mods")
            if isinstance(raw_char_mods, list):
                state.character_mods = [str(x) for x in raw_char_mods if str(x)]
            # 读界面语言
            raw_lang = raw.get("language")
            if raw_lang in LANGUAGES:
                state.language = str(raw_lang)
            # 读 auto_check_steam（兼容老配置没有这个字段）
            if isinstance(raw.get("auto_check_steam"), bool):
                state.auto_check_steam = bool(raw["auto_check_steam"])
            # ── P0 新增字段读取（全部兼容老配置缺省）──
            # 自定义分组
            raw_groups = raw.get("groups")
            if isinstance(raw_groups, dict):
                for gn, gmods in raw_groups.items():
                    if isinstance(gmods, list):
                        state.groups[str(gn)] = [str(m) for m in gmods]
            raw_go = raw.get("group_order")
            if isinstance(raw_go, list):
                state.group_order = [str(x) for x in raw_go if str(x).strip()]
            # 个人备注/别名
            raw_meta = raw.get("mod_meta")
            if isinstance(raw_meta, dict):
                for k, v in raw_meta.items():
                    if isinstance(v, dict):
                        state.mod_meta[str(k).lower()] = {
                            "alias": str(v.get("alias", "")),
                            "note": str(v.get("note", "")),
                        }
            # mod 颜色
            raw_colors = raw.get("mod_colors")
            if isinstance(raw_colors, dict):
                for k, v in raw_colors.items():
                    if isinstance(v, str) and v.startswith("#") and len(v) in (4, 7):
                        state.mod_colors[str(k).lower()] = v
            # Pinned（P1 预留）
            raw_pinned = raw.get("pinned")
            if isinstance(raw_pinned, dict):
                for k, v in raw_pinned.items():
                    if isinstance(v, dict):
                        state.pinned[str(k).lower()] = {
                            "version": str(v.get("version", "")),
                            "enabled": bool(v.get("enabled", True)),
                        }
            # 最近活动时间（停用列表排序用）
            raw_la = raw.get("last_activity")
            if isinstance(raw_la, dict):
                for k, v in raw_la.items():
                    try:
                        state.last_activity[str(k).lower()] = int(v)
                    except (TypeError, ValueError):
                        pass
            # mod 备份设置（兼容老配置缺省）
            if isinstance(raw.get("auto_backup"), bool):
                state.auto_backup = bool(raw["auto_backup"])
            try:
                if raw.get("keep_versions") is not None:
                    state.keep_versions = max(1, int(raw["keep_versions"]))
            except (TypeError, ValueError):
                pass
            state.backup_dir = (
                str(raw["backup_dir"]) if raw.get("backup_dir") else None
            )
            state.version = int(raw.get("version", 1))
        self._state = state
        # 构造器显式传了优先最高
        if self._override_prefer_bmm is not None:
            self._state.prefer_better_mod_menu = bool(self._override_prefer_bmm)

    def save_state(self) -> None:
        """原子写入 profile JSON，带唯一 tmp 文件名 + Windows 文件锁重试。

        Windows 下 os.replace 可能因文件句柄未完全释放 / 杀毒软件扫描临时文件 /
        前一个 tmp 被并发覆盖而抛出 PermissionError / FileNotFoundError / FileExistsError。
        用唯一 tmp 文件名消除「固定名被并发覆盖」问题，加重试 + 退避兜底 AV 扫描。
        """
        payload = asdict(self._state)
        # 2026-10 新增：把 per-source 启停状态也写进 profile.json
        # settings.save 只按 canonical 粒度写（游戏不认 source），per-source 状态独立存 profile.json
        payload["source_states"] = self._source_states
        payload["source_last_toggled"] = self._source_last_toggled
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        with self._save_lock:
            # .bak 备份（与 save_bmm_settings / save_native_mod_list 同安全级别）
            if self.profile_path.is_file():
                try:
                    bak = self.profile_path.with_suffix(self.profile_path.suffix + ".bak")
                    shutil.copy2(str(self.profile_path), str(bak))
                except OSError:
                    pass

            # 唯一 tmp 文件名：{profile}.{pid}.{timestamp}.tmp —— 即使前一个崩溃残留也不会冲突
            tmp = self.profile_path.with_name(
                f"{self.profile_path.stem}.{os.getpid()}.{int(time.time() * 1_000_000)}.tmp"
            )
            tmp.parent.mkdir(parents=True, exist_ok=True)

            last_err: Optional[Exception] = None
            for attempt in range(3):
                try:
                    # 确保每次重试都写新内容（之前可能被截断）
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    tmp.write_text(content, encoding="utf-8")
                    # os.replace 在 Windows 上等价于 MoveFileEx(MOVEFILE_REPLACE_EXISTING)
                    os.replace(str(tmp), str(self.profile_path))
                    last_err = None
                    break
                except (PermissionError, FileNotFoundError, FileExistsError, OSError) as e:
                    last_err = e
                    backoff = 0.05 * (2 ** attempt)  # 50ms → 100ms → 200ms
                    _log.warning(
                        "save_state 原子写第 %d/3 次失败: %s，等待 %.0fms 重试",
                        attempt + 1, e, backoff * 1000,
                    )
                    try:
                        time.sleep(backoff)
                    except Exception:
                        pass

            # 清理残留 tmp
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

            if last_err is not None:
                _log.error("save_state 原子写最终失败: %s", last_err)
                raise last_err

            # 任何 save_state 成功后都顺便清掉所有 dirty flag，免得 Timer 回调时重复写
            self._character_dirty = False
            self._state_dirty = False

            # P1: 同步 .active 文件（只在 profiles 管理模式下）—— 也用唯一文件名
            if hasattr(self, "_active_file") and hasattr(self, "_active_name"):
                try:
                    tmp_active = self._active_file.with_name(
                        f"{self._active_file.stem}.{os.getpid()}.{int(time.time() * 1_000_000)}.tmp"
                    )
                    tmp_active.parent.mkdir(parents=True, exist_ok=True)
                    tmp_active.write_text(self._active_name, encoding="utf-8")
                    os.replace(str(tmp_active), str(self._active_file))
                except Exception:
                    pass

    # ============================================================ P1 多 Profile --

    @property
    def active_profile_name(self) -> str:
        """当前活跃的 profile 名称。"""
        return getattr(self, "_active_name", "default")

    def list_profiles(self) -> list[dict]:
        """返回所有 profile 列表 [{name, mod_count, enabled_count, updated_at}]。"""
        result: list[dict] = []
        if not hasattr(self, "profiles_dir") or not self.profiles_dir.is_dir():
            return result
        for f in sorted(self.profiles_dir.glob("*.json")):
            name = f.stem
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                enabled = len(raw.get("enabled_order", [])) if isinstance(raw, dict) else 0
                disabled = len(raw.get("disabled_order", [])) if isinstance(raw, dict) else 0
            except Exception:
                enabled = disabled = 0
            try:
                updated = int(f.stat().st_mtime)
            except Exception:
                updated = 0
            result.append({
                "name": name,
                "mod_count": enabled + disabled,
                "enabled_count": enabled,
                "disabled_count": disabled,
                "updated_at": updated,
            })
        return result

    def create_profile(self, name: str, copy_from: Optional[str] = None) -> bool:
        """新建 profile。copy_from 不为 None 则复制该 profile 的内容作为初始值。"""
        name = name.strip()
        if not name or name.lower() == "default" and not copy_from:
            # default 始终存在，不允许创建空的 default
            pass
        target = self.profiles_dir / f"{name}.json"
        if target.is_file():
            return False  # 已存在
        if copy_from:
            src = self.profiles_dir / f"{copy_from}.json"
            if not src.is_file():
                return False
            shutil.copy2(str(src), str(target))
        else:
            # 空 profile：从当前 state 复制一份，清空 enabled/disabled（用户从零开始）
            empty = ManagerState.default()
            target.write_text(
                json.dumps(asdict(empty), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        _log.info("[P1] 新建 profile: %s (copy_from=%s)", name, copy_from)
        return True

    def switch_profile(self, name: str) -> bool:
        """切换到指定 profile：加载它的状态（替换 enabled/disabled 列表）。
        注意：切换后需要重新 refresh() 才能正确对齐磁盘 mod。"""
        name = name.strip()
        if not name:
            return False
        target = self.profiles_dir / f"{name}.json"
        if not target.is_file():
            return False
        # 先保存当前 state（如果当前 profile 还没保存的改动可以保留）
        self.save_state()
        self._active_name = name
        self.profile_path = target
        self.load_state()
        # 清除缓存（refresh 会重建）
        self._game = None
        self._mods = {}
        self._mod_versions = {}
        self._source_states = {}
        self._source_last_toggled = {}
        self._native_save_path = None
        self._native_full_data = None
        self._uses_native_mod_list_cache = None
        _log.info("[P1] 已切换到 profile: %s", name)
        self._invalidate_mod_caches()
        return True

    def delete_profile(self, name: str) -> bool:
        """删除 profile。default 不允许删除。当前活跃的 profile 被删了会自动切回 default。"""
        name = name.strip()
        if not name or name == "default":
            return False
        target = self.profiles_dir / f"{name}.json"
        if not target.is_file():
            return False
        try:
            target.unlink()
        except Exception:
            return False
        _log.info("[P1] 已删除 profile: %s", name)
        if self._active_name == name:
            # 自动切回 default
            self.switch_profile("default")
        return True

    def rename_profile(self, old: str, new: str) -> bool:
        """重命名 profile。"""
        old = old.strip()
        new = new.strip()
        if not old or not new or old == new or old == "default":
            return False
        src = self.profiles_dir / f"{old}.json"
        dst = self.profiles_dir / f"{new}.json"
        if not src.is_file() or dst.is_file():
            return False
        try:
            from .scanner import _safe_move
            _safe_move(src, dst)
        except Exception:
            return False
        _log.info("[P1] profile 重命名: %s → %s", old, new)
        if self._active_name == old:
            self._active_name = new
            self.save_state()
        return True

    # -------------------------------------------------------------- 整合包 --

    def save_pack(self, name: str, created_at: str) -> dict:
        """用当前启用的 mod 顺序创建一个整合包，落盘并返回该整合包 dict。"""
        import uuid

        # 分享识别码：STS2- + 24 位 hex（4 组 × 6 位），总长 32 位，足够长避免冲突
        hexid = uuid.uuid4().hex[:24]
        pack_id = f"STS2-{hexid[:6]}-{hexid[6:12]}-{hexid[12:18]}-{hexid[18:24]}"
        pack = {
            "id": pack_id,
            "name": name,
            "created_at": created_at,
            "mods": list(self._state.enabled_order),
        }
        self._state.packs.append(pack)
        self.save_state()
        return dict(pack)

    def rename_pack(self, pack_id: str, new_name: str) -> bool:
        for p in self._state.packs:
            if p.get("id") == pack_id:
                p["name"] = new_name
                self.save_state()
                return True
        return False

    def delete_pack(self, pack_id: str) -> bool:
        before = len(self._state.packs)
        self._state.packs = [p for p in self._state.packs if p.get("id") != pack_id]
        if len(self._state.packs) != before:
            self.save_state()
            return True
        return False

    def load_pack(self, pack_id: str) -> Optional[dict]:
        """加载整合包：把启用列表恢复为整合包记录的 mod 及顺序，其余 mod 全部停用。

        返回该整合包 dict；找不到返回 None。不存在的 mod（已删除）会被跳过。
        """
        pack = next((p for p in self._state.packs if p.get("id") == pack_id), None)
        if pack is None:
            return None

        wanted = [m for m in pack.get("mods", [])]
        wanted_lower = {m.lower() for m in wanted}

        # 1) 停用当前启用、但不在整合包里的 mod
        for mid in list(self._state.enabled_order):
            if mid.lower() not in wanted_lower:
                self.set_enabled(mid, False)

        # 2) 启用整合包里当前仍在停用侧的 mod（不存在的跳过）
        for mid in wanted:
            if self._find(mid) is None:
                continue
            if not any(m.lower() == mid.lower() for m in self._state.enabled_order):
                self.set_enabled(mid, True)

        # 3) 按整合包保存的顺序重排启用列表
        self.reorder_enabled(wanted)

        _log.info(
            "[load_pack] 已加载整合包 id=%s name=%s，启用 %d 个 mod",
            pack.get("id"), pack.get("name"), len(wanted),
        )
        return dict(pack)

    def list_packs(self) -> list[dict]:
        return [dict(p) for p in self._state.packs]

    # ---------------------------------------------------------- 管理器标签 --

    def list_manager_tags(self) -> list[str]:
        return list(self._state.manager_tags)

    def get_language(self) -> str:
        """返回当前界面语言：zh / en。"""
        return self._state.language if self._state.language in LANGUAGES else "zh"

    def set_language(self, lang: str) -> None:
        """设置界面语言（仅接受 zh / en）。"""
        if lang not in LANGUAGES:
            return
        self._state.language = lang
        self._mark_dirty()


    def tag_i18n(self) -> dict:
        """返回管理器标签中英文对照表（中文 -> 英文）。"""
        return dict(TAG_I18N)

    def tag_i18n_en2zh(self) -> dict:
        """返回工坊 / 本地 manifest 标签对照表（英文小写 -> 中文）。"""
        return dict(TAG_I18N_EN2ZH)

    def add_manager_tag(self, name: str) -> bool:
        """新增一个管理器标签（去重，忽略空串）。返回是否新增。"""
        name = name.strip()
        if not name or name in self._state.manager_tags:
            return False
        self._state.manager_tags.append(name)
        self._mark_dirty()

        return True

    def delete_manager_tag(self, name: str) -> bool:
        """删除一个标签，并同步移除所有 mod 上挂的该标签。"""
        if name not in self._state.manager_tags:
            return False
        self._state.manager_tags = [t for t in self._state.manager_tags if t != name]
        for k in list(self._state.mod_tags.keys()):
            tags = [t for t in self._state.mod_tags[k] if t != name]
            if tags:
                self._state.mod_tags[k] = tags
            else:
                del self._state.mod_tags[k]
        self._mark_dirty()
        return True

    def get_mod_tags(self, mod_id: str) -> list[str]:
        """返回某 mod 的有效标签：手动标签（一级）优先，否则按文件内容自动识别（二级）。"""
        low = mod_id.lower()
        manual = self._state.mod_tags.get(low)
        if manual is not None:
            return list(manual)
        info = self._mods.get(low)
        if info is None:
            return []
        return self._auto_detect_tags(info)

    def get_mod_tags_with_source(self, mod_id: str) -> tuple[list[str], str]:
        """返回 (标签列表, source)。source: "manual" / "workshop" / "local"。"""
        low = mod_id.lower()
        manual = self._state.mod_tags.get(low)
        if manual is not None:
            return list(manual), "manual"
        info = self._mods.get(low)
        if info is None:
            return [], "local"
        tag, reason, hits, source = self._detect_tag_with_reason(info)
        pool = set(self._state.manager_tags)
        tags = [tag] if tag in pool else []
        return tags, source

    def has_manual_tags(self, mod_id: str) -> bool:
        """该 mod 是否被用户手动设置过标签（手动标签优先级最高，分析时跳过）。"""
        return mod_id.lower() in self._state.mod_tags

    def is_known_character(self, mod_id: str) -> bool:
        """该 mod 是否已被持久化识别为角色 mod（避免清除工坊缓存后失效）。"""
        return mod_id.lower() in self._state.character_mods

    def mark_character(self, mod_id: str) -> None:
        """持久化记录该 mod 为角色 mod（防抖延迟保存，避免 get_skin_state 读路径抢锁）。"""
        low = mod_id.lower()
        if low not in self._state.character_mods:
            self._state.character_mods.append(low)
            self._schedule_character_flush()

    def unmark_character(self, mod_id: str) -> None:
        """撤销该 mod 的角色 mod 持久化记录（防抖延迟保存）。"""
        low = mod_id.lower()
        if low in self._state.character_mods:
            self._state.character_mods = [m for m in self._state.character_mods if m != low]
            self._schedule_character_flush()

    def _schedule_character_flush(self) -> None:
        """安排 500ms 后把 character_mods 的变更刷到磁盘。

        多次 mark/unmark 会 cancel 前一个 Timer 再重启（防抖），最终只 save_state 一次。
        如果 500ms 内有其他 save_state 调用（refresh/toggle 等），dirty state 自然被带上。
        """
        self._character_dirty = True
        if self._character_flush_timer is not None:
            try:
                self._character_flush_timer.cancel()
            except Exception:
                pass
        self._character_flush_timer = threading.Timer(0.5, self._flush_character_dirty)
        self._character_flush_timer.daemon = True
        self._character_flush_timer.start()

    def _flush_character_dirty(self) -> None:
        """Timer 回调：如果 dirty 还没被其他 save_state 覆盖，就主动刷一次。"""
        if not self._character_dirty:
            return
        try:
            self.save_state()
        except Exception as e:
            _log.warning("[character-flush] save_state 失败（稍后再试）: %s", e)
            # 失败则再安排一次（避免丢数据）
            self._character_flush_timer = threading.Timer(2.0, self._flush_character_dirty)
            self._character_flush_timer.daemon = True
            self._character_flush_timer.start()
            return
        self._character_dirty = False

    # ---------------------------------------------------- A. dirty gate (通用轻量路径)

    def _mark_dirty(self) -> None:
        """A. dirty gate：高频轻量路径调这个替代 save_state()。

        效果：设 _state_dirty + 500ms 防抖 Timer，多次 _mark_dirty 会 cancel + restart，
        最终只 save_state() 一次。核心操作末尾的 save_state() 成功后会顺便清掉 _state_dirty，
        所以如果 500ms 内有 refresh/toggle 等，Timer 回调进来会直接 return。
        """
        self._state_dirty = True
        if self._state_flush_timer is not None:
            try:
                self._state_flush_timer.cancel()
            except Exception:
                pass
        self._state_flush_timer = threading.Timer(0.5, self._flush_general_dirty)
        self._state_flush_timer.daemon = True
        self._state_flush_timer.start()

    def _flush_general_dirty(self) -> None:
        """Timer 回调：如果 dirty 还没被其他 save_state 覆盖，就主动刷一次。"""
        if not self._state_dirty:
            return
        try:
            self.save_state()
        except Exception as e:
            _log.warning("[state-flush] save_state 失败（稍后再试）: %s", e)
            self._state_flush_timer = threading.Timer(2.0, self._flush_general_dirty)
            self._state_flush_timer.daemon = True
            self._state_flush_timer.start()
            return
        self._state_dirty = False

    def _auto_detect_tags(self, info: ModInfo) -> list[str]:
        """按 manifest 字段 + 名称/描述关键词自动归类，返回已过滤到当前标签池的标签列表。"""
        tag, _reason, _hits, _src = self._detect_tag_with_reason(info)
        pool = set(self._state.manager_tags)
        return [tag] if tag in pool else []

    def get_mod_tags_reason(self, mod_id: str) -> str:
        """返回某 mod 自动标签的判定依据（给用户看的可读说明）。手动标签返回「手动设置」。"""
        low = mod_id.lower()
        if self._state.mod_tags.get(low) is not None:
            return "手动设置"
        info = self._mods.get(low)
        if info is None:
            return ""
        _tag, reason, _hits, _src = self._detect_tag_with_reason(info)
        return reason

    def get_mod_tags_hits(self, mod_id: str) -> list[str]:
        """返回某 mod 自动标签命中的关键词（供前端作为 tag 徽标展示）。手动标签返回空。"""
        low = mod_id.lower()
        if self._state.mod_tags.get(low) is not None:
            return []
        info = self._mods.get(low)
        if info is None:
            return []
        _tag, _reason, hits, _src = self._detect_tag_with_reason(info)
        return hits

    def _match_subtag(self, name: str, desc: str, subtags) -> Optional[str]:
        """在 subtags 里找第一个命中的细分标签。

        两层匹配：
          1) 名字子串命中 → 直接返回（最可靠）
          2) 描述词边界命中 → 只有当 desc 里的关键词是「功能声明」而非「引用」时才生效。

        负向排除（desc 匹配时）：
          - 中文引用排除：关键词前面紧跟"按照/根据/参考/仿照"等 → 是引用，跳过。
          - 英文泛指排除："power" 前面紧跟 his/their/immense/magical 等泛指修饰 →
            是泛指"力量"（如"his immense power"），不是游戏里的 Power（能力卡），跳过。
        """
        # 英文 power 泛指排除词：前面 N 个 token 里出现这些泛指修饰 → 跳过
        _POWER_GENERIC_EN = frozenset({
            "his", "her", "their", "its", "your", "our", "the", "an", "a",
            "immense", "great", "dark", "magical", "magic", "ancient", "former",
            "hidden", "lost", "evil", "mystical", "sacred", "newfound",
        })

        for tag, kws in subtags:
            if any(k in name for k in kws):
                return tag

        # desc 匹配：用负向排除避免引用/泛指误伤
        NEGATIVE_PREFIXES = ("按照", "根据", "参考", "仿照", "照", "沿", "依据")
        for tag, kws in subtags:
            for kw in kws:
                if not _kw_in(desc, kw):
                    continue
                idx = desc.find(kw)
                if idx < 0:
                    continue
                # 中文引用排除
                before_cn = desc[max(0, idx - 3):idx]
                if any(p in before_cn for p in NEGATIVE_PREFIXES):
                    continue  # "按照卡面..." → 跳过
                # 英文 power 泛指排除
                if kw == "power":
                    before_en_text = desc[max(0, idx - 40):idx]
                    before_words = re.findall(r'[a-z]+', before_en_text)
                    if any(w in _POWER_GENERIC_EN for w in before_words[-3:]):
                        continue  # "his immense power" → 泛指跳过
                return tag
        return None

    def _appear_subtag(self, name: str, desc: str) -> str:
        """外观细分：皮肤 / 卡面 / 语音 / 外观。

        只在「名字」里强匹配细分关键词：desc 里 card/skin 太容易误伤
        （动画 mod 的 desc 常写 "cards with animations"，不是卡面 mod）。
        名字里有 "Skin" / "Portrait" / "Card Art" / "Voice" → 才细分。
        """
        return self._match_subtag(name, "", APPEAR_SUBTAGS) or AUTO_TAG_APPEAR

    def _gameplay_subtag(self, info: ModInfo, name: str, desc: str) -> str:
        """玩法细分：新角色 / 新遗物 / ... / 玩法。

        affects_gameplay=true 时按四层信号命中，每层都比上一层弱但覆盖面更广：
          1) manifest name/desc 关键词（最可靠，mod 作者显式声明的）
          2) workshop cached desc（Steam 爬的完整描述，manifest 太简略时兜底）
          3) workshop cached tags（HTML 爬的 Steam 社区 tag，Character 兜底）
          4) DLL has_character 信号（代码里有角色注册逻辑，desc/tags 都没关键词时兜底）

        示例：圣主历险记 manifest desc 里没有"角色"关键词，但 workshop cached desc
        里有"圣主角色与专属起始牌组" → 靠第 2 层信号命中。
        """
        # 1) manifest name/desc（最可靠）
        tag = self._match_subtag(name, desc, GAMEPLAY_SUBTAGS)
        if tag:
            return tag

        # 2) workshop cached desc（Steam 爬的完整描述，manifest desc 太简略时兜底）
        if info.workshop_id:
            from .workshop import load_cached_any
            cached = load_cached_any(info.workshop_id, "")
            if cached:
                wdesc = (cached.get("description") or "").lower()
                if wdesc and wdesc != desc:
                    tag = self._match_subtag(name, wdesc, GAMEPLAY_SUBTAGS)
                    if tag:
                        return tag

        # 3) workshop cached tags（HTML 爬的 Steam 社区 tag，Character 兜底）
        if info.workshop_id:
            from .workshop import get_cached_tags
            if any(t.strip().lower() == "character" or t.strip() == "角色" for t in get_cached_tags(info.workshop_id)):
                return AUTO_TAG_CHARACTER

        # 4) DLL has_character 兜底（PenanceMod 这类 desc/tags 都无角色关键词的角色 mod）
        sig = self._dll_signals(info)
        if sig and sig.get("ok") and sig.get("has_character"):
            return AUTO_TAG_CHARACTER

        return AUTO_TAG_GAMEPLAY

    def _detect_tag_with_reason(self, info: ModInfo) -> tuple[str, str, list[str], str]:
        """返回 (标签, 判定依据说明, 命中的关键词列表, source)。

        source 取值：
          "workshop" — Steam 社区标签映射（权威，最高自动优先级）
          "local"    — 本地分析（关键词 / DLL / PCK，兜底）
        """
        m = info.manifest
        name = (info.display_name or "").lower()
        desc = (m.description or "").lower()
        affects = bool(m.affects_gameplay)

    # ================================================================
        # 第一优先级：工坊 tags（Steam 社区标签，权威来源）
        # 第 2 步：核心 + 独立模块判定 → 替代原来的"priority 排序选最优"
        # ================================================================
        if info.workshop_id:
            mapped_tags = self._collect_workshop_mapped_tags(info.workshop_id)
            if mapped_tags:
                # 检查是否有至少一个"内容类"标签（角色/卡牌/遗物/事件/敌人/地图/能力/药水/玩法）
                # 没有内容类 → 不走核心判定，退回 priority+tier 兜底排序
                _CONTENT_TYPES_SET = {
                    AUTO_TAG_CHARACTER, AUTO_TAG_NEW_CARD, AUTO_TAG_RELIC, AUTO_TAG_EVENT,
                    AUTO_TAG_ENEMY, AUTO_TAG_MAP, AUTO_TAG_POWER, AUTO_TAG_POTION,
                }
                has_content = any(t in _CONTENT_TYPES_SET for t in mapped_tags)

                if has_content:
                    main_tag, sub_tags, scale, content_hits, gameplay_signal = self._classify_core(mapped_tags)
                    # 组装 reason（给用户看的可读判定依据）
                    reason_parts = [f"工坊 tag"]
                    reason_parts.append(f"核心={main_tag}")
                    if sub_tags:
                        reason_parts.append(f"副={','.join(sub_tags)}")
                    reason_parts.append(f"规模={scale}, 内容类型={content_hits}, 独立信号={gameplay_signal}")
                    return main_tag, "; ".join(reason_parts), mapped_tags, "workshop"
                else:
                    # 纯非内容类（工具/依赖/外观/语音/修复/其它）→ priority+tier 兜底
                    wt = self._workshop_tag(info.workshop_id)
                    if wt is not None:
                        reason_parts = [f"工坊 tag（无内容类标签，兜底排序）", wt]
                        return wt, "; ".join(reason_parts), mapped_tags, "workshop"

        # ================================================================
        # 第二优先级：本地分析（manifest + 关键词 + DLL + PCK）
        # ================================================================
        tag: Optional[str] = None
        reason = ""
        hits: list[str] = []

        if not affects:
            # ---- 收集所有命中信号 ----
            fix_hits = [k for k in FIX_KEYWORDS if k in name or _kw_in(desc, k)]
            strong_appear_hits = [k for k in STRONG_APPEAR_KEYWORDS if k in name]
            tool_hits = [k for k in AUTO_TAG_TOOL_KEYWORDS if k in name or _kw_in(desc, k)]
            lib_hits = [k for k in AUTO_TAG_LIB_KEYWORDS if k in name or _kw_in(desc, k)]
            appear_hits = [k for k in AUTO_TAG_APPEAR_KEYWORDS if k in name or _kw_in(desc, k)]

            # 冲突消解
            if strong_appear_hits:
                tag = self._appear_subtag(name, desc)
                reason = f"名字含强外观词 {', '.join(strong_appear_hits)}"
                hits = strong_appear_hits
            elif fix_hits and not appear_hits:
                tag = AUTO_TAG_FIX
                reason = f"命中修复关键词 {', '.join(fix_hits)}"
                hits = fix_hits
            elif fix_hits and appear_hits:
                tag = self._appear_subtag(name, desc)
                reason = f"命中修复+外观，优先外观 ({', '.join(appear_hits)})"
                hits = appear_hits
            elif tool_hits:
                tag = AUTO_TAG_TOOL
                reason = f"命中工具关键词 {', '.join(tool_hits)}"
                hits = tool_hits
            elif lib_hits:
                tag = AUTO_TAG_LIB
                reason = f"命中依赖关键词 {', '.join(lib_hits)}"
                hits = lib_hits
            elif appear_hits:
                tag = self._appear_subtag(name, desc)
                reason = f"命中外观关键词 {', '.join(appear_hits)}"
                hits = appear_hits

        if tag is None:
            if affects:
                tag = self._gameplay_subtag(info, name, desc)
                reason = f"affects_gameplay=true → {tag}"
            else:
                tag = AUTO_TAG_OTHER
                reason = "无关键词命中"

        # DLL 信号：仅对「其它」用 has_character 兜底识别角色
        if tag == AUTO_TAG_OTHER:
            sig = self._dll_signals(info)
            if sig and sig.get("ok") and sig.get("has_character"):
                tag = AUTO_TAG_GAMEPLAY
                reason += " + DLL 检测到角色类型"

        # PCK 信号：纯美术 mod 的 .pck 含卡面/皮肤/立绘资源 → 归「外观」
        if tag == AUTO_TAG_OTHER and info.manifest.has_pck:
            sig = self._pck_signals(info)
            if sig and sig.get("ok"):
                text = sig.get("text", "")
                # 精确匹配：只有明确的卡面/皮肤/立绘资源才算外观
                # 宽泛的 "character" 目录（UI 工具/角色信息读取）不算
                appear_paths = (
                    "cardportrait", "card/portrait", "charselect",
                    "/skin/", "/costume/", "/outfit/",
                    "立绘", "卡面", "皮肤",
                )
                if any(p in text.lower() for p in appear_paths):
                    tag = AUTO_TAG_APPEAR
                    reason += " + PCK 含卡面/皮肤资源"

        return tag, reason, hits, "local"

    def _workshop_raw_tags(self, workshop_id: str) -> list[str]:
        """返回原始 workshop tags（未映射的英文标签列表）。"""
        from .workshop import get_cached_tags
        return list(get_cached_tags(workshop_id))

    def diagnose_all_tags(self) -> list[dict]:
        """返回所有 mod 的标签诊断结果（标签 + 判定依据 + source），供诊断面板展示。"""
        pool = set(self._state.manager_tags)
        result: list[dict] = []
        for mid, info in self._mods.items():
            manual = self._state.mod_tags.get(mid.lower())
            if manual is not None:
                tags = list(manual)
                reason = "手动设置"
                hits: list[str] = []
                tag_source = "manual"
            else:
                tag, reason, hits, tag_source = self._detect_tag_with_reason(info)
                tags = [tag] if tag in pool else []
            result.append({
                "id": info.mod_id,
                "name": info.display_name,
                "tags": tags,
                "hits": hits,
                "reason": reason,
                "tag_source": tag_source,  # "manual" / "workshop" / "local"
                "source": info.source,
                "affects": bool(info.manifest.affects_gameplay),
                "has_dll": bool(info.manifest.has_dll),
                "has_pck": bool(info.manifest.has_pck),
            })
        return result

    # 内置自检样例：mod_id(lower) -> 期望标签。用于回归保护，防止识别规则改动导致这些已知案例回归。
    TAG_SELFTEST_CASES = {
        "baselib": AUTO_TAG_LIB,
        "sts2-ritsulib": AUTO_TAG_LIB,
        "ata_ironclad": AUTO_TAG_CARD_ART,
        "watcherbeautified": AUTO_TAG_APPEAR,
        "chizuruironcladskin": AUTO_TAG_SKIN,
        "tressasilentskin": AUTO_TAG_SKIN,
        "alicedefectvoicebridge": AUTO_TAG_VOICE,
        "penancemod": AUTO_TAG_CHARACTER,
        "sts2_statsthespire_local": AUTO_TAG_OTHER,
    }

    def run_tag_selftest(self) -> list[dict]:
        """对内置样例跑自动标签识别，返回实际 vs 期望（用于发现识别规则回归）。"""
        result: list[dict] = []
        pool = set(self._state.manager_tags)
        for mid, expected in self.TAG_SELFTEST_CASES.items():
            info = self._mods.get(mid)
            if info is None:
                result.append({"id": mid, "name": mid, "expected": expected, "actual": None, "ok": False, "note": "未找到 mod"})
                continue
            tag, _reason, _hits, _src = self._detect_tag_with_reason(info)
            actual = tag if tag in pool else "(无标签)"
            result.append({
                "id": info.mod_id,
                "name": info.display_name,
                "expected": expected,
                "actual": actual,
                "ok": actual == expected,
            })
        return result

    def _workshop_tag(self, workshop_id: str) -> Optional[str]:
        """读缓存的创意工坊标签，映射到管理器自动标签。

        多标签冲突消解：
          1. 过滤语言标签（english/schinese/japanese... — Steam 标注的支持语言，不是分类）
          2. 过滤 IP/主题标签（touhou/alice/blue archive... — 内容主题，不是分类）
          3. 别名归一（character→characters, bosses→新敌人, defectskin→皮肤...）
          4. 通过归一化索引 _NORMALIZED_MAP 查 WORKSHOP_TAG_MAP（两侧一致，防大小写/空格失配）
          5. 按 priority → specificity_tier(HIGH>MED>LOW) 排序选最优
          6. 映射失败返回 None（绝不返回 raw tag）

        注：这是兜底路径。第 2 步落地后，_detect_tag_with_reason 的核心+独立模块判定
        会先跑，只有无明确核心信号时才退回这里。
        """
        from .store_core import (
            _LANG_TAGS, _TAG_IGNORE, _TAG_ALIASES, _NORMALIZED_MAP,
            _WORKSHOP_TAG_PRIORITY, _TAG_SPECIFICITY, _normalize_tag,
            _DEFAULT_SPEC,
        )
        from .workshop import get_cached_tags

        # tier 数值映射（HIGH 最优先，MED 次之，LOW 最后）
        _TIER_RANK = {"HIGH": 0, "MED": 1, "LOW": 2}

        candidates: list[tuple[int, int, str, str]] = []  # (priority, tier_rank, mapped_tag, raw_tag)
        for raw in get_cached_tags(workshop_id):
            low = raw.strip().lower()
            # 过滤语言标签
            if low in _LANG_TAGS:
                continue
            # 过滤 IP/主题/政策标签
            if low in _TAG_IGNORE:
                continue
            # 别名：raw → 标准 key → 查映射
            alias = _TAG_ALIASES.get(low, low)
            # 归一化索引查（两侧一致）
            mapped = _NORMALIZED_MAP.get(_normalize_tag(alias))
            if mapped is None:
                continue
            prio = _WORKSHOP_TAG_PRIORITY.get(mapped, 99)
            tier = _TAG_SPECIFICITY.get(mapped, _DEFAULT_SPEC)
            tier_rank = _TIER_RANK.get(tier, 2)  # 未知 tier 默认 LOW
            candidates.append((prio, tier_rank, mapped, raw))

        if not candidates:
            return None

        # 按 priority（越小越优先）→ tier_rank（越小越优先，HIGH→MED→LOW）→ mapped_tag 字典序兜底
        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        return candidates[0][2]

    # ── 第 2 步：核心 + 独立模块判定 ────────────────────────────────────────────
    # 替代原来的"priority 排序选最优"。先判定核心类型，再看有没有独立模块升格玩法，
    # 最后看有没有规模溢出。返回 (main_tag, sub_tags, scale, content_hits, gameplay_signal)。

    def _collect_workshop_mapped_tags(self, wid: str) -> list[str]:
        """收集一个工坊 mod 所有有效 mapped tag（过滤语言/IP + 别名 + 归一化）。"""
        from .store_core import (
            _LANG_TAGS, _TAG_IGNORE, _TAG_ALIASES, _NORMALIZED_MAP, _normalize_tag,
        )
        from .workshop import get_cached_tags
        result = []
        for raw in get_cached_tags(wid):
            low = raw.strip().lower()
            if low in _LANG_TAGS or low in _TAG_IGNORE:
                continue
            alias = _TAG_ALIASES.get(low, low)
            mapped = _NORMALIZED_MAP.get(_normalize_tag(alias))
            if mapped is not None:
                result.append(mapped)
        return result

    def _classify_core(self, mapped_tags: list[str]) -> tuple[str, list[str], str, int, int]:
        """核心信号判定。

        返回:
          main_tag          主标签（核心类型 或 玩法扩展 或 大型整合）
          sub_tags          副标签（还含什么，不含 main_tag 自身）
          scale             规模（"单一核心" / "核心+配套" / "多模块扩展" / "大型整合"）
          content_hits      命中的内容类型数（不含非内容类 tag）
          gameplay_signal   独立模块带权求和（方案 a：MAP=2=阈值）
        """
        from .store_core import (
            _TAG_STRUCTURAL_WEIGHT, GAMEPLAY_SIGNAL_THRESHOLD,
            AUTO_TAG_CHARACTER, AUTO_TAG_NEW_CARD, AUTO_TAG_RELIC,
            AUTO_TAG_EVENT, AUTO_TAG_ENEMY, AUTO_TAG_MAP, AUTO_TAG_GAMEPLAY,
            _WORKSHOP_TAG_PRIORITY, _TAG_SPECIFICITY, _DEFAULT_SPEC,
        )

        mapped_set = set(mapped_tags)
        weights = _TAG_STRUCTURAL_WEIGHT

        # Step 0: 先判 core（不变式：派生量不能含 core）
        _CORE_ORDER = [AUTO_TAG_CHARACTER, AUTO_TAG_NEW_CARD, AUTO_TAG_RELIC,
                       AUTO_TAG_ENEMY, AUTO_TAG_EVENT, AUTO_TAG_MAP]
        core_tag = None
        for core in _CORE_ORDER:
            if core in mapped_set:
                core_tag = core
                break

        # Step 1: gameplay_signal（排除 core 自身）
        signal_pool = mapped_set - {core_tag} if core_tag else mapped_set
        gameplay_signal = sum(weights.get(t, 0) for t in signal_pool)

        # Step 2: content_hits（纯机制词/垃圾桶 不算内容类型）
        _CONTENT_TYPES = {
            AUTO_TAG_CHARACTER, AUTO_TAG_NEW_CARD, AUTO_TAG_RELIC, AUTO_TAG_EVENT,
            AUTO_TAG_ENEMY, AUTO_TAG_MAP, AUTO_TAG_POWER, AUTO_TAG_POTION,
        }
        content_hits = len(mapped_set & _CONTENT_TYPES)

        # Step 3: 判定主标签
        # 不变式：升格只能在"无明确核心信号"时触发——有 core 的 mod 主标签就是 core
        #         核心信号优先级（core_tag 已按 CORE_ORDER 命中）
        main_tag: str
        if core_tag is None and gameplay_signal >= GAMEPLAY_SIGNAL_THRESHOLD \
                and AUTO_TAG_GAMEPLAY not in mapped_set:
            # 升格：无核心 + 足够独立模块 → 玩法扩展（作者已打玩法标签不覆盖）
            main_tag = AUTO_TAG_GAMEPLAY
        elif core_tag is not None:
            main_tag = core_tag
        else:
            # 无核心 → 从 mapped_set 里按 (priority, tier) 兜底（不变式：兜底不能是 GAMEPLAY）
            _TIER_RANK = {"HIGH": 0, "MED": 1, "LOW": 2}
            candidates = [t for t in mapped_set if t != AUTO_TAG_GAMEPLAY]
            if not candidates:
                # 极端：GAMEPLAY 单独 → 退回 priority+tier（排到最后）
                candidates = [t for t in mapped_tags if t != AUTO_TAG_GAMEPLAY]
                if not candidates:
                    candidates = list(mapped_set)
            candidates.sort(key=lambda t: (
                _WORKSHOP_TAG_PRIORITY.get(t, 99),
                _TIER_RANK.get(_TAG_SPECIFICITY.get(t, _DEFAULT_SPEC), 2),
            ))
            main_tag = candidates[0]

        # Step 4: 判定规模（独立属性，不影响主标签）
        scale: str
        if content_hits >= 5:
            scale = "大型整合"
        elif gameplay_signal >= GAMEPLAY_SIGNAL_THRESHOLD:
            scale = "多模块扩展"
        elif content_hits >= 2:
            scale = "核心+配套"
        else:
            scale = "单一核心"

        # Step 5: 副标签（不含 main_tag 自身，也不含非内容类）
        sub_tags = sorted(t for t in mapped_set if t != main_tag and t in _CONTENT_TYPES)

        return main_tag, sub_tags, scale, content_hits, gameplay_signal


    def _dll_signals(self, info: ModInfo) -> Optional[dict]:
        """读取 mod 的 .NET 程序集元数据信号（带缓存）。无 DLL 返回 None。"""
        if not info.manifest.has_dll or info.install_dir is None:
            return None
        from .dllinfo import find_mod_dll, inspect_dll

        dll = find_mod_dll(info.install_dir, [info.mod_id, info.folder_name])
        if dll is None:
            return None
        return inspect_dll(dll)

    # ============================================================ P3 显式 DLL 分析

    def analyze_all_dlls(self) -> int:
        """主动分析所有带 DLL 的 mod 程序集元数据（带磁盘缓存）。

        refresh() 只扫 manifest，**完全不碰 DLL**——这是 P3 懒加载的核心。
        只有当 UI 上用户点「分析全部 DLL」或某个 mod 需要显示详细分类信号时，
        才调用这个方法。已分析过的会从磁盘缓存命中（秒级）。
        返回实际解析的 DLL 数量（已缓存的不计入）。
        """
        from .dllinfo import inspect_dll, reset_dll_progress, get_dll_progress, find_mod_dll

        dll_mods = [info for info in self._mods.values()
                    if info.manifest.has_dll and info.install_dir is not None]
        reset_dll_progress(len(dll_mods))
        parsed = 0
        for info in dll_mods:
            dll = find_mod_dll(info.install_dir, [info.mod_id, info.folder_name])
            if dll is None:
                continue
            result = inspect_dll(dll, progress_name=info.mod_id)
            if result is not None:
                parsed += 1
        _log.info("[P3] analyze_all_dlls 完成: %d 个 mod 有 DLL, 新解析 %d 个",
                  len(dll_mods), parsed)
        return parsed

    def get_dll_analysis_progress(self) -> dict:
        """返回 DLL 分析进度（{current, done, total}）。"""
        from .dllinfo import get_dll_progress
        return get_dll_progress()

    def mod_dll_signals(self, mod_id: str) -> Optional[dict]:
        """返回某 mod 的 DLL 分析结果（懒加载：没有缓存就即时解析）。"""
        info = self._mods.get(mod_id.lower())
        if info is None:
            return None
        return self._dll_signals(info)

    # ============================================================ P0 Steam 工坊详情

    def _run_steam_check(self, force_refresh: bool = False) -> None:
        """拉取所有 workshop mod 的 Steam 在线详情，对比本地 mtime 标记更新状态。

        统一用 workshop.py 的 meta.{lang}.json 作为单一缓存（RimCrow 方案）：
          - 有 meta.zh.json 且有 title → 直接构建 SteamModInfo（零 HTTP 请求）
          - 只有 meta 缺失时才调 steam_webapi 批量 API
          - 避免每次启动 196 个全量拉触发 429 风控
        """
        from .steam_webapi import get_workshop_batch_info, enrich_with_local_mtime
        from .workshop import _load_cached, _migrate_legacy_meta

        ws_lang = self.get_language()
        workshop_ids: set[str] = set()
        dir_map: dict[str, Path] = {}
        for info in self._mods.values():
            if info.workshop_id:
                workshop_ids.add(str(info.workshop_id))
                if info.install_dir is not None:
                    dir_map[str(info.workshop_id)] = info.install_dir
        if not workshop_ids:
            self._steam_info = {}
            return

        # 1) 先从 meta.zh.json 构建已有缓存的那部分
        pre_filled: dict[str, SteamModInfo] = {}
        missing: list[str] = []
        from .steam_webapi import SteamModInfo
        for wid in workshop_ids:
            _migrate_legacy_meta(wid)
            cached = _load_cached(wid, ws_lang)
            if cached and cached.get("title"):
                # 从 meta.zh.json 构建 SteamModInfo（有 time_updated 就用，没有用 0）
                stats = cached.get("stats") or {}
                sub_raw = str(stats.get("当前订阅者") or cached.get("subscriptions") or "0").replace(",", "")
                subs = 0
                try: subs = int(sub_raw)
                except: pass
                pre_filled[wid] = SteamModInfo(
                    workshop_id=wid,
                    result=1,
                    title=cached.get("title", ""),
                    description=cached.get("description", ""),
                    subscriptions=subs,
                    time_updated=int(cached.get("time_updated") or 0),
                    preview_url=cached.get("preview_url", ""),
                    tags=cached.get("tags") or [],
                )
            else:
                missing.append(wid)

        _log.info("[P0] Steam 检查: %d 个有 meta 缓存直接复用, %d 个缺失需在线拉",
                  len(pre_filled), len(missing))

        # 2) 只有缺失的才批量 API 拉
        if missing:
            raw = get_workshop_batch_info(missing, force_refresh=force_refresh, language=ws_lang)
            pre_filled.update(raw)

        self._steam_info = enrich_with_local_mtime(pre_filled, dir_map)
        valid = sum(1 for info in self._steam_info.values() if info.is_valid)
        offline = sum(1 for info in self._steam_info.values() if not info.is_valid)
        has_update = sum(1 for info in self._steam_info.values() if info.update_status == "has-update")
        _log.info("[P0] Steam 检查完成: 总 workshop mod=%d, 在线返回=%d, 有效=%d, 已下架=%d, 有更新=%d",
                  len(workshop_ids), len(self._steam_info), valid, offline, has_update)

    def steam_info_for(self, mod_id: str) -> Optional[object]:
        """返回某 mod 的 Steam 在线信息（SteamModInfo 实例）；无 workshop_id 或未查到返回 None。"""
        info = self._mods.get(mod_id.lower())
        if info is None or not info.workshop_id:
            return None
        return self._steam_info.get(info.workshop_id)

    def check_steam_details(self, force_refresh: bool = False) -> dict[str, object]:
        """手动触发 Steam 检查（refresh 之后用户点按钮）。返回当前 _steam_info。"""
        self._run_steam_check(force_refresh=force_refresh)
        return dict(self._steam_info)

    def set_auto_check_steam(self, enabled: bool) -> None:
        """设置是否自动检查 Steam 工坊详情。"""
        self._state.auto_check_steam = bool(enabled)
        self._mark_dirty()


    # ============================================================ mod 备份 --

    @property
    def backup_root(self) -> Path:
        """备份根目录（用户自定义优先，否则 <data>/backups）。"""
        if self._state.backup_dir:
            try:
                return Path(self._state.backup_dir)
            except Exception:
                pass
        return self.data_dir / "backups"

    def get_backup_settings(self) -> dict:
        return {
            "auto_backup": bool(self._state.auto_backup),
            "keep_versions": int(self._state.keep_versions),
            "backup_dir": self._state.backup_dir,
            "effective_dir": str(self.backup_root),
        }

    def set_backup_settings(
        self,
        auto_backup: Optional[bool] = None,
        keep_versions: Optional[int] = None,
        backup_dir: Optional[str] = None,
    ) -> None:
        """更新备份设置（只更新传入的字段）。backup_dir 传空字符串表示恢复默认。"""
        if auto_backup is not None:
            self._state.auto_backup = bool(auto_backup)
        if keep_versions is not None:
            try:
                self._state.keep_versions = max(1, int(keep_versions))
            except (TypeError, ValueError):
                pass
        if backup_dir is not None:
            self._state.backup_dir = str(backup_dir).strip() or None
        self._mark_dirty()


    # ============================================================ 全局依赖检查

    @staticmethod
    def _id_strip_hash(mod_id_lower: str) -> str:
        """剥掉 mod_id 末尾的 hash 后缀（_xxxxxxxx，6+ 位 hex），
        解决 STS2-RitsuLib vs STS2-RitsuLib_6c5876ce 这种多版本 mod 匹配问题。"""
        return re.sub(r"_[0-9a-f]{6,}$", "", mod_id_lower)

    @staticmethod
    def _dep_id(dep_raw) -> str:
        """从 manifest.dependencies 的单个条目里提取 mod_id。
        条目可能是 str（旧格式）或 dict（新格式 {id, min_version}）。"""
        if isinstance(dep_raw, dict):
            return str(dep_raw.get("id") or dep_raw.get("mod_id") or "").strip()
        return str(dep_raw or "").strip()

    def _match_dep(self, dep_id_raw: str, candidate_ids: set[str]) -> Optional[str]:
        """依赖匹配：先精确，再剥 hash 宽松匹配。返回匹配到的 candidate_id（lower）或 None。"""
        dep = str(dep_id_raw).strip().lower()
        if dep in candidate_ids:
            return dep
        dep_stripped = self._id_strip_hash(dep)
        for cid in candidate_ids:
            if self._id_strip_hash(cid) == dep_stripped:
                return cid
        return None

    def _check_all_dependencies(self) -> list[str]:
        """扫描所有启用侧 mod 的依赖，返回警告列表（不修改状态）。

        警告分类：
          [MISSING_INSTALL]  依赖的 mod 本地根本没装
          [MISSING_ENABLE]   依赖的 mod 已装但未启用（最常见，比如你忘了启用 RitsuLib）
          [ORDER_WARN]       依赖顺序错误（前置 mod 排在子 mod 后面，可能导致加载失败）
          [CYCLE]            检测到循环依赖

        O2 优化：enabled_ids_set / enabled_list / enabled_ids_info 全部走缓存，
        避免每次 refresh 重建 list + dict。
        """
        warnings: list[str] = []
        all_ids = {k.lower() for k in self._mods.keys()}
        enabled_ids_set = self._enabled_ids_set()  # O2: 从缓存读
        enabled_list = [info.mod_id.lower() for info in self.ordered_enabled()]
        enabled_ids_info = list(zip(enabled_list, self.ordered_enabled()))

        # 1) 缺依赖：遍历每个启用侧 mod 的 dependencies（宽松匹配 hash 后缀）
        for mid, info in enabled_ids_info:
            for dep_raw in (info.manifest.dependencies or []):
                dep_lower = self._dep_id(dep_raw).lower()
                if not dep_lower:
                    continue
                matched_all = self._match_dep(dep_lower, all_ids)
                matched_enabled = self._match_dep(dep_lower, enabled_ids_set)
                if matched_all is None:
                    warnings.append(
                        f"[MISSING_INSTALL] {info.display_name} 依赖 {dep_lower}，但本地未安装"
                    )
                elif matched_enabled is None:
                    dep_info = self._mods.get(matched_all)
                    dep_label = dep_info.display_name if dep_info else matched_all
                    warnings.append(
                        f"[MISSING_ENABLE] {info.display_name} 依赖 {dep_label}，但 {dep_label} 未启用"
                    )

        # 先把启用侧每个 mod 的依赖解析成「已匹配的启用侧 id」，后面顺序/循环检查共用
        matched_enabled_deps: dict[str, list[str]] = {}
        for mid, info in enabled_ids_info:
            deps: list[str] = []
            for dep_raw in (info.manifest.dependencies or []):
                dep_lower = self._dep_id(dep_raw).lower()
                if not dep_lower:
                    continue
                matched_enabled = self._match_dep(dep_lower, enabled_ids_set)
                if matched_enabled and matched_enabled != mid and matched_enabled not in deps:
                    deps.append(matched_enabled)
            matched_enabled_deps[mid] = deps

        # 双向引用豁免：工坊页面常把「联动 mod」互相写进 Required Items（例如 A 与 B 各自
        # 把对方列为前置）。这不是真正的前置关系——任何排序都必有一条被判倒序、并形成环，
        # 属于必然误报。因此双向边既不参与顺序检查，也不参与循环检测。
        edges = {(mid, dep) for mid, deps in matched_enabled_deps.items() for dep in deps}
        mutual_edges = {e for e in edges if (e[1], e[0]) in edges}

        # 2) 顺序错误：启用侧列表中，子 mod 排在它的前置 mod 前面
        idx_map = {mid: i for i, mid in enumerate(enabled_list)}
        for mid, info in enabled_ids_info:
            my_idx = idx_map.get(mid, -1)
            for matched_enabled in matched_enabled_deps[mid]:
                if (mid, matched_enabled) in mutual_edges:
                    continue
                dep_idx = idx_map.get(matched_enabled)
                if dep_idx is not None and dep_idx > my_idx:
                    dep_info = self._mods.get(matched_enabled)
                    dep_label = dep_info.display_name if dep_info else matched_enabled
                    warnings.append(
                        f"[ORDER_WARN] {info.display_name} 排在它的前置 {dep_label} 前面，可能导致加载失败"
                    )

        # 3) 循环依赖检测（Tarjan SCC 简化版）
        graph: dict[str, set[str]] = {mid: set() for mid in enabled_list}
        for mid in enabled_list:
            for matched_enabled in matched_enabled_deps[mid]:
                if (mid, matched_enabled) in mutual_edges:
                    continue
                graph[mid].add(matched_enabled)
        cycles = _detect_cycles(graph)
        for cycle in cycles:
            names = " → ".join(
                self._mods[c].display_name if c in self._mods else c
                for c in cycle
            )
            warnings.append(f"[CYCLE] 检测到循环依赖：{names}")

        # 去重保持顺序
        seen: set[str] = set()
        deduped: list[str] = []
        for w in warnings:
            if w not in seen:
                seen.add(w)
                deduped.append(w)
        # 同时构建单 mod 警告映射（per_mod 也用宽松匹配）
        per_mod: dict[str, list[str]] = {}
        for mid, info in enabled_ids_info:
            for dep_raw in (info.manifest.dependencies or []):
                dep_lower = self._dep_id(dep_raw).lower()
                if not dep_lower:
                    continue
                matched_all = self._match_dep(dep_lower, all_ids)
                matched_enabled = self._match_dep(dep_lower, enabled_ids_set)
                if matched_all is None:
                    per_mod.setdefault(mid, []).append("missing_install")
                elif matched_enabled is None:
                    per_mod.setdefault(mid, []).append("missing_enable")
                else:
                    dep_idx = idx_map.get(matched_enabled)
                    my_idx = idx_map.get(mid, -1)
                    # 双向引用不是真正的前置关系，不计 order_warn
                    if dep_idx is not None and dep_idx > my_idx and (mid, matched_enabled) not in mutual_edges:
                        per_mod.setdefault(mid, []).append("order_warn")
        for cycle in cycles:
            for mid in cycle:
                per_mod.setdefault(mid, []).append("cycle")
        self._mod_warnings_map = per_mod
        return deduped

    def dependency_warnings(self) -> list[str]:
        """返回上次 refresh() 时收集的依赖警告列表。"""
        return list(self._warnings)

    def mod_warning_flags(self, mod_id: str) -> set[str]:
        """返回某 mod 的警告类型集合（空 set = 无警告）。
        可能值: missing_install / missing_enable / order_warn / cycle
        """
        return set(self._mod_warnings_map.get(str(mod_id).lower(), []))

    def _merge_workshop_deps(self, scan) -> None:
        """爬 Steam 工坊页面拿依赖，合并到 scan.mods 里的 ModInfo.manifest.dependencies。

        RimCrow 式策略：
          1) 有 7 天缓存的直接跳过（永不重复爬）
          2) 需要爬的用 ThreadPoolExecutor 4 线程并发（限速锁全局，不会撞风控）
          3) manifest 已自带 dependencies 的跳过
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from .workshop import fetch_workshop_dependencies
        from .scanner import set_scan_stage, tick_scan_progress

        # 1) 建 workshop_id → ModInfo 映射
        ws_to_mod: dict[str, ModInfo] = {}
        for info in scan.mods.values():
            if info.workshop_id:
                ws_to_mod[str(info.workshop_id)] = info

        if not ws_to_mod:
            return

        # 2) 分桶：已有依赖 → 跳过；没依赖 → 爬（2026-11-03 重构：旧 _is_rate_limited 已砍）
        rate_limited_skipped = 0
        to_fetch: list[tuple[str, ModInfo]] = []
        for wid, info in ws_to_mod.items():
            if info.manifest.dependencies:
                continue  # manifest 自带，不用爬
            to_fetch.append((wid, info))

        total = len(ws_to_mod)
        skip = total - len(to_fetch)
        _log.info("[ws-deps] %d 个 workshop mod: %d manifest自带跳过，%d 封禁跳过，%d 需要爬", total, skip - rate_limited_skipped, rate_limited_skipped, len(to_fetch))

        merged_count = 0

        if not to_fetch:
            _log.info("[ws-deps] 完成: %d/%d 补充（全自带）", merged_count, total)
            return

        set_scan_stage("fetch_deps")

        # 3) 并发爬 4 线程（workshop 模块的限速锁是全局的，线程间自动排队 1.2s 间隔）
        def _fetch_item(task: tuple[str, ModInfo]) -> tuple[str, Optional[list[dict]]]:
            wid, _ = task
            tick_scan_progress(current=f"爬依赖: {wid}")
            try:
                return wid, fetch_workshop_dependencies(wid, timeout=15)
            except Exception as e:
                _log.warning("[ws-deps] 爬 %s 失败: %s", wid, e)
                return wid, None

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ws-deps") as ex:
            futures = {ex.submit(_fetch_item, t): t for t in to_fetch}
            for fut in as_completed(futures):
                wid, ws_deps = fut.result()
                if not ws_deps:
                    continue
                info = ws_to_mod[wid]
                # 4) workshop 依赖 ID → 本地 mod_id 映射
                added: list[str] = []
                for dep in ws_deps:
                    dep_wid = str(dep.get("workshop_id", ""))
                    if dep_wid and dep_wid in ws_to_mod:
                        dep_mod_id = ws_to_mod[dep_wid].mod_id
                        if dep_mod_id not in added:
                            added.append(dep_mod_id)
                    else:
                        if dep_wid not in added:
                            added.append(dep_wid)
                if added:
                    info.manifest.dependencies = added
                    merged_count += 1

        _log.info("[ws-deps] 完成: %d/%d 个 workshop mod 补充了依赖", merged_count, total)

    def has_dependency_warning(self, mod_id: str) -> bool:
        """快速判断某 mod 是否有任何依赖警告（用于 UI 标红）。"""
        return bool(self._mod_warnings_map.get(str(mod_id).lower()))

    def recheck_dependencies(self) -> list[str]:
        """手动触发一次全局依赖检查（不写 refresh），更新 _warnings 并返回。"""
        self._warnings = self._check_all_dependencies()
        _log.info("[depcheck] 全局依赖检查: %d 条警告", len(self._warnings))
        return list(self._warnings)

    def _pck_signals(self, info: ModInfo) -> Optional[dict]:
        """读取 mod 的 .pck 资源路径信号（带缓存）。无 pck 返回 None。"""
        if info.install_dir is None:
            return None
        from .pckinfo import find_mod_pck, inspect_pck

        pck = find_mod_pck(info.install_dir, [info.mod_id, info.folder_name])
        if pck is None:
            return None
        return inspect_pck(pck)

    def set_mod_tags(self, mod_id: str, tags: list[str]) -> None:
        """设置某个 mod 的管理器标签（覆盖）。

        非空列表 → 记录为手动标签（优先级最高，覆盖自动识别）
        空列表   → 删除该 mod 的手动标签 key，让 get_mod_tags_with_source
                   回退到自动识别（_detect_tag_with_reason）。
        """
        low = mod_id.lower()
        clean = []
        for t in tags:
            t = str(t).strip()
            if t and t in self._state.manager_tags and t not in clean:
                clean.append(t)
        if clean:
            self._state.mod_tags[low] = clean
        else:
            # 空 → 删 key（不存空 list，否则 get_mod_tags_with_source 里
            # manual is not None 永远为 True，标签无法回退自动检测）
            self._state.mod_tags.pop(low, None)
        self._mark_dirty()


    # ------------------------------------------------------ P0 mod_meta/color --

    def get_mod_meta(self, mod_id: str) -> dict:
        """返回 {alias: str, note: str}，缺省值为空串。"""
        default = {"alias": "", "note": ""}
        meta = self._state.mod_meta.get(mod_id.lower())
        if not meta:
            return default
        return {
            "alias": str(meta.get("alias", "")),
            "note": str(meta.get("note", "")),
        }

    def set_mod_meta(self, mod_id: str, alias: Optional[str] = None, note: Optional[str] = None) -> None:
        """设置 alias/note（传 None 表示不改那个字段；传空串表示清空）。"""
        low = mod_id.lower()
        cur = self._state.mod_meta.get(low, {})
        new = {
            "alias": alias if alias is not None else str(cur.get("alias", "")),
            "note": note if note is not None else str(cur.get("note", "")),
        }
        if new["alias"] or new["note"]:
            self._state.mod_meta[low] = new
        else:
            self._state.mod_meta.pop(low, None)
        self._mark_dirty()


    def get_mod_color(self, mod_id: str) -> Optional[str]:
        """返回 mod 的自定义颜色 "#hex"，未设置返回 None。"""
        return self._state.mod_colors.get(mod_id.lower())

    def set_mod_color(self, mod_id: str, color: Optional[str]) -> None:
        """设置 mod 的颜色；传 None 或空串清空。"""
        low = mod_id.lower()
        if color and color.startswith("#") and len(color) in (4, 7):
            self._state.mod_colors[low] = color
        else:
            self._state.mod_colors.pop(low, None)
        self._mark_dirty()


    # ------------------------------------------------------ P1 Pinned Mods --

    def get_pinned(self, mod_id: str) -> Optional[dict]:
        """返回 mod 的锁定信息 {version, enabled}，未锁定返回 None。"""
        return self._state.pinned.get(mod_id.lower())

    def set_pinned(self, mod_id: str, version: Optional[str] = None, enabled: Optional[bool] = None) -> None:
        """锁定一个 mod：version 锁定版本号（None 表示锁当前），enabled 锁定启停状态。"""
        low = mod_id.lower()
        cur = self._state.pinned.get(low, {})
        entry = dict(cur)
        if version is not None:
            entry["version"] = version
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        self._state.pinned[low] = entry
        self.save_state()

    def unpin(self, mod_id: str) -> None:
        """解除一个 mod 的锁定。"""
        self._state.pinned.pop(mod_id.lower(), None)
        self.save_state()

    def all_pinned(self) -> dict[str, dict]:
        """返回所有锁定的 mods（浅拷贝）。"""
        return {k: dict(v) for k, v in self._state.pinned.items()}

    # ------------------------------------------------------ P0 自定义分组 --

    def get_groups(self) -> dict[str, list[str]]:
        """返回当前所有自定义分组（浅拷贝）。"""
        return {k: list(v) for k, v in self._state.groups.items()}

    def get_group_order(self) -> list[str]:
        return list(self._state.group_order)

    def create_group(self, name: str) -> bool:
        """创建空分组；name 已存在或非法返回 False。"""
        name = name.strip()
        if not name or name in self._state.groups:
            return False
        self._state.groups[name] = []
        if name not in self._state.group_order:
            self._state.group_order.append(name)
        self.save_state()
        return True

    def delete_group(self, name: str) -> bool:
        """删除分组（mods 不会被删除，只移除分组归属）。"""
        if name not in self._state.groups:
            return False
        del self._state.groups[name]
        if name in self._state.group_order:
            self._state.group_order.remove(name)
        self.save_state()
        return True

    def rename_group(self, old_name: str, new_name: str) -> bool:
        if old_name not in self._state.groups:
            return False
        new_name = new_name.strip()
        if not new_name or new_name in self._state.groups:
            return False
        self._state.groups[new_name] = self._state.groups.pop(old_name)
        if old_name in self._state.group_order:
            self._state.group_order[self._state.group_order.index(old_name)] = new_name
        self.save_state()
        return True

    def add_mod_to_group(self, mod_id: str, group_name: str) -> bool:
        if group_name not in self._state.groups:
            return False
        low = mod_id.lower()
        if low not in self._state.groups[group_name]:
            self._state.groups[group_name].append(low)
        self.save_state()
        return True

    def remove_mod_from_group(self, mod_id: str, group_name: str) -> bool:
        if group_name not in self._state.groups:
            return False
        low = mod_id.lower()
        if low in self._state.groups[group_name]:
            self._state.groups[group_name].remove(low)
            self.save_state()
            return True
        return False

    def set_group_mods(self, group_name: str, mod_ids: list[str]) -> bool:
        """覆盖设置某分组包含的 mods（mod_id list，可为空）。"""
        if group_name not in self._state.groups:
            return False
        clean = [m.lower() for m in mod_ids]
        self._state.groups[group_name] = clean
        self.save_state()
        return True

    def get_mod_groups(self, mod_id: str) -> list[str]:
        """返回该 mod 所在的所有自定义分组名。"""
        low = mod_id.lower()
        return [g for g, mods in self._state.groups.items() if low in mods]

    def reorder_group(self, mod_id: str, group_name: str, new_index: int) -> bool:
        """在某分组内移动 mod 的位置（用于分组内排序）。"""
        if group_name not in self._state.groups:
            return False
        low = mod_id.lower()
        mods = self._state.groups[group_name]
        if low not in mods:
            return False
        cur_idx = mods.index(low)
        mods.pop(cur_idx)
        new_index = max(0, min(new_index, len(mods)))
        mods.insert(new_index, low)
        self.save_state()
        return True

    # ------------------------------------------------------ 卡面角色归属 --

    def get_card_role(self, mod_id: str) -> Optional[str]:
        """返回卡面 mod 的手动角色归属（未手动指定返回 None）。"""
        return self._state.card_roles.get(mod_id.lower())

    def set_card_role(self, mod_id: str, role: Optional[str]) -> None:
        """手动指定卡面 mod 的角色（None/空则移除手动指定，回退到自动识别）。"""
        low = mod_id.lower()
        if role:
            self._state.card_roles[low] = role
        else:
            self._state.card_roles.pop(low, None)
        self.save_state()

    @_serialized
    def set_manual_paths(
        self,
        manual_game_root: Optional[str] = None,
        manual_mods_dirs: Optional[str] = None,
        manual_workshop_dir: Optional[str] = None,
        manual_mods_dir: Optional[str] = None,
        manual_save_dir: Optional[str] = None,
    ) -> None:
        """设置手动指定的游戏根目录 / 额外 mods 目录 / 创意工坊目录 / 本地 Mods 目录 / 存档目录，并落盘。"""
        self._state.manual_game_root = manual_game_root or None
        self._state.manual_mods_dirs = manual_mods_dirs or None
        self._state.manual_workshop_dir = manual_workshop_dir or None
        self._state.manual_mods_dir = manual_mods_dir or None
        self._state.manual_save_dir = manual_save_dir or None
        self.save_state()

    # ------------------------------------------------------------- 核心流程 --

    @_serialized
    def refresh(self, manual_game_root: Optional[Path] = None) -> ModScanResult:
        """重新检测游戏 + 扫描所有 mod，然后用扫描结果修正 state 中的 order。"""
        from .scanner import detect_game_install

        self._errors.clear()
        game_root_arg = (
            Path(manual_game_root)
            if manual_game_root
            else (Path(self._state.manual_game_root) if self._state.manual_game_root else None)
        )
        _log.info("refresh 开始, manual_root=%s", game_root_arg)
        game = detect_game_install(game_root_arg)
        if game is None:
            _log.error("refresh 失败: 未检测到 STS2 安装目录")
            self._game = None
            self._mods = {}
            scan = ModScanResult(game=None)  # type: ignore[arg-type]
            scan.errors.append("找不到 STS2 安装目录，请在设置里手动指定路径。")
            self._errors.extend(scan.errors)
            return scan

        _log.info(
            "refresh: 检测到游戏, root=%s, exe=%s, detected_by=%s, workshop=%s",
            game.game_root,
            game.exe_path,
            game.detected_by,
            game.workshop_dir,
        )
        self._game = game
        if manual_game_root is not None:
            self._state.manual_game_root = str(manual_game_root)
        elif self._state.manual_game_root and str(game_root_arg) != str(self._state.manual_game_root):
            pass  # 用户没指定就不改

        # 用户手动指定的创意工坊目录 / 本地 Mods 目录，覆盖自动检测结果
        if self._state.manual_workshop_dir:
            game.workshop_dir = Path(self._state.manual_workshop_dir)
        if self._state.manual_mods_dir:
            game.mods_dir = Path(self._state.manual_mods_dir)

        # 迁移：把工坊目录里遗留的 .json.disabled 还原为 .json（旧「改后缀停用」的遗留）。
        # 新行为改用原生 is_enabled 表达停用，mod 物理保留，游戏才能扫到并显示「停用」。
        try:
            # 2026-10-16: 删除 _restore_workshop_manifests
            # 旧：一次性迁移，把 .json.disabled 还原成 ACTIVE（假设停用只靠 settings.save）
            # 新：manifest 后缀本身就是启停状态的一部分（停用→.json.disabled）
            # 每次 refresh 还原来一次 → 用户 toggle 停用白改！
            pass
        except Exception as e:
            _log.warning("refresh: 工坊 manifest 迁移失败(继续): %s", e)

        # 解析用户手动指定的额外 mods 目录（用 ";" 或 "|" 分隔）
        extra_dirs: list[Path] = []
        if self._state.manual_mods_dirs:
            for part in self._state.manual_mods_dirs.replace("|", ";").split(";"):
                part = part.strip()
                if part:
                    extra_dirs.append(Path(part))

        scan = scan_all_mods(game, extra_mods_dirs=extra_dirs)
        # ⚠️ 先赋值 mods（秒完成）——不依赖爬依赖。爬依赖变成纯后台操作，完成后再合并
        self._mods = scan.mods
        self._mod_versions = scan.versions
        self._errors.extend(scan.errors)
        self._subscribed_known = bool(getattr(scan, "subscribed_known", True))

        # 爬 Steam 工坊依赖 → 纯后台线程（撞 429 也不阻塞 refresh）
        def _bg_merge_deps() -> None:
            try:
                _log.info("[ws-deps] 后台依赖爬取开始（不阻塞 UI）")
                self._merge_workshop_deps(scan)
                _log.info("[ws-deps] 后台依赖爬取完成")
            except Exception as e:
                _log.exception("[ws-deps] 后台依赖爬取失败: %s", e)

        threading.Thread(target=_bg_merge_deps, daemon=True, name="ws-deps-bg").start()

        # ================================================================
        # STS2 原生清单（settings.save → mod_settings.mod_list）：
        #   有就加载（占位符始终保留）；但 uses_native_mod_list property 会额外检查
        #   「LoadOrderManager 是否物理启用」——只有 LOM 真的会被 STS2 启动时加载，
        #   settings.save.is_enabled 才作为启用/停用权威分类依据。如果 LOM 没物理启用，
        #   这里会照常加载原生清单（用于排序/占位符），但启用/停用分类会按物理目录驱动。
        # ================================================================
        self._native_save_path = locate_settings_save(
            Path(self._state.manual_save_dir) if self._state.manual_save_dir else None
        )
        self._native_full_data = None
        if self._native_save_path is not None:
            self._native_full_data = load_native_mod_list(self._native_save_path)
        # 重置缓存：refresh 时一次性算出 uses_native_mod_list，set_enabled 反复访问也走缓存
        self._uses_native_mod_list_cache = None
        # 是否真的用原生清单作为启用/停用权威 = uses_native_mod_list property
        uses_native_now = bool(self.uses_native_mod_list)  # 第一次访问会计算并写入缓存
        _log.info(
            "refresh: 原生清单路径=%s 存在且有效=%s → 最终 uses_native_mod_list(权威分类)=%s",
            self._native_save_path,
            bool(self._native_full_data and isinstance(self._native_full_data.get("mod_settings"), dict)
                 and isinstance(self._native_full_data["mod_settings"].get("mod_list"), list)),
            uses_native_now,
        )
        if uses_native_now:
            # 原生清单里的顺序即游戏启动加载顺序
            nat_enabled = nat_extract_enabled_order_ids(self._native_full_data)  # type: ignore[arg-type]
            nat_disabled = nat_extract_disabled_order_ids(self._native_full_data)  # type: ignore[arg-type]

            # === source_states 恢复链路（2026-10 修复）===
            # settings.save 现在按 canonical 粒度写（不带 source 字段，避免重复 id 崩游戏）
            # per-source 状态独立存 profile.json
            # 优先级：profile.json source_states > settings.save（兼容老数据）
            profile_data: dict = {}
            try:
                if self.profile_path.is_file():
                    profile_data = json.loads(self.profile_path.read_text(encoding="utf-8"))
            except Exception:
                profile_data = {}
            profile_ss = profile_data.get("source_states") if isinstance(profile_data, dict) else None
            if isinstance(profile_ss, dict) and profile_ss:
                self._source_states = profile_ss
                _log.info(
                    "refresh: source_states 从 profile.json 恢复: %d 个 mod_id（其中 %d 个有 ≥2 个 source）",
                    len(self._source_states),
                    sum(1 for s in self._source_states.values() if isinstance(s, dict) and len(s) >= 2),
                )
            else:
                # 老数据 fallback：从 settings.save 读（如果有 source 字段的话）
                self._source_states = nat_extract_source_states(self._native_full_data)  # type: ignore[arg-type]
                _log.info(
                    "refresh: source_states 从 settings.save 提取（profile.json 无数据）: %d 个 mod_id",
                    len(self._source_states),
                )

            # 2026-10-15 迁移：把老的 "mods_directory" / 不认识的 inner key 替换成正确的 per-physical-copy key
            # 老 profile.json 和老 settings.save 里本地副本统一叫 "mods_directory"，
            # 导致 local + disabled 两个物理不同的副本撞 key → 一个覆盖另一个。
            # 迁移策略：用 _mod_versions 里的实际物理副本构建合法 inner key 集合，
            # 老 key 如果无法精确映射 → 丢掉，从 settings.save is_enabled + 物理位置重建。
            _migrated = 0
            for mid_lower, old_ss in list(self._source_states.items()):
                if not isinstance(old_ss, dict):
                    continue
                # 构建这个 mod_id 有哪些物理副本 → 合法 inner keys（2026-11-01 新格式）
                versions = self._mod_versions.get(mid_lower, [])
                valid_keys: set[str] = set()
                for v in versions:
                    if v.workshop_id:
                        # 新格式：每个 workshop 副本独立 key
                        valid_keys.add(f"ws_{v.workshop_id}")
                    else:
                        valid_keys.add(v.source)  # "local" 或 "disabled"
                # 如果没有物理副本（ghost），跳过
                if not valid_keys:
                    continue
                # 检查是否有老 key（不在合法集合里）
                old_keys = set(old_ss.keys())
                if old_keys <= valid_keys:
                    continue  # 已经是新格式
                # 迁移！所有 key 都用新格式，老格式 "steam_workshop" 丢掉
                _migrated += 1
                new_ss: dict[str, bool] = {}
                # 老 key 无法精确映射（比如老的 "steam_workshop" 不知道对应哪个 ws_{wid}）
                # → 从物理位置推断默认值 + 老的 "mods_directory" 值兜底
                canonical_enabled = bool(
                    next(
                        (e.get("is_enabled") for e in self._native_full_data.get("mod_settings", {}).get("mod_list", [])
                         if isinstance(e, dict) and str(e.get("id", "")).lower() == mid_lower),
                        False,
                    )
                )
                mods_dir_old_value = old_ss.get("mods_directory")
                for v in versions:
                    key = f"ws_{v.workshop_id}" if v.workshop_id else v.source
                    if v.source == "local":
                        new_ss[key] = True if mods_dir_old_value is None else bool(mods_dir_old_value)
                    elif v.source == "disabled":
                        new_ss[key] = False
                    else:
                        # workshop 副本
                        new_ss[key] = bool(mods_dir_old_value) if mods_dir_old_value is not None else canonical_enabled
                self._source_states[mid_lower] = new_ss
                _log.info("refresh: source_states 迁移 mod_id=%s: 老 key=%s → 新 %s",
                         mid_lower, old_keys - valid_keys, new_ss)
            if _migrated:
                _log.warning("refresh: source_states 迁移完成: %d 个 mod_id 从老格式迁移到 per-physical-copy 格式", _migrated)

            # source_last_toggled 同理
            profile_st = profile_data.get("source_last_toggled") if isinstance(profile_data, dict) else None
            if isinstance(profile_st, dict):
                self._source_last_toggled = profile_st
            else:
                self._source_last_toggled = {}

            ms = self._native_full_data["mod_settings"]  # type: ignore[index]
            nat_entries = ms.get("mod_list", [])  # type: ignore[union-attr]

            # 存 settings.save 里 mod_list 的 canonical 顺序（给游戏原生 Kahn 排序当平级优先级）
            seen_ids = set()
            self._native_modlist_canon_order = []
            for entry in nat_entries:
                if not isinstance(entry, dict):
                    continue
                mid = str(entry.get("id", ""))
                mid_l = mid.lower()
                if mid_l not in seen_ids:
                    seen_ids.add(mid_l)
                    self._native_modlist_canon_order.append(mid)

            # ------------------------------------------------------------
            # 关键修复：settings.save 里写了 mod_list，但当前 scan_all_mods 没扫到 manifest
            #          → 用占位 ModInfo 补入 self._mods，保证启用/停用列表不会把它们整个扔掉。
            # 场景：① workshop_dir 扫描暂时没命中（AppID 不一致 / 库目录没扫到）
            #      ② Steam 已经下载到 workshop/content/<AppID>/，但 Mods/ 下没种 stub
            #      ③ mod 目录结构奇怪扫描漏掉
            # 占位 ModInfo: source=原生的 source 映射, install_dir=None, is_placeholder=True
            # ------------------------------------------------------------
            placeholders_added = 0
            placeholders_updated = 0
            # 来自「已取消订阅的工坊残留目录」的 manifest id：游戏不会加载，不补占位
            orphan_ids = getattr(scan, "unsubscribed_mod_ids", set())
            for entry in nat_entries:
                if not isinstance(entry, dict):
                    continue
                mid = entry.get("id")
                if not isinstance(mid, str) or not mid:
                    continue
                src_native = entry.get("source")
                low = mid.lower()
                if low in self._mods:
                    continue  # 扫描已经命中，不用占位
                if low in orphan_ids:
                    continue  # 该 mod 来自已取消订阅的工坊残留目录，游戏不会加载，不补占位
                # 映射 source：settings.save 里是游戏 ModSource 枚举的序列化值
                #   "steam_workshop" → workshop；"mods_directory"（或历史脏值）→ local
                source_for_info = "workshop" if src_native == "steam_workshop" else (
                    "disabled" if str(src_native).lower() in ("disabled",) else "local"
                )
                manifest = ModManifest(
                    id=mid,
                    name=mid,  # 暂时没有更好的 name 就用 id
                    version=None, author=None,
                    affects_gameplay=False, has_pck=False, has_dll=False,
                )
                info = ModInfo(
                    manifest=manifest,
                    folder_name=mid,
                    install_dir=None,  # 占位
                    source=source_for_info,
                    workshop_id=None,  # 后续 workshop 扫描命中后会被真条目覆盖
                )
                self._mods[low] = info
                placeholders_added += 1
            # 如果 workshop_dir 现在能找到了，再扫一遍 workshop/content，用真实 manifest 覆盖掉占位符 / mods_disabled 里的旧 stub
            if self._game is not None and self._game.workshop_dir is not None and self._game.workshop_dir.is_dir():
                try:
                    from .scanner import _scan_folder, read_subscribed_workshop_ids

                    def _workshop_entry_should_override(existing_info, new_info):
                        """新 info 是从 workshop 原目录扫出来的真条目：只有它比 existing 更可信时才覆盖。

                        关键保护：
                        - 如果 existing 已经是真·workshop 条目（有 workshop_id + 非 placeholder），
                          且 source 一致（都是 workshop 或都是 disabled），就不覆盖——
                          避免主扫描和二次补扫互相打架。
                        - 只有 placeholder、source 不一致、或 workshop_id 不匹配时才允许覆盖。
                        """
                        if existing_info is None:
                            return True
                        # 任何真条目 > 占位
                        if existing_info.is_placeholder:
                            return True
                        # 没有 install_dir 的占位 > 有 install_dir 的 placeholder 也应该被覆盖（更可信）
                        if existing_info.install_dir is None:
                            return True
                        # source 不一致才覆盖（比如主扫描扫成 disabled，二次补扫扫成 workshop ACTIVE）
                        if existing_info.source != new_info.source:
                            return True
                        return False

                    def _compute_store_key(mod_id: str, workshop_id: Optional[str]) -> str:
                        """生成 store 唯一 key。

                        当多个 workshop mod 内部 mod_id 相同但 workshop_id 不同时，
                        用派生 key 并存（而非覆盖）。比如 ShengZhuSts2Mod 的原版和修复版
                        mod.json 里 id 都是 ShengZhuSts2Mod，但 workshop_id 分别是
                        3747713325 和 3815666326。
                        """
                        low = mod_id.lower()
                        # 基本 key 不存在 → 直接用基本 key
                        if low not in self._mods:
                            return low
                        existing = self._mods[low]
                        # 如果 existing 的 workshop_id 相同，说明是同一个 mod，正常覆盖逻辑处理
                        if existing.workshop_id == workshop_id:
                            return low
                        # 不同 workshop_id → 用派生 key 并存
                        # 先检查派生 key 是否已存在
                        derived = f"{low}::{workshop_id}"
                        if derived not in self._mods:
                            return derived
                        # 极端情况：派生 key 也被占了，再加点后缀
                        counter = 2
                        while f"{low}::{workshop_id}::{counter}" in self._mods:
                            counter += 1
                        return f"{low}::{workshop_id}::{counter}"

                    # 二次补扫也必须与主扫描一致：跳过未订阅的工坊残留目录（读到登记才过滤）
                    subscribed_ids = read_subscribed_workshop_ids(self._game.workshop_dir)
                    for pub_dir in sorted(self._game.workshop_dir.iterdir()):
                        if not pub_dir.is_dir():
                            continue
                        if subscribed_ids is not None and pub_dir.name not in subscribed_ids:
                            continue  # 未订阅残留目录：游戏不会加载，不补扫
                        # 和主扫描保持一致：先扫 ACTIVE manifest（source="workshop"），扫不到再扫 .disabled（source="disabled"）
                        real_info = _scan_folder(pub_dir, source="workshop", workshop_id=pub_dir.name,
                                                 allow_disabled_manifest=False)
                        if real_info is None:
                            real_info = _scan_folder(pub_dir, source="disabled", workshop_id=pub_dir.name,
                                                     allow_disabled_manifest=True)
                        if real_info is not None:
                            store_key = _compute_store_key(real_info.mod_id, pub_dir.name)
                            existing = self._mods.get(store_key)
                            # 派生 key（同 id 不同 ws）→ 总是并存（existing 一定是 None）
                            # 基本 key → 用 override 逻辑判断
                            should_add = (existing is None) or (
                                "::" not in store_key and _workshop_entry_should_override(existing, real_info)
                            )
                            if should_add:
                                self._mods[store_key] = real_info
                                if existing is None:
                                    placeholders_added += 1
                                else:
                                    placeholders_updated += 1
                except Exception as e:
                    _log.warning("refresh: workshop_dir 二次补扫失败, 但主流程不中断: %s", e)

            # ------------------------------------------------------------
            # 关键修复：清理"幽灵占位符"
            # 场景：Steam 取消订阅 workshop mod → workshop/content/<id>/ 被删 → 扫描扫不到 →
            #       settings.save 还留着 → 补占位符（placeholder=True, install_dir=None, source=workshop）
            # 这种 mod 物理上已经不存在了，不应该继续显示。local/disabled 同理。
            # 规则：placeholder=True 且 install_dir=None → 物理不存在 → 从 self._mods 剔除。
            # settings.save 里也不会写回去（下面 save_native_mod_list 只写 present_lower 里的 mod）。
            # ------------------------------------------------------------
            ghost_removed = 0
            to_remove = [k for k, v in self._mods.items() if v.is_placeholder and v.install_dir is None]
            for k in to_remove:
                del self._mods[k]
                ghost_removed += 1
            self._last_ghost_removed = ghost_removed  # 2026-10-15: 让 scan_report 能拿到 deleted 数
            if ghost_removed > 0:
                _log.info("refresh: 清理 %d 个幽灵占位符（物理目录已消失，settings.save 还留着）: %s",
                          ghost_removed, to_remove[:10])

            present_lower = {k.lower() for k in self._mods.keys()}

            def _canonical(ids: list[str]) -> list[str]:
                out: list[str] = []
                seen: set[str] = set()
                for mid in ids:
                    low = mid.lower()
                    if low in present_lower and low not in seen:
                        out.append(self._mods[low].mod_id)
                        seen.add(low)
                return out

            # ------------------------------------------------------------
            # 权威源 = 管理器。
            #   分类：工坊 mod 以 settings.save 的 is_enabled 为真值（管理器不改工坊文件，
            #         物理上恒为 ACTIVE）；本地 mod 以物理位置为准（source=="disabled" 归停用侧）。
            #   顺序：以管理器 profile.json 里记住的顺序为基准合并 ——
            #         保留已有顺序 → 追加本次新发现的 mod（默认排位沿用 settings.save 顺序）
            #         这样「扫描」不会再打乱用户手动拖拽的顺序。
            # ------------------------------------------------------------
            nat_order_hint = _canonical(nat_enabled) + _canonical(nat_disabled)

            # 分类真值：
            #   workshop mod 物理恒 ACTIVE，启停靠 source_states（profile.json 权威）+ settings.save（兼容老数据）
            #   本地 mod 物理位置决定（source=="disabled" 归停用侧）
            nat_disabled_lower = {m.lower() for m in nat_disabled}

            def _is_disabled_side(info) -> bool:
                if info.workshop_id or info.source == "workshop":
                    # 1) 优先查 profile.json 的 _source_states（用户权威——明确写了每个物理副本的启停）
                    ss = self._source_states.get(info.mod_id.lower(), {})
                    ws_key = f"ws_{info.workshop_id}"
                    if ws_key in ss:
                        return ss[ws_key] is False
                    # 2) 再看 settings.save 的 nat_disabled_lower（兼容老数据：用户从没通过管理器停过这个 workshop mod）
                    return info.mod_id.lower() in nat_disabled_lower
                return info.source == "disabled"

            phys_disabled_lower: set[str] = {
                mid.lower() for mid, info in self._mods.items() if _is_disabled_side(info)
            }
            phys_enabled_lower: set[str] = set(present_lower) - phys_disabled_lower

            def _merge_keep(prev: list[str], side_lower: set[str]) -> list[str]:
                out: list[str] = []
                seen: set[str] = set()
                # 2026-11-01 改：profile.json（prev）是用户权威顺序，settings.save 只做新发现的兜底。
                # 2026-10-14 的版本让 settings.save 优先——那是错的！settings.save 里只有 38 条 mod_list，
                # 顺序是游戏自己管理的（不是用户拖的），让它覆盖 profile.json 的 179 条用户顺序 =
                # 用户拖完保存→再扫描，顺序又被 settings.save 里的旧位置覆盖回去。
                # 正确顺序：
                # 1) 先按 profile.json 的 prev（用户拖好的顺序，权威）
                for m in prev:
                    low = m.lower()
                    if low in side_lower and low in present_lower and low not in seen:
                        out.append(self._mods[low].mod_id)
                        seen.add(low)
                # 2) 再追加 settings.save 里有但 profile.json 里没有的（罕见：游戏新加的 mod 还没被管理器看到）
                for m in nat_order_hint:
                    low = m.lower()
                    if low in side_lower and low in present_lower and low not in seen:
                        out.append(self._mods[low].mod_id)
                        seen.add(low)
                # 3) 最后追扫描新发现的（既不在 settings.save 也不在 profile.json 的）
                for mid in self._mods.keys():
                    low = mid.lower()
                    if low in side_lower and low not in seen:
                        out.append(self._mods[low].mod_id)
                        seen.add(low)
                return out

            self._state.enabled_order = _merge_keep(list(self._state.enabled_order), phys_enabled_lower)
            self._state.disabled_order = _merge_keep(list(self._state.disabled_order), phys_disabled_lower)
            _log.info(
                "refresh: 已加载 STS2 原生清单(settings.save→mod_list), 启用=%d, 停用=%d, 路径=%s; 新增占位=%d, 占位被真条目覆盖=%d",
                len(self._state.enabled_order),
                len(self._state.disabled_order),
                self._native_save_path,
                placeholders_added,
                placeholders_updated,
            )
        else:
            _log.info(
                "refresh: 未检测到可用的 STS2 原生清单(settings.save 没找到或没有 mod_settings.mod_list)，"
                "将按 BMM / 物理目录 fallback 逻辑处理"
            )

        # ================================================================
        # BetterModMenu（可选额外同步源，默认关闭，只有用户 prefer_better_mod_menu=True 才启用）
        #   仅当原生清单不可用时才允许 BMM 作为权威初始化来源；
        #   如果已经有原生清单了，这里只加载配置（供双写 BMM 用，不做 enabled_order 权威）
        # ================================================================
        if self._state.prefer_better_mod_menu:
            self._bmm = locate_better_mod_menu()
            if self._bmm is not None:
                self._bmm_settings = load_bmm_settings(self._bmm)
                self._bmm_profiles = load_bmm_profiles(self._bmm)
                _log.info(
                    "refresh: 已启用 BetterModMenu 同步, settings=%d 条, profile=%s, 路径=%s",
                    len(self._bmm_settings),
                    self._bmm_profiles.get("CurrentProfileIndex", "-"),
                    self._bmm.root,
                )
                # 仅当原生清单不可用时，才以 BMM 作为 enabled_order 权威初始化来源；
                # 原生清单已存在时，BMM 只做双写（不抢权威位置）
                if not self.uses_native_mod_list:
                    bmm_enabled_order = bmm_extract_enabled_order_ids(self._bmm_settings)
                    present_lower = {k.lower() for k in self._mods.keys()}
                    canonical_order: list[str] = []
                    seen: set[str] = set()
                    for mid in bmm_enabled_order:
                        low = mid.lower()
                        if low in present_lower and low not in seen:
                            info = self._mods[low]
                            canonical_order.append(info.mod_id)
                            seen.add(low)
                    self._state.enabled_order = canonical_order
                    _log.info("refresh: 以 BMM 为权威初始化 enabled_order(%d 项)", len(self._state.enabled_order))
                else:
                    _log.info("refresh: 原生清单已启用(state.enabled_order=%d 项)，BMM 仅作为额外同步源（不覆盖权威）",
                              len(self._state.enabled_order))
            else:
                self._bmm_settings = []
                self._bmm_profiles = {}
                _log.info("refresh: prefer_better_mod_menu=True，但未找到 BetterModMenu 配置 → 回退到纯游戏源逻辑（stub+搬目录）")
        else:
            # 默认路径：完全不加载 BMM，只使用 STS2 官方目录驱动加载 = 游戏源停用启用逻辑
            self._bmm = None
            self._bmm_settings = []
            self._bmm_profiles = {}
            _log.info("refresh: 已启用纯游戏源逻辑（prefer_better_mod_menu=False），忽略 BetterModMenu，用目录驱动决定启用/停用")

        _log.info(
            "refresh 完成: 共 %d 个mod, 启用侧 %d, 停用侧 %d, errors=%d",
            len(scan.mods),
            len(self._state.enabled_order),
            len(self._state.disabled_order),
            len(scan.errors),
        )
        if scan.errors:
            for e in scan.errors:
                _log.warning("  scan warn: %s", e)
        # --- BMM ⇄ 物理目录 双向对齐（只有 uses_better_mod_menu=True 才执行；默认关闭时完全跳过）---
        if self.uses_better_mod_menu and self._game is not None:
            self._bmm_physical_reconcile()
        # 活动/非活动副本选择逻辑已于 2026-10 删除，默认工坊优先展示（多副本通过 source_states 独立启停）
        self._sync_state_orders()
        # 管理器为唯一权威：把最终顺序写回 settings.save，覆盖游戏自行改写的结果，
        # 保证下一次启动游戏按管理器里的顺序加载（刷新/扫描不再打乱手动排序）。
        # 注意：refresh 阶段只「按管理器顺序重排 mod_list」，**保留每条原有 is_enabled**
        # （不收敛启用集合）——避免扫描不完整（工坊目录/订阅登记暂缺）时把大量工坊 mod
        # 误标 is_enabled=False 并持久化。启用集合的收敛放到启动前的
        # _apply_profile_to_filesystem（应用 profile 的明确意图时才写）。
        written = self._reorder_settings_save_from_state()
        if written:
            _log.info("refresh: 管理器顺序已重排写入 settings.save（保留 is_enabled）: %d 条", written)

        # --- 清理抢占/冲突操作遗留的 .swap-* / .old-* 孤儿目录 ---
        if self._game is not None:
            import shutil as _shutil, re as _re
            _cleaned = 0
            for _root in [self._game.mods_dir, self._game.disabled_dir]:
                if not _root.is_dir():
                    continue
                for _orphan in _root.iterdir():
                    if not _orphan.is_dir():
                        continue
                    if _re.search(r'\.(swap|old|bak)-\d{6,}$', _orphan.name):
                        try:
                            _shutil.rmtree(str(_orphan), ignore_errors=True)
                            _cleaned += 1
                            _log.info("refresh: 清理孤儿目录: %s", _orphan.name)
                        except Exception:
                            pass
            if _cleaned:
                _log.info("refresh: 共清理 %d 个 .swap-/ .old-/ .bak- 孤儿目录", _cleaned)

        # ============================================================ P0 Steam Web API 集成
        # 如果有 workshop mod 且 auto_check_steam=True，拉取在线详情（带磁盘缓存，离线不会阻塞）
        if self._state.auto_check_steam:
            try:
                self._run_steam_check()
            except Exception as e:
                _log.warning("[P0] Steam 检查失败（不影响主流程）: %s", e)

        # ============================================================ 全局依赖检查
        self._warnings = self._check_all_dependencies()
        if self._warnings:
            _log.warning("[depcheck] %d 条依赖警告:", len(self._warnings))
            for w in self._warnings:
                _log.warning("  %s", w)

        # ============================================================ last_activity 兜底
        # 所有 mod 都要有一个 last_activity（老用户迁移 + 新扫描的 mod）
        # 优先用 install_dir 的 mtime（作者更新时间），fallback 用当前时间
        for low, info in self._mods.items():
            if low not in self._state.last_activity:
                fallback = 0
                if info.install_dir and info.install_dir.is_dir():
                    try:
                        fallback = int(info.install_dir.stat().st_mtime)
                    except OSError:
                        pass
                self._state.last_activity[low] = fallback if fallback > 0 else int(time.time())
        # 顺手清理 state 里已不存在 mod 的 last_activity 记录
        stale = [k for k in self._state.last_activity if k not in self._mods]
        for k in stale:
            del self._state.last_activity[k]

        self._invalidate_mod_caches()
        return scan

    # -------------------------------------------------------------- ordering --

    def _invalidate_mod_caches(self) -> None:
        """O2: 任何修改 enabled_order/disabled_order/_mods 的操作后必须调。"""
        self._cache_enabled_list = None
        self._cache_enabled_ids_set = None
        self._cache_disabled_list = None
        self._cache_disabled_ids_set = None

    def ordered_enabled(self) -> list[ModInfo]:
        if self._cache_enabled_list is not None:
            return self._cache_enabled_list
        out: list[ModInfo] = []
        seen: set[str] = set()
        for mid in self._state.enabled_order:
            info = self._mods.get(mid.lower())
            if info and info.mod_id.lower() not in seen:
                out.append(info)
                seen.add(info.mod_id.lower())
        self._cache_enabled_list = out
        self._cache_enabled_ids_set = seen
        return out

    def ordered_disabled(self) -> list[ModInfo]:
        if self._cache_disabled_list is not None:
            return self._cache_disabled_list
        out: list[ModInfo] = []
        seen: set[str] = set()
        for mid in self._state.disabled_order:
            info = self._mods.get(mid.lower())
            if info and info.mod_id.lower() not in seen:
                out.append(info)
                seen.add(info.mod_id.lower())
        self._cache_disabled_list = out
        self._cache_disabled_ids_set = seen
        return out

    # ---- O2: 便捷访问器，避免 _check_all_dependencies 等热点方法自己重建 set ----

    def _enabled_ids_set(self) -> set[str]:
        """O2: 返回 lower mod_id 集合，从 _cache_enabled_ids_set 读取。"""
        # 确保缓存存在（如果没预热过，ordered_enabled 会顺便填好）
        self.ordered_enabled()
        return self._cache_enabled_ids_set  # type: ignore[return-value]

    def _disabled_ids_set(self) -> set[str]:
        self.ordered_disabled()
        return self._cache_disabled_ids_set  # type: ignore[return-value]

    # ------------------------------------------------------ version selection --

    def available_versions(self, mod_id: str) -> list[ModInfo]:
        """返回该 mod 的全部物理副本（工坊/本地/停用），供「切换版本」菜单使用。"""
        return list(self._mod_versions.get(mod_id.lower(), []))

    def active_version(self, mod_id: str) -> Optional[ModInfo]:
        return self._mods.get(mod_id.lower())

    def highest_version(self, mod_id: str) -> Optional[str]:
        """该 mod 全部副本中的最高版本号；没有版本号则返回 None。"""
        best: Optional[str] = None
        for v in self.available_versions(mod_id):
            ver = v.manifest.version
            if ver and (best is None or version_gt(ver, best)):
                best = ver
        return best

    def is_active_highest(self, mod_id: str) -> bool:
        """当前活动版本是否已是最高版本（决定是否显示「最高x.x.x版本」后缀）。"""
        active = self.active_version(mod_id)
        if active is None:
            return True
        hv = self.highest_version(mod_id)
        if hv is None:
            return True
        return not version_gt(hv, active.manifest.version)

    # 活动/非活动副本选择逻辑已删（2026-10）：select_version / _apply_selected_versions
    # 改由 source_states 多 source 独立启停；backend 的 available_versions / active_version
    # / highest_version / is_active_highest 保留（show_all_versions 预览面板等功能依赖）

    def _bmm_physical_reconcile(self) -> None:
        """BMM ⇄ 物理目录 双向对齐：只在 uses_better_mod_menu + game 存在时调用。

        背景：BMM 的 Enabled=true/false 只是 BMM 游戏内菜单的 UI 状态，STS2 官方是
        「目录驱动加载」—— 只要 Mods/ 或 workshop/content/<AppID>/<id>/
        里能扫到 manifest.json，就必然加载这个 mod（完全无视 BMM Enabled 设置）。
        所以这里必须把 BMM 的勾选状态「落到物理位置上」，才能保证真生效；
        同时用户手工搬了目录/用 fallback 模式搬过 → 也反向写回 BMM，
        保证下次进游戏打开 BetterModMenu 菜单，勾选状态跟管理器一致。

        同步优先级（按行执行，每行只处理需要对齐的那部分）：
        1) 先扫描：BMM Enabled=true  + 物理上确实在 .mods_disabled/ → 搬回 Mods/（真启用）
        2) 再扫描：BMM Enabled=false + 物理上在 Mods/（或 workshop 源目录）→ 搬去 disabled/种 stub 真停用
        3) 最后扫描：BMM 没记录，但物理位置有状态 → 写回 BMM（物理 local/workshop→Enabled=true，物理 disabled→Enabled=false）
        """
        if not self.uses_better_mod_menu or self._game is None:
            return
        from .scanner import (
            move_mod_between,
            ensure_workshop_stub_enabled,
            ensure_workshop_stub_disabled,
        )

        # 构造 BMM 分类（lower → True/False/None(没记录)）
        bmm_enabled_map: dict[str, bool] = {}
        for entry in self._bmm_settings:
            mid = entry.get("ModId")
            if not isinstance(mid, str) or not mid:
                continue
            low = mid.lower()
            if low not in self._mods:
                continue
            bmm_enabled_map[low] = bool(entry.get("Enabled"))

        dirty_bmm = False
        processed: set[str] = set()

        # ---- 1) BMM=true 但在 disabled → 搬回启用侧（真启用）----
        for info in list(self._mods.values()):
            low = info.mod_id.lower()
            if low in processed:
                continue
            bmm_wants_enable = bmm_enabled_map.get(low, None)
            if bmm_wants_enable is not True:
                continue
            is_ws = bool(info.workshop_id)
            if is_ws:
                # Workshop 类：启用 = 在 Mods/ 种 stub
                ok_in_mods = False
                try:
                    info_path = Path(info.install_dir).resolve()
                    info_path.relative_to(self._game.mods_dir.resolve())
                    ok_in_mods = True
                except Exception:
                    ok_in_mods = False
                if not ok_in_mods:
                    _log.info("[bmm_reconcile] BMM 要求启用 Workshop mod %s，install_dir=%s 不在 Mods/ → 补种 stub", info.mod_id, info.install_dir)
                    try:
                        new_info = ensure_workshop_stub_enabled(info, self._game)
                        self._mods[low] = new_info
                    except Exception as e:
                        _log.error("[bmm_reconcile] 1) 补种 stub 失败: mod=%s, err=%s", info.mod_id, e)
                else:
                    _log.debug("[bmm_reconcile] 1) Workshop mod %s 已在 Mods/，跳过", info.mod_id)
            else:
                # Local 类：物理在 .mods_disabled/ 才搬回
                if info.source == "disabled":
                    _log.info("[bmm_reconcile] BMM 要求启用 Local mod %s，当前在 disabled 目录 → 搬回 Mods/", info.mod_id)
                    try:
                        new_info = move_mod_between(info, self._game.mods_dir, target_source="local")
                        self._mods[low] = new_info
                    except Exception as e:
                        _log.error("[bmm_reconcile] 1) 搬回 Local mod 失败: mod=%s, err=%s", info.mod_id, e)
            processed.add(low)

        processed.clear()

        # ---- 2) BMM=false 但物理上仍在启用侧可见 → 真停用（搬去 disabled / stub 搬去 disabled）----
        for info in list(self._mods.values()):
            low = info.mod_id.lower()
            if low in processed:
                continue
            bmm_wants_disabled = bmm_enabled_map.get(low, None)
            if bmm_wants_disabled is not False:
                continue
            is_ws = bool(info.workshop_id)
            if is_ws:
                # Workshop 类：停用 = 把 Mods/ 下的 stub 整个搬到 .mods_disabled/
                # 先检查 stub 在 Mods/ 里吗？在 → 需要搬；不在 → 已经在 disabled/ 或仅在 workshop 源（源 manifest 不可识别化兜底）
                in_mods = False
                try:
                    info_path = Path(info.install_dir).resolve()
                    info_path.relative_to(self._game.mods_dir.resolve())
                    in_mods = True
                except Exception:
                    in_mods = False
                if in_mods:
                    _log.info("[bmm_reconcile] BMM 要求停用 Workshop mod %s，install_dir=%s 仍在 Mods/ → stub 搬去 .mods_disabled/", info.mod_id, info.install_dir)
                    try:
                        new_info = ensure_workshop_stub_disabled(info, self._game)
                        self._mods[low] = new_info
                    except Exception as e:
                        _log.error("[bmm_reconcile] 2) 搬 stub 去 disabled 失败: mod=%s, err=%s", info.mod_id, e)
                else:
                    # 即便 stub 已经在 disabled 了，也兜底 ensure_workshop_stub_disabled（保证源 manifest 不可识别化）
                    _log.debug("[bmm_reconcile] 2) Workshop mod %s install_dir=%s 不在 Mods/，兜底 ensure_workshop_stub_disabled（保证源 manifest 不可识别化）", info.mod_id, info.install_dir)
                    try:
                        new_info = ensure_workshop_stub_disabled(info, self._game)
                        self._mods[low] = new_info
                    except Exception as e:
                        _log.error("[bmm_reconcile] 2) 兜底 ensure_workshop_stub_disabled 失败: mod=%s, err=%s", info.mod_id, e)
            else:
                # Local 类：物理在 local/Mods/ 下 → 搬去 .mods_disabled/
                if info.source == "local":
                    _log.info("[bmm_reconcile] BMM 要求停用 Local mod %s，当前在 Mods/ → 搬去 .mods_disabled/", info.mod_id)
                    try:
                        new_info = move_mod_between(info, self._game.disabled_dir, target_source="disabled")
                        self._mods[low] = new_info
                    except Exception as e:
                        _log.error("[bmm_reconcile] 2) 搬 Local mod 去 disabled 失败: mod=%s, err=%s", info.mod_id, e)
            processed.add(low)

        processed.clear()

        # ---- 3) BMM 没记录（用户手工装的 mod 还没进 BMM settings）→ 按物理位置写回 BMM ----
        for info in list(self._mods.values()):
            low = info.mod_id.lower()
            if low in processed:
                continue
            if low in bmm_enabled_map:
                continue  # BMM 有记录，1)/2) 已处理
            phys_enable = info.source != "disabled"
            workshop_url: Optional[str] = None
            if info.workshop_id:
                workshop_url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={info.workshop_id}"
            self._bmm_settings = bmm_settings_set_enabled(
                self._bmm_settings,
                info.mod_id,
                phys_enable,
                workshop_url=workshop_url,
            )
            dirty_bmm = True
            _log.info("[bmm_reconcile] 3) mod=%s BMM 未记录 → 按物理位置(source=%s)写回 Enabled=%s",
                      info.mod_id, info.source, phys_enable)
            processed.add(low)

        if dirty_bmm:
            try:
                save_bmm_settings(self._bmm, self._bmm_settings)
            except Exception as e:
                _log.error("[bmm_reconcile] 3) 写回 BMM mod_settings.json 失败: %s", e)

        _log.info("[bmm_reconcile] 双向对齐完成（BMM ⇄ 物理目录），dirty_bmm=%s", dirty_bmm)

    def _sync_state_orders(self) -> None:
        """把扫描到但不在 order 列表里的 mod 补到末尾，从 order 里删掉不存在的 id。

        启用/停用 的「分类依据」优先级（从高到低）：
          1) 原生清单（uses_native_mod_list=True）：
             按 settings.save 里 mod_list[i]["is_enabled"] 分；
             原生清单里没记录的 mod → **按物理位置分**（local/workshop=启用，disabled=停用），
             保证用户刚订阅、LOM 还没来得及写入的 mod 默认启用，不会被误分类到停用侧。

          2) BetterModMenu（uses_better_mod_menu=True 且原生清单不可用）：
             - 若 BMM settings 里明确写了 Enabled=true/false → 优先按 BMM 分；
             - 若 BMM 里没写过这个 mod → 按物理位置分：
                 source=local/workshop → 启用侧（满足「Mods 下有 = 默认启用」）
                 source=disabled      → 停用侧

          3) Fallback（原生清单不可用 + 未启用 BMM）：
             启用态以目录位置为准（source==disabled → 停用侧，否则 → 启用侧）
        """
        present_ids: set[str] = set()
        for info in self._mods.values():
            present_ids.add(info.mod_id.lower())

        # 清理已删除 mod 的角色识别记录（避免脏 id 累积）
        if any(mid not in present_ids for mid in self._state.character_mods):
            self._state.character_mods = [mid for mid in self._state.character_mods if mid in present_ids]

        # 先把 order 里丢了的清掉
        enabled = [m.lower() for m in self._state.enabled_order if m.lower() in present_ids]
        disabled = [m.lower() for m in self._state.disabled_order if m.lower() in present_ids]
        enabled_set = set(enabled)
        disabled_set = set(disabled)

        # --- 构建「启用/停用 分类 map」(key=lower_id, value="enabled"/"disabled") ---
        classification: dict[str, str] = {}
        if self.uses_native_mod_list:
            # 1) 先按原生清单 is_enabled 字段分类（有明确记录的先记下）
            nat_class: dict[str, str] = {}
            ms = self._native_full_data["mod_settings"]  # type: ignore[index]
            for entry in ms.get("mod_list", []):  # type: ignore[union-attr]
                if not isinstance(entry, dict):
                    continue
                mid = entry.get("id")
                if not isinstance(mid, str) or not mid:
                    continue
                low = mid.lower()
                if low not in present_ids:
                    continue
                nat_class[low] = "enabled" if bool(entry.get("is_enabled")) else "disabled"
            # 2) 分类真值：
            #    workshop mod：先看 _source_states（profile.json 用户权威）→ 再看 settings.save → 默认启用
            #    本地 mod：物理位置为准（Mods/=启用，.mods_disabled/=停用）
            for info in self._mods.values():
                low = info.mod_id.lower()
                is_ws_mod = bool(info.workshop_id) or info.source == "workshop"
                if is_ws_mod:
                    # 2a) profile.json 的 _source_states 优先（用户在管理器里明确停过这个物理副本）
                    ss = self._source_states.get(low, {})
                    ws_key = f"ws_{info.workshop_id}"
                    if ws_key in ss:
                        classification[low] = "disabled" if ss[ws_key] is False else "enabled"
                        continue
                    # 2b) 再看 settings.save 里的 is_enabled（兼容老数据）
                    classification[low] = nat_class.get(low, "enabled")
                else:
                    classification[low] = "disabled" if info.source == "disabled" else "enabled"
        elif self.uses_better_mod_menu:
            # 1) 先按 BMM settings 的 Enabled 字段决定（有明确记录才覆盖）
            bmm_class: dict[str, str] = {}
            for entry in self._bmm_settings:
                mid = entry.get("ModId")
                if not isinstance(mid, str) or not mid:
                    continue
                low = mid.lower()
                if low not in present_ids:
                    continue
                if bool(entry.get("Enabled")):
                    bmm_class[low] = "enabled"
                else:
                    bmm_class[low] = "disabled"
            # 2) 再按物理位置（local/workshop → 启用侧，disabled → 停用侧）
            for info in self._mods.values():
                low = info.mod_id.lower()
                phys = "disabled" if info.source == "disabled" else "enabled"
                classification[low] = bmm_class.get(low, phys)
        else:
            # fallback 旧行为：看 source 字段 + workshop mod 真实物理启用/停用态
            # 为什么还要额外看物理？因为 source='workshop' 只代表它「来自 workshop_dir」，
            # 但 STS2 默认目录驱动是否真加载它，取决于 manifest 正常（.json 存在）
            # 还是已被我们加了 .json.disabled（真停用态）。_scan_folder 里已经按
            # allow_disabled_manifest 区分 source='workshop' / 'disabled'，所以在这
            # 里直接信任 source 字段是正确的——扫描阶段已经正确写入：
            #   - workshop_dir 下 manifest.json 正常 → source=workshop（真启用）
            #   - workshop_dir 下只有 .json.disabled → source=disabled（真停用）
            #   - Mods/ 下 = source=local（真启用）
            #   - .mods_disabled/ 下 = source=disabled（真停用）
            # 不过我们还要做二次保险：如果 info.source=workshop 但实际 manifest 已经
            # 被停用（或反过来），这里用 find_manifest 再确认一下，避免 LOM 没跑而
            # 扫描阶段的 source 字段滞后。
            from .scanner import find_manifest

            for info in self._mods.values():
                low = info.mod_id.lower()
                if info.install_dir is None:
                    # 占位 mod：按原生清单或默认启用——这里 fallback 没有原生清单，
                    # 统一默认把占位放启用侧（如果 manifest 真不存在，游戏也不会加载）
                    classification[low] = "enabled"
                    continue
                # 判断物理层真启用：manifest 正常(.json 可解析，不允许 .disabled)
                phys_enabled = False
                try:
                    phys_enabled = find_manifest(info.install_dir, allow_disabled=False) is not None
                except Exception:
                    phys_enabled = False
                # source='disabled' 或物理上 manifest 被停用 → 停用侧
                if info.source == "disabled" or not phys_enabled:
                    classification[low] = "disabled"
                else:
                    classification[low] = "enabled"

        # =============== 核心：重新同步所有 mod 的「侧归属」 ===============
        # 策略：
        #   1) 按 classification 为准绳，绝不因为旧 order（profile.json 里遗留的 settings.save
        #      脏数据 / 上一次 LOM 跑过留下的 is_enabled=false）把 mod 锁在错误侧。
        #   2) 在分类正确的前提下，尽量保留「原 enabled/disabled 数组的相对顺序」——用户之前
        #      拖过排序，只要侧没变，就不打乱；真的需要迁移的，放到目标侧末尾。
        #   3) 完全没在 order 里出现过的（新订阅 mod / 占位符），按分类追加到对应末尾。
        # ==================================================================
        new_enabled: list[str] = []
        new_disabled: list[str] = []
        migrate_to_enabled: list[str] = []   # 从 disabled 侧迁移到 enabled 的
        migrate_to_disabled: list[str] = []  # 从 enabled 侧迁移到 disabled 的

        for k in enabled:
            expected = classification.get(k, "enabled")
            if expected == "enabled":
                new_enabled.append(k)
            else:
                migrate_to_disabled.append(k)

        for k in disabled:
            expected = classification.get(k, "enabled")
            if expected == "disabled":
                new_disabled.append(k)
            else:
                migrate_to_enabled.append(k)

        # 迁移的放目标侧末尾（避免打乱目标侧原有相对顺序）
        new_enabled.extend(migrate_to_enabled)
        new_disabled.extend(migrate_to_disabled)

        # 最后补漏：扫描到的 mod，完全没在旧 order 里出现过 → 按分类追加到末尾
        seen_in_new: set[str] = set(new_enabled) | set(new_disabled)
        for info in sorted(self._mods.values(), key=lambda x: x.display_name.lower()):
            k = info.mod_id.lower()
            if k in seen_in_new:
                continue
            side = classification.get(k, "enabled")
            if side == "enabled":
                new_enabled.append(k)
            else:
                new_disabled.append(k)

        enabled = new_enabled
        disabled = new_disabled

        # 更新为真实 mod_id（保留大小写，从 _mods 里拿）
        def _restore_cases(lower_list: list[str]) -> list[str]:
            result: list[str] = []
            for lower in lower_list:
                info = self._mods.get(lower)
                if info:
                    result.append(info.mod_id)
            return result

        self._state.enabled_order = _restore_cases(enabled)
        self._state.disabled_order = _restore_cases(disabled)
        self._invalidate_mod_caches()
        self.save_state()

    # -------------------------------------------------------------- mutations -

    def _preempt_existing_local(self, info: ModInfo) -> tuple[Optional[ModInfo], ModInfo]:
        """在把一个 disabled 本地副本搬去 Mods/ 之前，抢占已有的占用者。

        返回 (被抢占的副本(或 None), 更新后的 info)。
        - 被抢占的 occupant：已搬到 mods_disabled/ + source_states 标 False + _mod_versions 已更新
        - 更新后的 info：如果是原子交换 → 已经在 Mods/ + source=local；否则不变
        """
        import os as _os
        import time as _time

        mods_dir = self._game.mods_dir
        disabled_dir = self._game.disabled_dir
        target_mods = mods_dir / info.folder_name
        if not target_mods.exists():
            return None, info

        mid_lower = info.mod_id.lower()
        occupant: Optional[ModInfo] = None
        for v in self._mod_versions.get(mid_lower, []):
            if v.source == "local" and str(v.install_dir) == str(target_mods):
                occupant = v
                break

        if occupant is None:
            _log.warning("[set_enabled] 抢占检测: Mods/%s 已存在但不是 mod_id=%s 的本地副本 → 跳过抢占",
                         info.folder_name, info.mod_id)
            return None, info

        _orig_occupant_dir = str(occupant.install_dir)
        # 2026-11-01 多副本修复：workshop 副本用 ws_{workshop_id} 唯一 key
        _orig_occupant_state_key = f"ws_{occupant.workshop_id}" if occupant.workshop_id else occupant.source

        _log.info("[set_enabled] 抢占检测: Mods/%s 被同 mod_id 的另一份副本占着（source=%s）",
                  info.folder_name, occupant.source)

        info_in_disabled_target = (info.source == "disabled"
                                   and str(info.install_dir) == str(disabled_dir / info.folder_name))

        occupant_new: Optional[ModInfo] = None
        new_info: ModInfo = info

        try:
            if info_in_disabled_target:
                # === 三步原子交换 ===
                backup_dir = disabled_dir / f"{info.folder_name}.swap-{int(_time.time())}"
                _os.replace(str(info.install_dir), str(backup_dir))
                _log.info("[set_enabled] 抢占交换 step1: 暂存 info 到 %s", backup_dir)
                _os.replace(str(target_mods), str(info.install_dir))
                _log.info("[set_enabled] 抢占交换 step2: occupant 搬去 %s", info.install_dir)
                _os.replace(str(backup_dir), str(target_mods))
                _log.info("[set_enabled] 抢占交换 step3: info 搬去 %s", target_mods)

                occupant_new = ModInfo(
                    manifest=occupant.manifest,
                    folder_name=occupant.folder_name,
                    install_dir=disabled_dir / occupant.folder_name,
                    source="disabled",
                    workshop_id=occupant.workshop_id,
                )
                new_info = ModInfo(
                    manifest=info.manifest,
                    folder_name=info.folder_name,
                    install_dir=target_mods,
                    source="local",
                    workshop_id=info.workshop_id,
                )
            else:
                occupant_new = move_mod_between(occupant, disabled_dir, target_source="disabled")

        except Exception as e:
            _log.error("[set_enabled] 抢占时搬旧副本失败: %s", e)
            raise

        # 更新 _mod_versions 里的 occupant 条目
        for i, v in enumerate(self._mod_versions.get(mid_lower, [])):
            if str(v.install_dir) == _orig_occupant_dir:
                self._mod_versions[mid_lower][i] = occupant_new
                break

        cur_main = self._mods.get(mid_lower)
        if cur_main and str(cur_main.install_dir) == _orig_occupant_dir:
            self._mods[mid_lower] = occupant_new

        # occupant 的 source_states = False
        self._source_states.setdefault(mid_lower, {})[_orig_occupant_state_key] = False
        self._source_last_toggled.setdefault(mid_lower, {})[_orig_occupant_state_key] = _time.time()

        _log.info("[set_enabled] 抢占完成: occupant→%s(disabled), info→%s(local)",
                  occupant_new.install_dir, new_info.install_dir)

        return occupant_new, new_info

    def _find(self, mod_id: str, source: Optional[str] = None) -> Optional[ModInfo]:
        """找 mod（向后兼容：返回 canonical 或按 source 找第一条）。

        2026-10 注：推荐用 _find_by_key(version_key) 精确定位物理副本，
        因为 norm_mod_source 会把 local 和 disabled 都映射到 mods_directory，
        用 source 参数无法区分同一个 mod_id 下 Mods/ vs .mods_disabled/ 的两份。
        """
        low = mod_id.lower()
        if source is None:
            return self._mods.get(low)
        target_enum = norm_mod_source(source)
        for v in self._mod_versions.get(low, []):
            if norm_mod_source(v.source) == target_enum:
                return v
        return self._mods.get(low)

    def _find_by_key(self, version_key: str) -> Optional[ModInfo]:
        """按 mod_version_key (= install_dir 字符串) 精确找物理副本。

        这是多副本独立启停的唯一精确定位方式——source="local" 和 source="disabled"
        的 norm_mod_source 结果都是 "mods_directory"，无法靠 source 区分。
        """
        for versions in self._mod_versions.values():
            for v in versions:
                if mod_version_key(v) == version_key:
                    return v
        return None

    def _touch_activity(self, mod_id: str) -> None:
        """更新 mod 的最近活动时间为当前时间（秒级 Unix ts）。
        用于停用列表按"最近操作"排序。"""
        self._state.last_activity[mod_id.lower()] = int(time.time())

    def _ensure_activity(self, mod_id: str, fallback_ts: int = 0) -> None:
        """如果 mod 还没 last_activity，设一个默认值。
        迁移老用户 + 扫描新 mod 时用，避免大量 mod 挤在一起。"""
        low = mod_id.lower()
        if low not in self._state.last_activity:
            self._state.last_activity[low] = fallback_ts if fallback_ts > 0 else int(time.time())

    @_serialized
    def delete_mod(self, mod_id: str, version_key: Optional[str] = None) -> bool:
        """删除物理文件夹。

        version_key: 可选，精确指定要删除的物理副本（= install_dir 路径）。
          如果不传，默认删 canonical install_dir 指向的那个副本（可能删错，
          因为 canonical 只记录"主"副本的路径，用户选中的可能是另一个副本）。
        删除前自动停用；删除后调用方应重新 refresh。
        """
        low = mod_id.lower()
        info = self._find(mod_id)
        if info is None or info.install_dir is None:
            return False

        # ----- 确定要删哪个物理目录 -----
        target_dir = None
        if version_key:
            # 从 _mod_versions 里找匹配的副本
            for v in self._mod_versions.get(low, []):
                if v.install_dir and str(v.install_dir) == version_key:
                    target_dir = v.install_dir
                    break
            if target_dir is None:
                _log.warning("[delete_mod] version_key 没匹配到任何副本: %s", version_key)
                return False
        else:
            # 兜底：删 canonical 指向的目录
            target_dir = info.install_dir

        # C2: 启用中 → 先停用（处理 manifest 后缀 / 目录搬移）
        if low in {m.lower() for m in self._state.enabled_order}:
            try:
                self.set_enabled(mod_id, False, version_key=str(target_dir), auto_save=False)
            except Exception as e:
                _log.warning("[delete_mod] 停用失败，尝试继续删除: %s", e)

        if not target_dir.exists():
            _log.warning("[delete_mod] 目录不存在: %s", target_dir)
            return False
        shutil.rmtree(str(target_dir))

        # ----- 从内存移除 -----
        # 如果这是最后一个版本 → 清理 canonical
        remaining = [v for v in self._mod_versions.get(low, [])
                     if v.install_dir and Path(str(v.install_dir)) != target_dir]
        if not remaining:
            self._mods.pop(low, None)
            self._mod_versions.pop(low, None)
            self._state.enabled_order = [m for m in self._state.enabled_order if m.lower() != low]
            self._state.disabled_order = [m for m in self._state.disabled_order if m.lower() != low]
            self._source_states.pop(low, None)
            self._source_last_toggled.pop(low, None)
        else:
            # 还有其他副本 → 更新 canonical install_dir 指向剩余第一个版本
            self._mod_versions[low] = remaining
            info.install_dir = remaining[0].install_dir
            info.source = remaining[0].source
            # 从 source_states 里删除对应 source
            v_enum = norm_mod_source(
                next((v.source for v in self._mod_versions.get(low, [])
                      if Path(str(v.install_dir)) == target_dir), '')
            )
            srcs = self._source_states.get(low, {})
            srcs.pop(v_enum, None)

        self._invalidate_mod_caches()
        self.save_state()
        _log.info("[delete_mod] 已删除 mod=%s 目录=%s (剩余版本=%d)",
                  info.mod_id, target_dir, len(remaining))
        return True

    # ---------------------------------------------------------------------
    # 游戏原生排序：Kahn 拓扑 + PriorityQueue 平级（1:1 复刻 ModManager.SortModList）
    # ---------------------------------------------------------------------
    def _game_native_sort(self, canonical_list: list[str]) -> list[str]:
        """游戏原生排序算法 —— 完全对应 ModManager.SortModList。

        1. Kahn 拓扑排序：依赖链保证前置（被依赖）在左
        2. 平级（无依赖关系）用 PriorityQueue：
           key = settings.save 里的 canonical 位置（尊重用户手动排序）
           如果 settings.save 里没有 → 用 canonical_list 当前位置兜底
        3. 循环依赖：保持原始顺序兜底（游戏也有这个 fallback）
        """
        import heapq
        from collections import defaultdict

        all_ids = {c.lower() for c in canonical_list}

        # --- 1. 构建每个 mod 的依赖集（只保留在 all_ids 里的有效依赖）---
        deps_of: dict[str, set[str]] = {}
        for c in canonical_list:
            low = c.lower()
            info = self._mods.get(low)
            if info is None:
                deps_of[low] = set()
                continue
            raw_deps = info.manifest.dependencies or []
            dep_set: set[str] = set()
            for d in raw_deps:
                d_l = str(d).lower().split('#')[0]
                if d_l in all_ids and d_l != low:
                    dep_set.add(d_l)
            deps_of[low] = dep_set

        # --- 2. in-degree + dependents 反向边 ---
        in_deg: dict[str, int] = {mid: len(deps) for mid, deps in deps_of.items()}
        dependents: dict[str, set[str]] = defaultdict(set)
        for mid, dep_set in deps_of.items():
            for d in dep_set:
                dependents[d].add(mid)

        # --- 3. PriorityQueue：种子队列（in-degree 0）---
        # key = settings.save 里的 canonical 位置（尊重用户意图）
        # 没有就 fallback 到 canonical_list 当前位置
        native_order = getattr(self, '_native_modlist_canon_order', [])
        native_idx = {m.lower(): i for i, m in enumerate(native_order)}
        orig_idx = {c.lower(): i for i, c in enumerate(canonical_list)}

        pq: list[tuple[int, str]] = []
        for mid, deg in in_deg.items():
            if deg == 0:
                # 优先用 settings.save 位置，fallback 到当前位置
                prio = native_idx.get(mid)
                if prio is None:
                    prio = orig_idx.get(mid, 999999)
                heapq.heappush(pq, (prio, mid))

        # --- 4. Kahn 主循环 ---
        sorted_ids: list[str] = []
        while pq:
            _, mid = heapq.heappop(pq)
            sorted_ids.append(mid)
            for child in dependents.get(mid, set()):
                in_deg[child] -= 1
                if in_deg[child] == 0:
                    prio = native_idx.get(child)
                    if prio is None:
                        prio = orig_idx.get(child, 999999)
                    heapq.heappush(pq, (prio, child))

        # --- 5. 循环依赖兜底：保持原始顺序 ---
        remaining = all_ids - set(sorted_ids)
        if remaining:
            for c in canonical_list:
                if c.lower() in remaining:
                    sorted_ids.append(c.lower())

        # --- 6. 转回原始大小写 ---
        lower_to_orig = {c.lower(): c for c in canonical_list}
        return [lower_to_orig[m] for m in sorted_ids]

    def _apply_game_sort_to_enabled(self) -> None:
        """对 _state.enabled_order 做游戏原生排序（set_enabled / reorder_enabled 后调）。"""
        self._state.enabled_order = self._game_native_sort(list(self._state.enabled_order))
        self._invalidate_mod_caches()

    @_serialized
    def reorder_enabled(self, new_order_ids: Iterable[str], auto_save: bool = False) -> None:
        """接受新的启用顺序。丢失的 id 自动 append；停用侧不动。

        落盘优先级（从高到低）：
          1) 原生清单（uses_native_mod_list=True）：重排 settings.save 里的 mod_list 顺序，
             然后 save_native_mod_list 原子写 settings.save（仅改 mod_settings，其他字段完全不动）；
             最后以原生清单为权威反填 state.enabled_order。
          2) BetterModMenu（uses_better_mod_menu=True 且原生清单不可用）：
             重排 BMM mod_settings.json 的顺序；以 BMM 为权威反填 state.enabled_order。
          3) Fallback：只改内存 state.enabled_order，落盘 manager.state.json。
        """
        present_ids_lower = {m.lower() for m in self._mods.keys()}
        # 把传进来的新顺序按 lower id 处理，同时保留原始大小写（用于展示）
        new_order_lower: list[str] = []
        for m in new_order_ids:
            low = m.lower()
            if low in present_ids_lower and low not in new_order_lower:
                new_order_lower.append(low)

        current_enabled_lower = {m.lower(): m for m in self._state.enabled_order}
        current_enabled_set_lower = set(current_enabled_lower.keys())
        # 不在新列表里、但本来在 enabled 侧的，放到末尾（避免误删）——按原顺序追加
        extras_lower = [low for low in current_enabled_set_lower if low not in new_order_lower]
        # extras_lower 保持 enabled_order 原来的顺序，不按 set 随机
        extras_lower = [m.lower() for m in self._state.enabled_order if m.lower() not in new_order_lower]

        # 还原为带大小写的 mod_id：优先以扫描到的 ModInfo.mod_id 为标准（最贴近磁盘）；
        # 如果该 lower id 在 enabled_order 里有就沿用其大小写；都没有就跳过
        def _canonical_from_lower(low: str) -> Optional[str]:
            info = self._mods.get(low)
            if info is not None:
                return info.mod_id
            return current_enabled_lower.get(low)

        result: list[str] = []
        seen_lower: set[str] = set()
        for low in new_order_lower:
            canonical = _canonical_from_lower(low)
            if canonical is None or low in seen_lower:
                continue
            result.append(canonical)
            seen_lower.add(low)
        for low in extras_lower:
            if low in seen_lower:
                continue
            canonical = _canonical_from_lower(low)
            if canonical is None:
                continue
            result.append(canonical)
            seen_lower.add(low)
        self._state.enabled_order = result

        # ----- 1) 原生清单：仅在 auto_save=True 时写 settings.save -----
        # 手动保存模式（auto_save=False）：只改内存 + _mark_dirty()，等 save_all 统一落盘
        if auto_save and self.uses_native_mod_list:
            self._native_full_data = nat_mod_list_reorder(
                self._native_full_data,  # type: ignore[arg-type]
                self._state.enabled_order,
            )
            try:
                save_native_mod_list(
                    self._native_save_path,  # type: ignore[arg-type]
                    self._native_full_data,  # type: ignore[arg-type]
                )
            except Exception as e:
                _log.error("[reorder_enabled] 写入 STS2 原生清单(settings.save) 失败: %s", e)
                raise
            # 重新以原生清单为权威：提取启用顺序作为 state.enabled_order 最终值
            nat_enabled = nat_extract_enabled_order_ids(self._native_full_data)  # type: ignore[arg-type]
            plower = {m.lower() for m in self._mods.keys()}
            canonical: list[str] = []
            seen2: set[str] = set()
            for mid in nat_enabled:
                low = mid.lower()
                if low in plower and low not in seen2:
                    canonical.append(self._mods[low].mod_id)
                    seen2.add(low)
            self._state.enabled_order = canonical
            _log.info("[reorder_enabled] 原生清单模式：同步 settings.save mod_list 顺序，enabled_order=%d 项", len(canonical))

        # ----- 2) BetterModMenu 模式：仅在 auto_save=True 时写 settings -----
        if auto_save and self.uses_better_mod_menu:
            self._bmm_settings = bmm_settings_reorder(self._bmm_settings, self._state.enabled_order)
            try:
                save_bmm_settings(self._bmm, self._bmm_settings)
            except Exception as e:
                _log.error("[reorder_enabled] 写入 BMM settings 失败: %s", e)
                raise
            # 如果原生清单没启用，才让 BMM 做权威反填；原生清单启用时 BMM 只是额外同步，不抢权威
            if not self.uses_native_mod_list:
                bmm_enabled = bmm_extract_enabled_order_ids(self._bmm_settings)
                plower = {m.lower() for m in self._mods.keys()}
                canonical_bmm: list[str] = []
                seen3: set[str] = set()
                for mid in bmm_enabled:
                    low = mid.lower()
                    if low in plower and low not in seen3:
                        canonical_bmm.append(self._mods[low].mod_id)
                        seen3.add(low)
                self._state.enabled_order = canonical_bmm
                _log.info("[reorder_enabled] BMM 模式：同步 enabled_order=%d 项", len(canonical_bmm))

        self._invalidate_mod_caches()
        # 手动保存模式：拖拽排序不自动写 profile.json，等 save_all 统一存
        self._mark_dirty()

    @_serialized
    def set_enabled(
        self, mod_id: str, enable: bool,
        source: Optional[str] = None, version_key: Optional[str] = None,
        auto_save: bool = True,
    ) -> bool:
        """启用/停用 mod。

        2026-10 核心改动：整条链路改用 version_key 精确定位物理副本。
        version_key = mod_version_key(info) = str(info.install_dir)，每个物理副本唯一。
        因为 norm_mod_source("local") 和 norm_mod_source("disabled") 都是 "mods_directory"，
        source 参数无法区分同一个 mod_id 下 Mods/ vs .mods_disabled/ 的两份本地副本。

        - version_key 有值 → _find_by_key 精确找物理副本（新路径，多副本独立启停）
        - version_key 无值 → 回退 _find(mod_id, source)（向后兼容）

        工坊副本：不动文件，改 settings.save 里 (id, "steam_workshop") 的 is_enabled
        本地副本：搬目录 Mods/ <-> .mods_disabled/（物理位置即启停状态）

        auto_save=False 时只改内存状态，不写 settings.save/BMM settings，
        由调用方（api.save_all）统一持久化。
        """
        if version_key:
            info = self._find_by_key(version_key)
        else:
            info = self._find(mod_id, source)
        if info is None or self._game is None:
            _log.warning("[set_enabled] mod_id=%s version_key=%s 找不到或 GameInstall 为空, enable=%s → 忽略",
                         mod_id, version_key, enable)
            return False

        # P1: Pinned Mod 保护 —— 如果 enabled 被锁定，拒绝停用/启用操作
        pinned = self._state.pinned.get(info.mod_id.lower())
        if pinned and "enabled" in pinned:
            target = bool(pinned["enabled"])
            if enable != target:
                _log.warning("[set_enabled] %s 被 pinned 锁定（enabled=%s），拒绝切换到 enable=%s", info.mod_id, target, enable)
                return False

        is_workshop_related = bool(info.workshop_id)

        # 2026-10-15：state_key 必须在物理层之前算好！
        # 物理层搬目录会改 info.source（"local" ↔ "disabled"），但我们需要的是
        # "这个物理副本原本是什么 source"——搬目录之前的原始值
        if info.workshop_id:
            # 2026-11-01 多副本修复：同 mod_id 可能有多个 workshop 副本（如圣主历险记原版 vs 修复版）
            # 必须用 ws_{workshop_id} 做唯一 key，不能都堆在 "steam_workshop" 下互相覆盖
            _orig_state_key = f"ws_{info.workshop_id}"
        else:
            _orig_state_key = info.source  # "local" 或 "disabled"
        _orig_install_dir = str(info.install_dir)  # 物理层可能搬目录，保存原始路径用于匹配 _mod_versions

        _log.info(
            "[set_enabled] mod=%s (id=%s) → enable=%s, source=%s(目标), info.source=%s(实际), workshop_id=%s, dir=%s",
            info.display_name, info.mod_id, enable, source, info.source, info.workshop_id, info.install_dir,
        )

        # =====================================================================
        # 物理层（2026-10-16 修正：仿制游戏原生逻辑）
        #   Workshop mod: 永远不改 manifest！游戏原生只用 settings.save is_enabled 表达启停。
        #                 manifest 保持 .json 不动，游戏能扫到 → 显示在列表里 → is_enabled 控制是否加载。
        #   Local mod:    搬目录 Mods/ <-> .mods_disabled/（本地 mod 保留物理方式）
        # =====================================================================
        phys_ok = True  # workshop mod 无物理操作，直接标记 OK
        if is_workshop_related:
            _log.info("[set_enabled] 物理层(workshop): %s — 不动 manifest，只改 settings.save is_enabled", info.mod_id)
        elif info.source == "local" and not enable:
            # 本地停用：搬 Mods/ → disabled_dir
            # 物理约束：disabled_dir/<folder_name> 同一时间也只能有一个副本
            # 如果已有同 mod_id 的另一个副本占着 → 先把它挪到 swap，搬完 info 后从 _mod_versions 移除
            # （物理上它被 info 覆盖了，游戏只会加载 info）
            mid_lower = info.mod_id.lower()
            disabled_target = self._game.disabled_dir / info.folder_name
            if disabled_target.exists():
                for i, v in enumerate(self._mod_versions.get(mid_lower, [])):
                    if v.source == "disabled" and str(v.install_dir) == str(disabled_target):
                        swap_dir = self._game.disabled_dir / f"{info.folder_name}.swap-{int(time.time())}"
                        import os as _os
                        _os.replace(str(v.install_dir), str(swap_dir))
                        _log.info("[set_enabled] 停用冲突: disabled_dir/%s 被另一份副本占着 → 先挪到 swap %s",
                                  info.folder_name, swap_dir)
                        self._mod_versions[mid_lower].pop(i)
                        cur_main = self._mods.get(mid_lower)
                        if cur_main and str(cur_main.install_dir) == str(disabled_target):
                            self._mods[mid_lower] = info
                        break
            info = move_mod_between(info, self._game.disabled_dir, target_source="disabled")
            phys_ok = True
            _log.info("[set_enabled] 物理层(local停用): %s 搬去 disabled/", info.mod_id)
        elif info.source == "disabled" and enable:
            # 停用侧启用：搬 mods_disabled/ → Mods/
            # 物理约束：Mods/<folder_name> 同一时间只能有一个副本
            # 如果已有同 mod_id 的副本占着 → 抢占（原子交换：旧的搬去 mods_disabled/ 停掉）
            _, info = self._preempt_existing_local(info)
            target_mods = self._game.mods_dir / info.folder_name
            if info.source == "local" and str(info.install_dir) == str(target_mods):
                _log.info("[set_enabled] 物理层(启用): %s 抢占已到位，跳过 move_mod_between", info.mod_id)
                phys_ok = True
            else:
                info = move_mod_between(info, self._game.mods_dir, target_source="local")
                phys_ok = True
                _log.info("[set_enabled] 物理层(启用): %s 搬去 Mods/", info.mod_id)
        else:
            _log.info("[set_enabled] 物理层: %s source=%s enable=%s → 已是目标状态，跳过物理操作",
                      info.mod_id, info.source, enable)
            phys_ok = True

        # ====== 关键修复：把物理层返回的新 info 同步回内存模型 self._mods ======
        # 否则 get_state → _mod_dict 序列化出来的还是旧 source/install_dir → 前端看到的图标/路径都不对
        mid_lower = info.mod_id.lower()
        # 2026-11-01 多副本修复：同 mod_id 可能有多个 workshop 副本，不能随便覆盖 _mods[mid_lower]
        # 只有当 canonical 条目本身就是我们刚操作的这个副本时才更新
        cur_main = self._mods.get(mid_lower)
        if cur_main is None or str(cur_main.install_dir) == _orig_install_dir:
            self._mods[mid_lower] = info
        # 同时更新 _mod_versions 里对应条目（物理层搬目录会改 info.source/install_dir）
        # 2026-10-15 修复：用物理层之前保存的 _orig_install_dir 精确匹配！
        # 物理层搬目录后 install_dir 变了，直接用新路径可能匹配到另一个物理副本
        # 同时去掉 norm_mod_source 匹配——norm_mod_source("local") == norm_mod_source("disabled") → 会覆盖
        for i, v in enumerate(self._mod_versions.get(mid_lower, [])):
            if str(v.install_dir) == _orig_install_dir:
                self._mod_versions[mid_lower][i] = info
                break
        # 更新最近活动时间（启/停都是用户操作，都要 touch）
        self._touch_activity(info.mod_id)
        _log.debug("[set_enabled] 内存同步: %s source=%s install_dir=%s",
                   info.mod_id, info.source, info.install_dir)

        # =====================================================================
        # 逻辑层：settings.save is_enabled + _source_states + 排序
        # =====================================================================
        if self.uses_native_mod_list:
            # 只改当前 source key（游戏原生逻辑：settings.save 里 is_enabled 是 canonical 粒度）
            self._source_states.setdefault(mid_lower, {})[_orig_state_key] = enable

            # 记录最近 toggle 时间（前端停用侧排序用）
            self._source_last_toggled.setdefault(mid_lower, {})[_orig_state_key] = time.time()

            # 从 source_states 聚合：该 canonical 整体 is_enabled（任何 source 启用即为 True）
            mod_ss = self._source_states.get(mid_lower, {})
            canonical_any_enabled = bool(mod_ss) and any(mod_ss.values())

            # 更新 settings.save 里该 canonical 的 is_enabled（canonical 粒度，不带 source）
            ms = self._native_full_data.setdefault("mod_settings", {})
            mod_list = ms.get("mod_list", [])
            for i, entry in enumerate(mod_list):
                if isinstance(entry, dict) and str(entry.get("id", "")).lower() == mid_lower:
                    mod_list[i] = dict(entry)
                    mod_list[i]["is_enabled"] = bool(canonical_any_enabled)
                    break

            if auto_save:
                self._do_save_native_mod_list()

            # 如果 BMM 双写打开 → 顺便写 BMM settings（保持跟游戏内 BMM 菜单勾选一致）
            if self.uses_better_mod_menu:
                workshop_url: Optional[str] = None
                if info.workshop_id:
                    workshop_url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={info.workshop_id}"
                self._bmm_settings = bmm_settings_set_enabled(
                    self._bmm_settings,
                    info.mod_id,
                    enable,
                    workshop_url=workshop_url,
                )
                if auto_save:
                    self._do_save_bmm_settings()

            # 同步 state.enabled_order / disabled_order
            # 2026-10 改动：不能简单移除整个 canonical，要看各 source 的整体状态：
            #   · 只要有一个 source 是 enabled → canonical 在 enabled_order
            #   · 所有 source 都是 disabled → canonical 在 disabled_order
            # 修复 2026-10-14 多副本停一个后强制排到第一个：
            #   原来用原生清单 nat_enabled_canon 整体覆盖 enabled_order，
            #   原生清单顺序和用户当前前端排的顺序不一致 → 停一个 = 重排整个列表。
            #   现在只做「canonical 在哪个侧」的简单判断，不动列表内部顺序。
            self._state.enabled_order = [m for m in self._state.enabled_order if m.lower() != mid_lower]
            self._state.disabled_order = [m for m in self._state.disabled_order if m.lower() != mid_lower]
            # mod_ss 和 canonical_any_enabled 上面已经算过了，直接用
            canonical = info.mod_id
            if canonical_any_enabled:
                # 该 mod 至少一个 source 启用 → 加回 enabled_order 后做游戏原生排序
                # Kahn 拓扑保证依赖链正确，PriorityQueue 尊重 settings.save 用户意图
                self._state.enabled_order.append(canonical)
                self._apply_game_sort_to_enabled()
            else:
                # 所有 source 都是停用 → 插到停用侧**顶部**（刚停的排最前）
                if canonical in self._state.disabled_order:
                    self._state.disabled_order.remove(canonical)
                self._state.disabled_order.insert(0, canonical)
            _log.info(
                "[set_enabled] 原生清单模式：state_key=%s enable=%s；source_states[%s]=%s；enabled_order(%d) disabled_order(%d)",
                _orig_state_key, enable, mid_lower, mod_ss,
                len(self._state.enabled_order), len(self._state.disabled_order),
            )
            if auto_save: self.save_state()
            else: self._mark_dirty()
            self._invalidate_mod_caches()  # ← 漏了！改了 enabled_order 必须清缓存
            return True

        # =====================================================================
        # 模式 A：BetterModMenu（原生清单不可用 + prefer_better_mod_menu=True + 已安装 BMM）
        #   物理层上面已经统一做过了，这里只负责写 BMM JSON + 同步 state
        # =====================================================================
        if self.uses_better_mod_menu:
            # 额外兜底：Workshop mod 启用 + 现在 Mods/ 下还是没命中（stub 注入又失败）
            if enable and is_workshop_related and self._game.workshop_dir is not None:
                installed_in_mods = False
                try:
                    info_path = Path(info.install_dir).resolve()
                    try:
                        info_path.relative_to(self._game.mods_dir.resolve())
                        installed_in_mods = True
                    except ValueError:
                        installed_in_mods = False
                except Exception:
                    installed_in_mods = False
                if not installed_in_mods:
                    _log.warning("[set_enabled] BMM：启用 Workshop mod %s (install=%s)，Mods/ 仍未命中 → 重试种 stub", info.mod_id, info.install_dir)
                    try:
                        info = ensure_workshop_stub_enabled(info, self._game)
                        # 2026-11-01 多副本保护：不要无条件覆盖 canonical 条目
                        _cur_main = self._mods.get(info.mod_id.lower())
                        if _cur_main is None or str(_cur_main.install_dir) == str(info.install_dir):
                            self._mods[info.mod_id.lower()] = info
                    except Exception as e:
                        _log.error("[set_enabled] BMM：重试种 stub 失败（BMM Enabled=true 会写入，但物理启用可能未完成）: %s", e)

            # ---- 同步写 BMM settings（保持游戏内菜单勾选一致）----
            workshop_url: Optional[str] = None
            if info.workshop_id:
                workshop_url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={info.workshop_id}"
            self._bmm_settings = bmm_settings_set_enabled(
                self._bmm_settings,
                info.mod_id,
                enable,
                workshop_url=workshop_url,
            )

            # BMM settings 原子落盘
            try:
                save_bmm_settings(self._bmm, self._bmm_settings)
            except Exception as e:
                _log.error("[set_enabled] BMM 模式：写入 mod_settings.json 失败（但物理层已生效）: %s", e)
                raise

            # 同步 state.enabled_order / disabled_order（物理位置是权威）
            mid_lower = info.mod_id.lower()
            canonical = info.mod_id
            self._state.enabled_order = [m for m in self._state.enabled_order if m.lower() != mid_lower]
            self._state.disabled_order = [m for m in self._state.disabled_order if m.lower() != mid_lower]
            if enable:
                self._state.enabled_order.append(canonical)
            else:
                self._state.disabled_order.append(canonical)
            _log.info("[set_enabled] BMM 模式：物理层已真生效 + BMM Enabled=%s 已写；同步 enabled_order(%d) disabled_order(%d)",
                      enable, len(self._state.enabled_order), len(self._state.disabled_order))
            if auto_save: self.save_state()
            else: self._mark_dirty()
            return True

        # =====================================================================
        # 模式 B：fallback（物理目录驱动 = LOM 没启用）
        #         物理层已经在顶部统一做完，这里只调整 order 列表 + 落盘 manager.state
        # =====================================================================
        # 调整 order 列表
        mid_lower = info.mod_id.lower()
        canonical = info.mod_id
        self._state.enabled_order = [m for m in self._state.enabled_order if m.lower() != mid_lower]
        self._state.disabled_order = [m for m in self._state.disabled_order if m.lower() != mid_lower]
        if enable:
            self._state.enabled_order.append(canonical)
            _log.info("[set_enabled] %s → 加入启用侧 (size=%d)", canonical, len(self._state.enabled_order))
        else:
            self._state.disabled_order.append(canonical)
            _log.info("[set_enabled] %s → 加入停用侧 (size=%d)", canonical, len(self._state.disabled_order))
        self._invalidate_mod_caches()
        if auto_save: self.save_state()
        else: self._mark_dirty()
        return True

    def toggle_enabled(self, mod_id: str) -> None:
        info = self._find(mod_id)
        if info is None:
            return
        currently_enabled = any(m.lower() == info.mod_id.lower() for m in self._state.enabled_order)
        self.set_enabled(mod_id, not currently_enabled)

    # --------------------------------------------------------------- 启动游戏

    DEFAULT_STEAM_APP_ID = "2868840"
    DEFAULT_STEAM_APP_ID_ALT = "2865400"
    DEFAULT_PROCESS_NAMES = ("SlayTheSpire2.exe",)
    DEFAULT_LAUNCH_TIMEOUT_SECONDS = 30

    def _try_launch_exe_directly(self) -> tuple[bool, str]:
        """兜底诊断：直启游戏 exe（STS2 会报 Steam 初始化失败，但能确认 exe 路径是否可执行）。
        返回 (是否成功子进程启动, 诊断说明)。
        """
        import subprocess
        import sys

        if self._game is None:
            return False, "无 GameInstall，无法直启"
        exe = self._game.exe_path
        if not exe.is_file():
            return False, f"exe 不存在: {exe}"
        try:
            _log.info("[直启诊断] 尝试直接启动 exe: %s (cwd=%s)", exe, self._game.game_root)
            proc = subprocess.Popen(
                [str(exe)],
                cwd=str(self._game.game_root),
                close_fds=True,
                creationflags=(0x08000000 if sys.platform == "win32" else 0),
            )
            _log.info("[直启诊断] 子进程已创建, pid=%s（如果随后立即退出，通常是 Steam API 初始化失败——这是正常的）", proc.pid)
            return True, f"已启动子进程 pid={proc.pid}，请留意 Steam 初始化失败提示（说明路径正确、但必须走 Steam 协议）"
        except Exception as e:
            _log.exception("[直启诊断] 直启 exe 抛出异常")
            return False, f"直启失败: {type(e).__name__}: {e}"

    def _detect_steam_protocol_available(self) -> tuple[bool, str]:
        """检测本机 steam:// URL 协议是否注册（Windows 注册表）。"""
        import sys

        if sys.platform != "win32":
            return True, "非 Windows，跳过注册表检测"
        try:
            import winreg  # type: ignore
        except Exception as e:
            return False, f"无法加载 winreg: {e}"
        probes = [
            (winreg.HKEY_CLASSES_ROOT, r"steam\shell\open\command", None),
            (winreg.HKEY_CURRENT_USER, r"Software\Classes\steam\shell\open\command", None),
        ]
        for hive, subkey, _ in probes:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    (val, typ) = winreg.QueryValueEx(key, "")
                    if val:
                        return True, f"协议已注册: {val}"
            except FileNotFoundError:
                continue
            except Exception as e:
                _log.warning("读取 steam 协议注册表 %s\\%s 失败: %s", hive, subkey, e)
        return False, "未在 HKCR/HKCU 找到 steam:// 协议，请确认 Steam 客户端已安装并至少登录过一次。"

    def launch_via_steam(self, steam_app_id: str | None = None) -> None:
        """通过 Steam 协议启动（按你要求：第一优先是 `cmd /c start "" steam://...`）。
        所有方案都失败时，最后尝试直启 exe 做诊断并抛异常。
        """
        import ctypes
        import subprocess
        import sys

        # 0. 前置：检查 Steam 协议是否注册
        proto_ok, proto_info = self._detect_steam_protocol_available()
        if proto_ok:
            _log.info("[launch] steam:// 协议: %s", proto_info)
        else:
            _log.error("[launch] steam:// 协议未注册: %s", proto_info)

        # 优先用用户传的 AppID；否则两个常见值都试一次
        app_ids: list[str] = []
        if steam_app_id:
            app_ids.append(steam_app_id)
        else:
            app_ids = [self.DEFAULT_STEAM_APP_ID, self.DEFAULT_STEAM_APP_ID_ALT]
        _log.info("[launch] 尝试 AppID 顺序: %s（游戏 exe=%s, game_root=%s）", app_ids,
                  self._game.exe_path if self._game else None,
                  self._game.game_root if self._game else None)
        _log.info("[launch] 提示：你也可以手动开 cmd 验证这两条命令，任意一条成功都说明 Steam 协议本身没问题：")
        for aid in app_ids:
            _log.info("[launch]    cmd> cmd /c start \"\" \"steam://rungameid/%s\"", aid)

        errors: list[str] = []
        for aid in app_ids:
            steam_url = f"steam://rungameid/{aid}"
            _log.info("[launch] AppID=%s, URL=%s", aid, steam_url)

            # ============================================================
            # 方案 1（你要求的第一优先）：cmd.exe 内置的 start 命令
            #   注意：必须加空标题 ""，否则 start 会把带引号的 URL 当成窗口标题
            # ============================================================
            try:
                _log.info("[launch][%s] 方案1: cmd /c start \"\" \"%s\"", aid, steam_url)
                # shell=False 但第一个参数是 cmd.exe，避免 shell=True 带来的转义问题
                # 不设 CREATE_NO_WINDOW / DETACHED，让 cmd 正常派发消息泵
                subprocess.Popen(
                    ["cmd.exe", "/c", "start", "", steam_url],
                    close_fds=True,
                )
                _log.info("[launch][%s] 方案1(cmd/start) 已提交。请在 10 秒内留意 Steam 版本选择弹窗（可能在窗口后面）。", aid)
                return
            except Exception as e:
                _log.warning("[launch][%s] 方案1(cmd/start) 失败: %s", aid, e, exc_info=True)
                errors.append(f"AppId {aid}: cmd/start 失败: {e}")

            # ============================================================
            # 方案 2：ctypes 直接调用 ShellExecuteW（拿返回值，精确诊断）
            #   返回值 > 32 = 成功；≤32 = 失败，值是错误码
            #   错误码含义：
            #     0 = 内存不足 / 2 = 文件不存在 / 3 = 路径不存在 / 5 = 拒绝访问
            #     8 = 内存不足 / 10 = 环境错误 / 11 = 无效的 .exe 格式 / ...
            #   这是 os.startfile 的"增强版"——os.startfile 把返回值吞掉了。
            # ============================================================
            if sys.platform == "win32":
                try:
                    _log.info("[launch][%s] 方案2: ShellExecuteW(NULL, 'open', '%s', NULL, NULL, SW_SHOWNORMAL)", aid, steam_url)
                    hinstance = ctypes.windll.shell32.ShellExecuteW(
                        None,                # hwnd = 无主窗口
                        "open",              # operation
                        steam_url,           # file/url
                        None,                # parameters
                        None,                # working dir
                        1,                   # nShowCmd = SW_SHOWNORMAL
                    )
                    _log.info("[launch][%s] 方案2 ShellExecuteW 返回值: %s (>32 表示成功)", aid, hinstance)
                    if int(hinstance) > 32:
                        _log.info("[launch][%s] 方案2(ShellExecuteW) 成功提交给 Shell。", aid)
                        return
                    # 失败：列出常见错误码
                    err_map = {
                        0: "系统内存不足",
                        2: "文件/URL 未找到(SE_ERR_FNF)",
                        3: "路径未找到(SE_ERR_PNF)",
                        5: "拒绝访问(SE_ERR_ACCESSDENIED)",
                        8: "动态内存不足(SE_ERR_OOM)",
                        10: "错误的 Windows 版本(SE_ERR_SHARE)",
                        11: "无效的 EXE 格式(SE_ERR_BADFORMAT)",
                        20: "程序名关联无效(SE_ERR_ASSOCINCOMPLETE)",
                        21: "DDE 超时(SE_ERR_DDETIMEOUT)",
                        27: "没有关联程序(SE_ERR_NOASSOC)——最常见，steam:// 协议未注册或关联损坏",
                        28: "DDE 启动失败(SE_ERR_DDEFAIL)",
                        29: "正在执行 DDE 事务(SE_ERR_DDEBUSY)",
                        30: "开发者：关联名无效(SE_ERR_DLLNOTFOUND)",
                        31: "没有可用的关联(SE_ERR_DLLNOTFOUND)",
                        32: "关联程序调用失败(SE_ERR_SHARE)",
                    }
                    meaning = err_map.get(int(hinstance), "未知错误码，查 WinError.h 中 SE_ERR_*")
                    msg = f"ShellExecuteW 返回错误码 {hinstance}: {meaning}"
                    _log.error("[launch][%s] 方案2 失败: %s", aid, msg)
                    errors.append(f"AppId {aid}: ShellExecuteW 失败 (code={hinstance}, {meaning})")
                except Exception as e:
                    _log.warning("[launch][%s] 方案2 异常: %s", aid, e, exc_info=True)
                    errors.append(f"AppId {aid}: ShellExecuteW 异常: {e}")

            # ============================================================
            # 方案 3：os.startfile（和 2 是同一底层 API，但拿不到返回值）
            # ============================================================
            if sys.platform == "win32":
                try:
                    _log.info("[launch][%s] 方案3: os.startfile('%s')", aid, steam_url)
                    os.startfile(steam_url)  # type: ignore[attr-defined]
                    _log.info("[launch][%s] 方案3(os.startfile) 已提交。", aid)
                    return
                except Exception as e:
                    _log.warning("[launch][%s] 方案3 失败: %s", aid, e)
                    errors.append(f"AppId {aid}: os.startfile 失败: {e}")

            # ============================================================
            # 方案 4：explorer.exe "steam://..."
            # ============================================================
            try:
                _log.info("[launch][%s] 方案4: explorer.exe \"%s\"", aid, steam_url)
                subprocess.Popen(
                    ["explorer.exe", steam_url],
                    close_fds=True,
                    creationflags=0,
                )
                _log.info("[launch][%s] 方案4(explorer) 已提交。", aid)
                return
            except Exception as e:
                _log.warning("[launch][%s] 方案4 失败: %s", aid, e)
                errors.append(f"AppId {aid}: explorer 失败: {e}")

        # ---------------- 所有 AppID 的所有方案全失败，做直启诊断 ----------------
        _log.error("[launch] 所有 AppID 的所有 Steam 协议方案都失败。错误列表:\n  %s",
                   "\n  ".join(errors) if errors else "(无)")
        _log.info("[launch] 进入兜底：尝试直接启动 exe 进行诊断（会弹出 Steam 初始化失败的提示，属预期行为）")
        ok, diag = self._try_launch_exe_directly()
        diag_info = f"直启诊断: {diag}"
        _log.info("[launch] %s", diag_info)

        # 顺便在日志里把可以手动执行的 cmd 命令再打印一遍，方便用户直接复制
        _log.error("[launch] ============== 手动诊断命令（请复制到 cmd 里执行看效果）==============")
        for aid in app_ids:
            _log.error("[launch]    命令 A :  cmd /c start \"\" \"steam://rungameid/%s\"", aid)
        if self._game is not None:
            _log.error("[launch]    命令 B :  \"%s\"  (直启 exe, 看是否弹出 Steam 初始化失败)",
                       self._game.exe_path)
        _log.error("[launch] =======================================================")

        raise RuntimeError(
            "Steam 协议启动失败（所有方案 + 所有 AppID 均未成功）：\n- "
            + ("\n- ".join(errors) if errors else "无具体错误")
            + f"\n\n另外：{diag_info}"
            + ("\n（如果直启显示 Steam API 初始化失败，说明游戏文件路径正确，问题在 Steam 客户端/协议）" if ok else "")
        )

    @classmethod
    def _list_named_processes(cls, process_names: Iterable[str] | None = None) -> list[tuple[str, int, Optional[str]]]:
        """返回 [(进程名, pid, exe_path 或 None), ...]，只返回名字在 process_names 中的进程。
        exe_path 在 Win32 上通过 WMIC（或 Get-Process 兜底）拿；拿不到则为 None（例如权限不足的系统进程）。
        process_names=None → 使用 DEFAULT_PROCESS_NAMES。
        """
        import subprocess
        import sys

        names = list(process_names if process_names is not None else cls.DEFAULT_PROCESS_NAMES)
        def _norm(n: str) -> str:
            n = n.strip().lower()
            if not n.endswith(".exe"):
                n += ".exe"
            return n
        wanted = {_norm(n) for n in names}
        if not wanted:
            return []
        results: list[tuple[str, int, Optional[str]]] = []

        creationflags = 0x08000000 if sys.platform == "win32" else 0

        # 方案1：WMIC 拿 Name,ProcessId,ExecutablePath（最稳，中文路径也 OK）
        try:
            r = subprocess.run(
                ["wmic", "process", "get", "Name,ProcessId,ExecutablePath", "/FORMAT:csv"],
                capture_output=True,
                text=True,
                timeout=12,
                encoding="utf-8",
                errors="ignore",
                creationflags=creationflags,
            )
            if r.returncode == 0:
                lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
                if lines:
                    header = [h.strip().lower() for h in lines[0].split(",")]
                    try:
                        name_idx = header.index("name")
                        pid_idx = header.index("processid")
                        path_idx = header.index("executablepath")
                        for ln in lines[1:]:
                            parts = ln.split(",")
                            if len(parts) <= max(name_idx, pid_idx, path_idx):
                                continue
                            pname = parts[name_idx].strip().strip('"')
                            try:
                                pid = int(parts[pid_idx].strip())
                            except ValueError:
                                continue
                            ppath = parts[path_idx].strip().strip('"') or None
                            if _norm(pname) in wanted:
                                results.append((pname, pid, ppath))
                        if results:
                            return results
                    except ValueError:
                        pass  # 某些列名不存在，fallback
        except Exception:
            pass

        # 方案2：PowerShell Get-Process（WMIC 没装/超时用）
        try:
            ps = (
                "Get-Process | Select-Object Name,Id,Path "
                "| ConvertTo-Csv -NoTypeInformation"
            )
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=12,
                encoding="utf-8",
                errors="ignore",
                creationflags=creationflags,
            )
            if r.returncode == 0:
                lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
                if len(lines) >= 2:
                    # CSV header 第一行
                    header = [h.strip().lower().strip('"') for h in lines[0].split(",")]
                    try:
                        name_idx = header.index("name")
                        pid_idx = header.index("id")
                        path_idx = header.index("path")
                        for ln in lines[1:]:
                            # split by ',' but respect simple quoted CSV
                            raw = ln.strip()
                            parts: list[str] = []
                            buf = ""
                            in_quote = False
                            for ch in raw:
                                if ch == '"':
                                    in_quote = not in_quote
                                elif ch == "," and not in_quote:
                                    parts.append(buf)
                                    buf = ""
                                else:
                                    buf += ch
                            parts.append(buf)
                            if len(parts) <= max(name_idx, pid_idx, path_idx):
                                continue
                            pname = parts[name_idx].strip().strip('"')
                            try:
                                pid = int(parts[pid_idx].strip().strip('"'))
                            except ValueError:
                                continue
                            ppath = parts[path_idx].strip().strip('"') or None
                            if _norm(pname) in wanted:
                                results.append((pname, pid, ppath))
                        return results
                    except ValueError:
                        pass
        except Exception:
            pass

        # 兜底：tasklist 只拿 name + pid（拿不到 path）
        try:
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=creationflags,
            )
            if r.returncode == 0:
                for ln in r.stdout.splitlines():
                    ln = ln.strip()
                    if not ln.startswith('"'):
                        continue
                    cols = [p for p in ln.split('"') if p]
                    if len(cols) < 2:
                        continue
                    pname = cols[0]
                    try:
                        pid = int(cols[1])
                    except ValueError:
                        continue
                    if _norm(pname) in wanted:
                        results.append((pname, pid, None))
        except Exception:
            pass

        # 路径补齐：对于已经名字匹配但 ppath == None 的，再用 Win32 API 试一次（成功率最高，中文 OK）
        #   - QueryFullProcessImageNameW（Vista+，PROCESS_QUERY_LIMITED_INFORMATION 即可）
        #   - 如果装了 psutil，再兜底一次
        if results and any(p is None for _, _, p in results) and sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                PROCESS_QUERY_INFORMATION = 0x0400
                PROCESS_VM_READ = 0x0010
                # 先试低权限的 LIMITED，再试 PROCESS_QUERY_INFORMATION|VM_READ（留给 GetModuleFileNameEx）
                access_flags_chain = [
                    PROCESS_QUERY_LIMITED_INFORMATION,
                    PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                ]

                _QPFN = None
                try:
                    kernel32.QueryFullProcessImageNameW.argtypes = [
                        wintypes.HANDLE,
                        wintypes.DWORD,
                        wintypes.LPWSTR,
                        ctypes.POINTER(wintypes.DWORD),
                    ]
                    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
                    _QPFN = kernel32.QueryFullProcessImageNameW
                except Exception:
                    _QPFN = None

                _GMFEX = None
                try:
                    psapi = ctypes.WinDLL("psapi", use_last_error=True)
                    psapi.GetModuleFileNameExW.argtypes = [
                        wintypes.HANDLE,
                        wintypes.HMODULE,
                        wintypes.LPWSTR,
                        wintypes.DWORD,
                    ]
                    psapi.GetModuleFileNameExW.restype = wintypes.DWORD
                    _GMFEX = psapi.GetModuleFileNameExW
                except Exception:
                    _GMFEX = None

                CloseHandle = kernel32.CloseHandle
                CloseHandle.argtypes = [wintypes.HANDLE]
                CloseHandle.restype = wintypes.BOOL

                def _win32_get_exe_path(pid: int) -> str | None:
                    hproc = None
                    for access in access_flags_chain:
                        try:
                            hproc = kernel32.OpenProcess(access, wintypes.BOOL(False), wintypes.DWORD(pid))
                        except Exception:
                            hproc = None
                        if not hproc:
                            continue
                        try:
                            # 方案A：QueryFullProcessImageNameW（带 0=Win32 路径格式）
                            if _QPFN is not None:
                                size = wintypes.DWORD(4096)
                                buf = ctypes.create_unicode_buffer(size.value)
                                ok = _QPFN(hproc, wintypes.DWORD(0), buf, ctypes.byref(size))
                                if ok and size.value > 0:
                                    val = buf.value
                                    if val:
                                        return val
                            # 方案B：GetModuleFileNameExW（NULL hmodule = exe 自身）
                            if _GMFEX is not None:
                                size = 4096
                                buf = ctypes.create_unicode_buffer(size)
                                got = _GMFEX(hproc, wintypes.HMODULE(None), buf, wintypes.DWORD(size))
                                if got:
                                    val = buf.value
                                    if val:
                                        return val
                        finally:
                            try:
                                CloseHandle(hproc)
                            except Exception:
                                pass
                    return None
                patched: list[tuple[str, int, Optional[str]]] = []
                for pname, pid, ppath in results:
                    if ppath is None:
                        try:
                            resolved = _win32_get_exe_path(pid)
                        except Exception:
                            resolved = None
                        patched.append((pname, pid, resolved))
                    else:
                        patched.append((pname, pid, ppath))
                results = patched
            except Exception:
                pass

        # 最后如果装了 psutil，再给剩下 ppath is None 的最后一次机会
        if results and any(p is None for _, _, p in results):
            try:
                import psutil  # type: ignore
                patched: list[tuple[str, int, Optional[str]]] = []
                proc_map: dict[int, object] = {pr.pid: pr for pr in psutil.process_iter(["pid", "exe"])}
                for pname, pid, ppath in results:
                    if ppath is None and pid in proc_map:
                        try:
                            e = proc_map[pid].info.get("exe") if hasattr(proc_map[pid], "info") else proc_map[pid].exe()
                            if e:
                                patched.append((pname, pid, str(e)))
                                continue
                        except Exception:
                            pass
                    patched.append((pname, pid, ppath))
                results = patched
            except Exception:
                pass

        return results

    @classmethod
    def is_process_running(
        cls,
        process_names: Iterable[str] | None = None,
        expected_game_root: Path | str | None = None,
    ) -> bool:
        """True 当且仅当：进程名匹配 AND（给定 expected_game_root 时，exe_path 前缀是它）。
        不给 expected_game_root 就只按名字判断（不推荐，容易误判同名程序）。
        """
        procs = cls._list_named_processes(process_names or cls.DEFAULT_PROCESS_NAMES)
        if not procs:
            return False
        if expected_game_root is None:
            return bool(procs)
        try:
            root = Path(str(expected_game_root)).resolve()
        except Exception:
            return bool(procs)
        for name, pid, ppath in procs:
            if ppath is None:
                continue  # 拿不到路径的，不算通过路径校验
            try:
                pr = Path(ppath).resolve()
            except Exception:
                continue
            try:
                pr.relative_to(root)  # 前缀为 root 或 root 子目录
                # 命中（exe 确实在游戏根目录下）
                return True
            except ValueError:
                continue
        return False

    @classmethod
    def describe_matching_processes(
        cls,
        process_names: Iterable[str] | None = None,
        expected_game_root: Path | str | None = None,
    ) -> list[str]:
        """返回给人看的诊断字符串列表，格式 "PID <pid>: <name> [@ <path>]"，并标记是否落在 game_root 下。"""
        procs = cls._list_named_processes(process_names or cls.DEFAULT_PROCESS_NAMES)
        if not procs:
            return ["（未检测到匹配进程）"]
        msgs: list[str] = []
        try:
            root = Path(str(expected_game_root)).resolve() if expected_game_root else None
        except Exception:
            root = None
        for name, pid, ppath in procs:
            tag = ""
            if ppath and root is not None:
                try:
                    pr = Path(ppath).resolve()
                    pr.relative_to(root)
                    tag = "  ✅ exe 在游戏根目录下"
                except ValueError:
                    tag = "  ⚠️  exe 不在游戏根目录（误判！）"
            elif ppath is None and root is not None:
                tag = "  ⚠️  拿不到 exe 路径，无法确认真实性"
            msgs.append(f"  PID {pid}: {name} @ {ppath or '<未知路径>'}{tag}")
        return msgs

    def launch_and_wait(
        self,
        timeout_seconds: int | None = None,
        steam_app_id: str | None = None,
        poll_interval_seconds: float = 1.0,
        process_names: Iterable[str] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_tick: Callable[[int, int], None] | None = None,
        force: bool = False,
    ) -> tuple[bool, str]:
        """触发 Steam 启动 + 持续轮询 STS2 进程。
        新增 force=True：即使先验检测到"已在运行"，也会强制再走一次 Steam 启动（避免误判时完全不调 Steam）。
        进程真实性校验：exe 路径必须在 self._game.game_root 目录下，同名程序不算。
        """
        enforce_timeout = (timeout_seconds is not None) and int(timeout_seconds) > 0
        timeout = int(timeout_seconds or self.DEFAULT_LAUNCH_TIMEOUT_SECONDS)
        process_names_log = list(process_names) if process_names else list(self.DEFAULT_PROCESS_NAMES)
        game_root = self._game.game_root if self._game else None

        _log.info(
            "[launch_and_wait] 开始: timeout_seconds=%s (enforce=%s), poll=%.2fs, process_names=%s, force=%s, game_root=%s",
            timeout_seconds, enforce_timeout, poll_interval_seconds, process_names_log,
            force, game_root,
        )

        # 先检查一遍：已经在跑就直接成功；但同时打印诊断（让你在日志里看到底匹配到了谁）
        # 只有"exe 确实在我们检测到的游戏根目录下"才算"真正在运行"
        already_ok = (not force) and bool(game_root) and self.is_process_running(process_names, expected_game_root=game_root)
        diag = self.describe_matching_processes(process_names, expected_game_root=game_root)
        _log.info("[launch_and_wait] 启动前进程诊断结果:\n%s", "\n".join(diag))
        if already_ok:
            _log.warning(
                "[launch_and_wait] 先验命中：检测到 STS2 进程已在运行且路径匹配 → 跳过 Steam 启动直接返回成功。\n"
                "  如果你确认游戏根本没开，就是误判（上面的诊断信息应该能看到 exe 不在游戏目录下）。\n"
                "  解决：在 UI 里按住 Ctrl 再点启动按钮（或改 force=True），会强制调 Steam。"
            )
            return True, "ok"
        if (not force) and diag and not any("未检测到" in s for s in diag):
            _log.warning(
                "[launch_and_wait] 有同名进程匹配但未通过路径校验（force=False，但仍会继续启动 Steam，不会误判成功）。\n"
                "  诊断详情见上。"
            )

        try:
            self._ensure_baselib_log_window()
            # 管理器为唯一权威：不再做「原生基线还原」。
            #   开关/排序在操作时已即时落盘（物理层 + settings.save），Steam 直接启动
            #   和管理器启动看到的是同一状态。这里只做一次幂等的 apply 兜底，
            #   确保物理层与当前 profile 一致（例如 Steam 校验重下把 .disabled 改回去的情况）。
            if self._game is not None:
                try:
                    self._apply_profile_to_filesystem()
                except Exception as e:
                    _log.warning("[launch] apply profile 失败（继续用当前物理状态启动）: %s", e)
            self.launch_via_steam(steam_app_id=steam_app_id)
        except Exception as e:
            _log.exception("[launch_and_wait] launch_via_steam 抛出异常")
            return False, f"steam_error:{e}"

        dead_line = (time.monotonic() + timeout) if enforce_timeout else None
        start_ts = time.monotonic()
        last_log_ts = 0.0
        tick_count = 0
        first_hit_diag_printed = False
        while True:
            now = time.monotonic()
            tick_count += 1
            if enforce_timeout and dead_line is not None:
                remaining_float = max(0.0, float(dead_line - now))
                remaining = int(remaining_float + 1e-9)
                elapsed = timeout - int(remaining_float)
                remaining_display = int(remaining_float)
            else:
                elapsed = int(now - start_ts)
                remaining_display = -1
                remaining = elapsed

            if on_tick is not None:
                try:
                    on_tick(elapsed, remaining_display)
                except Exception:
                    pass

            # 用户取消
            if should_stop is not None:
                try:
                    if bool(should_stop()):
                        _log.info("[launch_and_wait] 用户请求取消（等待 %ds）", elapsed)
                        return False, "cancelled"
                except Exception:
                    pass

            # 进程检测：必须 exe 路径确实在游戏根目录下
            hit_real = self.is_process_running(process_names, expected_game_root=game_root)
            if hit_real:
                real_diag = self.describe_matching_processes(process_names, expected_game_root=game_root)
                _log.info(
                    "[launch_and_wait] ✅ 真实 STS2 进程检测成功！耗时 %ds。进程详情:\n%s",
                    elapsed, "\n".join(real_diag),
                )
                return True, "ok"
            # 诊断用：如果命中了名字但路径不对，只在第一次打一条日志（避免刷屏）
            if not first_hit_diag_printed:
                name_hit = self._list_named_processes(process_names or ModManager.DEFAULT_PROCESS_NAMES)
                if name_hit:
                    first_hit_diag_printed = True
                    false_diag = self.describe_matching_processes(process_names, expected_game_root=game_root)
                    _log.warning(
                        "[launch_and_wait] 名称匹配但路径校验未通过（仍会继续等），疑似误判进程:\n%s",
                        "\n".join(false_diag),
                    )

            # 超时
            if enforce_timeout and dead_line is not None and (now + 1e-9 >= dead_line):
                end_diag = self.describe_matching_processes(process_names, expected_game_root=game_root)
                _log.warning(
                    "[launch_and_wait] ⏰ 超时 %ds，未检测到真实 STS2 进程。已轮询 %d 次。\n"
                    "  进程诊断:\n%s\n"
                    "  可能原因：Steam 启动框点了取消 / Steam 客户端掉线 / 游戏更新中。",
                    timeout, tick_count, "\n".join(end_diag),
                )
                return False, "timeout"

            # 每 10 秒打一次心跳日志
            if now - last_log_ts >= 10.0:
                last_log_ts = now
                if enforce_timeout:
                    _log.info(
                        "[launch_and_wait] 等待中: elapsed=%ds, remaining=%ds, ticks=%d",
                        elapsed, remaining_display, tick_count,
                    )
                else:
                    _log.info(
                        "[launch_and_wait] 等待中（无硬超时）: elapsed=%ds, ticks=%d",
                        elapsed, tick_count,
                    )

            # 分段 sleep，取消响应 ~0.15s
            total_sleep = max(0.1, float(poll_interval_seconds))
            chunk = 0.15
            slept = 0.0
            while slept + 1e-6 < total_sleep:
                step = min(chunk, total_sleep - slept)
                time.sleep(step)
                slept += step
                if should_stop is not None:
                    try:
                        if bool(should_stop()):
                            _log.info("[launch_and_wait] sleep 中用户取消")
                            return False, "cancelled"
                    except Exception:
                        pass

    # ---------------------------------------------------------- Profile → 物理文件 --

    def _restore_workshop_manifests(self) -> int:
        """迁移：把工坊目录里遗留的 .json.disabled manifest 还原为 .json。

        旧行为用「改 manifest 后缀」停用工坊 mod —— 游戏会直接扫不到它。新行为改用
        原生 settings.save 的 is_enabled 表达停用（mod 物理保留，游戏能扫到并显示
        「停用」）。这里做一次性还原，保证游戏始终能看到所有工坊 mod。
        只处理 Steam 订阅登记里的工坊项；返回还原数量。
        """
        if self._game is None or self._game.workshop_dir is None:
            return 0
        from .scanner import (
            _scan_folder,
            is_workshop_mod_manifest_disabled,
            read_subscribed_workshop_ids,
            set_workshop_mod_disabled,
        )
        ws = self._game.workshop_dir
        if not ws.is_dir():
            return 0
        subs = read_subscribed_workshop_ids(ws)
        restored = 0
        for pub_dir in sorted(ws.iterdir()):
            if not pub_dir.is_dir():
                continue
            if subs is not None and pub_dir.name not in subs:
                continue
            for cand in [pub_dir] + [e for e in pub_dir.iterdir() if e.is_dir()]:
                if is_workshop_mod_manifest_disabled(cand) is None:
                    continue
                info = _scan_folder(cand, source="disabled", workshop_id=pub_dir.name,
                                    allow_disabled_manifest=True)
                if info is None:
                    continue
                try:
                    set_workshop_mod_disabled(info, disabled=False, game=self._game)
                    restored += 1
                    _log.info("[migrate] 工坊 manifest 还原: %s (%s)", info.mod_id, cand)
                except Exception as e:
                    _log.warning("[migrate] 还原 %s 失败: %s", info.mod_id, e)
                break
        return restored

    def _apply_profile_to_filesystem(self) -> None:
        """把 profile.enabled_order / disabled_order 应用到物理层 + settings.save。

        管理器为唯一权威：幂等操作，物理层已是目标状态时不做改动，可安全重复调用
        （启动前兜底、Steam 校验重下后纠正等）。

        物理层操作：
          - Workshop mod: 根据 enabled_order 同步 manifest 后缀
                          启用 → .json（ACTIVE），停用 → .json.disabled
                          （游戏扫描 workshop 源目录时只看 ACTIVE manifest）
          - 本地 mod:     profile 说 disabled → Mods/ → .mods_disabled/
                          profile 说 enabled  → .mods_disabled/ → Mods/
        """
        enabled_set = {m.lower() for m in self._state.enabled_order}
        changed_count = 0

        for mid, info in list(self._mods.items()):
            want_enabled = mid.lower() in enabled_set
            is_ws = bool(info.workshop_id)

            try:
                if is_ws:
                    # workshop mod：永远不改 manifest！只靠 settings.save is_enabled
                    # （仿制游戏原生逻辑——manifest 保持 .json 不动，游戏能扫到→显示列表→is_enabled 控制加载）
                    pass
                elif info.install_dir is not None:
                    # 本地 mod（source="local"/"disabled"）
                    cur_local = (info.source == "local") or (info.install_dir.parent.name.lower() == "mods")
                    if want_enabled and not cur_local:
                        move_mod_between(info, self._game.mods_dir, target_source="local")
                        changed_count += 1
                        _log.info("[apply] local %s: → Mods/ (启用)", info.mod_id)
                    elif not want_enabled and cur_local:
                        move_mod_between(info, self._game.disabled_dir, target_source="disabled")
                        changed_count += 1
                        _log.info("[apply] local %s: → disabled_dir/ (停用)", info.mod_id)

            except Exception as e:
                _log.warning("[apply] %s 物理操作失败: %s", mid, e)

        # settings.save：写 enabled_order 顺序 + is_enabled=true
        n_written = self._write_settings_save_from_state()
        if n_written:
            _log.info("[apply] settings.save mod_list 已更新: %d 条 (changed_count=%d)", n_written, changed_count)

        _log.info("[apply] profile→filesystem 完成: 物理层变更 %d 个 mod", changed_count)

    def _settings_write_gate_ok(self) -> bool:
        """settings.save 写回「健康度」门禁：返回 True 允许写回，False 跳过（BUG-4）。

        两道保险（防止扫描不完整时把大量工坊 mod 误标 is_enabled=False）：
          1) 读不到 Steam 订阅登记（appworkshop acf 缺失/解析失败）→ 跳过写回；
          2) 本次扫到的工坊 mod 数比上次骤降超过 50%（且上次基数 >=6）→ 跳过写回。
        基线每次都会被本次结果更新，避免一次误判后永久锁死写回（下次恢复正常即放行）。
        """
        ws_count = sum(1 for info in self._mods.values() if info.workshop_id)
        prev = self._last_ws_mod_count
        self._last_ws_mod_count = ws_count
        # 纯本地场景（本次与上次都没有工坊 mod）：无骤降风险，也不依赖 acf
        if ws_count == 0 and prev == 0:
            return True
        subs_known = self._subscribed_known
        if subs_known:
            # scan_all_mods 已判定订阅登记可读；再兜底直接验一次 acf，双保险
            try:
                from .scanner import read_subscribed_workshop_ids
                if self._game is not None and self._game.workshop_dir is not None:
                    subs_known = read_subscribed_workshop_ids(self._game.workshop_dir) is not None
            except Exception:
                subs_known = True
        if not subs_known:
            _log.warning(
                "[gate] 无法读取 Steam 订阅登记(appworkshop acf 缺失/解析失败) → "
                "跳过 settings.save 写回，避免误停用工坊 mod（ws_count=%d）", ws_count
            )
            return False
        if prev >= 6 and ws_count < prev * 0.5:
            _log.warning(
                "[gate] 工坊 mod 数骤降(%d → %d，上次基数≥6) → "
                "跳过 settings.save 写回，避免误停用工坊 mod", prev, ws_count
            )
            return False
        return True

    def _reorder_settings_save_from_state(self) -> int:
        """按当前 enabled_order 重排 STS2 原生清单(settings.save) 的 mod_list 顺序。

        与 _write_settings_save_from_state 的区别（FIX-9 分层写回）：**只重排顺序，
        不修改任何条目的 is_enabled**——满足「扫描后写回管理器顺序」的约定，又不会因
        扫描不完整而误停用 mod。启用集合的收敛只在启动前的 _apply_profile_to_filesystem
        里做（应用 profile 的明确意图）。返回 mod_list 条目数；未使用原生清单/失败返回 0。
        """
        if not (self.uses_native_mod_list and self._native_save_path and self._native_full_data):
            return 0
        if not self._mods:
            return 0
        if not self._settings_write_gate_ok():
            return 0
        try:
            # 2026-10-15 修复：重排前先过滤掉物理已消失的 ghost 条目
            # （settings.save 里还留着 placeholder，但 install_dir=None 的已经被清出 self._mods 了）
            present_lower_ids = {k.lower() for k in self._mods.keys()}
            ms_data = self._native_full_data.get("mod_settings", {})
            old_list = ms_data.get("mod_list", [])
            filtered_list = [
                e for e in old_list
                if isinstance(e, dict) and str(e.get("id", "")).lower() in present_lower_ids
            ]
            removed_count = len(old_list) - len(filtered_list)
            if removed_count > 0:
                ghost_ids = [
                    e.get("id", "?") for e in old_list
                    if isinstance(e, dict) and str(e.get("id", "")).lower() not in present_lower_ids
                ]
                _log.info(
                    "_reorder_settings_save_from_state: 过滤 %d 个 ghost 条目（物理已消失）: %s",
                    removed_count, ghost_ids[:5],
                )
                ms_data["mod_list"] = filtered_list

            self._native_full_data = nat_mod_list_reorder(
                self._native_full_data,  # type: ignore[arg-type]
                list(self._state.enabled_order),
            )
            ms = self._native_full_data.setdefault("mod_settings", {})  # type: ignore[union-attr]
            new_list = ms.get("mod_list", [])
            save_native_mod_list(self._native_save_path, self._native_full_data)  # type: ignore[arg-type]
            return len(new_list)
        except Exception as e:
            _log.warning("settings.save 顺序重排写入失败: %s", e)
            return 0

    def _write_settings_save_from_state(self) -> int:
        """按当前 enabled_order 重写 STS2 原生清单(settings.save) 的 mod_list。

        2026-10-17 重大修复：
          settings.save 是游戏原生维护的（只有 ~37 条，每条有 source 字段）。
          之前错误地重新生成了 143 条管理器内部 canonical，丢了 source 字段。

          正确做法：
          1. **只更新 old_list 里已有的条目的 is_enabled**
          2. **保留所有 old_list 原有的字段**（id, source 等）
          3. **不添加新条目**（游戏原生会自己加）
          4. 同时按 enabled_order 重排顺序（refresh 也需要）
        """
        if not (self.uses_native_mod_list and self._native_save_path and self._native_full_data):
            return 0
        if not self._mods:
            return 0
        if not self._settings_write_gate_ok():
            return 0
        try:
            ms = self._native_full_data.setdefault("mod_settings", {})  # type: ignore[union-attr]
            old_list = ms.get("mod_list", [])

            # 建立 canonical → is_enabled 的映射
            canonical_enabled: dict[str, bool] = {}
            for mid in self._state.enabled_order:
                canonical_enabled[mid.lower()] = True
            for mid in self._state.disabled_order:
                canonical_enabled.setdefault(mid.lower(), False)
            for low, states in self._source_states.items():
                if low in self._mods:
                    canonical_enabled[low] = any(states.values())

            # 按 enabled_order 重排：enabled_order 里的排前面，disabled_order 里的排后面
            enabled_lower = {m.lower() for m in self._state.enabled_order}
            disabled_lower = {m.lower() for m in self._state.disabled_order}
            order_map: dict[str, int] = {}
            for i, mid in enumerate(self._state.enabled_order):
                order_map[mid.lower()] = i
            for i, mid in enumerate(self._state.disabled_order):
                order_map[mid.lower()] = len(self._state.enabled_order) + i

            def _sort_key(entry: dict) -> tuple[int, int, str]:
                low = str(entry.get("id", "")).lower()
                # 优先：enabled_order 在前 → disabled_order 在后 → 末尾
                if low in enabled_lower:
                    return (0, order_map.get(low, 9999), low)
                elif low in disabled_lower:
                    return (1, order_map.get(low, 9999), low)
                else:
                    return (2, 9999, low)

            # 只更新 old_list 里已有的条目，保留所有原有字段
            new_list: list[dict] = []
            for entry in old_list:
                if not isinstance(entry, dict):
                    continue
                updated = dict(entry)  # 浅拷贝，保留所有字段
                mid = str(entry.get("id", ""))
                low = mid.lower()
                if low in canonical_enabled:
                    updated["is_enabled"] = bool(canonical_enabled[low])
                new_list.append(updated)

            # 按 sort_key 重排
            new_list.sort(key=_sort_key)

            ms["mod_list"] = new_list
            save_native_mod_list(self._native_save_path, self._native_full_data)  # type: ignore[arg-type]
            return len(new_list)
        except Exception as e:
            _log.warning("settings.save 写入失败: %s", e)
            return 0

    # ---------------------------------------------------------- BaseLib 日志面板 --

    def _do_save_native_mod_list(self) -> None:
        """把当前内存状态（enabled_order + 各 source 启停）写回 settings.save。

        2026-10-17 重大修复：
          settings.save 是游戏原生维护的！它只有游戏扫描到的 mod（~37 条），
          每条有 source 字段（"steam_workshop" / "mods_directory"）。
          之前错误地重新生成了 143 条（管理器内部所有 canonical），
          丢了 source 字段，还覆盖了游戏原生的列表。

        正确做法：
          1. **只更新 old_list 里已有的条目的 is_enabled**
          2. **保留所有 old_list 原有的字段**（id, source 等）
          3. **不添加新条目**（游戏原生会自己加）
          4. 不在 old_list 里的 canonical（管理器内部有但游戏没扫到）→ 跳过
        """
        if not (self.uses_native_mod_list and self._native_full_data):
            return

        ms = self._native_full_data.setdefault("mod_settings", {})
        old_list = ms.get("mod_list", [])

        # 建立 canonical → is_enabled 的映射（从 source_states + enabled_order）
        canonical_enabled: dict[str, bool] = {}
        for mid in self._state.enabled_order:
            canonical_enabled[mid.lower()] = True
        for mid in self._state.disabled_order:
            canonical_enabled.setdefault(mid.lower(), False)
        # source_states 覆盖（多副本 granularity）
        for low, states in self._source_states.items():
            if low in self._mods:
                canonical_enabled[low] = any(states.values())

        # 只更新 old_list 里已有的条目
        new_list: list[dict] = []
        for entry in old_list:
            if not isinstance(entry, dict):
                continue
            mid = str(entry.get("id", ""))
            low = mid.lower()
            if low in canonical_enabled:
                # 更新 is_enabled，保留所有其他字段（source 等）
                updated = dict(entry)  # 浅拷贝
                updated["is_enabled"] = bool(canonical_enabled[low])
                new_list.append(updated)
            else:
                # 不在管理器内部列表里 → 原样保留（可能是游戏自己加的）
                new_list.append(entry)

        ms["mod_list"] = new_list

        try:
            save_native_mod_list(
                self._native_save_path,  # type: ignore[arg-type]
                self._native_full_data,  # type: ignore[arg-type]
            )
        except Exception as e:
            _log.error("[_do_save_native_mod_list] settings.save 写回失败: %s", e)
            raise

    def _do_save_bmm_settings(self) -> None:
        """把 _bmm_settings 写回 BMM settings（2026-10-13 抽成 helper 方便手动保存调用）。"""
        try:
            save_bmm_settings(self._bmm, self._bmm_settings)
        except Exception as e:
            _log.error("[_do_save_bmm_settings] BMM settings 写回失败: %s", e)
            raise

    def _ensure_baselib_log_window(self) -> None:
        """确保 BaseLib.cfg 里 OpenLogWindowOnStartup=True。

        BaseLib (workshop mod ID=3737335127) 自带游戏内 NLogWindow 面板，配置文件在
        %APPDATA%/SlayTheSpire2/mod_configs/BaseLib.cfg。用户可能在游戏内手动关掉了，
        导致启动游戏时不再自动弹出。我们启动前强制确保这个开关为 True。
        """
        cfg_path = (Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "mod_configs" / "BaseLib.cfg")
        if not cfg_path.is_file():
            _log.info("[baselib_log] BaseLib.cfg 不存在（mod 未安装或首次运行），跳过")
            return
        try:
            raw = cfg_path.read_text(encoding="utf-8")
            cfg = json.loads(raw)
        except Exception as e:
            _log.warning("[baselib_log] BaseLib.cfg 读失败: %s", e)
            return

        current = cfg.get("OpenLogWindowOnStartup", "")
        if str(current).lower() == "true":
            _log.info("[baselib_log] OpenLogWindowOnStartup 已是 True，无需修改")
            return

        cfg["OpenLogWindowOnStartup"] = "True"
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        _log.info("[baselib_log] OpenLogWindowOnStartup: %r → True（已写回 %s）", current, cfg_path)


def _detect_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC：返回所有节点数 ≥ 2 的强连通分量（即循环依赖）。
    graph: {node: {deps}}  依赖从子 mod 指向它的前置 mod。
    """
    index_counter = [0]
    stack: list[str] = []
    lowlink: dict[str, int] = {}
    index: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    result: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in graph.get(v, ()):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w):
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                scc.append(w)
                if w == v:
                    break
            if len(scc) >= 2:
                result.append(sorted(scc))

    for node in graph:
        if node not in index:
            strongconnect(node)
    return result
