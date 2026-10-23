"""把视频总结重写成面向 AI 检索的结构化笔记。

为什么不直接把摘要复制进去
--------------------------
给 AI 做 RAG 时，一堆格式雷同的长文会让切分器切出语义不完整的碎片，召回质量很差。
这里按检索友好重排，核心是四点：

1. **开头就是一段自足摘要** —— 被召回时只看这一段就能判断相关性，不必读全文；
2. **专有名词密集** —— 把 ``LangGraph``、``RAG`` 这些检索锚点提到显眼位置；
3. **小节标题是语义完整的短句** —— 切分器靠标题切块，标题质量直接决定块质量；
4. **关键结论带出处时间戳** —— AI 引用时可以核对，不容易编。

另外会自动补 ``[[双链]]``，把新笔记接到 vault 里已有的同主题笔记上 ——
这是把「一堆文件」变成「知识网络」的关键一步。
"""

from __future__ import annotations

import os
import re
from typing import Optional

from ..core.models import SummaryResult
from . import frontmatter
from .curator import Judgement, TIER_HIGH, TIER_MID

#: 生成笔记正文时用的提示词
_SYSTEM = """你是一个把视频内容整理成可检索知识笔记的助手。

产出的笔记未来会被搜索引擎/AI 检索系统读取，用来回答问题。所以要按「能被准确
检索和准确引用」的标准来写，而不是按「读起来舒服」的标准。

硬性要求：
1. **开头必须有一段自足摘要**（3-5 句），只看这段就能判断内容是否相关，
   不依赖后文、不出现「如前所述」「上面提到的」这类指代。
2. **专有名词保持原样**，不要翻译。代码里的标识符、库名、协议名一律照抄。
3. **每个结论尽量带出处时间戳**，格式 `[MM:SS]`，方便回溯核对。
4. **不要编造**。原文没有的数字、名称、结论一律不写。不确定的地方标注「（存疑）」。
5. 用简体中文，Markdown 格式，小节标题是完整语义短句（不要「概述」这种空标题）。
6. **结论要写成可直接执行的规则或事实，不要写成故事**。这条最容易做错：
   - 不要写「我一开始用 splitext 剥扩展名，把 .spec 也剥掉了」
   - 要写「os.path.splitext() 只按最后一个点切分，对多级扩展名会误判；
     判 md 文件名应用 f[:-3] if f.endswith('.md') else f」
   理由是笔记被召回时，读者需要的是规则本身，不是产生规则的 anecdote。

按下面的结构输出，不要输出其他内容：

# <视频标题>

<3-5 句自足摘要，写清讲了什么、结论是什么、适用场景>

## 核心结论
- 每条结论一句话，句末带 `[MM:SS]` 时间戳

## 关键数据与事实
- 具体数字、版本、参数、步骤。没有就写「无可提取的具体数据」
  （命令和配置项单独列在下一节）

## 可执行操作
- 可以直接照做的步骤、命令、配置。没有就写「（本视频无具体操作步骤）」

## 关键概念
- **概念名**：一句话解释，不使用该词本身来解释它。
  没有需要解释的术语就写「（无特殊术语）」

## 延伸信息
- 提到的工具/书籍/课程/人名/其他视频。没有就写「（无）」

如果内容里出现「需要注意」「坑」「前提」这类信息，单独加一节：
## 注意事项
- 风险、限制、适用前提"""


def build_user_prompt(result: SummaryResult, j: Judgement) -> str:
    meta = result.meta
    parts = [
        f"标题：{meta.title}",
        f"作者：{meta.author or '未知'}",
        f"平台：{meta.platform_name}",
    ]
    if meta.duration:
        parts.append(f"时长：{meta.duration} 秒")

    digest = (result.markdown or "").strip()[:7000]

    # 把筛选结果喂进去，省得模型重新判断一遍，也让两段输出保持一致
    hints = []
    if j.topics:
        hints.append("主题标签：" + "、".join(j.topics))
    if j.entities:
        hints.append("已识别的专有名词：" + "、".join(j.entities))
    if j.terms:
        hints.append("需要解释的术语：" + "、".join(j.terms))
    if j.actionable:
        hints.append("已识别的可执行动作：" + "；".join(j.actionable))
    if j.reason:
        hints.append(f"筛选判断：{j.reason}")

    return (
        "\n".join(parts) + "\n\n"
        "以下是视频内容的摘要（含时间戳）：\n\n" + digest + "\n\n"
        + ("筛选阶段的信息（请与之保持一致，不要另起一套说法）：\n"
           + "\n".join("  - " + h for h in hints) + "\n\n" if hints else "")
        + "请按要求的结构输出整理后的笔记。"
    )


