# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

产出两个**单文件**可执行程序（distpath 下直接就是 exe，没有 `_internal` 目录）：

* ``VideoSummarizer.exe`` —— 桌面界面（无控制台）
* ``vsum.exe``             —— 命令行工具（有控制台）

**为什么是单文件（不要 COLLECT）**::

    早先的 spec 里，``EXE()`` 传了 binaries/datas，**同时又写了一个 COLLECT**。
    这不是"双保险"，而是把同一套依赖生成了两份：

    * exe 里内嵌了一整份（onefile 行为，启动时解压到 %TEMP%）
    * ``_internal\\`` 里又躺了一整份（约 420-500 MB）

    实测（2026-10-09）：带 `_internal` 启动 4.73s / 4.64s，把 exe 单独拷到
    空目录启动 4.49s / 4.52s —— **两者一样**。也就是说 exe 每次启动都在解压，
    ``_internal`` 从头到尾没被读过，是纯浪费的几百 MB。

    所以这里**只保留 EXE、不写 COLLECT**，产物就是干净的单文件。
    单文件对用户也更重要：发到别的电脑时只需拷一个 exe，不用管什么文件夹。

**为什么要分两次构建**：同一个 spec 里的两个 EXE 共用一份 workpath，
第二个 EXE 会把第一个生成的 ``base_library.zip`` 整包删掉，
在开了批量删除保护的环境里会被拦下，导致只产出第一个。
所以用环境变量 ``VSUM_TARGET`` 选择本次构建哪个，并各自指定独立 workpath::

    set VSUM_TARGET=gui && pyinstaller VideoSummarizer.spec --noconfirm --workpath build/gui --distpath dist
    set VSUM_TARGET=cli && pyinstaller VideoSummarizer.spec --noconfirm --workpath build/cli --distpath dist

``build.bat`` 已经封装好了，直接双击即可。
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

import imageio_ffmpeg
import importlib.util
import os

# vsum 自己的子模块也要全量收集（插件是靠注册表发现的，静态分析扫不到）
vsum_mods = collect_submodules("vsum")

# 内置一份 ffmpeg（用于抽取音频），免得用户自己装
ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

# yt-dlp 的 extractor 是按需导入的，必须全量收集，否则打包后只认得少数站点
yt_mods = collect_submodules("yt_dlp")
fw_mods = collect_submodules("faster_whisper")

# Playwright —— 浏览器兜底：抖音/快手/小红书这类强风控平台靠它拿视频流。
#
# **必需依赖，缺了直接中止打包**（2026-10-10 从"可选"改成这样）：
#
#   原先的写法是 `collect_submodules(...) if find_spec(...) else []`，
#   也就是"装了就打、没装静默跳过"，当时的理由是"不该让一个可选功能拖垮构建"。
#   代价是**它真的静默降级了**：某次 venv 里没了 playwright，之后每一版 exe
#   都悄悄少了这块能力（实测：老版 dist 有 182 个 playwright 条目，
#   v13 / v15 / v16 全是 0），而用户侧看到的只有"抖音解析失败"这一个模糊症状，
#   根本联想不到是打包缺了东西。
#
#   既然目标是"每一版功能都完整"，就不能让必需能力静默消失 ——
#   和下面的 pywebview 一样：缺了就在**构建时**炸掉，逼人先把环境装齐。
#
# 注：运行时用的是**系统已装的 Edge/Chrome**（channel="msedge"），
#     所以**不需要**跑 `playwright install chromium`，
#     也就不会多出几百 MB 的浏览器内核，只多约 40 MB driver。
if not importlib.util.find_spec("playwright"):
    raise SystemExit(
        "[打包中止] 缺少 playwright，抖音/快手/小红的浏览器兜底会失效。\n"
        "           装一下再打包：.venv\\Scripts\\pip install playwright\n"
        "           （不用跑 playwright install，运行时直接用系统 Edge/Chrome）"
    )
