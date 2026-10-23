"""
文章 / 图文笔记适配器 —— 补上「视频之外」的空白。

为什么要单独一个适配器
--------------------
原项目只处理视频：``VideoMeta`` 记的是「有哪些音视频流」，``Transcript``
记的是「说了什么话」。文章类内容没有音视频流，但**同样有标题、正文、
元信息**，结构完全对得上 —— 所以能复用同一条流水线，不需要改动摘要、
导出、入库那一整条链路。真正的差别只在「正文从哪来」。

三个扩展点
----------
1. **网页文章 / 公众号 / 知乎 / 博客园** → ``ArticleAdapter``
   抓 HTML 页面，用 ``core.article`` 抽取正文。
2. **小红书图文笔记** → ``XiaohongshuAdapter`` 的图文分支
   小红书的图文笔记页面里，正文在 ``__INITIAL_STATE__`` 里而非视频流。
3. **纯文本直贴 / 本地文档** → ``PlainTextAdapter``
   用户直接粘正文，或把 .txt / .md / .pdf / .docx / .xlsx / .pptx / .epub
   等拖进来（界面支持拖拽）。

本地文档解析
------------
``.txt .md .csv .json`` 等直接按文本读；其余走各自的解析器：

| 类别 | 后缀 | 依赖 |
| --- | --- | --- |
| 版式文档 | `.pdf` | pypdf |
| Office（新版） | `.docx` `.xlsx` `.pptx` | python-docx / openpyxl / python-pptx |
| Office（旧版） | `.doc` `.xls` `.ppt` | xlrd 只覆盖 `.xls`；`.doc`/`.ppt` 走 OLE 兜底或提示另存 |
| OpenDocument | `.odt` `.ods` `.odp` | 标准库 |
| 电子书 | `.epub` `.mhtml` | 标准库 |
| 网页存档 | `.html` `.htm` `.mhtml` | 标准库 |
| 富文本 | `.rtf` | 标准库（自研剥离器） |
| 字幕 | `.srt` `.vtt` `.ass` | 标准库 |

解析器都是**可选依赖**，缺了不报导入错误，而是在用户真的拖进这类文件时
给出「装什么、怎么装」的明确提示 —— 这样没装库的人依然能用链接总结，
不会整个程序起不来。

**按内容识别（不只看后缀）**
后缀只作为第一线索。真正的判别以文件头（magic bytes）为准 ——
于是「把 .docx 改名成 .wps」「WPS 存出来的 .et/.dps」「后缀丢了变成 .bin」
都能正确读出来，而不是甩一句「暂不支持」。老版 OLE2 二进制（Word 97-2003
的 .doc/.ppt）没有可靠的纯 Python 提取路径，会明确提示另存为新格式。

合规
----
遇到登录墙 / 付费墙 / 验证页**不尝试绕过**，明确报错并提示可在设置里填
Cookie（用户自己的登录态）。不接第三方付费内容源，不做验证码破解。
"""

from __future__ import annotations

import os
import re
from typing import Optional

from ..core.article import extract_article, extract_from_plain, html_to_text
from ..core.errors import FetchError
from ..core.models import Segment, Transcript, VideoMeta
from ..core.registry import register_platform
from .base import SUPPORT_FULL, SUPPORT_PARTIAL, PlatformAdapter


#: 纯文本类后缀（直接按文本解码读）
_DOC_EXT = (".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
            ".json", ".yaml", ".yml", ".xml", ".ini", ".toml", ".cfg",
            ".conf", ".properties", ".tex", ".sql", ".srt", ".vtt",
            ".ass", ".ssa", ".sbv")
#: 需要解析器的二进制/结构化文档
_BIN_DOC_EXT = (".pdf", ".docx", ".doc", ".html", ".htm", ".mhtml", ".mht",
                ".pptx", ".ppt", ".xlsx", ".xls", ".epub", ".rtf",
                ".odt", ".ods", ".odp",
                # WPS 三大件（WPS 文字/表格/演示）。新版 WPS 保存的其实是
                # OOXML 或 ODF 内核，靠内容嗅探识别；老版是 OLE2 二进制。
                ".wps", ".et", ".dps")
#: 界面与文档用：一次性列全，避免三处各写一份格式清单而对不上
SUPPORTED_DOC_EXT = _DOC_EXT + _BIN_DOC_EXT

#: 认得出、但**没有可靠纯 Python 提取路径**的老格式。
#: 命中这些时给的是「怎么另存为」的操作指引，而不是一句「解析失败」。
#: 注意 .xls 不在其中 —— xlrd 能读它。
_LEGACY_OFFICE = (".doc", ".ppt", ".wps", ".et", ".dps")

#: 字幕后缀 → 需要洗掉时间轴再总结（否则正文全是 `00:00:01,200 --> ...`）
_SUBTITLE_EXT = (".srt", ".vtt", ".ass", ".ssa", ".sbv")

#: 来源标签：给「第 N 段 - 来源为 XX」这类说明用，别把 PDF 说成「网页正文」
_KIND_LABEL = {
    "pdf": "PDF 文字层", "docx": "Word 正文", "pptx": "PPT 文本",
    "xlsx": "Excel 表格", "xls": "Excel 表格（旧版）",
    "epub": "EPUB 章节", "mhtml": "网页存档",
    "html": "网页正文", "rtf": "RTF 正文",
    "odt": "OpenDocument 正文", "ods": "OpenDocument 表格",
    "odp": "OpenDocument 演示",
    "md": "Markdown 正文", "markdown": "Markdown 正文", "rst": "reST 正文",
    "txt": "文本文件", "csv": "CSV 表格", "tsv": "TSV 表格",
    "json": "JSON", "xml": "XML", "yaml": "YAML", "yml": "YAML",
    "srt": "字幕", "vtt": "字幕", "ass": "字幕", "ssa": "字幕", "sbv": "字幕",
}

