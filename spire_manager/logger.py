from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional


_LOG_CONFIGURED = False


def _default_log_dir() -> Path:
    # 优先放在项目下 data/logs；不行就 %APPDATA%\spiremanager\logs
    project_root = Path(__file__).resolve().parent.parent
    base = project_root / "data"
    try:
        base.mkdir(parents=True, exist_ok=True)
        test = base / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        log_dir = base / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except Exception:
        appdata = Path.home() / "AppData" / "Roaming" / "spiremanager" / "logs"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata


def setup_logger(
    log_dir: Optional[Path] = None,
    level: int = logging.DEBUG,
    console: bool = True,
) -> Path:
    """配置全局 logger（只生效一次）。返回日志文件所在目录。
    默认 console=True：日志同时落到 data/logs/app.log 文件 + 终端(stderr)彩色输出；
    不想终端输出就显式传 console=False。"""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return _default_log_dir() if log_dir is None else Path(log_dir)

    log_dir = Path(log_dir) if log_dir else _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    root = logging.getLogger("spiremanager")
    root.setLevel(level)
    root.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件：按大小轮转，保留 5 个备份 * 4MB = 最多 20MB
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=4 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:
        # 日志文件写不了也不能让程序崩，至少打到 stderr
        sys.stderr.write(f"[logger] 无法创建日志文件 {log_file}: {e}\n")

    # 控制台（简单 StreamHandler）
    if console:
        try:
            import colorlog  # 可选依赖，装了就彩色
            ch = logging.StreamHandler(sys.stderr)
            ch.setLevel(logging.INFO)
            colored = colorlog.ColoredFormatter(
                "%(log_color)s%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
                log_colors={
                    "DEBUG": "cyan",
                    "INFO": "green",
                    "WARNING": "yellow",
                    "ERROR": "red",
                    "CRITICAL": "red,bg_white",
                },
            )
            ch.setFormatter(colored)
            root.addHandler(ch)
        except Exception:
            ch = logging.StreamHandler(sys.stderr)
            ch.setLevel(logging.INFO)
            ch.setFormatter(fmt)
            root.addHandler(ch)

    _LOG_CONFIGURED = True
    return log_dir


def get_logger(name: str) -> logging.Logger:
    """获取命名 logger（形如 spiremanager.store / spiremanager.ui）。"""
    if not _LOG_CONFIGURED:
        setup_logger()
    return logging.getLogger(f"spiremanager.{name}")
