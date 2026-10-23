"""RAG 子系统：在语料上做语义检索问答。

与主流水线（``core/pipeline.py``）的关系：主流水线负责「把链接变成文本」，
这里负责「把文本变成可检索的知识」。两者共享同一套基础设施 ——
插件注册表、配置、HTTP 客户端、降级思路。

最常用的三个入口::

    from vsum.rag import ingest_dir, ask
    ingest_dir(ctx, r"D:\\KnowledgeBase", pattern=".md")   # 建库
    print(ask(ctx, "切块大小是怎么定的？")["answer"])       # 问答
"""

from .ask import ask, build_prompt
from .base import Chunk, Embedder, Hit, VectorStore
from .ingest import ingest_chunks, ingest_dir, ingest_path, ingest_transcript
from .retriever import Retriever, build_embedder, build_store

# 导入实现模块，让 @register_embedder / @register_store 装饰器执行。
# 这几个 import 必须在最后：它们依赖上面那些模块已经就绪。
from . import embedder, store                            # noqa: E402,F401

__all__ = [
    "Chunk", "Hit", "Embedder", "VectorStore",
    "ask", "build_prompt",
    "ingest_chunks", "ingest_dir", "ingest_path", "ingest_transcript",
    "Retriever", "build_embedder", "build_store",
]
