"""知识库索引与术语表 —— 为「面向 AI」额外生成的两份文件。

为什么需要这两份
----------------
把一堆笔记丢进 vault 只是第一步。要真正服务 AI 检索，还需要：

**MAP 索引笔记**
    一份「这个库有什么、每个主题在哪」的地图。RAG 检索时如果能把地图也召回，
    模型就知道该去哪个文件找，召回质量明显提升。

**术语表**
    专业术语的速查表。用户此前反复追问 tiny/ASR/VAD 这类概念，说明这是真实的
    知识缺口；沉淀成笔记后，AI 检索到这些术语时能直接引用规范解释。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

from . import frontmatter
from .vault import write_note, safe_note_name

MAP_NAME = "视频笔记索引"
GLOSSARY_NAME = "术语表"

#: 术语表模板。用户遇到生词时会来查，同时也是 AI 的检索目标。
_GLOSSARY_TEMPLATE = """---
type: moc
category: "索引"
tags: [MOC, 术语表, 信息输入]
created: {added}
---

# 术语表

> 本库专有名词速查。检索这些词时以这里的解释为准，避免同名词混淆。
> 由 VideoSummarizer 自动生成，可直接编辑补充。

## 平台与格式
- **BV 号**：B 站视频的永久标识（`BV1xxxxxxxxxx`），比短链稳定，可直接拼出视频页
- **Share Token / 分享口令**：抖音、小红书等 App 复制的整段文案里含短链码，需先提取真链接
- **wbi 签名**：B 站 Web 接口的请求签名算法（`w_rid`），靠 img_key + sub_key + 固定置换表算出

## 语音识别
- **ASR**：Automatic Speech Recognition，把语音转成文字
- **Whisper**：OpenAI 开源的语音识别模型，离线可用
- **faster-whisper**：Whisper 的 CTranslate2 重写版，速度提升约 4 倍、内存更省
- **VAD**：Voice Activity Detection，语音活动检测。先切掉静音再送 ASR，既快又准
- **compute_type (int8)**：量化精度。int8 是精度与体积的折中，CPU 上首选
- **时间戳**：字幕/转写里每句话的起止秒数，用于回溯定位原视频

## 内容获取
- **Cookie / did**：站点识别你身份的凭据。缺 Cookie 时抖音、快手、小红书常降级为只取标题
- **风控**：平台对高频请求的拦截。表现为拿不到数据或返回验证页
- **风控页 / 验证码中间页**：平台拦截时返回的占位页。特征是**标题有内容但内容是验证提示**（如「验证码中间页」），所以不能靠「标题是否为空」判断
- **未登录 IP 风控**：国内平台按出口 IP 判定身份。没登录过的 IP 常被直接拦到验证页 —— 这是平台硬性要求，不是程序故障
- **M3U8**：流媒体分片格式。平台常用它做防盗链，下载需要逐片合并
- **yt-dlp**：支持上千站点的下载解析器，本程序作为通用兜底

## AI 与检索
- **Embedding**：把文本转成向量表示，语义相近的文本向量距离近，是检索的基础
- **RAG**：Retrieval-Augmented Generation，检索增强生成。先召回资料再让模型基于资料回答
- **Chunk 切分**：把长文切成小块再做索引。切分质量直接决定召回质量
- **top-k**：召回条数上限。k 太大引入噪声，太小漏掉答案
- **专实体**：领域专有名词（库名、协议名、参数名）。它们是语义检索最有效的锚点
- **MOC**：Map of Content，内容索引。一份指向其他笔记的导航页

## 摘要
- **抽取式摘要**：从原文里挑高信息密度的句子拼成摘要，不改写。零依赖、可离线
- **生成式摘要**：让大模型重写。更连贯但需要 API Key
- **Frontmatter**：笔记开头的 YAML 头，让 Obsidian 把笔记当结构化记录查询
"""

_MAP_TEMPLATE = r"""---
type: moc
category: "索引"
tags: [MOC, 视频总结, 索引]
created: {added}
---

# 视频笔记索引

> 本页汇总库内 **{count}** 篇视频笔记（由 VideoSummarizer 自动生成，可手动编辑）。
> 检索视频内容时先看这里定位，再去对应笔记取细节。

想要按条件实时查询，可在 Obsidian 里装 Dataview 插件后写：

```dataview
LIST FROM #视频总结 AND #精选 SORT file.mtime DESC
```

