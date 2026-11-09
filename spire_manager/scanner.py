from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# 注意：STS2 有两个常见 AppID
#  2868840 = Steam 商店正式 AppID（主条目，steam://rungameid/ 必须用这个才能唤起启动框）
#  2865400 = 早期/备用 AppID，部分用户 libraryfolders.vdf 里可能出现
# 工坊下载目录统一是 <workshop>/content/2865400，两个 AppID 都能命中扫描
STEAM_APP_ID_VARIANTS = ("2868840", "2865400")
WORKSHOP_APP_ID = "2865400"  # 工坊内容目录统一是这个
GAME_FOLDER_NAME = "Slay the Spire 2"
GAME_EXE_NAME = "SlayTheSpire2.exe"
DISABLED_DIRNAME = ".mods_disabled"
# 真实用户环境里经常出现大小写/点号变体的 mod 目录，检测到了就一起扫描，按优先级选中“最可靠”的为主启用目录
MODS_DIR_VARIANTS = ("Mods", "mods")
DISABLED_DIR_VARIANTS = (DISABLED_DIRNAME, "mods_disabled")
MANIFEST_REQUIRED_KEYS = ("id", "name")  # has_pck / has_dll 可选（皮肤 mod 可能只有 dll，内容 mod 可能只有 pck）

# LoadOrderManager 的已知 mod_id + 创意工坊发布文件 id：
# 只有它处于「STS2 启动时默认目录驱动会扫到（即 manifest 正常，没被 .disabled）」的状态，
# 才能保证 Harmony 补丁真正运行，settings.save → mod_settings.mod_list 才会被游戏启用/停用逻辑
# 所尊重。否则管理器如果信 settings.save 而游戏按目录驱动加载，就会出现"管理器显示 22 个
# 启用，实际游戏加载 26 个 workshop mod"的严重不一致。
LOAD_ORDER_MANAGER_MOD_ID_CANON = "LoadOrderManager"
LOAD_ORDER_MANAGER_WORKSHOP_ID = "3747605109"

# 已知 STS2 framework mod（被大量其他 mod 作为依赖）
# 注意：如果某 framework mod 同时有 content（card/relic/enemy），仍应被判为 content
KNOWN_FRAMEWORK_MODS: frozenset[str] = frozenset({
    "BaseLib", "JmcModLib", "JmcModLib.Runtime", "MinionLib",
    "STS2-RitsuLib", "RitsuLib",
    "DownfallHelper",
    "LoadOrderManager", "BMM", "BetterModMenu",
    "HookGen", "SCTInjector",
})

# Content 关键词（manifest.tags / name / description 命中则优先判为 content）
CONTENT_KEYWORDS: tuple[str, ...] = (
    "card", "power", "relic", "event", "enemy", "monster",
    "potion", "map", "campaign", "act", "character", "hero",
    "deck", "starting", "boss", "minion",
)

# Skin/Cosmetic 关键词（用于从 name/tags/description 推断 mod 类型）
SKIN_KEYWORDS: tuple[str, ...] = (
    "skin", "cardart", "card art", "card_art",
    "cosmetic", "costume", "spine", "model", "art fix",
    "voice", "sfx",
)

# 从 manifest.url 推断外部来源的规则
_SOURCE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nexusmods", ("nexusmods.com", "nexus")),
    ("github", ("github.com", "raw.githubusercontent.com")),
    ("curseforge", ("curseforge.com", "curseforge.net")),
    ("modrinth", ("modrinth.com",)),
    ("steam", ("steamcommunity.com", "steamworkshop")),
)


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass
class ModManifest:
    id: str
    name: str
    version: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    dependencies: list[str] = field(default_factory=list)
    affects_gameplay: bool = False
    has_pck: bool = False
    has_dll: bool = False
    # 本地文件自带的标签（manifest 里的 tags 字段，可为空）
    tags: list[str] = field(default_factory=list)
    # 原始来源 URL（manifest 里的 url / homepage / repository）
    url: Optional[str] = None
    # mod 要求的游戏版本范围（manifest 里的 min_game_version / max_game_version）
    min_game_version: Optional[str] = None
    max_game_version: Optional[str] = None


@dataclass
class ModInfo:
    manifest: ModManifest
    folder_name: str               # 目录名（通常=id）。占位 mod = id
    install_dir: Optional[Path]    # 磁盘绝对路径（mod 根目录）。占位 mod = None
    source: str                    # "local" / "workshop" / "disabled"
    workshop_id: Optional[str] = None  # 若来自工坊，则是发布文件 id（目录名）
    # 2026-11-07 Step 1 新增：扫描时一次性 stat() 固化文件时间（隐藏问题 2）
    # 绝不能每次前端渲染时实时 stat()，否则停启用搬目录会刷 ctime
    mtime: float = 0.0
    ctime: float = 0.0

    @property
    def mod_id(self) -> str:
        return self.manifest.id

    @property
    def display_name(self) -> str:
        name = self.manifest.name
        if name:
            return name
        return f"[{self.folder_name}]" if self.folder_name else self.mod_id

    @property
    def is_placeholder(self) -> bool:
        """是否是占位 ModInfo（settings.save 里有记录，但当前扫描目录里暂时找不到 manifest 文件）。
        这种情况常见于：订阅了 mod 但 Steam 尚未下载、或 workshop_dir 暂时未挂载。
        我们仍需要在启用/停用列表里展示它（因为它真实存在于游戏源启用清单里），
        等下次 refresh（或用户手动点启用/停用）时会尝试补种 stub。"""
        return self.install_dir is None

    @property
    def mod_type(self) -> str:
        """推断 mod 类型：content / skin / framework / utility。

        推断优先级（content 和 skin 从 manifest.tags 里取，最准确）：
          1) manifest.tags 命中 CONTENT_KEYWORDS → content（玩法 mod，最高优先级）
          2) manifest.tags / name / desc 命中 SKIN_KEYWORDS → skin
          3) id 在 KNOWN_FRAMEWORK_MODS 里 → framework（纯框架才命中）
          4) manifest.affects_gameplay=False → utility（工具/UI 增强）
          5) 默认 → content
        """
        tags_lower = [t.lower() for t in (self.manifest.tags or [])]
        if any(kw in tags_lower for kw in CONTENT_KEYWORDS):
            return "content"

        hay = (
            " ".join(tags_lower) + " " +
            (self.manifest.name or "").lower() + " " +
            (self.manifest.description or "").lower()
        )
        for kw in SKIN_KEYWORDS:
            if kw in hay:
                return "skin"

        mid = self.mod_id.lower()
        if self.mod_id in KNOWN_FRAMEWORK_MODS or mid in {m.lower() for m in KNOWN_FRAMEWORK_MODS}:
            return "framework"

        if not self.manifest.affects_gameplay:
            return "utility"
        return "content"

    @property
    def mod_type_label(self) -> str:
        """mod_type 的中文显示标签。"""
        labels = {
            "framework": "框架",
            "utility": "工具",
            "content": "内容",
            "skin": "皮肤",
        }
        return labels.get(self.mod_type, "其他")

    @property
    def mod_type_icon(self) -> str:
        """mod_type 的 emoji 图标。"""
        icons = {
            "framework": "🔧",
            "utility": "🛠️",
            "content": "🎮",
            "skin": "🎨",
        }
        return icons.get(self.mod_type, "📦")

    @property
    def external_source(self) -> Optional[str]:
        """从 manifest.url / workshop_id 推断外部来源（nexusmods / github / curseforge / modrinth / steam）。"""
        url = (self.manifest.url or "").lower()
        if not url and self.workshop_id:
            return "steam"
        for label, patterns in _SOURCE_HINTS:
            for p in patterns:
                if p in url:
                    return label
        return None

    @property
    def source_label(self) -> str:
        """来源的中文标签：Steam工坊 / NexusMods / GitHub / 工坊 / 本地。

        注意：**永远不返回 "停用"** —— source 字段是扫描路径（"从哪个目录扫到的"），
        不是来源。"disabled" 是启用状态，来源只能是 Steam工坊 / Nexus / GitHub / 本地。
        启用/停用状态由列表位置表达，不需要来源徽章再说一遍。
        """
        external = self.external_source
        ext_labels = {
            "steam": "工坊",
            "nexusmods": "Nexus",
            "github": "GitHub",
            "curseforge": "Curse",
            "modrinth": "Mod",
        }
        if external and external in ext_labels:
            return ext_labels[external]
        if self.source == "workshop":
            return "工坊"
        # local / disabled / placeholder 且没有 external_source → 都算"本地"
        return "本地"

    @property
    def source_icon(self) -> str:
        """来源 emoji 小图标（列表里类型图标旁边显示）。"""
        external = self.external_source
        if external == "steam": return "🎭"
        if external == "nexusmods": return "🟠"
        if external == "github": return "🐙"
        if external == "curseforge": return "🟥"
        if external == "modrinth": return "🟩"
        if self.source == "workshop": return "🎭"
        if self.source == "local": return "📁"
        return "📦"

    @property
    def local_preview(self) -> Optional[str]:
        """本地 mod 文件夹里的预览图（icon/preview/thumbnail），返回 file:// URL。
        按优先级扫描：icon.png > icon.jpg > preview.png > preview.jpg > screenshot.png > 任意 .png/.jpg。
        扫描结果缓存到 install_dir/.preview_cache 文件里，避免每次都遍历目录。
        """
        if self.install_dir is None or self.workshop_id:
            # workshop mod 不需要本地预览（有 Steam 宣传图），placeholder mod 没有目录
            return None
        try:
            cache_path = self.install_dir / ".preview_cache"
            if cache_path.is_file():
                try:
                    cached = cache_path.read_text(encoding="utf-8").strip()
                    p = Path(cached)
                    if p.is_file():
                        return p.as_uri()
                except Exception:
                    pass

            names_priority = [
                "icon.png", "icon.jpg", "icon.webp",
                "preview.png", "preview.jpg", "preview.webp",
                "thumbnail.png", "thumbnail.jpg", "thumbnail.webp",
                "screenshot.png", "screenshot.jpg", "screenshot.webp",
                "card.png", "card.jpg",
            ]
            for name in names_priority:
                p = self.install_dir / name
                if p.is_file() and p.stat().st_size > 512:  # 至少 512 字节，过滤占位空图
                    try:
                        cache_path.write_text(str(p), encoding="utf-8")
                    except Exception:
                        pass
                    return p.as_uri()

            # 兜底：找任意 > 40KB 的 .png/.jpg 文件（排除 .dll/.pck/.json 等）
            for ext in (".png", ".jpg", ".webp"):
                candidates = [
                    p for p in self.install_dir.iterdir()
                    if p.is_file() and p.suffix.lower() == ext and p.stat().st_size > 40_000
                ]
                if candidates:
                    candidates.sort(key=lambda x: -x.stat().st_size)
                    pick = candidates[0]
                    try:
                        cache_path.write_text(str(pick), encoding="utf-8")
                    except Exception:
                        pass
                    return pick.as_uri()
        except Exception:
            pass
        return None


_VERSION_SPLIT_RE = re.compile(r"[.\-_+]")


def _version_key(v: str) -> tuple:
    """把版本字符串切成可比较的元组：数字段转 int 参与数值比较，非数字段退化按字符串。

    例："1.10.0" → (1, 10, 0)；"1.2-beta" → (1, 2, "beta")。数字段永远排在
    非数字段之前（Python 无法直接比较 int 与 str，用 (0, int) / (1, str) 包装）。
    纯自实现，不依赖 packaging，避免不同环境（有无该库）下排序结果不一致。
    """
    key: list = []
    for part in _VERSION_SPLIT_RE.split(str(v).strip()):
        if part == "":
            continue
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part))
    return tuple(key)


def version_gt(a: Optional[str], b: Optional[str]) -> bool:
    """a 的版本号是否严格高于 b（自实现 semver：数字段按数值比较，非数字段按字符串）。"""
    if not a:
        return False
    if not b:
        return True
    return _version_key(a) > _version_key(b)


def mod_version_key(info: ModInfo) -> str:
    """一个物理副本的物理层标识（用于 source_states / 菜单命中 / snapshot tuple key 第二维）。

    2026-11-05 重构：加 #mod_id.lower() 后缀确保唯一性。
    原因：合集工坊同目录平铺多个 manifest 时，多个 mod_id 共享同一个 install_dir
    → 旧格式 str(install_dir) 会撞 key；加 mod_id 后缀后每个副本独立唯一。

    注意：key 仍会随搬目录（Mods ↔ .mods_disabled）变化，互斥/比较必须用搬目录之后的新 key。
    """
    if info.install_dir is not None:
        return f"{str(info.install_dir)}#{info.mod_id.lower()}"
    return f"placeholder:{info.mod_id.lower()}"


@dataclass
class GameInstall:
    game_root: Path
    exe_path: Path
    mods_dir: Path
    disabled_dir: Path          # 主 disabled 目录（用于搬目录等操作）
    disabled_dirs: list[Path]  # 所有已存在的 disabled 目录变体（用于扫描，可能多个）
    workshop_dir: Optional[Path]
    workshop_beta_dir: Optional[Path] = None  # P2: Steam 测试版分支目录
    detected_by: str = ""
    # 游戏版本信息（从 godot.log 解析）
    version: str = ""                 # 如 "0.111.0"；空字符串=未检测到（没启动过或日志不存在）
    version_detected: bool = False    # 是否成功解析过版本
    # Steam Beta 分支信息（从 appmanifest_2868840.acf 解析）
    beta_key: str = ""                 # "public" / "public-beta" / "" (默认=稳定) / 其它自定义分支
    beta_label: str = ""                 # "public" / "public-beta" / "" (未知)
    steam_appmanifest_path: Optional[Path] = None


# --------------------------------------------------------------------------- #
# 游戏版本 & Steam Beta 分支检测
# --------------------------------------------------------------------------- #


def detect_game_version_from_log() -> Optional[dict]:
    """从 %APPDATA%\\SlayTheSpire2\\logs\\godot.log 解析游戏版本号。

    STS2 每次启动会在 godot.log 第 8 行左右写 Sentry.NET 初始化日志：
        [INFO] [Sentry.NET] Initialized: env=unknown, release=v0.111.0
    这个 release=vX.Y.Z 就是真实游戏版本。godot.log 每次启动会被轮转覆盖，
    所以这里读最新的那份即可。

    返回 {"version": "0.111.0"} 或 None。
    """
    log_dir = os.environ.get("APPDATA", "")
    if not log_dir:
        return None
    log_path = Path(log_dir) / "SlayTheSpire2" / "logs" / "godot.log"
    if not log_path.is_file():
        return None

    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    # 匹配 release=v0.111.0 或 release=0.111.0（有/无 v 前缀）
    match = re.search(r"release=v?(\d+\.\d+\.\d+)", text)
    if not match:
        return None
    version = match.group(1)  # "0.111.0"
    return {"version": version}


def _locate_godot_log() -> Optional[Path]:
    """定位 godot.log 文件路径（APPDATA\\SlayTheSpire2\\logs\\godot.log）。"""
    log_dir = os.environ.get("APPDATA", "")
    if not log_dir:
        return None
    p = Path(log_dir) / "SlayTheSpire2" / "logs" / "godot.log"
    return p if p.is_file() else None


# 正则：游戏加载 mod 时在 godot.log 里注册的子模块/子角色/子内容
#   1) Calling initializer method of type Snecko.SneckoCode.SneckoMainFile for Downfall, ...
#      → Downfall 有子模块 Snecko
#   2) [Downfall] Creating Snecko / [Downfall] Creating Seraph
#      → Downfall 创建了 Snecko 角色/敌人/卡牌池
_INITIALIZER_RE = re.compile(
    r'Calling initializer method of type '
    r'([A-Za-z_][\w\.]+?)MainFile\b.*?'
    r'for\s+([\w]+)'
)
_CREATING_RE = re.compile(
    r'\[([A-Z][\w]+)\]\s+Creating\s+(\w+)'
)


def scan_mod_subcontents_from_log() -> dict:
    """从 godot.log 解析每个 mod 包含的子内容（子角色/子模块/子卡牌池等）。

    这是最准确的来源——游戏自己在启动时通过 BaseLib 注册 mod 内容，
    会把每个子模块的类型名和创建日志吐到 godot.log。

    返回 {"Downfall": {"initializers": ["Snecko", "SlimeBoss", ...],
                       "creatings":    ["Snecko", ...]}}.
    如果没 godot.log 或 log 里无 mod 注册日志，返回空 dict。
    """
    log_path = _locate_godot_log()
    if log_path is None:
        return {}

    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    results: dict[str, dict] = {}
    for m in _INITIALIZER_RE.finditer(text):
        type_path = m.group(1)          # 如 "Snecko.SneckoCode.Snecko"
        mod_id = m.group(2)             # 如 "Downfall"
        short = type_path.split(".")[0]  # "Snecko"
        results.setdefault(mod_id, {"initializers": [], "creatings": []})
        if short not in results[mod_id]["initializers"]:
            results[mod_id]["initializers"].append(short)

    for m in _CREATING_RE.finditer(text):
        mod_id = m.group(1)             # 如 "Downfall"
        what = m.group(2)               # 如 "Snecko"
        results.setdefault(mod_id, {"initializers": [], "creatings": []})
        if what not in results[mod_id]["creatings"]:
            results[mod_id]["creatings"].append(what)

    # 去除纯 library/工具型 mod（只有自身 initializer，没额外 creating）
    # 它们的 initializers 是名字自身（比如 MinionLib.MainFile → MinionLib）
    # 这种不算"有子内容"，只保留有 1+ 额外 initializers 或 creatings 的
    _NOISE_MOD_IDS = {"INFO", "WARN", "ERROR", "DEBUG", "System", "Microsoft", "MegaCrit",
                      "Sentry", "Serilog", "Log", "Main"}
    _NOISE_SUBMODULES = {"ModName", "ModInfo", "Config", "Settings", "Logger", "Log",
                         "Main", "Entry", "Init"}

    pruned: dict[str, dict] = {}
    for mod_id, content in results.items():
        if mod_id in _NOISE_MOD_IDS or len(mod_id) < 3:
            continue
        init = content["initializers"]
        creates = content["creatings"]
        extras_init = [x for x in init if x != mod_id and x not in _NOISE_SUBMODULES]
        clean_creates = [x for x in creates if x not in _NOISE_SUBMODULES]
        if extras_init or clean_creates:
            pruned[mod_id] = {"initializers": extras_init, "creatings": clean_creates}

    return pruned


