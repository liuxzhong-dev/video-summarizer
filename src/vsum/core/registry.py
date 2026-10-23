"""插件注册表 —— 这是「可扩展」的枢纽。

五类插件：

============  =========================================  ==========================
类型          注册装饰器                                   约定基类
============  =========================================  ==========================
platform      ``@register_platform``                      ``PlatformAdapter``
asr           ``@register_asr``                           ``AsrEngine``
llm           ``@register_llm``                           ``LlmEngine``
embedder      ``@register_embedder``                      ``rag.base.Embedder``
store         ``@register_store``                         ``rag.base.VectorStore``
============  =========================================  ==========================

后两类是 RAG 版新增的：前者负责「文本 → 向量」，后者负责「向量存在哪、怎么查」。
把它们做成插件而不是写死，是因为这两处最可能被替换 —— 换 embedding 服务商
（硅基流动 / OpenAI / 本地模型）或换向量库（本地 / FAISS / Chroma / Milvus）
都不该动上层的检索与问答代码。

新增一种能力只需三步（见 README「二次开发」）：

1. 在对应包下新建一个模块，继承基类并实现接口；
2. 用装饰器注册；
3. 在 ``vsum/extensions.py`` 里 ``import`` 一下（或直接放进
   ``plugins/`` 目录，程序启动时会自动扫描加载）。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import pkgutil
from typing import Callable, Optional, Type

log = logging.getLogger(__name__)

PLATFORMS: dict[str, Type] = {}
ASR_ENGINES: dict[str, Type] = {}
LLM_ENGINES: dict[str, Type] = {}
EMBEDDERS: dict[str, Type] = {}
VECTOR_STORES: dict[str, Type] = {}

#: 注册表 -> 对应的基类属性名（用于校验）
_BUCKETS = {
    "platform": PLATFORMS,
    "asr": ASR_ENGINES,
    "llm": LLM_ENGINES,
    "embedder": EMBEDDERS,
    "store": VECTOR_STORES,
}


def _register(bucket: str, cls: Type, key: str | None = None) -> Type:
    name = key or getattr(cls, "key", None) or cls.__name__.lower()
    table = _BUCKETS[bucket]
    if name in table:
        log.warning("插件 %s.%s 被重复注册，后者覆盖前者", bucket, name)
    table[name] = cls
    log.debug("注册 %s 插件: %s -> %s", bucket, name, cls.__name__)
    return cls


def register_platform(key: str | None = None) -> Callable[[Type], Type]:
    """注册平台适配器。"""
    return lambda cls: _register("platform", cls, key)


def register_asr(key: str | None = None) -> Callable[[Type], Type]:
    """注册语音转写引擎。"""
    return lambda cls: _register("asr", cls, key)


def register_llm(key: str | None = None) -> Callable[[Type], Type]:
    """注册大模型引擎。"""
    return lambda cls: _register("llm", cls, key)


def register_embedder(key: str | None = None) -> Callable[[Type], Type]:
    """注册向量化引擎（文本 -> 向量）。"""
    return lambda cls: _register("embedder", cls, key)


def register_store(key: str | None = None) -> Callable[[Type], Type]:
    """注册向量库实现（向量存在哪、怎么查）。"""
    return lambda cls: _register("store", cls, key)


def get_platform_cls(key: str) -> Optional[Type]:
    return PLATFORMS.get(key)


def all_platforms() -> dict[str, Type]:
    return dict(PLATFORMS)


def all_asr() -> dict[str, Type]:
    return dict(ASR_ENGINES)


def all_llm() -> dict[str, Type]:
    return dict(LLM_ENGINES)


def all_embedders() -> dict[str, Type]:
    return dict(EMBEDDERS)


def all_stores() -> dict[str, Type]:
    return dict(VECTOR_STORES)


def autoload(package_names: list[str]) -> None:
    """自动导入包内的所有模块，触发装饰器注册。"""
    for pkg in package_names:
        try:
            module = importlib.import_module(pkg)
        except Exception as exc:                       # pragma: no cover
            log.warning("加载插件包 %s 失败: %s", pkg, exc)
            continue
        for _, modname, _ in pkgutil.iter_modules(module.__path__):
            if modname.startswith("_"):
                continue
            try:
                importlib.import_module(f"{pkg}.{modname}")
            except Exception as exc:
                # 某个插件因为缺依赖挂了，不能拖垮整个程序
                log.warning("插件 %s.%s 加载失败: %s", pkg, modname, exc)


def load_external_plugins(directory: str) -> int:
    """从外部目录动态加载 .py 插件文件（用户自建插件目录）。

    返回成功加载的插件数量。文件里只要用了上面的装饰器就会被注册。
    """
    if not directory or not os.path.isdir(directory):
        return 0
    import sys

    count = 0
    if directory not in sys.path:
        sys.path.insert(0, directory)
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        modname = f"vsum_plugin_{fn[:-3]}"
        try:
            spec = importlib.util.spec_from_file_location(modname, os.path.join(directory, fn))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            count += 1
            log.info("已加载外部插件: %s", fn)
        except Exception as exc:
            log.warning("外部插件 %s 加载失败: %s", fn, exc)
    return count


def describe() -> list[dict]:
    """列出所有已注册插件（界面「关于/插件」页与 CLI ``--list`` 用）。"""
    rows = []
    for bucket, table in _BUCKETS.items():
        for key, cls in sorted(table.items()):
            rows.append({
                "type": bucket,
                "key": key,
                "class": f"{cls.__module__}.{cls.__name__}",
                "doc": inspect.getdoc(cls) or "",
            })
    return rows
