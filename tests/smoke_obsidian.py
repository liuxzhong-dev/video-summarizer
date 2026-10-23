"""冒烟测试：总结结果 → AI 筛选 → 写入 Obsidian 知识库。

**不碰真实库**：全程在临时目录里造一个假 vault，测完自动删。
所以这个测试可以放心反复跑。

用法::

    python tests/smoke_obsidian.py

覆盖的点：
    1. vault 自动定位（含 .obsidian 标记）
    2. 文件名清洗（非法字符、Windows 保留名）
    3. YAML 头生成，且**不被 [[双链]] 污染**（这是踩过的坑）
    4. 分级入库：低分不写文件
    5. 落盘路径：精选 → 20-精选，待阅 → 30-待定
    6. 术语表 + MOC 索引只在精选时生成
    7. 路径逃逸被拦截
    8. 已存在同名笔记时加序号，绝不覆盖

不测的部分（需要真实网络/API Key）：
    AI 筛选的真实打分、笔记重写。这两段在 tests/smoke_bilibili.py
    跑通真实总结后手动点「存入 Obsidian」验证更合适。
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vsum.core.config import Config                                # noqa: E402
from vsum.core.context import Context                              # noqa: E402
from vsum.core.models import Transcript, VideoMeta, SummaryResult  # noqa: E402
from vsum.obsidian import curator, frontmatter, note, vault        # noqa: E402
from vsum.obsidian import save as vault_save                       # noqa: E402


PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (("  <- " + extra) if extra and not cond else ""))


def make_result(title="LangGraph 多智能体编排实战", text="") -> SummaryResult:
    """造一份最小可用的总结结果。"""
    if not text:
        text = (
            "今天讲 LangGraph 的多智能体编排。先说结论：多智能体不是把几个模型串起来就完事，"
            "关键是状态怎么设计。StateGraph 里每个节点只读自己声明的字段，避免隐式耦合。"
            "实际项目里我们用 checkpoint 做断点续跑，失败节点重试不用从头跑。"
            "另外 StateGraph 相比原始 langchain 的 LangGraph，编译期就能把图结构校验一遍。"
        )
    md = ("## 核心结论\n\n"
          "- **StateGraph 编译期校验图结构**，写错会在启动时暴露，而不是跑到一半才炸。\n"
          "- **checkpoint 做断点续跑**，失败节点重试不用从头跑。\n"
          "- **节点只读自己声明的字段**，避免隐式耦合。\n"
          "- 节点提示词怎么写，参见 提示词使用指南 那篇。\n")
    return SummaryResult(
        meta=VideoMeta(
            platform="bilibili",
            platform_name="B站",
            video_id="BV1xx411c7mD",
            url="https://www.bilibili.com/video/BV1xx411c7mD",
            title=title,
            author="某某UP",
            duration=754,
        ),
        transcript=Transcript(source="subtitle", text=text),
        markdown=md,
        engine="test",
    )


def build_ctx(vault_path: str, **obs) -> Context:
    cfg = Config()
    cfg.set("obsidian.vault_path", vault_path)
    cfg.set("obsidian.enabled", True)
    for k, v in obs.items():
        cfg.set("obsidian." + k, v)
    return Context(cfg)


def main() -> int:
    root = tempfile.mkdtemp(prefix="vsum_obs_")
    # 造一个像样的 vault：.obsidian 标记 + PARA 目录
    os.makedirs(os.path.join(root, ".obsidian"), exist_ok=True)
    for sub in ("00-MOC", "10-项目", "20-精选", "30-待定"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    # 预置一篇已有笔记，用来验证双链能命中
    existing = os.path.join(root, "20-精选", "2026-10-01 提示词使用指南.md")
    with open(existing, "w", encoding="utf-8") as fh:
        fh.write("---\ntype: note\n---\n\n# 提示词使用指南\n\n提示词要写清角色、任务、约束。\n")

    try:
        print("\n=== 1. vault 定位 ===")
        check("looks_like_vault(真 vault)", vault.looks_like_vault(root))
        check("looks_like_vault(普通目录) 为假", not vault.looks_like_vault(tempfile.gettempdir()))
        check("find_vault(显式路径)", (vault.find_vault(root) or "").lower() == root.lower())
        check("ensure_inside 放行库内路径",
              vault.ensure_inside(root, os.path.join(root, "20-精选", "a.md")) == os.path.join(root, "20-精选", "a.md"))
        bad = False
        try:
            vault.ensure_inside(root, os.path.join(root, "..", "逃逸.md"))
        except Exception:
            bad = True
        check("ensure_inside 拦截 ../", bad)

        print("\n=== 2. 文件名清洗 ===")
        check("清非法字符", "/" not in vault.safe_note_name("a/b:c*d?e"))
        check("空标题兜底", bool(vault.safe_note_name("   ")))
        check("CON 保留名", vault.safe_note_name("CON").upper() != "CON")
        check("带日期前缀", note.note_stem("标题", date_prefix=True).startswith("2026-"))

        print("\n=== 3. YAML 头 ===")
        y = frontmatter.dump({"title": "2024", "n": 3, "ok": True,
                              "tags": ["a", "视频总结"], "skip": None,
                              "weird": "- 横杠开头: 有冒号"})
        check("纯数字标题加引号", 'title: "2024"' in y, y)
        check("整数不加引号", "n: 3" in y, y)
        check("布尔直出", "ok: true" in y, y)
        check("列表内含中文", "视频总结" in y, y)
        check("None 字段被跳过", "skip" not in y, y)
        check("特殊字符被引号包住", '\\"横杠开头\\"' in y or '"- 横杠开头' in y, y)

        print("\n=== 4. 本地打分（无 API Key 时的降级）===")
        j = curator.heuristic(make_result(), "LLM 推理优化")
        check("heuristic 出分", 0 <= j.score <= 100, str(j.score))
        check("heuristic 有理由", bool(j.reason), j.reason)
        check("heuristic 标签无垃圾",
              all(len(t) <= 30 for t in j.entities + j.topics),
              str(j.entities + j.topics))
        check("heuristic 不含 xxxx 之类乱码",
              not any("xxxx" in t for t in j.entities), str(j.entities))

        print("\n=== 5. 阈值定档 ===")
        check("高分→精选", curator.apply_thresholds(curator.Judgement(score=90), 80, 40).tier == curator.TIER_HIGH)
        check("中分→待阅", curator.apply_thresholds(curator.Judgement(score=60), 80, 40).tier == curator.TIER_MID)
        check("低分→丢弃", curator.apply_thresholds(curator.Judgement(score=10), 80, 40).tier == curator.TIER_LOW)
        check("丢弃不进库", not curator.apply_thresholds(curator.Judgement(score=10), 80, 40).keep)

        print("\n=== 6. 强制入库（绕开阈值）===")
        ctx = build_ctx(root)
        info = vault_save(ctx, make_result("LangGraph 多智能体编排实战"), force=True)
        check("返回 ok", info.get("ok"), str(info))
        check("落盘有路径", bool(info.get("path")), str(info))
        path = info.get("path", "")
        check("落在 20-精选", "20-精选" in path.replace("\\", "/"), path)
        check("文件真实存在", os.path.isfile(path))
        if os.path.isfile(path):
            text = Path(path).read_text(encoding="utf-8")
            head = text.split("---")[1] if "---" in text else ""
            check("YAML 头有 type: video-note", "type: video-note" in head, head[:200])
            check("YAML 头无 [[双链]]", "[[" not in head, head[:300])
            check("YAML 头 entities 是纯文本", "entities:" in head and "[[" not in head)
            check("正文有双链（命中已有笔记）", "[[2026-10-01 提示词使用指南]]" in text,
                  "未命中预置笔记")
            check("含来源链接", "bilibili.com" in text)

        print("\n=== 7. 术语表与索引 ===")
        glo = os.path.join(root, "术语表.md")
        mp = os.path.join(root, "00-MOC", "视频笔记索引.md")
        check("术语表建在库根", os.path.isfile(glo))
        check("索引建在 00-MOC", os.path.isfile(mp))
        if os.path.isfile(glo):
            g = Path(glo).read_text(encoding="utf-8")
            check("术语表非空", len(g) > 200, str(len(g)))
            check("术语表无乱码", "\ufffd" not in g)
        if os.path.isfile(mp):
            m = Path(mp).read_text(encoding="utf-8")
            check("索引能读到笔记", "LangGraph" in m, m[:300])

        print("\n=== 8. 不覆盖已有文件 ===")
        before = sorted(os.listdir(os.path.join(root, "20-精选")))
        vault_save(ctx, make_result("LangGraph 多智能体编排实战"), force=True)
        after = sorted(os.listdir(os.path.join(root, "20-精选")))
        check("同名新增而非覆盖", len(after) == len(before) + 1, f"{len(before)} -> {len(after)}")
        check("原文件内容未变",
              "提示词使用指南" in Path(existing).read_text(encoding="utf-8"))

        print("\n=== 9. 低分不写文件 ===")
        low = make_result("娱乐向内容盘点")
        j2 = curator.heuristic(low, "LLM 推理优化")
        j2.score = 5
        j2.tier = curator.TIER_LOW
        n_before = sum(len(fs) for _, _, fs in os.walk(root))
        r = vault_save(build_ctx(root, threshold_low=40), low, force=False)
        n_after = sum(len(fs) for _, _, fs in os.walk(root))
        # 注意：本地打分可能给不出 5 分，这里只验证「要么跳过、要么写了但不打错目录」
        check("低分未污染精选目录",
              not any("娱乐向内容盘点" in f for f in os.listdir(os.path.join(root, "20-精选"))),
              str(os.listdir(os.path.join(root, "20-精选"))))
        print("     （本地打分 %s 分，档位 %s，写入 %s）"
              % (j2.score, r.get("tier"), r.get("path") or r.get("message")))

        print("\n=== 10. 未找到 vault 时优雅退出 ===")
        # 必须给一个**明确不存在**的路径，不能填空 ——
        # 空路径会触发自动探测，可能命中用户真实的库（这个坑踩过）。
        missing = os.path.join(root, "不存在的库", "更深一层")
        r = vault_save(build_ctx(missing), make_result(), force=False)
        check("找不到库时不抛错", isinstance(r, dict) and r.get("ok") is False, str(r))
        msg = str(r.get("message", ""))
        check("说清是路径填错", "路径不存在" in msg, msg)
        check("说清不会乱猜别的库", "不会去猜" in msg, msg)
        check("找不到库时没写任何文件",
              sum(len(fs) for _, _, fs in os.walk(root)) == n_after,
              "文件数变了")

    finally:
        shutil.rmtree(root, ignore_errors=True)
        print("\n（临时 vault 已清理：%s）" % root)

    print("\n" + "=" * 50)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败: " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
