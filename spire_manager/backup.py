"""工坊/本地 mod 的本地备份（压缩留档 + 版本回退）。

设计要点（已与用户确认）：
  - 智能压缩：普通文件用 ZIP_LZMA 压缩；单个文件 >200MB 且通常是已内压缩的 .pck 时
    改用 ZIP_STORED 原样存（Godot 大 pck 实测只能压 1~2%，不值得白等 CPU）。
  - 目录结构：<data>/backups/index.json + <data>/backups/<mod_id>/<ts>__<ver>.zip
  - index.json 原子写 + .bak 备份，防损坏。
  - 更新检测：对 mod 目录算「内容指纹」（全部文件的 rel:size:mtime + 版本号），
    与 index 里该 mod 最新一条备份的指纹比对，不同即认为 mod 被 Steam 更新过。
  - 保留版本数：每个 mod 只保留最新 N 份，超出删最旧。
  - 还原前自动把目标目录改名成 .bak-<ts>；解包到临时目录再原子替换，失败可回滚。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .logger import get_logger

_log = get_logger("backup")

# 单文件超过此大小且扩展名属于「通常已内压缩」时，改用 ZIP_STORED（不压）
_BIG_FILE_BYTES = 200 * 1024 * 1024

# 已内压缩、压缩收益极低的扩展名（实测 Godot .pck 只能压 1~2%）
_ALREADY_COMPRESSED_EXTS = {".pck", ".zip", ".rar", ".7z", ".ogg", ".mp3", ".jpg", ".jpeg", ".png", ".webp"}

# 压缩级别（ZIP_LZMA preset）
_LZMA_PRESET = 6

# 索引写入锁
_LOCK = threading.RLock()


def _safe_name(name: str) -> str:
    """把 mod_id 转成安全的目录名。"""
    out = []
    for ch in str(name):
        out.append(ch if (ch.isalnum() or ch in "-_.") else "_")
    s = "".join(out).strip("._") or "mod"
    return s[:80]


def _backup_key(mod_id: str, workshop_id=None) -> str:
    """备份身份 key（bucket_key 风格，把 `::` 换成 Windows 安全的 `__`）。

    2026-11-06: 新增，让不同 workshop 撞 mod_id 的副本各有独立备份目录。
    有数字 workshop_id → `f"{mid_lower}__{wid}"`；否则 `mid_lower`（local/disabled 副本）。
    """
    low = str(mod_id).lower()
    if workshop_id is not None:
        s = str(workshop_id)
        if s.isdigit():
            return f"{low}__{s}"
    return low


def _now() -> tuple[int, str]:
    ts = int(time.time())
    return ts, datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")


class BackupManager:
    """管理某个 data 目录下的 mod 备份。线程安全（单进程内）。"""

    def __init__(self, backup_root: Path, data_root: Optional[Path] = None):
        """backup_root=备份存放目录（= data/backups 或用户自定义路径）。
        data_root=SMM data 根目录（= data/），用来找 cache/。不传则回退 backup_root.parent。"""
        self.root = Path(backup_root)
        self.data_root = Path(data_root) if data_root else self.root.parent
        self.index_path = self.root / "index.json"
        self._cache: Optional[list[dict]] = None
        self._cache_mtime: float = -1.0

    # ------------------------------------------------------------------ 索引
    def _load_index(self) -> list[dict]:
        with _LOCK:
            try:
                mtime = self.index_path.stat().st_mtime if self.index_path.is_file() else -1.0
            except OSError:
                mtime = -1.0
            if self._cache is not None and mtime == self._cache_mtime:
                return self._cache
            data: list[dict] = []
            if self.index_path.is_file():
                try:
                    raw = json.loads(self.index_path.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        data = [r for r in raw if isinstance(r, dict)]
                except Exception as e:
                    _log.warning("读取备份索引失败: %s", e)
            self._cache = data
            self._cache_mtime = mtime
            return data

    def _save_index(self, records: list[dict]) -> None:
        with _LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                if self.index_path.is_file():
                    shutil.copy2(str(self.index_path), str(self.index_path) + ".bak")
            except OSError:
                pass
            tmp = self.index_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(self.index_path))
            self._cache = records
            try:
                self._cache_mtime = self.index_path.stat().st_mtime
            except OSError:
                self._cache_mtime = -1.0

    # ------------------------------------------------------------------ 查询
    def list(self, mod_id: Optional[str] = None,
             workshop_id: Optional[Any] = None) -> list[dict]:
        """列出备份记录。

        2026-11-06: 新增可选 workshop_id 参数，精确匹配 backup_key。
        老备份没有 backup_key 字段 → fallback 到 mod_id_lower 匹配（兼容升级）。
        """
        recs = list(self._load_index())
        if mod_id:
            # 目标 backup_key（有 workshop_id 用派生桶，否则用基础桶）
            target_bk = _backup_key(mod_id, workshop_id)
            # 过滤：新格式 backup_key 精确匹配 OR 老格式 mod_id_lower fallback
            # 但只在没有 backup_key 的老记录里 fallback——新记录必须严格匹配 backup_key
            def _match(r: dict) -> bool:
                r_bk = str(r.get("backup_key", ""))
                if r_bk:
                    return r_bk == target_bk
                # 老记录（无 backup_key）：按 mod_id_lower 匹配
                return str(r.get("mod_id", "")).lower() == str(mod_id).lower()
            recs = [r for r in recs if _match(r)]
        recs.sort(key=lambda r: int(r.get("ts", 0)), reverse=True)
        return [self._enrich(r) for r in recs]

    def _enrich(self, r: dict) -> dict:
        out = dict(r)
        try:
            out["exists"] = (self.root / str(r.get("archive", ""))).is_file()
        except Exception:
            out["exists"] = False
        return out

    def get(self, backup_id: str) -> Optional[dict]:
        for r in self._load_index():
            if str(r.get("id")) == str(backup_id):
                return r
        return None

    def latest_for(self, mod_id: str, workshop_id=None) -> Optional[dict]:
        recs = self.list(mod_id, workshop_id)
        return recs[0] if recs else None

    def stats(self) -> dict:
        recs = self._load_index()
        total_archive = sum(int(r.get("archive_bytes", 0)) for r in recs)
        total_original = sum(int(r.get("original_bytes", 0)) for r in recs)
        mods = {str(r.get("mod_id", "")).lower() for r in recs}
        return {
            "count": len(recs),
            "mod_count": len(mods),
            "archive_bytes": total_archive,
            "original_bytes": total_original,
            "saved_bytes": max(0, total_original - total_archive),
            "root": str(self.root),
        }

    # ------------------------------------------------------------------ 指纹
    @staticmethod
    def fingerprint(mod_dir: Path, version: Optional[str] = None) -> str:
        """对目录算内容指纹：全部文件 rel|size|mtime 排序后哈希，附带版本号。"""

        h = hashlib.sha256()
        items: list[str] = []
        try:
            for p in sorted(mod_dir.rglob("*")):
                if not p.is_file():
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = str(p.relative_to(mod_dir)).replace("\\", "/")
                items.append(f"{rel}|{st.st_size}|{int(st.st_mtime)}")
        except Exception as e:
            _log.debug("计算指纹失败 %s: %s", mod_dir, e)
        h.update("\n".join(items).encode("utf-8"))
        h.update(f"\x00ver={version or ''}".encode("utf-8"))
        return h.hexdigest()[:32]

    def needs_update(self, info) -> bool:
        """该 mod 自上次备份以来是否被更新过（有基线且指纹不同）。

        没有历史备份时返回 False —— 首次备份由用户手动触发（用户在方案里
        明确选择「仅 mod 更新时自动」，不自动备份全新 mod）。
        """
        if info is None or info.install_dir is None:
            return False
        latest = self.latest_for(info.mod_id, info.workshop_id)
        if latest is None:
            return False
        try:
            cur = self.fingerprint(Path(info.install_dir), info.manifest.version)
        except Exception:
            return False
        return cur != latest.get("fingerprint")

    # ------------------------------------------------------------------ 创建
    def _collect_cache_files(self, info) -> list[tuple[Path, str]]:
        """收集与该 mod 关联的缓存文件，返回 [(绝对路径, zip内相对路径), ...]。

        缓存都放在 zip 的 `__cache__/` 子目录下，与 mod 本体隔离，方便还原时选择性恢复。
        """
        cache_root = self.data_root / "cache"
        result: list[tuple[Path, str]] = []
        wid = getattr(info, "workshop_id", None)
        if wid:
            wid = str(wid)
            # 1. workshop/{wid}/ 整个目录（meta.zh.json, deps.json, preview.*）
            ws_cache = cache_root / "workshop" / wid
            if ws_cache.is_dir():
                for p in ws_cache.rglob("*"):
                    if p.is_file():
                        result.append((p, f"__cache__/workshop/{wid}/{p.name}"))
            # 2. steam_workshop/{wid}.json
            steam_cache = cache_root / "steam_workshop" / f"{wid}.json"
            if steam_cache.is_file():
                result.append((steam_cache, f"__cache__/steam_workshop/{wid}.json"))
        # 3. dll / pck 扫描缓存（按 mod 目录内的 DLL/PCK 文件哈希匹配）
        #    跳过：扫描缓存体积小但 key 不稳定（依赖文件哈希），还原时收益低
        return result

    def create(self, info, kind: str = "manual", note: str = "") -> dict:
        """为 mod 创建一份压缩备份，返回记录 dict。"""
        if info is None or info.install_dir is None:
            raise ValueError("该 mod 没有可备份的本地目录（占位 mod）")
        src = Path(info.install_dir)
        if not src.is_dir():
            raise ValueError(f"目录不存在: {src}")

        files = [p for p in src.rglob("*") if p.is_file()]
        if not files:
            raise ValueError("目录为空，无可备份内容")

        # 收集关联缓存（workshop mod 才有）
        cache_files = self._collect_cache_files(info)

        mod_id = str(info.mod_id)
        version = str(info.manifest.version or "")
        ts, stamp = _now()
        backup_id = f"{mod_id.lower()}_{ts}"

        # 2026-11-06: 目录名用 backup_key 让不同 workshop 撞 mod_id 的副本独立存
        bk = _backup_key(mod_id, info.workshop_id)
        folder = self.root / _safe_name(bk)
        folder.mkdir(parents=True, exist_ok=True)
        safe_ver = _safe_name(version) if version else "nover"
        archive = folder / f"{stamp}__{safe_ver}.zip"

        original_bytes = 0
        for p in files:
            try:
                original_bytes += p.stat().st_size
            except OSError:
                pass
        cache_bytes = 0
        for p, _ in cache_files:
            try:
                cache_bytes += p.stat().st_size
            except OSError:
                pass

        _log.info("[backup] 开始备份 %s (%d 个文件, %.1f MB, 缓存 %d 个/%.1f MB) -> %s",
                  mod_id, len(files), original_bytes / 1e6,
                  len(cache_files), cache_bytes / 1e6, archive.name)
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_LZMA,
                                 compresslevel=_LZMA_PRESET, allowZip64=True) as zf:
                # mod 本体
                for p in files:
                    rel = str(p.relative_to(src)).replace("\\", "/")
                    try:
                        size = p.stat().st_size
                    except OSError:
                        size = 0
                    use_stored = size > _BIG_FILE_BYTES and p.suffix.lower() in _ALREADY_COMPRESSED_EXTS
                    if use_stored:
                        zf.write(p, rel, compress_type=zipfile.ZIP_STORED)
                    else:
                        zf.write(p, rel, compress_type=zipfile.ZIP_LZMA)
                # 关联缓存（放在 __cache__/ 子目录）
                for p, arcname in cache_files:
                    try:
                        size = p.stat().st_size
                    except OSError:
                        size = 0
                    use_stored = size > _BIG_FILE_BYTES and p.suffix.lower() in _ALREADY_COMPRESSED_EXTS
                    if use_stored:
                        zf.write(p, arcname, compress_type=zipfile.ZIP_STORED)
                    else:
                        zf.write(p, arcname, compress_type=zipfile.ZIP_LZMA)
        except Exception as e:
            _log.error("[backup] 备份失败 %s: %s", mod_id, e)
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        archive_bytes = archive.stat().st_size
        rel_archive = str(archive.relative_to(self.root)).replace("\\", "/")
        record = {
            "id": backup_id,
            "mod_id": mod_id,
            "mod_id_lower": mod_id.lower(),
            "backup_key": bk,  # 2026-11-06: 备份身份 key，让不同 workshop 撞 mod_id 独立
            "name": str(info.display_name or mod_id),
            "version": version,
            "author": str(info.manifest.author or ""),
            "source": str(getattr(info, "source", "") or ""),
            "source_label": str(getattr(info, "source_label", "") or ""),
            "external_source": str(getattr(info, "external_source", "") or ""),
            "workshop_id": info.workshop_id,
            "created_at": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            "ts": ts,
            "original_bytes": original_bytes,
            "archive_bytes": archive_bytes,
            "file_count": len(files),
            "cache_file_count": len(cache_files),
            "cache_bytes": cache_bytes,
            "has_cache": len(cache_files) > 0,
            "fingerprint": self.fingerprint(src, version),
            "origin_dir": str(src),
            "archive": rel_archive,
            "kind": kind,
            "note": note,
        }

        with _LOCK:
            recs = self._load_index()
            recs = [r for r in recs if str(r.get("id")) != backup_id]
            recs.append(record)
            recs = self._rotate(recs, bk)
            self._save_index(recs)

        _log.info("[backup] 完成 %s: %.1f MB -> %.1f MB",
                  mod_id, original_bytes / 1e6, archive_bytes / 1e6)
        return self._enrich(record)

    def _rotate(self, recs: list[dict], backup_key: str) -> list[dict]:
        """每个 backup_key 只保留最新 keep_versions 份，删掉超出的（含归档文件）。

        2026-11-06: 参数从 mod_id 改成 backup_key，让不同 workshop 撞 mod_id 的备份独立分组。
        老备份没有 backup_key 字段 → 用 mod_id_lower fallback 匹配。
        """
        keep = self._keep_versions

        def _match_bk(r: dict) -> bool:
            r_bk = str(r.get("backup_key", ""))
            if r_bk:
                return r_bk == backup_key
            return str(r.get("mod_id", "")).lower() == backup_key

        mine = [r for r in recs if _match_bk(r)]
        others = [r for r in recs if not _match_bk(r)]
        if len(mine) <= keep:
            return recs
        mine.sort(key=lambda r: int(r.get("ts", 0)), reverse=True)
        for r in mine[keep:]:
            self._remove_archive(r)
        return others + mine[:keep]

    @property
    def _keep_versions(self) -> int:
        v = getattr(self, "keep_versions", 3)
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return 3

    def _remove_archive(self, record: dict) -> None:
        try:
            p = self.root / str(record.get("archive", ""))
            if p.is_file():
                p.unlink()
                # 目录空了就删掉
                if p.parent != self.root and not any(p.parent.iterdir()):
                    p.parent.rmdir()
        except Exception as e:
            _log.debug("删除旧备份失败: %s", e)

    # ------------------------------------------------------------------ 删除
    def delete(self, backup_id: str) -> bool:
        with _LOCK:
            recs = self._load_index()
            target = next((r for r in recs if str(r.get("id")) == str(backup_id)), None)
            if target is None:
                return False
            self._remove_archive(target)
            self._save_index([r for r in recs if str(r.get("id")) != str(backup_id)])
        return True

    def delete_all_for(self, mod_id: str, workshop_id=None) -> int:
        n = 0
        for r in self.list(mod_id, workshop_id):
            if self.delete(str(r.get("id"))):
                n += 1
        return n

    # ------------------------------------------------------------------ 校验
    def verify(self, backup_id: str) -> dict:
        r = self.get(backup_id)
        if r is None:
            return {"ok": False, "error": "备份不存在"}
        p = self.root / str(r.get("archive", ""))
        if not p.is_file():
            return {"ok": False, "error": "归档文件缺失"}
        try:
            with zipfile.ZipFile(p) as zf:
                bad = zf.testzip()
                n = len(zf.namelist())
            if bad:
                return {"ok": False, "error": f"文件损坏: {bad}"}
            return {"ok": True, "files": n, "expected": int(r.get("file_count", 0))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ 还原
    def _restore_cache(self, tmp: Path, force: bool = False) -> tuple[int, int]:
        """把 tmp/__cache__/ 里的文件恢复到 data/cache/。返回 (文件数, 总字节数)。

        默认只补缺失文件（force=False）。force=True 时覆盖已有。
        """
        cache_dir = tmp / "__cache__"
        if not cache_dir.is_dir():
            return 0, 0
        real_cache = self.data_root / "cache"
        count = 0
        total = 0
        skipped = 0
        for p in cache_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(cache_dir)
            dest = real_cache / rel
            if dest.exists() and not force:
                skipped += 1
                continue   # 默认只补缺失，不覆盖已有（定论 #6）
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(p), str(dest))
                count += 1
                total += p.stat().st_size
            except OSError as e:
                _log.warning("[backup] 恢复缓存失败 %s: %s", rel, e)
        if count or skipped:
            _log.info("[backup] 缓存恢复: 写入 %d 个 (%.1f MB), 跳过已有 %d 个 (force=%s)",
                      count, total / 1e6, skipped, force)
        return count, total

    def restore(self, backup_id: str, target_dir: Path,
                restore_cache: bool = True, force_cache: bool = False) -> dict:
        """把备份解包到 target_dir。还原前把已存在的 target 改名留档，失败可回滚。

        restore_cache=True 时，如果 zip 里带了 __cache__/，会把缓存写回 data/cache/。
        force_cache=True 时覆盖已有缓存文件；默认只补缺失（定论 #6）。
        """
        r = self.get(backup_id)
        if r is None:
            return {"ok": False, "error": "备份不存在"}
        archive = self.root / str(r.get("archive", ""))
        if not archive.is_file():
            return {"ok": False, "error": "归档文件缺失"}

        target = Path(target_dir)
        ts, _ = _now()
        target.parent.mkdir(parents=True, exist_ok=True)

        tmp = target.parent / f"{target.name}.restore-{ts}"
        moved_old: Optional[Path] = None
        restored_cache_files = 0
        restored_cache_bytes = 0
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as zf:
                tmp_root = tmp.resolve()
                cache_members: list[str] = []
                for member in zf.namelist():
                    # 防 zip slip：解包目标解析后必须仍在临时目录内
                    dest = (tmp / member).resolve()
                    if not dest.is_relative_to(tmp_root):
                        raise ValueError(f"非法归档路径: {member}")
                    if member.startswith("__cache__/"):
                        cache_members.append(member)
                zf.extractall(tmp)

            # 还原缓存（可选）
            if restore_cache and cache_members:
                restored_cache_files, restored_cache_bytes = self._restore_cache(tmp, force=force_cache)

            if target.exists():
                moved_old = target.parent / f"{target.name}.bak-{ts}"
                i = 1
                while moved_old.exists():
                    moved_old = target.parent / f"{target.name}.bak-{ts}-{i}"
                    i += 1
                target.rename(moved_old)
            tmp.rename(target)
        except Exception as e:
            _log.error("[backup] 还原失败 %s -> %s: %s", backup_id, target, e)
            # 回滚：把旧的移回来
            try:
                if moved_old is not None and moved_old.exists() and not target.exists():
                    moved_old.rename(target)
            except Exception:
                pass
            try:
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass
            return {"ok": False, "error": str(e)}

        _log.info("[backup] 已还原 %s -> %s (缓存 %d 个/%.1f MB)",
                  backup_id, target, restored_cache_files, restored_cache_bytes / 1e6)
        return {
            "ok": True,
            "target": str(target),
            "file_count": int(r.get("file_count", 0)),
            "cache_file_count": restored_cache_files,
            "cache_bytes": restored_cache_bytes,
            "backup_of_old": str(moved_old) if moved_old else None,
        }

    # ------------------------------------------------------------------ 辅助
    def folder_of(self, backup_id: str) -> Optional[Path]:
        r = self.get(backup_id)
        if r is None:
            return None
        return (self.root / str(r.get("archive", ""))).parent