#: 单个文档最多读取的字数。纯粹是内存/耗时护栏 —— 真正决定喂给模型多少字
#: 的是 ``llm.max_input_chars``（默认 24000）。一本几百万字的小说如果原样
#: 拼进字符串，光是切分和正则清洗就要好几秒。
_MAX_DOC_CHARS = 200_000

#: 文档正文少于此长度就认定「没读到东西」，直接报错。
#: **不能放行** —— 否则会拿文件名当标题去让模型编总结，
#: 和之前视频号「编占位标题」是同一个坑。
_MIN_DOC_CHARS = 20


def _clip(text: str, limit: int = _MAX_DOC_CHARS) -> str:
    """清洗并限长：压掉多余空白，超长截断并注明。"""
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        return text[:limit] + f"\n\n（原文过长，以上为前 {limit} 字）"
    return text


def _missing_lib(pkg: str, what: str) -> FetchError:
    """可选依赖缺失时给一条「能照着做」的提示，而不是干巴巴的报错。"""
    return FetchError(
        f"读{what}需要额外装一个库",
        hint=f"在项目目录执行：.venv\\Scripts\\pip install {pkg}\n"
             f"（用 exe 的话不用管这条，发布版已内置）")



@register_platform("article")
class ArticleAdapter(PlatformAdapter):
    """通用网页文章 / 公众号 / 知乎 / 技术博客。"""

    key = "article"
    name = "网页文章（通用抽取）"
    # 不设 domains —— 它是「非视频链接」的兜底，排在视频平台之后匹配
    domains = ()
    patterns = ()
    support = SUPPORT_PARTIAL
    note = "抓取网页正文后总结。遇到登录墙/付费墙会明确提示，可填 Cookie 后重试。"

    #: 这些站点的正文更可能需要登录，提示用户填 Cookie
    _NEEDS_COOKIE_HINT = (
        "zhihu.com", "mp.weixin.qq.com", "xhs", "xiaohongshu",
        "juejin.cn", "csdn.net", "toutiao.com", "weibo.com",
    )

    @classmethod
    def match(cls, url: str) -> bool:
        return bool(re.match(r"^https?://", (url or "").strip(), re.I))

    # ---------------- 解析 ----------------
    def resolve(self, url: str) -> VideoMeta:
        html_text = self._fetch_html(url)
        art = extract_article(html_text)
        if not art["ok"]:
            hint = ("在「设置 → 平台 Cookie」里填该站点的 Cookie 后重试"
                    if self._wants_cookie(url) else
                    "确认这个链接能在浏览器里直接打开；需要登录的内容要先登录")
            raise FetchError(f"没有抽到正文：{art['reason']}", hint=hint)

        title = art["title"] or "（无标题）"
        return VideoMeta(
            platform="article",
            platform_name="网页文章",
            video_id=self.extract_id(url) or "",
            url=url,
            title=title,
            author=self._pick_author(html_text),
            description=art["excerpt"][:200],
            extra={"kind": "article", "chars": len(art["text"])},
        )

    def fetch_subtitle(self, meta: VideoMeta) -> Optional[Transcript]:
        """文章没有「字幕」，正文就是语料。返回正文让流水线直接用。"""
        try:
            html_text = self._fetch_html(meta.url)
        except Exception as exc:
            self.log.warning("文章正文重新抓取失败: %s", exc)
            return None
        art = extract_article(html_text)
        if not art["ok"]:
            return None
        return _article_transcript(art["text"], meta)

    # ---------------- 内部 ----------------
    def _fetch_html(self, url: str) -> str:
        try:
            return self.http.get_text(url, timeout=25)
        except Exception as exc:
            raise FetchError(f"页面抓取失败：{exc}",
                             hint="检查网络/代理，或确认该链接能在浏览器打开")

    def _wants_cookie(self, url: str) -> bool:
        u = url.lower()
        return any(k in u for k in self._NEEDS_COOKIE_HINT)

    @staticmethod
    def _pick_author(html_text: str) -> str:
        for pat in (r'(?is)<meta[^>]+name=["\']author["\'][^>]+content=["\'](.*?)["\']',
                    r'(?is)<meta[^>]+property=["\']article:author["\'][^>]+content=["\'](.*?)["\']'):
            m = re.search(pat, html_text)
            if m:
                v = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if v and len(v) <= 60:
                    return v
        return ""


