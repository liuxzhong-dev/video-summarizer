"""云端语音识别（OpenAI 兼容的 ``/audio/transcriptions`` 接口）。

不用本地显卡、不用下几百 MB 模型，适合长视频或低配机器。

常用服务：

===========================  ============================================  ==================================
服务商                        Base URL                                      model
===========================  ============================================  ==================================
硅基流动（有免费额度）        https://api.siliconflow.cn/v1                  FunAudioLLM/SenseVoiceSmall
Groq（有免费额度）            https://api.groq.com/openai/v1                 whisper-large-v3-turbo
OpenAI                        https://api.openai.com/v1                      whisper-1 / gpt-4o-transcribe
自建 one-api / new-api        https://your.domain/v1                         视上游而定
===========================  ============================================  ==================================
"""

from __future__ import annotations

import glob
import os
import shutil
import tempfile
from typing import Callable, Optional

from ..core.errors import LLMError
from ..core.models import Segment, Transcript
from ..core.registry import register_asr
from .base import AsrEngine

#: 超过这个时长（秒）就切片上传。单请求上传几十 MB 的长音频，
#: 速度慢时很容易撞上请求超时（实测 35 分钟 / 66MB 的 wav 单次上传 + 识别
#: 要 10 分钟上下，网络稍差就超过 600s 超时，整个识别白跑）。
#: 切成 5 分钟一片后每片只有几 MB，上传秒级、单片失败只重试该片。
_CHUNK_THRESHOLD_SEC = 600
_CHUNK_SEC = 300

CLOUD_ASR_PRESETS = {
    "siliconflow": {
        "label": "硅基流动 SenseVoice（有免费额度）",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "FunAudioLLM/SenseVoiceSmall",
    },
    "groq": {
        "label": "Groq Whisper（有免费额度）",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3-turbo",
    },
    "openai": {
        "label": "OpenAI Whisper",
        "base_url": "https://api.openai.com/v1",
        "model": "whisper-1",
    },
}