def detect_steam_beta_key(game_root: Optional[Path] = None) -> Optional[dict]:
    """读 Steam 安装目录下的 appmanifest_2868840.acf，解析当前 Beta 分支。

    Beta 信息存在两处（UserConfig 和 MountedConfig）：
        "BetaKey"    "public-beta"
    空字符串 或 "public" → 稳定版
    "public-beta" → 公开测试版
    其它字符串 → 自定义/未知分支

    Steam 安装路径来源：
      1) 先从 game_root 反推库根（game_root = <lib>/steamapps/common/...）
      2) 再用 _candidate_steam_roots() 的注册表 + 硬编码兜底

    返回 {"beta_key": str, "beta_label": str, "appmanifest_path": Path} 或 None。
    """
    # 1) 先反推 game_root 所在库
    candidate_acfs: list[Path] = []
    if game_root is not None:
        try:
            common_dir = game_root.resolve().parent  # common
            steamapps_dir = common_dir.parent       # steamapps
            lib = steamapps_dir.parent               # <lib>
            if common_dir.name == "common" and steamapps_dir.name == "steamapps" and lib.is_dir():
                candidate_acfs.append(steamapps_dir / "appmanifest_2868840.acf")
        except Exception:
            pass

    # 2) 注册表 + 硬编码候选
    for root in _candidate_steam_roots():
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            for lib in _parse_library_folders(vdf):
                candidate_acfs.append(lib / "steamapps" / "appmanifest_2868840.acf")
        else:
            candidate_acfs.append(root / "steamapps" / "appmanifest_2868840.acf")

    # 3) 去重 + 取第一个 is_file
    seen: set[str] = set()
    manifest_path: Optional[Path] = None
    for acf in candidate_acfs:
        try:
            key = str(acf.resolve()).lower()
        except Exception:
            key = str(acf).lower()
        if key in seen:
            continue
        seen.add(key)
        if acf.is_file():
            manifest_path = acf
            break

    if manifest_path is None:
        return None

    try:
        text = manifest_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    # 解析 BetaKey（简单字符串匹配，acf 是 VDF 格式，不是 JSON）
    beta_key = ""
    for line in text.splitlines():
        # 匹配 "BetaKey"    "public-beta"
        m = re.search(r'"BetaKey"\s+"([^"]*)"', line)
        if m:
            beta_key = m.group(1).strip()
            break

    # 确定显示名：直接用 Steam BetaKey 原文，不做中文翻译——
    # STS2 官方没给这些分支名出正式中文译名，保持英文原文最准确。
    if beta_key == "":
        beta_label = "public"  # 空 BetaKey = 默认稳定版，展示为 public
    else:
        beta_label = beta_key  # public-beta / 其它自定义分支 → 直接展示

    return {
        "beta_key": beta_key,
        "beta_label": beta_label,
        "appmanifest_path": manifest_path,
    }


def switch_steam_beta(branch: str) -> Optional[dict]:
    """切换 STS2 的 Steam Beta 分支。

    流程：游戏未运行 → 改 appmanifest BetaKey → 后台线程重启 Steam。

    必须重启 Steam 才能让它重读 acf 里的 BetaKey——外部程序没法调
    Steam 内部的 ChangeBetas() 方法，flushconfig/install 协议都不更新
    内存中的 BetaKey。

    branch:  "", "public" → 稳定版；"public-beta" → 公开测试版。
    """
    import threading

    # 1) 游戏进程不能在运行
    running = _find_running_game_process()
    if running:
        return {
            "ok": False, "error": "game_running",
            "message": f"游戏正在运行中（PID {running['pid']}），请先关闭。",
        }

    # 2) 规范化目标分支名
    normalized = branch.strip()
    if normalized in ("", "public"):
        target_branch, display_branch = "", "public"
    else:
        target_branch, display_branch = normalized, normalized

    # 3) 找 appmanifest
    manifest_info = detect_steam_beta_key()
    if manifest_info is None:
        return {"ok": False, "error": "manifest_not_found",
                "message": "找不到 appmanifest_2868840.acf。"}

    manifest_path: Path = manifest_info["appmanifest_path"]
    old_beta_key = manifest_info["beta_key"]

    if old_beta_key == target_branch:
        return {"ok": True, "branch": target_branch, "display": display_branch,
                "note": f"当前已是 {display_branch}。"}

    # 4) 改 acf（同步，毫秒级）
    try:
        text = manifest_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"ok": False, "error": "read_failed", "message": f"读取失败: {e}"}

    new_text, count = re.subn(
        r'("BetaKey"\s+")([^"]*)(")',
        lambda m: m.group(1) + target_branch + m.group(3),
        text,
    )
    if count == 0:
        return {"ok": False, "error": "parse_failed", "message": "BetaKey 没找到。"}

    bak_path = manifest_path.with_suffix(manifest_path.suffix + ".bak")
    try: shutil.copy2(manifest_path, bak_path)
    except Exception: pass

    try:
        manifest_path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": "write_failed",
                "message": f"写回失败: {e}"}

    # 5) 后台线程重启 Steam（不阻塞前端！）
    steam_was_running = _is_steam_running()
    steam_exe = _find_steam_exe() if steam_was_running else None

    def _restart_steam_async():
        if not steam_was_running:
            # Steam 没在运行 → 直接启动，启动时自然会读新 acf
            try:
                subprocess.Popen([str(steam_exe)], close_fds=True)
            except Exception:
                try: os.startfile("steam://open/main")  # type: ignore[attr-defined]
                except Exception: pass
            return
        # 在运行 → 优雅退出 → 等 → 启动
        if _shutdown_steam(timeout_sec=45):
            time.sleep(2)
            try:
                subprocess.Popen([str(steam_exe)], close_fds=True)
            except Exception:
                try: os.startfile("steam://open/main")  # type: ignore[attr-defined]
                except Exception: pass

    threading.Thread(target=_restart_steam_async, daemon=True).start()

    return {
        "ok": True, "branch": target_branch, "display": display_branch,
        "old_branch": old_beta_key, "beta_keys_updated": count,
        "manifest_path": str(manifest_path),
        "steam_was_running": steam_was_running,
        "note": "Steam 正在后台重启以加载新分支，重启后会自动下载对应 depot。",
    }


def get_steam_app_update_status() -> Optional[dict]:
    """读取 appmanifest，返回 Steam 对 STS2 的下载/更新状态。

    StateFlags 含义（bitmask）：
      1   = installed（已安装）
      2   = needs update（需要更新）
      4   = downloading / update in progress（下载中）
      8   = unpacking（解压中）
      16  = verifying（校验中）
      32  = pre-allocating
      4096 = mount update

    返回 None 表示找不到 manifest。
    """
    manifest_info = detect_steam_beta_key()
    if manifest_info is None:
        return None

    manifest_path: Path = manifest_info["appmanifest_path"]
    try:
        text = manifest_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    def _field(name: str, text: str) -> str:
        m = re.search(rf'"{name}"\s+"([^"]*)"', text)
        return m.group(1) if m else ""

    state_flags = int(_field("StateFlags", text) or "0")
    bytes_to_download = int(_field("BytesToDownload", text) or "0")
    bytes_downloaded = int(_field("BytesDownloaded", text) or "0")
    bytes_to_stage = int(_field("BytesToStage", text) or "0")
    bytes_staged = int(_field("BytesStaged", text) or "0")
    update_result = int(_field("UpdateResult", text) or "0")

    # 状态判断
    is_installed = bool(state_flags & 1) and not (state_flags & 2) and not (state_flags & 4)
    is_downloading = bool(state_flags & 4) or bytes_downloaded > 0
    progress = (bytes_downloaded / bytes_to_download * 100) if bytes_to_download > 0 else 0

    return {
        "beta_key": manifest_info["beta_key"],
        "state_flags": state_flags,
        "is_installed": is_installed,
        "is_downloading": is_downloading,
        "bytes_to_download": bytes_to_download,
        "bytes_downloaded": bytes_downloaded,
        "progress": round(progress, 1),
        "update_result": update_result,
    }


def _is_steam_running() -> bool:
    """检测 Steam.exe / steamservice.exe 是否在运行。"""
    # 1) 优先 WMI（最快、最准，不需要 psutil）
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name='Steam.exe' or name='SteamService.exe' or name='steamwebhelper.exe'",
             "get", "name"],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout.lower()
        if "steam.exe" in output or "steamservice.exe" in output or "steamwebhelper.exe" in output:
            return True
    except Exception:
        pass

    # 2) tasklist 兜底（注意：Windows 返回的进程名是小写！）
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Steam.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
        # tasklist 返回形如 "steam.exe","14952",...  （小写）
        if "steam.exe" in result.stdout.lower():
            return True
        # 也检查 SteamService
        result2 = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SteamService.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
        if "steamservice.exe" in result2.stdout.lower():
            return True
    except Exception:
        pass
    return False


def _find_steam_exe() -> Optional[Path]:
    """尽量找到 Steam.exe 的路径，用于关闭后重启。"""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        val, _ = winreg.QueryValueEx(key, "SteamExe")
        if val:
            return Path(str(val))
    except Exception:
        pass
    # 常见路径兜底
    for p in [
        Path(r"C:\Program Files (x86)\Steam\Steam.exe"),
        Path(r"C:\Program Files\Steam\Steam.exe"),
    ]:
        if p.is_file():
            return p
    return None


def _shutdown_steam(timeout_sec: int = 30) -> bool:
    """优雅关闭 Steam：先发 exit 协议，超时后 taskkill。返回是否成功退出。"""
    # 1) 尝试 steam://exit 协议（优雅退出）
    try:
        os.startfile("steam://exit")  # type: ignore[attr-defined]
    except Exception:
        pass

    # 2) 等待退出
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not _is_steam_running():
            return True
        time.sleep(1)

    # 3) 强制杀进程
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "Steam.exe", "/T"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    time.sleep(2)
    return not _is_steam_running()


def _find_running_game_process() -> Optional[dict]:
    """检测 STS2 游戏进程是否在运行；返回 {"pid": int, "path": str} 或 None。

    优先 psutil（准确、能拿到 exe_path 做 path 校验）；失败时用 tasklist 命令兜底。
    """
    GAME_PROCESS_NAMES = ("SlayTheSpire2.exe", "Slay the Spire 2.exe")

    # 1) 优先 psutil
    try:
        import psutil  # type: ignore
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                exe = proc.info.get("exe") or ""
                if name in GAME_PROCESS_NAMES or (exe and exe.lower().endswith("slaythespire2.exe")):
                    return {"pid": proc.info["pid"], "path": exe}
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass
    except Exception:
        pass

    # 2) 兜底：tasklist /FI "IMAGENAME eq SlayTheSpire2.exe"
    try:
        import subprocess
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SlayTheSpire2.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            if not line.strip() or line.startswith("INFO:") or line.startswith("错误:"):
                continue
            # CSV 格式: "SlayTheSpire2.exe","12345","Console","1","123,456 K"
            parts = [p.strip('"') for p in line.split('","')]
            if parts and parts[0].lower() == "slaythespire2.exe":
                try:
                    pid = int(parts[1].strip('"'))
                except (IndexError, ValueError):
                    continue
                return {"pid": pid, "path": ""}
    except Exception:
        pass

    return None


# --------------------------------------------------------------------------- #
# Steam / game install detection
# --------------------------------------------------------------------------- #


def _read_steam_root_windows() -> Optional[Path]:
    """Windows 注册表三种路径依次尝试：HKLM WOW6432 / HKLM native / HKCU."""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore
    except Exception:
        return None

    probes = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
    ]
    for hive, subkey, value_name in probes:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                path, _ = winreg.QueryValueEx(key, value_name)
                p = Path(str(path))
                if p.is_dir():
                    return p
        except Exception:
            continue
    return None


def _parse_library_folders(vdf_path: Path) -> list[Path]:
    """解析 Steam libraryfolders.vdf，返回所有库根目录，优先含 STS2 的放在前面。"""
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    prioritized: list[Path] = []
    normal: list[Path] = []
    current_path: Optional[str] = None
    has_app = False

    for line in text.splitlines():
        s = line.strip()
        if s.startswith('"path"'):
            # "path"  "D:\\SteamLibrary"
            parts = [x for x in s.split('"') if x]
            if len(parts) >= 2:
                current_path = parts[-1].replace("\\\\", "\\").replace("\\/", "\\")
                has_app = False
        if current_path:
            for app_id in STEAM_APP_ID_VARIANTS:
                if f'"{app_id}"' in s:
                    has_app = True
                    break
        if s == "}" and current_path:
            p = Path(current_path)
            if p.is_dir():
                if has_app:
                    prioritized.append(p)
                else:
                    normal.append(p)
            current_path = None
            has_app = False

    return prioritized + normal


