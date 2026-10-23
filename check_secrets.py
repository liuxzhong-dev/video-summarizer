# -*- coding: utf-8 -*-
"""
密钥自查：列出所有 config.json 的 Key 状态。

用途：迁移或发给别人之前，确认该带的是脱敏版。
判定标准：任何 Key 字段非空的 config.json 都是「带密钥」的文件。
"""
import glob
import io
import json
import os

ROOT = r"C:\Users\LXZ\WorkBuddy\2026-10-05-00-31-58\video-summarizer"
SKIP_DIRS = {".git", "build", "out", "src", "plugins", "tests",
             "docs", "__pycache__", ".venv", "_edge2", "_edge_s",
             "_edge_v", "_old_dist"}


def key_state(path: str) -> tuple[int, int, list]:
    d = json.load(io.open(path, encoding="utf-8"))
    lk = (d.get("llm") or {}).get("api_key", "") or ""
    ak = (d.get("asr") or {}).get("cloud_api_key", "") or ""
    filled = [k for k, v in (("llm.api_key", lk), ("asr.cloud_api_key", ak)) if v]
    return len(lk), len(ak), filled


print("=" * 78)
print("密钥自查 —— 非空即敏感")
print("=" * 78)

rows = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames
                   if d not in SKIP_DIRS and not d.startswith("build")]
    for fn in filenames:
        if fn.startswith("config") and fn.endswith(".json"):
            rows.append(os.path.join(dirpath, fn))

# 项目根优先，其余按路径排序
rows.sort(key=lambda p: (os.path.dirname(p) != ROOT, p))

unsafe = []
for p in rows:
    rel = os.path.relpath(p, ROOT)
    try:
        lk, ak, filled = key_state(p)
    except Exception as exc:
        print(f"  [读取失败] {rel}: {exc}")
        continue
    tag = "🔴 带 Key" if filled else "🟢 已脱敏"
    if filled:
        unsafe.append((rel, filled))
    print(f"  {tag}  {rel:48s} LLM {lk:3d} / ASR {ak:3d} 字符")

print()
print("=" * 78)
if unsafe:
    print(f"⚠️  {len(unsafe)} 个文件含真实 Key，发给别人前必须处理：")
    for rel, keys in unsafe:
        print(f"    {rel}")
        for k in keys:
            print(f"      └─ {k}")
    print()
    print("处理方式（对方拿到的目录里应该有 config.example.json）：")
    print("    copy config.json config.json.mine")
    print("    del  config.json")
    print("    copy config.example.json config.json")
else:
    print("✅ 全部已脱敏，可以直接分发")
print("=" * 78)
