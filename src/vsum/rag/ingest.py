"""入库（ingest）—— 把语料灌进向量库。

三条入口，都收敛到 ``ingest_chunks``：

* ``ingest_transcript`` —— 视频/文章跑完总结后顺手入库（复用已有流水线的产物）
* ``ingest_path``       —— 单个本地文件（Markdown 笔记、PDF、Word 等）
* ``ingest_dir``        —— 批量扫一个目录

一个刻意的设计：**向量化失败不阻断入库**。语料先落库、只存文本，检索退化成
关键词匹配；等 Key 或网络恢复后重新跑一次同样的命令，就会把向量补上
（``add`` 会按「来源 + 序号」替换旧块，不会产生重复）。
"""

from __future__ import annotations

import os

from ..core.errors import RagError
from .base import Chunk
from .chunker import chunk_document, chunk_transcript
from .retriever import build_embedder, build_store

#: 直接按文本读的后缀 —— 不绕文档适配器，避免「少于 180 字符」这类为网页正文
#: 设计的门槛误伤短笔记。
_TEXT_EXT = (".md", ".markdown", ".txt", ".rst", ".log", ".csv", ".tsv")


def _read_text(path: str) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise RagError(f"读取文件失败：{exc}")
    # 几种常见编码都解不出来时，用替换模式强读 —— 总比整篇丢掉好
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def ingest_chunks(ctx, chunks: list[Chunk], store=None, embedder=None) -> dict:
    """把切好的块写进库。这是所有入库路径的终点。"""
    store = store if store is not None else build_store(ctx)
    chunks = [c for c in (chunks or []) if (c.text or "").strip()]
    if not chunks:
        return {"ok": False, "chunks": 0, "vectors": False,
                "message": "没有可入库的内容"}

    vectors = None
    note = ""

    emb = embedder if embedder is not None else build_embedder(ctx)
    if emb is None:
        note = "没有可用的向量化引擎，已只存文本（检索退化为关键词匹配）"
    else:
        ok, reason = emb.available()
        if not ok:
            note = f"向量服务未就绪（{reason}），已只存文本"
        else:
            try:
                ctx.report(f"正在向量化 {len(chunks)} 个片段…", 60)
                vectors = emb.embed([c.text for c in chunks])
            except RagError as exc:
                # 不抛出：语料先落库，向量可以事后补
                ctx.log.warning("向量化失败，改为只存文本：%s", exc)
                note = f"向量化失败（{exc}），已只存文本；修好后重跑一次即可补上向量"
                vectors = None

    added = store.add(chunks, vectors)
    return {
        "ok": True,
        "chunks": added,
        "vectors": bool(vectors),
        "dim": store.dim,
        "total": store.count(),
        "message": note,
    }


def ingest_transcript(ctx, meta, transcript, store=None, embedder=None,
                      max_chars: int = 600, overlap: int = 80) -> dict:
    """把一次总结任务的转写结果入库。

    复用 ``meta`` 里的标题和平台，于是答案里的出处能写成
    「《视频标题》 03:12」这种能直接点回去的形式。
    """
    source_id = f"{getattr(meta, 'platform', 'v')}/{getattr(meta, 'video_id', '') or 'x'}"
    chunks = chunk_transcript(
        transcript,
        source_id=source_id,
        title=getattr(meta, "title", "") or "",
        max_chars=max_chars, overlap=overlap,
        meta={"origin": getattr(transcript, "source", "") or "",
              "url": getattr(meta, "url", "") or "",
              "platform": getattr(meta, "platform", "") or ""},
    )
    info = ingest_chunks(ctx, chunks, store=store, embedder=embedder)
    info["source_id"] = source_id
    return info