@register_asr("cloud")
class CloudAsrEngine(AsrEngine):
    """云端 ASR（OpenAI 兼容协议）。"""

    key = "cloud"
    name = "云端语音识别（OpenAI 兼容）"
    note = "速度快、免本地算力，需要 API Key"
    online = True

    def available(self) -> tuple[bool, str]:
        base = self.ctx.config.get("asr.cloud_base_url", "")
        key = self.ctx.config.get("asr.cloud_api_key", "") or self.ctx.config.get("llm.api_key", "")
        if not base:
            return False, "未配置云端 ASR 的 Base URL"
        if not key:
            return False, "未配置云端 ASR 的 API Key"

        # 防误填：用户常把「密钥管理页地址」当成 Base URL 填进来。
        # 那样请求会命中一个 HTML 页面 —— HTTP 200 但 body 是网页源码，
        # 程序会把 HTML 当识别结果，最后报「未返回识别文本」，
        # 而错误信息完全指不到真正的原因，只能在服务器日志里看到。
        # 这里提前拦下并说人话。
        low = base.rstrip("/").lower()
        if "/me/account" in low or "/console" in low or "/dashboard" in low:
            return False, ("你填的是控制台页面地址，不是 API 地址。"
                           "Base URL 应填 https://api.siliconflow.cn/v1")
        if not low.startswith(("http://", "https://")):
            return False, f"Base URL 要以 http(s):// 开头，当前是：{base[:40]}"
        return True, ""

    def transcribe(self, audio_path: str, language: str = "zh",
                   on_progress: Optional[Callable[[str, float], None]] = None
                   ) -> Transcript:
        ok, reason = self.available()
        if not ok:
            raise LLMError(reason, hint="在「设置 → 语音识别」填写云端 ASR 的 Base URL / Key / 模型")

        cfg = self.ctx.config
        base = cfg.get("asr.cloud_base_url", "").rstrip("/")
        key = cfg.get("asr.cloud_api_key", "") or cfg.get("llm.api_key", "")
        model = cfg.get("asr.cloud_model", "whisper-1") or "whisper-1"

        if not os.path.isfile(audio_path):
            raise LLMError(f"音频文件不存在：{audio_path}")

        # 长音频分片：每片小、上传快、单片失败只重试单片，
        # 还能把每片的相对时间戳平移成全片时间轴（_segments 带 offset）。
        dur = self._probe_seconds(audio_path)
        if dur > _CHUNK_THRESHOLD_SEC:
            return self._transcribe_chunked(
                audio_path, base, key, model, language, dur, on_progress)

        if on_progress:
            on_progress("上传音频到云端 ASR…", 0.3)
        payload = self._post_audio(audio_path, base, key, model, language)

        text = (payload.get("text") or "").strip()
        segs = _parse_segments(payload, 0.0)
        if not text and segs:
            text = "".join(s.text for s in segs)
        if not text:
            raise LLMError("云端 ASR 未返回识别文本",
                           hint="音频里可能没有人声（纯音乐/图文帖）；"
                                "或换一个模型试试、改用本地 Whisper")
        return Transcript(source="asr", text=text, segments=segs,
                          language=language, engine=f"cloud:{model}",
                          notes=[f"云端识别：{model}"])

    # ---------------- 内部 ----------------
    def _post_audio(self, path: str, base: str, key: str,
                    model: str, language: str, timeout: int = 600) -> dict:
        """上传一个音频文件，返回服务器的 JSON 响应；失败抛 LLMError。"""
        url = base + "/audio/transcriptions"
        try:
            with open(path, "rb") as f:
                files = {"file": (os.path.basename(path), f, "audio/wav")}
                data = {"model": model}
                if language and language != "auto":
                    data["language"] = language
                data["response_format"] = "verbose_json"
                resp = self.ctx.http.session.post(
                    url, headers={"Authorization": f"Bearer {key}"},
                    files=files, data=data, timeout=timeout)
        except Exception as exc:
            raise LLMError(f"云端 ASR 请求失败：{exc}")

        if resp.status_code != 200:
            body = resp.text[:300]
            # 硅基流动的模型不存在是最高频的错误（官方没有 whisper-* 系列），
            # 但它的报错只说「Model does not exist」，用户很难自行联想。
            if "does not exist" in body or "20012" in body:
                raise LLMError(f"云端 ASR 不存在模型「{model}」",
                               hint="该服务商可能没有这个模型。"
                                    "硅基流动可用的是 FunAudioLLM/SenseVoiceSmall "
                                    "或 Qwen/Qwen3-ASR-1.7B；"
                                    "OpenAI 用 whisper-1；Groq 用 whisper-large-v3-turbo")
            raise LLMError(f"云端 ASR 返回 {resp.status_code}：{body}",
                           hint="检查 Base URL（应为 .../v1，不是控制台页面）、"
                                "API Key、模型名是否正确")
        try:
            return resp.json()
        except Exception:
            return {"text": resp.text}

    def _probe_seconds(self, audio_path: str) -> float:
        """探测音频时长；失败返回 0（0 会走单请求老路，不会更糟）。"""
        try:
            from ..core.ffmpeg import probe_duration
            return probe_duration(self.ctx.ffmpeg(), audio_path)
        except Exception:
            return 0.0

    def _transcribe_chunked(self, audio_path: str, base: str, key: str,
                            model: str, language: str, dur: float,
                            on_progress: Optional[Callable[[str, float], None]]
                            ) -> Transcript:
        """长音频分片识别。

        为什么不用单请求
        ----------------
        实测（2026-10-10，35 分 54 秒 / 65.8MB wav）：单次上传 + 识别约 10 分钟，
        网络稍差就会撞上超时 —— 而且一旦超时，**已经识别的全部作废**。
        分片后每片 5 分钟 / 约 10MB，上传以秒计；某片失败只需重试那一片。
        """
        import subprocess

        ffmpeg = self.ctx.ffmpeg()
        chunk_dir = tempfile.mkdtemp(prefix="vsum_asr_")
        chunks: list[str] = []
        try:
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", audio_path,
                "-ac", "1", "-ar", "16000",
                "-f", "segment", "-segment_time", str(_CHUNK_SEC),
                "-c:a", "pcm_s16le",
                os.path.join(chunk_dir, "ch_%04d.wav"),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=600)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "ignore")[-300:]
                raise LLMError(f"长音频切片失败：{err}",
                               hint="已改回整段上传重试")
            chunks = sorted(glob.glob(os.path.join(chunk_dir, "ch_*.wav")))
            if not chunks:
                raise LLMError("长音频切片后没有产出文件")

            texts: list[str] = []
            segs: list[Segment] = []
            n = len(chunks)
            for i, ch in enumerate(chunks, 1):
                offset = (i - 1) * _CHUNK_SEC
                label = f"识别第 {i}/{n} 段（每段 {_CHUNK_SEC // 60} 分钟）…"
                if on_progress:
                    on_progress(label, i / n)
                payload = None
                last: Optional[Exception] = None
                for attempt in (1, 2):        # 单片失败重试一次
                    try:
                        payload = self._post_audio(ch, base, key, model,
                                                   language, timeout=600)
                        break
                    except LLMError as exc:
                        last = exc
                        if attempt == 1:
                            import time
                            time.sleep(2)
                if payload is None:
                    raise LLMError(
                        f"第 {i}/{n} 段识别失败：{last}",
                        hint="分段识别中某一段连续两次失败。"
                             "可关闭程序后重新发起（音频已保留，无需重新下载视频）。")
                t = (payload.get("text") or "").strip()
                if t:
                    texts.append(t)
                segs.extend(_parse_segments(payload, float(offset)))

            text = "".join(texts).strip()
            if not text and segs:
                text = "".join(s.text for s in segs)
            if not text:
                raise LLMError("云端 ASR 未返回识别文本（音频里可能没有人声）",
                               hint="纯音乐/图文帖没有语音可识别；"
                                    "或换一个模型、改用本地 Whisper")
            notes = [f"云端识别：{model}（长音频已分 {n} 段识别）"]
            return Transcript(source="asr", text=text, segments=segs,
                              language=language, engine=f"cloud:{model}",
                              notes=notes)
        finally:
            shutil.rmtree(chunk_dir, ignore_errors=True)


def _parse_segments(payload: dict, offset: float) -> list:
    """把一帧响应里的 segments 抠出来，时间戳整体平移 ``offset`` 秒。

    分片识别时每片的 segments 时间是**片内相对时间**，加上片的起点
    才是整条音频里的真实位置 —— 这样即使走云端 ASR，srt 导出也有
    可用的时间轴（片边界精度 = 切片时长）。
    """
    out = []
    for s in (payload.get("segments") or []):
        try:
            out.append(Segment(float(s.get("start", 0) or 0) + offset,
                               float(s.get("end", 0) or 0) + offset,
                               (s.get("text") or "").strip()))
        except Exception:
            continue
    return [s for s in out if s.text]