def _candidate_steam_roots() -> list[Path]:
    """注册表 + 常见硬编码候选路径。"""
    roots: list[Path] = []
    reg = _read_steam_root_windows()
    if reg:
        roots.append(reg)

    # 默认 C:\Program Files (x86)\Steam / C:\Program Files\Steam
    for env_key in ("ProgramFiles(x86)", "ProgramFiles"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v) / "Steam"
            if p.is_dir():
                roots.append(p)

    for drive_letter in "CDEFGH":
        for tail in (f"{drive_letter}:\\Steam", f"{drive_letter}:\\SteamLibrary"):
            p = Path(tail)
            if p.is_dir():
                roots.append(p)

    # 去重保持顺序
    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(r.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def detect_game_install(manual_game_root: Optional[Path] = None) -> Optional[GameInstall]:
    """找到 STS2 安装目录；找不到返回 None。"""
    # 用户手动指定优先
    game_roots: list[Path] = []
    if manual_game_root and (Path(manual_game_root) / GAME_EXE_NAME).is_file():
        game_roots.append(Path(manual_game_root))

    # 从每个 Steam 库路径找 common/Slay the Spire 2
    for steam_root in _candidate_steam_roots():
        libs: list[Path] = [steam_root]
        vdf = steam_root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            libs = [steam_root] + _parse_library_folders(vdf)
        for lib in libs:
            candidate = lib / "steamapps" / "common" / GAME_FOLDER_NAME
            if (candidate / GAME_EXE_NAME).is_file() and candidate not in game_roots:
                game_roots.append(candidate)

    # 扫不到就再找一遍常见非标准目录
    if not game_roots:
        for drive in "CDEFGH":
            for tail in (
                f"{drive}:\\Games\\{GAME_FOLDER_NAME}",
                f"{drive}:\\{GAME_FOLDER_NAME}",
            ):
                c = Path(tail)
                if (c / GAME_EXE_NAME).is_file():
                    game_roots.append(c)

    if not game_roots:
        return None

    game_root = game_roots[0]

    def _pick_existing(priority: tuple[str, ...]) -> Path:
        for name in priority:
            p = game_root / name
            if p.is_dir():
                return p
        # 都不存在就取优先级第一个（soft_mkdir 会尝试创建）
        return game_root / priority[0]

    mods_dir = _pick_existing(MODS_DIR_VARIANTS)
    disabled_dir = _pick_existing(DISABLED_DIR_VARIANTS)

    # 收集所有已存在的 disabled 目录变体（防止用户电脑上 .mods_disabled 和 mods_disabled 并存只扫一个）
    disabled_dirs: list[Path] = []
    for name in DISABLED_DIR_VARIANTS:
        p = game_root / name
        if p.is_dir() and p not in disabled_dirs:
            disabled_dirs.append(p)
    # 确保主 disabled_dir 也在列表里（即使它是不存在但会被 soft_mkdir 创建的）
    if disabled_dir not in disabled_dirs:
        disabled_dirs.insert(0, disabled_dir)

    def _soft_mkdir(p: Path) -> None:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            # 沙箱或权限不够时，创建失败也不影响扫描/展示
            pass

    _soft_mkdir(mods_dir)
    _soft_mkdir(disabled_dir)

    # 工坊目录：在「所有已知的 Steam 库根目录」下，尝试两个常见 AppID（官方 2868840 和备用 2865400）
    # 为什么两个都要？真实世界里 Workshop 的 workshop/content/<AppID> 的 AppID 跟商店页的
    # rungameid AppID 并不一定一致（有早期/备用 AppID），两个都命中命中率最高。
    workshop_dir: Optional[Path] = None
    workshop_candidates: list[Path] = []
    # 1) 来自 _candidate_steam_roots() 的所有 Steam 安装根 / 库硬编码根
    for root in _candidate_steam_roots():
        for app_id in STEAM_APP_ID_VARIANTS:
            p = root / "steamapps" / "workshop" / "content" / app_id
            workshop_candidates.append(p)
    # 2) 从 game_root 直接反推库根：
    #    game_root = <lib>/steamapps/common/<GAME_FOLDER_NAME>
    #      parent          = common
    #      parent.parent   = steamapps
    #      parent.parent.parent = <lib>  ← 库根
    #    然后在 <lib>/steamapps/workshop/content/<AppID> 找工坊。
    try:
        common_dir = game_root.resolve().parent
        steamapps_dir = common_dir.parent
        lib_from_game = steamapps_dir.parent
        if common_dir.name == "common" and steamapps_dir.name == "steamapps" and lib_from_game.is_dir():
            for app_id in STEAM_APP_ID_VARIANTS:
                p = lib_from_game / "steamapps" / "workshop" / "content" / app_id
                workshop_candidates.insert(0, p)  # 反推出来的库放最前面（优先级最高）
    except Exception:
        pass
    # 3) 如果用户指定了 manual_game_root，那我们也尝试 game_root 自身所在盘的各种 Steam 变体（已经由上面的硬编码覆盖）
    # 统一：先扫 libraryfolders.vdf 解析出的所有库根 → 从每个根拼 workshop/content/<AppID>
    #    这里再做一次兜底：把 detect_game_install 循环里 _parse_library_folders 的结果作为候选
    for steam_root in _candidate_steam_roots():
        vdf = steam_root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            for lib in _parse_library_folders(vdf):
                for app_id in STEAM_APP_ID_VARIANTS:
                    p = lib / "steamapps" / "workshop" / "content" / app_id
                    workshop_candidates.append(p)

    # 去重 + 命中即选第一个 is_dir
    seen_ws: set[str] = set()
    for cand in workshop_candidates:
        try:
            key = str(cand.resolve()).lower()
        except Exception:
            key = str(cand).lower()
        if key in seen_ws:
            continue
        seen_ws.add(key)
        if cand.is_dir():
            workshop_dir = cand
            break

    # P2: 同时检查 Steam 测试版分支目录（workshop/content/<AppID>_beta）
    # 结构：workshop/content/2868840_beta/ 或 workshop/content/2865400_beta/
    workshop_beta_dir: Optional[Path] = None
    workshop_beta_candidates: list[Path] = []
    # 沿用上面 workshop_dir 的所有候选根目录（已在 seen_ws 循环前构建好）
    for app_id in STEAM_APP_ID_VARIANTS:
        beta_dir = workshop_dir.parent / f"{app_id}_beta" if workshop_dir else None
        # 直接从所有 steam root 拼 beta 目录
        for root in _candidate_steam_roots():
            vdf = root / "steamapps" / "libraryfolders.vdf"
            libs = [root]
            if vdf.is_file():
                libs = [root] + _parse_library_folders(vdf)
            for lib in libs:
                workshop_beta_candidates.append(lib / "steamapps" / "workshop" / "content" / f"{app_id}_beta")
    seen_beta: set[str] = set()
    for cand in workshop_beta_candidates:
        try:
            key = str(cand.resolve()).lower()
        except Exception:
            key = str(cand).lower()
        if key in seen_beta:
            continue
        seen_beta.add(key)
        if cand.is_dir():
            workshop_beta_dir = cand
            break

    game = GameInstall(
        game_root=game_root,
        exe_path=game_root / GAME_EXE_NAME,
        mods_dir=mods_dir,
        disabled_dir=disabled_dir,
        disabled_dirs=disabled_dirs,
        workshop_dir=workshop_dir,
        workshop_beta_dir=workshop_beta_dir,
        detected_by="workshop_registry" if manual_game_root is None else "manual",
    )

    # 填充游戏版本号（从 godot.log 解析）和 Steam Beta 分支（从 appmanifest 解析）
    try:
        detected = detect_game_version_from_log()
        if detected:
            game.version = detected["version"]
            game.version_detected = True
    except Exception:
        pass
    try:
        beta_info = detect_steam_beta_key(game_root)
        if beta_info:
            game.beta_key = beta_info["beta_key"]
            game.beta_label = beta_info["beta_label"]
            game.steam_appmanifest_path = beta_info.get("appmanifest_path")
    except Exception:
        pass

    return game


# --------------------------------------------------------------------------- #
# Manifest parsing (wakaka6 同款优先级策略：<FolderName>.json -> mod_manifest.json -> 任何 json)
# --------------------------------------------------------------------------- #


def _parse_tags(raw) -> list[str]:
    """把 manifest 的 tags 字段归一化成字符串列表（兼容数组 / 逗号分隔字符串）。"""
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                out.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                out.append(item["id"].strip())
    elif isinstance(raw, str):
        for part in re.split(r"[,;|]", raw):
            part = part.strip()
            if part:
                out.append(part)
    return [t for t in out if t]


def _try_parse_manifest_json(raw_text: str) -> Optional[ModManifest]:
    # 去 BOM
    if raw_text.startswith("\ufeff"):
        raw_text = raw_text[1:]
    try:
        data = json.loads(raw_text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # 必填字段严格校验（4 个必须都存在）
    for k in MANIFEST_REQUIRED_KEYS:
        if k not in data:
            return None
    mid = str(data.get("id") or "").strip()
    mname = str(data.get("name") or "").strip()
    if not mid or not mname:
        return None
    # 前置依赖：兼容两种写法
    #   1) 字符串数组  ["BaseLib", "STS2-RitsuLib"]
    #   2) 对象数组    [{"id": "STS2-RitsuLib", "min_version": "0.5.12"}]
    deps: list[str] = []
    deps_raw = data.get("dependencies")
    if isinstance(deps_raw, list):
        for d in deps_raw:
            if isinstance(d, str):
                deps.append(d)
            elif isinstance(d, dict) and isinstance(d.get("id"), str):
                deps.append(d["id"])
    # 兼容多种 URL 字段名
    url_val = data.get("url") or data.get("homepage") or data.get("repository") or data.get("source_url")
    url_val = str(url_val).strip() if url_val else None
    if url_val and not (url_val.startswith("http://") or url_val.startswith("https://")):
        url_val = None
    return ModManifest(
        id=mid,
        name=mname,
        version=data.get("version") if data.get("version") is not None else None,
        author=data.get("author") if data.get("author") is not None else None,
        description=data.get("description") if data.get("description") is not None else None,
        dependencies=deps,
        affects_gameplay=bool(data.get("affects_gameplay", False)),
        has_pck=bool(data.get("has_pck", False)),
        has_dll=bool(data.get("has_dll", False)),
        tags=_parse_tags(data.get("tags")),
        url=url_val,
        min_game_version=(str(data["min_game_version"]).strip() if data.get("min_game_version") else None),
        max_game_version=(str(data["max_game_version"]).strip() if data.get("max_game_version") else None),
    )


def find_manifest(mod_dir: Path, allow_disabled: bool = False) -> Optional[Path]:
    """返回 mod 目录里的 manifest 路径。
    allow_disabled=True 时，同时识别被我们加了 .disabled 后缀的 manifest（表示用户停用了这个 mod）。
    递归搜索子目录——workshop mod 的 manifest 常在子目录里（如 BaseLib/BaseLib.json）。
    """
    folder = mod_dir.name

    # 收集所有候选文件（递归）
    def _collect_json_files(root: Path):
        results = []
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                for fn in filenames:
                    full = Path(dirpath) / fn
                    if fn == 'mod_manifest.json':
                        results.append((1, full))  # 最高优先
                    elif fn.endswith('.json') and not fn.endswith('.json.disabled'):
                        results.append((2, full))
                    elif fn.endswith('.json.disabled'):
                        results.append((3, full))
        except Exception:
            pass
        return results

    # 正常 manifest 优先级最高
    candidates = _collect_json_files(mod_dir)
    candidates.sort(key=lambda x: x[0])  # mod_manifest.json 优先
    for _, candidate in candidates:
        name = candidate.name
        if name == 'mod_manifest.json':
            pass  # 最高优先
        elif name.endswith('.json.disabled'):
            if not allow_disabled:
                continue
        elif name.endswith('.json'):
            if allow_disabled:
                continue  # 只找 disabled
        else:
            continue
        try:
            txt = candidate.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _try_parse_manifest_json(txt) is not None:
            return candidate

    return None


def read_mod_manifest(mod_dir: Path, allow_disabled: bool = False) -> Optional[ModManifest]:
    path = find_manifest(mod_dir, allow_disabled=allow_disabled)
    if path is None:
        return None
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    return _try_parse_manifest_json(txt)


def _scan_folder(
    mod_dir: Path,
    source: str,
    workshop_id: Optional[str] = None,
    allow_disabled_manifest: bool = False,
) -> Optional[ModInfo]:
    """扫一个 mod 文件夹。
    allow_disabled_manifest=True 时扫不出正常 manifest 会尝试 .disabled 结尾的（返回 source 保持调用方传入，方便 workshop 停用态识别）。

    install_dir 取 manifest 实际所在目录（find_manifest 递归搜索的结果），
    不再硬编码成传入的 mod_dir——这对三层嵌套的 mod（如 SnakeMapMod）很关键，
    同时也让二次补扫 wid 根目录时自动指向子目录，合并重复副本。
    """
    mf_path = find_manifest(mod_dir, allow_disabled=allow_disabled_manifest)
    if mf_path is None:
        return None
    try:
        txt = mf_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    manifest = _try_parse_manifest_json(txt)
    if manifest is None:
        return None
    # 2026-11-07 Step 1: stat() 一次性固化 mtime/ctime（隐藏问题 2）
    try:
        _st = mf_path.parent.stat()
        _mt = _st.st_mtime
        _ct = _st.st_ctime
    except OSError:
        _mt = 0.0
        _ct = 0.0
    return ModInfo(
        manifest=manifest,
        folder_name=mf_path.parent.name,
        install_dir=mf_path.parent,
        source=source,
        workshop_id=workshop_id,
        mtime=_mt,
        ctime=_ct,
    )


# --------------------------------------------------------------------------- #
# Workshop mod 启用/停用：不动目录，只改 manifest 后缀
#   停用: xxx.json -> xxx.json.disabled   (STS2 扫描不到合法 manifest = 不加载)
#   启用: xxx.json.disabled -> xxx.json   (恢复识别)
# 这样 Steam 校验 Workshop 完整性不会触发重新下载（目录结构和文件大小几乎没变）。
# --------------------------------------------------------------------------- #


def is_workshop_mod_manifest_disabled(mod_dir: Path) -> Optional[Path]:
    """如果 mod 目录里存在 ".disabled" 的 manifest，返回它的 Path；否则 None。"""
    # 直接找 .disabled 文件，不等同于"找不到正常的 manifest"（因为可能根本没装对）
    folder = mod_dir.name
    for base in (f"{folder}.json.disabled", "mod_manifest.json.disabled"):
        p = mod_dir / base
        if p.is_file():
            try:
                if _try_parse_manifest_json(p.read_text(encoding="utf-8", errors="ignore")) is not None:
                    return p
            except Exception:
                pass
    try:
        for entry in sorted(mod_dir.iterdir()):
            if entry.is_file() and entry.name.lower().endswith(".json.disabled"):
                try:
                    if _try_parse_manifest_json(entry.read_text(encoding="utf-8", errors="ignore")) is not None:
                        return entry
                except Exception:
                    pass
    except Exception:
        pass
    return None


def set_workshop_mod_disabled(info: ModInfo, disabled: bool, game: Optional["GameInstall"] = None) -> ModInfo:
    """切换 Workshop mod 的启用状态：改 manifest 后缀名。

    **关键修复**：当传入 game（有 workshop_dir）且 info.workshop_id 存在时，
    优先操作 workshop/content/<AppID>/<workshop_id> 真源目录的 manifest，
    因为 STS2 默认目录驱动会**直接扫描 workshop 源目录**——仅改 Mods/ 下的 stub
    manifest 完全无效，游戏还是会从源目录扫到正常 manifest 就直接加载。
    同时也会同步改 info.install_dir（如果是 Mods/ 下的 stub 路径）的 manifest
    后缀，保证 stub 状态与源目录一致（双保险）。

    disabled=True:  停用 = 把 .json  重命名成 .json.disabled
    disabled=False: 启用 = 把 .json.disabled 重命名回 .json
    返回更新后的 ModInfo（source 字段会变成 "workshop" / "disabled"）。
    """
    from .logger import get_logger

    log = get_logger("scanner.workshop")

    # 定位要操作的目录列表（按优先级从高到低）：
    #   1) workshop 源目录（如果能定位到）—— 必改，否则 STS2 直接扫源目录就加载了
    #   2) info.install_dir（stub 或其他扫描时命中的路径）—— 同步改，保持一致
    targets: list[tuple[Path, str]] = []  # (dir, tag)
    if game is not None and game.workshop_dir is not None and info.workshop_id:
        origin_dir = game.workshop_dir / str(info.workshop_id)
        if origin_dir.is_dir():
            # 源目录不在 targets 首位——先改它（最重要的）
            targets.append((origin_dir, "ws-origin"))
    # info.install_dir 如果不等于源目录，也加上（避免重复改同一个目录）
    if info.install_dir is not None and info.install_dir.is_dir():
        already_added = any(t.resolve() == info.install_dir.resolve() for t, _ in targets)
        if not already_added:
            targets.append((info.install_dir, "info.install_dir"))
    if not targets:
        log.warning("set_workshop_mod_disabled: 没有任何可操作的目标目录! info=%s game=%s", info, game)
        return info

    def _rename(src: Path, dst: Path) -> None:
        if dst.exists():
            bak = dst.with_name(dst.name + f".bak-{int(time.time())}")
            log.info("set_workshop_mod_disabled: 目标 %s 已存在，备份为 %s", dst, bak)
            shutil.move(str(dst), str(bak))
        log.info("set_workshop_mod_disabled: %s -> %s (disabled=%s)", src, dst, disabled)
        shutil.move(str(src), str(dst))

    def _process_one_dir(work_dir: Path, tag: str) -> None:
        folder = work_dir.name
        if disabled:
            normal = find_manifest(work_dir, allow_disabled=False)
            if normal is None:
                log.info("set_workshop_mod_disabled[%s]: %s 无正常 manifest，已是停用态或损坏", tag, work_dir)
                return
            disabled_path = normal.with_name(normal.name + ".disabled")
            _rename(normal, disabled_path)
        else:
            dis = is_workshop_mod_manifest_disabled(work_dir)
            if dis is None:
                log.info("set_workshop_mod_disabled[%s]: %s 无 .disabled manifest，已是启用态或损坏", tag, work_dir)
                return
            if dis.name.lower().endswith(".json.disabled"):
                new_name = dis.name[: -len(".disabled")]
            else:
                new_name = dis.stem
            restored = dis.with_name(new_name)
            _rename(dis, restored)

    for work_dir, tag in targets:
        try:
            _process_one_dir(work_dir, tag)
        except Exception as e:
            log.error("set_workshop_mod_disabled[%s]: 处理 %s 失败: %s", tag, work_dir, e)

    # 返回的 ModInfo 以 info.install_dir 为准重新读
    final_dir = info.install_dir if (info.install_dir and info.install_dir.is_dir()) else targets[0][0]
    allow_dis = disabled
    new_source: str
    if not allow_dis:
        mf_norm = find_manifest(final_dir, allow_disabled=False)
        new_source = "workshop" if mf_norm is not None else "disabled"
    else:
        mf_norm = find_manifest(final_dir, allow_disabled=False)
        mf_dis = find_manifest(final_dir, allow_disabled=True)
        if mf_norm is not None:
            new_source = "workshop"
        elif mf_dis is not None:
            new_source = "disabled"
        else:
            new_source = info.source
    manifest = read_mod_manifest(final_dir, allow_disabled=True)
    if manifest is None:
        log.error("set_workshop_mod_disabled: 处理完后 %s 读不到 manifest，返回原 info", final_dir)
        return info
    # 2026-11-07 Step 1: stat() 固化 mtime/ctime
    try:
        _st = final_dir.stat()
        _mt = _st.st_mtime
        _ct = _st.st_ctime
    except OSError:
        _mt = 0.0
        _ct = 0.0
    return ModInfo(
        manifest=manifest,
        folder_name=final_dir.name,
        install_dir=final_dir,
        source=new_source,
        workshop_id=info.workshop_id,
        mtime=_mt,
        ctime=_ct,
    )


# =========================================================================== #
# Native Baseline: capture / restore Steam 原生 mod 加载状态快照
#   目的：让管理器和 Steam 原生配置完全隔离——
#       capture 记录"当前原生状态"，restore 把物理文件精确还原到那个状态，
#       这样 Steam 直接启动时看到的就是未经管理器触碰的原生状态。
#   只记录 / 还原被我们**可能改过**的东西：
#       - workshop/content/<AppID>/<id>/ 里的 manifest 后缀
#       - Mods/ 目录里的 stub（管理器种的原生状态应该为空）
#       - settings.save 的 mod_settings 块
# =========================================================================== #


def capture_native_baseline(game, settings_save_path: Path, baseline_path: Path) -> dict:
    """拍快照。会检测并修复 dual-manifest 残留。

    game: GameInstall 实例（有 .game_root / .workshop_dir / .mods_dir / .disabled_dirs）
    settings_save_path: Path 到 STS2 的 settings.save
    baseline_path: Path 到我们的 baseline.json（通常是 data/baseline/native.json）
    """
    import json as _json
    import shutil as _shutil
    from datetime import datetime

    baseline: dict = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "game_root": str(game.game_root) if game.game_root else None,
        "workshop_dir": str(game.workshop_dir) if game.workshop_dir else None,
        "mods_dir": str(game.mods_dir) if game.mods_dir else None,
        "workshop_mods": {},  # workshop_id -> {"active": relpath or null, "disabled": relpath or null}
        "mods_dir_entries": [],  # Mods/ 下的目录名列表
        "settings_save": None,  # 完整 settings.save 内容
    }

    # 1) workshop 目录状态
    if game.workshop_dir is not None and game.workshop_dir.is_dir():
        for pub_dir in sorted(game.workshop_dir.iterdir()):
            if not pub_dir.is_dir():
                continue
            entry: dict = {"active": None, "disabled": None, "dual_fixed": False}

            # 和 scan_all_mods 一样处理嵌套：先看 pub_dir 顶层有没有 manifest，
            # 如果没有，看 pub_dir 的子目录有没有（比如 BaseLib/BaseLib.json 嵌套一层）
            manifest_dir = pub_dir
            real_active = None
            real_disabled = None
            # 1) 顶层找 active manifest
            real_active = find_manifest(pub_dir, allow_disabled=False)
            if real_active is None:
                # 顶层没 active → 顶层有没有 disabled？
                real_disabled = is_workshop_mod_manifest_disabled(pub_dir)
                if real_disabled is None:
                    # 两个都在顶层没找到 → 尝试子目录
                    for sd in [d for d in pub_dir.iterdir() if d.is_dir()]:
                        real_active = find_manifest(sd, allow_disabled=False)
                        if real_active is not None:
                            manifest_dir = sd
                            break
                        real_disabled = find_manifest(sd, allow_disabled=True)
                        if real_disabled is not None:
                            manifest_dir = sd
                            break
                else:
                    # 顶层找到了 disabled，还要看子目录有没有 active（dual-manifest 场景）
                    for sd in [d for d in pub_dir.iterdir() if d.is_dir()]:
                        real_active_sub = find_manifest(sd, allow_disabled=False)
                        if real_active_sub is not None:
                            real_active = real_active_sub
                            manifest_dir = sd
                            break
            else:
                # 顶层找到了 active，再看有没有 disabled（子目录或顶层）
                for sd in [d for d in pub_dir.iterdir() if d.is_dir()]:
                    real_disabled_sub = find_manifest(sd, allow_disabled=True)
                    if real_disabled_sub is not None:
                        real_disabled = real_disabled_sub
                        break
                if real_disabled is None:
                    real_disabled = is_workshop_mod_manifest_disabled(pub_dir)
            if real_disabled is None:
                real_disabled = is_workshop_mod_manifest_disabled(manifest_dir)

            if real_active is not None:
                entry["active"] = str(real_active.relative_to(pub_dir))
            if real_disabled is not None:
                entry["disabled"] = str(real_disabled.relative_to(pub_dir))

            # Dual-manifest 清理：同时有 active 和 disabled manifest → 删掉 disabled
            if entry["active"] and entry["disabled"]:
                try:
                    disabled_abs = pub_dir / entry["disabled"]
                    if disabled_abs.is_file():
                        _shutil.move(str(disabled_abs), str(disabled_abs) + f".bak-{datetime.now().strftime('%H%M%S')}")
                        entry["dual_fixed"] = True
                except Exception:
                    pass

            baseline["workshop_mods"][pub_dir.name] = entry

    # 2) Mods/ 目录：baseline 时记录有什么，restore 时清空
    if game.mods_dir is not None and game.mods_dir.is_dir():
        baseline["mods_dir_entries"] = sorted([d.name for d in game.mods_dir.iterdir() if d.is_dir()])

    # 3) settings.save：完整快照
    if settings_save_path.is_file():
        try:
            baseline["settings_save"] = _json.loads(settings_save_path.read_text(encoding="utf-8"))
        except Exception:
            baseline["settings_save"] = None

    # 写 baseline 文件
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    # 先写 .tmp 再 os.replace 原子替换（Windows 下 shutil.move 不能覆盖已存在目标）
    tmp = baseline_path.with_suffix(baseline_path.suffix + ".tmp")
    tmp.write_text(_json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(baseline_path))

    from .logger import get_logger
    get_logger("scanner.baseline").info(
        "capture_native_baseline: workshop=%d entries, mods_dir=%d entries, settings_save=%s, dual_fixed=%d",
        len(baseline["workshop_mods"]),
        len(baseline["mods_dir_entries"]),
        "ok" if baseline["settings_save"] else "missing",
        sum(1 for e in baseline["workshop_mods"].values() if e.get("dual_fixed")),
    )
    return baseline


def restore_native_baseline(game, settings_save_path: Path, baseline_path: Path) -> None:
    """把物理文件精确还原到 baseline 记录的状态。

    幂等：目标状态已是 baseline 状态时，不做任何改动。
    """
    import json as _json
    import shutil as _shutil

    if not baseline_path.is_file():
        from .logger import get_logger
        get_logger("scanner.baseline").warning("restore_native_baseline: baseline 文件不存在 %s", baseline_path)
        return

    baseline = _json.loads(baseline_path.read_text(encoding="utf-8"))

    from .logger import get_logger
    log = get_logger("scanner.baseline")

    # 1) 还原 workshop manifest 后缀
    if game.workshop_dir is not None and game.workshop_dir.is_dir():
        for ws_id, entry in baseline.get("workshop_mods", {}).items():
            pub_dir = game.workshop_dir / ws_id
            if not pub_dir.is_dir():
                continue
            target_active = entry.get("active")  # relpath or None
            target_disabled = entry.get("disabled")  # relpath or None

            # 先看当前状态（也要考虑嵌套目录！）
            # 和 capture 一样的 manifest_dir 定位
            manifest_dir = pub_dir
            cur_active = find_manifest(pub_dir, allow_disabled=False)
            cur_disabled = is_workshop_mod_manifest_disabled(pub_dir)
            # 顶层没找到，看子目录
            if cur_active is None and cur_disabled is None:
                for sd in [d for d in pub_dir.iterdir() if d.is_dir()]:
                    if cur_active is None:
                        cur_active = find_manifest(sd, allow_disabled=False)
                    if cur_disabled is None:
                        cur_disabled_sub = find_manifest(sd, allow_disabled=True)
                        if cur_disabled_sub is not None:
                            cur_disabled = cur_disabled_sub
                    if cur_active is not None or cur_disabled is not None:
                        manifest_dir = sd
                        break

            # 目标：DISABLED (有 .json.disabled，无正常 .json)
            if target_disabled and not target_active:
                if cur_disabled is not None and cur_active is None:
                    continue  # 已是目标状态
                if cur_active is not None:
                    # 需要把 active → disabled
                    dis_path = cur_active.with_name(cur_active.name + ".disabled")
                    log.info("[restore] workshop/%s: %s → .json.disabled", ws_id, cur_active.name)
                    _shutil.move(str(cur_active), str(dis_path))
                # 清理意外的 disabled（如果目标是 active 但有残留 disabled）
                if cur_disabled is not None and cur_disabled.name != (target_disabled.split("\\")[-1] if "\\" in target_disabled else target_disabled.split("/")[-1]):
                    # disabled 的名字和目标不一样——可能是之前改了 manifest 文件名，不太可能，跳过
                    pass

            # 目标：ACTIVE (有正常 .json，无 .json.disabled)
            elif target_active and not target_disabled:
                if cur_active is not None and cur_disabled is None:
                    continue  # 已是目标状态
                if cur_disabled is not None:
                    # 需要把 disabled → active
                    restored_name = cur_disabled.name[: -len(".disabled")] if cur_disabled.name.lower().endswith(".disabled") else cur_disabled.name
                    restored = cur_disabled.with_name(restored_name)
                    log.info("[restore] workshop/%s: %s → %s", ws_id, cur_disabled.name, restored.name)
                    _shutil.move(str(cur_disabled), str(restored))

            # 目标：都没有（可能是没有 manifest 的 mod）
            elif not target_active and not target_disabled:
                # 不需要还原什么，skip
                pass

            # Dual-manifest：目标同时有 active 和 disabled？这意味着 baseline 本身有 dual-fix
            # 这种情况极少，skip

    # 2) 清空 Mods/ 下的 stub（baseline 里记录的条目之外的都删掉）
    #    baseline 里的 mods_dir_entries 是"capture 时就存在的"——通常应该是空的
    baseline_mods = set(baseline.get("mods_dir_entries", []))
    if game.mods_dir is not None and game.mods_dir.is_dir():
        for entry in list(game.mods_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name in baseline_mods:
                continue  # capture 时就存在，保留
            # 管理器种的 stub → 删掉
            try:
                _shutil.rmtree(str(entry))
                log.info("[restore] Mods/: 删除 stub %s", entry.name)
            except Exception as e:
                log.warning("[restore] Mods/: 删除 stub %s 失败: %s", entry.name, e)

    # 3) 还原 settings.save（完整覆盖）
    saved = baseline.get("settings_save")
    if saved is not None:
        # 比较一下避免无意义写
        current = None
        try:
            current = _json.loads(settings_save_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        if current is None or _json.dumps(current, sort_keys=True) != _json.dumps(saved, sort_keys=True):
            tmp = settings_save_path.with_suffix(settings_save_path.suffix + ".bak")
            if settings_save_path.is_file():
                _shutil.copy2(str(settings_save_path), str(tmp))
            # 原子写
            tmp2 = settings_save_path.with_suffix(settings_save_path.suffix + ".tmp")
            tmp2.write_text(_json.dumps(saved, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp2), str(settings_save_path))
            log.info("[restore] settings.save 已还原 (之前备份 .bak)")
        else:
            log.info("[restore] settings.save 已是 baseline 状态，不写")

    log.info("restore_native_baseline 完成")



# Workshop mod 启用/停用（v2：绝不改动 workshop 目录本身，只操作用户可写目录）
#   启用：在 Mods/ 下创建一个 stub 文件夹，以"硬链接（优先）/复制"方式把 workshop 原始目录里
#        所有文件都拉到 stub 里，STS2 扫描 Mods/ 时会读到这个 stub 就等于加载了该 mod。
#   停用：把 stub 文件夹从 Mods/ 移到 .mods_disabled/（与本地 mod 停用逻辑一致）。
#   排序：enabled_order 里只存 mod_id（local+workshop 统一列表），再写回 Mods/order.json。
#   优点：
#     1. Steam Workshop 目录（workshop/content/<AppID>）完全只读对待，Steam 更新/校验不会冲突
#     2. 即使以后换电脑/重新下载 Workshop，只要 Stub 链接/复制还在，排序/启用态保持不变
#     3. Stub 目录命名里带 workshop_id，不会和本地同名 mod 冲突
# --------------------------------------------------------------------------- #


def workshop_stub_folder_name(info: ModInfo) -> str:
    """Stable folder name for the stub placed in Mods/ (or disabled dir)."""
    if info.workshop_id:
        return f"workshop-{info.workshop_id}"
    # 罕见：没记录 workshop_id，但 source 是 workshop（从 Mods/ 已有的旧 stub 扫回来或其它路径）
    return f"workshop-{info.mod_id}"


def _link_or_copy_file(src: Path, dst: Path) -> None:
    """把 src 文件"复制"到 dst；优先尝试 NTFS 硬链接（省空间且原子），失败就整文件复制。
    调用方必须确保 dst.parent 存在。
    """
    if dst.exists():
        return  # 已存在就跳过，允许增量更新
    # 硬链接：同卷 NTFS 可用，跨盘 / FAT32 会失败 → 退化成普通复制
    try:
        os.link(str(src), str(dst))
        return
    except Exception:
        pass
    shutil.copy2(str(src), str(dst))


def _populate_workshop_stub(src_root: Path, dst_root: Path) -> None:
    """把 src_root 下所有文件/子目录复制/硬链接到 dst_root（保持相对结构）。"""
    if not src_root.is_dir():
        return
    dst_root.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src_root.rglob("*")):
        try:
            rel = entry.relative_to(src_root)
        except Exception:
            continue
        target = dst_root / rel
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if entry.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            _link_or_copy_file(entry, target)


def ensure_workshop_stub_enabled(info: ModInfo, game: GameInstall) -> ModInfo:
    """启用 Workshop mod：在 game.mods_dir 下创建/补齐 stub，成功后返回新的基于 stub 的 ModInfo。
    不修改任何 workshop 目录里的文件。
    """
    from .logger import get_logger

    log = get_logger("scanner.workshop.stub")
    stub_name = workshop_stub_folder_name(info)
    stub_dir = game.mods_dir / stub_name

    # 如果 stub 现在在 disabled_dir 里，先移回 mods_dir（省得重新链接/复制）
    disabled_dir = game.disabled_dir / stub_name
    if not stub_dir.exists() and disabled_dir.exists():
        log.info("ensure_workshop_stub_enabled: 在停用目录发现现成 stub，移回 Mods/ -> %s", stub_dir)
        try:
            if stub_dir.exists():
                bak = stub_dir.with_name(stub_dir.name + f"-old-{int(time.time())}")
                shutil.move(str(stub_dir), str(bak))
            shutil.move(str(disabled_dir), str(stub_dir))
        except Exception as e:
            log.warning("ensure_workshop_stub_enabled: 搬运停用目录失败，将重新生成 stub: %s", e)

    if not stub_dir.exists() or find_manifest(stub_dir, allow_disabled=False) is None:
        log.info(
            "ensure_workshop_stub_enabled: 生成 stub 目录 %s (源=%s, workshop_id=%s)",
            stub_dir, info.install_dir, info.workshop_id,
        )
        _populate_workshop_stub(info.install_dir, stub_dir)

    # 校验 stub manifest 可正常读取
    manifest = read_mod_manifest(stub_dir, allow_disabled=False)
    if manifest is None:
        log.error("ensure_workshop_stub_enabled: stub 创建完后仍然读不到 manifest: %s", stub_dir)
        # 降级：返回原 info（调用方仍可把它放进 enabled_order 侧，但游戏里加载效果取决于 Stub 是否真有效）
        return info
    return ModInfo(
        manifest=manifest,
        folder_name=stub_name,
        install_dir=stub_dir,
        source="local",  # 我们把它"种在" Mods 里，对 scanner 来说就是 local 来源
        workshop_id=info.workshop_id,
    )


def ensure_workshop_stub_disabled(info: ModInfo, game: GameInstall) -> ModInfo:
    """停用 Workshop mod：把 Mods/ 下对应 stub 整个搬到 disabled_dir。
    注意：**不会再改 workshop 原目录里的 manifest 文件名**——这是故意的！
    现在我们的真·物理停用是用 set_workshop_mod_disabled() 把 workshop 原目录的
    manifest.json → .json.disabled。这个函数只负责「Mods/ 下 stub 的搬运」，
    绝对不可以再把 .json.disabled 还原回 .json，否则会抵消掉 set_workshop_mod_disabled 刚做的真停用。
    """
    from .logger import get_logger

    log = get_logger("scanner.workshop.stub")
    stub_name = workshop_stub_folder_name(info)
    stub_dir = game.mods_dir / stub_name
    disabled_dir = game.disabled_dir / stub_name

    if stub_dir.exists():
        log.info("ensure_workshop_stub_disabled: stub 从 Mods/ 搬去 disabled_dir -> %s", disabled_dir)
        try:
            game.disabled_dir.mkdir(parents=True, exist_ok=True)
            if disabled_dir.exists():
                bak = disabled_dir.with_name(disabled_dir.name + f"-old-{int(time.time())}")
                shutil.move(str(disabled_dir), str(bak))
            shutil.move(str(stub_dir), str(disabled_dir))
            final_dir = disabled_dir
            final_source = "disabled"
        except Exception as e:
            log.error("ensure_workshop_stub_disabled: 搬运 stub 失败: %s", e)
            return info
    else:
        # 已经不在 Mods/ 里了，可能是之前就搬了，或者本就没启用
        if disabled_dir.exists():
            final_dir = disabled_dir
        else:
            final_dir = info.install_dir
        final_source = "disabled"

    # 重新从 final_dir 读 manifest（disabled 目录里允许 .disabled/manifest disabled 都识别）
    manifest = read_mod_manifest(final_dir, allow_disabled=True)
    if manifest is None:
        log.warning("ensure_workshop_stub_disabled: 搬完后读不到 manifest，保持原 info 结构")
        return info
    return ModInfo(
        manifest=manifest,
        folder_name=stub_name,
        install_dir=final_dir,
        source=final_source,
        workshop_id=info.workshop_id,
    )



# --------------------------------------------------------------------------- #
# BetterModMenu (BMM) 集成 —— STS2 生态事实上的「启用清单 + 排序 + 预设」标准
#   地位等价于 RimCrow / RimWorld 的 ModsConfig.xml：
#     - %APPDATA%\SlayTheSpire2\steam\<steam_id_64>\mod_data\BetterModMenu\mod_settings.json
#         List[{ModId: str, Enabled: bool, WorkshopUrl: str?}]
#         顺序 = 加载顺序（排序），Enabled=true 即为启用。
#     - %APPDATA%\SlayTheSpire2\steam\<steam_id_64>\mod_data\BetterModMenu\mod_profiles.json
#         多预设体系，与 RimCrow profile.json 对应。
#
#   我们的策略（完全对齐 RimCrow）：
#     1. Mods/ 目录、workshop/content/ 目录、.mods_disabled/ 目录全部「只读」对待，
#        绝不搬文件夹、绝不改 workshop manifest、绝不复制/移动 workshop 文件。
#     2. 启用/停用/排序 = 写 mod_settings.json（Enabled 字段 + 数组顺序）
#     3. 唯一例外：用户勾选「启用」一个 Workshop mod，但 Mods/ 下当前真的没有对应目录，
#        Steam 还没把它"种"进来 → 我们只在这时做「一次种 stub」（硬链接/复制），
#        之后依旧通过 BMM 的 Enabled=true/false 控制后续状态，不再回退搬运。
#     4. 如果用户没装 BetterModMenu（返回 None），走 fallback：之前的 stub + 搬目录方案
# --------------------------------------------------------------------------- #


@dataclass
class BmmPaths:
    """BetterModMenu 对当前 Steam 登录账号的配置文件路径定位结果。"""
    root: Path
    settings_file: Path
    profiles_file: Path


def locate_better_mod_menu() -> Optional[BmmPaths]:
    r"""定位 BetterModMenu 配置目录。
    优先策略：
      1) %APPDATA%\SlayTheSpire2\steam\* 的所有子目录里，检查 <d>/mod_data/BetterModMenu/ 是否存在（目录存在即视为已安装 BetterModMenu）。
         从合格候选中挑「settings 或 profiles 或 backup mtime 最新的」那个（一般是当前正在用的账号）。
      2) 找不到返回 None → 调用方就知道「没装 BMM」，要走回旧方案。

    注意：真实世界里 BMM 可能会把 settings.json 以 `.runstart.json` 为名放到 backups/，
    主目录下 settings.json 未必存在。所以这里**不要求 settings.json 必须存在**，
    只需要「BetterModMenu 根目录」存在即可；真正 load 的时候会兜底读 backups 里最新的一份。
    """
    import os as _os
    from pathlib import Path as _Path

    roaming = _os.environ.get("APPDATA")
    if not roaming:
        return None
    sts2_root = _Path(roaming) / "SlayTheSpire2" / "steam"
    if not sts2_root.is_dir():
        return None
    candidates: list[tuple[float, BmmPaths]] = []
    for child in sts2_root.iterdir():
        if not child.is_dir():
            continue
        root = child / "mod_data" / "BetterModMenu"
        settings = root / "mod_settings.json"
        profiles = root / "mod_profiles.json"
        if not root.is_dir():
            continue
        # 关键改变：只要 BetterModMenu 根目录存在就算命中（settings.json 可以不存在，
        # 我们 save 时会新建；load 时会从 backups 里找最新的兜底）
        try:
            score_candidates: list[float] = []
            if settings.exists():
                score_candidates.append(settings.stat().st_mtime)
            if profiles.exists():
                score_candidates.append(profiles.stat().st_mtime)
            bu_dir = root / "backups"
            if bu_dir.is_dir():
                # 拿 backups 里最新一个的 mtime 也计入（真实环境里 settings.json 常被挪到 backups）
                latest_backup = max((p for p in bu_dir.iterdir() if p.is_file()),
                                    key=lambda p: p.stat().st_mtime, default=None)
                if latest_backup is not None:
                    score_candidates.append(latest_backup.stat().st_mtime)
            score = max(score_candidates) if score_candidates else 0.0
        except OSError:
            score = 0.0
        candidates.append((score, BmmPaths(root=root, settings_file=settings, profiles_file=profiles)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _find_latest_bmm_settings_backup(paths: BmmPaths) -> Optional[Path]:
    """BetterModMenu 有时会把 mod_settings.json 移到 backups/ 作为
    mod_settings.YYYYMMDD-HHMMSS.runstart.json，而主目录里没有 settings。
    我们这里找 backups 里"文件名匹配 mod_settings.*.json"且 mtime 最新的一个用来兜底加载。
    """
    bu = paths.root / "backups"
    if not bu.is_dir():
        return None
    try:
        candidates = [p for p in bu.iterdir()
                      if p.is_file()
                      and p.name.startswith("mod_settings.")
                      and p.name.endswith(".json")]
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _atomic_write_json(path: Path, payload) -> None:
    """先写 .tmp 再 os.replace 原子替换，避免写入中途崩溃导致 JSON 损坏。

    使用 os.replace 而非 shutil.move：前者在 Windows 上等价于
    MoveFileEx(MOVEFILE_REPLACE_EXISTING)，可以原子覆盖已存在的目标文件；
    shutil.move 在 Windows 上先 os.rename（目标存在则 FileExistsError），
    降级走 copy2+unlink 时若 .tmp 被并发覆盖/删除会触发 FileNotFoundError。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# mod_settings.json —— 启用/停用/排序 权威清单
# ---------------------------------------------------------------------------

def load_bmm_settings(paths: BmmPaths) -> list[dict]:
    """读取 mod_settings.json；主目录文件不存在或损坏 → 兜底读 backups 里最新的一份 mod_settings 备份；
    都没有 → 返回空列表。结构稳定为 list[dict]（只保留 ModId=字符串 的项）。"""
    probe_files: list[Path] = []
    if paths.settings_file.exists():
        probe_files.append(paths.settings_file)
    backup = _find_latest_bmm_settings_backup(paths)
    if backup is not None:
        # backup 放第二个：主目录优先，主目录读不到才 fallback 到 backup
        probe_files.append(backup)
    from .logger import get_logger
    log = get_logger("scanner.bmm")
    if not probe_files:
        log.warning("load_bmm_settings: 主目录没有 mod_settings.json，backups 里也没有 mod_settings.*.json 备份，返回空 settings")
        return []
    last_err: Optional[Exception] = None
    for fp in probe_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            last_err = e
            log.warning("load_bmm_settings: %s 读取失败 -> %s", fp, e)
            continue
        if isinstance(data, list):
            filtered = [x for x in data if isinstance(x, dict) and isinstance(x.get("ModId"), str)]
            log.info("load_bmm_settings: 从 %s 加载 settings %d 条(其中 %d 条为合法 dict)", fp, len(data), len(filtered))
            return filtered
        log.warning("load_bmm_settings: %s 顶层类型 %s 不是 list，忽略", fp, type(data))
    log.error("load_bmm_settings: 所有候选文件均加载失败（最后一个错误=%s），返回空", last_err)
    return []


def save_bmm_settings(paths: BmmPaths, settings: list[dict]) -> None:
    """原子写 mod_settings.json；写之前自动做备份（保留一份 .bak 文件）。"""
    from .logger import get_logger
    log = get_logger("scanner.bmm")
    try:
        if paths.settings_file.exists():
            try:
                bak = paths.settings_file.with_suffix(paths.settings_file.suffix + ".bak")
                shutil.copy2(str(paths.settings_file), str(bak))
            except OSError:
                pass
        _atomic_write_json(paths.settings_file, list(settings))
        log.info("save_bmm_settings: 写入 %d 条 mod 状态 -> %s", len(settings), paths.settings_file)
    except Exception as e:
        log.error("save_bmm_settings 失败: %s", e)
        raise


def bmm_settings_reorder(settings: list[dict], new_order_ids: list[str]) -> list[dict]:
    """按新的 mod_id 顺序重排 settings，同时保持未知条目追加到末尾（避免丢配置）。
    new_order_ids 一般是 UI 拖拽后的「启用顺序」。BMM settings 同时存在 Enabled=false 的 mod，
    它们也需要排序。策略：
      - 在 new_order_ids 里出现过的：按给定顺序排到前面
      - 没出现过的：保持相对原顺序，追加到末尾
    """
    id_to_idx = {mid.lower(): i for i, mid in enumerate(new_order_ids)}
    # stable: 用 (分组, 新位置, 原始位置) 做排序键 → 同组里未知条目保持相对顺序不变
    ranked = sorted(
        enumerate(settings),
        key=lambda kv: (
            0 if (kv[1].get("ModId") or "").lower() in id_to_idx else 1,
            id_to_idx.get((kv[1].get("ModId") or "").lower(), 10_000_000),
            kv[0],
        ),
    )
    return [entry for _, entry in ranked]


def bmm_settings_set_enabled(
    settings: list[dict],
    mod_id: str,
    enabled: bool,
    *,
    workshop_url: Optional[str] = None,
) -> list[dict]:
    """设置某个 mod 的 Enabled 字段；若列表里没见过这个 mod 则追加。
    返回新的 settings 列表。不会改变顺序（已存在的话位置保持，新 mod 默认追加到末尾 —— 调用方可以随后 reorder）。
    """
    mid_lower = mod_id.lower()
    found_idx: Optional[int] = None
    for i, entry in enumerate(settings):
        if (entry.get("ModId") or "").lower() == mid_lower:
            found_idx = i
            break
    new_settings = list(settings)
    if found_idx is not None:
        entry = dict(new_settings[found_idx])
        entry["Enabled"] = bool(enabled)
        if workshop_url and (not entry.get("WorkshopUrl")):
            entry["WorkshopUrl"] = workshop_url
        new_settings[found_idx] = entry
    else:
        base_entry: dict = {"ModId": mod_id, "Enabled": bool(enabled)}
        if workshop_url:
            base_entry["WorkshopUrl"] = workshop_url
        new_settings.append(base_entry)
    return new_settings


def bmm_extract_enabled_order_ids(settings: list[dict]) -> list[str]:
    """从 BMM settings 提取「启用顺序」（只取 Enabled=true 的 ModId，按它们在列表里的顺序）。
    正好可以作为我们 manager 自己 state.enabled_order 的初始化值，保持与 BMM 一致。
    """
    return [e["ModId"] for e in settings if isinstance(e.get("ModId"), str) and bool(e.get("Enabled"))]


# ---------------------------------------------------------------------------
# mod_profiles.json —— 多预设（RimCrow 同款多 Profile 切换）
# ---------------------------------------------------------------------------

_DEFAULT_PROFILES = {
    "Profiles": [
        {"Name": "Default", "DisabledMods": []},
    ],
    "CurrentProfileIndex": 0,
    "CustomGroups": [],
    "ModGroups": {},
    "CollapsedGroups": [],
    "Tutorial": {"LastSeenVersion": "0.0.6"},
    "CloudBackups": {
        "Enabled": False,
        "Directory": "",
        "MirrorProfileBackups": True,
        "MirrorModSettingsBackups": True,
        "MirrorModListExports": True,
    },
    "ModNameStyles": {
        "Enabled": True,
        "UseDefaultTagFormats": True,
        "TagFormats": {},
        "TagPriority": [],
        "DisabledTags": [],
        "ModFormats": {},
    },
    "ModAnnotations": {},
}


def load_bmm_profiles(paths: BmmPaths) -> dict:
    """读 mod_profiles.json；文件不存在/损坏返回默认骨架。"""
    if not paths.profiles_file.exists():
        return json.loads(json.dumps(_DEFAULT_PROFILES))
    try:
        with open(paths.profiles_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("profiles 顶层不是 dict")
        if "Profiles" not in data or not isinstance(data["Profiles"], list):
            data["Profiles"] = [{"Name": "Default", "DisabledMods": []}]
        return data
    except Exception as e:
        from .logger import get_logger
        get_logger("scanner.bmm").warning("load_bmm_profiles 读取失败 %s: %s", paths.profiles_file, e)
        return json.loads(json.dumps(_DEFAULT_PROFILES))


def save_bmm_profiles(paths: BmmPaths, profiles: dict) -> None:
    """原子写 mod_profiles.json，写前先备份。"""
    from .logger import get_logger
    log = get_logger("scanner.bmm")
    try:
        if paths.profiles_file.exists():
            try:
                bak = paths.profiles_file.with_suffix(paths.profiles_file.suffix + ".bak")
                shutil.copy2(str(paths.profiles_file), str(bak))
            except OSError:
                pass
        _atomic_write_json(paths.profiles_file, profiles)
        log.info("save_bmm_profiles: 写入 profiles(CurrentProfileIndex=%s) -> %s",
                 profiles.get("CurrentProfileIndex"), paths.profiles_file)
    except Exception as e:
        log.error("save_bmm_profiles 失败: %s", e)
        raise


# --------------------------------------------------------------------------- #
# STS2 原生清单 —— settings.save → mod_settings.mod_list
#   地位：真·游戏源启用/停用/排序权威清单，不依赖任何第三方 mod。
#
#   真实结构（从本地 settings.save 260 行验证）：
#     {
#       "aspect_ratio": ..., "fps_limit": ..., "volume_master": ...,  # 其他用户设置（绝不碰）
#       "mod_settings": {
#         "mods_enabled": true,                                        # 总开关
#         "mod_list": [                                                # 数组顺序 = 加载顺序
#           {"id": "STS2-RitsuLib",       "is_enabled": true,  "source": "steam_workshop"},
#           {"id": "BaseLib",             "is_enabled": true,  "source": "steam_workshop"},
#           ...（37 条）
#           {"id": "LoadOrderManager",    "is_enabled": true,  "source": "steam_workshop"},
#         ]
#       },
#       "schema_version": 5, ...
#     }
#
#   策略（LoadOrderManager 同款实现思路）：
#     1. settings.save 整个文件原样读取，除了 mod_settings 字段外 **完全不改动**
#        （controller_mapping、音量、窗口大小等必须保持字节级一致，避免破坏用户配置）
#     2. 启用/停用 = 只改对应 mod_list[i]["is_enabled"]
#     3. 排序     = 只改 mod_list 数组的元素顺序（未知项追加到末尾，避免丢配置）
#     4. 写入     = 原子写（.tmp → os.replace）+ 自动 .bak 备份
#     5. 找不到 settings.save 或里面没有 mod_settings.mod_list → 返回空/None，
#        调用方自动 fallback 到搬目录/stub 方案
# --------------------------------------------------------------------------- #


def locate_settings_save(manual_save_dir: Optional[Path] = None) -> Optional[Path]:
    """定位 STS2 settings.save（原生启用/停用/排序清单所在地）。

    定位策略：
      0) manual_save_dir 指定时：直接检查 <manual_save_dir>/settings.save
      1) %APPDATA%\\SlayTheSpire2\\steam\\<steamid64>\\settings.save
         （多账号时，挑 settings.save 的 mtime 最新的那个，基本就是当前在用账号）
      2) 没有 steam 子目录 → 兜底 %APPDATA%\\SlayTheSpire2\\default\\1\\settings.save
      3) 都没有 → 返回 None（调用方要 fallback 到搬目录/stub 旧方案）
    """
    import os as _os
    # 0) 手动指定存档目录
    if manual_save_dir is not None:
        try:
            p = Path(manual_save_dir).expanduser()
        except Exception:
            p = None
        if p is not None:
            if p.is_dir():
                f = p / "settings.save"
                if f.is_file():
                    return f
            elif p.is_file():
                return p
        # 手动指定但找不到，继续走自动定位（不要直接失败）

    roaming = _os.environ.get("APPDATA")
    if not roaming:
        return None
    sts2_root = Path(roaming) / "SlayTheSpire2"
    if not sts2_root.is_dir():
        return None

    # 1) 优先 steam 目录下的账号配置（真实用户玩游戏一定用的是 steam 登录账号）
    steam_root = sts2_root / "steam"
    candidates: list[tuple[float, Path]] = []
    if steam_root.is_dir():
        for child in steam_root.iterdir():
            if not child.is_dir():
                continue
            f = child / "settings.save"
            if f.is_file():
                try:
                    candidates.append((f.stat().st_mtime, f))
                except OSError:
                    pass
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # 2) 兜底 default 账号
    fallback = sts2_root / "default" / "1" / "settings.save"
    if fallback.is_file():
        return fallback
    return None


def load_native_mod_list(save_path: Path) -> Optional[dict]:
    """加载 settings.save 的完整 JSON，并额外做一次 mod_settings.mod_list 合法性校验。

    返回：
      - 成功：整个 settings.save 的 dict（含顶层其他字段 + mod_settings.mod_list）
      - 文件不存在 / JSON 损坏 / 没有 mod_settings.mod_list → 返回 None
        （调用方看到 None 就知道「没有原生清单可用」，要 fallback 到搬目录/stub 方案）
    """
    from .logger import get_logger
    log = get_logger("scanner.native")
    if not save_path.is_file():
        log.warning("load_native_mod_list: %s 不存在", save_path)
        return None
    try:
        with open(save_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("load_native_mod_list: %s JSON 解析失败 -> %s", save_path, e)
        return None
    if not isinstance(data, dict):
        log.warning("load_native_mod_list: %s 顶层不是 dict (type=%s)", save_path, type(data))
        return None
    mod_settings = data.get("mod_settings")
    if not isinstance(mod_settings, dict):
        log.warning("load_native_mod_list: %s 没有合法 mod_settings 字段", save_path)
        return None
    mod_list = mod_settings.get("mod_list")
    if not isinstance(mod_list, list):
        log.warning("load_native_mod_list: %s mod_settings.mod_list 不是 list", save_path)
        return None
    # 校验：至少是 list[dict] 且每个 dict 有 id/is_enabled
    legit = 0
    for entry in mod_list:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("id"), str):
            continue
        if "is_enabled" not in entry:
            continue
        legit += 1
    log.info("load_native_mod_list: 从 %s 加载 mod_list %d 条（其中 %d 条合法）",
             save_path, len(mod_list), legit)
    return data


def save_native_mod_list(save_path: Path, full_data: dict) -> None:
    """写回 settings.save；**只允许调用方改 mod_settings，其他顶层字段必须原样保留**。

    写入流程（与 BMM 同样的安全级别）：
      1) 若 settings.save 已存在 → 先 copy2 一份 .bak 备份（崩溃可回滚）
      2) 先写 .tmp（完整 JSON）
      3) os.replace 原子替换目标文件（Windows 上等价于 MoveFileEx 覆盖，要么全写成功，要么原文件纹丝不动）
    """
    from .logger import get_logger
    log = get_logger("scanner.native")
    # 防御式校验：调用方不能把 mod_settings.mod_list 弄丢（否则就是我们有 bug）
    ms = full_data.get("mod_settings")
    if not isinstance(ms, dict) or not isinstance(ms.get("mod_list"), list):
        raise ValueError("save_native_mod_list: full_data 里 mod_settings.mod_list 丢失，拒绝写入，避免破坏用户 settings.save")
    try:
        if save_path.exists():
            try:
                bak = save_path.with_suffix(save_path.suffix + ".bak")
                shutil.copy2(str(save_path), str(bak))
            except OSError:
                pass
        _atomic_write_json(save_path, full_data)
        log.info("save_native_mod_list: 写入 mod_list %d 条 -> %s",
                 len(ms["mod_list"]), save_path)
    except Exception as e:
        log.error("save_native_mod_list 失败: %s", e)
        raise


# ---------------------------------------------------------------------------
# mod_list 纯函数操作（不读盘、不写盘，只返回一个新的 full_data dict）
# ---------------------------------------------------------------------------

def _validate_full_data(full_data: dict) -> dict:
    """确保 full_data 有 mod_settings.mod_list，没有就就地建一个空骨架。"""
    ms = full_data.get("mod_settings")
    if not isinstance(ms, dict):
        ms = {}
        full_data["mod_settings"] = ms
    if not isinstance(ms.get("mod_list"), list):
        ms["mod_list"] = []
    if "mods_enabled" not in ms:
        ms["mods_enabled"] = True
    return full_data


def norm_mod_source(source_hint: Optional[str]) -> str:
    """把我们 ModInfo.source（'local'/'workshop'/'disabled'）映射成 settings.save 里
    游戏 ModSource 枚举的序列化值。

    游戏用 SnakeCaseJsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) 序列化枚举：
      ModSource.ModsDirectory → "mods_directory"
      ModSource.SteamWorkshop → "steam_workshop"
    ModSettings.IsModDisabled(id, source) 要求 id 和 source **都相等** 才认为该条停用生效，
    所以这里必须写游戏认识的枚举值（写 "local" 会导致 source 匹配失败 → 停用不生效）。"""
    if source_hint in ("workshop", "steam_workshop"):
        return "steam_workshop"
    # local / disabled：本质都在 exe 同级 mods 目录（停用态由 is_enabled=false 表达）
    return "mods_directory"


def nat_mod_list_find_entry(
    mod_list: list[dict],
    mod_id: str,
    *,
    source_enum: Optional[str] = None,
) -> Optional[tuple[int, dict]]:
    """在 mod_list 里找一条匹配的条目。

    - source_enum 指定时：按 (id.lower(), source_enum) 双字段精确匹配（settings.save 层标准匹配）
    - source_enum=None 时：只按 id.lower() 找第一条（向后兼容旧逻辑）

    返回 (index, entry_dict) 或 None。
    """
    mid_lower = mod_id.lower()
    if source_enum is not None:
        for i, entry in enumerate(mod_list):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id", "")).lower() == mid_lower and str(entry.get("source", "")) == source_enum:
                return (i, entry)
        return None
    # 向后兼容：只按 id.lower() 找第一条
    for i, entry in enumerate(mod_list):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id", "")).lower() == mid_lower:
            return (i, entry)
    return None


def nat_mod_list_set_enabled(
    full_data: dict,
    mod_id: str,
    enabled: bool,
    *,
    source_hint: Optional[str] = None,
) -> dict:
    """改 mod_list 中某个 mod 的 is_enabled。

    核心改动（2026-10）：按 (id, source) 双字段精确匹配，而不是只按 id 找第一条。
    游戏的 ModSettings.IsModDisabled(id, source) 也是双字段匹配，所以 settings.save 里
    可以存多条同 id 不同 source 的记录，各自独立启停（工坊副本/本地副本互不干扰）。

    匹配逻辑：
      - source_hint 有值 → 按 (id.lower(), norm_mod_source(source_hint)) 精确匹配
        · 找到 → 只改这条的 is_enabled（同 id 其他 source 的条目不动）
        · 没找到 → 追加新条目（同 id 不同 source 的已有条目保留原位）
      - source_hint=None → 向后兼容旧逻辑，按 id.lower() 找第一条

    返回新的 full_data（顶层其他字段完全共享引用，不会改其他值）。
    """
    fd = _validate_full_data(dict(full_data))
    mod_list = list(fd["mod_settings"]["mod_list"])  # 浅复制一层 list，避免改到原对象
    target_source = norm_mod_source(source_hint) if source_hint is not None else None
    found = nat_mod_list_find_entry(mod_list, mod_id, source_enum=target_source)
    if found is not None:
        idx, entry = found
        new_entry = dict(entry)
        new_entry["is_enabled"] = bool(enabled)
        mod_list[idx] = new_entry
    else:
        new_entry = {
            "id": mod_id,
            "is_enabled": bool(enabled),
            "source": norm_mod_source(source_hint),
        }
        mod_list.append(new_entry)
    fd["mod_settings"] = dict(fd["mod_settings"])  # 换个新 dict，避免上层共享引用被意外修改
    fd["mod_settings"]["mod_list"] = mod_list
    return fd


def nat_mod_list_reorder(full_data: dict, new_order_ids: list[str]) -> dict:
    """按 new_order_ids 的顺序重排 mod_list；未出现过的条目保持原相对顺序追加到末尾（避免丢配置）。

    2026-10 核心改动：支持同 lower id 多条条目（不同 source）的场景。
    - new_order_ids 是 mod_id 粒度的排序（游戏加载顺序就是 mod_id 粒度）
    - 同 lower id 的所有条目（不管 source）紧挨着排，组内保持原有相对顺序
    - 剩余没出现过的条目保持原相对顺序追加到末尾
    返回新的 full_data。"""
    fd = _validate_full_data(dict(full_data))
    old_list = list(fd["mod_settings"]["mod_list"])
    # 按 lower id 分组：{lower_id: [(idx, entry), ...]}
    groups: dict[str, list[tuple[int, dict]]] = {}
    for i, entry in enumerate(old_list):
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id", ""))
        if not mid:
            continue
        low = mid.lower()
        groups.setdefault(low, []).append((i, entry))

    ranked: list = []
    seen_indices: set[int] = set()
    # 1) 先把 new_order_ids 里出现过的 lower id 的整组，按给定顺序塞到前面
    for mid in new_order_ids:
        low = str(mid).lower()
        grp = groups.get(low)
        if not grp:
            continue
        for idx, entry in grp:
            if idx in seen_indices:
                continue
            ranked.append(entry)
            seen_indices.add(idx)
    # 2) 剩余没出现过的，保持原相对顺序塞到末尾
    for i, entry in enumerate(old_list):
        if i in seen_indices:
            continue
        ranked.append(entry)
    fd["mod_settings"] = dict(fd["mod_settings"])
    fd["mod_settings"]["mod_list"] = ranked
    return fd


def nat_extract_source_states(full_data: dict) -> dict[str, dict[str, bool]]:
    """从原生 full_data 里提取每个 mod_id 下各 source 的启停状态。

    返回：{ lower_mod_id: { source_enum_str: is_enabled_bool, ... } }
    例如：{ "baselib": { "steam_workshop": True, "mods_directory": False } }

    同 mod_id 多条不同 source 的条目独立记录，互不覆盖。"""
    result: dict[str, dict[str, bool]] = {}
    ms = full_data.get("mod_settings") if isinstance(full_data, dict) else None
    ml = ms.get("mod_list") if isinstance(ms, dict) else None
    if not isinstance(ml, list):
        return result
    for entry in ml:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        low = mid.lower()
        src = str(entry.get("source", ""))
        # source 缺失的条目兜底成 mods_directory（老数据兼容）
        if not src:
            src = "mods_directory"
        enabled = bool(entry.get("is_enabled"))
        result.setdefault(low, {})[src] = enabled
    return result


def nat_extract_enabled_order_ids(full_data: dict) -> list[str]:
    """从原生 full_data 里提取「启用顺序」：只取 is_enabled=true 的 id，按数组顺序去重。

    2026-10 改动：同 lower id 多条（不同 source）都 is_enabled=true 时只返回一份 mod_id，
    但保持它们在 mod_list 里的相对位置（第一次出现的位置即为该 mod 在排序里的位置）。
    正好作为 manager state.enabled_order 的初始化值。"""
    ms = full_data.get("mod_settings") if isinstance(full_data, dict) else None
    ml = ms.get("mod_list") if isinstance(ms, dict) else None
    if not isinstance(ml, list):
        return []
    result: list[str] = []
    seen_lower: set[str] = set()
    for entry in ml:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        if not bool(entry.get("is_enabled")):
            continue
        low = mid.lower()
        if low in seen_lower:
            continue
        result.append(mid)
        seen_lower.add(low)
    return result


def nat_extract_disabled_order_ids(full_data: dict) -> list[str]:
    """从原生 full_data 里提取「停用顺序」：只取 is_enabled=false 的 id，按数组顺序去重。

    2026-10 改动：同 lower id 多条不同 source 时，只要任意一条 is_enabled=false 就收录？
    不——更准确地说：如果同 lower id 存在 is_enabled=true 的条目，说明该 mod 在至少一个 source
    上是启用的，就不该出现在停用列表里。只有该 mod 所有 source 都是 is_enabled=false 才收录。

    为了实现这点，先扫一遍 source_states 确认每个 lower id 是否全停用。"""
    ms = full_data.get("mod_settings") if isinstance(full_data, dict) else None
    ml = ms.get("mod_list") if isinstance(ms, dict) else None
    if not isinstance(ml, list):
        return []
    # 先建 source_states 确认每个 lower id 的启用/停用集合
    src_states = nat_extract_source_states(full_data)
    result: list[str] = []
    seen_lower: set[str] = set()
    for entry in ml:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        low = mid.lower()
        if low in seen_lower:
            continue
        # 该 lower id 所有 source 都是停用 → 才加入停用列表
        per_mod = src_states.get(low, {})
        all_disabled = bool(per_mod) and all(not v for v in per_mod.values())
        if not all_disabled:
            continue
        result.append(mid)
        seen_lower.add(low)
    return result


def is_load_order_manager_physically_active(
    mods_index: dict[str, ModInfo],
    game: Optional[GameInstall] = None,
    fallback_scan_workshop: bool = True,
) -> bool:
    """判断 LoadOrderManager 在 STS2 启动时是否真的会被加载（从而 settings.save.mod_list 才有意义）。

    ⚠️ 已废弃（2026-10）—— store.py 的 _sync_state_orders 改为始终以物理 source 字段为最终分类真值，
    不再依赖 LOM 是否物理启用。settings.save 仅作为排序权威（同一边内的顺序），启用/停用的实际分类
    由物理位置决定（STS2 目录驱动加载）。保留此函数仅供调试/手动排查。

    规则：STS2 默认目录驱动会加载「Mods/ 子目录里 manifest 正常」的 mod，以及
    「workshop/content/<AppID> 下发布文件子目录里 manifest 正常（没被改成 .disabled）」的 mod。
    只有 LoadOrderManager 处于其中之一的"物理启用态"，它的 Harmony 补丁才会跑，
    游戏才会按 settings.save.mod_list.is_enabled 做启用/停用；否则 settings.save 里的
    数值是上次 LOM 运行留下的"脏数据"，管理器绝不能作为权威。
    """
    from .logger import get_logger
    _dbg_log = get_logger("scanner.lom_detect")

    def _is_under(child: Path, parent: Path) -> bool:
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except Exception:
            return False

    # 1) 从已扫出来的 mods 索引里找（最直接）
    info = mods_index.get(LOAD_ORDER_MANAGER_MOD_ID_CANON.lower())
    if info is not None and not info.is_placeholder and info.install_dir is not None:
        # 关键：必须 source 不是 disabled，并且安装目录真的在 Mods/ 或 workshop/ 下
        # （停用目录里的 mod 就算 manifest 正常也永远不会被 STS2 扫到）
        if info.source == "disabled":
            _dbg_log.info("LOM 命中 mods_index 但 source=disabled → 物理停用（%s）", info.install_dir)
        else:
            # 再用物理路径交叉验证：必须在 mods_dir 或 workshop_dir 下
            location_ok = False
            if game is not None:
                if game.mods_dir is not None and _is_under(info.install_dir, game.mods_dir):
                    location_ok = True
                if game.workshop_dir is not None and _is_under(info.install_dir, game.workshop_dir):
                    location_ok = True
            # 如果没传 game，就信任 source 字段（local/workshop 都代表可被 STS2 扫到）
            if game is None or location_ok:
                # manifest 必须"正常"（不允许 .disabled 后缀）
                try:
                    if find_manifest(info.install_dir, allow_disabled=False) is not None:
                        _dbg_log.info(
                            "LOM 命中 mods_index: info.install_dir=%s source=%s workshop_id=%s location_ok=%s → ACTIVE",
                            info.install_dir, info.source, info.workshop_id, location_ok,
                        )
                        return True
                    else:
                        _dbg_log.info("LOM 命中 mods_index 但 manifest 非正常（.disabled 或读不到）: %s", info.install_dir)
                except Exception as e:
                    _dbg_log.info("LOM 命中 mods_index 但 find_manifest 异常: %s", e)
            else:
                _dbg_log.info(
                    "LOM 命中 mods_index 但不在 Mods/ 也不在 workshop/ 下（可能在停用目录？）: install_dir=%s mods_dir=%s workshop_dir=%s",
                    info.install_dir,
                    game.mods_dir if game and game.mods_dir else None,
                    game.workshop_dir if game and game.workshop_dir else None,
                )
    else:
        _dbg_log.info("LOM 未命中 mods_index 或占位/无 install_dir：info=%s", info)

    # 2) 没命中？那在 workshop_dir 里直接按发布文件 id 3747605109 再检查一次
    if fallback_scan_workshop and game is not None and game.workshop_dir is not None:
        try:
            lom_dir = game.workshop_dir / LOAD_ORDER_MANAGER_WORKSHOP_ID
            if lom_dir.is_dir() and find_manifest(lom_dir, allow_disabled=False) is not None:
                mp = read_mod_manifest(lom_dir, allow_disabled=False)
                if mp is not None and mp.id.lower() == LOAD_ORDER_MANAGER_MOD_ID_CANON.lower():
                    _dbg_log.info("LOM 命中 workshop_dir 3747605109 真目录：%s → ACTIVE", lom_dir)
                    return True
                _dbg_log.info("LOM workshop 3747605109 有 manifest 但 id 不匹配或解析失败: mp=%s", mp)
            else:
                _dbg_log.info("LOM workshop 3747605109 不存在或 manifest 非正常: %s is_dir=%s find_manifest=%s",
                              lom_dir, lom_dir.is_dir(), find_manifest(lom_dir, allow_disabled=True) if lom_dir.is_dir() else None)
        except Exception as e:
            _dbg_log.info("LOM workshop 3747605109 检查异常: %s", e)

    # 3) 最后兜底：只在 Mods/ 下按目录名找（.mods_disabled/ 下的一律不算启用态）
    if game is not None:
        parents_to_check = []
        if game.mods_dir is not None:
            parents_to_check.append((game.mods_dir, "Mods"))
        # 注意：**故意不把 .mods_disabled 加进来**——停用目录里就算有 LOM 也不会被 STS2 加载
        for parent, tag in parents_to_check:
            try:
                if not parent.is_dir():
                    continue
            except Exception:
                continue
            try:
                for d in parent.iterdir():
                    if not d.is_dir():
                        continue
                    low_name = d.name.lower()
                    # 按目录名精确匹配 LOM 三种常见命名
                    if low_name not in (
                        LOAD_ORDER_MANAGER_MOD_ID_CANON.lower(),
                        LOAD_ORDER_MANAGER_WORKSHOP_ID,
                        "loadordermanager",
                    ):
                        continue
                    try:
                        mf = find_manifest(d, allow_disabled=False)
                        if mf is not None:
                            mp = read_mod_manifest(d, allow_disabled=False)
                            if mp is not None and mp.id.lower() == LOAD_ORDER_MANAGER_MOD_ID_CANON.lower():
                                _dbg_log.info("LOM 命中 %s 子目录：%s manifest=%s → ACTIVE", tag, d, mf)
                                return True
                            _dbg_log.info("LOM %s 子目录 %s 有 manifest 但 id 不匹配: mp=%s", tag, d, mp)
                        else:
                            _dbg_log.info("LOM %s 子目录 %s 无正常 manifest（可能在 .disabled）", tag, d)
                    except Exception as e:
                        _dbg_log.info("LOM %s 子目录 %s 检查异常: %s", tag, d, e)
            except Exception as e:
                _dbg_log.info("LOM 遍历 %s 目录异常: %s", tag, e)
    _dbg_log.info("LOM 判定最终：INACTIVE（LOM 没真的物理启用 → settings.save 不应做启用/停用权威）")
    return False


def ensure_load_order_manager(game: "GameInstall", bundled_dir: Optional[Path] = None) -> Optional[Path]:
    """确保 LoadOrderManager 物理启用（在 Mods/ 下），用于实现「排序功能内置」。

    ⚠️ 已废弃（2026-10）—— 管理器通过 settings.save 直接管理排序，不需要安装 LOM mod。
    保留此函数仅供需要旧行为的用户手动调用。

    优先级：
      1) Mods/ 下已存在 manifest 正常的 LOM → 直接返回其目录
      2) workshop 源目录里已存在（Steam 订阅）→ 直接返回
      3) 都没有 → 从 bundled_dir/LoadOrderManager 复制到 Mods/LoadOrderManager

    返回 LOM 目录 Path；失败或没有可用的 bundled 源时返回 None。
    """
    from .logger import get_logger
    log = get_logger("scanner.lom")

    # 1) Mods/ 下按目录名找
    for name in (LOAD_ORDER_MANAGER_MOD_ID_CANON, "loadordermanager"):
        lom_dir = game.mods_dir / name
        try:
            if lom_dir.is_dir() and find_manifest(lom_dir, allow_disabled=False):
                log.info("ensure_load_order_manager: 已存在 %s", lom_dir)
                return lom_dir
        except Exception:
            continue

    # 2) workshop 源目录
    if game.workshop_dir is not None:
        ws_lom = game.workshop_dir / LOAD_ORDER_MANAGER_WORKSHOP_ID
        try:
            if ws_lom.is_dir() and find_manifest(ws_lom, allow_disabled=False):
                log.info("ensure_load_order_manager: 已存在 workshop 源 %s", ws_lom)
                return ws_lom
        except Exception:
            pass

    # 3) 从 bundled 复制
    if bundled_dir is None:
        bundled_dir = Path(__file__).resolve().parent.parent / "data" / "bundled_mods"
    src = bundled_dir / "LoadOrderManager"
    if not src.is_dir() or find_manifest(src, allow_disabled=False) is None:
        log.warning("ensure_load_order_manager: bundled 源 %s 不存在或无 manifest", src)
        return None

    target = game.mods_dir / "LoadOrderManager"
    if target.exists():
        bak = target.with_name(target.name + f"-old-{int(time.time())}")
        log.info("ensure_load_order_manager: 目标 %s 已存在，备份为 %s", target, bak)
        shutil.move(str(target), str(bak))
    try:
        shutil.copytree(str(src), str(target))
        log.info("ensure_load_order_manager: 从 bundled 复制 LOM -> %s", target)
        return target
    except Exception as e:
        log.error("ensure_load_order_manager: 复制失败: %s", e)
        return None


@dataclass
class ModScanResult:
    game: GameInstall
    mods: dict[str, ModInfo] = field(default_factory=dict)            # key = lowercase mod_id → 默认展示版本
    versions: dict[str, list[ModInfo]] = field(default_factory=dict)  # key = lowercase mod_id → 全部物理副本
    errors: list[str] = field(default_factory=list)
    # 未订阅的工坊残留目录里读到的 manifest id（小写）：供 store.py 过滤 settings.save 里的同名占位
    unsubscribed_mod_ids: set[str] = field(default_factory=set)
    # Steam 订阅登记（appworkshop_*.acf）是否成功读取；False = 只能遍历目录不过滤，
    # 工坊列表可能混入已取消订阅的孤儿项，前端据此提示
    subscribed_known: bool = True

    def to_list(self) -> list[ModInfo]:
        return list(self.mods.values())


def read_subscribed_workshop_ids(workshop_dir: Path) -> Optional[set[str]]:
    """读取 Steam 工坊订阅登记，返回「已订阅/已安装」的 publishedfileid 集合。

    workshop_dir 形如 <lib>/steamapps/workshop/content/<AppID>，
    登记文件是 <lib>/steamapps/workshop/appworkshop_<AppID>.acf（Valve VDF 文本）。

    为什么要用它：游戏枚举工坊 mod 走 SteamUGC.GetSubscribedItems()，与这里
    ``WorkshopItemsInstalled`` 的键集合一致；而 Steam 取消订阅后常**只清登记、
    残留目录**，直接遍历目录会扫出游戏根本不会加载的「孤儿」工坊目录。

    读取或解析失败返回 None —— 调用方应回退为「不过滤」，避免误隐藏正常 mod。
    """
    try:
        wdir = Path(workshop_dir)
        acf = wdir.parent.parent / f"appworkshop_{wdir.name}.acf"
        if not acf.is_file():
            return None
        text = acf.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    ids: set[str] = set()
    in_block = False
    depth = 0
    for line in text.splitlines():
        s = line.strip()
        if not in_block:
            if "WorkshopItemsInstalled" in s:
                in_block = True
            continue
        if s == "{":
            depth += 1
            continue
        if s == "}":
            depth -= 1
            if depth <= 0:
                break
            continue
        # depth==1 时该行是工坊项键（形如 "3737335127"）；depth>=2 是 size/manifest 等属性
        if depth == 1 and s.startswith('"'):
            key = s[1:].split('"', 1)[0]
            if key.isdigit():
                ids.add(key)
    return ids


def scan_all_mods(game: GameInstall, extra_mods_dirs: Optional[list[Path]] = None) -> ModScanResult:
    result = ModScanResult(game=game)

    # 估 total 用于进度条（粗略：各主要目录下的子目录数之和）
    _progress_total = 0
    try:
        for root in [game.mods_dir] + (list(getattr(game, "disabled_dirs", []) or []) or []):
            if root.is_dir():
                _progress_total += sum(1 for e in root.iterdir() if e.is_dir())
        if game.workshop_dir and game.workshop_dir.is_dir():
            _progress_total += sum(1 for e in game.workshop_dir.iterdir() if e.is_dir())
    except Exception:
        pass
    if _progress_total <= 0:
        _progress_total = 1  # 至少有个基数避免除零
    reset_scan_progress(_progress_total)

    def _should_override(existing: ModInfo, new: ModInfo) -> bool:
        """同 lower_id 冲突时判断 new 是否比 existing 更值得作为「默认展示版本」。

        优先级（工坊优先）：workshop > local > disabled > 占位；
        同组内版本号更高者优先；版本相同有 workshop_id 者优先。
        """
        # 占位永远最低优先级
        if existing.is_placeholder and not new.is_placeholder:
            return True
        if new.is_placeholder and not existing.is_placeholder:
            return False
        if existing.is_placeholder and new.is_placeholder:
            return False  # 都是占位，保持原条目
        # 与游戏原生规则对齐：本地 > 工坊（游戏 RemoveDisabledMods 对同 id 的工坊副本标 DisabledDuplicate，优先本地）
        _src_rank = {"local": 0, "workshop": 1, "disabled": 2}
        ex_rank = _src_rank.get(existing.source, 3)
        new_rank = _src_rank.get(new.source, 3)
        if new_rank < ex_rank:
            return True
        if new_rank > ex_rank:
            return False
        if version_gt(new.manifest.version, existing.manifest.version):
            return True
        if version_gt(existing.manifest.version, new.manifest.version):
            return False
        if existing.workshop_id is None and new.workshop_id is not None:
            return True
        return False

    def _add(info: ModInfo) -> None:
        # 桶 key：有 workshop_id（数字）→ 派生 key "mid_lower::wid"；否则 → 基础 key mid_lower
        # 让两个不同 Steam 工坊但 manifest mod_id 撞名的副本各自独立入桶，不合并
        mid = info.mod_id.lower()
        if info.workshop_id is not None:
            _s = str(info.workshop_id)
            k = f"{mid}::{_s}" if _s.isdigit() else mid
        else:
            k = mid
        # 收集所有物理副本，供「选择版本」菜单使用
        result.versions.setdefault(k, []).append(info)
        # canonical 层（_mods）用基础 key 存第一个可信副本（refresh workshop 补扫路径再用派生 key 并存）
        existing = result.mods.get(mid)
        if existing is None or _should_override(existing, info):
            result.mods[mid] = info
        # 进度条 tick（每识别到一个 mod 就推进一步）
        tick_scan_progress(current=info.display_name or info.mod_id)

    def _scan_dir(root: Path, source: str, workshop_id: Optional[str] = None) -> None:
        if not root.is_dir():
            return
        try:
            for entry in sorted(root.iterdir()):
                if entry.is_dir():
                    info = _scan_folder(entry, source=source, workshop_id=workshop_id)
                    if info:
                        _add(info)
        except Exception as e:
            result.errors.append(f"扫描 {root} 失败: {e}")

    # 1. 启用目录：只扫管理器的标准 Mods 目录（不再扫大小写变体）
    primary = game.mods_dir
    scanned: set[Path] = set()
    if primary.is_dir():
        _scan_dir(primary, source="local")
        scanned.add(primary.resolve())

    # 1b. 额外 mods 目录（用户在设置里手动指定，用分号/竖线分隔的多个目录）
    #     这些目录里的 mod 视为「本地启用态」来源（source=local），和 Mods/ 同级处理
    for extra_raw in (extra_mods_dirs or []):
        try:
            extra = Path(extra_raw).expanduser()
        except Exception:
            continue
        if not extra.is_dir():
            continue
        try:
            rp = extra.resolve()
        except Exception:
            rp = extra
        if rp in scanned:
            continue
        _scan_dir(extra, source="local")
        scanned.add(rp)

    # 2. 停用目录：扫所有已存在的 disabled 目录变体（.mods_disabled + mods_disabled + ...）
    all_disabled = getattr(game, "disabled_dirs", None) or [game.disabled_dir]
    for dis_dir in all_disabled:
        try:
            rp_dis = dis_dir.resolve()
        except Exception:
            rp_dis = dis_dir
        if rp_dis in scanned:
            continue
        if dis_dir.is_dir():
            _scan_dir(dis_dir, source="disabled")
            try:
                scanned.add(rp_dis)
            except Exception:
                pass

    # 3. Steam 工坊 workshop/content/2865400/<publishedfileid>/
    if game.workshop_dir is not None and game.workshop_dir.is_dir():
        # 与游戏对齐：只认 Steam 订阅登记里的工坊项。取消订阅后 Steam 常常只清登记、
        # 残留目录，这些目录游戏根本不会加载，这里直接跳过（读不到登记则回退为不过滤）。
        subscribed_ids = read_subscribed_workshop_ids(game.workshop_dir)
        # 读不到登记（acf 缺失/解析失败）时显式记录，供前端提示工坊列表可能含孤儿项
        result.subscribed_known = subscribed_ids is not None
        skipped_unsubscribed: list[str] = []
        try:
            for pub_dir in sorted(game.workshop_dir.iterdir()):
                if not pub_dir.is_dir():
                    continue
                workshop_id = pub_dir.name
                if subscribed_ids is not None and workshop_id not in subscribed_ids:
                    skipped_unsubscribed.append(workshop_id)
                    # 记录到 unsubscribed_mod_ids 集合，供前端黄标用
                    for cand in [pub_dir] + [e for e in pub_dir.iterdir() if e.is_dir()]:
                        m = read_mod_manifest(cand, allow_disabled=True)
                        if m is not None and m.id:
                            result.unsubscribed_mod_ids.add(m.id.lower())
                    # 去掉 continue —— 让未订阅 wid 正常扫进 _mods / _mod_versions
                    # （Steam 退订残留目录里有合法 manifest 的 mod，游戏会加载但管理器要加黄标）
                # 工坊里可能还有更深一层（有些 Steam 客户端会嵌套），两层都扫一下
                direct_mods = [e for e in pub_dir.iterdir() if e.is_dir()] if pub_dir.is_dir() else []
                # 这里判断"是否更深一层"时也必须允许 disabled manifest，否则被停用的 workshop mod 嵌套时永远扫不到
                nested_ok = False
                if direct_mods:
                    for dm in direct_mods:
                        if find_manifest(dm, allow_disabled=True) is not None:
                            nested_ok = True
                            break
                if nested_ok:
                    candidates: list[Path] = direct_mods
                else:
                    # 根目录平铺多个 manifest 的合集 mod（例如一个 workshop item 打包 4 个 mod）
                    # 这里不能只拿 find_manifest 返回的第一个，要遍历所有 json 全部解析
                    candidates = [pub_dir]

                # ---- 扫 candidates ----
                any_scanned = False
                for c in candidates:
                    if not c.is_dir():
                        continue

                    # ---- 平铺目录场景扫出所有 manifest ----
                    flat_manifests: list[tuple[Path, bool]] = []  # [(json_path, is_disabled)]
                    if c == pub_dir and not direct_mods:
                        # 扫所有 *.json 和 *.json.disabled
                        try:
                            for entry in sorted(c.iterdir()):
                                if not entry.is_file():
                                    continue
                                name = entry.name.lower()
                                if name.endswith(".json.disabled"):
                                    txt = entry.read_text(encoding="utf-8", errors="ignore")
                                    if _try_parse_manifest_json(txt) is not None:
                                        flat_manifests.append((entry, True))
                                elif name.endswith(".json"):
                                    txt = entry.read_text(encoding="utf-8", errors="ignore")
                                    if _try_parse_manifest_json(txt) is not None:
                                        flat_manifests.append((entry, False))
                        except Exception:
                            pass

                    if flat_manifests:
                        # 为每个 manifest 都建一个 ModInfo，共享同一个 workshop_id
                        for mf_path, is_disabled in flat_manifests:
                            manifest = _try_parse_manifest_json(mf_path.read_text(encoding="utf-8", errors="ignore"))
                            if manifest is None:
                                continue
                            info = ModInfo(
                                manifest=manifest,
                                folder_name=c.name,       # pub_dir 名（workshop item id）
                                install_dir=c,            # 安装目录就是 pub_dir 根
                                source="disabled" if is_disabled else "workshop",
                                workshop_id=workshop_id,
                            )
                            _add(info)
                            any_scanned = True
                        continue  # 跳过下面的单次 _scan_folder 路径

                    # 嵌套目录场景 / 单 manifest 平铺场景：保持原有逻辑
                    info = _scan_folder(c, source="workshop", workshop_id=workshop_id, allow_disabled_manifest=False)
                    if info:
                        _add(info)
                        any_scanned = True
                        continue
                    info_d = _scan_folder(c, source="disabled", workshop_id=workshop_id, allow_disabled_manifest=True)
                    if info_d:
                        _add(info_d)
                        any_scanned = True
                        continue

                # ---- 兜底：有子文件夹但子文件夹里没 manifest（比如 CookieCursor 有 Cursors/ 子目录但 manifest 平铺根）----
                if not any_scanned and direct_mods and not nested_ok:
                    # 直接扫 pub_dir 根（平铺多 manifest 路径）
                    flat_manifests: list[tuple[Path, bool]] = []
                    try:
                        for entry in sorted(pub_dir.iterdir()):
                            if not entry.is_file():
                                continue
                            name = entry.name.lower()
                            if name.endswith(".json.disabled"):
                                txt = entry.read_text(encoding="utf-8", errors="ignore")
                                if _try_parse_manifest_json(txt) is not None:
                                    flat_manifests.append((entry, True))
                            elif name.endswith(".json"):
                                txt = entry.read_text(encoding="utf-8", errors="ignore")
                                if _try_parse_manifest_json(txt) is not None:
                                    flat_manifests.append((entry, False))
                    except Exception:
                        pass
                    for mf_path, is_disabled in flat_manifests:
                        manifest = _try_parse_manifest_json(mf_path.read_text(encoding="utf-8", errors="ignore"))
                        if manifest is None:
                            continue
                        info = ModInfo(
                            manifest=manifest,
                            folder_name=pub_dir.name,
                            install_dir=pub_dir,
                            source="disabled" if is_disabled else "workshop",
                            workshop_id=workshop_id,
                        )
                        _add(info)
        except Exception as e:
            result.errors.append(f"扫描 workshop_dir {game.workshop_dir} 失败: {e}")
        if skipped_unsubscribed:
            from .logger import get_logger
            get_logger("scanner.workshop").info(
                "跳过 %d 个未订阅的工坊残留目录（游戏不会加载）: %s",
                len(skipped_unsubscribed), ", ".join(skipped_unsubscribed),
            )

    return result


# --------------------------------------------------------------------------- #
# Helpers for store.py: move folder between mods/ <-> .mods_disabled/
# --------------------------------------------------------------------------- #


def _safe_move(src: Path, dst: Path, *, backup_label: str = "bak") -> Optional[Path]:
    """安全搬移 src → dst，目标已存在时先原子备份，再搬移。

    为什么不用裸 shutil.move：Windows 下 shutil.move 无法原子覆盖已存在目标——
    先 os.rename 失败（FileExistsError）后降级为 copy2+unlink，并发场景下极易因
    临时文件被覆盖/删除而抛 FileNotFoundError（见项目记忆 Lessons）。这里统一改为：
      1) dst 已存在 → os.replace(dst, dst.<label>-<ts>)（同分区原子重命名，挪走旧目标）
      2) shutil.move(src, dst)（此时 dst 不存在 → 直接 rename，原子）
    返回被挪走的备份路径（未发生备份则 None）。
    """
    backup: Optional[Path] = None
    if dst.exists():
        backup = dst.with_name(f"{dst.name}.{backup_label}-{int(time.time())}")
        n = 1
        while backup.exists():  # 同秒重复时加序号，避免覆盖已有备份
            backup = dst.with_name(f"{dst.name}.{backup_label}-{int(time.time())}-{n}")
            n += 1
        os.replace(str(dst), str(backup))
    shutil.move(str(src), str(dst))
    return backup


def locate_mod_by_id(mods: dict[str, ModInfo], mod_id: str) -> Optional[ModInfo]:
    return mods.get(mod_id.lower())


def move_mod_between(
    info: ModInfo,
    target_root: Path,
    target_source: Optional[str] = None,
) -> ModInfo:
    """把一个 mod 从当前位置搬到 target_root（比如 mods_dir 或 disabled_dir）。
    新增 target_source 参数：显式指定目标 source，不再靠目录名猜，避免大小写变体 ("Mods"/"mods") 判断错。
    """
    target = target_root / info.folder_name
    if target.resolve() == info.install_dir.resolve():
        # 已经在目标位置：如 target_source 指定则仍修正 source
        if target_source is None or info.source == target_source:
            return info
        return ModInfo(
            manifest=info.manifest,
            folder_name=target.name,
            install_dir=target,
            source=target_source,
            workshop_id=info.workshop_id,
        )
    # 目标已存在（可能上次留下）→ 先原子备份再搬移，避免 Windows 非原子覆盖
    _safe_move(info.install_dir, target, backup_label="old")
    if target_source is None:
        # 保持兼容：没传就根据目录名猜（走宽松判断忽略大小写和变体集合）
        tn = target_root.name.lower()
        if tn in {n.lower() for n in MODS_DIR_VARIANTS}:
            new_state = "local"
        elif tn in {n.lower() for n in DISABLED_DIR_VARIANTS}:
            new_state = "disabled"
        else:
            new_state = info.source
    else:
        new_state = target_source
    return ModInfo(
        manifest=info.manifest,
        folder_name=target.name,
        install_dir=target,
        source=new_state,
        workshop_id=info.workshop_id,
    )


def move_workshop_mod_to_mods(info: ModInfo, game: "GameInstall") -> ModInfo:
    """把 workshop mod 从 workshop 源目录搬到 Mods/workshop-<ws_id>。

    停用 → 启用：workshop/content/<AppID>/<ws_id>  →  Mods/workshop-<ws_id>
    （如果 install_dir 是 workshop 源目录的子目录，整目录搬到 Mods/ 下，保留内部结构）

    防止文件丢失：完整搬运整个目录，不删除任何文件。
    返回更新后的 ModInfo（source 变成 "local"，workshop_id 保留）。
    """
    from .logger import get_logger
    log = get_logger("scanner.workshop.move")

    if info.workshop_id is None:
        raise ValueError(f"move_workshop_mod_to_mods: mod={info.mod_id} 没有 workshop_id")

    stub_name = workshop_stub_folder_name(info)
    target = game.mods_dir / stub_name

    src = info.install_dir
    if src is None:
        raise ValueError(f"move_workshop_mod_to_mods: mod={info.mod_id} install_dir=None")

    # 目标已存在（可能上次启用留下的，或同 id 的本地 mod）→ 先原子备份再搬移
    log.info("move_workshop_mod_to_mods: %s -> %s", src, target)
    _safe_move(src, target, backup_label="old")

    # 搬运后确保 manifest 是正常的 .json（不是 .json.disabled）
    # 否则 STS2 扫不到 manifest，mod 不会被加载
    dis_manifest = is_workshop_mod_manifest_disabled(target)
    if dis_manifest is not None:
        if dis_manifest.name.lower().endswith(".json.disabled"):
            new_name = dis_manifest.name[: -len(".disabled")]
        else:
            new_name = dis_manifest.stem
        restored = dis_manifest.with_name(new_name)
        # 若目标 .json 已存在，_safe_move 会先原子备份它；再搬移 manifest
        if restored != dis_manifest:
            log.info("move_workshop_mod_to_mods: 还原 manifest %s -> %s", dis_manifest, restored)
            _safe_move(dis_manifest, restored)

    # 重新读 manifest 验证搬运成功
    manifest = read_mod_manifest(target, allow_disabled=False)
    if manifest is None:
        log.error("move_workshop_mod_to_mods: 搬运后 %s 读不到 manifest", target)
    return ModInfo(
        manifest=manifest or info.manifest,
        folder_name=stub_name,
        install_dir=target,
        source="local",  # 搬到 Mods/ 下就是 local
        workshop_id=info.workshop_id,
    )


def move_workshop_mod_to_disabled(info: ModInfo, game: "GameInstall") -> ModInfo:
    """把 workshop mod 从 Mods/ 搬到 .mods_disabled/workshop-<ws_id>。

    启用 → 停用：Mods/workshop-<ws_id>  →  .mods_disabled/workshop-<ws_id>
    （不搬回 workshop 源目录，因为搬回 = STS2 扫描 workshop 目录加载 = 启用态，停用无效）

    防止文件丢失：完整搬运整个目录，不删除任何文件。
    返回更新后的 ModInfo（source 变成 "disabled"，workshop_id 保留）。
    """
    from .logger import get_logger
    log = get_logger("scanner.workshop.move")

    if info.workshop_id is None:
        raise ValueError(f"move_workshop_mod_to_disabled: mod={info.mod_id} 没有 workshop_id")

    stub_name = workshop_stub_folder_name(info)
    target = game.disabled_dir / stub_name

    src = info.install_dir
    if src is None:
        raise ValueError(f"move_workshop_mod_to_disabled: mod={info.mod_id} install_dir=None")

    # 目标已存在 → 先原子备份再搬移
    log.info("move_workshop_mod_to_disabled: %s -> %s", src, target)
    _safe_move(src, target, backup_label="old")

    # 重新读 manifest（disabled 目录允许 .disabled 后缀的 manifest）
    manifest = read_mod_manifest(target, allow_disabled=True)
    if manifest is None:
        log.error("move_workshop_mod_to_disabled: 搬运后 %s 读不到 manifest", target)
    return ModInfo(
        manifest=manifest or info.manifest,
        folder_name=stub_name,
        install_dir=target,
        source="disabled",
        workshop_id=info.workshop_id,
    )


def restore_workshop_mod_from_disabled(info: ModInfo, game: "GameInstall") -> ModInfo:
    """扫描时自动修复：如果 mod 在 .mods_disabled/ 且 workshop 源目录没有 manifest
    （被搬走或 Steam 还没下载），不做事。
    如果 mod 在 .mods_disabled/ 但 workshop 源目录又有 manifest（Steam 重新下载了），
    也不做事——以 .mods_disabled/ 为权威（用户上次明确停用了）。
    """
    # 这个函数主要保留作为占位，目前自动修复逻辑直接在 scanner 里处理
    return info


# ---------------------------------------------------------------------------
# Sts2SkinManager 皮肤/卡面管理（皮肤选择配置 skin_choices.json）
# ---------------------------------------------------------------------------

# 角色 id -> 显示名（用于皮肤管理页签）
SKIN_ROLE_LABELS = {
    "ironclad": "铁甲战士",
    "silent": "静默猎手",
    "defect": "故障机器人",
    "necrobinder": "亡灵契约师",
    "regent": "储君",
}

# 卡面 mod id -> 角色 id 的关键词自动识别（匹配到即归为该角色）
CARD_ROLE_KEYWORDS = {
    "ironclad": ("ironclad", "iron", "铁甲", "战士"),
    "silent": ("silent", "lie", "hunter", "猎", "静默"),
    "defect": ("defect", "故障", "机"),
    "necrobinder": ("necrobinder", "necro", "亡灵", "契约"),
    "regent": ("regent", "储君", "摄政", "regent"),
}

# 角色 mod 自动识别关键词：出现在 name / description 里即视为「新增可游玩角色」的 mod。
# 只做粗识别，识别错/漏的由用户手动给该 mod 加上官方角色 tag 来修正。
CHARACTER_KEYWORDS = (
    "character", "playable", "角色", "可游玩", "新角色",
    "custom character", "自定義角色", "自定义角色",
)


def is_character_mod(info: "ModInfo") -> bool:
    """按名称/描述关键词粗判「角色 mod」（新增可游玩角色）。"""
    hay = " ".join([
        info.display_name or "",
        info.manifest.description or "",
    ]).lower()
    return any(kw.lower() in hay for kw in CHARACTER_KEYWORDS)

# 皮肤/卡面配置相对路径：%APPDATA%\SlayTheSpire2\Sts2SkinManager\skin_choices.json
SKIN_CHOICES_DIRNAME = "Sts2SkinManager"
SKIN_CHOICES_FILENAME = "skin_choices.json"


def locate_skin_choices() -> Optional[Path]:
    """定位 Sts2SkinManager 的皮肤选择配置 skin_choices.json；找不到返回 None。"""
    roaming = os.environ.get("APPDATA")
    if not roaming:
        return None
    p = Path(roaming) / "SlayTheSpire2" / SKIN_CHOICES_DIRNAME / SKIN_CHOICES_FILENAME
    if p.is_file():
        return p
    return None


def load_skin_choices() -> Optional[dict]:
    """读取 skin_choices.json；不存在或损坏返回 None。"""
    p = locate_skin_choices()
    if p is None:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_skin_choices(data: dict) -> bool:
    """原子写回 skin_choices.json（.tmp → move），失败返回 False。"""
    p = locate_skin_choices()
    if p is None:
        return False
    try:
        _atomic_write_json(p, data)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 游戏日志（STS2 = Godot，日志在 %APPDATA%\SlayTheSpire2\logs\godot.log）
# ---------------------------------------------------------------------------

GAME_LOG_RELPATH = ("SlayTheSpire2", "logs", "godot.log")


def locate_game_log() -> Optional[Path]:
    """定位 STS2 游戏日志；找不到返回 None。"""
    roaming = os.environ.get("APPDATA")
    if not roaming:
        return None
    p = Path(roaming).joinpath(*GAME_LOG_RELPATH)
    return p if p.is_file() else None


# ---------------------------------------------------------------------------
# 完整游戏日志查看（管理器日志面板用）
#
# BaseLib 没有独立日志文件，它把日志写进同一个 godot.log（行首带 [BaseLib] 前缀），
# 游戏内那个 NLogWindow 读的也是它。游戏每次启动会把上次的 godot.log 轮转为
# godot<时间戳>.log，所以要同时枚举当前会话 + 历史轮转文件，供前端切换查看。
# 日志行本身不带时间戳，时间只能靠文件名/文件 mtime 判断。
# 以下函数一律只读，不改动任何文件。
# ---------------------------------------------------------------------------

_GAME_LOG_LEVEL_RANK = {"other": 0, "info": 1, "warn": 2, "error": 3}
_GAME_LOG_MIN_RANK = {"all": 0, "info": 1, "warn": 2, "error": 3}


def _classify_game_log_line(s: str) -> Optional[str]:
    """按行首标签归类等级；无法识别（缩进的堆栈/续行）返回 None，交由调用方继承上一行。"""
    if s.startswith("[ERROR]") or s.startswith("[FATAL]"):
        return "error"
    if s.startswith("[WARN]"):
        return "warn"
    if s.startswith("[INFO]"):
        return "info"
    if s.startswith(("ERROR:", "SCRIPT ERROR:", "USER ERROR:")):
        return "error"
    if s.startswith(("WARNING:", "USER WARNING:")):
        return "warn"
    return None


def list_game_logs() -> list[dict]:
    """列出 logs 目录下的 .log 文件（当前 godot.log + 历史轮转 godot<ts>.log）。

    返回 [{name, path, size, mtime, is_current}]；godot.log 置顶，其余按修改时间倒序。
    """
    roaming = os.environ.get("APPDATA")
    if not roaming:
        return []
    d = Path(roaming).joinpath(*GAME_LOG_RELPATH[:-1])
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in d.glob("*.log"):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.name,
            "path": str(p),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "is_current": p.name == GAME_LOG_RELPATH[-1],
        })
    out.sort(key=lambda x: (not x["is_current"], -x["mtime"]))
    return out


def read_game_log(
    file_name: Optional[str] = None,
    level: str = "all",
    keyword: str = "",
    use_regex: bool = False,
    max_lines: int = 3000,
    max_bytes: int = 4 * 1024 * 1024,
) -> dict:
    """读取并逐行解析指定游戏日志，供管理器的日志面板展示。

    - file_name：logs 目录下的文件名；None/空 表示当前 godot.log
    - level：最低显示等级（all / info / warn / error，warn 表示 warn 及以上）
    - keyword：关键词过滤（use_regex=True 时按正则匹配）
    """
    files = list_game_logs()
    target: Optional[Path] = None
    if file_name:
        safe = Path(file_name).name  # 只取文件名，避免路径穿越
        for f in files:
            if f["name"] == safe:
                target = Path(f["path"])
                break
        if target is None:
            target = locate_game_log()
    else:
        target = locate_game_log()

    if target is None or not target.is_file():
        return {
            "available": False, "files": files, "lines": [],
            "name": file_name or "", "path": "",
            "counts": {"info": 0, "warn": 0, "error": 0, "other": 0},
            "total": 0, "shown": 0, "truncated": False,
        }

    try:
        size = target.stat().st_size
        with open(target, "r", encoding="utf-8", errors="ignore") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # 丢弃被截断的半行
            text = f.read()
    except OSError as e:
        return {
            "available": False, "files": files, "lines": [],
            "name": target.name, "path": str(target), "error": str(e),
            "counts": {"info": 0, "warn": 0, "error": 0, "other": 0},
            "total": 0, "shown": 0, "truncated": False,
        }

    min_rank = _GAME_LOG_MIN_RANK.get((level or "all").lower(), 0)
    kw = (keyword or "").strip()
    rx = None
    if kw and use_regex:
        try:
            rx = re.compile(kw, re.IGNORECASE)
        except re.error:
            rx = None
    kw_low = kw.lower()

    counts = {"info": 0, "warn": 0, "error": 0, "other": 0}
    out_lines: list[dict] = []
    total = 0
    last_level = "other"
    for raw in text.splitlines():
        total += 1
        lvl = _classify_game_log_line(raw)
        if lvl is not None:
            last_level = lvl
        else:
            # 缩进的续行（堆栈帧等）继承上一行等级，保持与所属条目一致
            lvl = last_level if raw[:1].isspace() else "other"
        counts[lvl] = counts.get(lvl, 0) + 1
        if _GAME_LOG_LEVEL_RANK.get(lvl, 0) < min_rank:
            continue
        if kw:
            hit = (rx.search(raw) is not None) if rx is not None else (kw_low in raw.lower())
            if not hit:
                continue
        out_lines.append({"level": lvl, "text": raw})

    truncated = size > max_bytes
    if len(out_lines) > max_lines:
        out_lines = out_lines[-max_lines:]  # 保留最新的尾部
        truncated = True

    return {
        "available": True,
        "files": files,
        "name": target.name,
        "path": str(target),
        "lines": out_lines,
        "counts": counts,
        "total": total,
        "shown": len(out_lines),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# 扫描进度（供前端轮询展示进度条）
# ---------------------------------------------------------------------------
import threading as _threading

_scan_progress: dict = {
    "running": False,
    "stage": "",         # "scan_mods" / "fetch_deps" / "finalize"
    "current": "",       # 当前正在处理的 mod 名
    "done": 0,
    "total": 0,
    "result": None,      # 完成后存 get_state() 结果
    "error": "",
}
_scan_progress_lock = _threading.Lock()


def reset_scan_progress(total: int = 0) -> None:
    """重置进度计数器（清 done/current/stage/total，但**不碰 running 标志**）。

    running 标志由 api.refresh() 这个异步入口独占管理——store.refresh() 是同步调用链，
    会在 main.py 里提前被调一次，不能在这里设置 running=True，否则没人调 finish 置回 False，
    前端下次轮询 get_scan_progress 就永远看到 running=true → 提示"上一次扫描未完成"。
    """
    with _scan_progress_lock:
        _scan_progress["stage"] = "scan_mods"
        _scan_progress["current"] = ""
        _scan_progress["done"] = 0
        _scan_progress["total"] = max(0, int(total))
        _scan_progress["result"] = None
        _scan_progress["error"] = ""


def tick_scan_progress(current: str = "", done_increment: int = 1) -> None:
    with _scan_progress_lock:
        _scan_progress["done"] = _scan_progress.get("done", 0) + done_increment
        # 2026-10-13 修复：done 不超过 total，防止出现 "190/160" 这种反直觉显示
        total = _scan_progress.get("total", 0)
        if total > 0 and _scan_progress["done"] > total:
            _scan_progress["done"] = total
        if current:
            _scan_progress["current"] = current


def set_scan_stage(stage: str) -> None:
    """切换扫描阶段（scan_mods → fetch_deps → finalize）。"""
    with _scan_progress_lock:
        _scan_progress["stage"] = stage
        _scan_progress["current"] = ""


def finish_scan_progress(result: dict | None = None, error: str = "") -> None:
    with _scan_progress_lock:
        _scan_progress["running"] = False
        _scan_progress["stage"] = "done"
        _scan_progress["current"] = ""
        _scan_progress["result"] = result
        _scan_progress["error"] = error or ""


def get_scan_progress() -> dict:
    with _scan_progress_lock:
        return dict(_scan_progress)


# ---------------------------------------------------------------------------
# 存档目录扫描（profile1/2/3 × official/modded）
# ---------------------------------------------------------------------------

def list_game_save_slots() -> dict:
    """扫描 STS2 所有存档槽位，返回 profiles 列表。

    存档结构：
        %APPDATA%\\SlayTheSpire2\\steam\\<steamid64>\\
          ├── profile1\\saves\\          ← 官方槽位 1
          ├── profile2\\saves\\          ← 官方槽位 2
          ├── profile3\\saves\\          ← 官方槽位 3
          └── modded\\
              ├── profile1\\saves\\      ← 带 mod 槽位 1
              ├── profile2\\saves\\
              └── profile3\\saves\\

    返回 {"steam_id": str, "profiles": [...]}；profiles 可能为空（没检测到存档）。
    """
    roaming = os.environ.get("APPDATA", "")
    if not roaming:
        return {"steam_id": None, "profiles": []}

    base = Path(roaming) / "SlayTheSpire2"
    if not base.is_dir():
        return {"steam_id": None, "profiles": []}

    steam_root = base / "steam"
    steam_id = None
    if steam_root.is_dir():
        # steam 目录下的第一个子目录就是当前登录用户的 steamid64（通常只有一个）
        for child in steam_root.iterdir():
            if child.is_dir() and child.name.isdigit() and len(child.name) >= 15:
                steam_id = child.name
                break

    profiles: list[dict] = []
    for slot in (1, 2, 3):
        slot_info: dict = {"profile": slot, "official": None, "modded": None}

        # 官方槽位
        official_path = steam_root / steam_id / f"profile{slot}" / "saves" if steam_id else None
        if official_path and official_path.is_dir():
            slot_info["official"] = _scan_save_dir(official_path)

        # modded 槽位
        modded_path = steam_root / steam_id / "modded" / f"profile{slot}" / "saves" if steam_id else None
        if modded_path and modded_path.is_dir():
            slot_info["modded"] = _scan_save_dir(modded_path)

        # 3 个槽位始终返回——即使全空也显示占位，方便用户知道"我还可以切存档"
        profiles.append(slot_info)

    return {"steam_id": steam_id, "profiles": profiles, "base_dir": str(base)}


def _scan_save_dir(saves_dir: Path) -> dict:
    """扫描单个 saves 目录，返回文件列表和统计。"""
    files: list[dict] = []
    total_size = 0
    latest_mtime = 0
    try:
        for child in sorted(saves_dir.rglob("*")):
            if child.is_file():
                try:
                    st = child.stat()
                    total_size += st.st_size
                    latest_mtime = max(latest_mtime, st.st_mtime)
                    files.append({
                        "name": str(child.relative_to(saves_dir)).replace("\\", "/"),
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    })
                except OSError:
                    continue
    except OSError:
        pass
    return {
        "path": str(saves_dir),
        "file_count": len(files),
        "total_size": total_size,
        "latest_mtime": latest_mtime,
        "files": files,
    }


def get_save_slot_dir(profile_num: int, kind: str) -> Optional[Path]:
    """根据 profile 编号 + 官方/modded，计算存档目录。找不到返回 None。"""
    result = list_game_save_slots()
    base = Path(result["base_dir"]) if result.get("base_dir") else None
    steam_id = result.get("steam_id")
    if not base or not steam_id:
        return None

    if kind == "official":
        saves = base / "steam" / steam_id / f"profile{profile_num}" / "saves"
    else:  # modded
        saves = base / "steam" / steam_id / "modded" / f"profile{profile_num}" / "saves"
    return saves if saves.is_dir() else None
