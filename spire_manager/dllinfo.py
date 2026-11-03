"""读取 .NET(C#) mod 程序集元数据（不执行），用于辅助 mod 自动分类。

STS2 是 Godot(.NET) 游戏，mod 的 .dll 是 C# 程序集。用 dnfile 只读元数据
（命名空间 / 类型名 / 引用程序集），不做反编译、不执行任何代码，安全。

产出的信号用于：
  - 自动标签（识别 Harmony 补丁 -> 玩法、库类 -> 依赖）
  - 角色识别（识别 Character/CharSelect 相关类型）
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
from pathlib import Path
from typing import Optional

from .logger import get_logger

_log = get_logger("dllinfo")

try:
    import dnfile  # type: ignore
    _HAS_DNFILE = True
except Exception:
    dnfile = None
    _HAS_DNFILE = False

# 明显是引擎/运行时自带的原生 DLL，跳过，避免误当作 mod 程序集解析
_NATIVE_DLL_PREFIXES = (
    "fmod", "fmodstudio", "libgodotfmod", "libsentry", "libspine",
    "crashpad", "concrt", "msvcp", "vcruntime", "ucrtbase",
)

# DLL 分析磁盘缓存目录（与工坊预览缓存并列，位于 data/cache/dll/）
_CACHE_ROOT = Path(__file__).resolve().parent.parent / "data" / "cache" / "dll"

# 解析结果内存缓存：key=(path, mtime)
_cache: dict[tuple[str, int], Optional[dict]] = {}

# 解析锁：analyze_dlls 后台线程与 get_state 懒加载（_dll_signals）可能并发触发
# inspect_dll，用锁保证同一个 DLL 只解析一次。
_cache_lock = threading.Lock()

# DLL 分析进度（供前端轮询展示进度条）
_progress: dict = {"current": None, "done": 0, "total": 0}
_progress_lock = threading.Lock()


def reset_dll_progress(total: int) -> None:
    with _progress_lock:
        _progress["current"] = None
        _progress["done"] = 0
        _progress["total"] = max(0, int(total))


def get_dll_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _tick_progress(name: Optional[str], done: bool) -> None:
    if name is None:
        return
    with _progress_lock:
        if done:
            _progress["done"] += 1
            _progress["current"] = None
        else:
            _progress["current"] = name


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
        _log.debug("DLL 分析缓存写盘失败: %s", e)


def clear_dll_cache() -> None:
    """清空 DLL 分析缓存（内存 + 磁盘）。"""
    global _cache
    _cache = {}
    try:

        if _CACHE_ROOT.is_dir():
            shutil.rmtree(_CACHE_ROOT)
            _log.info("[dllinfo] 已清空 DLL 分析缓存: %s", _CACHE_ROOT)
    except Exception as e:
        _log.warning("[dllinfo] 清空 DLL 分析缓存失败: %s", e)


def find_mod_dll(mod_dir: Path, prefer_names: Optional[list[str]] = None) -> Optional[Path]:
    """在 mod 目录里挑一个最像 mod 本体的 .NET 程序集 dll。

    prefer_names（如 mod id / 目录名）优先匹配，其次跳过原生 DLL，最后取任意 dll。
    """
    if not mod_dir or not mod_dir.is_dir():
        return None
    dlls = [p for p in mod_dir.glob("*.dll") if p.is_file()]
    if not dlls:
        return None

    prefs = [n.lower() for n in (prefer_names or []) if n]

    def rank(p: Path) -> int:
        stem = p.stem.lower()
        if prefs and any(stem == n or stem.startswith(n) for n in prefs):
            return 0
        if stem.startswith(_NATIVE_DLL_PREFIXES):
            return 2
        return 1

    dlls.sort(key=rank)
    return dlls[0]


def _parse_assembly(path: Path) -> Optional[dict]:
    """执行 dnfile 解析（调用方需在 _cache_lock 内调用）。"""
    try:
        pe = dnfile.dnPE(str(path))
        if pe.net is None:
            return None
        type_names = [str(r.TypeName) for r in pe.net.mdtables.TypeDef.rows]
        namespaces = [str(r.TypeNamespace) for r in pe.net.mdtables.TypeDef.rows]
        refs = [str(r.Name) for r in pe.net.mdtables.AssemblyRef.rows]
        tn_lower = [t.lower() for t in type_names if t and t != "<Module>"]
        ns_lower = [n.lower() for n in namespaces if n]
        refs_lower = [r.lower() for r in refs if r]
        text = " ".join(tn_lower + ns_lower + refs_lower)
        patch_count = sum(1 for t in tn_lower if "patch" in t)
        return {
            "ok": True,
            "type_names": tn_lower,
            "namespaces": ns_lower,
            "text": text,
            "refs": refs_lower,
            # 新增可游玩角色：通常会有 CharacterSelect / AllCharacters 相关类型
            "has_character": (
                "charselect" in text
                or "characterselect" in text
                or "allcharacters" in text
            ),
            # Harmony Patch 类数量（>0 通常表示在改游戏逻辑）
            "patch_count": patch_count,
        }
    except Exception as e:
        _log.debug("inspect_dll 解析失败 %s: %s", path, e)
        return None


def inspect_dll(path: Path, progress_name: Optional[str] = None) -> Optional[dict]:
    """解析 .NET 程序集，返回分类信号 dict；解析失败返回 None。

    progress_name 不为 None 时，会在解析期间更新全局进度（供前端展示进度条）。
    使用 _cache_lock 保证并发调用（analyze_dlls 后台线程 / get_state 懒加载）不重复解析。
    """
    if not _HAS_DNFILE or not path or not path.is_file():
        _tick_progress(progress_name, done=True)
        return None
    try:
        mtime = int(path.stat().st_mtime)
    except Exception:
        _tick_progress(progress_name, done=True)
        return None

    key = (str(path), mtime)

    # 快路径：内存缓存命中（dict 读取在 GIL 下原子，无需加锁）
    if key in _cache:
        _tick_progress(progress_name, done=True)
        return _cache[key]

    # 磁盘缓存命中
    cached = _load_disk_cache(path, mtime)
    if cached is not None:
        _cache[key] = cached
        _tick_progress(progress_name, done=True)
        return cached

    _tick_progress(progress_name, done=False)
    with _cache_lock:
        # 二次检查：可能另一个线程刚解析完并写入了缓存
        if key in _cache:
            _tick_progress(progress_name, done=True)
            return _cache[key]
        result = _parse_assembly(path)
        _cache[key] = result
        if result is not None:
            _save_disk_cache(path, mtime, result)
        _tick_progress(progress_name, done=True)
        return result


def cleanup_dll_cache(valid_dlls: list) -> int:
    """删除磁盘缓存里不属于 valid_dlls（(path, mtime) 列表）的孤儿文件，返回删除数量。

    在 analyze_dlls 扫描到当前所有 DLL 后调用，清掉已删除/已更新 mod 留下的旧缓存。
    """
    valid_names = set()
    for item in valid_dlls:
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
        _log.warning("[dllinfo] 清理 DLL 缓存失败: %s", e)
    return removed
