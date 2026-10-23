# -*- coding: utf-8 -*-
"""冒烟测试：拖拽进来的文档能不能被正确解析。

覆盖四件事：

1. **各格式真能读出文字** —— 测试文件是当场生成的（真 PDF / 真 docx /
   真 xlsx / 真 xls / 真 pptx / 真 epub / 真 ODF / 真 RTF），不是 mock。
   mock 掉的解析器测不出「库没装」「页数不对」这类真问题。
2. **读不到文字时必须报错，不能编** —— 空白 PDF（扫描件）如果放行，
   标题会退化成文件名，然后让大模型对着文件名编一篇总结。
3. **后缀不可信** —— 文件头才是准绳。WPS 存成 `.wps/.et/.dps`、后缀改错、
   后缀丢失都要能读；真老版 OLE2 要给出「另存为」指引。
4. **上传接口往返** —— 前端拖文件走的是 ``POST /api/upload``，
   它得把文件落盘、返回可读路径，并且对「后缀陌生但认得出」的文件同样放行。

用法::

    python tests/smoke_doc_formats.py
"""

import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vsum.core.config import Config                    # noqa: E402
from vsum.core.context import Context                  # noqa: E402
from vsum.core.errors import FetchError                # noqa: E402
from vsum.platforms.article import PlainTextAdapter    # noqa: E402

PASS, FAIL, SKIP = 0, 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  SKIP {name} —— {why}")


# --------------------------------------------------------------------------
# 生成测试文件（全部当场造，不依赖外部素材）
# --------------------------------------------------------------------------
MARK = "量子纠缠的贝尔不等式检验"


def make_pdf(path: str, text: str) -> None:
    """手写一个最小合法 PDF。pypdf 只能读不能写，所以自己拼字节。"""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length STREAMLEN >>\nstream\nSTREAMDATA\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = ("BT /F1 18 Tf 72 700 Td (" + text + ") Tj ET").encode("ascii", "replace")
    objs[3] = (b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n"
               + body + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objs) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    with open(path, "wb") as f:
        f.write(bytes(out))


def make_blank_pdf(path: str) -> None:
    """有页面、但一个字都抽不出来的 PDF（模拟扫描件）。"""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objs) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    with open(path, "wb") as f:
        f.write(bytes(out))


def make_epub(path: str, title: str, chapters: list) -> None:
    """手写一个最小 EPUB（zip + xhtml），顺便验证 spine 顺序解析。"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        items, refs = [], []
        for i, body in enumerate(chapters, 1):
            name = f"OEBPS/ch{i}.xhtml"
            z.writestr(name,
                       '<?xml version="1.0" encoding="utf-8"?><html><body><p>'
                       + body + '</p></body></html>')
            items.append(f'<item id="c{i}" href="ch{i}.xhtml" '
                         f'media-type="application/xhtml+xml"/>')
            refs.append(f'<itemref idref="c{i}"/>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package version="2.0" '
                   'xmlns="http://www.idpf.org/2007/opf" unique-identifier="id">'
                   f'<metadata><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">{title}'
                   '</dc:title></metadata><manifest>' + "".join(items)
                   + '</manifest><spine>' + "".join(refs) + '</spine></package>')


def make_xlsx(path: str, rows: list) -> None:
    """真 .xlsx（openpyxl 写出）。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "实验数据"
    for r in rows:
        ws.append(r)
    wb.save(path)


def make_xls(path: str, rows: list) -> None:
    """真 .xls —— 旧版 OLE2 二进制格式，只有 xlwt 造得出来。"""
    import xlwt
    wb = xlwt.Workbook()
    ws = wb.add_sheet("旧表")
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            ws.write(i, j, v)
    wb.save(path)


