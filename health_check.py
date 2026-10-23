# -*- coding: utf-8 -*-
"""
全量回归体检 —— 不联网也能跑的检查 + 需要联网的功能自检。

分两部分：
  A. 静态检查：语法、导入、配置完整性、平台注册、UI 资源
  B. 功能自检：文章抽取、纯文本、本地文档、B站字幕（真实跑）
"""
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = str(pathlib.Path(__file__).resolve().parent)
PY = sys.executable          # 用当前解释器，不写死路径
sys.path.insert(0, os.path.join(ROOT, "src"))

PASS, FAIL, WARN = [], [], []


def ok(name, detail=""):
    PASS.append(name)
    print(f"  [OK] {name}" + (f"  {detail}" if detail else ""))


def bad(name, detail=""):
    FAIL.append((name, detail))
    print(f"  [!!] {name}  {detail}")


def warn(name, detail=""):
    WARN.append((name, detail))
    print(f"  [--] {name}  {detail}")


# ================= A. 静态检查 =================
print("\n" + "=" * 66)
print("A. 静态检查")
print("=" * 66)

# A1 语法
try:
    r = subprocess.run([PY, "-m", "compileall", "-q", os.path.join(ROOT, "src")],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        ok("全部源码语法正确")
    else:
        bad("语法错误", r.stdout[-200:] + r.stderr[-200:])
except Exception as exc:
    bad("语法检查失败", str(exc)[:80])

# A2 平台注册
try:
    from vsum.extensions import load_builtin
    from vsum.core.registry import all_platforms
    load_builtin()
    plats = all_platforms()
    expect = {"bilibili", "douyin", "kuaishou", "xiaohongshu",
              "weixin_channels", "generic", "article", "plain"}
    missing = expect - set(plats)
    if missing:
        bad("平台注册不全", f"缺 {missing}")
    else:
        ok("8 个平台全部注册", ", ".join(sorted(plats)))
except Exception as exc:
    bad("平台注册检查失败", str(exc)[:120])

# A3 浏览器兜底配置
try:
    from vsum.core.config import Config
    from vsum.core.pipeline import Pipeline
    c = Config.load()
    fb = c.get("asr.browser_fallback")
    plats_fb = Pipeline.BROWSER_FALLBACK
    if fb is None:
        warn("browser_fallback 未在配置里", "用默认值 True")
    else:
        ok("browser_fallback 配置存在", f"= {fb}")
    if "bilibili" in plats_fb:
        bad("B站被误列入浏览器兜底", "会影响速度")
    else:
        ok("兜底范围正确", str(plats_fb))
except Exception as exc:
    bad("兜底配置检查失败", str(exc)[:100])

# A4 配置文件一致性
try:
    cfg_path = os.path.join(ROOT, "config.json")
    cfg = json.load(io.open(cfg_path, encoding="utf-8"))
    obs = cfg.get("obsidian", {})
    dirs = {obs.get("folder_high"), obs.get("folder_mid"), obs.get("folder_moc")}
    vault = obs.get("vault_path", "")
    missing = [d for d in dirs if d and not os.path.isdir(os.path.join(vault, d))]
    if missing:
        bad("配置的目录在库里不存在", str(missing))
    else:
        ok("Obsidian 目录配置有效", str(sorted(dirs)))
except Exception as exc:
    warn("配置检查失败", str(exc)[:80])

# A5 UI 资源完整性
try:
    ui = os.path.join(ROOT, "src", "vsum", "ui", "assets", "index.html")
    html = io.open(ui, encoding="utf-8").read()
    need = ["btnClear", "btnRun", "btnSettings", "clearAll", "taLong",
            "openSettings", "ck_douyin", "hint"]
    miss = [k for k in need if k not in html]
    if miss:
        bad("UI 缺少元素", str(miss))
    else:
        ok("UI 关键元素完整", f"{len(html)} 字符")
    if "�" in html:
        bad("UI 有乱码")
    else:
        ok("UI 无乱码")
except Exception as exc:
    bad("UI 检查失败", str(exc)[:80])

# A6 密钥自查
try:
    r = subprocess.run([PY, os.path.join(ROOT, "check_secrets.py")],
                       capture_output=True, text=True, cwd=ROOT, timeout=60)
    if "🔴" in r.stdout:
        n = r.stdout.count("🔴")
        warn(f"{n} 个 config.json 含真实 Key", "迁移/分发前需脱敏")
    else:
        ok("配置文件已脱敏")
except Exception as exc:
    warn("密钥自查失败", str(exc)[:60])

# A7 spec 引用完整性
try:
    spec = io.open(os.path.join(ROOT, "VideoSummarizer.spec"), encoding="utf-8").read()
    for mod in ("pw_mods", "pw_datas"):
        if mod in spec:
            ok("spec 已包含 Playwright", mod)
        else:
            warn("spec 未包含 " + mod, "浏览器兜底可能打包不全")
    if "browser_fetch" not in spec:
        warn("spec 未显式提browser_fetch", "但 vsum_mods 会全量收集")
except Exception as exc:
    bad("spec 检查失败", str(exc)[:80])

# ================= B. 功能自检 =================
print("\n" + "=" * 66)
print("B. 功能自检（真实运行）")
print("=" * 66)

try:
    from vsum.core.article import extract_from_plain
    from vsum.platforms.article import PlainTextAdapter
    from vsum.core.context import Context
    from vsum.core.config import Config

    ctx = Context(Config.load())

    # B1 纯文本
    sample = "# 测试标题\n\n这是第一段正文，用来验证纯文本路径是否正常工作。\n\n这是第二段，再多写一点让它超过长度阈值。"
    a = PlainTextAdapter(ctx)
    m = a.resolve(sample)
    tr = a.fetch_subtitle(m)
    if tr and tr.char_count > 20 and m.title == "测试标题":
        ok("纯文本直贴", f"标题={m.title}, {tr.char_count} 字")
    else:
        bad("纯文本直贴", f"title={m.title}, chars={tr.char_count if tr else 0}")

    # B2 本地文件
    p = os.path.join(tempfile.gettempdir(), "vsum_check.md")
    io.open(p, "w", encoding="utf-8").write("# 文件测试\n\n" + sample.split("\n", 2)[2])
    m2 = a.resolve(p)
    if m2.title == "文件测试":
        ok("本地 .md 读取", m2.video_id)
    else:
        bad("本地 .md 读取", f"title={m2.title}")

    # B3 网页文章抽取（不联网，用固定 HTML）
    from vsum.core.article import extract_article
    html_test = """<html><head><title>测试文章 - 某站</title></head><body>
    <nav><a href=#>导航1</a><a href=#>导航2</a></nav>
    <article><h1>真正的标题</h1>
    <p>""" + "正文内容足够长才能通过阈值判定，所以这里多写一些内容。" * 12 + """</p>
    <p>第二段内容，同样写长一点让它稳定通过阈值检查。</p></article>
    <footer><a href=#>页脚链接</a></footer></body></html>"""
    art = extract_article(html_test)
    if art["ok"] and len(art["text"]) > 100 and "导航" not in art["text"][:50]:
        ok("网页正文抽取", f"{len(art['text'])} 字, 标题={art['title'][:20]}")
    else:
        bad("网页正文抽取", art.get("reason", "")[:60])

    # B4 验证页识别
    wall = "<html><title>验证码中间页</title><body>请完成验证 captcha</body></html>"
    art2 = extract_article(wall)
    if not art2["ok"] and ("验证" in art2["reason"] or "登录" in art2["reason"]):
        ok("验证页识别", art2["reason"][:40])
    else:
        warn("验证页识别", f"结果={art2['reason'][:50]}")

    os.remove(p)
except Exception as exc:
    bad("功能自检异常", f"{type(exc).__name__}: {exc}"[:120])

# B5 B站真实字幕（唯一一个快的网络测试）
try:
    from vsum.core.pipeline import Pipeline
    t0 = time.time()
    p = Pipeline(Context(Config.load()))
    r = p.run("https://www.bilibili.com/video/BV1GJ411x7h7")
    el = time.time() - t0
    if r.transcript.char_count > 100:
        ok("B站字幕路径", f"{r.transcript.char_count} 字, {el:.0f} 秒, 来源={r.transcript.source}")
    else:
        warn("B站字幕路径", f"只拿到 {r.transcript.char_count} 字（{el:.0f} 秒）")
except Exception as exc:
    warn("B站测试失败", f"{type(exc).__name__}: {exc}"[:100])

# ================= 汇总 =================
print("\n" + "=" * 66)
print(f"体检结果：{len(PASS)} 通过 / {len(WARN)} 提醒 / {len(FAIL)} 失败")
print("=" * 66)
if FAIL:
    print("\n失败项：")
    for n, d in FAIL:
        print(f"  ✗ {n}  {d}")
if WARN:
    print("\n提醒项：")
    for n, d in WARN:
        print(f"  ! {n}  {d}")