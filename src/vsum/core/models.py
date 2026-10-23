"""流水线在各阶段之间传递的数据模型。

数据流向::

    URL -> VideoMeta -> Transcript -> SummaryResult -> 导出文件

每个 dataclass 都带 ``to_dict()``，方便直接 JSON 序列化落盘。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class VideoMeta:
    """视频元数据。平台适配器 ``resolve()`` 的返回值。"""

    platform: str                      # 平台 key，如 "bilibili"
    platform_name: str                 # 中文名，如 "哔哩哔哩"
    video_id: str                      # 平台内部 ID（BV号 / aweme_id ...）
    url: str                           # 规范化后的可播放地址
    title: str = ""
    author: str = ""
    description: str = ""
    duration: Optional[int] = None     # 秒
    cover: Optional[str] = None
    published: Optional[str] = None
    extra: dict = field(default_factory=dict)   # 平台私有字段（cid/aid/photoId ...）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Segment:
    """带时间轴的文本片段（字幕或 ASR 的一句）。"""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "text": self.text}


@dataclass
class Transcript:
    """转写结果。``source`` 决定摘要阶段对它信任多少。"""

    #: subtitle=官方/AI字幕, asr=语音识别, metadata=标题简介等元信息,
    #: comments=弹幕评论, mixed=多源合并
    source: str
    text: str
    segments: list = field(default_factory=list)   # list[Segment]
    language: str = "zh"
    engine: str = ""                                # 具体引擎名，便于排查
    notes: list = field(default_factory=list)       # 人类可读的降级说明

    #: 没有**真实**时间轴的来源。文章的段落时间戳是按字数估的
    #: （见 ``platforms/article._article_transcript``），标成 mm:ss
    #: 只会误导 —— 拖进去的 Excel 每行都会显示 [00:00]。
    #: 这类来源只在「分段细讲」里按顺序列点，不冒充时间线。
    _NO_TIMELINE_SOURCES = ("article", "plain")

    @property
    def has_timeline(self) -> bool:
        return bool(self.segments) and self.source not in self._NO_TIMELINE_SOURCES

    @property
    def char_count(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "engine": self.engine,
            "language": self.language,
            "char_count": self.char_count,
            "notes": self.notes,
            "text": self.text,
            "segments": [
                s.to_dict() if isinstance(s, Segment) else s for s in self.segments
            ],
        }


@dataclass
class SummaryResult:
    """最终摘要产物。"""

    meta: VideoMeta
    transcript: Transcript
    markdown: str
    engine: str = ""          # 使用的 LLM 引擎名
    keywords: list = field(default_factory=list)
    elapsed: float = 0.0
    #: Obsidian 入库结果（评分/档位/写入路径）。未启用时为空 dict。
    obsidian: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "meta": self.meta.to_dict(),
            "transcript": self.transcript.to_dict(),
            "summary_engine": self.engine,
            "keywords": self.keywords,
            "elapsed_seconds": round(self.elapsed, 2),
            "markdown": self.markdown,
            "obsidian": self.obsidian,
        }


_SRT_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{1,3}$")


def fmt_ts(seconds: float) -> str:
    """秒 -> SRT 时间格式 ``00:01:02,340``。"""
    if seconds is None or seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_hms(seconds: float) -> str:
    """秒 -> ``[01:02]`` 这种便于人看的短格式。"""
    if seconds is None or seconds < 0:
        seconds = 0.0
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def to_srt(segments: Any) -> str:
    """把时间轴片段导出为 SRT 字幕文本。"""
    if not segments:
        return ""
    out = []
    for i, seg in enumerate(segments, 1):
        if isinstance(seg, Segment):
            start, end, text = seg.start, seg.end, seg.text
        elif isinstance(seg, dict):
            start, end, text = seg.get("start", 0), seg.get("end", 0), seg.get("text", "")
        else:
            continue
        out.append(f"{i}\n{fmt_ts(start)} --> {fmt_ts(end)}\n{text}\n")
    return "\n".join(out)