def make_odf(path: str, kind: str, data: list) -> None:
    """手写最小 OpenDocument（zip + content.xml），三种内核都要覆盖。"""
    mime = {
        "odt": "application/vnd.oasis.opendocument.text",
        "ods": "application/vnd.oasis.opendocument.spreadsheet",
        "odp": "application/vnd.oasis.opendocument.presentation",
    }[kind]
    if kind == "ods":
        body = ("<office:body><office:spreadsheet>"
                '<table:table table:name="表1">'
                + "".join(
                    "<table:table-row>"
                    + "".join(f"<table:table-cell><text:p>{c}</text:p>"
                              "</table:table-cell>" for c in row)
                    + "</table:table-row>"
                    for row in data)
                + "</table:table></office:spreadsheet></office:body>")
    elif kind == "odp":
        body = ("<office:body><office:presentation>"
                + "".join(
                    f'<draw:page draw:name="p{i}">'
                    + "".join(f"<text:p>{t}</text:p>" for t in page)
                    + "</draw:page>"
                    for i, page in enumerate(data, 1))
                + "</office:presentation></office:body>")
    else:
        body = ("<office:body><office:text>"
                + "".join(f"<text:p>{p}</text:p>" for p in data)
                + "</office:text></office:body>")

    content = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<office:document-content"
        ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
        ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
        ' xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0">'
        + body + "</office:document-content>")

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", mime)
        z.writestr("META-INF/manifest.xml", '<?xml version="1.0"?><manifest/>')
        z.writestr("content.xml", content)


def make_rtf(path: str, lines: list) -> None:
    """造中文 RTF。中文按 Word 的写法写成 ``\\uNNNN?``（后跟一个占位符）。"""
    def esc(s: str) -> str:
        out = []
        for ch in s:
            if ord(ch) < 128:
                out.append({"\\": "\\\\", "{": "\\{", "}": "\\}"}.get(ch, ch))
            else:
                out.append(f"\\u{ord(ch)}?")
        return "".join(out)

    src = ("{\\rtf1\\ansi\\ansicpg936\\deff0"
           "{\\fonttbl{\\f0\\fnil\\fcharset134 Microsoft YaHei;}}"
           "\\viewkind4\\uc1\\pard\\f0\\fs24 "
           + "".join(esc(t) + "\\par\n" for t in lines) + "}")
    with open(path, "wb") as f:
        f.write(src.encode("latin-1", "replace"))


