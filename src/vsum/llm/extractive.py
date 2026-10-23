"""本地抽取式摘要 —— **零配置兜底**。

没有 API Key、没有网络、没有 GPU 时，它依然能产出一份结构完整的总结。
思路是经典的抽取式摘要（不是生成式）：给每个句子打分，挑最有信息量的若干句，
再按时间顺序组织成「要点 + 时间线 + 关键词」。

质量显然不如大模型，但胜在：永远有输出、永远不花钱、完全离线。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable, Optional

from ..core.errors import LLMError
from ..core.models import Transcript, VideoMeta, fmt_hms
from ..core.registry import register_llm
from .base import LlmEngine

#: 中文常见停用词（够用即可，不做语言学严谨性保证）
STOPWORDS = set("""
的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这
那 我们 他们 什么 怎么 因为 所以 但是 然后 这个 那个 可以 就是 还是 这样 那样 一下 一些
还是 其实 现在 已经 可能 应该 觉得 知道 如果 虽然 而且 于是 并且 或者 对于 通过 关于
一个 一样 一直 一定 这些 那些 之后 之前 里面 出来 起来 过来 过去 大家 这样子 有点 有点儿
啊 吧 呢 吗 哦 嗯 哈 嘿 呀 啦 嘛 唔 呗 咯 惹 呐 呵 哎 唉
video 视频 分享 点赞 关注 评论 收藏 转发 抖音 bilibili 快手 up 主 up主
""".split())

#: 英文高频虚词，避免把 "and / the" 之类当成关键词
EN_STOPWORDS = set("""
the a an and or of to in is it that this for on with as at be was are were you we they
my his her its from by not but if then than so very can will just do does did have has had
what when who how why all more most other some such only own same too don now new like get
got make made one two also about after before over under out up down off into via
""".split())

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])|\s{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9+#.\-]{1,}")
_URLISH = re.compile(r"https?://|www\.|\.(com|cn|net|org|io)", re.I)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{2,}")

#: 几乎不可能作为词首/词尾的虚词字。用来砍掉跨词边界的 n-gram 碎片
#: （"应用层的"、"的爆发" 这类），代价是少数以这些字开头的实词会被漏掉。
EDGE_STOP = set("的了是在和也就都而与其这那被把让给对从向有着过还很更最我你他她它们个")


def _tokens(text: str) -> list[str]:
    """分词：中文 2~3 字词组 / 英文单词，并剔除停用词与链接残片。

    没有引入分词词典（不额外拖依赖），所以对连续中文串用滑动窗口取 2-gram / 3-gram：
    ASR 出来的文本常常整段没有标点，直接整串当一个词会产出几十字的"关键词"，
    切成 n-gram 后靠词频自然浮现真实的高频词。
    """
    out = []
    for t in _TOKEN.findall(text or ""):
        if _URLISH.search(t):
            continue
        if not _CJK.search(t):
            low = t.lower()
            if low in STOPWORDS or low in EN_STOPWORDS:
                continue
            if len(t) < 3:                        # 太短的英文词没有意义
                continue
            out.append(t)
            continue
        # 连续中文串：切成 2-gram 与 3-gram
        for n in (2, 3):
            for i in range(len(t) - n + 1):
                g = t[i:i + n]
                if g.lower() not in STOPWORDS:
                    out.append(g)
    return out


def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_SPLIT.split(text or "") if s and s.strip()]
    # 过长的句子再按逗号切一刀，避免整段被当成一句
    out = []
    for p in parts:
        if len(p) > 120:
            out.extend(x.strip() for x in re.split(r"[，,]", p) if x.strip())
        else:
            out.append(p)
    return out




def _score(sents: list[str], title: str) -> list[float]:
    """综合打分：词频 + 位置 + 与标题的相关度 - 长度惩罚。"""
    if not sents:
        return []
    freq = Counter()
    for s in sents:
        for t in set(_tokens(s)):      # 句内重复只算一次（近似 TF-IDF 的 TF 部分）
            freq[t] += 1
    max_freq = max(freq.values()) if freq else 1
    title_toks = set(_tokens(title or ""))

    scores = []
    n = len(sents)
    for i, s in enumerate(sents):
        toks = _tokens(s)
        if not toks:
            scores.append(0.0)
            continue
        tf = sum(freq[t] / max_freq for t in set(toks)) / max(1, len(set(toks)))
        pos = 1.0 - (i / max(1, n)) * 0.6          # 越靠前越重要（开头常点题）
        rel = len(set(toks) & title_toks) / max(1, len(set(toks)))
        # 长度：太短没信息，太长啰嗦，20~80 字最佳
        L = len(s)
        length = math.exp(-((L - 45) ** 2) / (2 * 35 ** 2))
        scores.append(tf * 1.0 + pos * 0.35 + rel * 0.8 + length * 0.5)
    return scores


def keywords(text: str, top_k: int = 8) -> list[str]:
    """高频词/词组作为关键词。

    不依赖分词词典：直接从连续中文串统计 2/3/4-gram，按「频次 × 长度加成」打分
    （同样出现 3 次时，"推理成本" 比 "推理" 更值得当关键词），
    再剔除与已选词互相包含的碎片，避免输出一堆同义重叠项。
    """
    text = text or ""
    counts: Counter = Counter()
    cjk: set[str] = set()
    nb: dict[str, int] = {}                 # 左右邻字的多样性，用来判断是不是跨词碎片
    for run in _CJK_RUN.findall(text):
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if g in STOPWORDS:
                    continue
                counts[g] += 1
                cjk.add(g)
                s = nb.setdefault(g, set())
                s.add(run[i - 1] if i > 0 else "\x00")
                s.add(run[i + n] if i + n < len(run) else "\x00")
    for w in _EN_WORD.findall(text):
        low = w.lower()
        if low in STOPWORDS or low in EN_STOPWORDS or _URLISH.search(w):
            continue
        counts[w] += 1
    if not counts:
        return []

    def _ok(p: str) -> bool:
        """砍掉跨词边界的 n-gram。

        判据两条：①首尾是虚词字（"应用层的"、"的爆发"）；
        ②左右邻字太单调（"用层的爆" 永远只出现在"应...发"之间，说明它是词的错位切片）。
        """
        if p[0] in EDGE_STOP or p[-1] in EDGE_STOP:
            return False
        if p not in cjk:                    # 英文词不做邻接判断
            return True
        need = 2 if len(p) <= 2 else (3 if len(p) == 3 else 4)
        return len(nb.get(p, ())) >= need

    scored = sorted(
        ((c * (1 + 0.25 * (len(p) - 2)), p)
         for p, c in counts.items() if c >= 2 and _ok(p)),
        reverse=True,
    )
    def _grams(s: str) -> set:
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s}

    picked: list[str] = []
    picked_g: list[set] = []
    for _, phrase in scored[:top_k * 10]:
        g = _grams(phrase)
        # 与已选词高度重叠（如"应用层的"与"层的爆发"）就丢掉，避免输出同义碎片
        if any(len(g & pg) / max(1, min(len(g), len(pg))) > 0.5 for pg in picked_g):
            continue
        picked.append(phrase)
        picked_g.append(g)
        if len(picked) >= top_k:
            break
    return picked


def _timeline(transcript: Transcript, top_sent_idx: set, per_seconds: int = 60) -> list[str]:
    """按时间段把命中的句子组织成时间线。"""
    if not transcript.segments:
        return []
    buckets: dict[int, list[str]] = {}
    for i, seg in enumerate(transcript.segments):
        st = seg.start if hasattr(seg, "start") else (seg.get("start") or 0)
        tx = seg.text if hasattr(seg, "text") else (seg.get("text") or "")
        b = int(st // per_seconds)
        buckets.setdefault(b, []).append(tx)
    lines = []
    for b in sorted(buckets)[:24]:           # 最多 24 段，避免超长
        text = " ".join(buckets[b]).strip()
        if not text:
            continue
        text = re.sub(r"\s+", " ", text)
        if len(text) > 90:
            text = text[:90] + "…"
        lines.append(f"- [{fmt_hms(b * per_seconds)}] {text}")
    return lines


@register_llm("extractive")
class ExtractiveEngine(LlmEngine):
    """本地抽取式摘要（零配置兜底）。"""

    key = "extractive"
    name = "本地抽取式摘要（无需 API Key）"
    note = "完全离线，质量低于大模型，但保证有输出"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def summarize(self, meta: VideoMeta, transcript: Transcript,
                  on_token: Optional[Callable[[str], None]] = None) -> str:
        cfg = self.ctx.config
        style = cfg.get("summary.style", "detailed")
        text = transcript.text or ""

        body_text = text
        if transcript.source == "metadata":
            # 元信息模式：直接用标题+简介，不做句子抽取
            sents = _sentences(meta.description or meta.title) or [meta.title]
        else:
            sents = _sentences(body_text)

        scores = _score(sents, meta.title)
        order = sorted(range(len(sents)), key=lambda i: scores[i], reverse=True)
        keep_n = {"brief": 3, "bullets": 6, "detailed": 7}.get(style, 6)
        top_idx = sorted(order[:keep_n])

        # 含链接的句子（"官网：https://…"）几乎不会是好摘要，往后排
        ranked = [i for i in top_idx if not _URLISH.search(sents[i])] or list(top_idx)

        lead = sents[ranked[0]] if ranked else (meta.title or "（内容不足）")
        if len(lead) > 120:
            lead = lead[:120] + "…"

        bullets = [f"- {sents[i]}" for i in ranked[1:] if i < len(sents)]
        if not bullets and meta.description:
            bullets = [f"- {meta.description[:120]}"]

        kws = keywords(body_text or meta.description or meta.title)

        # 标题按来源措辞：# 视频总结 / # 文章总结。文档（PDF/Word/PPT/EPUB）
        # 也走 article 来源，不该被叫成「视频总结」。
        doc_kind = "文章总结" if transcript.source in ("article", "plain") else "视频总结"
        parts = [f"# {doc_kind}", "",
                 "> 本总结由本地抽取式算法生成（未调用大模型），"
                 "仅按原文抽取关键句，未做改写。", "",
                 "## 一句话总结", lead, ""]

        meta_line = " / ".join(filter(None, [
            f"平台：{meta.platform_name}",
            f"作者：{meta.author}" if meta.author else "",
            f"时长：{meta.duration} 秒" if meta.duration else "",
        ]))
        if meta_line:
            parts += [f"> {meta_line}", ""]

        if style == "bullets":
            parts += ["## 要点清单"] + (bullets or ["- （内容不足）"])
        elif style == "brief":
            parts += ["## 核心要点"] + (bullets[:4] or ["- （内容不足）"])
        else:
            parts += ["## 核心要点"] + (bullets or ["- （内容不足）"])
            # 只有**真**时间轴才输出「时间线概览」：文章的段落时间是按字数
            # 估的，列出来全是 [00:00]，不如不列（见 Transcript.has_timeline）
            if transcript.has_timeline:
                tl = _timeline(transcript, set(top_idx))
                if tl:
                    parts += ["", "## 时间线概览"] + tl

        if kws and cfg.get("summary.with_keywords", True):
            parts += ["", "## 关键词", " / ".join(kws)]

        if transcript.notes:
            parts += ["", "> 提示：" + " ".join(transcript.notes)]

        md = "\n".join(parts)
        if on_token:
            # 逐段吐出，让界面也有「流式」体验
            for ch in md:
                on_token(ch)
        return md

    def chat(self, system: str, user: str,
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None) -> str:
        """本地引擎没有对话能力。

        调用方（内容筛选、笔记重写）应该先检查 ``available()`` 并准备降级路径，
        走到这里说明调用方漏了判断，这里抛错比静默返回空串更容易发现问题。
        """
        raise LLMError(
            "本地抽取式引擎不支持对话",
            hint="筛选与重写需要真实大模型：设置 → 大模型 里配置 API Key，"
                 "或把 obsidian.auto_save 关掉改为手动触发")

