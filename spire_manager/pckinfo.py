"""读取 Godot .pck 资源包里的文件路径列表（不提取内容），用于辅助 mod 自动分类。

STS2 是 Godot(.NET) 游戏，mod 的 .pck 是 Godot 资源包（贴图/动画/模型/卡面等）。
这里只从二进制里用正则抽取 `res://` 开头的资源路径，不做正式解包，安全且轻量。

产出的信号用于自动标签：识别纯美术 mod 到底替换的是卡面 / 角色 / 皮肤等。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from pathlib import Path
from typing import Optional

from .logger import get_logger
from .dllinfo import _tick_progress  # 复用「分析 mod」的统一进度（DLL + PCK 共用一个进度条）

_log = get_logger("pckinfo")

# PCK 分析磁盘缓存目录（与 DLL 分析缓存并列，位于 data/cache/pck/）
_CACHE_ROOT = Path(__file__).resolve().parent.parent / "data" / "cache" / "pck"

# 解析结果内存缓存：key=(path, mtime)
_cache: dict[tuple[str, int], Optional[dict]] = {}

# 解析锁：analyze 后台线程与 get_state 懒加载可能并发触发
_cache_lock = threading.Lock()

# res:// 资源路径：匹配到非路径字符为止（Godot 资源路径一般只含 ASCII 字母数字 _ . / -）
_RE_PATH = re.compile(rb"res://[A-Za-z0-9_./\-]+")


def find_mod_pck(mod_dir: Path, prefer_names: Optional[list[str]] = None) -> Optional[Path]:
    """在 mod 目录里挑一个 pck 文件（优先名字匹配 mod id / 目录名）。"""
    if not mod_dir or not mod_dir.is_dir():
        return None
    pcks = [p for p in mod_dir.glob("*.pck") if p.is_file()]
    if not pcks:
        return None

    prefs = [n.lower() for n in (prefer_names or []) if n]

    def rank(p: Path) -> int:
        stem = p.stem.lower()
        if prefs and any(stem == n or stem.startswith(n) for n in prefs):
            return 0
        return 1

    pcks.sort(key=rank)
    return pcks[0]


def _extract_paths(data: bytes) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for m in _RE_PATH.finditer(data):
        try:
            p = m.group(0).decode("ascii", "ignore").strip().lower()
        except Exception:
            continue
        if not p or p in seen:
            continue
        seen.add(p)
        paths.append(p)
    return paths


def _disk_cache_path(path: Path, mtime: int) -> Path:
    h = hashlib.md5(f"{str(path)}:{mtime}".encode("utf-8")).hexdigest()[:16]
    return _CACHE_ROOT / f"{h}.json"


def _load_disk_cache(path: Path, mtime: int) -> Optional[dict]:
    p = _disk_cache_path(path, mtime)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _save_disk_cache(path: Path, mtime: int, result: dict) -> None:
    try:
        _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        _disk_cache_path(path, mtime).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        _log.debug("PCK 分析缓存写盘失败: %s", e)


def clear_pck_cache() -> None:
    """清空 PCK 分析缓存（内存 + 磁盘）。"""
    global _cache
    _cache = {}
    try:

        if _CACHE_ROOT.is_dir():
            shutil.rmtree(_CACHE_ROOT)
            _log.info("[pckinfo] 已清空 PCK 分析缓存: %s", _CACHE_ROOT)
    except Exception as e:
        _log.warning("[pckinfo] 清空 PCK 分析缓存失败: %s", e)


def _parse_pck(path: Path) -> Optional[dict]:
    """读取 pck 并抽取资源路径（调用方需在 _cache_lock 内调用）。"""
    try:
        data = path.read_bytes()
    except Exception as e:
        _log.debug("读取 pck 失败 %s: %s", path, e)
        return None
    paths = _extract_paths(data)
    text = " ".join(paths)
    return {
        "ok": True,
        "paths": paths,
        "text": text,
        "file_count": len(paths),
    }


def inspect_pck(path: Path, progress_name: Optional[str] = None) -> Optional[dict]:
    """解析 .pck 资源路径，返回信号 dict；失败返回 None。

    与 inspect_dll 相同的缓存/并发策略：内存缓存 + 磁盘缓存 + 加锁二次检查。
    """
    if not path or not path.is_file():
        _tick_progress(progress_name, done=True)
        return None
    try:
        mtime = int(path.stat().st_mtime)
    except Exception:
        _tick_progress(progress_name, done=True)
        return None

    key = (str(path), mtime)

    if key in _cache:
        _tick_progress(progress_name, done=True)
        return _cache[key]

    cached = _load_disk_cache(path, mtime)
    if cached is not None:
        _cache[key] = cached
        _tick_progress(progress_name, done=True)
        return cached

    _tick_progress(progress_name, done=False)
    with _cache_lock:
        if key in _cache:
            _tick_progress(progress_name, done=True)
            return _cache[key]
        result = _parse_pck(path)
        _cache[key] = result
        if result is not None:
            _save_disk_cache(path, mtime, result)
        _tick_progress(progress_name, done=True)
        return result


def cleanup_pck_cache(valid_pcks: list) -> int:
    """删除磁盘缓存里不属于 valid_pcks（(path, mtime) 列表）的孤儿文件，返回删除数量。"""
    valid_names = set()
    for item in valid_pcks:
        try:
            p, mtime = item
            valid_names.add(_disk_cache_path(Path(p), int(mtime)).name)
        except Exception:
            continue
    removed = 0
    try:
        if not _CACHE_ROOT.is_dir():
            return 0
        for f in _CACHE_ROOT.glob("*.json"):
            if f.name not in valid_names:
                try:
                    f.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
    except Exception as e:
        _log.warning("[pckinfo] 清理 PCK 缓存失败: %s", e)
    return removed
