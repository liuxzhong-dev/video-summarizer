"""向量库 —— 默认实现是「零依赖的本地库」。

为什么默认不上 Chroma / FAISS
----------------------------
判断依据是**数据规模**，不是「哪个更专业」。这个项目的语料是几千条字幕块和
笔记段落，在这个量级上：

* 暴力算余弦的耗时是毫秒级，索引带来的收益感知不到；
* Chroma 会连带拖进 onnxruntime 等一堆东西，打包后 exe 明显变大；
* 多一个依赖，就多一个「换台机器装不上」的风险。

所以默认用这个：**索引元数据存 JSON，向量存成定长二进制**。等语料真的涨到
几万条以上、毫秒级变成秒级了，再按 ``VectorStore`` 接口换一个实现即可 ——
接口是稳定的，这也是留着插件机制的意义。

文件布局::

    <库目录>/index.json     元信息 + 所有块的文本与出处
    <库目录>/vectors.f32    归一化后的 float32 向量，行主序

向量在**写入时就归一化**，于是检索退化成一次点积，省掉每次查询里的开方。
换 embedding 模型会导致维度变化，那时库必须重建 —— 这一点由 ``add`` 显式拦住，
不静默兼容，因为维度对不上的库检索出来的结果全是噪声。
"""

from __future__ import annotations

import heapq
import json
import math
import os
from array import array
from operator import mul

from ..core.errors import RagError
from ..core.registry import register_store
from .base import Chunk, Hit, VectorStore

#: 库格式版本。将来改结构时靠它判断是否需要迁移。
FORMAT_VERSION = 1

try:                                     # numpy 只是加速项，缺了照跑
    import numpy as _np
except Exception:                        # pragma: no cover
    _np = None


def _normalize(vec: list[float]) -> list[float]:
    """把向量缩放到单位长度。零向量原样返回（避免除零）。"""
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else list(vec)