def _article_transcript(text: str, meta: VideoMeta) -> Transcript:
    """把正文包装成 Transcript，好让下游（摘要/导出/入库）零改动复用。

    按空行切段并给每段估个时间戳 —— 这样界面上的「时间线概览」对文章
    依然有意义（显示为「第 N 段」），而不是一片空白。
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    segs, acc = [], 0.0
    for i, p in enumerate(paras):
        # 按字数估时长：中文约 5 字/秒，仅用于让时间轴单调递增
        dur = max(1.0, len(p) / 5.0)
        segs.append(Segment(start=round(acc, 2), end=round(acc + dur, 2), text=p))
        acc += dur

    # 措辞按真实来源区分。以前这里写死「网页正文抽取」，导致拖进来的
    # PDF / Word 也在摘要末尾显示「来源为网页文章」，与实际不符。
    kind = ((meta.extra or {}).get("kind") or "").lower()
    label = _KIND_LABEL.get(kind, "正文文本")
    tr = Transcript(
        source="article",                     # 来源类型：正文文本（非音视频）
        text="\n\n".join(paras),
        segments=segs,
        language="zh",
        engine="article-extractor",
        notes=[f"{label}抽取：{len(text)} 字 / {len(paras)} 段"],
    )
    tr.notes.append("来源为正文文本，无音视频，未做语音识别")
    return tr


@register_platform("plain")
class PlainTextAdapter(PlatformAdapter):
    """纯文本直贴 / 本地文档。

    触发方式有三种：
    1. 用户直接粘一段正文（不含任何链接）
    2. 用户给一个本地文件路径
    3. 界面把文件拖进来（前端上传后落成临时文件，再走路径这条）

    支持的后缀见 ``SUPPORTED_DOC_EXT``；**后缀只是线索，内容才是准绳** ——
    文件头认得出真实格式的话，后缀写错也能读（详见 ``_sniff_container``）。
    """

    key = "plain"
    name = "纯文本 / 本地文档"
    domains = ()
    patterns = ()
    support = SUPPORT_PARTIAL
    note = ("直接粘正文，或把文件拖进来 / 给出路径。支持 PDF、Word（.docx/.doc）、"
            "Excel（.xlsx/.xls）、PPT（.pptx）、EPUB、RTF、OpenDocument"
            "（.odt/.ods/.odp）、网页（.html/.mhtml）、字幕（.srt/.vtt/.ass）、"
            "以及 txt/md/csv/json 等纯文本。不联网。")

    @classmethod
    def match(cls, url: str) -> bool:
        u = (url or "").strip()
        if not u or re.match(r"^https?://", u, re.I):
            return False
        # 本地路径（含盘符）
        if re.match(r"^[a-zA-Z]:[\\/]", u) or re.match(r"^[\\/]{1,2}", u):
            return True
        # 已知文档后缀
        if u.lower().endswith(_DOC_EXT + _BIN_DOC_EXT):
            return True
        # 纯粘贴的长文本：不是 URL、也不是路径，但内容够长（够当一篇正文）。
        # 阈值 60 字是刻意的：短于此更像随手输入的查询词，
        # 长于此基本可以确定是让总结正文。
        if len(u) >= 60 and "\n" in u:
            return True
        if len(u) >= 120:
            return True
        return False

    def resolve(self, url: str) -> VideoMeta:
        path = url.strip()
        text, kind = self._read(path)
        is_file = kind != "paste"
        # **没读到文字就报错，绝不放行**。放行的后果是：标题退化成文件名，
        # 然后让大模型对着一个文件名编一篇"总结"——用户以为总结的是文档，
        # 实际总结的是文件名。扫描版 PDF 是最常见的触发场景。
        if len((text or "").strip()) < _MIN_DOC_CHARS:
            raise FetchError(
                f"没能从这个文件里读到文字（{kind}）",
                hint="如果是扫描件/图片版，需要先 OCR；"
                     "也可以把文字复制出来直接粘到输入框")
        art = extract_from_plain(text)
        title = art["title"] or "粘贴的文本"
        if is_file:
            # **文件一律用文件名当标题**。既有的「从正文首行猜标题」对文档
            # 不适用：PPT 首行是「【第 1 页】」、PDF 首行常是页眉/刊名，
            # 拿去当标题会很难看；而且用户也更认自己拖进来的那个文件名。
            title = os.path.splitext(os.path.basename(path))[0]
            # 例外：Markdown / 纯文本里**显式的 `# 一级标题`** 比文件名更准
            # （知识库笔记常以日期做文件名，真标题在正文里）。
            if kind in ("md", "markdown", "txt", "rst", "text"):
                m = re.search(r"(?m)^#\s+(.+)$", text)
                if m and m.group(1).strip():
                    title = m.group(1).strip()[:120]
        return VideoMeta(
            platform="plain",
            platform_name=f"本地文档（{kind}）" if is_file else "纯文本",
            # 粘贴的文本没有「文件名」，用标题代替；文件才记 basename。
            video_id=(os.path.basename(path)[:60] if is_file
                      else (art["title"] or "粘贴的文本")),
            # 关键：**必须把原始输入存进 extra.path**。video_id 被清洗过
            # （去掉了路径分隔符），靠它回读文件会失败。
            url=path if is_file else "",
            title=title,
            description=art["excerpt"][:200],
            duration=None,
            extra={"kind": kind, "chars": len(text), "path": path},
        )

    def fetch_subtitle(self, meta: VideoMeta) -> Optional[Transcript]:
        # 回读原始输入：粘贴的文本其 path 就是正文本身。
        path = (meta.extra or {}).get("path") or meta.video_id or ""
        if not path:
            return None
        try:
            text, _kind = self._read(path)
        except Exception as exc:
            self.log.warning("文档读取失败: %s", exc)
            text = meta.description or ""
        if not text:
            return None
        return _article_transcript(text, meta)

    # ---------------- 内部 ----------------
    def _read(self, path: str) -> tuple[str, str]:
        """返回 ``(正文, 类型)``。文件不存在时视为「用户粘的正文」。"""
        if not os.path.isfile(path):
            # 不是文件 → 当作用户直接粘的正文
            return path, "paste"

        ext = os.path.splitext(path)[1].lower()
        if ext in _DOC_EXT:
            text = self._read_text_file(path)
            if ext in _SUBTITLE_EXT:
                text = _clean_subtitle(text)
            return text, ext.lstrip(".")

        # 二进制/结构化文档：**先看内容、再看后缀**。
        # 后缀并不可信 —— WPS 把 docx 存成 .wps、用户手动改过后缀、
        # 浏览器下载丢了扩展名，这些都不该直接报「不支持」。
        sniffed = _sniff_container(path)
        if sniffed == "ole2":
            # OLE2 是旧版二进制 Office 的**公共外壳**：doc / xls / ppt 的文件头
            # 一模一样，光看头分不出来。这里靠内部流名再认一层 ——
            # 只有 .xls 有可靠的纯 Python 读取路径（xlrd），其余给「另存为」指引。
            real = _ole2_kind(path)
            if ext == ".xls" or real == ".xls":
                return self._read_xls(path), "xls"
            raise _legacy_office_error(os.path.basename(path), real)
        if sniffed and sniffed in _BIN_DOC_EXT:
            if sniffed != ext:
                self.log.debug("后缀 %s 与内容不符，按内容识别为 %s", ext, sniffed)
            ext = sniffed
        elif ext not in _BIN_DOC_EXT:
            raise FetchError(
                f"暂不支持的文件类型：{ext or '（无后缀）'}",
                hint="支持 " + "  ".join(SUPPORTED_DOC_EXT))
        return self._read_binary(path, ext)

    @staticmethod
    def _read_text_file(path: str) -> str:
        """按常见编码逐个试，别让一个 GBK 文件把整条流程卡死。"""
        # utf-8-sig 放最前：它能吃下带 BOM 与不带 BOM 的两种 UTF-8
        for enc in ("utf-8-sig", "utf-8", "gbk", "big5", "latin-1"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, OSError):
                continue
        raise FetchError(
            f"读不了这个文本文件（编码不支持）：{os.path.basename(path)}",
            hint="用记事本打开，另存为 UTF-8 编码，再拖进来")

    def _read_binary(self, path: str, ext: str) -> tuple[str, str]:
        """各类二进制/结构化文档 → ``(正文, 类型)``。"""
        if ext in (".html", ".htm"):
            return self._read_html(path), "html"
        if ext in (".mhtml", ".mht"):
            return self._read_mhtml(path), "mhtml"
        if ext == ".pdf":
            return self._read_pdf(path), "pdf"
        if ext == ".docx":
            return self._read_docx(path), "docx"
        if ext == ".pptx":
            return self._read_pptx(path), "pptx"
        if ext == ".xlsx":
            return self._read_xlsx(path), "xlsx"
        if ext == ".xls":
            return self._read_xls(path), "xls"
        if ext == ".epub":
            return self._read_epub(path), "epub"
        if ext == ".rtf":
            return self._read_rtf(path), "rtf"
        if ext in (".odt", ".ods", ".odp"):
            return self._read_odf(path, ext), ext.lstrip(".")
        if ext in _LEGACY_OFFICE:
            raise _legacy_office_error(os.path.basename(path))
        raise FetchError(f"暂不支持的文件类型：{ext}")

    # ---- html / mhtml ----
    @staticmethod
    def _read_html(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError as exc:
            raise FetchError(f"读不了这个 HTML 文件：{exc}")
        art = extract_article(src)
        # HTML 抽不到正文时**退回整页清洗**而不是直接报错：
        # 本地 .html 常常是「网页另存为」的单页，没有 article/main 容器，
        # extract_article 的正文字数下限会把它们判成"抽不到"。
        return _clip(art["text"] if art["ok"] else html_to_text(src))

    @staticmethod
    def _read_mhtml(path: str) -> str:
        """MHTML（网页「另存为单个文件」）。本质是 MIME 邮件，正文在 text/html 段。"""
        import email
        from email import policy

        try:
            with open(path, "rb") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
        except Exception as exc:
            raise FetchError(f"网页存档打不开：{exc}")
        html = ""
        for part in msg.walk():
            if part.get_content_type() != "text/html":
                continue
            try:
                html = part.get_content()
            except Exception:
                html = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            break
        if not html.strip():
            raise FetchError("这个网页存档里没有 HTML 正文",
                             hint="文件可能不是真正的 MHTML，或只存下了图片")
        art = extract_article(html)
        return _clip(art["text"] if art["ok"] else html_to_text(html))

    # ---- pdf ----
    @staticmethod
    def _read_pdf(path: str) -> str:
        try:
            from pypdf import PdfReader          # 可选依赖
        except ImportError:
            try:
                from PyPDF2 import PdfReader       # 兼容旧包名
            except ImportError:
                raise _missing_lib("pypdf", "PDF")
        try:
            reader = PdfReader(path)
        except Exception as exc:
            raise FetchError(f"PDF 打不开：{exc}",
                             hint="确认文件没损坏、不是别的格式改后缀而来")

        if getattr(reader, "is_encrypted", False):
            try:
                # 空白密码是版权保护类 PDF 的常见做法，先试一下
                if reader.decrypt("") == 0:
                    raise ValueError("需要密码")
            except Exception:
                raise FetchError("这个 PDF 有密码保护，读不了",
                                 hint="先用 PDF 阅读器另存为一份无密码的副本")

        pages, blank = [], 0
        for pg in reader.pages:
            try:
                t = (pg.extract_text() or "").strip()
            except Exception:
                t = ""
            if t:
                pages.append(t)
            else:
                blank += 1
        text = "\n\n".join(pages)
        if len(text) < _MIN_DOC_CHARS:
            # 扫描件 / 图片版 PDF 的典型特征：有页面，但一个字符都抽不出来。
            raise FetchError(
                f"这个 PDF 里没有可提取的文字（{len(reader.pages)} 页）",
                hint="多半是扫描件或图片版 PDF，需要 OCR 才能读。\n"
                     "可以：① 用「微信/QQ 的长按提取文字」等工具先转成文本，"
                     "再粘贴到输入框用「纯文本总结」；② 或换一份带文字层的电子版。")
        if blank:
            text += f"\n\n（另有 {blank} 页为图片或空白，未提取到文字）"
        return _clip(text)

    # ---- docx ----
    @staticmethod
    def _read_docx(path: str) -> str:
        try:
            import docx                                 # 可选依赖
        except ImportError:
            raise _missing_lib("python-docx", "Word 文档")
        try:
            d = docx.Document(path)
        except Exception as exc:
            raise FetchError(f"Word 文档解析失败：{exc}",
                             hint="确认是 .docx（不是改了后缀的 .doc）")

        blocks: list[str] = []
        try:
            # 按文档流顺序遍历段落与表格 —— 直接取 d.paragraphs 会把所有表格
            # 挤到正文末尾，读起来前言不搭后语。
            from docx.oxml.ns import qn
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            for child in d.element.body.iterchildren():
                if child.tag == qn("w:p"):
                    t = (Paragraph(child, d).text or "").strip()
                    if t:
                        blocks.append(t)
                elif child.tag == qn("w:tbl"):
                    for row in Table(child, d).rows:
                        cells = [c.text.strip() for c in row.cells]
                        if any(cells):
                            blocks.append(" | ".join(cells))
        except Exception:
            # 排版复杂时上面的 XML 遍历可能出意外，退回最朴素的做法
            blocks = [p.text.strip() for p in d.paragraphs if p.text.strip()]
            for tb in getattr(d, "tables", []) or []:
                for row in tb.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        blocks.append(" | ".join(cells))
        return _clip("\n\n".join(blocks))

    # ---- pptx ----
    @staticmethod
    def _read_pptx(path: str) -> str:
        try:
            from pptx import Presentation               # 可选依赖
        except ImportError:
            raise _missing_lib("python-pptx", "PPT")
        try:
            prs = Presentation(path)
        except Exception as exc:
            raise FetchError(f"PPT 解析失败：{exc}",
                             hint="只支持 .pptx（新版格式）；.ppt 请先另存为 .pptx")

        parts: list[str] = []
        for i, slide in enumerate(prs.slides, 1):
            buf: list[str] = []
            for shape in _iter_shapes(slide.shapes):
                if getattr(shape, "has_text_frame", False):
                    t = "\n".join(p.text for p in shape.text_frame.paragraphs
                                  if p.text.strip())
                    if t.strip():
                        buf.append(t.strip())
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells]
                        if any(cells):
                            buf.append(" | ".join(cells))
            # 讲稿备注往往是信息密度最高的部分，别丢
            if slide.has_notes_slide:
                try:
                    nt = (slide.notes_slide.notes_text_frame.text or "").strip()
                except Exception:
                    nt = ""
                if nt:
                    buf.append("[备注] " + nt)
            if buf:
                parts.append(f"【第 {i} 页】\n" + "\n".join(buf))

        if not parts:
            raise FetchError("这个 PPT 里没提取到文字",
                             hint="幻灯片可能全是图片/截图，需要 OCR 或手动整理文字")
        return _clip("\n\n".join(parts))

    # ---- epub ----
    @staticmethod
    def _read_epub(path: str) -> str:
        """只用标准库解 EPUB：它本质是个 zip，正文是一堆 xhtml。"""
        import zipfile

        try:
            zf = zipfile.ZipFile(path)
        except Exception as exc:
            raise FetchError(f"EPUB 打不开：{exc}")
        with zf:
            names = set(zf.namelist())
            order = _epub_spine(zf) or sorted(
                n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
            chunks: list[str] = []
            for name in order:
                if name not in names:
                    continue
                try:
                    raw = zf.read(name).decode("utf-8", "replace")
                except Exception:
                    continue
                t = html_to_text(raw)
                if t:
                    chunks.append(t)
        text = "\n\n".join(chunks)
        if len(text) < _MIN_DOC_CHARS:
            raise FetchError("这个 EPUB 里没提取到文字",
                             hint="可能是纯图片漫画，需要 OCR")
        return _clip(text)

    # ---- xlsx / xls（表格）----
    @staticmethod
    def _read_xlsx(path: str) -> str:
        try:
            from openpyxl import load_workbook        # 可选依赖
        except ImportError:
            raise _missing_lib("openpyxl", "Excel 表格")

        parts: list[str] = []
        # 传**文件对象**而不是路径 —— openpyxl 会拿后缀名拦人
        # （「does not support .et file format」），而我们的「按内容识别」
        # 恰恰允许后缀是 .et / .wps / 干脆没后缀。
        try:
            with open(path, "rb") as fh:
                # read_only：几十万行的表也不会把内存吃爆；
                # data_only：取公式的**计算结果**，而不是 `=SUM(A1:A9)` 这种表达式
                wb = load_workbook(fh, read_only=True, data_only=True)
                try:
                    for ws in wb.worksheets:
                        rows = []
                        for row in ws.iter_rows(values_only=True):
                            cells = [str(c).strip() for c in row
                                     if c is not None and str(c).strip()]
                            if cells:
                                rows.append(" | ".join(cells))
                        if rows:
                            parts.append(f"【工作表：{ws.title}】\n" + "\n".join(rows))
                finally:
                    wb.close()
        except OSError as exc:
            raise FetchError(f"Excel 文件读不了：{exc}")
        except Exception as exc:
            raise FetchError(
                f"Excel 打不开：{exc}",
                hint="确认是 Excel 文件（.xlsx / .xls），且没有设密码")
        if not parts:
            raise FetchError("这个 Excel 里没提取到文字",
                             hint="可能是空表，或内容全在图片/图表里")
        return _clip("\n\n".join(parts))

    @staticmethod
    def _read_xls(path: str) -> str:
        """旧版 Excel（.xls）。xlrd 2.x 只保留了对 .xls 的支持，正好对口。"""
        try:
            import xlrd                               # 可选依赖
        except ImportError:
            raise _missing_lib("xlrd", "旧版 Excel（.xls）")
        try:
            book = xlrd.open_workbook(path)
        except Exception as exc:
            raise FetchError(
                f"Excel 打不开：{exc}",
                hint="确认是 Excel 文件（.xls 或 .xlsx），且没有加密码")
        parts = []
        for ws in book.sheets():
            rows = []
            for r in range(ws.nrows):
                cells = [str(v).strip() for v in ws.row_values(r)
                         if str(v).strip()]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"【工作表：{ws.name}】\n" + "\n".join(rows))
        if not parts:
            raise FetchError("这个 Excel 里没提取到文字",
                             hint="可能是空表，或内容全在图片/图表里")
        return _clip("\n\n".join(parts))

    # ---- rtf ----
    @staticmethod
    def _read_rtf(path: str) -> str:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as exc:
            raise FetchError(f"RTF 打不开：{exc}")
        text = _rtf_to_text(raw)
        if len(text) < _MIN_DOC_CHARS:
            raise FetchError("这个 RTF 里没提取到文字",
                             hint="文件可能是空的，或正文全在图片里")
        return _clip(text)

    # ---- odt / ods / odp ----
    @staticmethod
    def _read_odf(path: str, ext: str) -> str:
        """OpenDocument 三件套。本质和 EPUB 一样是 zip，正文在 content.xml。"""
        import xml.etree.ElementTree as ET
        import zipfile

        try:
            with zipfile.ZipFile(path) as zf:
                raw = zf.read("content.xml")
        except KeyError:
            raise FetchError(
                "这个 OpenDocument 文件里没有 content.xml",
                hint="文件可能损坏，或只是把别的格式改成了这个后缀")
        except Exception as exc:
            raise FetchError(f"OpenDocument 打不开：{exc}")
        try:
            root = ET.fromstring(raw)
        except Exception as exc:
            raise FetchError(f"OpenDocument 内容解析失败：{exc}")

        if ext == ".ods":
            lines = []
            for row in root.iter():
                if not row.tag.endswith("}table-row"):
                    continue
                cells = []
                for cell in row:
                    if not cell.tag.endswith("}table-cell"):
                        continue
                    t = "".join(cell.itertext()).strip()
                    if t:
                        cells.append(t)
                if cells:
                    lines.append(" | ".join(cells))
            text = "\n".join(lines)
        else:
            text = "\n\n".join(_odf_paragraphs(root, top_only=True))

        if len(text) < _MIN_DOC_CHARS:
            raise FetchError(
                f"这个 {ext.lstrip('.')} 里没提取到文字",
                hint="文档可能是空的，或内容全在图片里")
        return _clip(text)


# --------------------------------------------------------------------------
# 模块级辅助
# --------------------------------------------------------------------------
def _sniff_container(path: str) -> Optional[str]:
    """按文件头判断真实格式，**不看后缀**。

    返回标准后缀（``.pdf`` / ``.docx`` / ``.odt`` …）、``"ole2"``
    （Office 97-2003 那类二进制容器），或 ``None``（认不出来）。

    这是「后缀不对也能读」的关键：WPS 把 docx 存成 .wps、用户手动改名、
    浏览器下载丢了扩展名 —— 以前这些一律甩「暂不支持」，现在都能救回来。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(1200)
    except OSError:
        return None
    if not head:
        return None

    if head[:5] == b"%PDF-":
        return ".pdf"
    if head[:5].lower() == b"{\\rtf":
        return ".rtf"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        # OLE2 复合文档：doc / xls / ppt 97-2003、老 WPS 三大件都是它
        return "ole2"
    if head[:2] == b"PK":
        return _sniff_zip(path, head)

    probe = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    # MIME 头要**先判**：MHTML 的正文里就嵌着 `<html><head>…`，
    # 先判 HTML 的话它会被当成普通网页，丢掉「网页存档」这个来源标签。
    if probe.startswith((b"from:", b"mime-version:", b"content-type: multipart")):
        return ".mhtml"
    if probe.startswith((b"<!doctype html", b"<html")) or \
            b"<head>" in probe[:600] or b"<meta charset" in probe[:600]:
        return ".html"
    return None


