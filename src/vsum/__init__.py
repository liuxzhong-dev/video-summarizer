"""VideoSummarizer —— 把国内主流平台的视频链接变成一份中文总结。

最常用的两个入口::

    from vsum import summarize           # 一句话调用
    from vsum.core.pipeline import Pipeline   # 需要进度回调时用这个

    result = summarize("https://www.bilibili.com/video/BV1xx411c7mD")
    print(result.markdown)

版本信息见 ``__version__``。
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "VideoSummarizer"

from .extensions import load_all  # noqa: E402  (保证导入即用)


def summarize(text_or_url: str, config=None, progress=None, on_token=None) -> "SummaryResult":
    """高层 API：传链接（或整段分享文案），返回 ``SummaryResult``。

    :param text_or_url: 视频链接，或手机 App 复制出来的一整段分享文案
    :param config:      ``vsum.core.config.Config``；None 则自动加载配置文件
    :param progress:    ``f(message, percent)`` 进度回调
    :param on_token:    大模型流式输出的逐字回调
    """
    load_all()
    from .core.config import Config
    from .core.context import Context
    from .core.pipeline import Pipeline

    ctx = Context(config=config or Config.load(), progress=progress)
    try:
        return Pipeline(ctx).run(text_or_url, on_token=on_token)
    finally:
        ctx.close()


__all__ = ["summarize", "load_all", "__version__"]