pw_mods = collect_submodules("playwright")
# playwright 的 driver（node 驱动 + 包清单）是**数据文件**，
# 漏了它不会构建失败，而是打包后 launch() 找不到内核、运行时才报错 —— 必须一起收。
pw_datas = collect_data_files("playwright")
# 这两个是 playwright 的依赖包，属于**顶层包**，collect_submodules("playwright")
# 不会带出来。playwright 的 import 写在函数体内（延迟导入），静态分析容易漏，
# 显式写上最省事 —— 漏了的症状同样是"打包成功、运行时 launch() 报 ImportError"。
pw_deps = ["greenlet", "pyee"]

# ---- 内置浏览器内核（v18 / B1 方案）----
#
# 上面打进去的只是 playwright **驱动**（node + 协议实现，约 100 MB），
# 浏览器本体默认在 %LOCALAPPDATA%\ms-playwright（271 MB），**不在包里**。
# 后果：把 exe 发给没装 Edge/Chrome 的人，抖音解析直接失败，且看不出原因。
#
# v18 起把精简后的 chrome-headless-shell 一并打进 exe（271 MB → 219 MB，
# 压缩后给 exe 加约 100 MB）。生成这份副本的是 tools/prepare_browser_vendor.py。
#
# **和 playwright 一样是硬校验**：缺了就中止构建，不静默产出"没有内核"的 exe。
# 确实要跳过（比如只想快速打个自测包）时设 VSUM_BUNDLE_BROWSER=0。
def _bundled_browser_datas():
    root = os.path.join(SPECPATH, "vendor", "pw-browsers")
    if not os.path.isdir(root):
        return []
    out = []
    for dirpath, _dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace("\\", "/")
        dest = "pw-browsers" if rel == "." else "pw-browsers/" + rel
        for f in files:
            out.append((os.path.join(dirpath, f), dest))
    return out

if os.environ.get("VSUM_BUNDLE_BROWSER", "1") == "0":
    pw_browser_datas = []
else:
    pw_browser_datas = _bundled_browser_datas()
    if not pw_browser_datas:
        raise SystemExit(
            "[打包中止] 没有内置浏览器内核（vendor/pw-browsers 为空）。\n"
            "           先生成一份精简副本：python tools/prepare_browser_vendor.py\n"
            "           （它会从本机 %LOCALAPPDATA%\\ms-playwright 复制并精简，\n"
            "            本机没有就先跑 python -m playwright install "
            "chromium-headless-shell）\n"
            "           确实不想内置内核：set VSUM_BUNDLE_BROWSER=0 后重新打包"
        )

# pywebview 提供内嵌桌面窗口。**它没装的话必须在这里炸掉**：
# collect_submodules 对不存在的包只返回空列表、不报错，于是打包"成功"，
# 但 exe 双击后 import webview 失败 → 退回浏览器 → 而浏览器模式下
# 主线程会提前 return，daemon 服务线程跟着死 → 浏览器刷新就连接被拒。
# 这个坑排查过一次，别让它再悄悄发生。
try:
    import webview  # noqa: F401
    webview_mods = collect_submodules("webview")
except ImportError:
    raise SystemExit(
        "[打包中止] 缺少 pywebview，界面版会双击打不开。\n"
        "           装一下再打包：pip install pywebview\n"
        "           （如果确定要退化到浏览器模式，把下面这行 raise 改成 "
        "webview_mods = []，并记得 launcher 也得能撑住进程不退出）"
    )

# 文档解析（PDF / Word / Excel / PPT）—— 「把文件拖进来就能总结」靠这几个库。
# 它们都是**模块内延迟 import**（缺了就提示用户 pip install），
# 静态分析有可能扫不到；而且 docx / pptx **自带模板数据文件**，
# 漏了 collect_data_files 打包后会报「找不到 default.pptx」这类错。
# 没装时不报错、照常打包，只是拖这类文件会提示装库（可选降级）。
doc_mods = []
doc_datas = []
for _pkg in ("docx", "pptx", "openpyxl"):
    if importlib.util.find_spec(_pkg):
        doc_mods.append(_pkg)
        doc_datas += collect_data_files(_pkg)
