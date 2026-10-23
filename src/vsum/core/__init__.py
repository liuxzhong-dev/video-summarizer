"""核心层：配置、上下文、流水线、数据模型。"""

from .config import Config, LLM_PRESETS
from .context import Context
from .errors import VSumError
from .models import Segment, SummaryResult, Transcript, VideoMeta
from .pipeline import Pipeline

__all__ = [
    "Config", "LLM_PRESETS", "Context", "VSumError",
    "Segment", "SummaryResult", "Transcript", "VideoMeta", "Pipeline",
]
