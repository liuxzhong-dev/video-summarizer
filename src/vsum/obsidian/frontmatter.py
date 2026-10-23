"""生成 Obsidian 笔记的 YAML 头（frontmatter）。

Obsidian 靠文件开头的 YAML 头把笔记变成可查询的记录：没有它，一堆 md 就只是文本堆。
手写而不引 PyYAML 的原因有两个：打包体积已经不小；而且我们只需要输出，
需要的字段全是自己控制的自定义类型，风险低于引入一个通用 YAML 库。

生成策略是**按类型分流**，避免出现这种让 Obsidian 解析失败的写法::

    tags: [视频笔记, 3D 标注]      # 值里有空格和中文，裸写会被当成非法 YAML

复杂值一律加引号转义，宁可保守。
"""

from __future__ import annotations

import re
from typing import Any

#: 需要加引号才安全的字符：YAML 里这些字符开头会改变解析结果
_NEEDS_QUOTE = re.compile(r'^[\s\[\]{}>|*&!%#`@\'"-]|:\s|\s$|[\n\r]')
_LEADING_SPECIAL = "-?:,[]{}#&*!|>'\"%@`"


def scalar(value: Any) -> str:
    """把一个值渲染成安全的 YAML 标量。"""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if s == "":
        return '""'
    # 看起来像数字但本来是字符串（如标题 "2024"），要加引号否则会被读成 int
    looks_numeric = re.fullmatch(r"[-+]?\d+(\.\d+)?", s) is not None
    if looks_numeric or _NEEDS_QUOTE.search(s):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def inline_list(values: list) -> str:
    """渲染成 Obsidian 友好的行内列表（它就是按这个格式展示多标签的）。"""
    if not values:
        return "[]"
    return "[" + ", ".join(scalar(v) for v in values) + "]"


def dump(fields: dict) -> str:
    """生成 ``---`` 包裹的 YAML 头字符串（末尾带换行）。"""
    lines = ["---"]
    for key, value in fields.items():
        # 空串跟 None 一样没内容，写进 YAML 头只会让笔记变脏。
        # 注意 0 和 False 是有效值，不能一起跳掉。
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            lines.append(f"{key}: {inline_list(value)}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            s = str(value)
            # 嵌进列表的值若含换行会破坏结构，这里压平
            if "\n" in s:
                s = " ".join(s.split())
            lines.append(f"{key}: {scalar(s)}")
    lines.append("---")
    return "\n".join(lines) + "\n"
