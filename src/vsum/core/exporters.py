"""结果导出：Markdown / 纯文本 / SRT 字幕 / JSON。

``export(result, outdir)`` 一次生成全部格式，文件名用「平台_视频ID」打头，
方便批量跑完直接在资源管理器里翻。
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable

from .models import SummaryResult, fmt_hms, to_srt


def safe_filename(name: str, maxlen: int = 60) -> str:
    """去掉 Windows/Unix 都不允许的文件名字符。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:maxlen] or "video").strip()


def base_name(result: SummaryResult) -> str:
    meta = result.meta
    return safe_filename(f"{meta.platform}_{meta.video_id or ''}_{meta.title}")


def to_plain_text(result: SummaryResult) -> str:
    """Markdown -> 易读纯文本（去掉 #、* 等标记）。"""
    md = result.markdown or ""
    md = re.sub(r"^#+\s*", "", md, flags=re.M)
    md = re.sub(r"^\s*>\s?", "", md, flags=re.M)
    md = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
    md = re.sub(r"^\s*[-*]\s*", "· ", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def to_markdown(result: SummaryResult) -> str:
    """带元信息头的完整 Markdown（比 result.markdown 多一段来源说明）。"""
    meta = result.meta
    head = [
        f"# {meta.title}",
        "",
        f"- 平台：{meta.platform_name}",
        f"- 链接：{meta.url}",
    ]
    if meta.author:
        head.append(f"- 作者：{meta.author}")
    if meta.duration:
        head.append(f"- 时长：{meta.duration} 秒")
    head.append(f"- 文本来源：{_source_label(result)}")
    head.append(f"- 摘要引擎：{result.engine}")
    head.append("")
    return "\n".join(head) + result.markdown


def _source_label(result: SummaryResult) -> str:
    t = result.transcript
    return {
        "subtitle": f"视频字幕（{t.engine}）",
        "asr": f"语音识别（{t.engine}）",
        "metadata": "标题/简介等元信息",
        "mixed": "多来源合并",
    }.get(t.source, t.source)


def export(result: SummaryResult, outdir: str,
           formats: Iterable[str] = ("md", "txt", "json")) -> list[str]:
    """导出到目录，返回生成的文件路径列表。"""
    os.makedirs(outdir, exist_ok=True)
    stem = base_name(result)
    written = []

    if "md" in formats:
        p = os.path.join(outdir, stem + ".md")
        _write(p, to_markdown(result))
        written.append(p)
    if "txt" in formats:
        p = os.path.join(outdir, stem + ".txt")
        _write(p, to_plain_text(result))
        written.append(p)
    if "json" in formats:
        p = os.path.join(outdir, stem + ".json")
        _write(p, json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        written.append(p)
    if "srt" in formats and result.transcript.segments:
        p = os.path.join(outdir, stem + ".srt")
        _write(p, to_srt(result.transcript.segments))
        written.append(p)
    return written


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def timeline_text(result: SummaryResult, per_seconds: int = 60) -> str:
    """生成「时间线 + 该时段文本」的对照文本，方便回看原视频。"""
    segs = result.transcript.segments
    if not segs:
        return ""
    buckets: dict[int, list[str]] = {}
    for s in segs:
        st = s.start if hasattr(s, "start") else (s.get("start") or 0)
        tx = s.text if hasattr(s, "text") else (s.get("text") or "")
        buckets.setdefault(int(st // per_seconds), []).append(tx)
    return "\n".join(f"[{fmt_hms(b * per_seconds)}] " + " ".join(v)
                     for b, v in sorted(buckets.items()))
