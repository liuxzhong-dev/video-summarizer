"""检索器 —— 向量召回为主，关键词兜底为辅。

为什么一定要有关键词这条路
--------------------------
不是为了「更好」，是为了「还能用」。向量检索依赖外部服务，它会因为没配 Key、
额度用尽、网络抖动、服务商改接口而失效；而这些时候语料本身还在库里，
字面匹配依然能召回一部分内容。这与本项目在视频链路上的处置一致 ——
字幕拿不到就转写，转写失败就退到元信息，**每一步都留一条能走的路**。

两条路的分工：

* **向量**：理解语义。问「怎么防止模型胡说」也能召回写着「防幻觉」的段落。
* **关键词**：字面命中。问「bge-m3 多少维」时，只要原文出现过这个词就能抓到。

实际做法是先跑向量，命中数不够 top_k 时再用关键词补齐 —— 而不是两路各取
一半：向量结果质量更高，能凑满就不该掺入字面结果。
"""

from __future__ import annotations

import re
from typing import Optional

from ..core.errors import RagError
from ..core.registry import all_embedders, all_stores
from .base import Chunk, Embedder, Hit, VectorStore

#: 中文连续片段
_ZH = re.compile(r"[\u4e00-\u9fff]+")
#: 英文 / 数字词组（长度 >= 2 才有区分度，单字母噪声太大）
_EN = re.compile(r"[A-Za-z0-9_]{2,}")


def terms_of(text: str) -> set[str]:
    """把文本拆成可比较的检索单元。

    中文没有空格分词，这里用 **2-gram**（相邻两字）近似：它的召回率比按字
    单切高，又不需要引入 jieba 这类分词依赖。代价是会有一些无意义的组合，
    但对「排序对不对」这个目标来说够用 —— 真正的语义判断交给向量那一路。
    """
    out: set[str] = set()
    for run in _ZH.findall(text or ""):
        if len(run) == 1:
            out.add(run)
            continue
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    for w in _EN.findall(text or ""):
        out.add(w.lower())
    return out


def keyword_score(query_terms: set[str], doc_terms: set[str]) -> float:
    """二值余弦：交集大小除以两边长度的几何平均。

    为什么不用「命中数 / 查询词数」：那样长文档天然占优（词多，撞上的机会多）。
    用几何平均归一化后，长短块之间可比。
    """
    if not query_terms or not doc_terms:
        return 0.0
    inter = len(query_terms & doc_terms)
    if not inter:
        return 0.0
    return inter / ((len(query_terms) ** 0.5) * (len(doc_terms) ** 0.5))


# ------------------------------------------------------------------ 工厂

def build_store(ctx, path: str = "") -> VectorStore:
    """按配置实例化向量库。配置里写了不存在的实现时，退回默认的那个。"""
    key = (ctx.config.get("rag.store", "") or "local-json").strip()
    table = all_stores()
    cls = table.get(key) or table.get("local-json")
    if cls is None:
        raise RagError("向量库插件未加载",
                       hint="检查 vsum.rag 是否被正常导入（打包版看 --list 输出）")
    return cls(ctx, path or (ctx.config.get("rag.store_path", "") or ""))


def build_embedder(ctx) -> Optional[Embedder]:
    """按配置实例化向量化引擎。没注册任何实现时返回 None（上层转关键词）。"""
    key = (ctx.config.get("rag.embedder", "") or "siliconflow").strip()
    cls = all_embedders().get(key)
    if cls is None:
        # 只按 key 找不到时，退回唯一可用的那个实现 —— 比直接失败友好
        table = all_embedders()
        cls = next(iter(table.values()), None)
    return cls(ctx) if cls else None


# ------------------------------------------------------------------ 检索

class Retriever:
    """查询入口。上层只需要 ``search(问题)``。"""

    def __init__(self, ctx, store: Optional[VectorStore] = None,
                 embedder: Optional[Embedder] = None, path: str = ""):
        self.ctx = ctx
        self.store = store if store is not None else build_store(ctx, path)
        self.embedder = embedder if embedder is not None else build_embedder(ctx)
        #: 关键词那一路要对每个块算 term 集合，几千条时每次查询重算是纯浪费。
        #: 缓存到库里内容变化为止（用 count 作为粗略失效信号）。
        self._term_cache: list[set[str]] = []
        self._term_cache_at = -1
        self._last_mode = ""
        #: 上次检索里向量那一路失败的原因。空串表示没失败。
        #: 单独留下来是因为「退化成 keyword」有两种完全不同的成因 ——
        #: 一种是压根没配 Key（正常），另一种是库和模型的维度对不上（配置错误）。
        #: 只看 mode 分不出来，而两者的处置方式完全相反。
        self.last_error = ""

    # ---- 缓存 ----
    def invalidate(self) -> None:
        """库内容变了（刚灌完数据）之后调用，避免关键词检索命中旧快照。"""
        self._term_cache, self._term_cache_at = [], -1

    def _doc_terms(self, chunks: list[Chunk]) -> list[set[str]]:
        if self._term_cache_at != len(chunks) or len(self._term_cache) != len(chunks):
            self._term_cache = [terms_of(c.text) for c in chunks]
            self._term_cache_at = len(chunks)
        return self._term_cache

    # ---- 主入口 ----
    def search(self, query: str, top_k: int = 5,
               min_score: float = 0.0) -> tuple[list[Hit], str]:
        """返回 ``(命中列表, 实际使用的检索方式)``。

        检索方式取值：``vector`` / ``keyword`` / ``mixed``。把它一起返回，
        是因为「答得不好」时第一个要看的判断就是它 —— 退成 keyword 往往
        意味着向量那一路已经挂了，而不是语料里没有。
        """
        query = (query or "").strip()
        if not query:
            return [], "none"
        k = max(1, int(top_k))

        hits: list[Hit] = []
        mode = "keyword"
        self.last_error = ""

        if self.embedder is not None:
            ok, reason = self.embedder.available()
            if ok and self.store.count():
                try:
                    vec = self.embedder.embed([query])[0]
                    hits = self.store.search(vec, k)
                    mode = "vector"
                except RagError as exc:
                    # 不抛出：检索还有关键词这条路可走。
                    # 但把原因记下来，否则「搜不准」会被误判成语料质量差。
                    self.last_error = str(exc)
                    self.ctx.log.warning("向量检索不可用，退化为关键词：%s", exc)
                    hits, mode = [], "keyword"
            elif not ok:
                self.last_error = reason
                self.ctx.log.info("向量化引擎不可用（%s），改用关键词检索", reason)

        if min_score:
            hits = [h for h in hits if h.score >= min_score]

        if len(hits) < k:
            seen = {(h.chunk.source_id, h.chunk.index) for h in hits}
            extra = self._keyword_search(query, k - len(hits), seen)
            if extra:
                mode = "mixed" if hits else "keyword"
                hits.extend(extra)

        self._last_mode = mode
        return hits, mode

    def _keyword_search(self, query: str, k: int,
                        exclude: set[tuple[str, int]]) -> list[Hit]:
        q = terms_of(query)
        if not q or k <= 0:
            return []
        chunks = self.store.all_chunks()
        cache = self._doc_terms(chunks)
        scored: list[tuple[float, Chunk]] = []
        for c, dt in zip(chunks, cache):
            if (c.source_id, c.index) in exclude:
                continue
            s = keyword_score(q, dt)
            if s > 0:
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [Hit(chunk=c, score=s, via="keyword") for s, c in scored[:k]]


__all__ = ["Retriever", "build_store", "build_embedder", "terms_of", "keyword_score"]
