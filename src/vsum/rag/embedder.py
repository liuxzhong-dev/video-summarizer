"""向量化引擎 —— 把文本换成向量。

当前只有一个实现：硅基流动的 ``BAAI/bge-m3``。选它不是因为它是唯一选择，
而是因为本项目已经在用它家的语音识别，**Key 和 Base URL 都能直接复用**，
不必为了 RAG 再注册一个账号。

关于配置的一个设计：``rag.base_url`` / ``rag.api_key`` 留空时会**自动回落到
``asr.cloud_*``**。这两项本来就指向同一个服务商，要求用户把同一个 Key 抄两遍
是没必要的；想分开配置（比如 embedding 走别家）时，填上就能覆盖。
"""

from __future__ import annotations

import time

from ..core.errors import RagError
from ..core.registry import register_embedder
from .base import Embedder

#: 一次请求最多带多少条文本。bge-m3 单条上限 8192 token，
#: 但一次塞太多会被服务端限流；32 条是本机实测下来重试率最低的档位。
BATCH = 32

#: 这些状态码值得重试：限流、网关抖动、服务端临时异常。
_RETRYABLE = {429, 500, 502, 503, 504}


@register_embedder("siliconflow")
class SiliconFlowEmbedder(Embedder):
    """硅基流动 ``BAAI/bge-m3``（OpenAI 兼容 ``/embeddings``）。

    实测：1024 维；中文相关句与无关句的余弦相似度分别约 0.52 / 0.40 ——
    **区分度够用但绝对值不高**，所以别按 0.8 这种想当然的阈值做过滤，
    阈值要靠自己的语料实测校准（见 ``retriever`` 里的 min_score）。
    """

    key = "siliconflow"
    name = "硅基流动 bge-m3"
    note = "OpenAI 兼容接口，1024 维；留空配置时自动复用云端 ASR 的 Key"
    dim = 1024

    #: 单次请求超时（秒）。向量化是同步等待，别设太长。
    timeout = 60
    retries = 2

    # ---------------- 可用性与配置 ----------------
    def _endpoint(self) -> tuple[str, str, str]:
        """返回 ``(base_url, api_key, model)``，把回落逻辑收在一处。"""
        cfg = self.ctx.config
        base = (cfg.get("rag.base_url", "") or "").strip()
        key = (cfg.get("rag.api_key", "") or "").strip()
        if not base:
            base = (cfg.get("asr.cloud_base_url", "") or "").strip()
        if not key:
            key = (cfg.get("asr.cloud_api_key", "") or "").strip()
        model = (cfg.get("rag.embed_model", "") or "BAAI/bge-m3").strip()
        return base.rstrip("/"), key, model

    def available(self) -> tuple[bool, str]:
        base, key, model = self._endpoint()
        if not base:
            return False, "未配置向量服务地址"
        if not key:
            return False, "未配置向量服务 API Key（可在设置里填，或复用云端 ASR 的 Key）"
        if not model:
            return False, "未配置向量模型名"
        return True, ""

    # ---------------- 向量化 ----------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。输入输出严格一一对应，条数不符即抛错。

        抛错而不是「补零凑数」：长度对不上意味着写入向量库时会把 A 的向量
        配到 B 的文本上，属于会静默污染整个库的错误，必须立刻停下。
        """
        ok, reason = self.available()
        if not ok:
            raise RagError(reason, hint="在 config.json 的 rag 段填写 base_url 与 api_key")

        clean = [(t or "").strip() or " " for t in texts]
        if not clean:
            return []

        out: list[list[float]] = []
        for i in range(0, len(clean), BATCH):
            out.extend(self._embed_batch(clean[i:i + BATCH]))
        if len(out) != len(clean):
            raise RagError(
                f"向量条数与输入不符：输入 {len(clean)} 条，返回 {len(out)} 条",
                hint="向量与文本错配会污染整个库，已中止本次写入",
            )
        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        base, key, model = self._endpoint()
        url = f"{base}/embeddings"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {key}"}
        payload = {"model": model, "input": batch, "encoding_format": "float"}

        last = ""
        for attempt in range(self.retries + 1):
            try:
                resp = self.ctx.http.session.post(url, headers=headers,
                                                  json=payload, timeout=self.timeout)
            except Exception as exc:                       # 网络层
                last = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RagError(f"连接向量服务失败：{last}",
                               hint="检查网络与代理；地址应为 OpenAI 兼容的 /v1 根路径")

            if resp.status_code == 200:
                try:
                    data = resp.json().get("data") or []
                except Exception:
                    raise RagError(f"向量服务返回无法解析：{resp.text[:200]}")
                # 有些兼容实现不保证顺序，按 index 排回去
                if data and isinstance(data[0], dict) and "index" in data[0]:
                    data = sorted(data, key=lambda d: d.get("index", 0))
                return [d.get("embedding") or [] for d in data]

            last = f"HTTP {resp.status_code}：{resp.text[:200]}"
            if resp.status_code in _RETRYABLE and attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            break

        raise RagError(f"向量服务报错：{last}",
                       hint="常见原因：API Key 无效、余额不足、模型名写错")


__all__ = ["SiliconFlowEmbedder"]
