"""运行上下文：插件之间共享的服务定位器。

插件不直接依赖全局单例，而是从 ``ctx`` 拿配置、HTTP、缓存目录和进度回调，
这样单元测试里塞一个假的 ctx 就能跑。
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Callable, Optional

from .config import Config
from .net import HttpClient


class Context:
    """一次任务执行的上下文。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        http: Optional[HttpClient] = None,
        workdir: str = "",
        progress: Optional[Callable[[str, int], None]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config or Config.load()
        self.log = logger or logging.getLogger("vsum")
        self.http = http or HttpClient(proxy=self.config.get("media.proxy", ""))
        self.progress = progress or (lambda msg, pct: None)

        cache_dir = self.config.get("media.cache_dir") or os.path.join(
            tempfile.gettempdir(), "vsum_cache")
        self.workdir = workdir or cache_dir
        os.makedirs(self.workdir, exist_ok=True)

        # 用户粘贴的**完整原始输入**（可能是整段分享文案）。
        # 有些平台的标题不在链接里、而在分享文案里（典型：微信视频号的
        # 「张三的作品《标题》」），适配器需要从原文案里抠标题。
        # 由 Pipeline.run() 在 resolve 之前填上；默认空串保证适配器
        # 随时可以安全读取。
        self.share_text = ""

    # ---- 便捷访问 ----
    def cookie(self, platform: str) -> str:
        return self.config.get(f"cookies.{platform}", "") or ""

    def ffmpeg(self) -> str:
        """返回可用的 ffmpeg 可执行文件路径，找不到抛 MediaError。"""
        from .ffmpeg import find_ffmpeg, ensure_ffmpeg
        path = self.config.get("media.ffmpeg_path", "") or ""
        if path and os.path.isfile(path):
            return path
        found = find_ffmpeg()
        if found:
            return found
        # 最后兜底：尝试联网下载
        return ensure_ffmpeg(self.workdir)

    def report(self, message: str, percent: int = -1) -> None:
        self.log.info(message)
        try:
            self.progress(message, percent)
        except Exception:            # 回调异常不能影响主流程
            pass

    def path(self, *parts: str) -> str:
        return os.path.join(self.workdir, *parts)

    def close(self) -> None:
        self.http.close()
