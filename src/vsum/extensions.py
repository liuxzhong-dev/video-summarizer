"""插件装载入口。

``vsum`` 被导入时会调用这里的 ``load_all()``，把三类插件都注册好；
用户自建插件目录（默认 ``<程序目录>/plugins``）里的 ``.py`` 也会一起加载。
"""

from __future__ import annotations

import os
import sys

_LOADED = False


def default_plugin_dir() -> str:
    if getattr(sys, "frozen", False):
        root = os.path.dirname(sys.executable)
    else:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "plugins")


def load_builtin(force: bool = False) -> None:
    """加载内置插件。

    这里**显式 import 每一个模块**而不是靠 ``pkgutil`` 扫描，是因为
    PyInstaller 打包后模块被塞进 PYZ 归档，``pkgutil.iter_modules`` 扫不到，
    结果就是「源码跑得好好的，打包后只剩通用适配器」。
    显式 import 既能被 PyInstaller 静态分析到，也更直观。

    逐个 try：某个插件缺依赖时只丢一条 WARNING，不影响其他插件。
    """
    global _LOADED
    if _LOADED and not force:
        return
    import logging

    log = logging.getLogger(__name__)

    # 逐个 try，且必须是**字面量 import 语句**：PyInstaller 靠静态分析收集模块，
    # 写成 __import__("vsum.xxx") 的话打包后就只剩通用适配器了。
    try:
        from .platforms import bilibili  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 bilibili 加载失败：%s", exc)
    try:
        from .platforms import douyin  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 douyin 加载失败：%s", exc)
    try:
        from .platforms import kuaishou  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 kuaishou 加载失败：%s", exc)
    try:
        from .platforms import xiaohongshu  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 xiaohongshu 加载失败：%s", exc)
    try:
        from .platforms import weixin_channels  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 微信视频号 加载失败：%s", exc)
    try:
        from .platforms import article  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 article 加载失败：%s", exc)
    try:
        from .platforms import generic_ytdlp  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 generic 加载失败：%s", exc)
    try:
        from .asr import faster_whisper  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 faster-whisper 加载失败：%s", exc)
    try:
        from .asr import cloud  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 cloud-asr 加载失败：%s", exc)
    try:
        from .llm import openai_compat  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 openai-compat 加载失败：%s", exc)
    try:
        from .llm import extractive  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 extractive 加载失败：%s", exc)
    # RAG（语义检索）—— 向量化引擎与向量库。
    # 这两个模块一旦漏打，症状是「检索退化成关键词、但程序不报错」，
    # 属于最难从表象倒推根因的一类问题，所以和上面一样逐个 try + 记日志。
    try:
        from .rag import embedder as _rag_embedder  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 rag-embedder 加载失败：%s", exc)
    try:
        from .rag import store as _rag_store  # noqa: F401
    except Exception as exc:
        log.warning("内置插件 rag-store 加载失败：%s", exc)

    # 源码模式下再扫一遍，方便「丢个新文件进去就生效」
    try:
        from .core import registry
        registry.autoload(["vsum.platforms", "vsum.asr", "vsum.llm", "vsum.rag"])
    except Exception as exc:
        # 这里以前是 `except Exception: pass` —— 静默吞掉。
        # 后果是：某个平台模块导入失败时，注册表里只剩一部分插件，
        # 但 _LOADED 已经置 True，再调 load_all() 会被直接跳过，
        # 于是**其他平台永久丢失**，症状是「只有某一个平台能用」，
        # 而且没有任何日志线索。至少要留下痕迹。
        log.warning("扫描源码模式插件失败（内置插件不受影响）: %s", exc)

    # 装载后自检：内置的六个平台必须都在。
    # 少任何一个都说明上面的某个 import 挂了 —— 而调用方看到的是
    # 「这个平台解析不出来」，完全猜不到是装载出了问题。
    try:
        from .core import registry
        missing = [k for k in ("bilibili", "douyin", "kuaishou",
                               "xiaohongshu", "weixin_channels", "generic")
                   if k not in registry.all_platforms()]
        if missing:
            log.warning("以下内置平台未成功注册，对应链接会退到通用解析器: %s",
                        ", ".join(missing))
        # RAG 两个插件同样自检：它们不在时程序照常运行，只是检索悄悄退化成
        # 关键词匹配 —— 不主动报出来的话，使用方会把「搜不准」归咎于语料质量。
        if not registry.all_embedders():
            log.warning("向量化引擎未注册，RAG 检索将退化为关键词匹配")
        if not registry.all_stores():
            log.warning("向量库未注册，RAG 建库与检索都不可用")
    except Exception as exc:
        log.warning("插件自检失败: %s", exc)

    _LOADED = True


def load_all(plugin_dir: str = "", force: bool = False) -> int:
    """加载内置插件 + 外部插件，返回外部插件数量。"""
    from .core import registry

    load_builtin(force=force)
    return registry.load_external_plugins(plugin_dir or default_plugin_dir())