def make_mhtml(path: str, html: str) -> None:
    """造 MHTML（浏览器「网页另存为单个文件」就是这个结构）。"""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("From: <saved by browser>\r\n"
                "MIME-Version: 1.0\r\n"
                'Content-Type: multipart/related; boundary="----BOUNDARY"\r\n'
                "\r\n"
                "------BOUNDARY\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Content-Transfer-Encoding: 8bit\r\n"
                "Content-Location: file:///C:/page.html\r\n"
                "\r\n" + html + "\r\n"
                "------BOUNDARY--\r\n")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="vsum_doc_")
    ctx = Context(Config())
    ad = PlainTextAdapter(ctx)

    # ---------------- 1. 文本类 ----------------
    print("\n=== 1. 纯文本类（txt / md / csv）直接读 ===")
    md = os.path.join(tmp, "笔记.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("# 标题行\n\n" + MARK + "，这是正文。" * 6)
    text, kind = ad._read(md)
    check("md 能读", MARK in text and kind == "md", f"kind={kind}")
    check("md 被 match 命中", PlainTextAdapter.match(md))

    gbk = os.path.join(tmp, "老编码.txt")
    with open(gbk, "w", encoding="gbk") as f:
        f.write(MARK * 20)
    text, kind = ad._read(gbk)
    check("GBK 编码文本能读（中文不乱码）", MARK in text, f"kind={kind}")

    # ---------------- 2. PDF ----------------
    print("\n=== 2. PDF ===")
    pdf = os.path.join(tmp, "论文.pdf")
    make_pdf(pdf, "Bell inequality test of quantum entanglement")
    try:
        text, kind = ad._read(pdf)
        check("PDF 读出文字", "Bell inequality" in text, f"实际={text[:60]!r}")
        check("PDF kind 正确", kind == "pdf")
    except FetchError as e:
        skip("PDF 读出文字", f"缺解析器：{e}")

    blank = os.path.join(tmp, "扫描件.pdf")
    make_blank_pdf(blank)
    try:
        ad._read(blank)
        check("空白/扫描 PDF 必须报错", False, "居然读出了内容")
    except FetchError as e:
        check("空白/扫描 PDF 必须报错", True)
        check("错误提示指向 OCR", "OCR" in (e.hint or ""), f"hint={e.hint!r}")
        # 关键：不能退化成「拿文件名当标题出总结」
        check("错误信息不含文件名", "扫描件" not in str(e), f"实际={e!r}")

    # ---------------- 3. Word ----------------
    print("\n=== 3. Word (.docx) ===")
    docx_path = os.path.join(tmp, "报告.docx")
    try:
        import docx
        d = docx.Document()
        d.add_heading("实验报告", level=1)
        d.add_paragraph(MARK + "。" * 10)
        t = d.add_table(rows=1, cols=2)
        t.cell(0, 0).text = "变量"
        t.cell(0, 1).text = "观测值"
        d.add_paragraph("表格后面还有一段正文，用来验证顺序没被打乱。")
        d.save(docx_path)
        text, kind = ad._read(docx_path)
        check("docx 读出段落", MARK in text, f"实际={text[:60]!r}")
        check("docx 读出表格", "变量 | 观测值" in text, f"实际={text[:200]!r}")
        check("docx 表格后正文未被吃掉", "表格后面还有一段正文" in text)
        check("docx 段落顺序：标题在表格前",
              text.index("实验报告") < text.index("变量 | 观测值"))
    except ImportError:
        skip("docx 解析", "未安装 python-docx")

    # ---------------- 4. PPT ----------------
    print("\n=== 4. PPT (.pptx) ===")
    pptx_path = os.path.join(tmp, "汇报.pptx")
    try:
        from pptx import Presentation
        from pptx.util import Inches
        prs = Presentation()
        s1 = prs.slides.add_slide(prs.slide_layouts[5])
        s1.shapes.title.text = "第一页标题"
        tb = s1.shapes.add_textbox(Inches(1), Inches(2), Inches(5), Inches(1))
        tb.text_frame.text = MARK
        s1.notes_slide.notes_text_frame.text = "这页讲的是纠缠检验"
        s2 = prs.slides.add_slide(prs.slide_layouts[5])
        s2.shapes.title.text = "第二页标题"
        prs.save(pptx_path)
        text, kind = ad._read(pptx_path)
        check("pptx 读出标题与正文", MARK in text and "第一页标题" in text,
              f"实际={text[:120]!r}")
        check("pptx 读出演讲备注", "这页讲的是纠缠检验" in text)
        check("pptx 分页标注", text.count("【第 ") == 2, f"实际={text[:200]!r}")
    except ImportError:
        skip("pptx 解析", "未安装 python-pptx")

    # ---------------- 5. EPUB ----------------
    print("\n=== 5. EPUB ===")
    epub_path = os.path.join(tmp, "电子书.epub")
    make_epub(epub_path, "测试书", ["第一章：" + MARK, "第二章：另一段内容"])
    text, kind = ad._read(epub_path)
    check("epub 读出章节", MARK in text and "第二章" in text, f"实际={text[:80]!r}")
    check("epub 按 spine 顺序（第一章在前）",
          text.index("第一章") < text.index("第二章"))

    # ---------------- 6. 不支持 / 特别的类型 ----------------
    print("\n=== 6. 边界情况 ===")
    weird = os.path.join(tmp, "怪东西.xyz")
    with open(weird, "wb") as f:
        f.write(b"\x00\x01\x02binary")
    try:
        ad._read(weird)
        check("未知后缀明确拒绝（不硬读二进制）", False, "居然读了")
    except FetchError as e:
        check("未知后缀明确拒绝（不硬读二进制）",
              "不支持" in str(e), f"实际={e!r}")

    doc = os.path.join(tmp, "老版.doc")
    with open(doc, "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0" + b"\x00" * 200)     # OLE2 头
    try:
        ad._read(doc)
        check(".doc 给出明确提示", False, "居然没报错")
    except FetchError as e:
        check(".doc 给出明确提示", "docx" in (e.hint or "").lower(),
              f"hint={e.hint!r}")

    empty = os.path.join(tmp, "空文件.txt")
    open(empty, "w").close()
    try:
        ad.resolve(empty)
        check("空文件报错而不是编总结", False, "居然返回了元数据")
    except FetchError as e:
        check("空文件报错而不是编总结", True, f"{e}")

    # ---------------- 7. 路由：拖进来的路径要走 plain 适配器 ----------------
    print("\n=== 7. 流水线路由 ===")
    from vsum.core.pipeline import Pipeline
    from vsum.extensions import load_all
    load_all()
    p = Pipeline(Context(Config()))
    check("pdf 路径 → plain 适配器",
          p.detect(pdf) is not None and p.detect(pdf).key == "plain")
    check("docx 路径 → plain 适配器", p.detect(docx_path).key == "plain")
    check("带空格的中文路径也能识别",
          PlainTextAdapter.match("D:\\我的 文档\\某 报告.pdf"))
    check("正文粘贴仍走 plain", PlainTextAdapter.match("随便一段正文" * 20))

    # ---------------- 9. 标题取值（离线，不跑模型） ----------------
    print("\n=== 9. 标题取值 ===")
    # 文件：标题取文件名，不能拿正文首行（PPT 首行是「【第 1 页】」）
    check("PDF 标题 = 文件名（去后缀）",
          ad.resolve(pdf).title == "论文", f"实际={ad.resolve(pdf).title!r}")
    check("PPT 标题 = 文件名，不是「【第 N 页】」",
          ad.resolve(pptx_path).title == "汇报",
          f"实际={ad.resolve(pptx_path).title!r}")
    ppt_text, _k = ad._read(pptx_path)
    check("PPT 页码标记仍保留在正文里（没丢信息）", "【第 1 页】" in ppt_text)
    # 文本类：正文里显式的 # 一级标题比「文件名」更准
    check("Markdown 文件取正文的 # 标题",
          ad.resolve(md).title == "标题行", f"实际={ad.resolve(md).title!r}")
    # 直贴正文（非文件）：仍按内容猜标题
    check("直贴正文仍按内容定标题",
          ad.resolve("随便一段够长的正文" * 8).title != "")
    # 提示语要按真实来源措辞（以前写死「网页正文抽取」，拖 PDF 也说网页文章）
    if os.path.isfile(docx_path):
        notes = ad.fetch_subtitle(ad.resolve(docx_path)).notes
        check("docx 的说明写「Word」，不写「网页」",
              any("Word" in n for n in notes)
              and not any("网页文章" in n for n in notes), f"实际={notes}")
    else:
        skip("docx 的说明措辞", "未生成 docx（缺 python-docx）")
    notes_pdf = ad.fetch_subtitle(ad.resolve(pdf)).notes
    check("pdf 的说明写「PDF 文字层」",
          any("PDF 文字层" in n for n in notes_pdf), f"实际={notes_pdf}")

    # 端到端：扫描件必须报错，且**不能**去回退到 yt-dlp
    # （本地路径没有 URL 协议头，yt-dlp 只会报 Unsupported url scheme，
    #   那行 WARNING 会让人误以为程序在联网解析本地文件）
    from vsum.core.pipeline import Pipeline as _P
    try:
        _P(Context(Config())).run(blank)
        check("端到端：扫描件报错而非出总结", False, "居然成功了")
    except FetchError as e:
        check("端到端：扫描件报错而非出总结", True)
        check("端到端：错误仍是 OCR 提示", "OCR" in (e.hint or ""), f"hint={e.hint!r}")

    # ---------------- 10. 上传接口（前端拖文件走的就是它） ----------------
    print("\n=== 10. POST /api/upload 往返 ===")
    from vsum.ui.launcher import launch_background
    import urllib.parse

    base, _port = launch_background(Config(), port=0)

    def upload(local_path: str, send_name: str = "") -> tuple:
        payload = open(local_path, "rb").read()
        req = urllib.request.Request(base + "api/upload", data=payload, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("X-Filename",
                       urllib.parse.quote(send_name or os.path.basename(local_path)))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8")), r.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8")), e.code

    md2 = os.path.join(tmp, "拖拽测试.md")
    with open(md2, "w", encoding="utf-8") as f:
        f.write("# 拖拽进来的文档\n\n" + MARK + "。" * 20)
    info, code = upload(md2)
    check("上传返回 200", code == 200, f"code={code} body={info}")
    check("返回落盘路径", bool(info.get("path")) and os.path.isfile(info.get("path", "")),
          f"body={info}")
    check("保留原始文件名（中文不乱码）",
          os.path.basename(info.get("path", "")) == "拖拽测试.md",
          f"实际={os.path.basename(info.get('path', ''))!r}")
    if info.get("path"):
        read_back, _k = ad._read(info["path"])
        check("落盘内容可被解析器读回", MARK in read_back)
        check("上传后的路径 → plain 适配器", p.detect(info["path"]).key == "plain")

    bad, code = upload(md2, "病毒.exe")
    check("不支持的扩展名被拒（400）", code == 400, f"code={code} body={bad}")
    check("拒绝时说明支持哪些格式", "支持" in (bad.get("hint") or ""), f"body={bad}")

    # 页面本身要带上拖拽逻辑 —— 打包时 index.html 是 data 文件，
    # 漏收的话接口还在、界面却没有拖拽入口，很难从日志看出来。
    page = urllib.request.urlopen(base, timeout=20).read().decode("utf-8")
    check("首页含拖拽遮罩元素", 'id="dropMask"' in page)
    check("首页含选择文件按钮", 'id="btnFile"' in page)
    check("首页含上传逻辑", "/api/upload" in page and "readAsArrayBuffer" in page)

    # 目录穿越：文件名里塞路径，必须只留 basename
    evil, code = upload(md2, "../../../../evil.md")
    check("文件名里的路径被剥掉（防目录穿越）",
          code == 200 and os.path.basename(evil.get("path", "")) == "evil.md",
          f"code={code} body={evil}")

    # 空文件
    empty_up = os.path.join(tmp, "empty.dat")
    open(empty_up, "w").close()
    _e, code = upload(empty_up, "空.dat")
    check("空文件被拒（400）", code == 400, f"code={code}")

    # ---------------- 11. Excel ----------------
    print("\n=== 11. Excel（.xlsx / .xls）===")
    xlsx = os.path.join(tmp, "实验数据.xlsx")
    make_xlsx(xlsx, [["变量", "观测值"], ["S 值", 2.7], [MARK, "与预测一致"]])
    text, kind = ad._read(xlsx)
    check("xlsx 读出工作表名", "【工作表：实验数据】" in text, f"实际={text[:140]!r}")
    check("xlsx 读出单元格内容", MARK in text and "2.7" in text,
          f"实际={text[:160]!r}")
    check("xlsx kind 正确", kind == "xlsx", f"kind={kind}")

    xls = os.path.join(tmp, "旧版表格.xls")
    try:
        make_xls(xls, [["变量", "观测值"], [MARK, 2.7]])
    except ImportError:
        xls = ""
    if xls:
        text, kind = ad._read(xls)
        check("xls（真 OLE2 二进制）读出单元格", MARK in text and "2.7" in text,
              f"实际={text[:160]!r}")
        check("xls 不该被当成「老格式读不了」", kind == "xls", f"kind={kind}")
    else:
        skip("xls 读到内容", "造 .xls 需要 dev 依赖 xlwt，未安装")

    # ---------------- 12. OpenDocument ----------------
    print("\n=== 12. OpenDocument（.odt / .ods / .odp）===")
    odt = os.path.join(tmp, "开放文档.odt")
    make_odf(odt, "odt", [MARK + "的第一段。", "第二段讲的是实验设计。"])
    text, kind = ad._read(odt)
    check("odt 读出段落", MARK in text and "第二段" in text, f"实际={text[:140]!r}")
    check("odt kind 正确", kind == "odt", f"kind={kind}")

    ods = os.path.join(tmp, "开放表格.ods")
    make_odf(ods, "ods", [["变量", "观测值"], [MARK, "与预测一致"]])
    text, kind = ad._read(ods)
    check("ods 读出表格行（单元格用 | 分隔）", "变量 | 观测值" in text,
          f"实际={text[:160]!r}")
    check("ods kind 正确", kind == "ods", f"kind={kind}")

    odp = os.path.join(tmp, "开放演示.odp")
    make_odf(odp, "odp", [["第一页：" + MARK, "副标题"], ["第二页：结论"]])
    text, kind = ad._read(odp)
    check("odp 读出两页文字", MARK in text and "第二页" in text,
          f"实际={text[:140]!r}")
    check("odp 段落没被重复计入（嵌套去重）",
          text.count(MARK) == 1, f"实际={text!r}")

    # ---------------- 13. RTF ----------------
    print("\n=== 13. RTF（富文本）===")
    rtf = os.path.join(tmp, "富文本.rtf")
    make_rtf(rtf, [MARK + "的实验设计要点。", "光源制备、测量基选择与误差分析。"])
    text, kind = ad._read(rtf)
    check("rtf 中文不乱码（\\uNNNN 转义被还原）", MARK in text, f"实际={text[:140]!r}")
    check("rtf 段落换行被还原（\\par）", "\n" in text, f"实际={text[:140]!r}")
    check("rtf 剥掉字体表等控制信息",
          "fcharset" not in text and "Microsoft YaHei" not in text,
          f"实际={text[:200]!r}")
    check("rtf kind 正确", kind == "rtf", f"kind={kind}")

    # ---------------- 14. 字幕 ----------------
    print("\n=== 14. 字幕（.srt / .vtt / .ass）===")
    srt = os.path.join(tmp, "课程.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,500\n" + MARK + "讲的是什么\n\n"
                "2\n00:00:03,500 --> 00:00:06,000\n它说的是局域隐变量不成立\n\n"
                "3\n00:00:06,000 --> 00:00:08,000\n它说的是局域隐变量不成立\n")
    text, kind = ad._read(srt)
    check("srt 洗掉序号与时间轴",
          "-->" not in text and "00:00" not in text, f"实际={text[:140]!r}")
    check("srt 留下正文", MARK in text and "局域隐变量" in text,
          f"实际={text[:140]!r}")
    check("srt 相邻重复行被合并（滚动字幕）",
          text.count("它说的是局域隐变量不成立") == 1, f"实际={text!r}")

    vtt = os.path.join(tmp, "网页字幕.vtt")
    with open(vtt, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n" + MARK + "\n")
    text, _k = ad._read(vtt)
    check("vtt 去掉 WEBVTT 头与时间戳",
          MARK in text and "WEBVTT" not in text and "-->" not in text,
          f"实际={text[:140]!r}")

    ass = os.path.join(tmp, "样式字幕.ass")
    with open(ass, "w", encoding="utf-8") as f:
        f.write("[Script Info]\nTitle: 课程\nScriptType: v4.00+\n\n"
                "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
                "MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
                + MARK + "\n")
    text, _k = ad._read(ass)
    check("ass 切掉 Dialogue 前置字段留下正文", MARK in text,
          f"实际={text[:200]!r}")
    check("ass 丢掉 [Script Info] 等头部",
          "Script Info" not in text and "Format:" not in text,
          f"实际={text[:200]!r}")

    # ---------------- 15. 网页存档 ----------------
    print("\n=== 15. 网页存档（.mhtml）===")
    mhtml = os.path.join(tmp, "网页存档.mhtml")
    make_mhtml(mhtml,
               "<html><head><title>存档页</title></head><body><article>"
               f"<h1>{MARK}</h1><p>" + ("这是从网页存档里抽出来的正文段落。" * 8)
               + "</p></article></body></html>")
    text, kind = ad._read(mhtml)
    check("mhtml 抽出 HTML 正文", MARK in text, f"实际={text[:140]!r}")
    check("mhtml kind 正确", kind == "mhtml", f"kind={kind}")

    # ---------------- 16. 按内容识别（后缀不可信）----------------
    print("\n=== 16. 按内容识别（后缀名不可信）===")

    def copy_as(src: str, name: str) -> str:
        dst = os.path.join(tmp, name)
        with open(src, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())
        return dst

    # 真实存在、但既没后缀线索、内容也认不出的二进制（拿 PNG 头冒充）
    unknown = os.path.join(tmp, "真的不认识.xyz")
    with open(unknown, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    # WPS 存出来的 .wps 内核其实就是 docx；.et 是 xlsx；.dps 是 pptx
    fake_wps = copy_as(docx_path, "冒充.wps")
    text, kind = ad._read(fake_wps)
    check("docx 改名为 .wps 也能读", MARK in text, f"实际={text[:90]!r}")
    check(".wps 的 kind 按真实内容给出 docx", kind == "docx", f"kind={kind}")

    fake_et = copy_as(xlsx, "冒充.et")
    text, kind = ad._read(fake_et)
    check("xlsx 改名为 .et 也能读", MARK in text, f"实际={text[:90]!r}")
    check(".et 的 kind 按真实内容给出 xlsx", kind == "xlsx", f"kind={kind}")

    fake_dps = copy_as(pptx_path, "冒充.dps")
    text, kind = ad._read(fake_dps)
    check("pptx 改名为 .dps 也能读", MARK in text, f"实际={text[:90]!r}")
    check(".dps 的 kind 按真实内容给出 pptx", kind == "pptx", f"kind={kind}")

    noext = copy_as(docx_path, "浏览器丢了后缀")
    text, kind = ad._read(noext)
    check("完全没有后缀也能按内容识别",
          MARK in text and kind == "docx", f"kind={kind} 实际={text[:60]!r}")

    if xls:
        # 真 .xls 是 OLE2，和 .doc/.ppt 共用文件头 —— 靠内部流名（Workbook）
        # 才能认出来，光看 magic 只能判到「这是老版 Office」这一层
        hidden_xls = copy_as(xls, "改名叫别的.xyz")
        text, kind = ad._read(hidden_xls)
        check("真 .xls 换成陌生后缀也能读（靠 OLE2 内部流名）",
              MARK in text and kind == "xls",
              f"kind={kind} 实际={text[:80]!r}")

    from vsum.platforms.article import sniff_document
    check("sniff_document 认得 docx", sniff_document(docx_path) == ".docx",
          f"实际={sniff_document(docx_path)!r}")
    check("sniff_document 对认不出的二进制返回 None",
          sniff_document(unknown) is None, f"实际={sniff_document(unknown)!r}")

    # 上传接口这道闸门也要有同样的宽容度 ——
    # 否则后端能力有了，用户却在「上传」这一步就被拦住
    weird_doc = copy_as(docx_path, "改了后缀.xyz")
    info2, code2 = upload(weird_doc, "改了后缀.xyz")
    check("后缀陌生但内容是 docx → 上传放行", code2 == 200,
          f"code={code2} body={info2}")
    if info2.get("path"):
        check("放行后的文件能被解析器读回",
              MARK in ad._read(info2["path"])[0], f"body={info2}")

    body_bad, code_bad = upload(unknown, "真的不认识.xyz")
    check("内容和后缀都不认识 → 上传仍拒（400）", code_bad == 400,
          f"code={code_bad}")
    hint_txt = body_bad.get("hint") or ""
    check("拒收提示按类别列出格式（不是一长串后缀）",
          "PDF" in hint_txt and "字幕" in hint_txt, f"hint={hint_txt[:80]!r}")

    # 真·老版 OLE2 的 .wps 没有可靠的纯 Python 路径 —— 要给出「另存为」指引，
    # 而不是一句「解析失败」
    old_wps = os.path.join(tmp, "老版.wps")
    with open(old_wps, "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 300)
    try:
        ad._read(old_wps)
        check("老版 .wps 给出另存为指引", False, "居然没报错")
    except FetchError as e:
        check("老版 .wps 给出另存为指引",
              "另存为" in (e.hint or ""), f"hint={e.hint!r}")

    # 真实存在但不认识的后缀+内容，仍要明确拒绝（别硬读二进制）
    try:
        ad._read(unknown)
        check("内容也认不出的文件仍明确拒绝", False, "居然读了")
    except FetchError as e:
        check("内容也认不出的文件仍明确拒绝",
              "不支持" in str(e), f"实际={e!r}")

    # ---------------- 17. 文档不冒充「时间线」----------------
    print("\n=== 17. 文档类内容不该标时间戳 ===")
    md_doc = os.path.join(tmp, "无时间轴笔记.md")
    with open(md_doc, "w", encoding="utf-8") as f:
        f.write("# 打包笔记\n\n" + "这是第一段的内容。" * 12
                + "\n\n" + "这是第二段的内容。" * 12)
    tr = ad.fetch_subtitle(ad.resolve(md_doc))
    check("文档的 transcript 来源是 article", tr.source == "article",
          f"source={tr.source}")
    check("仍然切了段（供「分段细讲」按顺序列点）",
          len(tr.segments) >= 2, f"段数={len(tr.segments)}")
    check("但 has_timeline 为 False（文档的段落时间是按字数估的，不该标 mm:ss）",
          tr.has_timeline is False, f"实际={tr.has_timeline!r}")

    # 端到端（离线抽取式，不耗额度）确认输出里没有假时间线
    from vsum.core.pipeline import Pipeline as _Pipe
    cfg_off = Config()
    cfg_off.set("llm.api_key", "")        # 强制走本地抽取式，不联网
    cfg_off.set("obsidian.enabled", False)
    ctx_off = Context(cfg_off)
    try:
        r_off = _Pipe(ctx_off).run(md_doc)
        md_off = r_off.markdown or ""
        check("离线摘要标题写「文章总结」",
              md_off.lstrip().startswith("# 文章总结"), f"实际={md_off[:40]!r}")
        check("离线摘要不再输出「时间线概览」",
              "时间线概览" not in md_off, "文档不该有时间线")
        # 别把这个能力一起关掉：视频来源必须照旧保留时间轴
        from vsum.core.models import Segment as _Seg, Transcript as _Tr
        check("视频来源（subtitle）仍保留时间线",
              _Tr(source="subtitle", text="x",
                  segments=[_Seg(start=0.0, end=1.0, text="x")]).has_timeline is True)
        check("文章类来源一律没有时间线",
              _Tr(source="article", text="x",
                  segments=[_Seg(start=0.0, end=1.0, text="x")]).has_timeline is False)

        # 提示词模板本身也不能留时间戳示例 —— 模型会照抄那个 `[00:00]` 前缀，
        # 结果文档的分段细讲每行都顶着假时间戳（这个 bug 是打包版实测抓出来的）
        from vsum.llm.base import build_prompt, STYLE_DETAILED
        tr_art = _Tr(source="article", text="正文内容。" * 20,
                     segments=[_Seg(start=0.0, end=2.0, text="正文内容。" * 20)])
        _sys, user_art = build_prompt(r_off.meta, tr_art, STYLE_DETAILED)
        check("文档的提示词模板里没有 [00:00] 示例",
              "[00:00]" not in user_art, "模型会照抄这个前缀")
        check("文档的提示词明确要求不要加时间戳",
              "不要加时间戳" in user_art)
        check("文档的分段细讲标题不带「带时间戳」",
              "分段细讲（带时间戳）" not in user_art)

        tr_vid = _Tr(source="subtitle", text="讲话内容。" * 20,
                     segments=[_Seg(start=1.0, end=3.0, text="讲话内容。" * 20)])
        _s2, user_vid = build_prompt(r_off.meta, tr_vid, STYLE_DETAILED)
        check("视频的提示词仍带时间戳", "[00:00]" in user_vid)
        check("视频的分段细讲标题带「带时间戳」",
              "分段细讲（带时间戳）" in user_vid)
    finally:
        ctx_off.close()

    ctx.close()
    return 0


if __name__ == "__main__":
    main()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项，跳过 {SKIP} 项")
    sys.exit(1 if FAIL else 0)
