"""从「分享文案」里抠出链接。

手机 App 分享出来的是一整段文本，长这样::

    6.66 gAh:/ 复制打开抖音，看看【人民日报的作品】标题 
    https://v.douyin.com/xxxxxxx/ 复制此链接，打开Dou音搜索，直接观看视频！

所以需要一条能正确处理「URL 后面紧跟中文」的规则。
"""

from __future__ import annotations

import re

#: URL 合法字符集（RFC 3986 的子集），遇到其它字符就认为链接结束
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
_URL_RE = re.compile(rf"https?://[{_URL_CHARS}]+", re.I)
_BARE_RE = re.compile(rf"(?<![\w@.])(?:www\.)[A-Za-z0-9\-._~]+(?:/[{_URL_CHARS}]*)?")

#: 抖音/快手这类口令里的标题，形如 【xxx的作品】
_TITLE_RE = re.compile(r"【([^【】]{2,80})】")
#: 视频号等平台的标题用书名号包着：「张三的作品《标题》」
_BOOK_RE = re.compile(r"《([^《》]{2,60})》")
_HASH_RE = re.compile(r"#([^#\s]{4,60})#")


def extract_url(text: str) -> str:
    """从任意文本里提取第一个链接；**没有链接时原样返回**。

    「原样返回」是为了支持直接粘正文：流水线后面会按「像不像本地路径 /
    文档后缀」判断，若不是才当链接处理。详见 PlainTextAdapter.match()。
    """
    if not text:
        return ""
    m = _URL_RE.search(text)
    if m:
        return _rstrip_punct(m.group(0))
    m = _BARE_RE.search(text)
    if m:
        return "https://" + _rstrip_punct(m.group(0))
    return text.strip()


def extract_all_urls(text: str) -> list[str]:
    """提取文本里的全部链接（去重保序）。"""
    if not text:
        return []
    out, seen = [], set()
    for m in _URL_RE.finditer(text):
        u = _rstrip_punct(m.group(0))
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def guess_title(text: str) -> str:
    """从分享文案里猜标题（链接本身之外）。"""
    if not text:
        return ""
    cleaned = _URL_RE.sub("", text)
    m = _TITLE_RE.search(cleaned)
    if m:
        t = m.group(1).strip()
        # 「xxx的作品」-> 取后面的部分
        t = re.sub(r"^.*?的作品[:：]?\s*", "", t)
        return t
    m = _BOOK_RE.search(cleaned)
    if m:
        return m.group(1).strip()
    m = _HASH_RE.search(cleaned)
    if m:
        return m.group(1).strip()
    return ""


def _rstrip_punct(url: str) -> str:
    """去掉结尾被误吸收的标点（中文句号、括号等）。"""
    return re.sub(r"[。，、；,.!！？?)\]】]+$", "", url)