def ingest_path(ctx, path: str, store=None, embedder=None,
                max_chars: int = 800, overlap: int = 100) -> dict:
    """把一个本地文件入库。Markdown / 纯文本走直读，其余交给文档适配器。"""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return {"ok": False, "chunks": 0, "vectors": False,
                "message": f"文件不存在：{path}"}

    name = os.path.basename(path)
    title = os.path.splitext(name)[0]
    ext = os.path.splitext(path)[1].lower()

    if ext in _TEXT_EXT:
        text = _read_text(path)
    else:
        text = _read_via_adapter(ctx, path)
        if text is None:
            return {"ok": False, "chunks": 0, "vectors": False,
                    "message": f"这个格式暂时读不了：{ext or '(无后缀)'}"}

    chunks = chunk_document(text, source_id=f"doc/{name}", title=title,
                            max_chars=max_chars, overlap=overlap,
                            meta={"origin": "doc", "path": path})
    info = ingest_chunks(ctx, chunks, store=store, embedder=embedder)
    info["source_id"] = f"doc/{name}"
    info["title"] = title
    return info


def _read_via_adapter(ctx, path: str) -> str | None:
    """PDF / Word / Excel / PPT 这类二进制文档，复用平台适配器里的解析器。

    不自己再写一套 —— 那套解析（按文件头识别、老版 OLE2 细分、可选依赖缺失时
    给出安装提示）已经在 ``platforms/article.py`` 里做过并且被测试覆盖了。
    """
    try:
        from ..platforms.article import PlainTextAdapter

        ad = PlainTextAdapter(ctx)
        meta = ad.resolve(path)
        tr = ad.fetch_subtitle(meta)
        text = (tr.text if tr else "") or ""
        # 适配器对「网页正文」有 180 字下限；本地文档不受这个限制，
        # 所以这里不看它的失败提示，只认拿到手的文本。
        return text or None
    except Exception as exc:                             # noqa: BLE001
        ctx.log.warning("文档解析失败 %s：%s", path, exc)
        return None


def ingest_dir(ctx, directory: str, pattern: str = ".md", store=None, embedder=None,
               max_chars: int = 800, overlap: int = 100,
               recursive: bool = True, skip_dirs: tuple[str, ...] = (".obsidian",)) -> dict:
    """批量入库一个目录。

    注意 ``pattern`` 是**后缀**而不是 glob：这样调用方不用关心
    ``**/*.md`` 这类写法在不同平台的差异，也顺便绕开了 glob 的大小写问题。
    """
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        return {"ok": False, "files": 0, "chunks": 0, "vectors": False,
                "message": f"目录不存在：{directory}"}

    want = (pattern or "").lower()
    files: list[str] = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for n in names:
            if want and not n.lower().endswith(want):
                continue
            files.append(os.path.join(root, n))
        if not recursive:
            break
    files.sort()

    if not files:
        return {"ok": False, "files": 0, "chunks": 0, "vectors": False,
                "message": f"目录里没有匹配 {pattern} 的文件：{directory}"}

    store = store if store is not None else build_store(ctx)
    emb = embedder if embedder is not None else build_embedder(ctx)
    #: 向量化引擎只探一次可用性，避免每个文件都问一遍
    vec_ok, vec_reason = (emb.available() if emb is not None else (False, "没有向量化引擎"))

    all_chunks: list[Chunk] = []
    ok_files, failed = 0, []
    for i, fp in enumerate(files, 1):
        ctx.report(f"正在读取 {os.path.basename(fp)}（{i}/{len(files)}）", 30)
        try:
            name = os.path.basename(fp)
            ext = os.path.splitext(fp)[1].lower()
            text = _read_text(fp) if ext in _TEXT_EXT else _read_via_adapter(ctx, fp)
            if not text:
                failed.append(f"{name}（读不出内容）")
                continue
            title = os.path.splitext(name)[0]
            all_chunks.extend(chunk_document(
                text, source_id=f"doc/{name}", title=title,
                max_chars=max_chars, overlap=overlap,
                meta={"origin": "doc", "path": fp}))
            ok_files += 1
        except Exception as exc:                         # noqa: BLE001
            failed.append(f"{os.path.basename(fp)}（{exc}）")

    info = ingest_chunks(ctx, all_chunks, store=store, embedder=emb)
    info.update({
        "files": ok_files,
        "scanned": len(files),
        "failed": failed,
        "vectors_ready": vec_ok,
        "vector_note": "" if vec_ok else vec_reason,
    })
    return info


__all__ = ["ingest_chunks", "ingest_transcript", "ingest_path", "ingest_dir"]
