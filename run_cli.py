#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行入口：VideoSummarizer CLI。

常用用法::

    python run_cli.py "https://www.bilibili.com/video/BV1xx411c7mD"
    python run_cli.py "粘贴整段抖音分享文案" --asr subtitle
    python run_cli.py 链接 --out ./out --format md,txt,json,srt
    python run_cli.py --list
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from vsum import __version__, load_all, summarize                 # noqa: E402
from vsum.core.config import Config, LLM_PRESETS                 # noqa: E402
from vsum.core.exporters import export                           # noqa: E402
from vsum.core.pipeline import Pipeline                          # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vsum",
        description="把主流平台的视频链接变成一份中文总结",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("url", nargs="?", help="视频链接 / 分享文案；省略则交互式输入")
    p.add_argument("--out", "-o", default="", help="导出目录（同时会落盘文件）")
    p.add_argument("--format", default="md,txt,json",
                   help="导出格式，逗号分隔：md,txt,json,srt")
    p.add_argument("--json", action="store_true", help="控制台输出 JSON 而非 Markdown")
    p.add_argument("--asr", default="", choices=["", "auto", "subtitle",
                                                 "faster-whisper", "cloud"],
                   help="文本来源策略（覆盖配置文件）")
    p.add_argument("--style", default="", choices=["", "brief", "detailed", "bullets"])
    p.add_argument("--llm", default="", help="临时切换大模型预设，如 deepseek / ollama")
    p.add_argument("--no-llm", action="store_true", help="强制使用本地抽取式摘要")
    p.add_argument("--proxy", default="", help="HTTP 代理，如 http://127.0.0.1:7890")

    g = p.add_argument_group("Obsidian 知识库")
    g.add_argument("--obsidian", action="store_true",
                   help="把总结经 AI 筛选后写入 Obsidian 知识库")
    g.add_argument("--vault", default="",
                   help="Obsidian 库路径（留空自动探测）")
    g.add_argument("--focus", default="",
                   help="关注方向，如「LLM推理优化, 求职面试」。写给 AI 筛选器当评判标准")
    g.add_argument("--force-save", action="store_true",
                   help="忽略评分强制入库（放进精选目录）")
    g.add_argument("--vault-status", action="store_true",
                   help="只打印 Obsidian 库状态与笔记数")

    g = p.add_argument_group("RAG 检索问答（把语料建库后直接提问）")
    g.add_argument("--rag-ingest", default="", metavar="路径或链接",
                   help="入库：本地文件 / 目录（如 D:\\KnowledgeBase），或视频链接")
    g.add_argument("--rag-ask", default="", metavar="问题",
                   help="对已建的库提问，答案带出处")
    g.add_argument("--rag-list", action="store_true", help="列出库里已入库的来源")
    g.add_argument("--rag-clear", action="store_true", help="清空向量库")
    g.add_argument("--rag-topk", type=int, default=0, help="取回片段条数（默认读配置）")
    g.add_argument("--rag-pattern", default=".md",
                   help="批量入库时的文件后缀，默认 .md")

    p.add_argument("--list", action="store_true", help="列出已注册的插件与支持的平台")
    p.add_argument("--check", action="store_true",
                   help="自检：检查各项能力是否就位（含实测启动一次浏览器）")
    p.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    p.add_argument("--version", action="version", version=f"vsum {__version__}")
    return p


