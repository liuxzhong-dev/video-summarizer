"""知识库入库的对外统一入口。

对外只暴露两个函数：

* :func:`status` —— 报告 vault 是否就绪、有多少笔记了（界面顶部展示用）；
* :func:`save`   —— 筛选 → 重写 → 写入 → 更新索引，一步到位。

设计原则是**永不抛错打断主流程**：知识库是附加功能，视频总结成功但入库失败时，
总结结果必须照样交付，只是提示一句入库没成。
"""

from __future__ import annotations

import os
from typing import Optional

from ..core.context import Context
from ..core.errors import VSumError
from ..core.models import SummaryResult
from . import curator, knowledge, note, vault
from .curator import Judgement, TIER_HIGH, TIER_LOW, TIER_MID

#: 三档分别写到哪个子目录。
#: 默认值对齐用户库的成熟度分层结构
#: （``00-MOC`` / ``10-项目`` / ``20-精选`` / ``30-待定`` / ``40-日志`` / ``90-附件``）——
#: 精选=80 分以上（判定了可长期复用），待定=60-80 分（还没定性，留着观察）。
#: 一条轴只表达「成熟度」；早先把待定笔记放进「30-日志」是命名错误——
#: 「待定」是成熟度，「日志」是时间，两者混在一起没法区分。
#: 视频笔记属于可复用的知识，归到「精选」最合适。
FOLDER_HIGH = "20-精选"
FOLDER_MID = "30-待定"

# 这两个常量是「配置读不到时的兜底」。而DEFAULT_CONFIG 里也有一份，
# 两处不一致的话，程序在「配置缺失」和「配置存在」两种状态下
# 会写进不同目录 —— 这种 bug 只在特定路径下出现，极难排查。
# 所以这里做一次导入期自检，不一致就直接炸（而不是等到用户踩坑）。
def _assert_defaults_match() -> None:
    from ..core.config import DEFAULT_CONFIG
    dflt = DEFAULT_CONFIG.get("obsidian", {})
    for key, const in (("folder_high", FOLDER_HIGH), ("folder_mid", FOLDER_MID)):
        other = dflt.get(key)
        if other and other != const:
            raise RuntimeError(
                f"默认目录名不一致：config.py 的 obsidian.{key}={other!r} "
                f"与 obsidian/__init__.py 的常量 {const!r} 不同。"
                f"两处必须同步，否则配置缺失时会写进不同目录。"
            )


_assert_defaults_match()


def _cfg_folder(ctx: Context, tier: str) -> str:
    """按档位取目标子目录，用户可以在设置里改名字。"""
    if tier == TIER_HIGH:
        return str(ctx.config.get("obsidian.folder_high", FOLDER_HIGH) or FOLDER_HIGH)
    if tier == TIER_MID:
        return str(ctx.config.get("obsidian.folder_mid", FOLDER_MID) or FOLDER_MID)
    return ""


def _no_vault_message(hint: str) -> str:
    """没找到库时的统一说法。

    界面和 CLI 都调这里，避免两处各写一份、措辞又不一致。
    """
    if hint:
        return ("填的路径不存在，程序不会去猜别的库"
                "（否则你以为写进了 A 库、其实躺在 B 库）。"
                "请填库根目录 —— 就是含 .obsidian 文件夹的那层；"
                "不确定就清空这一栏让程序自动探测")
    return "没找到 Obsidian 库。请创建库后把库根目录（含 .obsidian 那层）填进来"


def status(ctx: Context) -> dict:
    """vault 连接状态，供界面展示。"""
    cfg = ctx.config
    hint = str(cfg.get("obsidian.vault_path", "") or "")
    found = vault.find_vault(hint)
    out = {
        "configured": bool(hint),
        "found": bool(found),
        "vault": found or "",
        "enabled": bool(cfg.get("obsidian.enabled", False)),
        "note_count": 0,
        "llm_ready": False,
        "message": "",
    }
    if not found:
        out["message"] = _no_vault_message(hint)
        return out
    # 统计全库的视频笔记（可能分布在「方法」「日志」多个目录）
    try:
        out["note_count"] = knowledge.collect_stat(found, "")["count"]
    except OSError:
        pass

    # 筛选依赖大模型，没配 Key 就只能走保守的本地规则。
    # message 只说「原因」，结论由界面自己拼 —— 两边都写一遍会显示成两句话。
    from ..core.registry import all_llm
    cls = all_llm().get("openai-compat")
    if cls:
        ok, reason = cls(ctx).available()
        out["llm_ready"] = ok
        if not ok:
            out["message"] = reason
    return out