def _sniff_zip(path: str, head: bytes) -> Optional[str]:
    """zip 内核：靠内部条目名判断到底是哪种 Open XML / ODF / EPUB。"""
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "word/document.xml" in names:
                return ".docx"
            if "ppt/presentation.xml" in names:
                return ".pptx"
            if "xl/workbook.xml" in names:
                return ".xlsx"

            mimetype = ""
            if "mimetype" in names:
                try:
                    mimetype = zf.read("mimetype").decode("ascii", "replace")
                except Exception:
                    mimetype = ""
            if "opendocument.text" in mimetype:
                return ".odt"
            if "opendocument.spreadsheet" in mimetype:
                return ".ods"
            if "opendocument.presentation" in mimetype:
                return ".odp"
            if "epub" in mimetype:
                return ".epub"

            # mimetype 条目缺失时（不少工具不写）按结构兜底
            if "content.xml" in names:
                try:
                    body = zf.read("content.xml")[:4000].decode("utf-8", "replace")
                except Exception:
                    body = ""
                if "office:presentation" in body:
                    return ".odp"
                if "office:spreadsheet" in body:
                    return ".ods"
                return ".odt"
            if "META-INF/container.xml" in names and any(
                    n.lower().endswith((".xhtml", ".html", ".opf")) for n in names):
                return ".epub"
    except Exception:
        return None
    return None


