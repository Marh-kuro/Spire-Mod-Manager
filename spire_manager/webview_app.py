from __future__ import annotations

import json
from pathlib import Path

import webview

from .api import Api
from .store import ModManager

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WINDOW_STATE_FILE = DATA_DIR / "window_state.json"

DEFAULT_WIDTH = 1680
DEFAULT_HEIGHT = 900
MIN_WIDTH = 1200
MIN_HEIGHT = 700


def _load_window_state() -> dict:
    try:
        if WINDOW_STATE_FILE.is_file():
            return json.loads(WINDOW_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_window_state(state: dict) -> None:
    try:
        WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def run_webview(manager: ModManager) -> None:
    """pywebview 桌面窗口入口：前端 Vue3 + 后端 ModManager。

    窗口尺寸/最大化状态持久化到 data/window_state.json，重启后恢复。
    """
    api = Api(manager)
    url = (FRONTEND_DIR / "index.html").as_uri()

    # WebView2 用户数据目录（DATA_DIR/webview_cache）。
    # RimCrow 式持久化：private_mode=False + storage_path 让 WebView2 的 HTTP 磁盘缓存
    # （CDN 预览图、静态资源）跨重启保留，第二次加载直接命中缓存秒出。
    # 前端资源的新鲜度由 index.html 里的版本号查询串（app.js?v=<版本>）保证——
    # 版本号一变，浏览器就会重新拉取，无需清空整个缓存目录。
    user_data = DATA_DIR / "webview_cache"
    user_data.mkdir(parents=True, exist_ok=True)

    saved = _load_window_state()
    try:
        width = int(saved.get("width", DEFAULT_WIDTH))
        height = int(saved.get("height", DEFAULT_HEIGHT))
    except (TypeError, ValueError):
        width, height = DEFAULT_WIDTH, DEFAULT_HEIGHT
    width = max(width, MIN_WIDTH)
    height = max(height, MIN_HEIGHT)
    maximized = bool(saved.get("maximized", False))

    window = webview.create_window(
        "Spire Mod Manager",
        url,
        js_api=api,
        width=width,
        height=height,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        maximized=maximized,
    )

    # 窗口状态持久化：关闭时落盘宽高/最大化状态
    state = {"width": width, "height": height, "maximized": maximized}

    def on_resized(w, h):
        if not state["maximized"]:
            state["width"] = int(w)
            state["height"] = int(h)

    def on_maximized():
        state["maximized"] = True

    def on_restored():
        state["maximized"] = False

    def on_closed():
        _save_window_state(state)

    window.events.resized += on_resized
    window.events.maximized += on_maximized
    window.events.restored += on_restored
    window.events.closed += on_closed

    # private_mode=False + storage_path → WebView2 持久化 HTTP 缓存（CDN 预览图等）
    # 与前端优先本地 file:// 预览图形成双重保障
    webview.start(
        private_mode=False,
        storage_path=str(user_data),
    )
