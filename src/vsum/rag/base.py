"""RAG 的公共数据模型与接口约定。

把两样东西放在一起，是因为它们是这个系统最常被替换的两个零件：

* ``Chunk`` / ``Hit`` —— 在检索链路里流动的数据；
* ``Embedder`` / ``VectorStore`` —— 两个基类，换服务商 / 换向量库时只动实现。

设计取向：**任何一步都要有能用的兜底**。这与本项目的其他部分一致（字幕失败
退转写、转写失败退元信息）。RAG 里对应的两条兜底是：

1. 向量化失败 → 库仍可建，只是没有向量，检索退化为关键词匹配；
2. 大模型不可用 → 不做生成，直接把检索到的原文片段按相关度列出来。

两条都实现之后，「没有 API Key」不再是不可用，只是效果打折。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:                                    # pragma: no cover
    from ..core.context import Context


# ------------------------------------------------------------------ 数据模型

@dataclass
class Chunk:
    """一个可被检索的最小单元。

    对视频字幕来说，一个 chunk 是「一段时间内说的一串话」，带起止秒数；
    对纯文本文档来说，它是一段正文，时间字段为 -1（表示没有时间轴）。

    为什么不用「固定字符数切」这种最省事的做法：字幕本身已经带时间戳，
    按说话节奏切出来的块在语义上更完整，而且能回链到播放器的那一秒。
    """

    text: str
    #: 来源标识。视频用 ``bilibili/BV1xx``，文档用相对路径。
    source_id: str = ""
    #: 人类可读的来源标题（视频标题 / 文件名），用于在答案里标出处。
    title: str = ""
    #: 起始秒；-1 表示该来源没有真实时间轴。
    start: float = -1.0
    end: float = -1.0
    #: 在本来源内的序号，从 0 开始。检索结果展示「第 N 段」时用。
    index: int = 0
    #: 可选附加信息（平台、来源类型 subtitle/asr/... ）
    meta: dict = field(default_factory=dict)

    @property
    def has_time(self) -> bool:
        return self.start >= 0 and self.end >= 0

    @property
    def cite(self) -> str:
        """出处短标签，例：``《标题》 03:12``。"""
        from ..core.models import fmt_hms

        head = f"《{self.title}》" if self.title else self.source_id
        return f"{head} {fmt_hms(self.start)}" if self.has_time else head

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source_id": self.source_id,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "index": self.index,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        return cls(
            text=d.get("text", ""),
            source_id=d.get("source_id", ""),
            title=d.get("title", ""),
            start=float(d.get("start", -1)),
            end=float(d.get("end", -1)),
            index=int(d.get("index", 0)),
            meta=d.get("meta") or {},
        )


@dataclass
class Hit:
    """一次检索命中。

    ``via`` 标明这条命中的来路，是排查「为什么这个问题答不好」的第一现场：
    ``vector`` 说明是语义相近（改写过的问法也能召回），
    ``keyword`` 说明只是字面撞上了（多半意味着语料里没有语义上更贴的内容）。
    """

    chunk: Chunk
    score: float
    via: str = "vector"

    @property
    def cite(self) -> str:
        return self.chunk.cite

    def to_dict(self) -> dict:
        return {"chunk": self.chunk.to_dict(), "score": round(self.score, 4),
                "via": self.via}


# ------------------------------------------------------------------ 基类

class Embedder:
    """把文本变成向量的东西。

    实现方只需要管三件事：能不能用（``available``）、向量多少维（``dim``）、
    怎么把一批文本换成向量（``embed``）。
    """

    key: str = ""
    name: str = ""
    note: str = ""
    #: 向量维度。运行时由服务端返回的向量长度校正，这里只是声明式的默认值。
    dim: int = 0

    def __init__(self, ctx: "Context"):
        self.ctx = ctx

    def available(self) -> tuple[bool, str]:
        """返回 ``(是否可用, 不可用的原因)``。"""
        return False, "未实现"

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。**返回顺序必须与输入一一对应**，长度不等即视为失败。"""
        raise NotImplementedError


class VectorStore:
    """向量库。

    刻意只定义四个方法。真要用 FAISS / Chroma / Milvus 时，
    把它们的索引包一层适配即可，上层检索逻辑不用知道底下是什么。
    """

    key: str = ""
    name: str = ""
    note: str = ""
    #: 维度，0 表示库里还没有向量（可能只存了文本，靠关键词检索）。
    dim: int = 0

    def __init__(self, ctx: "Context", path: str = ""):
        self.ctx = ctx
        self.path = path

    def add(self, chunks: list[Chunk], vectors: Optional[list[list[float]]] = None) -> int:
        """写入一批块。``vectors`` 为空表示只存文本（离线降级模式）。"""
        raise NotImplementedError

    def search(self, vector: list[float], top_k: int = 5) -> list[Hit]:
        """按向量检索。"""
        raise NotImplementedError

    def all_chunks(self) -> list[Chunk]:
        """取出全部块，供关键词兜底检索使用。"""
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def sources(self) -> list[dict]:
        """列出库里的来源，供「我到底灌了些什么」的检查用。"""
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


__all__ = ["Chunk", "Hit", "Embedder", "VectorStore"]