def sniff_document(path: str) -> Optional[str]:
    """公开的内容识别入口：返回真实后缀，认不出返回 ``None``。

    上传接口用它做「后缀不认识、但内容认得出」的二次判断 ——
    否则 WPS 存出来的文件和浏览器丢了后缀的下载件一律被拒。
    """
    kind = _sniff_container(path)
    if kind == "ole2":
        return _ole2_kind(path)          # 也可能认不出（返回 None）
    return kind


def _ole2_kind(path: str) -> Optional[str]:
    """OLE2 容器里装的到底是哪种 Office 文档。

    OLE2 是 doc / xls / ppt 97-2003 的**公共外壳**，文件头完全一样，
    靠 magic 分不出来。但内部流的名字各不相同：``WordDocument`` /
    ``Workbook`` / ``PowerPoint Document``。这些名字在文件里以 UTF-16LE
    明文存放，扫一遍就够用 —— 比完整实现复合文档解析划算得多。
    """
    try:
        with open(path, "rb") as f:
            blob = f.read(4 * 1024 * 1024)
    except OSError:
        return None

    def u16(s: str) -> bytes:
        return s.encode("utf-16-le")

    for stream, ext in (("WordDocument", ".doc"),
                        ("Workbook", ".xls"),
                        ("Book", ".xls"),
                        ("PowerPoint Document", ".ppt")):
        if u16(stream) in blob:
            return ext
    return None


