"""本地 faster-whisper 转写引擎（离线可用，推荐配置）。

模型在**首次使用时自动下载**并缓存到 ``%LOCALAPPDATA%/VideoSummarizer/models``，
之后离线也能用。国内下载默认走 hf-mirror.com 镜像。

===============  =========  ==========  ==========================
模型             体积       显存/内存    说明
===============  =========  ==========  ==========================
tiny             ~75 MB     ~1 GB       最快，准确率一般
base             ~140 MB    ~1 GB       短视频可用
**small**        ~460 MB    ~2 GB       默认，中文效果与速度平衡
medium           ~1.5 GB    ~5 GB       准确，慢
large-v3         ~3 GB      ~10 GB      最好，需要独显才现实
===============  =========  ==========  ==========================
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from ..core.context import Context
from ..core.errors import DependencyMissing
from ..core.models import Transcript
from ..core.registry import register_asr
from .base import AsrEngine

_model_cache: dict = {}
_cache_lock = threading.Lock()

# 国内直连 huggingface.co 基本不通，默认走镜像；
# 且镜像不支持 xet 存储协议，必须禁用，否则下载会 401。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

#: 模型默认下载目录（也可被 HF_HOME 覆盖）
def default_model_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "VideoSummarizer", "models")


@register_asr("faster-whisper")
class FasterWhisperEngine(AsrEngine):
    """本地 faster-whisper（CTranslate2）。"""

    key = "faster-whisper"
    name = "本地 Whisper（faster-whisper）"
    note = "离线可用，首次使用需下载模型（几百 MB）"
    online = False

    def available(self) -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
            import ctranslate2     # noqa: F401
        except Exception as exc:
            return False, (
                f"未安装 faster-whisper：{exc}。"
                "在「设置 → 语音识别」点「安装本地模型组件」，"
                "或手动执行 pip install faster-whisper")
        return True, ""

    def transcribe(self, audio_path: str, language: str = "zh",
                   on_progress: Optional[Callable[[str, float], None]] = None
                   ) -> Transcript:
        ok, reason = self.available()
        if not ok:
            raise DependencyMissing(reason, hint="安装后无需联网即可使用")

        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # pragma: no cover
            raise DependencyMissing(f"faster-whisper 导入失败：{exc}")

        cfg = self.ctx.config
        size = cfg.get("asr.model_size", "small")
        device = cfg.get("asr.device", "auto")
        compute = cfg.get("asr.compute_type", "int8")
        if device == "auto":
            device = "cuda" if _has_cuda() else "cpu"
            if device == "cpu" and compute == "float16":
                compute = "int8"

        model = self._load(size, device, compute)
        self.ctx.report(f"正在转写音频（{size} / {device}），长视频需要几分钟…", 55)
        if on_progress:
            on_progress("模型加载完成，开始转写", 0.05)

        # 直接把 PCM 数组喂进去：绕开 PyAV 解码（不同 av 版本接口差异很大，
        # 而我们的音频本来就是自己用 ffmpeg 抽的 16k 单声道，格式确定）
        audio = load_pcm(audio_path)

        kw = dict(
            language=(language or None) if language != "auto" else None,
            beam_size=5,
            condition_on_previous_text=False,   # 防止模型在长音频里复读上一句
        )
        try:
            # VAD 能显著减少音乐/静音段的胡言乱语，但依赖 silero_vad.onnx，
            # 打包环境万一漏了这个资源文件，就退回不带 VAD 再跑一次。
            segments, info = model.transcribe(
                audio, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500}, **kw)
        except Exception as exc:
            self.log.warning("带 VAD 的转写失败，改用无 VAD 模式：%s", exc)
            segments, info = model.transcribe(audio, vad_filter=False, **kw)

        collected = []
        last = 0.0
        for seg in segments:
            collected.append(seg)
            if on_progress and info.duration:
                cur = min(1.0, (seg.end or 0) / max(1e-6, info.duration))
                if cur - last > 0.02:
                    last = cur
                    on_progress(f"转写中 {int(cur * 100)}%", cur)

        tr = self._segments_to_transcript(
            collected, engine=f"faster-whisper/{size}",
            language=getattr(info, "language", language) or language)
        tr.notes.append(f"识别引擎：faster-whisper {size}，音频时长约 "
                        f"{int(getattr(info, 'duration', 0) or 0)} 秒")
        return tr

    # ---------------- 内部 ----------------
    def _load(self, size: str, device: str, compute: str):
        ck = (size, device, compute)
        with _cache_lock:
            if ck in _model_cache:
                return _model_cache[ck]
        from faster_whisper import WhisperModel

        endpoint = self.ctx.config.get("asr.hf_endpoint", "") or "https://hf-mirror.com"
        os.environ["HF_ENDPOINT"] = endpoint
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        download_root = os.environ.get("VSUM_MODEL_DIR") \
            or self.ctx.config.get("asr.model_dir", "") or default_model_dir()
        os.makedirs(download_root, exist_ok=True)

        # 手动下载到本地再加载：这样能断点续传、可重试，也不会卡在意外的 xet 协议上
        self.ctx.report(f"首次使用需要下载 Whisper 模型（{size}），请耐心等待…", 50)
        self.ctx.report(f"下载源：{endpoint}（慢或失败可在设置里改镜像）", 50)
        model_path = self._download_model(size, download_root)

        model = WhisperModel(model_path, device=device, compute_type=compute)
        with _cache_lock:
            _model_cache[ck] = model
        return model

    def _download_model(self, size: str, root: str) -> str:
        """下载 CTranslate2 版 Whisper 模型到本地，返回可直接加载的目录。

        用 ``local_dir`` 而不是默认的 cache+symlink：Windows 没开开发者模式时
        创建符号链接会失败，缓存目录里会留下一堆 0 字节的「快照」，
        加载时表现就是「model.bin is incomplete」。
        """
        import shutil
        from huggingface_hub import snapshot_download

        repo = f"Systran/faster-whisper-{size}"
        local = os.path.join(root, f"faster-whisper-{size}")

        # 清掉上次中断留下的残骸（0 字节的 model.bin 会让后续加载直接报错）
        bin_path = os.path.join(local, "model.bin")
        if os.path.isfile(bin_path) and os.path.getsize(bin_path) < 1024 * 1024:
            self.log.warning("发现不完整的模型文件，删除后重新下载")
            shutil.rmtree(local, ignore_errors=True)

        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                return snapshot_download(
                    repo, local_dir=local, max_workers=2,
                    allow_patterns=["*.bin", "*.txt", "*.json"],
                )
            except Exception as exc:
                last = exc
                self.log.warning("模型下载第 %d 次失败：%s", attempt + 1, exc)
                time.sleep(2)
        raise DependencyMissing(
            f"Whisper 模型下载失败：{last}",
            hint="可尝试：① 换网络/代理重试；② 在设置里把镜像改成 https://huggingface.co；"
                 "③ 手动下载模型后把目录填到「模型目录」。")


def load_pcm(path: str, sr: int = 16000):
    """把本地音频读成 float32 单声道数组，直接喂给 Whisper。

    我们的音频都是自己用 ffmpeg 抽的 16k/单声道/wav，格式确定，
    所以这里自己解码比依赖 PyAV 更稳（PyAV 各版本接口差异极大，
    且在 Windows + Python 3.13 上经常装不上）。

    读不了就返回原路径，交给 faster-whisper 自己解码。
    """
    try:
        import numpy as np
        if path.lower().endswith((".raw", ".pcm", ".f32")):
            return np.fromfile(path, dtype=np.float32)
        import wave
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                return path
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return path


def _has_cuda() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False
