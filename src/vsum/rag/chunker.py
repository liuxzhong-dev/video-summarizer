"""切块（chunking）—— RAG 里最影响上限的一步。

切得好不好，直接决定检索能不能召回对的东西；模型再强也救不回来切碎的语义。
所以这里按**语料类型**分两条路，而不是统一按字符数硬切：

============  ==========================================================
语料           切法
============  ==========================================================
视频字幕       按自带的时间戳累积成块，块边界落在说话停顿处，
               并记下起止秒数 —— 检索结果因此能回链到播放器的那一秒
Markdown 笔记  按标题层级成节，节内按段落打包；标题会写进块首，
               让「标题里的词」也能参与语义匹配
============  ==========================================================

两条路最后都收敛到 ``_hard_split``：只有当单个块实在超过上限时才硬切，
且优先在句号/换行处下刀，避免把一句话从中间切断。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from .base import Chunk

#: 句末标点。硬切时向后找这些字符，切在它们后面读起来最自然。
_SENT_END = ("。", "！", "？", "；", "…", "\n", ".", "!", "?", ";")

#: Markdown 标题行：一到六个 #
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")

#: YAML frontmatter 的开头
_FM_OPEN = re.compile(r"^---\s*$")


# ------------------------------------------------------------------ 工具

def strip_frontmatter(text: str) -> tuple[str, dict]:
    """剥掉 Markdown 的 YAML frontmatter，返回 ``(正文, 元数据)``。

    frontmatter 里的 ``tags`` / ``type`` 是有检索价值的标签，但要单独处理 ——
    留在正文里会被当成普通句子参与向量化，稀释语义。

    只认**文件最开头**的那一对 ``---``：正文中间出现的水平分割线不能被误吞。
    """
    lines = text.split("\n")
    if not lines or not _FM_OPEN.match(lines[0]):
        return text, {}
    for i in range(1, min(len(lines), 60)):
        if _FM_OPEN.match(lines[i]):
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            return body, _parse_simple_yaml(raw)
    return text, {}


def _parse_simple_yaml(raw: str) -> dict:
    """够用就好的 frontmatter 解析：``key: value`` 与 ``[a, b]`` 列表。

    不引 yaml 库 —— 它对这份文件里的内容是杀鸡用牛刀，而这个项目一直在
    避免为小功能引入重依赖（引进来还得跟着打包）。
    """
    out: dict = {}
    for ln in raw.split("\n"):
        if not ln.strip() or ln.lstrip().startswith("#") or ":" not in ln:
            continue
        key, _, val = ln.partition(":")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if val.startswith("[") and val.endswith("]"):
            items = [v.strip().strip("'\"") for v in val[1:-1].split(",")]
            out[key] = [v for v in items if v]
        else:
            out[key] = val.strip("'\"")
    return out


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    """单块超过上限时的兜底硬切，尽量切在句末标点上。"""
    n = len(text)
    if n <= max_chars:
        return [text.strip()] if text.strip() else []
    out: list[str] = []
    i = 0
    while i < n:
        end = min(i + max_chars, n)
        if end < n:
            # 只看窗口后 40%，避免切得太靠前导致块过小
            lo = i + int(max_chars * 0.6)
            cut = -1
            for mark in _SENT_END:
                pos = text.rfind(mark, lo, end)
                if pos > cut:
                    cut = pos
            if cut > 0:
                end = cut + 1
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        # 保证一定前进，否则 overlap >= 窗口时会死循环
        i = max(end - overlap, i + 1)
    return out


# ------------------------------------------------------------------ 字幕

def chunk_transcript(transcript, source_id: str = "", title: str = "",
                     max_chars: int = 600, overlap: int = 80,
                     meta: Optional[dict] = None) -> list[Chunk]:
    """把一条视频的转写结果切成块。

    有真实时间轴的来源（字幕 / 语音识别）按 ``segments`` 累积；
    ``article`` / ``plain`` 这类没有时间轴的内容退到文档切法，并明确
    把时间标成 -1 —— 假装有时间会让答案给出错误的定位。
    """
    base_meta = dict(meta or {})
    base_meta.setdefault("origin", getattr(transcript, "source", "") or "unknown")

    segs = getattr(transcript, "segments", None) or []
    if not segs or not getattr(transcript, "has_timeline", False):
        chunks = chunk_document(transcript.text or "", source_id, title,
                                max_chars=max_chars, overlap=overlap, meta=base_meta)
        return chunks

    chunks: list[Chunk] = []
    idx = 0
    buf: list[str] = []
    buf_len = 0
    start = end = -1.0

    def flush() -> None:
        nonlocal buf, buf_len, start, end, idx
        text = " ".join(x for x in buf if x).strip()
        if text:
            chunks.append(Chunk(
                text=text, source_id=source_id, title=title,
                start=start, end=end, index=idx, meta=dict(base_meta),
            ))
            idx += 1
        buf, buf_len, start, end = [], 0, -1.0, -1.0

    for s in segs:
        if isinstance(s, dict):
            t = (s.get("text") or "").strip()
            s_start = float(s.get("start", 0) or 0)
            s_end = float(s.get("end", s_start) or s_start)
        else:
            t = (s.text or "").strip()
            s_start, s_end = float(s.start), float(s.end)
        if not t:
            continue
        if buf and buf_len + len(t) > max_chars:
            flush()
        if start < 0:
            start = s_start
        end = s_end
        buf.append(t)
        buf_len += len(t) + 1
    flush()

    # 极端情况：所有 segment 加起来还不如一句话长，也至少给一个块
    if not chunks and (transcript.text or "").strip():
        return chunk_document(transcript.text, source_id, title,
                              max_chars=max_chars, overlap=overlap, meta=base_meta)
    return chunks


# ------------------------------------------------------------------ 文档

def _iter_sections(text: str) -> Iterable[tuple[str, list[str]]]:
    """把 Markdown 拆成 ``(标题路径, 段落列表)``。

    标题路径形如 ``架构 > 打包 > 内置内核``：层级用 ``>`` 连起来写进块首，
    这样单独一个段落块被召回时，读者（和模型）也知道它属于哪一节。

    纯文本没有标题，整体作为一节返回，段落按空行切。
    """
    lines = text.split("\n")
    heads: dict[int, str] = {}
    paras: list[str] = []
    buf: list[str] = []

    def path() -> str:
        return " > ".join(heads[k] for k in sorted(heads) if heads.get(k))

    def flush_para() -> None:
        nonlocal buf
        p = "\n".join(buf).strip()
        if p:
            paras.append(p)
        buf = []

    for ln in lines:
        m = _MD_HEAD.match(ln)
        if m:
            flush_para()
            if paras:
                yield path(), paras
                paras = []
            level = len(m.group(1))
            heads[level] = m.group(2).strip()
            for k in [k for k in heads if k > level]:
                del heads[k]
            continue
        if not ln.strip():
            flush_para()
            continue
        buf.append(ln)
    flush_para()
    if paras:
        yield path(), paras


def chunk_document(text: str, source_id: str = "", title: str = "",
                   max_chars: int = 800, overlap: int = 100,
                   meta: Optional[dict] = None) -> list[Chunk]:
    """把一篇文档（Markdown 笔记 / 纯文本 / 文章正文）切成块。

    ``max_chars`` 比字幕那条路大一些：文档的段落本来就是完整语义单元，
    切太碎反而丢上下文。
    """
    base_meta = dict(meta or {})
    body, fm = strip_frontmatter(text or "")
    if fm:
        # frontmatter 里的标签不进正文，但记在 meta 里 —— 排查召回问题时
        # 能一眼看出「这个块来自哪类笔记」。
        base_meta.setdefault("frontmatter", fm)
    if title:
        base_meta.setdefault("doc_title", title)

    out: list[Chunk] = []
    idx = 0
    for prefix, paras in _iter_sections(body):
        head = f"{prefix}\n" if prefix else ""
        buf: list[str] = []
        buf_len = 0

        def flush() -> None:
            nonlocal buf, buf_len, idx
            joined = "\n".join(buf).strip()
            if joined:
                out.append(Chunk(
                    text=(head + joined).strip(),
                    source_id=source_id, title=title,
                    start=-1.0, end=-1.0, index=idx, meta=dict(base_meta),
                ))
                idx += 1
            buf, buf_len = [], 0

        for p in paras:
            if len(head) + len(p) > max_chars:
                # 单段就超长：先冲掉已攒的，再硬切这一段（每片都带标题路径）
                flush()
                for piece in _hard_split(head + p, max_chars, overlap):
                    out.append(Chunk(text=piece, source_id=source_id, title=title,
                                     start=-1.0, end=-1.0, index=idx,
                                     meta=dict(base_meta)))
                    idx += 1
                continue
            if buf and buf_len + len(p) + len(head) > max_chars:
                flush()
            buf.append(p)
            buf_len += len(p) + 1
        flush()
    return out


def chunk_text(text: str, source_id: str = "", title: str = "",
               max_chars: int = 800, overlap: int = 100,
               meta: Optional[dict] = None) -> list[Chunk]:
    """不区分来源类型的通用入口（给「直接贴一段文字」用）。"""
    return chunk_document(text, source_id, title,
                          max_chars=max_chars, overlap=overlap, meta=meta)


__all__ = ["chunk_transcript", "chunk_document", "chunk_text", "strip_frontmatter"]