def _legacy_office_error(name: str, real: Optional[str] = None) -> FetchError:
    """老版二进制 Office / WPS：纯 Python 没有可靠的提取路径，如实说清。"""
    if real == ".doc":
        return FetchError(
            "读不了 Word 97-2003 的老格式（.doc 是二进制，不是 XML 包）",
            hint="用 Word / WPS 打开，另存为 .docx，再拖进来")
    if real == ".ppt":
        return FetchError(
            "读不了 PowerPoint 97-2003 的老格式（.ppt 是二进制）",
            hint="用 PowerPoint / WPS 打开，另存为 .pptx，再拖进来")
    if name.lower().endswith((".wps", ".et", ".dps")):
        return FetchError(
            "读不了 WPS 老版格式（.wps / .et / .dps 的二进制内核）",
            hint="用 WPS 打开后「另存为」.docx / .xlsx / .pptx，再拖进来；\n"
                 "新版 WPS 若另存为的就是这三种格式，直接拖也能读")
    return FetchError(
        "读不了 Office 97-2003 的老格式（.doc / .ppt 是二进制，不是 XML 包）",
        hint="用 Word / WPS 打开，另存为 .docx（演示存 .pptx、表格存 .xlsx），"
             "再拖进来")


def _odf_paragraphs(root, top_only: bool = True) -> list[str]:
    """取 ODF 里的段落文本。段落可能互相嵌套，只取最外层避免重复计数。"""
    inner = set()
    if top_only:
        for el in root.iter():
            if el.tag.endswith(("}p", "}h")):
                for child in el.iter():
                    if child is not el:
                        inner.add(id(child))
    out = []
    for el in root.iter():
        if not el.tag.endswith(("}p", "}h")) or id(el) in inner:
            continue
        t = "".join(el.itertext()).strip()
        if t:
            out.append(t)
    return out


