"""
store_core —— Spire Mod Manager 的标签常量 & 自动归类辅助。

从 store.py 拆分而来（2026-10-14 O1-1 重构）。
此模块只含纯常量和纯函数，不持有 ModManager 实例，也不导入 store.py 任何内容——
被 api.py / store.py / scanner.py 多个文件 import，需保持单向依赖。
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, asdict, field
from typing import Callable, Iterable, Optional

from .scanner import SKIN_ROLE_LABELS


__all__ = [
    # 关键词元组
    "AUTO_TAG_TOOL_KEYWORDS", "AUTO_TAG_LIB_KEYWORDS", "AUTO_TAG_APPEAR_KEYWORDS",
    "STRONG_APPEAR_KEYWORDS", "PCK_APPEAR_KEYWORDS", "FIX_KEYWORDS",
    # 自动标签常量
    "AUTO_TAG_GAMEPLAY", "AUTO_TAG_CHARACTER", "AUTO_TAG_RELIC", "AUTO_TAG_NEW_CARD",
    "AUTO_TAG_EVENT", "AUTO_TAG_POTION", "AUTO_TAG_ENEMY", "AUTO_TAG_POWER", "AUTO_TAG_MAP",
    "AUTO_TAG_APPEAR", "AUTO_TAG_SKIN", "AUTO_TAG_CARD_ART", "AUTO_TAG_VOICE",
    "AUTO_TAG_LIB", "AUTO_TAG_TOOL", "AUTO_TAG_FIX", "AUTO_TAG_OTHER",
    # 内置/细分标签映射
    "BUILTIN_TAGS", "GAMEPLAY_SUBTAGS", "APPEAR_SUBTAGS",
    "WORKSHOP_TAG_MAP", "_WORKSHOP_TAG_PRIORITY", "OFFICIAL_CHARACTER_TAGS", "LANGUAGES",
    # I18N 映射表
    "TAG_I18N", "TAG_I18N_EN2ZH",
    # 辅助函数（下划线开头需显式列在 __all__ 才会被 from X import * 导出）
    "_kw_in", "_serialized",
    # 数据类
    "ManagerState",
    # scanner 反向 re-export（store_core 通过 store re-export 提供给下游）
    "SKIN_ROLE_LABELS",
]


AUTO_TAG_TOOL_KEYWORDS = (
    "tracker", "editor", "hotkey", "tool", "quick", "save",
    "menu", "cheat", "qol", "quality of life", "ui", "config", "settings",
    "热键", "工具", "快速", "管理器", "配置", "设置", "界面",
    "便捷", "便利", "生活质量",
)
AUTO_TAG_LIB_KEYWORDS = (
    "lib", "library", "core", "framework", "库", "前置", "依赖",
)
AUTO_TAG_APPEAR_KEYWORDS = (
    "skin", "voice", "portrait", "art", "animation", "card", "anime",
    "皮肤", "语音", "卡面", "卡图", "美化", "立绘", "动画", "外观", "音效",
)
# mod 名字里的「强外观词」：命中即优先归「外观」，避免皮肤/卡面 mod 因描述里提到
# 「config options」等词被误判为工具（如 Chizuru Ironclad Skin）。
STRONG_APPEAR_KEYWORDS = ("skin", "portrait", "卡面", "皮肤", "立绘", "卡图")
# PCK 资源路径关键词：纯美术 mod（无 DLL）的 .pck 里出现这些资源路径时，归「外观」
PCK_APPEAR_KEYWORDS = (
    "card", "portrait", "skin", "character", "charselect", "body", "face",
)


def _kw_in(text: str, kw: str) -> bool:
    """关键词匹配：英文用「词边界」避免子串误伤（chart→art、discard→card），中文用子串。"""
    if kw.isascii():
        return re.search(rf"\b{re.escape(kw)}", text) is not None
    return kw in text


# 自动标签候选（与内置 manager_tags 对应，用户可删改）
AUTO_TAG_GAMEPLAY = "玩法"
AUTO_TAG_CHARACTER = "新角色"
AUTO_TAG_RELIC = "新遗物"
AUTO_TAG_NEW_CARD = "新卡牌"
AUTO_TAG_EVENT = "新事件"
AUTO_TAG_POTION = "新药水"
AUTO_TAG_ENEMY = "新敌人"
AUTO_TAG_POWER = "新能力"
AUTO_TAG_MAP = "新地图"
AUTO_TAG_APPEAR = "外观"
AUTO_TAG_SKIN = "皮肤"
AUTO_TAG_CARD_ART = "卡面"
AUTO_TAG_VOICE = "语音"
AUTO_TAG_LIB = "依赖"
AUTO_TAG_TOOL = "工具"
AUTO_TAG_FIX = "修复"
AUTO_TAG_OTHER = "其它"

# 内置标签池（供默认配置与旧配置迁移使用）
BUILTIN_TAGS = (
    AUTO_TAG_GAMEPLAY, AUTO_TAG_CHARACTER, AUTO_TAG_RELIC, AUTO_TAG_NEW_CARD,
    AUTO_TAG_EVENT, AUTO_TAG_POTION, AUTO_TAG_ENEMY, AUTO_TAG_POWER, AUTO_TAG_MAP,
    AUTO_TAG_APPEAR, AUTO_TAG_SKIN, AUTO_TAG_CARD_ART, AUTO_TAG_VOICE,
    AUTO_TAG_LIB, AUTO_TAG_TOOL, AUTO_TAG_FIX, AUTO_TAG_OTHER,
)

# 修复/补丁关键词：优先于皮肤/卡面（如「Mesugaki Regent Skin Fix」是修复补丁，不是皮肤）
# 注意不含英文 "patch"/"compat"——它们会命中库框架里的 "patching"/"compatible"（技术术语）
FIX_KEYWORDS = ("fix", "修复", "补丁", "兼容")

# 玩法细分：affects_gameplay=true 时按顺序命中（用 STS2 官方术语）
GAMEPLAY_SUBTAGS = (
    (AUTO_TAG_CHARACTER, ("character", "playable", "角色", "可游玩", "新角色", "custom character", "自定义角色")),
    (AUTO_TAG_RELIC, ("relic", "遗物")),
    (AUTO_TAG_EVENT, ("event", "事件")),
    (AUTO_TAG_NEW_CARD, ("card", "cards", "卡牌")),
    (AUTO_TAG_POTION, ("potion", "药水")),
    (AUTO_TAG_ENEMY, ("enemy", "monster", "boss", "敌人", "怪物")),
    (AUTO_TAG_POWER, ("power", "能力")),
    (AUTO_TAG_MAP, ("map", "地图")),
)

# 外观细分：affects_gameplay=false 时按顺序命中
APPEAR_SUBTAGS = (
    (AUTO_TAG_SKIN, ("skin", "皮肤")),
    (AUTO_TAG_CARD_ART, ("portrait", "card", "卡面", "卡图", "立绘")),
    (AUTO_TAG_VOICE, ("voice", "语音", "音效", "audio")),
)

# 创意工坊 Steam tag -> 管理器自动标签。
# 注：Steam 还会返回 language tags (english/schinese/japanese...) —
#     这些在 _workshop_tag() 里跳过，不算分类信号。
WORKSHOP_TAG_MAP: dict[str, str] = {
    # ── 依赖 / 库（骨架） ──
    "library": AUTO_TAG_LIB, "dependency": AUTO_TAG_LIB, "framework": AUTO_TAG_LIB,
    "lib": AUTO_TAG_LIB, "tools & apis": AUTO_TAG_LIB, "tools & api": AUTO_TAG_LIB,
    "api": AUTO_TAG_LIB, "库": AUTO_TAG_LIB, "依赖": AUTO_TAG_LIB, "前置": AUTO_TAG_LIB,
    # ── 工具 / QoL ──
    "utility": AUTO_TAG_TOOL, "tool": AUTO_TAG_TOOL, "config": AUTO_TAG_TOOL,
    "ui": AUTO_TAG_TOOL, "interface": AUTO_TAG_TOOL, "qol": AUTO_TAG_TOOL,
    "quality of life": AUTO_TAG_TOOL, "enhancement": AUTO_TAG_TOOL,
    "expansion": AUTO_TAG_GAMEPLAY,  # "扩展"：大内容扩展（新角色/卡牌/遗物）或玩法扩展，不是工具
    "工具": AUTO_TAG_TOOL, "界面": AUTO_TAG_TOOL, "便捷": AUTO_TAG_TOOL,
    # ── 玩法 / 新内容 ──
    "gameplay": AUTO_TAG_GAMEPLAY, "content": AUTO_TAG_GAMEPLAY,
    # 细分子标签（优先用最具体的分类）
    "character": AUTO_TAG_CHARACTER, "characters": AUTO_TAG_CHARACTER, "角色": AUTO_TAG_CHARACTER,
    "relic": AUTO_TAG_RELIC, "relics": AUTO_TAG_RELIC, "遗物": AUTO_TAG_RELIC,
    "card": AUTO_TAG_NEW_CARD, "cards": AUTO_TAG_NEW_CARD, "卡牌": AUTO_TAG_NEW_CARD,
    "potion": AUTO_TAG_POTION, "potions": AUTO_TAG_POTION, "药水": AUTO_TAG_POTION,
    "enemy": AUTO_TAG_ENEMY, "enemies": AUTO_TAG_ENEMY, "monster": AUTO_TAG_ENEMY,
    "monsters": AUTO_TAG_ENEMY, "boss": AUTO_TAG_ENEMY, "敌人": AUTO_TAG_ENEMY,
    "power": AUTO_TAG_POWER, "powers": AUTO_TAG_POWER, "能力": AUTO_TAG_POWER,
    "event": AUTO_TAG_EVENT, "events": AUTO_TAG_EVENT, "事件": AUTO_TAG_EVENT,
    "map": AUTO_TAG_MAP, "maps": AUTO_TAG_MAP, "地图": AUTO_TAG_MAP,
    # 通用玩法
    "modifiers": AUTO_TAG_GAMEPLAY, "multiplayer": AUTO_TAG_GAMEPLAY,
    "balance": AUTO_TAG_GAMEPLAY, "rebalance": AUTO_TAG_GAMEPLAY,
    # ── 外观 / 美术 ──
    "skin": AUTO_TAG_SKIN, "skins": AUTO_TAG_SKIN, "皮肤": AUTO_TAG_SKIN,
    "cosmetic": AUTO_TAG_APPEAR, "cosmetics": AUTO_TAG_APPEAR, "appearance": AUTO_TAG_APPEAR,
    "art": AUTO_TAG_APPEAR, "animation": AUTO_TAG_APPEAR, "皮肤": AUTO_TAG_APPEAR, "外观": AUTO_TAG_APPEAR,
    "voice": AUTO_TAG_VOICE, "audio": AUTO_TAG_VOICE, "music": AUTO_TAG_VOICE,
    "语音": AUTO_TAG_VOICE, "音效": AUTO_TAG_VOICE,
    "portrait": AUTO_TAG_CARD_ART, "卡面": AUTO_TAG_CARD_ART, "卡图": AUTO_TAG_CARD_ART,
    # ── 修复 / 兼容 ──
    "silent": AUTO_TAG_FIX,  # 静默模式 / 修复兼容性
    # ── 兜底 ──
    "misc": AUTO_TAG_OTHER, "mod": AUTO_TAG_OTHER, "other": AUTO_TAG_OTHER,
    "enchantment": AUTO_TAG_OTHER,  # "附魔"：魔法系统扩展，不直接归为某类
    "ancient": AUTO_TAG_EVENT,  # 先古（Ancients 标签）——独立玩法模块
}

# ── 归一化：双侧一致 ─────────────────────────────────────────────────────────
# 启动时构建，只构建一次。两侧（输入侧 + WORKSHOP_TAG_MAP key）都过 normalize()。
def _normalize_tag(tag: str) -> str:
    """Steam tag 归一化：strip + lower，供两侧共用。"""
    return tag.strip().lower()

# WORKSHOP_TAG_MAP 的归一化索引（启动时从 WORKSHOP_TAG_MAP 构建）
# 用 __init_subclass__ 或模块级代码在 import 后立即构建
_NORMALIZED_MAP: dict[str, str] = {}

def _build_normalized_index():
    """启动时构建：{normalize(key): mapped_tag}"""
    _NORMALIZED_MAP.clear()
    for k, v in WORKSHOP_TAG_MAP.items():
        _NORMALIZED_MAP[_normalize_tag(k)] = v

_build_normalized_index()  # import 时立即执行

# ── 显式别名表（不用算法去复数） ──────────────────────────────────────────────
# Steam 原始 tag → WORKSHOP_TAG_MAP 里的标准 key。
# 所有别名都要在 WORKSHOP_TAG_MAP 里有对应的标准 key（通过归一化索引命中）。
# 这张表只处理 "拼写变体/常见替代"，不处理 IP/主题标签（那些进忽略清单）。
_TAG_ALIASES: dict[str, str] = {
    # 单复数（显式，不用算法去复数）→ 指向 WORKSHOP_TAG_MAP 里已有的标准 key
    "character": "characters",    # 单数 → 复数（WORKSHOP_TAG_MAP 里 "characters" → 新角色）
    "ancients": "ancient",        # 复数 → 单数（WORKSHOP_TAG_MAP 里 "ancient" → 新事件）
    "bosses": "boss",             # 复数 → 单数（WORKSHOP_TAG_MAP 里 "boss" → 新敌人）
    # 高频未映射 Steam tag → 补进 WORKSHOP_TAG_MAP 的标准 key
    "act": "event",               # Acts（章节/幕）→ 事件
    "acts": "event",
    "extensions": "expansion",    # 扩展 → expansion → 玩法
    "rooms": "map",               # 房间 → 地图
    "defectskin": "skin",         # 缺陷皮肤 → 皮肤（Hades 系列）
    "defectcard": "card",         # 缺陷卡牌 → 新卡牌
    "localization": "tool",       # 本地化 → 工具（翻译辅助）
}

# ── 显式忽略清单（IP/主题/政策标签，不进主标签竞争） ─────────────────────────
# 这些 tag 在 Steam 上高频出现，但描述的是"内容主题"而非"玩法类型"。
# 进主标签竞争会产生新 bug（比如打了 "touhou" + "cards" 的 Mod
# 本应归为"新卡牌"，但如果 touhou 进映射后被误判成"其它"）。
# 语言标签在 _workshop_tag() 里单独过滤（_LANG_TAGS）。
_TAG_IGNORE: frozenset[str] = frozenset({
    # IP / 主题标签（按统计高频排序）
    "touhou", "touhou project",      # 东方 Project
    "alice",                         # 碧蓝档案 Alice
    "blue archive",                  # 碧蓝档案
    "limbus company",                # Limbus Company
    "arknights",                     # 明日方舟
    "chaos zero nightmare",          # 混沌零夜
    # 内容政策徽标
    "ai-generated content",          # AI 生成内容
    # 其他非分类标签
    "co-op", "coop", "co operative",  # 多人合作（Multiplayer 已在映射）
})

# ── 语言标签（在 _workshop_tag() 里过滤） ─────────────────────────────────────
_LANG_TAGS: frozenset[str] = frozenset({
    "english", "schinese", "tchinese", "japanese", "koreana", "korean",
    "simplified chinese", "traditional chinese", "chinese", "简体中文",
    "russian", "french", "german", "spanish", "italian", "polish",
    "portuguese", "turkish", "dutch", "ukrainian",
})

# ── specificity（具体度）：三档制 ────────────────────────────────────────────
# 仅服务兜底排序路径（第 2 步落地后兜底排序只在无明确核心信号时触发）。
# 裁判：分歧集 56 条非空 → specificity 有理由，但九档精度伪 → 压三档。
# 定义：
#   HIGH = 玩家一眼能说出这 mod 是什么（角色/卡牌/遗物）
#   MED  = 玩家能说出这 mod 大致是什么类型（敌人/事件/药水/能力/地图）
#   LOW  = 玩家说不出来这 mod 具体是什么（工具/玩法/修复/其他/外观）
# 兜底排序键：(priority, specificity_tier, mapped_tag)
_TAG_SPECIFICITY: dict[str, str] = {
    # p1 依赖
    AUTO_TAG_LIB: "LOW",         # 依赖（优先级最高但玩家感知最低）
    # p2 工具
    AUTO_TAG_TOOL: "LOW",
    # p3 内容类
    AUTO_TAG_CHARACTER: "HIGH",  # 角色最具体
    AUTO_TAG_NEW_CARD: "HIGH",   # 卡牌
    AUTO_TAG_RELIC: "HIGH",      # 遗物
    AUTO_TAG_ENEMY: "MED",       # 敌人（STS2 里敌人 mod 可以是独立模块也可以是配套）
    AUTO_TAG_EVENT: "MED",       # 事件/先古
    AUTO_TAG_POTION: "MED",      # 药水
    AUTO_TAG_POWER: "MED",       # 能力
    AUTO_TAG_MAP: "MED",         # 地图（结构权重最高）
    AUTO_TAG_GAMEPLAY: "LOW",    # 玩法（最宽泛）
    # p4 外观
    AUTO_TAG_SKIN: "LOW",
    AUTO_TAG_CARD_ART: "LOW",
    AUTO_TAG_VOICE: "LOW",
    AUTO_TAG_APPEAR: "LOW",
    # p5 修复
    AUTO_TAG_FIX: "LOW",
    # p6 兜底
    AUTO_TAG_OTHER: "LOW",
}
_DEFAULT_SPEC = "LOW"

# ── 结构权重表：gameplay_signal 计算 ───────────────────────────────────────
# 方案 (a)：权重放强度里，MAP=阈值=2 单独即升格，阈值永远固定 2。
# 消灭"单强模块永不升格"的结构性漏洞（角色 + 一整个新幕 = 权重 2 → 升格）。
#
# 共现表校准结果：
#   MAP/幕 在角色/卡牌/遗物 mod 中配套占比 7-9% → 强独立，权重=2
#   EVENT/先古 在角色/卡牌/遗物 mod 中配套占比 63-70% → 弱独立，权重=1
#   EVENT 边际率 17%（全库基线），P(事件|角色)=21%（lift=1.24）→ 配套信号，权重=0
#   ENEMY 边际率 6%，P(敌人|角色)=10%（lift=1.67）→ 弱独立，权重=1
#   CHARACTER/CARD/RELIC/POTION → 配套信号，权重=0（天然是自己的配套）
#
# 判定：gameplay_signal >= GAMEPLAY_SIGNAL_THRESHOLD → 升格玩法扩展
#   gameplay_signal 计算**排除 core 自身**（不变式：派生量不能含 core）
#   玩法扩展 永远不能是默认值，只能从核心信号升上来
_TAG_STRUCTURAL_WEIGHT: dict[str, int] = {
    # 配套信号（权重=0，边际率高 / 条件概率 lift≈1）
    AUTO_TAG_CHARACTER: 0,
    AUTO_TAG_NEW_CARD: 0,
    AUTO_TAG_RELIC: 0,
    AUTO_TAG_POTION: 0,
    AUTO_TAG_EVENT: 0,       # 边际率 17%（基线），lift=1.24 → 配套
    # 弱独立模块（权重=1，边际率低 + lift>1，需要搭档才升格）
    AUTO_TAG_ENEMY: 1,       # 边际率 6%，lift=1.67 → 弱独立
    AUTO_TAG_POWER: 1,       # 新能力：单独能力 mod 少见
    # 强独立模块（权重=2，方案 (a)：单独即升格 = 阈值）
    AUTO_TAG_MAP: 2,         # 边际率 2%，lift=1.0（和基线一样低）→ 纯独立
    # 非内容类 / 纯机制词（权重=0，不参与 gameplay_signal）
    AUTO_TAG_GAMEPLAY: 0,
    AUTO_TAG_TOOL: 0,
    AUTO_TAG_LIB: 0,
    AUTO_TAG_SKIN: 0,
    AUTO_TAG_CARD_ART: 0,
    AUTO_TAG_VOICE: 0,
    AUTO_TAG_APPEAR: 0,
    AUTO_TAG_FIX: 0,
    AUTO_TAG_OTHER: 0,
}

GAMEPLAY_SIGNAL_THRESHOLD = 2  # 固定阈值，方案 (a)：MAP 单独即升格

# 工坊标签 → 优先级数字（越小越优先）。同一 mod 有多个 workshop tag 时，
# 选数字最小的那个（按功能层级：骨架 > 辅助 > 核心玩法 > 视觉 > 兜底）。
_WORKSHOP_TAG_PRIORITY: dict[str, int] = {
    AUTO_TAG_LIB: 1,     # 依赖/库 → 最优先（mod 的骨架）
    AUTO_TAG_TOOL: 2,    # 工具/QoL → 辅助
    AUTO_TAG_CHARACTER: 3, AUTO_TAG_RELIC: 3, AUTO_TAG_NEW_CARD: 3,
    AUTO_TAG_POTION: 3, AUTO_TAG_ENEMY: 3, AUTO_TAG_POWER: 3,
    AUTO_TAG_EVENT: 3, AUTO_TAG_MAP: 3,
    AUTO_TAG_GAMEPLAY: 3,  # 通用玩法和细分并列
    AUTO_TAG_SKIN: 4, AUTO_TAG_APPEAR: 4, AUTO_TAG_CARD_ART: 4, AUTO_TAG_VOICE: 4,
    AUTO_TAG_FIX: 5,
    AUTO_TAG_OTHER: 6,
}

# 官方角色标签（加入管理器标签池，供用户给皮肤/卡面/角色 mod 打角色 tag）
OFFICIAL_CHARACTER_TAGS = tuple(SKIN_ROLE_LABELS.values())

# 管理器标签中英文对照（中文 -> 英文）。用于设置页的界面语言切换（简中 / English）。
# 用户自定义标签不在映射内时，前端保持原样显示。
TAG_I18N = {
    AUTO_TAG_GAMEPLAY: "Gameplay",
    AUTO_TAG_CHARACTER: "New Character",
    AUTO_TAG_RELIC: "New Relic",
    AUTO_TAG_NEW_CARD: "New Card",
    AUTO_TAG_EVENT: "New Event",
    AUTO_TAG_POTION: "New Potion",
    AUTO_TAG_ENEMY: "New Enemy",
    AUTO_TAG_POWER: "New Power",
    AUTO_TAG_MAP: "New Map",
    AUTO_TAG_APPEAR: "Appearance",
    AUTO_TAG_SKIN: "Skin",
    AUTO_TAG_CARD_ART: "Card Art",
    AUTO_TAG_VOICE: "Voice",
    AUTO_TAG_LIB: "Dependency",
    AUTO_TAG_TOOL: "Tool",
    AUTO_TAG_FIX: "Fix",
    AUTO_TAG_OTHER: "Other",
}
# 官方角色标签的英文对照：直接复用 SKIN_ROLE_LABELS 的英文 id（首字母大写），保持单一来源。
TAG_I18N.update({label: rid.title() for rid, label in SKIN_ROLE_LABELS.items()})

# 工坊 / 本地 manifest 标签 英文 -> 中文 对照（中文界面下把英文 tag 显示成中文）。
# key 统一小写，前端做大小写不敏感匹配；value 复用管理器中文标签（保证和 TAG_I18N 互通）。
TAG_I18N_EN2ZH = {
    "library": AUTO_TAG_LIB, "lib": AUTO_TAG_LIB, "dependency": AUTO_TAG_LIB,
    "dependencies": AUTO_TAG_LIB, "framework": AUTO_TAG_LIB, "core": AUTO_TAG_LIB,
    "skin": AUTO_TAG_SKIN, "cosmetic": AUTO_TAG_APPEAR, "appearance": AUTO_TAG_APPEAR,
    "visual": AUTO_TAG_APPEAR, "anime": AUTO_TAG_APPEAR, "animation": AUTO_TAG_APPEAR,
    "voice": AUTO_TAG_VOICE, "audio": AUTO_TAG_VOICE, "music": AUTO_TAG_VOICE, "sound": AUTO_TAG_VOICE,
    "art": AUTO_TAG_CARD_ART, "portrait": AUTO_TAG_CARD_ART, "card art": AUTO_TAG_CARD_ART,
    "gameplay": AUTO_TAG_GAMEPLAY, "character": AUTO_TAG_CHARACTER, "content": AUTO_TAG_GAMEPLAY,
    "playable": AUTO_TAG_CHARACTER, "custom character": AUTO_TAG_CHARACTER,
    "relic": AUTO_TAG_RELIC, "relics": AUTO_TAG_RELIC,
    "card": AUTO_TAG_NEW_CARD, "cards": AUTO_TAG_NEW_CARD,
    "event": AUTO_TAG_EVENT, "events": AUTO_TAG_EVENT,
    "potion": AUTO_TAG_POTION, "potions": AUTO_TAG_POTION,
    "enemy": AUTO_TAG_ENEMY, "monster": AUTO_TAG_ENEMY, "boss": AUTO_TAG_ENEMY,
    "character": AUTO_TAG_CHARACTER, "characters": AUTO_TAG_CHARACTER,
    "cosmetic": AUTO_TAG_APPEAR, "cosmetics": AUTO_TAG_APPEAR,
    "power": AUTO_TAG_POWER,
    "modifiers": "修饰器",
    "map": AUTO_TAG_MAP, "utility": AUTO_TAG_TOOL, "tool": AUTO_TAG_TOOL,
    "qol": AUTO_TAG_TOOL,
    "config": AUTO_TAG_TOOL, "ui": AUTO_TAG_TOOL, "interface": AUTO_TAG_TOOL,
    "tracker": AUTO_TAG_TOOL, "editor": AUTO_TAG_TOOL, "hotkey": AUTO_TAG_TOOL,
    "quick": AUTO_TAG_TOOL, "save": AUTO_TAG_TOOL, "menu": AUTO_TAG_TOOL, "cheat": AUTO_TAG_TOOL,
    "fix": AUTO_TAG_FIX, "patch": AUTO_TAG_FIX, "compat": AUTO_TAG_FIX,
    # 角色标签（工坊直接用角色英文名做 tag）
    "silent": "静默猎手", "regent": "储君",
    # 工坊特殊分类标签
    "reskin": AUTO_TAG_SKIN, "tools & apis": AUTO_TAG_TOOL,
    "hud": AUTO_TAG_TOOL, "quality of life": AUTO_TAG_TOOL,
    "card portraits": AUTO_TAG_CARD_ART, "texture": AUTO_TAG_APPEAR,
    # 本地化标签
    "simplified chinese": "简体中文", "koreana": "韩语",
}

# 界面语言选项（简中 / English）
LANGUAGES = ("zh", "en")


# ============================================================================
# 锁装饰器（O1-2 从 store.py 搬来：粗粒度串行化公共写操作）
# ============================================================================

def _serialized(method):
    """粗粒度操作锁：串行化所有会改写扫描内存状态（self._mods / self._state）的公共操作。

    背景：api 层用 _IO_POOL 多线程派发，refresh / delete_mod / set_enabled 等若并发
    执行会互相踩内存（如一个线程迭代 _mods 时另一个线程 pop）。用一把 RLock 串行化，
    RLock 可重入，因此方法内部再调用被装饰的方法（如 delete_mod → set_enabled → refresh）
    不会死锁。
    """
    @functools.wraps(method)
    def _wrapper(self, *args, **kwargs):
        with self._op_lock:
            return method(self, *args, **kwargs)

    return _wrapper


# ============================================================================
# 状态 dataclass（O1-2 从 store.py 搬来：ModManager 内部状态模型）
# ============================================================================

@dataclass
class ManagerState:
    """ModManager 的完整持久化状态。"""
    enabled_order: list[str]
    disabled_order: list[str]
    # 活动/非活动副本选择逻辑已于 2026-10 删除（改由 source_states 多 source 独立启停）
    manual_game_root: Optional[str] = None
    # 手动指定额外 mods 目录（用 ";" 或 "|" 分隔多个；为空则只扫游戏默认 Mods/ + workshop）
    manual_mods_dirs: Optional[str] = None
    # 手动指定创意工坊目录（workshop/content/<AppID>；为空则自动从 Steam 库反推）
    manual_workshop_dir: Optional[str] = None
    # 手动指定本地 Mods 目录（游戏 Mods/ 文件夹；为空则用 game_root/Mods）
    manual_mods_dir: Optional[str] = None
    # 手动指定存档目录（settings.save 所在目录；为空则自动定位 %APPDATA%\SlayTheSpire2\...）
    manual_save_dir: Optional[str] = None
    # 是否启用 BetterModMenu 同步：
    #   False (默认) = 完全忽略 BetterModMenu，只使用 STS2 游戏源逻辑
    #   True         = 已安装 BetterModMenu 时，额外同步它的 mod_settings.json
    prefer_better_mod_menu: bool = False
    # 整合包列表：每个元素 dict {id, name, created_at, mods:[mod_id,...]}
    packs: list[dict] = field(default_factory=list)
    # 管理器标签（全局标签池，内置 + 用户自定义）
    manager_tags: list[str] = field(default_factory=list)
    # 每个 mod 的管理器标签：key=lower mod_id, value=[标签,...]
    mod_tags: dict[str, list[str]] = field(default_factory=dict)
    # 卡面 mod 的手动角色归属：key=lower mod_id, value=角色 id（覆盖自动识别）
    card_roles: dict[str, str] = field(default_factory=dict)
    # 已识别为「角色 mod」的 mod id（lower）。持久化，避免清除工坊缓存后角色识别失效
    character_mods: list[str] = field(default_factory=list)
    # 界面语言：zh = 简体中文，en = English（默认 zh）
    language: str = "zh"
    # P0: 自动检查 Steam 工坊详情（在线标题/更新时间/有效性）。默认 True。
    auto_check_steam: bool = True
    # 工坊页面自动补全（依赖 + 中文描述）。默认 True；False 时只用本地缓存/API 快照
    workshop_html_fill: bool = True
    # ── P0 新增：自定义分组 ──
    # key=分组名，value=[mod_id (lower), ...]；用户自建的分组
    groups: dict[str, list[str]] = field(default_factory=dict)
    # 分组显示顺序（含内置分组和自定义分组）
    group_order: list[str] = field(default_factory=list)
    # ── P0 新增：个人备注/别名/颜色 ──
    # key=mod_id (lower), value={alias: str, note: str}
    mod_meta: dict[str, dict] = field(default_factory=dict)
    # key=mod_id (lower), value="#hex" 颜色
    mod_colors: dict[str, str] = field(default_factory=dict)
    # ── P1 预留：Pinned Mods ──
    # key=mod_id (lower), value={version: str, enabled: bool}
    pinned: dict[str, dict] = field(default_factory=dict)
    # ── 最近活动时间（秒级 Unix 时间戳） ──
    # 用于停用列表排序（刚停用/刚启用/刚更新的排最上）
    # key=mod_id (lower), value=int timestamp
    last_activity: dict[str, int] = field(default_factory=dict)
    # ── mod 备份 ──
    # mod 更新时是否自动创建备份（对比内容指纹，有基线且变化才触发）
    auto_backup: bool = True
    # 每个 mod 最多保留的备份版本数（超出删最旧）
    keep_versions: int = 3
    # 自定义备份目录（为空则用 <data>/backups）
    backup_dir: Optional[str] = None
    # ── 游戏版本持久化（多分支） ──
    # key=beta_key（""=public, "public-beta"=测试分支）, value=版本号 "0.107.1"
    # 启动时自动写当前分支的版本号；另一个分支保持为空（未知）直到用户切过去启动游戏
    branch_versions: dict[str, str] = field(default_factory=dict)
    version: int = 2

    @classmethod
    def default(cls) -> "ManagerState":
        return cls(
            enabled_order=[],
            disabled_order=[],
            manager_tags=[*BUILTIN_TAGS, *OFFICIAL_CHARACTER_TAGS],
        )
