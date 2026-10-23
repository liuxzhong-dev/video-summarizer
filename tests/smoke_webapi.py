"""冒烟测试：本地 Web 服务全流程（提交任务 → 轮询 → 拿结果 → 导出）。

用法::

    python tests/smoke_webapi.py
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vsum.core.config import Config          # noqa: E402
from vsum.ui.launcher import launch_background  # noqa: E402


def http(method, url, data=None):
    req = urllib.request.Request(url, method=method)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, body, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    cfg = Config.load()
    cfg.set("asr.engine", "subtitle")        # 冒烟测试不下载模型
    base, port = launch_background(cfg, port=0)
    print("[1] 服务地址:", base)

    print("[2] 健康检查:", http("GET", base + "api/health"))

    html = urllib.request.urlopen(base, timeout=20).read().decode("utf-8")
    print("[3] 首页 HTML: %d 字节，含标题=%s" % (len(html), "VideoSummarizer" in html))

    print("[4] 平台列表:")
    for p in http("GET", base + "api/platforms")["platforms"]:
        print("    ", p["support"].ljust(9), p["name"])

    print("[5] 提交任务 …")
    task = http("POST", base + "api/summarize",
                {"text": "https://www.bilibili.com/video/BV1yRH66VEHm"})
    tid = task["task_id"]
    print("    task_id =", tid)

    for _ in range(120):
        time.sleep(0.5)
        st = http("GET", f"{base}api/status?task_id={tid}")
        print(f"    [{st['percent']:3d}%] {st['message']}")
        if st["state"] != "running":
            break
    else:
        print("    超时")
        return 1

    if st["state"] != "done":
        print("    失败:", st.get("error"), st.get("hint"))
        return 1

    print("[6] 摘要前 200 字:")
    print("    " + (st.get("markdown") or "")[:200].replace("\n", "\n    "))
    print("[7] 来源:", st["result"]["transcript"]["source"],
          "| 引擎:", st["result"]["summary_engine"])

    out = Path(__file__).resolve().parents[1] / "out" / "smoke_webapi"
    res = http("POST", base + "api/export", {"task_id": tid, "dir": str(out)})
    print("[8] 导出:", [Path(f).name for f in res.get("files", [])])

    print("\n结果: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