def _fix_console_encoding() -> None:
    """Windows 控制台默认是 GBK，直接 print UTF-8 会一片乱码。"""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main(argv=None) -> int:
    _fix_console_encoding()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    load_all()

    if args.list:
        return _print_list()

    cfg = Config.load()

    if args.check:
        return _run_check(cfg)

    # RAG 子集：建库 / 提问 / 查看。这几个动作不需要视频链接，
    # 所以必须在下面「没输入链接就报错」之前拦下来。
    if args.rag_ingest or args.rag_ask or args.rag_list or args.rag_clear:
        return _run_rag(cfg, args)

    # 先把 --vault / --focus 落到配置里，再看 --vault-status。
    # 顺序反了的话 `--vault-status --vault X` 会把 X 忽略掉、显示自动探测到的
    # 另一个库 —— 看着像程序没生效，其实是自己写错了顺序。
    if args.obsidian or args.vault or args.focus:
        # 命令行显式传了就打开自动入库；只 --vault-status 时不动配置
        cfg.set("obsidian.enabled", bool(args.obsidian))
        if args.vault:
            cfg.set("obsidian.vault_path", args.vault)
        if args.focus:
            cfg.set("obsidian.focus", args.focus)

    if args.vault_status:
        return _print_vault_status(cfg)

    url = args.url
    if not url:
        print("请输入视频链接或整段分享文案（回车结束）：")
        try:
            url = input("> ").strip()
        except EOFError:
            return 1
    if not url:
        print("错误：没有输入链接", file=sys.stderr)
        return 1

    if args.asr:
        cfg.set("asr.engine", args.asr)
    if args.style:
        cfg.set("summary.style", args.style)
    if args.proxy:
        cfg.set("media.proxy", args.proxy)
    if args.no_llm:
        cfg.set("llm.enabled", False)
    elif args.llm:
        if not cfg.apply_preset(args.llm):
            print(f"未知预设：{args.llm}，可选：{', '.join(LLM_PRESETS)}", file=sys.stderr)
            return 1

    def progress(msg, pct):
        bar = "" if pct < 0 else f"[{pct:3d}%] "
        print(f"  {bar}{msg}", flush=True)

    try:
        result = summarize(url, config=cfg, progress=progress)
    except Exception as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        hint = getattr(exc, "hint", "")
        if hint:
            print(f"提示：{hint}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("\n" + "=" * 60)
        print(result.markdown)
        print("=" * 60)
        print(f"文本来源：{result.transcript.source}（{result.transcript.engine}），"
              f"{result.transcript.char_count} 字 | 摘要引擎：{result.engine} | "
              f"耗时 {result.elapsed:.1f}s")

    if args.out:
        fmts = [f.strip() for f in args.format.split(",") if f.strip()]
        files = export(result, args.out, fmts)
        print("\n已导出：")
        for f in files:
            print("  " + os.path.abspath(f))

    # --obsidian 没走自动入库（比如自动入库被关掉了）时补一次手动写入
    if args.obsidian and not result.obsidian:
        from vsum.core.context import Context
        from vsum.obsidian import save as vault_save

        info = vault_save(Context(cfg), result, force=args.force_save)
        result.obsidian = info

    _print_obsidian(result.obsidian, args.force_save)
    return 0


def _print_obsidian(info: dict, forced: bool = False) -> None:
    """打印入库结果。没入库就什么都不显示。"""
    if not info:
        return
    if not info.get("ok"):
        print(f"\n知识库入库失败：{info.get('message', '未知原因')}", file=sys.stderr)
        return
    if info.get("skipped"):
        print(f"\n未入库：{info.get('message', '')}")
        if not forced:
            print("  确实想存的话加 --force-save 强制写入精选目录。")
        return
    print(f"\n知识库：{info.get('message', '')}")
    if info.get("reason"):
        print(f"  理由：{info['reason']}")
    if info.get("entities"):
        print(f"  识别到的概念：{', '.join(info['entities'])}")
    if info.get("note"):
        print(f"  提示：{info['note']}")


def _run_rag(cfg, args) -> int:
    """RAG 子命令：建库 / 提问 / 查看 / 清空。"""
    from vsum.core.context import Context
    from vsum.rag import ask as rag_ask
    from vsum.rag.ingest import ingest_dir, ingest_path
    from vsum.rag.retriever import Retriever, build_store

    ctx = Context(cfg)
    ctx.progress = lambda msg, pct: print(f"  {msg}", flush=True)
    try:
        store = build_store(ctx)

        if args.rag_clear:
            before = store.count()
            store.clear()
            print(f"已清空向量库（原有 {before} 个片段）")
            print(f"  库目录：{store.path}")
            return 0

        if args.rag_list:
            return _print_rag_list(store)

        if args.rag_ingest:
            target = args.rag_ingest
            if target.startswith(("http://", "https://")):
                info = _ingest_url(ctx, target, store)
            elif os.path.isdir(target):
                print(f"正在入库目录：{os.path.abspath(target)}")
                info = ingest_dir(ctx, target, pattern=args.rag_pattern, store=store)
            else:
                print(f"正在入库文件：{os.path.abspath(target)}")
                info = ingest_path(ctx, target, store=store)
            _print_ingest(info, store)
            if not info.get("ok"):
                return 1
            # 刚灌完数据，关键词检索的缓存必须作废，否则本次提问会用到旧快照
            Retriever(ctx, store=store).invalidate()

        if args.rag_ask:
            top_k = args.rag_topk or int(cfg.get("rag.top_k", 5) or 5)
            ret = Retriever(ctx, store=store)
            result = rag_ask(ctx, args.rag_ask, top_k=top_k, retriever=ret)
            _print_answer(result)
            return 0
        return 0
    except Exception as exc:                                  # noqa: BLE001
        print(f"\n失败：{exc}", file=sys.stderr)
        hint = getattr(exc, "hint", "")
        if hint:
            print(f"提示：{hint}", file=sys.stderr)
        return 2
    finally:
        ctx.close()


def _ingest_url(ctx, url: str, store) -> dict:
    """视频链接入库：先跑一遍现有流水线拿到字幕/转写，再切块灌进向量库。

    这条路径是「8 平台字幕库」真正的入口 —— 语料不是现成的，
    得靠原来那套抓取能力一条条攒出来。

    **跑之前临时关掉 Obsidian 自动入库**：两条入库路径的目的地不同
    （一个进向量库，一个写用户的知识库）。用户敲的是 ``--rag-ingest``，
    要的就是进向量库，不该顺手改动他的知识库文件。
    """
    from vsum.core.pipeline import Pipeline
    from vsum.rag.ingest import ingest_transcript

    ctx.config.set("obsidian.enabled", False)
    print("正在解析视频并获取文本（无字幕时会下载音频做识别，可能需要几分钟）…")
    result = Pipeline(ctx).run(url)
    tr = result.transcript
    if not (tr.text or "").strip():
        return {"ok": False, "chunks": 0, "vectors": False, "total": store.count(),
                "message": "这条视频没有拿到任何文本（无字幕且音频也取不到）"}
    print(f"  已获取文本 {tr.char_count} 字（来源：{tr.source}／{tr.engine}）")
    info = ingest_transcript(ctx, result.meta, tr, store=store)
    info["title"] = result.meta.title
    return info


def _print_ingest(info: dict, store=None) -> None:
    if not info.get("ok"):
        print(f"入库失败：{info.get('message', '未知原因')}", file=sys.stderr)
        return
    print()
    if "files" in info:
        print(f"扫描 {info.get('scanned', 0)} 个文件，成功读入 {info.get('files', 0)} 个")
    print(f"写入片段：{info.get('chunks', 0)} 个（库内合计 {info.get('total', 0)} 个）")
    if info.get("vectors"):
        print(f"向量：已生成（{info.get('dim', 0)} 维）")
    else:
        print("向量：未生成 —— 检索将退化为关键词匹配")
    if info.get("message"):
        print(f"注意：{info['message']}")
    failed = info.get("failed") or []
    if failed:
        print(f"跳过 {len(failed)} 个文件：")
        for f in failed[:10]:
            print(f"  · {f}")
        if len(failed) > 10:
            print(f"  … 另有 {len(failed) - 10} 个")


def _print_rag_list(store) -> int:
    # 先调一次 count() —— 向量库是懒加载的，dim/model 只有在读过索引之后
    # 才有值。顺序反了会显示「尚未向量化」，而库里其实存着向量，
    # 会让人以为向量化那一步白跑了。
    fragment_count = store.count()
    print("RAG 向量库")
    print(f"  库目录：{store.path}")
    print(f"  片段数：{fragment_count}　维度：{store.dim or '（尚未向量化）'}"
          f"　模型：{store.model or '-'}")
    rows = store.sources()
    if not rows:
        print()
        print("  库里还没有内容。先把语料灌进去，例如：")
        print('    vsum --rag-ingest "D:\\KnowledgeBase"')
        return 0
    print(f"  来源数：{len(rows)}")
    print()
    for r in rows[:200]:
        label = r.get("title") or r.get("source_id") or "(未知)"
        print(f"    {r['count']:>5} 段  {label}")
    if len(rows) > 200:
        print(f"    … 另有 {len(rows) - 200} 个来源")
    return 0


def _print_answer(result: dict) -> None:
    print()
    print(result.get("answer") or "（没有回答）")
    print()
    print("─" * 56)
    print(f"检索方式：{result.get('mode')}　｜　生成：{result.get('engine')}")
    if result.get("degraded"):
        print(f"注意：{result['degraded']}")
    hits = result.get("hits") or []
    if hits:
        print()
        print("引用片段（按相关度排序）：")
        for i, h in enumerate(hits, 1):
            c = h.get("chunk") or {}
            where = f"《{c.get('title')}》" if c.get("title") else \
                (c.get("source_id") or "未知来源")
            start = c.get("start", -1)
            if isinstance(start, (int, float)) and start >= 0:
                where += f" {int(start) // 60:02d}:{int(start) % 60:02d}"
            print(f"  [{i}] {where}　相关度 {h.get('score', 0):.3f}（{h.get('via')}）")
            preview = (c.get("text") or "").replace("\n", " ")
            print(f"      {preview[:100]}{'…' if len(preview) > 100 else ''}")


def _print_vault_status(cfg) -> int:
    """打印 Obsidian 库状态，不跑总结。"""
    from vsum.core.context import Context
    from vsum.obsidian import status

    st = status(Context(cfg))
    print("Obsidian 知识库状态")
    print(f"  库路径  ：{st['vault'] or '未找到'}")
    if not st["found"]:
        # 找不到时把原因顶到最前面。这种情况下后面的「已入库 0 篇」是误导 ——
        # 读的人会以为库里真的没有笔记，其实是没连上。
        print(f"  原因    ：{st.get('message') or '未配置库路径'}")
        print()
        print("  连上之后，入库筛选与重写会调用大模型；没配 Key 则退回本地打分。")
        return 1
    print(f"  已入库  ：{st['note_count']} 篇视频笔记")
    print(f"  自动入库：{'已开启' if st['enabled'] else '未开启'}")
    print(f"  AI 筛选：{'可用' if st['llm_ready'] else '不可用（将用保守的本地打分）'}")
    if st.get("message"):
        print(f"  说明    ：{st['message']}")
    return 0


def _run_check(cfg) -> int:
    """自检：一次说清「这台机器上有哪些能力可用」。

    存在的意义：打包后的 exe 是个黑盒。用户遇到「抖音解析失败」时，
    分不清是网络问题、缺 Cookie，还是**包里压根没带这个能力**。
    跑一条 ``--check`` 就能先把最后一种排除掉 —— 而这正是本项目踩过的真坑：
    playwright 曾被静默漏打，连着好几版 exe 都没有浏览器兜底，症状却只是
    「某个平台解析失败」，从表象完全推不到根因。

    返回码：关键能力齐备 = 0，有缺失 = 1（方便自动化里直接用）。
    """
    import importlib.util as _iu

    frozen = bool(getattr(sys, "frozen", False))
    is_cli_build = frozen and os.path.basename(sys.executable).lower().startswith("vsum")
    marks = {"ok": "√", "miss": "×", "na": "—"}
    #: 硬缺失项 —— 缺了就是功能不完整，影响返回码
    missing = []

    def say(area: str, name: str, state: str, note: str = "") -> None:
        print(f"  {marks[state]} {area:<4} {name:<22} {note}")

    def has(mod: str) -> bool:
        try:
            return _iu.find_spec(mod) is not None
        except Exception:
            return False

    print(f"vsum {__version__} 自检")
    print(f"  运行方式：{'打包版 exe（冻结）' if frozen else '源码目录'}")
    print(f"  可执行文件：{sys.executable}")
    print()

    # ---------- 配置 ----------
    cp = cfg.path or ""
    if cp and os.path.isfile(cp):
        say("配置", "config.json", "ok", f"{cp}（{os.path.getsize(cp)} B）")
    else:
        say("配置", "config.json", "ok", f"{cp}（本次已自动生成）")

    # ---------- 链接解析 ----------
    if has("yt_dlp"):
        try:
            from yt_dlp.version import __version__ as _ytv
        except Exception:
            _ytv = "版本未知"
        say("解析", "yt-dlp", "ok", f"{_ytv}  ← B站/YouTube 等靠它")
    else:
        say("解析", "yt-dlp", "miss", "缺失 —— 所有链接都解析不了")
        missing.append("yt-dlp")

    # ---------- 桌面界面 ----------
    if is_cli_build:
        say("界面", "pywebview", "na", "命令行版不需要（界面版才有）")
    elif has("webview"):
        say("界面", "pywebview", "ok", "可开内嵌桌面窗口")
    else:
        say("界面", "pywebview", "miss", "缺失 —— 界面版双击会打不开")
        missing.append("pywebview")

    # ---------- 浏览器兜底（抖音/快手/小红书）----------
    if not has("playwright"):
        say("兜底", "playwright", "miss", "缺失 —— 抖音/快手/小红书没有兜底")
        missing.append("playwright")
    else:
        try:
            from importlib.metadata import version as _pkg_ver
            _pwv = _pkg_ver("playwright")
        except Exception:
            _pwv = "已就位"
        say("兜底", "playwright", "ok", _pwv)
        # 内置内核（v18 起）：exe 自带的精简 chrome-headless-shell。
        # 打包版没有它 = 换到没装 Edge/Chrome 的机器上抖音必失败，算硬缺失。
        from vsum.media.browser_fetch import _bundled_shell

        _shell = _bundled_shell()
        if _shell:
            say("兜底", "内置精简内核", "ok",
                f"{os.path.basename(os.path.dirname(os.path.dirname(_shell)))}"
                f"（{os.path.getsize(_shell) // 1024 // 1024} MB）")
        elif frozen:
            say("兜底", "内置精简内核", "miss",
                "此 exe 没带内核 —— 没装 Edge/Chrome 的机器上抖音解析会失败")
            missing.append("内置浏览器内核")
        else:
            say("兜底", "内置精简内核", "na",
                "源码运行用本机 ms-playwright，不需要内置")

        # 只是 import 成功**不算数**：driver（node 内核）漏打时，
        # import 照样通过，直到 launch() 才报错。所以这里真启动一次浏览器。
        print("       （正在实测启动一次浏览器，约 3-10 秒…）")
        try:
            from playwright.sync_api import sync_playwright

            from vsum.media.browser_fetch import _launch_browser

            with sync_playwright() as _p:
                _b, _chan = _launch_browser(_p, True, lambda _s: None)
                _ver = _b.version
                _b.close()
            say("兜底", "浏览器内核", "ok", f"实测可启动（{_chan} {_ver}）")
        except Exception as exc:
            say("兜底", "浏览器内核", "miss",
                f"无可用内核：{type(exc).__name__} —— 抖音/快手/小红书将无法解析")
            missing.append("浏览器内核（Edge / Chrome / Playwright Chromium）")

    # ---------- 本地文档解析 ----------
    _docs = ("pypdf", "docx", "openpyxl", "xlrd", "pptx")
    _lacked = [m for m in _docs if not has(m)]
    if _lacked:
        say("文档", "PDF/Word/Excel/PPT", "miss", "缺：" + "、".join(_lacked))
        missing.append("文档解析库")
    else:
        say("文档", "PDF/Word/Excel/PPT", "ok", "5/5 就位（另含 epub/odf/rtf/字幕）")

    # ---------- 音频 ----------
    try:
        import imageio_ffmpeg

        say("音频", "ffmpeg", "ok", os.path.basename(imageio_ffmpeg.get_ffmpeg_exe()))
    except Exception as exc:
        say("音频", "ffmpeg", "miss", f"缺失 —— 抽不了音频（{type(exc).__name__}）")
        missing.append("ffmpeg")

    if has("faster_whisper"):
        say("音频", "faster-whisper", "ok", "无字幕视频可本地转写")
    else:
        say("音频", "faster-whisper", "miss", "缺失 —— 无字幕视频转写不了")
        missing.append("faster-whisper")

    _cloud_key = (cfg.get("asr.cloud_api_key") or "").strip()
    if _cloud_key:
        say("音频", "云端 ASR", "ok",
            f"{cfg.get('asr.cloud_model')}（key …{_cloud_key[-4:]}）")
    else:
        say("音频", "云端 ASR", "na", "未配 Key（本地转写仍可用）")

    # ---------- 总结 ----------
    _llm_key = (cfg.get("llm.api_key") or "").strip()
    if cfg.get("llm.enabled") and _llm_key:
        say("总结", "大模型", "ok",
            f"{cfg.get('llm.preset')} / {cfg.get('llm.model')}（key …{_llm_key[-4:]}）")
    else:
        say("总结", "大模型", "na", "未配 Key（退回本地抽取式摘要，质量较低）")

    try:
        from vsum.core.context import Context
        from vsum.obsidian import status as _vault_status

        _st = _vault_status(Context(cfg))
        if _st.get("found"):
            say("总结", "Obsidian 库", "ok", f"{_st['vault']}（{_st['note_count']} 篇）")
        else:
            say("总结", "Obsidian 库", "na", _st.get("message") or "未配置库路径")
    except Exception as exc:
        say("总结", "Obsidian 库", "na", f"探测失败：{type(exc).__name__}")

    # ---------- 结论 ----------
    print()
    if missing:
        print(f"  结论：{len(missing)} 项关键能力缺失 —— {'、'.join(missing)}")
        print("        这些属于打包/环境问题，不是操作错误。")
        return 1
    print("  结论：关键能力齐备，可以正常使用。")
    return 0


def _print_list() -> int:
    from vsum.core.registry import describe

    print("已注册插件：\n")
    rows = describe()
    width = max((len(r["key"]) for r in rows), default=10)
    for r in rows:
        print(f"  [{r['type']:8s}] {r['key']:{width}s}  {r['class']}")
    print("\n平台支持情况：\n")
    from vsum.core.context import Context
    from vsum.core.config import Config
    pl = Pipeline(Context(Config()))
    for p in pl.list_platforms():
        mark = {"full": "★★★", "partial": "★★", "metadata": "★"}.get(p["support"], "?")
        print(f"  {mark} {p['name']:14s} {p['support']:9s} {p['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
