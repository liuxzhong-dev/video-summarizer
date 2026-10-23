"""定位 Obsidian vault 并安全地写入 Markdown 文件。

**为什么直接写文件、而不用 Obsidian 的 API**

Obsidian 的 vault 本质就是一个普通文件夹，里面是纯文本 ``.md``。官方没有写入 API
（只有社区插件提供的本地 REST API）。直接写文件有三个好处：

* 不需要装任何插件、不需要 Obsidian 处于运行状态；
* 不引入网络/端口/鉴权这些额外的失败点；
* Obsidian 会在下次打开时自动读取新文件，用不着通知它。

唯一要注意的是**别写坏用户已有的笔记**，所以下面几处都做了保守处理：
文件名冲突自动加序号、只写 ``.md``、路径逃逸拦截、原子替换。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Optional

from ..core.errors import VSumError

#: Obsidian 的 vault 根目录下都有这个文件夹，用它来确认「这确实是个 vault」
MARKER = ".obsidian"


def looks_like_vault(path: str) -> bool:
    """目录看起来像个 Obsidian vault 吗（只看有没有 ``.obsidian/``）。"""
    try:
        return os.path.isdir(os.path.join(path, MARKER))
    except (OSError, TypeError):
        return False


def find_vault(hint: str = "") -> Optional[str]:
    """尽量自动找到 vault 路径。

    顺序：用户填的路径 → 从 Obsidian 自己的配置文件里读 → 常见位置扫一遍。
    返回 ``None`` 表示没找到，调用方要提示用户手动填。

    **填了路径就只认这个路径。** 路径不存在时直接返回 None，不继续自动探测 ——
    否则用户填错一个字符，程序会悄悄把笔记写进另一个库，
    他以为写进了 A 库、实际躺在 B 库，这种错比直接报错难查得多。
    """
    if hint:
        p = os.path.abspath(os.path.expanduser(hint.strip()))
        return p if os.path.isdir(p) else None

    # Obsidian 会把已打开过的 vault 记在配置里，这是最准的来源
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg = os.path.join(appdata, "obsidian", "obsidian.json")
    if os.path.isfile(cfg):
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
            # {"vaults": {"<id>": {"path": "...", "name": "..."}, ...}}
            for item in (data.get("vaults") or {}).values():
                p = item.get("path") or ""
                if p and os.path.isdir(p):
                    return os.path.abspath(p)
        except (OSError, ValueError, AttributeError):
            pass

    # 兜底：常见位置浅层找带 .obsidian 的目录
    home = os.path.expanduser("~")
    for base in (os.path.join(home, "Documents"), os.path.join(home, "Desktop"),
                 home, os.path.join(home, "OneDrive"), os.path.join(home, "云盘")):
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                p = os.path.join(base, name)
                if os.path.isdir(p) and looks_like_vault(p):
                    return os.path.abspath(p)
        except OSError:
            continue
    return None


#: Windows 与 Obsidian 都不接受的文件名字符，另加控制字符
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')


def safe_note_name(title: str, maxlen: int = 60) -> str:
    """把视频标题转成合法且好找的文件名。

    Obsidian 的链接是按**文件名**匹配的，所以文件名太随意会让 ``[[双链]]`` 难写；
    这里保留中文、去掉非法字符、压缩空白。
    """
    name = _BAD_CHARS.sub(" ", title or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Windows 保留设备名，不能当文件名
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL",
                                      "COM1", "COM2", "LPT1", "LPT2"}:
        name = "_" + name
    return (name[:maxlen] or "未命名笔记").strip(" .")


def unique_path(folder: str, stem: str) -> str:
    """返回 ``folder/stem.md``；已存在就加 ``(2)`` ``(3)`` 序号。

    绝不能直接覆盖用户可能已经手动改过的笔记 —— 宁可多一个文件。
    """
    candidate = os.path.join(folder, stem + ".md")
    if not os.path.exists(candidate):
        return candidate
    for i in range(2, 1000):
        candidate = os.path.join(folder, f"{stem} ({i}).md")
        if not os.path.exists(candidate):
            return candidate
    raise VSumError("同名笔记过多，无法生成新文件名", hint=f"目录：{folder}")


def write_note(path: str, content: str) -> str:
    """原子写入一个 Markdown 文件，返回最终路径。

    先写临时文件再 ``os.replace``：中途失败不会留下半截文件损坏笔记。
    """
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise VSumError(f"无法创建目录 {folder}：{exc}",
                         hint="检查 vault 路径是否正确、磁盘是否可写")

    # Obsidian 会在写入时按 UTF-8 读；newline="\n" 避免 Windows 写成 CRLF
    # 导致 YAML 头解析出错（YAML 规范要求 LF）
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".vsum-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, path)
        tmp = None
        return path
    except OSError as exc:
        raise VSumError(f"写入笔记失败：{exc}",
                         hint="若 vault 在 OneDrive/坚果云等同步盘里，确认已同步完成")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def ensure_inside(vault: str, path: str) -> str:
    """确认 ``path`` 落在 ``vault`` 之内，挡住 ``../`` 之类的路径逃逸。"""
    v = os.path.abspath(vault)
    p = os.path.abspath(path)
    if p != v and not p.startswith(v + os.sep):
        raise VSumError("目标路径超出 vault 范围，已阻止写入", hint=f"vault：{v}")
    return p