# pypdf / xlrd 都是纯 Python，无数据文件，但延迟 import 静态扫不到，显式带上。
# xlrd 管的是旧版 .xls；openpyxl 管 .xlsx，两者是**互补**不是替换关系
# （openpyxl 明确声明不支持 .xls，xlrd 2.x 又只留了 .xls）—— 少一个就有一类表格读不了。
for _pkg in ("pypdf", "xlrd"):
    if importlib.util.find_spec(_pkg):
        doc_mods.append(_pkg)
if importlib.util.find_spec("PIL"):
    doc_mods.append("PIL")
# python-pptx 读图表数据时才用 XlsxWriter，静态扫不到，显式带上
if importlib.util.find_spec("xlsxwriter"):
    doc_mods.append("xlsxwriter")
# openpyxl 的读取链路里会用到 et_xmlfile（依赖包，静态分析时容易漏）
if importlib.util.find_spec("et_xmlfile"):
    doc_mods.append("et_xmlfile")

common_kwargs = dict(
    pathex=["src"],
    binaries=[(ffmpeg_exe, ".")],
    datas=[
        ("src/vsum/ui/assets/index.html", "vsum/ui/assets"),
    ] + collect_data_files("faster_whisper")   # VAD 模型等资源，漏了转写会直接失败
      + doc_datas
      + pw_datas                               # playwright driver（node 内核），漏了 launch() 必失败
      + pw_browser_datas,                      # 内置精简浏览器内核（v18），换台电脑也能解析抖音
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "matplotlib", "scipy", "pandas", "notebook", "PIL.ImageQt",
    ],
    noarchive=False,
    cipher=None,
)

#: 本次构建目标：gui / cli / all。默认 gui（与旧行为一致，直接 pyinstaller 也能跑）
_target = os.environ.get("VSUM_TARGET", "gui").strip().lower()

if _target in ("gui", "all"):
    # ---------------- 界面版 ----------------
    a_gui = Analysis(
        ["run_ui.py"],
        hiddenimports=vsum_mods + yt_mods + fw_mods + webview_mods + pw_mods + pw_deps
        + doc_mods + [
            "numpy", "onnxruntime", "ctranslate2", "clr", "pythonnet",
        ],
        **common_kwargs,
    )
    pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data, cipher=None)
    # 注意：**不要**再写 COLLECT。理由见文件顶部说明 —— 那会把依赖重复生成一份。
    exe_gui = EXE(
        pyz_gui, a_gui.scripts, a_gui.binaries, a_gui.zipfiles, a_gui.datas, [],
        name="VideoSummarizer",
        debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        runtime_tmpdir=None, console=False, disable_windowed_traceback=False,
        argv_emulation=False, target_arch=None, codesign_identity=None,
        entitlements_file=None,
    )

if _target in ("cli", "all"):
    # ---------------- 命令行版 ----------------
    a_cli = Analysis(
        ["run_cli.py"],
        hiddenimports=vsum_mods + yt_mods + fw_mods + pw_mods + pw_deps + doc_mods + [
            "numpy", "onnxruntime", "ctranslate2",
        ],
        **common_kwargs,
    )
    pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=None)
    # 同上：只出 EXE，不写 COLLECT。
    exe_cli = EXE(
        pyz_cli, a_cli.scripts, a_cli.binaries, a_cli.zipfiles, a_cli.datas, [],
        name="vsum",
        debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        runtime_tmpdir=None, console=True, disable_windowed_traceback=False,
        argv_emulation=False, target_arch=None, codesign_identity=None,
        entitlements_file=None,
    )
