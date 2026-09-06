"""workbench 启动命令：起本机服务 + 打开 Chrome app 模式的独立看板窗口。"""
from __future__ import annotations

import argparse
import subprocess
import threading


def _open_board_window(url: str) -> None:
    """优先 Chrome --app 模式（独立窗口、Dock 有图标，Cmd+Tab 即回看板），失败退回默认浏览器。"""
    try:
        subprocess.run(
            ["open", "-na", "Google Chrome", "--args",
             f"--app={url}", "--window-size=1240,880"],
            check=True, capture_output=True, timeout=15,
        )
        return
    except Exception:
        pass
    try:
        subprocess.Popen(["open", url])
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 看板")
    parser.add_argument("command", nargs="?", choices=["open"],
                        help="open：只弹出看板窗口（服务需已在运行），不启动服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="只起服务，不开窗口")
    parser.add_argument("--no-open-window", dest="no_browser", action="store_true")
    args = parser.parse_args()

    if args.command == "open":
        _open_board_window(f"http://127.0.0.1:{args.port}")
        return

    import uvicorn

    from .web import app

    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, _open_board_window, args=(url,)).start()

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
