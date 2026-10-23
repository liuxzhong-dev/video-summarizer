"""批量验证：取 B 站榜单前 N 个视频，各转写前 M 秒，看命中率。

用来确认「下载音频 → 本地 Whisper」这条主链路确实可用。
纯音乐/无人声的视频输出为空是正常的（VAD 会过滤掉）。

用法::

    python tests/smoke_asr_batch.py [count] [seconds] [model_size]
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vsum import load_all                                        # noqa: E402
from vsum.asr.faster_whisper import FasterWhisperEngine           # noqa: E402
from vsum.core.config import Config                               # noqa: E402
from vsum.core.context import Context                             # noqa: E402
from vsum.media.ytdlp import (_extract_with_headers, extract_info,  # noqa: E402
                              headers_arg, pick_audio)


def main():
    load_all()
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 45
    size = sys.argv[3] if len(sys.argv) > 3 else "tiny"

    cfg = Config.load()
    cfg.set("asr.model_size", size)
    ctx = Context(cfg)
    engine = FasterWhisperEngine(ctx)
    ok, reason = engine.available()
    print("ASR 可用:", ok, reason or "")
    if not ok:
        return 1

    print("[榜单] 拉取中 ...")
    data = ctx.http.get_json("https://api.bilibili.com/x/web-interface/popular",
                             params={"ps": count, "pn": 1})
    items = (data.get("data") or {}).get("list") or []
    print(f"      共 {len(items)} 个\n")

    ffmpeg = ctx.ffmpeg()
    hit = 0
    for it in items:
        bv, title = it.get("bvid"), (it.get("title") or "")[:24]
        url = f"https://www.bilibili.com/video/{bv}"
        try:
            info = extract_info(url, ctx)
            if not info:
                print(f"  {bv} {title} -> 解析失败")
                continue
            fmt = pick_audio(info)
            wav = str(Path(ctx.workdir) / f"{bv}.wav")
            _extract_with_headers(ffmpeg, fmt["url"], wav,
                                  headers_arg(fmt, info.get("webpage_url", "")),
                                  seconds / 60.0)
            t = time.time()
            tr = engine.transcribe(wav, language="zh")
            dt = time.time() - t
            flag = "OK " if tr.char_count else "-- "
            if tr.char_count:
                hit += 1
            print(f"  {flag}{bv} {title}")
            print(f"      {tr.char_count} 字 / {len(tr.segments)} 段 / {dt:.1f}s :: "
                  f"{tr.text[:90]}")
            os.remove(wav)
        except Exception as exc:
            print(f"  XX {bv} {title} -> {type(exc).__name__}: {str(exc)[:100]}")

    print(f"\n命中 {hit}/{len(items)}（无人声的视频为空属正常）")
    return 0 if hit > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
