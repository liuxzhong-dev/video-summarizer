#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把本机 Playwright 的 **headless shell 内核** 精简后复制一份到 ``vendor/pw-browsers/``，
供 PyInstaller 打进 exe。

为什么要有这个脚本
------------------
``playwright`` 这个 Python 包只带**驱动**（node + 协议实现，约 100 MB）；
真正的浏览器内核是 ``playwright install`` 单独下载的，落在
``%LOCALAPPDATA%\\ms-playwright\\``，**不在 Python 包里，也不在 exe 里**。

于是分发时会出现一个很隐蔽的缺口：我们的 exe 里 playwright 齐全
（v17 实测归档里 182 个条目 + node.exe 都在），但拿到 exe 的那台机器上
如果既没有 Edge 也没有 Chrome、且从没跑过 ``playwright install``，
浏览器兜底就整个失效 —— 抖音解析失败，而用户看不出原因。

v18 起把内核也打进 exe（B1 方案）。但不能整个 271 MB 硬塞，所以这里做精简。

精简掉了什么（每一项都实测过能正常启动并过抖音风控）
------------------------------------------------------
===============================  ========  ==========================
删除项                           体积      为什么能删
===============================  ========  ==========================
``locales/`` 除 zh-CN/en-US       42 MB    220 种语言的界面文案，抓流用不到；
                                           ``locale="zh-CN"`` 只认这两个
``LICENSE.headless_shell``       2.2 MB   许可证文本，运行时不读
``hyphen-data/``                 1.8 MB   断字词典（西文换行用），中文用不上
``vk_swiftshader.dll``           5.2 MB   SwiftShader **Vulkan** 软件渲染，
``vulkan-1.dll``                 964 KB   无头抓流不开 GPU 光栅化；
``vk_swiftshader_icd.json``      1 KB     保留 libEGL/libGLESv2 兜住基础 GL 初始化
``PrivacySandboxAttestationsPreloaded/``  9 KB  隐私沙箱预置声明，抓流无关
===============================  ========  ==========================

**保留 libEGL.dll / libGLESv2.dll**：这俩是 Chromium 启动时做 GL 能力探测会
加载的兄弟库（删掉 vk_swiftshader 后它们是唯一的 GL 实现），留着更稳。

最终：271 MB → 220 MB，经 PyInstaller 压缩后约 +100 MB（实测 zlib level 9：
完整 114.0 MiB / 精简 104.0 MiB）。

用法::

    python tools/prepare_browser_vendor.py          # 生成 vendor（已存在则跳过复制）
    python tools/prepare_browser_vendor.py --force   # 强制重建
    python tools/prepare_browser_vendor.py --check   # 只看有没有，不复制
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

#: 精简时保留的界面语言。抓视频不需要多语言 UI，但 zh-CN 决定了页面里
#: ``navigator.language`` 的观感，en-US 是 Chromium 的兜底默认值。
KEEP_LOCALES = ("zh-CN.pak", "en-US.pak")

#: 整个目录直接跳过（见文件头表格）
SKIP_DIRS = {"hyphen-data", "PrivacySandboxAttestationsPreloaded"}

#: 单文件直接跳过
SKIP_FILES = {"LICENSE.headless_shell", "vk_swiftshader.dll", "vulkan-1.dll",
              "vk_swiftshader_icd.json"}


def _browsers_root() -> str:
    """本机 Playwright 浏览器内核根目录。"""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    local = os.environ.get("LOCALAPPDATA", "")
    return os.path.join(local, "ms-playwright") if local else ""


def find_headless_shell() -> tuple[str, str]:
    """返回 ``(chrome-headless-shell-win64 目录, revision)``；找不到返回 ``("", "")``。"""
    root = _browsers_root()
    if not root or not os.path.isdir(root):
        return "", ""
    cands = []
    for name in os.listdir(root):
        if not name.startswith("chromium_headless_shell-"):
            continue
        exe_dir = os.path.join(root, name, "chrome-headless-shell-win64")
        if os.path.isfile(os.path.join(exe_dir, "chrome-headless-shell.exe")):
            cands.append((name.rsplit("-", 1)[-1], exe_dir))
    if not cands:
        return "", ""
    # revision 是纯数字，取最新版本
    cands.sort(key=lambda t: int(t[0]) if t[0].isdigit() else 0)
    return cands[-1][1], cands[-1][0]


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def build_vendor(dst_root: str, force: bool = False) -> tuple[str, int, int]:
    """生成精简副本，返回 ``(目标目录, 精简后字节数, 原始字节数)``。"""
    src, rev = find_headless_shell()
    if not src:
        raise SystemExit(
            "[中止] 本机没有 chromium-headless-shell 内核。\n"
            "       先下载：python -m playwright install chromium-headless-shell\n"
            "       （约 130 MB，只此一次；完整 chromium 不用下，433 MB 且我们不用）")

    dst = os.path.join(dst_root, "chromium_headless_shell-%s" % rev,
                       "chrome-headless-shell-win64")
    if os.path.isdir(dst) and not force:
        return dst, _dir_size(dst), _dir_size(src)
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)

    for entry in os.listdir(src):
        s = os.path.join(src, entry)
        if os.path.isdir(s):
            if entry in SKIP_DIRS:
                continue
            if entry == "locales":
                os.makedirs(os.path.join(dst, "locales"), exist_ok=True)
                for loc in KEEP_LOCALES:
                    f = os.path.join(s, loc)
                    if os.path.isfile(f):
                        shutil.copy2(f, os.path.join(dst, "locales", loc))
                continue
            shutil.copytree(s, os.path.join(dst, entry))
        else:
            if entry in SKIP_FILES:
                continue
            shutil.copy2(s, os.path.join(dst, entry))

    # 这两个标记文件是 Playwright 判断"这个内核装完了"的依据。
    # 我们走 executable_path 启动，不看它；但带上更接近官方布局，排查时省事。
    outer = os.path.dirname(dst)
    for marker in ("INSTALLATION_COMPLETE", "DEPENDENCIES_VALIDATED"):
        open(os.path.join(outer, marker), "a").close()

    return dst, _dir_size(dst), _dir_size(src)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在也重建")
    ap.add_argument("--check", action="store_true", help="只检查，不复制")
    args = ap.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vendor = os.path.join(here, "vendor", "pw-browsers")

    if args.check:
        ok = os.path.isdir(vendor) and any(
            n.startswith("chromium_headless_shell-") for n in os.listdir(vendor)
        ) if os.path.isdir(vendor) else False
        print("[%s] vendor/pw-browsers" % ("OK" if ok else "缺失"))
        return 0 if ok else 1

    dst, slim, full = build_vendor(vendor, args.force)
    mb = lambda n: n / 1024 / 1024
    print("内核来源: %s" % find_headless_shell()[0])
    print("精简副本: %s" % dst)
    print("体积: %.0f MB → %.0f MB（省 %.0f MB）" % (mb(full), mb(slim), mb(full - slim)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
