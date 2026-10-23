"""冒烟测试：B 站视频 → 下载音频 → 本地 Whisper 转写 → 摘要。

用法::

    python tests/smoke_asr.py [BVID] [seconds] [model_size]

不传参数时用一个公开的热门视频，只取前 30 秒、用 tiny 模型，跑得快。
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vsum import load_all                                    # noqa: E402
from vsum.asr.faster_whisper import FasterWhisperEngine       # noqa: E402
from vsum.core.config import Config                           # noqa: E402
from vsum.core.context import Context                         # noqa: E402
from vsum.core.ffmpeg import extract_audio                    # noqa: E402
from vsum.media.ytdlp import extract_info, pick_audio, headers_arg  # noqa: E402


def main():
    load_all()
    # 【别随便换默认链接】默认值一旦失效，整条测试会 FAIL，但**代码根本没问题**
    # —— 2026-10-10 就因为这个白排查一轮：旧的 BV1yRH66VEHm 返回
    # "No video formats found"，换成还在线的 BV1VeHQ6tEaS 后 tiny/small 全通过。
    # 规则：冒烟测试挂了，**先确认测试用的链接/素材还在**，再怀疑代码。
    bv = sys.argv[1] if len(sys.argv) > 1 else "BV1VeHQ6tEaS"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    size = sys.argv[3] if len(sys.argv) > 3 else "tiny"

    cfg = Config.load()
    cfg.set("asr.model_size", size)
    ctx = Context(cfg)

    print(f"[1] 解析 {bv} ...")
    info = extract_info(f"https://www.bilibili.com/video/{bv}", ctx)
    assert info, "yt-dlp 解析失败"
    print("    标题:", info.get("title"))

    print("[2] 提取前 %d 秒音频 ..." % seconds)
    fmt = pick_audio(info)
    wav = str(Path(ctx.workdir) / "smoke.wav")
    ffmpeg = ctx.ffmpeg()
    # 直接用带 headers 的方式抽音频
    from vsum.media.ytdlp import _extract_with_headers
    t = time.time()
    _extract_with_headers(ffmpeg, fmt["url"], wav,
                          headers_arg(fmt, info.get("webpage_url", "")), seconds / 60.0)
    print(f"    {os.path.getsize(wav)/1024:.0f} KB, {time.time()-t:.1f}s")

    print(f"[3] Whisper({size}) 转写 ...")
    engine = FasterWhisperEngine(ctx)
    ok, reason = engine.available()
    if not ok:
        print("    不可用:", reason)
        return 1
    t = time.time()
    tr = engine.transcribe(wav, language="zh",
                           on_progress=lambda m, p: print(f"    {m}") if int(p * 10) % 3 == 0 else None)
    print(f"    转写完成 {time.time()-t:.1f}s，{tr.char_count} 字，{len(tr.segments)} 段")
    print("    前 200 字:", tr.text[:200].replace("\n", " "))

    print("[4] 本地抽取式摘要 ...")
    from vsum.llm.extractive import ExtractiveEngine
    from vsum.core.models import VideoMeta
    meta = VideoMeta(platform="bilibili", platform_name="哔哩哔哩", video_id=bv,
                     url=info.get("webpage_url", ""), title=info.get("title", ""),
                     author=info.get("uploader", ""))
    md = ExtractiveEngine(ctx).summarize(meta, tr)
    print("\n" + md[:900])

    os.remove(wav)
    print("\n结果:", "PASS" if tr.char_count > 0 else "FAIL")
    return 0 if tr.char_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