@register_store("local-json")
class LocalVectorStore(VectorStore):
    """JSON + 二进制向量的本地向量库，零第三方依赖。

    支持「只存文本不存向量」（``vectors=None``）：这样即使向量服务不可用，
    语料也能先入库，检索退化成关键词匹配，等 Key 补上再重建向量。
    """

    key = "local-json"
    name = "本地向量库"
    note = "零依赖，索引存 JSON、向量存二进制；几万条以内暴力检索足够用"

    def __init__(self, ctx, path: str = ""):
        super().__init__(ctx, path or _default_store_path(ctx))
        self._dim = 0
        self._model = ""
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []
        self._loaded = False

    # ---- 维度与模型名 ----
    # 做成属性而不是普通字段，是因为读它们会**自动触发懒加载**。
    # 否则 ``LocalVectorStore(ctx, p).dim`` 拿到的是「还没读索引」的 0，
    # 调用方会据此判断「这个库没有向量」—— 而库里其实存得好好的。
    # 这个坑真实发生过：--rag-list 一度显示「尚未向量化」。
    @property
    def dim(self) -> int:
        self._ensure_loaded()
        return self._dim

    @dim.setter
    def dim(self, value) -> None:
        self._dim = int(value or 0)

    @property
    def model(self) -> str:
        self._ensure_loaded()
        return self._model

    @model.setter
    def model(self, value) -> None:
        self._model = value or ""

    # ---------------- 磁盘读写 ----------------
    @property
    def _index_file(self) -> str:
        return os.path.join(self.path, "index.json")

    @property
    def _vector_file(self) -> str:
        return os.path.join(self.path, "vectors.f32")

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not os.path.isfile(self._index_file):
            return
        try:
            with open(self._index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise RagError(f"向量库索引读取失败：{exc}",
                           hint=f"检查 {self._index_file} 是否损坏；必要时删除后重建")
        meta = data.get("meta") or {}
        self.dim = int(meta.get("dim") or 0)
        self.model = meta.get("model") or ""
        self._chunks = [Chunk.from_dict(d) for d in (data.get("chunks") or [])]

        if self.dim and self._chunks and os.path.isfile(self._vector_file):
            try:
                with open(self._vector_file, "rb") as f:
                    raw = f.read()
                flat = array("f")
                flat.frombytes(raw[: (len(raw) // 4) * 4])
                need = len(self._chunks) * self.dim
                if len(flat) < need:
                    # 向量文件比索引短：多半是上次写入到一半被中断。
                    # 不报错，退化成「文本还在、向量丢了」，关键词检索仍可用。
                    self.ctx.log.warning("向量文件不完整（%d/%d），已退化为纯文本库",
                                         len(flat), need)
                    self.dim, self._vectors = 0, []
                else:
                    self._vectors = [
                        list(flat[i * self.dim:(i + 1) * self.dim])
                        for i in range(len(self._chunks))
                    ]
            except Exception as exc:                      # pragma: no cover
                self.ctx.log.warning("向量文件读取失败：%s，退化为纯文本库", exc)
                self.dim, self._vectors = 0, []

    def _flush(self) -> None:
        """整库重写。语料在千条量级时耗时几十毫秒，不值得为增量写做优化。"""
        os.makedirs(self.path, exist_ok=True)
        index = {
            "meta": {
                "version": FORMAT_VERSION,
                "dim": self.dim,
                "model": self.model,
                "count": len(self._chunks),
            },
            "chunks": [c.to_dict() for c in self._chunks],
        }
        _atomic_write_text(self._index_file, json.dumps(index, ensure_ascii=False))
        if self.dim and self._vectors:
            flat = array("f")
            for v in self._vectors:
                flat.extend(v)
            _atomic_write_bytes(self._vector_file, flat.tobytes())
        elif os.path.isfile(self._vector_file):
            # 库里已经没有向量了，旧文件不能留 —— 留着会让下次加载读到
            # 与索引条数不匹配的向量，症状是检索结果张冠李戴。
            try:
                os.remove(self._vector_file)
            except OSError:
                pass

    # ---------------- 写入 ----------------
    def add(self, chunks: list[Chunk], vectors: list[list[float]] | None = None) -> int:
        self._ensure_loaded()
        chunks = list(chunks or [])
        if not chunks:
            return 0

        if vectors:
            if len(vectors) != len(chunks):
                raise RagError(f"块与向量数量不一致：{len(chunks)} vs {len(vectors)}")
            new_dim = len(vectors[0]) if vectors[0] else 0
            if self.dim and new_dim and new_dim != self.dim:
                raise RagError(
                    f"向量维度不一致：库里是 {self.dim} 维，本次是 {new_dim} 维",
                    hint="换了 embedding 模型就必须重建库（旧向量与新模型不在同一空间）",
                )
            model = (self.ctx.config.get("rag.embed_model", "") or "BAAI/bge-m3").strip()
            if self.model and model and model != self.model:
                raise RagError(
                    f"向量模型不一致：库里是 {self.model}，本次是 {model}",
                    hint="不同模型的向量不可混用，请清空后重建",
                )
            self.dim = new_dim or self.dim
            if model:
                self.model = model

        # 同一来源重复入库时先删旧的，否则同一段内容会有两份向量，
        # 检索结果里出现两条几乎一样的命中，白白占掉 top_k 名额。
        incoming = {(c.source_id, c.index) for c in chunks}
        if incoming:
            keep_idx = [i for i, c in enumerate(self._chunks)
                        if (c.source_id, c.index) not in incoming]
            if len(keep_idx) != len(self._chunks):
                self._chunks = [self._chunks[i] for i in keep_idx]
                if self._vectors:
                    self._vectors = [self._vectors[i] for i in keep_idx]

        self._chunks.extend(chunks)
        if vectors:
            self._vectors.extend(_normalize(v) for v in vectors)
        self._flush()
        return len(chunks)

    # ---------------- 检索 ----------------
    def search(self, vector: list[float], top_k: int = 5) -> list[Hit]:
        self._ensure_loaded()
        if not vector or not self._vectors or not self.dim:
            return []
        q = _normalize(vector)
        if len(q) != self.dim:
            raise RagError(f"查询向量维度不符：库 {self.dim} 维，查询 {len(q)} 维",
                           hint="确认 embedding 配置与建库时用的是同一个模型")

        if _np is not None:
            mat = _np.asarray(self._vectors, dtype=_np.float32)
            qv = _np.asarray(q, dtype=_np.float32)
            scores = (mat @ qv).tolist()
        else:                                            # pragma: no cover
            scores = [sum(map(mul, q, v)) for v in self._vectors]

        k = max(1, min(int(top_k), len(scores)))
        top = heapq.nlargest(k, range(len(scores)), key=lambda i: scores[i])
        return [Hit(chunk=self._chunks[i], score=float(scores[i]), via="vector")
                for i in top]

    # ---------------- 读取 ----------------
    def all_chunks(self) -> list[Chunk]:
        self._ensure_loaded()
        return list(self._chunks)

    def count(self) -> int:
        self._ensure_loaded()
        return len(self._chunks)

    def sources(self) -> list[dict]:
        self._ensure_loaded()
        agg: dict[str, dict] = {}
        for c in self._chunks:
            rec = agg.setdefault(c.source_id or "(未知)", {
                "source_id": c.source_id, "title": c.title, "count": 0,
                "origin": (c.meta or {}).get("origin", ""),
            })
            rec["count"] += 1
            if not rec["title"] and c.title:
                rec["title"] = c.title
        return sorted(agg.values(), key=lambda r: -r["count"])

    def clear(self) -> None:
        self._chunks, self._vectors, self.dim, self.model = [], [], 0, ""
        self._loaded = True
        for f in (self._index_file, self._vector_file):
            if os.path.isfile(f):
                try:
                    os.remove(f)
                except OSError:
                    pass


def _default_store_path(ctx) -> str:
    """库目录默认放在 config.json 旁边 —— 整个文件夹拷走时库跟着走。"""
    cfg_path = getattr(ctx.config, "path", "") or ""
    if cfg_path:
        return os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "rag_store")
    return os.path.join(ctx.workdir, "rag_store")


def _atomic_write_text(path: str, text: str) -> None:
    """先写临时文件再替换。

    直接往目标文件写的话，写到一半断电/被杀进程，留下的是**半截 JSON** ——
    下次启动整个库都读不出来。临时文件 + ``os.replace`` 是原子的，
    要么是新的完整文件，要么还是旧的完整文件。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


__all__ = ["LocalVectorStore"]
