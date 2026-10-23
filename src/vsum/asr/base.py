"""语音转写引擎基类。"""

from __future__ import annotations

from typing import Callable, Optional

from ..core.context import Context
from ..core.models import Transcript


class AsrEngine:
    """所有 ASR 引擎的父类。"""

    key: str = ""
    name: str = ""
    note: str = ""
    #: 是否需要联网
    online: bool = False

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.log = ctx.log

    def available(self) -> tuple[bool, str]:
        """返回 ``(是否可用, 不可用原因)``。子类实现。"""
        raise NotImplementedError

    def transcribe(self, audio_path: str, language: str = "zh",
                   on_progress: Optional[Callable[[str, float], None]] = None
                   ) -> Transcript:
        raise NotImplementedError

    @staticmethod
    def _segments_to_transcript(segments, engine: str, language: str = "zh") -> Transcript:
        from ..core.models import Segment
        segs = [Segment(float(getattr(s, "start", 0) or 0),
                        float(getattr(s, "end", 0) or 0),
                        (getattr(s, "text", "") or "").strip())
                for s in segments]
        segs = [s for s in segs if s.text]
        return Transcript(
            source="asr", text="".join(s.text for s in segs),
            segments=segs, language=language, engine=engine,
        )