def rewrite(result: SummaryResult, j: Judgement, engine) -> str:
    """调用大模型重写笔记正文。失败时返回空串，由调用方降级。"""
    return engine.chat(_SYSTEM, build_user_prompt(result, j),
                       temperature=0.4, max_tokens=3000).strip()


# ---------------------------------------------------------------- 降级构造

def _strip_title(md: str) -> str:
    """去掉重写结果里的一级标题（frontmatter 之后正文自带 h1 就够了）。"""
    lines = (md or "").splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        return "\n".join(lines[1:]).strip()
    return (md or "").strip()


def fallback_note(result: SummaryResult, j: Judgement) -> str:
    """重写失败时的保守版本：直接用摘要，但不丢任何信息。"""
    meta = result.meta
    head = [
        f"# {meta.title}",
        "",
        "> 本笔记由程序在「AI 重写失败」时自动降级生成，内容为视频摘要原文，"
        "未做结构化整理。信息完整但格式不利于检索，可手动整理。",
        "",
    ]
    if j.entities:
        head += ["**涉及概念**：" + " · ".join(j.entities), ""]
    body = _strip_title(result.markdown or "")
    if not body:
        body = (result.transcript.text or "")[:1500] or "（无正文内容）"
    # body 可能以 "## 核心结论" 这类标题紧接上来，跟上面的「涉及概念」粘在一块。
    # head 末尾那个 "" 只够断行，这里补一个空行让 Markdown 分成两段。
    out = "\n".join(head)
    if not out.endswith("\n\n"):
        out += "\n"
    return out + body.lstrip("\n")


# ---------------------------------------------------------------- 双链

def _existing_titles(vault: str, subfolder: str = "", limit: int = 600) -> set[str]:
    """扫 vault 里已有的笔记标题（不含本篇），用来建立双链。

    递归遍历整个 vault —— PARA 结构下笔记分散在多个目录，只看一层
    会漏掉大部分已有笔记，双链也就连不起来。
    """
    titles: set[str] = set()
    skip = {".obsidian", ".git", ".trash", "node_modules", "90-附件"}
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for name in files:
            if name.endswith(".md") and not name.startswith("."):
                titles.add(name[:-3].strip())
            if len(titles) >= limit:
                return titles
    return titles


def link_existing(body: str, entities: list[str], topics: list[str],
                  existing: set[str]) -> str:
    """把与已有笔记对应的实体/主题改成 ``[[双链]]``。

    只在 vault 里确实存在对应笔记时才加链接 —— 指向不存在页面的链接
    在 Obsidian 里会显示成红色空页，反而污染图谱。

    候选词 = 实体 ∪ 主题 ∪ **库里已有的标题**。
    最后一组很关键：AI 未必会把「提示词使用指南」这种跨笔记概念
    提取成实体，但只要正文提到了、库里也确实有这篇，就该链上 ——
    双链网络的价值恰恰在于跨笔记的横向连接。
    """
    if not existing:
        return body
    index = _title_index(existing)
    out = body
    linked: set[str] = set()

    def _replace(term: str):
        target = index.get(term.strip())
        if not target or target in linked:
            return term
        linked.add(target)
        return f"[[{target}]]"

    # 库里已有的标题（去掉日期前缀的短标题形式）也作为候选。
    # 但不是全部 —— 「求职行动方案」这种专有标题该链，「读书」这种谁都可能的词
    # 链了就是噪音。所以要求 4 字以上，或含英文/数字（术语通常带这些特征）。
    from_vault = []
    for full in existing:
        bare = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", full).strip()
        if len(bare) >= 4 and (len(bare) >= 6 or re.search(r"[A-Za-z0-9]", bare)):
            from_vault.append(bare)
    candidates = set(entities) | set(topics) | set(from_vault)

    # 长词优先：先链 "向量检索" 再链 "检索"，避免后者抢先匹配到词的一部分
    for term in sorted(candidates, key=len, reverse=True):
        if len(term) < 3 or term in _GENERIC_TITLES:
            continue
        pattern = re.escape(term)
        out = re.sub(rf"(?<![\w\u4e00-\u9fff]){pattern}(?![\w\u4e00-\u9fff])",
                     lambda m, t=term: _replace(t), out, count=3)
    return out


#: 太泛的标题，见谁链谁会把笔记网搅乱。
#: 这些词往往正是 MOC/索引页的名字，链过去等于没链。
_GENERIC_TITLES = {
    "笔记", "日志", "索引", "首页", "导航", "导航首页", "知识库", "知识库导航首页",
    "术语表", "MOC", "moc", "待阅", "精选", "收集箱", "收件箱", "附件",
    "项目", "方法", "日志笔记", "视频笔记", "视频笔记索引",
}