def _pick_engine(ctx: Context):
    """取一个可用的对话引擎。没有大模型时返回 None 走降级。"""
    from ..core.registry import all_llm
    cls = all_llm().get("openai-compat")
    if not cls:
        return None
    engine = cls(ctx)
    ok, _ = engine.available()
    return engine if ok else None


def save(ctx: Context, result: SummaryResult, force: bool = False) -> dict:
    """把一份总结入库。

    ``force=True`` 时无视分数直接写入精选区（用户已经自己判断过了）。

    返回 ``{"ok", "score", "tier", "reason", "path", "message", ...}``。
    除了 vault 根本没配这类硬错误，其余问题都以 ``ok=False`` + message 呈现，
    绝不抛异常打断上层。
    """
    cfg = ctx.config
    hint = str(cfg.get("obsidian.vault_path", "") or "")
    root = vault.find_vault(hint)
    if not root:
        return {"ok": False, "message": _no_vault_message(hint)}

    engine = _pick_engine(ctx)
    focus = str(cfg.get("obsidian.focus", "") or "")
    strict = bool(cfg.get("obsidian.strict", True))

    # 1) 筛选
    ctx.report("正在判断内容是否值得入库…", 93)
    if engine is not None:
        try:
            j = curator.judge(result, engine, focus, strict)
        except Exception as exc:
            ctx.log.warning("AI 筛选失败，降级为本地打分: %s", exc)
            j = curator.heuristic(result, focus)
            j.note = f"AI 筛选失败（{exc}），已降级为本地打分"
    else:
        j = curator.heuristic(result, focus)

    high = int(cfg.get("obsidian.threshold_high", 80) or 80)
    low = int(cfg.get("obsidian.threshold_low", 40) or 40)
    j = curator.apply_thresholds(j, high, low)

    if force:
        j.tier = TIER_HIGH
        # 强制保存时分数要跟着抬 —— 否则笔记里会留下
        # 「value: 20 / tier: 精选」这种自相矛盾的元数据，
        # 半年后回看会以为程序坏了。同时留痕说明这是手动放的。
        if j.score < high:
            j.score = high
            forced_note = "用户手动强制入库"
            j.reason = (j.reason + "；" if j.reason else "") + forced_note
    if j.tier == TIER_LOW and not force:
        return {"ok": True, "skipped": True, "score": j.score, "tier": j.tier,
                "reason": j.reason, "note": j.note,
                "message": f"评分 {j.score}，低于阈值 {low}，未写入文件。"
                           f"确实想存的话用「强制保存」。"}

    sub = _cfg_folder(ctx, j.tier)
    folder = os.path.join(root, sub) if sub else root

    # 2) 重写正文
    ctx.report("正在整理成可检索的笔记…", 95)
    body = ""
    if engine is not None:
        try:
            body = note.rewrite(result, j, engine)
        except Exception as exc:
            ctx.log.warning("笔记重写失败: %s", exc)
            j.note = (j.note + "；" if j.note else "") + f"重写失败（{exc}），已用摘要原文"
    if not body:
        body = note.fallback_note(result, j)

    # 3) 写文件（文件名加日期前缀，与用户库里的命名约定一致）
    stem = note.note_stem(result.meta.title, date_prefix=True)
    path = vault.unique_path(folder, stem)
    vault.ensure_inside(root, path)
    content = note.build_note(result, j, body, vault=root, subfolder=sub,
                              date_prefix=True)
    vault.write_note(path, content)

    # 4) 维护术语表与视频索引
    #    术语表是全库性的，放到 vault 根目录（而不是笔记所在目录）；
    #    视频索引放到 MOC 目录，与用户已有的「知识库导航首页」并列。
    #    只在精选档维护 —— 待阅区的内容还没定性，进索引会误导。
    glossary_path = map_path = None
    if j.tier == TIER_HIGH:
        glossary_path = knowledge.ensure_glossary(root, "")
        map_dir = str(ctx.config.get("obsidian.folder_moc", "00-MOC") or "00-MOC")
        map_path = knowledge.update_map(root, map_dir)

    return {"ok": True, "skipped": False, "score": j.score, "tier": j.tier,
            "reason": j.reason, "note": j.note, "path": path, "vault": root,
            "folder": sub, "entities": j.entities, "topics": j.topics,
            "map_path": map_path, "glossary_path": glossary_path,
            "message": f"已入库：{sub}/{os.path.basename(path)}（评分 {j.score}）"}
