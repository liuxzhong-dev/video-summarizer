"""示例插件：给「知乎视频/回答」加一个适配器。

把它放在 ``plugins/`` 目录下，程序启动时会自动加载 —— **主流程一行都不用改**。

这就是本项目扩展点的全部约定：
继承基类 → 实现接口 → 用装饰器注册 → 丢进 plugins/。
"""

from __future__ import annotations

import re

# 注意：作为外部插件被加载时，用绝对导入引用 vsum
from vsum.core.models import VideoMeta
from vsum.core.registry import register_platform
from vsum.platforms.base import SUPPORT_METADATA, PlatformAdapter


@register_platform("zhihu")
class ZhihuAdapter(PlatformAdapter):
    """知乎（视频/回答）。"""

    key = "zhihu"
    name = "知乎"
    domains = ("zhihu.com", "zhuanlan.zhihu.com")
    support = SUPPORT_METADATA
    note = "示例插件：基于分享页 HTML meta 解析标题与简介。"

    # ---- 必须实现的接口 ----
    def resolve(self, url: str) -> VideoMeta:
        vid = self.extract_id(url) or ""
        info = self._meta(url)
        return VideoMeta(
            platform=self.key, platform_name=self.name, video_id=vid,
            url=url, title=info.get("title") or "知乎内容",
            description=info.get("description") or "",
            extra={"via": "plugin-example"},
        )

    # ---- 可选接口：返回字幕/元信息，返回 None 则流水线自动降级 ----
    def fetch_subtitle(self, meta: VideoMeta):
        if not meta.description:
            return None
        from vsum.core.models import Transcript
        return Transcript(source="metadata",
                          text=f"标题：{meta.title}\n简介：{meta.description[:1200]}",
                          engine="zhihu-plugin", language="zh",
                          notes=["这是示例插件产出的元信息转写。"])

    # ---- 内部 ----
    @classmethod
    def extract_id(cls, url: str):
        m = re.search(r"/(?:zvideo|answer|p)/(\d{6,})", url or "")
        return m.group(1) if m else None

    def _meta(self, url: str) -> dict:
        out = {}
        import html as _h
        try:
            page = self.http.get_text(url)
        except Exception:
            return out
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="(.*?)"', page, re.S)
        if m:
            out["title"] = _h.unescape(m.group(1)).strip()
        m = re.search(r'<meta[^>]+name="description"[^>]+content="(.*?)"', page, re.S)
        if m:
            out["description"] = _h.unescape(m.group(1)).strip()
        if not out.get("title"):
            m = re.search(r"<title>(.*?)</title>", page, re.S)
            if m:
                out["title"] = _h.unescape(m.group(1)).strip()
        return out


# ---------------------------------------------------------------------------
# 同理，新增「语音识别引擎」和「摘要引擎」也是同样的套路：
#
#   from vsum.core.registry import register_asr, register_llm
#   from vsum.asr.base import AsrEngine
#
#   @register_asr("my-asr")
#   class MyAsr(AsrEngine):
#       key = "my-asr"; name = "我的识别服务"
#       def available(self): return True, ""
#       def transcribe(self, audio_path, language="zh", on_progress=None):
#           ...返回 vsum.core.models.Transcript...
#
#   @register_llm("my-llm")
#   class MyLlm(LlmEngine):        # from vsum.llm.base import LlmEngine
#       key = "my-llm"; name = "我的大模型"
#       def available(self): return True, ""
#       def summarize(self, meta, transcript, on_token=None):
#           ...返回 Markdown 字符串...
# ---------------------------------------------------------------------------
