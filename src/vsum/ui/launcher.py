"""启动器：起本地服务 → 开桌面窗口（失败就退回浏览器）。

降级链::

    Edge WebView2 内核窗口  →  WinForms(IE) 窗口  →  默认浏览器打开

无论落到哪一档，功能完全一致，只是外壳不同；这样即便目标机器缺
WebView2 运行时，程序也不会「双击没反应」。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from typing import Optional

from ..core.config import Config
from .server import start_server

WINDOW_TITLE = "VideoSummarizer · 视频链接一键总结"


def _icon_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("icon.ico", os.path.join("assets", "icon.ico")):
        p = os.path.join(here, name)
        if os.path.isfile(p):
            return p
    return ""


def open_window(url: str, width: int = 1100, height: int = 800) -> bool:
    """尝试用 pywebview 开一个桌面窗口，成功返回 True。"""
    try:
        import webview
    except Exception:
        return False
    for gui in ("edgechromium", "winforms"):
        try:
            webview.create_window(
                WINDOW_TITLE, url=url, width=width, height=height,
                min_size=(820, 620), text_select=True,
            )
            webview.start(gui=gui, debug=False, private_mode=False)
            return True
        except Exception as exc:
            print(f"[vsum] 桌面窗口（{gui}）启动失败：{exc}", file=sys.stderr)
    return False


def launch(cfg: Optional[Config] = None, port: int = 0,
           prefer_window: bool = True, open_browser: bool = True) -> str:
    """启动服务并打开界面，返回访问地址（阻塞直到窗口关闭或用户中断）。"""
    cfg = cfg or Config.load()
    _server, port = start_server(cfg, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"[vsum] 服务已启动：{url}")

    shown = False
    window_mode = False
    if prefer_window and open_window(url):
        shown = True
        window_mode = True
    if not shown and open_browser:
        try:
            webbrowser.open(url)
            shown = True
        except Exception:
            pass

    if not shown:
        print("[vsum] 无法自动打开界面，请手动访问：" + url)

    # 窗口模式下窗口已关闭才算结束。
    # 浏览器模式下**必须一直阻塞**：服务跑在 daemon 线程里，
    # 主线程一返回，解释器就退出了，daemon 线程被一起杀掉，
    # 浏览器再刷新就是 ERR_CONNECTION_REFUSED。
    # 判断依据是「实际是不是用窗口打开的」，而不是「用户是否想要窗口」——
    # 后者在窗口启动失败时会判断错。
    if window_mode:
        return url

    print("[vsum] 按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return url


def launch_background(cfg: Optional[Config] = None, port: int = 0) -> tuple[str, int]:
    """只起服务不阻塞（测试与二次开发用）。"""
    cfg = cfg or Config.load()
    server, p = start_server(cfg, port=port)
    return f"http://127.0.0.1:{p}/", p
