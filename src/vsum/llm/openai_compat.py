"""OpenAI 兼容的大模型引擎。

DeepSeek / Kimi / 通义千问 / 智谱 / 硅基流动 / OpenAI / Ollama / 自建 one-api
全都走这一套协议，所以只需要一个实现：填对 ``base_url`` + ``model`` + ``key``。

支持流式输出（界面上逐字显示）。
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from ..core.errors import LLMError
from ..core.models import Transcript, VideoMeta
from ..core.registry import register_llm
from .base import LlmEngine, build_prompt


@register_llm("openai-compat")
class OpenAICompatEngine(LlmEngine):
    """OpenAI 兼容接口（默认引擎）。"""

    key = "openai-compat"
    name = "OpenAI 兼容接口"
    note = "DeepSeek / Kimi / 通义 / 智谱 / Ollama 等均适用"

    # ---------------- 可用性 ----------------
    def available(self) -> tuple[bool, str]:
        cfg = self.ctx.config
        if not cfg.get("llm.enabled", True):
            return False, "已在大模型设置里关闭"
        if not cfg.get("llm.base_url", ""):
            return False, "未配置 Base URL"
        if not cfg.get("llm.api_key", ""):
            preset = cfg.get("llm.preset", "")
            if preset != "ollama":      # Ollama 本地不需要 key
                return False, "未配置 API Key"
        if not cfg.get("llm.model", ""):
            return False, "未配置模型名"
        return True, ""

    # ---------------- 摘要 ----------------
    def summarize(self, meta: VideoMeta, transcript: Transcript,
                  on_token: Optional[Callable[[str], None]] = None) -> str:
        ok, reason = self.available()
        if not ok:
            raise LLMError(reason, hint="在「设置 → 大模型」里选择服务商并填写 API Key")

        cfg = self.ctx.config
        base = cfg.get("llm.base_url", "").rstrip("/")
        model = cfg.get("llm.model", "")
        key = cfg.get("llm.api_key", "")
        temperature = float(cfg.get("llm.temperature", 0.3) or 0.3)
        timeout = int(cfg.get("llm.timeout", 180) or 180)
        max_chars = int(cfg.get("llm.max_input_chars", 24000) or 24000)

        system, user = build_prompt(
            meta, transcript,
            style=cfg.get("summary.style", "detailed"),
            with_timeline=bool(cfg.get("summary.with_timeline", True)),
            with_keywords=bool(cfg.get("summary.with_keywords", True)),
            max_chars=max_chars,
        )

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "stream": bool(on_token),
        }
        url = base + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        try:
            if on_token:
                return self._stream(url, headers, payload, timeout, on_token)
            resp = self.ctx.http.session.post(url, headers=headers, json=payload, timeout=timeout)
        except Exception as exc:
            raise LLMError(f"调用大模型失败：{exc}", hint="检查网络、代理与 Base URL 是否可达")

        if resp.status_code != 200:
            raise LLMError(f"大模型返回 {resp.status_code}：{resp.text[:400]}",
                           hint="常见原因：API Key 错误、余额不足、模型名不匹配")
        try:
            data = resp.json()
            return (data["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            raise LLMError(f"大模型返回内容无法解析：{resp.text[:300]}")

    # ---------------- 通用对话 ----------------
    def chat(self, system: str, user: str,
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None) -> str:
        """一次非流式对话。摘要之外的 AI 能力（内容筛选、笔记重写）走这里。"""
        ok, reason = self.available()
        if not ok:
            raise LLMError(reason, hint="在「设置 → 大模型」里选择服务商并填写 API Key")

        cfg = self.ctx.config
        base = cfg.get("llm.base_url", "").rstrip("/")
        model = cfg.get("llm.model", "")
        key = cfg.get("llm.api_key", "")
        # 筛选/打标签这类任务要稳定、可复现，默认温度比摘要更低
        temp = float(cfg.get("llm.temperature", 0.3) or 0.3) if temperature is None else temperature
        timeout = int(cfg.get("llm.timeout", 180) or 180)

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temp,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        try:
            resp = self.ctx.http.session.post(base + "/chat/completions",
                                               headers=headers, json=payload, timeout=timeout)
        except Exception as exc:
            raise LLMError(f"调用大模型失败：{exc}", hint="检查网络、代理与 Base URL 是否可达")
        if resp.status_code != 200:
            raise LLMError(f"大模型返回 {resp.status_code}：{resp.text[:400]}",
                           hint="常见原因：API Key 错误、余额不足、模型名不匹配")
        try:
            return (resp.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            raise LLMError(f"大模型返回内容无法解析：{resp.text[:300]}")


    # ---------------- 流式 ----------------
    def _stream(self, url: str, headers: dict, payload: dict, timeout: int,
                on_token: Callable[[str], None]) -> str:
        try:
            resp = self.ctx.http.session.post(url, headers=headers, json=payload,
                                              timeout=timeout, stream=True)
        except Exception as exc:
            raise LLMError(f"调用大模型失败：{exc}", hint="检查网络、代理与 Base URL 是否可达")
        if resp.status_code != 200:
            raise LLMError(f"大模型返回 {resp.status_code}：{resp.text[:400]}",
                           hint="常见原因：API Key 错误、余额不足、模型名不匹配")
        chunks = []
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                chunks.append(piece)
                try:
                    on_token(piece)
                except Exception:
                    pass
        return "".join(chunks).strip()