def _title_index(existing: set[str]) -> dict[str, str]:
    """把「去掉日期前缀的标题」映射回**完整文件名**。

    用户的笔记都叫 ``2026-10-05 xxx.md``，而实体名是 ``xxx`` 这种短词，
    直接拿短词去匹配文件名永远对不上，所以要建这个映射。
    """
    idx: dict[str, str] = {}
    for full in existing:
        bare = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", full).strip()
        # 长的和短的都登记，短词匹配不到时还能回退
        idx.setdefault(bare, full)
        idx.setdefault(full, full)
    return idx


# ---------------------------------------------------------------- 组装

def build_note(result: SummaryResult, j: Judgement, body: str,
               vault: str = "", subfolder: str = "",
               date_prefix: bool = True) -> str:
    """把 frontmatter、正文、双链拼成最终的 Markdown 内容。

    ``date_prefix`` 会给文件名加 ``YYYY-MM-DD`` 前缀 —— 用户的知识库
    （PARA 结构）里所有笔记都遵循这个约定，新笔记应当保持一致。
    """
    from datetime import datetime

    meta = result.meta
    today = datetime.now().strftime("%Y-%m-%d")

    # 先把正文里的实体改成双链，**必须在拼 YAML 头之前**完成 ——
    # "[[" 是 YAML 的特殊语法，一旦混进 frontmatter，整个头会解析失败。
    content = (body or "").strip()
    if vault and (j.entities or j.topics):
        existing = _existing_titles(vault, subfolder)
        if existing:
            content = link_existing(content, j.entities, j.topics, existing)

    # 标签：主题 + 平台 + 档位。Obsidian 用嵌套标签便于左侧栏分组
    tags = ["视频总结"]
    tags += [t.strip("[]【】") for t in j.topics if t.strip()]
    tags += [meta.platform_name]
    tags.append({"精选": "精选", "待定": "待阅", "丢弃": "丢弃"}[j.tier])

    # 字段命名对齐用户现有笔记：type / category / created / tags
    # 没拿到就不写 —— author: "" 这种空字段只会让 YAML 头变脏。
    fields = {
        "type": "video-note",
        "category": "视频笔记",
        "title": meta.title,
        "source": meta.url,
        "platform": meta.platform_name,
        "author": meta.author,
        "duration": fmt_duration(meta.duration) if meta.duration else None,
        "created": today,
        "value": j.score,
        "tier": j.tier,
        "tags": tags,
    }
    if j.reason:
        fields["value_reason"] = j.reason
    if j.entities:
        # 只放纯文本实体，不放 [[链接]]，否则 YAML 头解析不了
        fields["entities"] = j.entities
    if j.topics:
        fields["topics"] = j.topics

    text = frontmatter.dump(fields)
    text += "\n" + content + "\n"

    # 原文出处放在最后，让检索时优先命中上面的结构化内容
    text += "\n---\n\n## 出处\n\n"
    text += f"- 链接：{meta.url}\n"
    text += f"- 平台：{meta.platform_name}"
    text += f" ｜ 作者：{meta.author}\n" if meta.author else "\n"
    text += f"- 时长：{fmt_duration(meta.duration)}\n"
    text += f"- 文本来源：{_source_label(result)}\n"
    text += f"- 入库时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    text += f"- 价值评分：{j.score} / 100（{j.tier}）"
    text += f" —— {j.reason}\n" if j.reason else "\n"
    if j.note:
        text += f"- 备注：{j.note}\n"
    return text


def note_stem(title: str, date_prefix: bool = True) -> str:
    """生成笔记文件名（不含扩展名）。

    默认加日期前缀，与用户库里的命名约定一致；``safe_note_name`` 负责清掉非法字符。
    """
    from datetime import datetime

    base = safe_name(title)
    if not date_prefix:
        return base
    return f"{datetime.now().strftime('%Y-%m-%d')} {base}"


def safe_name(title: str) -> str:
    """转发到 vault.safe_note_name，避免循环导入时的符号缺失。"""
    from .vault import safe_note_name

    return safe_note_name(title)


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "未知"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _source_label(result: SummaryResult) -> str:
    t = result.transcript
    names = {"subtitle": "官方字幕", "asr": "语音识别",
             "metadata": "仅元信息", "mixed": "多来源"}
    base = names.get(t.source, t.source)
    return f"{base}（{t.engine}）" if t.engine else base
