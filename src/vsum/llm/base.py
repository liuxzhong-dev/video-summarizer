"""大模型摘要引擎基类 + 统一的提示词构造。"""

from __future__ import annotations

from typing import Callable, Optional

from ..core.context import Context
from ..core.models import Transcript, VideoMeta

#: 三种输出风格
STYLE_BRIEF = "brief"        # 一句话 + 要点
STYLE_DETAILED = "detailed"  # 含分段细讲
STYLE_BULLETS = "bullets"    # 纯要点清单

STYLE_LABELS = {
    STYLE_BRIEF: "极简（一句话 + 要点）",
    STYLE_DETAILED: "详细（含分段细讲）",
    STYLE_BULLETS: "要点清单（Markdown 列表）",
}


def build_prompt(meta: VideoMeta, transcript: Transcript, style: str = STYLE_DETAILED,
                 with_timeline: bool = True, with_keywords: bool = True,
                 max_chars: int = 24000) -> tuple[str, str]:
    """构造 ``(system, user)`` 提示词。

    返回的是两段的字符串，方便各引擎自行拼装消息体。
    """
    text = transcript.text or ""
    if len(text) > max_chars:
        # 保留头尾（开头常是引入，结尾常是结论），中间截断
        head = text[: int(max_chars * 0.65)]
        tail = text[-int(max_chars * 0.35):]
        text = head + "\n…（中间部分已省略）…\n" + tail

    has_ts = transcript.has_timeline and with_timeline
    if has_ts:
        from ..core.models import fmt_hms
        lines = []
        for s in transcript.segments:
            if isinstance(s, dict):
                st, tx = s.get("start", 0), s.get("text", "")
            else:
                st, tx = s.start, s.text
            lines.append(f"[{fmt_hms(st)}] {tx}")
        body = "\n".join(lines)
    else:
        body = text

    meta_block = "\n".join(filter(None, [
        f"标题：{meta.title}",
        f"作者：{meta.author}" if meta.author else "",
        f"平台：{meta.platform_name}",
        f"时长：{meta.duration} 秒" if meta.duration else "",
    ]))

    src_desc = {
        "subtitle": "视频字幕原文",
        "asr": "视频语音识别文本",
        "metadata": "仅标题/简介等元信息",
        "mixed": "字幕与元信息合并",
        # 来源写「正文文本」而不是「网页文章正文（由 HTML 抽取）」：
        # 本地 PDF / Word / PPT 走的是同一个 article 来源，
        # 按原文案会把 PDF 说成网页，可能把模型带偏。
        "article": "正文文本（文章 / 文档抽取）",
        "plain": "用户提供的纯文本或本地文档正文",
    }.get(transcript.source, "原文文本")

    # 文章类内容没有「视频」这个概念，提示词要跟着变 ——
    # 否则会让模型去找「视频里讲了什么」，而输入其实是长文。
    if transcript.source in ("article", "plain"):
        role_desc = "你正在总结一篇**文章/图文内容**（不是视频）。"
        extra_req = (
            "6. 文章类内容重点关注：作者的核心论点、支撑论据、"
            "给出的结论或建议；不要写「视频中提到」这类措辞。\n"
        )
    else:
        role_desc = "你是一个专业的视频内容分析助手，擅长把冗长的视频文本压缩成高信息密度的中文摘要。\n"
        extra_req = ""

    style_instruction = {
        STYLE_BRIEF: "输出尽量精简，一句话讲清这个视频在说什么，再给 3-5 条要点。",
        STYLE_DETAILED: "先一句话概括，再给 3-6 条核心要点，然后按内容推进分段细讲，最后给出结论/建议。",
        STYLE_BULLETS: "只输出分层的 Markdown 要点清单，不要长段落。",
    }.get(style, "")

    system = (
        role_desc +
        "要求：\n"
        "1. 只依据给定内容，不臆造、不补充外部知识；\n"
        "2. 用简体中文，Markdown 格式，条理清晰；\n"
        "3. 数字、人名、产品名、时间等关键信息必须保留；\n"
        "4. 不要出现「根据视频内容」这类套话，直接给结论。\n"
        + extra_req
    )

    # 标题随内容类型变：文章写成「文章总结」，否则读起来很别扭
    doc_title = "文章总结" if transcript.source in ("article", "plain") else "视频总结"
    fmt = [f"# {doc_title}", "", "## 一句话总结", "（一句话）", "", "## 核心要点",
           "- （3-6 条，每条 1 行）"]
    if style == STYLE_DETAILED:
        if has_ts:
            fmt += ["", "## 分段细讲（带时间戳）",
                    "- [00:00] （按内容推进，每段 1-2 句）"]
        else:
            # 这里**不能**再沿用 `- [00:00]` 那个示例行：模型会照抄前缀，
            # 结果文档（PDF/Excel/文章）的分段细讲每行都顶着假的 [00:00]。
            # 显式写「不要加时间戳」，比只把示例删掉更稳。
            fmt += ["", "## 分段细讲",
                    "- （按内容顺序，每段 1-2 句；**不要加时间戳**）"]
        fmt += ["", "## 结论与建议", "- （如果有）"]
    if with_keywords:
        fmt += ["", "## 关键词", "标签1 / 标签2 / 标签3（5-8 个）"]

    user = (
        f"{meta_block}\n\n以下是{src_desc}"
        f"{'（含时间戳）' if has_ts else ''}：\n\n{body}\n\n"
        f"请按下面的模板输出，{style_instruction}\n\n"
        + "\n".join(fmt)
    )
    if transcript.source == "metadata":
        user += ("\n\n注意：当前只拿到了元信息，没有视频正文。"
                 "请明确说明结论是基于标题/简介推断的，不要编造正文细节。")
    return system, user


class LlmEngine:
    """大模型引擎基类。"""

    key: str = ""
    name: str = ""
    note: str = ""

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def summarize(self, meta: VideoMeta, transcript: Transcript,
                  on_token: Optional[Callable[[str], None]] = None) -> str:
        """返回 Markdown 摘要文本。"""
        raise NotImplementedError

    # ---------------- 通用对话（供筛选/重写等能力复用） ----------------
    def chat(self, system: str, user: str,
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None) -> str:
        """一次普通的非流式对话。

        摘要之外的 AI 能力（内容筛选、笔记重写、打标签）都走这里，
        好处是配置、代理、错误处理只有一份实现 —— 新增能力不用再抄一遍 HTTP 代码。
        """
        raise NotImplementedError

