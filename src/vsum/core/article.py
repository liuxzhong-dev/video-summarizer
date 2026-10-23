"""
网页正文抽取 —— 视频之外的文章、图文笔记也��总结。

为什么不用现成库
----------------
`readability-lxml` / `trafilatura` 这类库效果好，但会给 exe 加上几十 MB 依赖
（lxml 是 C 扩展，打包体积涨、跨平台容易出问题）。本模块用标准库实现，
**零新依赖**，代价是对极端排版的页面不如 lxml 精细 —— 但对绝大多数
公众号、知乎、技术博客足够。

核心思路（简化版 Readability）
--------------------------------
1. 丢掉 ``<script> <style> <nav> <header> <footer> <aside>`` 等噪声容器
2. 剩下候选块按「文本量 / 链接密度」打分
3. 链接密度 = 链接文字长度 / 全部文字长度。导航和推荐列表这个比值极高
4. 取最高分的容器作为正文，再做轻量清洗

输入是 requests 拿到的 HTML，输出是干净正文 + 标题 + 元信息。
遇到登录墙 / 付费墙不尝试绕过，**明确报错让用户决定**（配合 Cookie 使用）。
"""

from __future__ import annotations

import html
import re
from typing import Optional

# --------------------------------------------------------------------------
# 噪声标签：整块丢掉
# --------------------------------------------------------------------------
_DROP_TAGS = (
    "script", "style", "noscript", "iframe", "svg", "canvas", "video", "audio",
    "nav", "header", "footer", "aside", "form", "button", "input", "select",
    "textarea", "template", "link", "meta",
)

#: 语义上可能是正文，但要先看内容的容器（不当成必然丢弃）
_ARTICLE_TAGS = ("article", "main")

#: class / id 里含这些词，多半是正文容器（各家 CMS 的常见命名）
_POSITIVE_HINT = re.compile(
    r"(article|content|post|entry|main|body|detail|rich_media|markdown|"
    r"post_body|article_body|news_content|blog|essay|text)",
    re.I,
)
#: 含这些词的几乎肯定不是正文
_NEGATIVE_HINT = re.compile(
    r"(comment|sidebar|footer|header|nav|menu|recommend|related|advert|"
    r"promotion|share|toolbar|breadcrumb|pagination|tag-list|widget|"
    r"copyright|qrcode|follow|like|rank|author-card)",
    re.I,
)

#: 常见正文标签的权重（越高越可能是正文）
_TAG_SCORE = {
    "article": 30, "main": 25, "div": 0, "section": 5, "td": 3,
    "pre": 8, "blockquote": 8, "ul": 3, "ol": 3, "table": 3,
    "h1": 12, "h2": 6, "h3": 3,
}

#: 中文正文长度下限（字符）。低于此值认为是摘要页 / 列表页
_MIN_ARTICLE_CHARS = 180


# --------------------------------------------------------------------------
# 清洗
# --------------------------------------------------------------------------
def _strip_tags(fragment: str) -> str:
    """把 HTML 片段转成纯文本，保留段落换行。"""
    # 块级标签结束处补换行，否则整篇会糊成一行
    frag = re.sub(r"(?i)<(br|hr)\s*/?>", "\n", fragment)
    frag = re.sub(r"(?i)</(p|div|section|article|li|tr|h[1-6]|blockquote)>", "\n", frag)
    frag = re.sub(r"(?s)<[^>]+>", "", fragment)
    frag = html.unescape(frag)
    # 全角空格 / 不间断空格统一
    frag = frag.replace("　", " ").replace("\xa0", " ")
    # 压缩空白但保留段落结构
    frag = re.sub(r"[ \t]+", " ", frag)
    frag = re.sub(r"\n\s*\n\s*\n+", "\n\n", frag)
    return frag.strip()


def _visible_text(node: str) -> str:
    """只取可见文本（先删掉要丢弃的标签）。"""
    s = node
    for tag in _DROP_TAGS:
        s = re.sub(rf"(?is)<{tag}\b.*?</{tag}>", " ", s)
        s = re.sub(rf"(?is)<{tag}\b[^>]*/?>", " ", s)
    return _strip_tags(s)


def _link_chars(node: str) -> int:
    """链接里的文字字符数。链接密度是判断正文/导航的关键。"""
    total = 0
    for m in re.finditer(r"(?is)<a\b[^>]*>(.*?)</a>", node):
        total += len(_strip_tags(m.group(1)))
    return total


#: 对外暴露的 HTML → 纯文本。EPUB 等「一片一片的 HTML」场景要按片段清洗，
#: 不能走 extract_article（它有 180 字的正文下限，会把短章节判成"抽不到正文"
#: 而整章丢弃）。别名而非新函数，保证和网页抽取用的是同一套清洗规则。
html_to_text = _strip_tags


# --------------------------------------------------------------------------
# 标题
# --------------------------------------------------------------------------
def _pick_title(html_text: str) -> str:
    """从文档里挑最像文章标题的文本。"""
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html_text)
    if m:
        t = _strip_tags(m.group(1))
        if t and len(t) <= 120:
            return t
    # og:title / twitter:title
    for pat in (r'(?is)<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
                r'(?is)<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\'](.*?)["\']'):
        m = re.search(pat, html_text)
        if m:
            t = html.unescape(m.group(1)).strip()
            if t and len(t) <= 120:
                return t
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text)
    if m:
        t = _strip_tags(m.group(1))
        # 去掉 "标题 - 站点名" 里的站点名
        for sep in (" | ", " - ", " _ ", " — "):
            if sep in t and len(t.split(sep)[0].strip()) > 4:
                t = t.split(sep)[0].strip()
                break
        if t:
            return t
    return ""


