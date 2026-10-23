"""冒烟测试：随机取一个 B 站热门视频，跑通「元数据 → 字幕 → 弹幕」。

用法::

    python tests/smoke_bilibili.py [BVID]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vsum.core.config import Config          # noqa: E402
from vsum.core.context import Context        # noqa: E402
from vsum.platforms.bilibili import BilibiliAdapter  # noqa: E402


def main():
    ctx = Context(Config())
    bv = sys.argv[1] if len(sys.argv) > 1 else None

    if not bv:
        print("[1] 拉取热门榜单取一个视频 ...")
        try:
            data = ctx.http.get_json(
                "https://api.bilibili.com/x/web-interface/popular",
                params={"ps": 5, "pn": 1})
            items = (data.get("data") or {}).get("list") or []
            bv = items[0]["bvid"]
        except Exception as exc:
            print("  榜单接口失败:", exc)
            print("  改用硬编码 BV 号")
            bv = "BV1GJ411x7h7"
    print("    BV =", bv)

    ad = BilibiliAdapter(ctx)
    url = f"https://www.bilibili.com/video/{bv}"
    print("[2] 解析元数据 ...")
    meta = ad.resolve(url)
    print("    标题:", meta.title)
    print("    UP  :", meta.author, "| 时长:", meta.duration, "秒")
    print("    cid :", meta.extra.get("cid"))

    print("[3] 取字幕 ...")
    tr = ad.fetch_subtitle(meta)
    if tr:
        print(f"    命中字幕: engine={tr.engine} 段数={len(tr.segments)} 字数={tr.char_count}")
        print("    前 3 句:", " / ".join(s.text for s in tr.segments[:3])[:200])
    else:
        print("    未拿到字幕（需要降级到 ASR）")

    print("[4] 取弹幕 ...")
    sup = ad.supplement_text(meta, limit=20)
    print("    ", sup[:200] if sup else "(空)")

    ok = bool(meta.title)
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
