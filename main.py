from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from spire_manager.logger import setup_logger, get_logger  # noqa: E402
from spire_manager.store import ModManager  # noqa: E402


def default_profile() -> Path:
    # P1: 现在返回 data/ 目录，ModManager 会自动管理 profiles/ 子目录
    #     （legacy manager.state.json 会在首次加载时自动迁移到 profiles/default.json）
    base = PROJECT_ROOT / "data"
    try:
        base.mkdir(parents=True, exist_ok=True)
        test = base / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return base
    except Exception:
        appdata = Path.home() / "AppData" / "Roaming" / "spire_manager"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata / "default_profile.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spire-mod-manager",
        description="最简 STS2 mod 管理器（扫描 / 启用停用 / 拖拽排序）",
    )
    p.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="指定配置文件（默认 data/default_profile.json）",
    )
    p.add_argument(
        "--game-root",
        type=Path,
        default=None,
        help="手动指定 STS2 根目录（<根目录>/SlayTheSpire2.exe 存在）",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    sub.add_parser("gui", help="启动图形界面（默认）")
    p_scan = sub.add_parser("scan", help="只扫描并打印 mod 列表（无头，调试用）")
    p_scan.add_argument("--json", action="store_true", help="输出 JSON")
    return p


def cmd_scan(manager: ModManager, game_root: Path | None, use_json: bool) -> int:
    log = get_logger("main.scan")
    log.info("开始 scan 命令: use_json=%s", use_json)
    scan = manager.refresh(manual_game_root=game_root)
    if use_json:
        payload = {
            "game_root": str(manager.game.game_root) if manager.game else None,
            "mods_dir": str(manager.game.mods_dir) if manager.game else None,
            "workshop_dir": str(manager.game.workshop_dir) if manager.game and manager.game.workshop_dir else None,
            "enabled_order": manager.state().enabled_order,
            "disabled_order": manager.state().disabled_order,
            "mods": [
                {
                    "id": info.mod_id,
                    "name": info.display_name,
                    "author": info.manifest.author,
                    "version": info.manifest.version,
                    "source": info.source,
                    "workshop_id": info.workshop_id,
                    "folder": info.folder_name,
                    "path": str(info.install_dir),
                    "has_pck": info.manifest.has_pck,
                    "has_dll": info.manifest.has_dll,
                }
                for info in sorted(scan.mods.values(), key=lambda m: m.display_name.lower())
            ],
            "errors": scan.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if manager.game is None:
            print("[x] 未检测到 STS2 安装目录。使用 --game-root 手动指定。")
        else:
            print(f"[√] 游戏目录: {manager.game.game_root}")
            print(f"[√] mods 目录: {manager.game.mods_dir}")
            if manager.game.workshop_dir:
                print(f"[√] 工坊目录: {manager.game.workshop_dir}")
            print(
                f"[√] 共 {len(scan.mods)} 个 mod "
                f"(启用侧 {len(manager.ordered_enabled())}, 停用侧 {len(manager.ordered_disabled())})"
            )
            for info in manager.ordered_enabled():
                src = info.source
                print(f"  [启用][{src}] {info.mod_id}  →  {info.display_name}")
            for info in manager.ordered_disabled():
                print(f"  [停用]        {info.mod_id}  →  {info.display_name}")
        if scan.errors:
            print("\n警告/错误:")
            for e in scan.errors:
                print(f"  - {e}")
    log.info("scan 命令结束")
    return 0


def _set_dpi_awareness() -> None:
    """设置进程 DPI 感知，避免 pywebview 在高 DPI 缩放下坐标/视口尺寸错位。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _set_dpi_awareness()
    args = build_parser().parse_args(argv)
    profile = Path(args.profile) if args.profile else default_profile()

    # 启动第一件事：初始化 logger，日志目录和 profile 的 data 目录放在一起
    log_dir = setup_logger()
    log = get_logger("main")
    log.info("=" * 60)
    log.info("程序启动: argv=%s, cmd=%s, profile=%s, log_dir=%s",
             argv if argv is not None else sys.argv[1:], args.cmd, profile, log_dir)
    log.info("工作目录: %s, PROJECT_ROOT=%s", Path.cwd(), PROJECT_ROOT)

    manager = ModManager(profile)

    if args.cmd in (None, "gui"):
        try:
            from spire_manager.webview_app import run_webview
        except ImportError as e:
            log.exception("GUI 依赖导入失败")
            print(
                "[x] 启动 GUI 需要 pywebview。先安装依赖：\n"
                f"        pip install -r \"{PROJECT_ROOT / 'requirements.txt'}\"\n"
                f"  导入错误: {e}",
                file=sys.stderr,
            )
            return 2
        # ⚠️ 先打开窗口，再后台 refresh——UI 立刻可见，扫描进度在界面底部显示
        def _bg_refresh() -> None:
            try:
                log.info("[main] 后台 refresh 开始")
                manager.refresh(manual_game_root=args.game_root)
                log.info("[main] 后台 refresh 完成")
            except Exception as e:
                log.exception("[main] 后台 refresh 失败")

        threading.Thread(target=_bg_refresh, daemon=True).start()
        log.info("进入 pywebview 窗口（扫描在后台进行）")
        run_webview(manager)
        log.info("pywebview 窗口结束")
        # 窗口关闭后立即终止线程池（否则 ThreadPoolExecutor 里还在跑的 auto_backup
        # 压缩大文件（如 STS2mahonomajiyo 1GB）会让进程挂住几分钟才退出）
        try:
            from spire_manager import api as _api_mod
            _api_mod._IO_POOL.shutdown(wait=False, cancel_futures=True)
            log.info("已终止后台 IO 线程池")
        except Exception:
            pass
        return 0

    if args.cmd == "scan":
        return cmd_scan(manager, game_root=args.game_root, use_json=bool(args.json))

    log.error("未知命令: %s", args.cmd)
    print(f"未知命令: {args.cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