def _pick_meta_description(html_text: str) -> str:
    for pat in (r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                r'(?is)<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']'):
        m = re.search(pat, html_text)
        if m:
            return html.unescape(m.group(1)).strip()[:500]
    return ""


# --------------------------------------------------------------------------
# 正文抽取
# --------------------------------------------------------------------------
def _iter_containers(html_text: str):
    """产出候选正文容器（标签或带 class/id 的 div/section）。"""
    # 优先语义标签
    for tag in _ARTICLE_TAGS:
        for m in re.finditer(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", html_text):
            yield m.group(1), _TAG_SCORE.get(tag, 0), None
    # 其次带 class/id 提示的容器
    for m in re.finditer(r"(?is)<(div|section)\b([^>]*)>(.*?)</\1>", html_text):
        attrs = m.group(2) or ""
        ident = (re.search(r'(?i)id=["\'](.*?)["\']', attrs) or [None, ""])[1] if attrs else ""
        cls = (re.search(r'(?i)class=["\'](.*?)["\']', attrs) or [None, ""])[1] if attrs else ""
        tag = f"{ident} {cls}"
        base = _TAG_SCORE.get(m.group(1), 0)
        if _NEGATIVE_HINT.search(tag):
            base -= 25
        if _POSITIVE_HINT.search(tag):
            base += 18
        yield m.group(3), base, tag


def _score(node: str, base: int) -> float:
    """给一个容器打正文分。"""
    text = _visible_text(node)
    n = len(text)
    if n < _MIN_ARTICLE_CHARS:
        return 0.0
    lc = _link_chars(node)
    link_ratio = lc / n if n else 1.0
    # 链接占比高 → 多半是导航/推荐列表
    if link_ratio > 0.55:
        return 0.0
    # 段落数：文章有多个自然段
    paras = len([p for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 20])
    score = base + n / 40.0 + paras * 3.0
    score -= link_ratio * 60.0
    return score


def extract_article(html_text: str) -> dict:
    """从 HTML 里抽出正文。

    返回 ``{"ok", "title", "text", "excerpt", "reason", "kind"}``。
    ``ok=False`` 时 ``reason`` 说明为什么抽不到（登录墙 / 验证页 / 内容太短）。
    """
    if not html_text or not html_text.strip():
        return {"ok": False, "reason": "页面内容为空", "title": "", "text": "",
                "excerpt": "", "kind": ""}

    title = _pick_title(html_text)

    # 整页文本量太短 → 可能是验证页 / 登录墙 / SPA 未渲染
    whole = _visible_text(html_text)
    if len(whole) < _MIN_ARTICLE_CHARS:
        low = whole.lower()
        wall_markers = ("登录", "请登录", "扫码", "验证", "captcha", "sign in",
                        "subscribe", "开通会员", "付费", "订阅")
        kind = ""
        if any(m in whole for m in wall_markers):
            kind = "登录或付费墙"
        elif "验证码" in whole or "安全验证" in whole or "captcha" in low:
            kind = "验证页"
        return {"ok": False,
                "reason": (f"页面可读文本只有 {len(whole)} 字符"
                           + (f"，判断为{kind}" if kind else "，可能是验证页或内容需登录")),
                "title": title, "text": "", "excerpt": "", "kind": kind}

    # 打分选最佳容器
    best, best_score = "", 0.0
    for node, base, _tag in _iter_containers(html_text):
        s = _score(node, base)
        if s > best_score:
            best_score, best = s, node
    text = _visible_text(best) if best else whole
    if len(text) < _MIN_ARTICLE_CHARS:
        text = whole
    if len(text) < _MIN_ARTICLE_CHARS:
        return {"ok": False, "reason": f"未能定位正文（可读文本 {len(text)} 字符）",
                "title": title, "text": "", "excerpt": "", "kind": ""}

    # 粗清理：去掉连续重复的行（导航残留）
    lines, seen_run = [], {}
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        seen_run[s] = seen_run.get(s, 0) + 1
        if seen_run[s] > 2:          # 同��行出现 3 次以上基本是噪声
            continue
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    excerpt = text[:300]
    return {
        "ok": True,
        "title": title,
        "text": text,
        "excerpt": excerpt,
        "reason": "",
        "kind": "article",
    }


# --------------------------------------------------------------------------
# 纯文本
# --------------------------------------------------------------------------
def extract_from_plain(raw: str) -> dict:
    """用户直接粘了一段正文（没链接）时的处理。

    做轻度结构化：识别 markdown 标题、列表、代码块，便于摘要时按段取材。
    """
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        return {"ok": False, "reason": "输入为空", "title": "", "text": "",
                "excerpt": "", "kind": ""}

    # 标题：markdown 的 # 一级标题，或第一行短句
    title = ""
    m = re.search(r"(?m)^#\s+(.+)$", text)
    if m:
        title = m.group(1).strip()[:120]
    else:
        for ln in text.split("\n"):
            s = ln.strip()
            if s:
                if 4 <= len(s) <= 60 and not s.endswith(("。", "！", "？", ".", "!", "?")):
                    title = s
                break

    # 段落按空行归一，代码块（```）保持原样
    text = re.sub(r"\n{3,}", "\n\n", text)
    return {
        "ok": True,
        "title": title,
        "text": text,
        "excerpt": text[:300],
        "reason": "",
        "kind": "plain",
    }