## 按主题
{topic_section}
## 按平台
{platform_section}
## 最新入库
{recent_section}
"""


def ensure_glossary(vault: str, subfolder: str = "") -> Optional[str]:
    """确保术语表存在，不存在就写一份。已存在则不动（不覆盖用户改动）。"""
    folder = os.path.join(vault, subfolder) if subfolder else vault
    path = os.path.join(folder, safe_note_name(GLOSSARY_NAME) + ".md")
    if os.path.exists(path):
        return path
    try:
        return write_note(path, _GLOSSARY_TEMPLATE.format(
            added=datetime.now().strftime("%Y-%m-%d")))
    except Exception:
        return None


def collect_stat(vault: str, subfolder: str = "") -> dict:
    """统计 vault 里已有的视频笔记，供索引与状态展示用。

    ``subfolder`` 为空时递归扫全库 —— 视频笔记可能同时落在「方法」和「日志」
    两个目录，只扫一个会漏。
    """
    stats = {"count": 0, "topics": {}, "platforms": {}, "recent": []}
    if not os.path.isdir(vault):
        return stats

    paths: list[str] = []
    skip = {".obsidian", ".git", ".trash", "node_modules"}
    if subfolder:
        root = os.path.join(vault, subfolder)
        if os.path.isdir(root):
            for name in os.listdir(root):
                if name.endswith(".md") and not name.startswith("."):
                    paths.append(os.path.join(root, name))
    else:
        for dirpath, dirnames, filenames in os.walk(vault):
            dirnames[:] = [d for d in dirnames
                           if d not in skip and not d.startswith(".")]
            for name in filenames:
                if name.endswith(".md") and not name.startswith("."):
                    paths.append(os.path.join(dirpath, name))

    rows = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                head = f.read(2000)
        except OSError:
            continue
        # 只统计本程序生成的笔记，避免把用户手写的笔记也算进去
        if "视频总结" not in head or "type: video-note" not in head:
            continue
        stats["count"] += 1
        title = _field(head, "title") or os.path.basename(p)[:-3]
        score = _field(head, "value") or "0"
        platform = _field(head, "platform") or "未知"
        added = _field(head, "created") or ""
        for t in _list_field(head, "topics"):
            stats["topics"][t] = stats["topics"].get(t, 0) + 1
        stats["platforms"][platform] = stats["platforms"].get(platform, 0) + 1
        rows.append((added, int(score) if str(score).isdigit() else 0, title))

    rows.sort(reverse=True)
    stats["recent"] = [{"added": a, "value": s, "title": t} for a, s, t in rows[:20]]
    return stats


def _field(head: str, key: str) -> str:
    """从前 2000 字符里取一个简单标量字段（只处理不带引号的值）。"""
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", head, re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    if v.startswith('"') and v.endswith('"') and len(v) > 1:
        v = v[1:-1]
    return v


def _list_field(head: str, key: str) -> list[str]:
    """取行内列表字段，如 ``topics: [A, B]``。"""
    m = re.search(rf"^{re.escape(key)}:\s*\[(.*?)\]", head, re.M | re.S)
    if not m:
        return []
    return [p.strip().strip("\"'") for p in m.group(1).split(",") if p.strip()]


def update_map(vault: str, subfolder: str = "", max_per_topic: int = 8) -> Optional[str]:
    """重建视频笔记索引。索引是派生数据，随时可重建，所以直接覆盖同名文件。"""
    stats = collect_stat(vault, "")
    if not stats["count"]:
        return None

    def _section(counter: dict, empty: str) -> str:
        if not counter:
            return f"{empty}\n"
        items = sorted(counter.items(), key=lambda kv: -kv[1])
        return "".join(f"- **{k}**（{v} 篇）\n" for k, v in items) + "\n"

    recent = "".join(f"- {r['added']} ｜ {r['title']}（{r['value']} 分）\n"
                     for r in stats["recent"]) or "- （暂无）\n"

    folder = os.path.join(vault, subfolder) if subfolder else vault
    name = safe_note_name(f"{MAP_NAME}") + ".md"
    path = os.path.join(folder, name)
    content = _MAP_TEMPLATE.format(
        title=MAP_NAME,
        added=datetime.now().strftime("%Y-%m-%d"),
        count=stats["count"],
        topic_section=_section(stats["topics"], "（暂无主题标签）"),
        platform_section=_section(stats["platforms"], "（暂无）"),
        recent_section=recent,
    )
    try:
        return write_note(path, content)
    except Exception:
        return None