_RTF_SKIP_DEST = frozenset((
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object",
    "themedata", "datastore", "shppict", "nonshppict", "listtable",
    "listoverridetable", "rsidtbl", "generator", "filetbl", "revtbl",
    "xmlnstbl", "latentstyles", "fldinst",
))

_RTF_SYMBOLS = {
    "emdash": "—", "endash": "–", "lquote": "‘", "rquote": "’",
    "ldblquote": "“", "rdblquote": "”", "bullet": "•",
}


def _rtf_to_text(raw: bytes) -> str:
    """RTF → 纯文本。

    RTF 里中文有两条写法：``\\uNNNN``（Unicode 转义）和 ``\\'hh``（当前代码页
    的字节）。所以不能先 decode 再解析 —— 那样 ``\\'hh`` 已经被解成乱码字符了。
    这里的做法是：**先按字节扫描剥离控制字**，普通字符和 ``\\'hh`` 都攒回字节
    缓冲，最后按文档声明的代码页（``\\ansicpg``）统一解码。
    """
    src = raw.decode("latin-1")        # latin-1 把字节 1:1 映成字符，不丢信息
    codepage = 1252
    m = re.search(r"\\ansicpg(\d+)", src[:4096])
    if m:
        codepage = int(m.group(1))

    parts: list = []                   # 元素是 str（来自 \u）或 bytes（来自字节流）
    buf = bytearray()

    def flush() -> None:
        if buf:
            parts.append(bytes(buf))
            buf.clear()

    def emit(s: str) -> None:
        flush()
        parts.append(s)

    stack: list[bool] = []
    skipping = False
    pending_skip = False               # 刚遇到 \* 或跳过目标字，下个 { 要整组跳过
    uc = 1
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "{":
            stack.append(skipping)
            if pending_skip:
                skipping = True
                pending_skip = False
            i += 1
            continue
        if c == "}":
            if stack:
                skipping = stack.pop()
            i += 1
            continue
        if c == "\\":
            nxt = src[i + 1] if i + 1 < n else ""
            if nxt in ("\\", "{", "}"):
                if not skipping:
                    buf += nxt.encode("latin-1")
                i += 2
                continue
            if nxt == "'":                                   # \'hh —— 一个字节
                if not skipping:
                    try:
                        buf.append(int(src[i + 2:i + 4], 16))
                    except ValueError:
                        pass
                i += 4
                continue
            if nxt == "*":                                   # \* 可忽略的目标
                pending_skip = True
                i += 2
                continue
            cm = re.match(r"\\([A-Za-z]+)(-?\d+)?", src[i:])
            if cm:
                word, arg = cm.group(1), cm.group(2)
                i += cm.end()
                if i < n and src[i] == " ":
                    i += 1                                   # 控制字后的分隔空格
                if word == "uc" and arg is not None:
                    uc = int(arg)
                elif word in _RTF_SKIP_DEST:
                    pending_skip = True
                elif word == "u" and arg is not None:
                    code = int(arg)
                    if code < 0:
                        code += 65536
                    if not skipping:
                        emit(chr(code))
                    # 丢掉 \uc 个「替代字符」：它们是同一个字在当前代码页里的
                    # 退化写法（中文 RTF 常写成 \'xx），不丢就会重复或乱码
                    k = 0
                    while k < uc and i < n:
                        if src[i] == "\\" and src[i + 1:i + 2] == "'":
                            i += 4
                        elif src[i] == "\\" and src[i + 1:i + 2].isalpha():
                            break                            # 是新的控制字
                        elif src[i] == "\\":
                            i += 2
                        else:
                            i += 1
                        k += 1
                elif word in ("par", "line", "sect", "page"):
                    if not skipping:
                        emit("\n")
                elif word == "tab":
                    if not skipping:
                        emit("\t")
                elif word in _RTF_SYMBOLS:
                    if not skipping:
                        emit(_RTF_SYMBOLS[word])
                continue
            i += 2                                           # 其它转义符号，忽略
            continue
        if not skipping:
            buf += c.encode("latin-1", "replace")
        i += 1

    flush()

    chunks = []
    for p in parts:
        if isinstance(p, bytes):
            for enc in (f"cp{codepage}", "gbk", "utf-8", "latin-1"):
                try:
                    chunks.append(p.decode(enc))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                chunks.append(p.decode("latin-1", "replace"))
        else:
            chunks.append(p)
    text = "".join(chunks).replace("\r", "\n")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_SUB_TIME = re.compile(r"\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}")
