#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌面界面入口：双击即可用。

    python run_ui.py                 # 自动开窗口（失败则用浏览器）
    python run_ui.py --browser       # 强制用浏览器
    python run_ui.py --port 8765     # 固定端口（多实例/联调用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="VideoSummarizer 桌面界面")
    p.add_argument("--browser", action="store_true", help="不用内嵌窗口，直接用浏览器打开")
    p.add_argument("--port", type=int, default=0, help="固定端口，0 表示自动选择")
    p.add_argument("--no-open", action="store_true", help="只起服务，不自动打开界面")
    args = p.parse_args(argv)

    from vsum.core.config import Config
    from vsum.ui.launcher import launch

    cfg = Config.load()
    try:
        launch(cfg, port=args.port, prefer_window=not args.browser,
               open_browser=not args.no_open)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
