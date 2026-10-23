"""问答编排 —— RAG 的最后一公里。

这一步的风险不在「能不能答」，而在「会不会编」。检索总会返回几条东西
（哪怕都不相关），模型拿到就倾向于硬答 —— 用户看到的是流畅的一段话，
实际上没有任何依据。所以提示词里的约束比技术实现更关键，四条规则一条都不能省：

1. **只用给定片段**，不许引入片段外的知识；
2. **每个结论标编号**，让用户能自己点回去核验；
3. **找不到就说找不到**，明确给出「没找到」这个选项，否则模型没有退路；
4. **矛盾要指出来**，不要擅自选一个当作事实。

另外，大模型不可用时不做生成，直接把检索到的原文片段列出来 ——
这在「只有片段、没有答案」和「编一个像样的答案」之间，选前者。
"""

from __future__ import annotations

from typing import Optional

from ..core.models import fmt_hms
from ..core.registry import all_llm
from .base import Hit
from .retriever import Retriever, build_embedder, build_store

#: 拼进提示词的片段上限（字符）。检索回来的块可能很多，全塞进去既贵又会让
#: 模型注意力被稀释；留下最相关的若干条即可。
MAX_CONTEXT_CHARS = 6000


def build_prompt(question: str, hits: list[Hit]) -> tuple[str, str]:
    """构造 ``(system, user)``。单独抽出来是为了能被测试直接断言。"""
    system = (
        "你是一个资料库问答助手。你只能依据下面提供的「资料片段」回答问题，"
        "不得使用片段以外的知识，也不得根据常识补全。\n"
        "必须遵守：\n"
        "1. 每个结论后面用 [编号] 标注依据来自哪个片段，例如「…[2]」。\n"
        "2. 如果片段里找不到答案，直接回答「资料库中没有相关内容」，"
        "并说明还缺什么信息。不要猜测，不要编造。\n"
        "3. 如果片段之间说法冲突，把冲突指出来，不要擅自选一个当成结论。\n"
        "4. 用中文回答，直接给结论，不要复述问题，不要写「根据提供的资料」这类开场白。"
    )

    lines: list[str] = []
    used = 0
    for i, h in enumerate(hits, 1):
        c = h.chunk
        where = f"《{c.title}》" if c.title else (c.source_id or "未知来源")
        if c.has_time:
            where += f" {fmt_hms(c.start)}"
        block = f"[{i}] {where}\n{c.text}"
        if used + len(block) > MAX_CONTEXT_CHARS and lines:
            break
        lines.append(block)
        used += len(block) + 2

    context = "\n\n".join(lines) if lines else "（没有检索到任何片段）"
    user = f"问题：{question}\n\n资料片段：\n\n{context}"
    return system, user


def ask(ctx, question: str, top_k: int = 5, min_score: float = 0.0,
        retriever: Optional[Retriever] = None) -> dict:
    """对语料库提问。**任何情况下都返回结果**，不抛异常。

    返回字段：

    ==========  ==================================================
    answer      回答正文（可能是模型生成，也可能是原文片段直列）
    hits        检索到的片段（含出处与分数）
    mode        检索方式：vector / keyword / mixed / none
    engine      生成引擎名；``extractive`` 表示未调用大模型
    degraded    降级说明，正常时为空串
    ==========  ==================================================
    """
    ret = retriever if retriever is not None else Retriever(ctx)
    hits, mode = ret.search(question, top_k=top_k, min_score=min_score)

    result = {
        "question": question,
        "answer": "",
        "hits": [h.to_dict() for h in hits],
        "mode": mode,
        "engine": "",
        "degraded": "",
    }

    # 向量那一路没生效时必须说出来。否则用户看到的是「答得不准」，
    # 会去怀疑语料质量，而真正的原因往往是 Key 没配或库与模型维度对不上。
    if mode != "vector" and getattr(ret, "last_error", ""):
        result["degraded"] = (f"向量检索未生效（{ret.last_error}），"
                              f"本次结果来自关键词匹配")

    if not hits:
        result["answer"] = ("资料库中没有检索到相关内容。"
                            "可以先确认这个库是否已经灌入语料（看 --rag-list）。")
        result["engine"] = "none"
        return result

    system, user = build_prompt(question, hits)
    llm = _pick_llm(ctx)
    if llm is None:
        # 没有可用的大模型：不做生成，直接给原文。宁可少给答案，不给假答案。
        result["answer"] = "（未配置可用的大模型，下面是库中最相关的原文片段）\n\n" + \
                           _format_hits(hits)
        result["engine"] = "extractive"
        result["degraded"] = _join_note(result["degraded"], "大模型不可用，已退化为原文摘录")
        return result

    engine, instance = llm
    try:
        answer = instance.chat(system, user, temperature=0.1)
    except Exception as exc:
        ctx.log.warning("RAG 生成失败：%s", exc)
        result["answer"] = f"（大模型调用失败：{exc}）\n\n下面是检索到的原文片段：\n\n" + \
                           _format_hits(hits)
        result["engine"] = "extractive"
        result["degraded"] = _join_note(result["degraded"], f"大模型调用失败：{exc}")
        return result

    result["answer"] = (answer or "").strip() or "（模型没有返回内容）"
    result["engine"] = engine
    return result


def _pick_llm(ctx):
    """挑一个可用的大模型引擎，返回 ``(名字, 实例)``；都不可用则 None。"""
    for key in ("openai-compat",):
        cls = all_llm().get(key)
        if not cls:
            continue
        inst = cls(ctx)
        ok, reason = inst.available()
        if not ok:
            ctx.log.info("RAG 跳过引擎 %s：%s", key, reason)
            continue
        return inst.name, inst
    return None


def _format_hits(hits: list[Hit]) -> str:
    out = []
    for i, h in enumerate(hits, 1):
        c = h.chunk
        where = f"《{c.title}》" if c.title else (c.source_id or "未知来源")
        if c.has_time:
            where += f" {fmt_hms(c.start)}"
        out.append(f"[{i}] {where}　相关度 {h.score:.3f}\n{c.text}")
    return "\n\n".join(out)


def _join_note(existing: str, note: str) -> str:
    """降级说明可能不止一条（检索降级 + 生成降级），用分号连起来而不是覆盖。"""
    return f"{existing}；{note}" if existing else note


__all__ = ["ask", "build_prompt", "MAX_CONTEXT_CHARS"]