_SUB_HEADER = re.compile(
    r"^(WEBVTT|NOTE|STYLE|REGION|ScriptType|Title|PlayRes|WrapStyle|"
    r"ScaledBorderAndShadow|Collisions|Original Script|Style|Format|"
    r"Comment|Dialogue)\b")


def _clean_subtitle(text: str) -> str:
    """字幕文件 → 纯正文。

    留时间轴的话，正文会变成一堆 `00:00:01,200 --> 00:00:03,400`，
    既挤占模型的输入长度，又干扰摘要质量。
    """
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s:
            continue
        if re.fullmatch(r"\d{1,6}", s):                 # SRT 的序号行
            continue
        if "-->" in s or _SUB_TIME.match(s):            # 时间轴 / VTT 时间标记
            continue
        if s.startswith("[") and s.endswith("]"):       # ASS 的 [Script Info] 等
            continue
        head = _SUB_HEADER.match(s)
        if head:
            # Dialogue 行**要留**：正文就在它后面（切掉前置字段即可）；
            # 其余是头部元信息，丢掉
            if head.group(1) == "Dialogue":
                s = s.split(",", 9)[-1]
            else:
                continue
        s = re.sub(r"\{[^}]*\}", "", s)                 # {\an8} {\pos(1,2)} 等
        s = re.sub(r"<[^>]+>", "", s)                   # <i> <b> <font …>
        s = s.strip()
        if s:
            lines.append(s)

    # 滚动字幕会把上一句重复一遍，去重后摘要才不会被同一句话带偏
    dedup: list[str] = []
    for s in lines:
        if not dedup or dedup[-1] != s:
            dedup.append(s)
    return "\n".join(dedup)
def _iter_shapes(shapes, _depth: int = 0):
    """递归展开 PPT 形状。

    **组合（group）里的形状不能漏** —— 演讲者很喜欢把并列要点打成一个组，
    只遍历顶层的话，一页可能只剩个标题，正文全丢。
    """
    if _depth > 6:                 # 防病态嵌套
        return
    for shape in shapes:
        yield shape
        # GroupShape 才有 .shapes；普通形状（文本框/图片/表格）没有
        if hasattr(shape, "shapes"):
            yield from _iter_shapes(shape.shapes, _depth + 1)


def _epub_spine(zf) -> list[str]:
    """按 EPUB 的阅读顺序返回章节文件名；解析不出来就返回空（由调用方兜底）。"""
    import posixpath

    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
    except KeyError:
        return []
    m = re.search(r'full-path=["\']([^"\']+)["\']', container)
    if not m:
        return []
    opf_path = m.group(1)
    base = posixpath.dirname(opf_path)
    try:
        opf = zf.read(opf_path).decode("utf-8", "replace")
    except KeyError:
        return []

    id2href: dict[str, str] = {}
    for im in re.finditer(r"<item\b[^>]*>", opf):
        tag = im.group(0)
        mid = re.search(r'id=["\']([^"\']+)["\']', tag)
        href = re.search(r'href=["\']([^"\']+)["\']', tag)
        if mid and href:
            id2href[mid.group(1)] = href.group(1)

    order: list[str] = []
    for rm in re.finditer(r"<itemref\b[^>]*>", opf):
        idr = re.search(r'idref=["\']([^"\']+)["\']', rm.group(0))
        if not idr or idr.group(1) not in id2href:
            continue
        href = id2href[idr.group(1)].split("#")[0]
        full = posixpath.normpath(posixpath.join(base, href)) if base else href
        order.append(full.replace("\\", "/"))
    return order

