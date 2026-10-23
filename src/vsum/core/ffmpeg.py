"""ffmpeg 定位与获取。

音频提取（用于语音转写）强依赖 ffmpeg。查找顺序：

1. 配置里手工指定的路径
2. PyInstaller 打包时内置的那份（``sys._MEIPASS``）
3. 已安装的 ``imageio-ffmpeg`` 包自带的二进制
4. 系统 PATH
5. 常见安装目录（winget / scoop / chocolatey）
6. 联网 ``pip install imageio-ffmpeg`` 拿一份
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .errors import MediaError


def _exe_name() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def find_ffmpeg() -> str:
    """按优先级查找 ffmpeg，找不到返回空字符串。"""
    name = _exe_name()

    # 1) PyInstaller 单文件模式解压目录 / 打包时 bundled
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        for rel in (name, os.path.join("ffmpeg", name), os.path.join("bin", name)):
            p = os.path.join(meipass, rel)
            if os.path.isfile(p):
                return p

    # 2) imageio-ffmpeg 包
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass

    # 3) PATH
    p = shutil.which("ffmpeg")
    if p:
        return p

    # 4) 常见安装位置
    local = os.environ.get("LOCALAPPDATA", "")
    for rel in (
        os.path.join(local, "Microsoft", "WinGet", "Packages"),
        os.path.join(os.path.expanduser("~"), "scoop", "shims"),
        r"C:\ProgramData\chocolatey\bin",
        r"C:\ffmpeg\bin",
    ):
        cand = os.path.join(rel, name)
        if os.path.isfile(cand):
            return cand
    return ""


def ensure_ffmpeg(workdir: str = "") -> str:
    """保证有 ffmpeg 可用；实在没有就尝试联网装 ``imageio-ffmpeg``。"""
    found = find_ffmpeg()
    if found:
        return found
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "imageio-ffmpeg"],
            check=True, timeout=300,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        import importlib
        importlib.invalidate_caches()
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    raise MediaError(
        "未找到 ffmpeg，无法提取音频做语音转写",
        hint="在「设置 → 媒体」手工指定 ffmpeg.exe 路径，或在命令行执行 "
             "pip install imageio-ffmpeg 后重试。若视频本身有字幕则不受影响。",
    )


def extract_audio(ffmpeg: str, source: str, out_wav: str,
                  max_minutes: float = 90) -> str:
    """把视频/音频抽成 16kHz 单声道 wav（Whisper 的标准输入）。

    ``source`` 可以是本地文件，也可以是 ffmpeg 能直接读的 URL。
    """
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", source,
        "-vn",                      # 不要视频
        "-ac", "1", "-ar", "16000",  # 单声道 16k
        "-f", "wav",
    ]
    if max_minutes and max_minutes > 0:
        cmd += ["-t", str(int(max_minutes * 60))]
    cmd.append(out_wav)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.isfile(out_wav):
        err = proc.stderr.decode("utf-8", "ignore")[-500:]
        raise MediaError(f"音频提取失败：{err}", hint="确认 ffmpeg 可用、链接未过期且未被平台风控")
    return out_wav


def probe_duration(ffmpeg: str, source: str) -> float:
    """用 ffmpeg 探测时长（秒），失败返回 0。"""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", source],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)",
                      proc.stderr.decode("utf-8", "ignore"))
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0
