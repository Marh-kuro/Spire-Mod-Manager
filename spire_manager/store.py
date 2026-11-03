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
    mod_version_key,  # noqa: F401  # 2026-10 重新启用：_find_by_key 按 install_dir 精确找副本
    version_gt,
    norm_mod_source,  # noqa: F401  # store_core.py 通过 re-export 继承此符号
    # STS2 原生清单（settings.save → mod_settings.mod_list）：真·游戏源启用/停用/排序权威
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

# O1-1 重构：标签常量 / AUTO_TAG_* / TAG_I18N / _kw_in / 辅助常量 全部搬到 store_core.py
# 这里统一 re-export，保证所有 from .store import AUTO_TAG_LIB 等老导入路径不炸
from .store_core import *  # noqa: F403  (wildcard 用于 re-export 标签常量)

_log = get_logger("store")




# O1-3 重构：ModManager 类体已搬到 store_mods.py
from .store_mods import ModManager  # noqa: E402 (import at end)
