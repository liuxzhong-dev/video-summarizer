"""平台适配器基类 —— 新增平台时继承它。

最小可用实现只需要 ``key`` / ``name`` / ``domains`` 和 ``resolve()``；
``fetch_subtitle()`` 等可选方法留空时，流水线会自动走通用降级路径
（yt-dlp 抓字幕 → 下载音频做 ASR → 只用元信息摘要）。
"""

from __future__ import annotations

import re
from typing import Optional

from ..core.context import Context
from ..core.models import Transcript, VideoMeta

#: 支持等级：界面上会直接展示给用户，避免「点了没反应」的困惑
SUPPORT_FULL = "full"          # 元数据 + 字幕 + 音频，全自动
SUPPORT_PARTIAL = "partial"    # 能拿元数据和音频，字幕看运气
SUPPORT_METADATA = "metadata"  # 只能拿标题/简介，需要 Cookie 才能更进一步

#: 平台风控时会返回这些页面。它们的 <title> **不是空的**，
#: 所以「if not title」这种判空拦不住 —— 结果是拿「验证码中间页」当视频标题，
#: 界面看着像"解析不出来"，日志里也看不出哪里不对。
#: 各家适配器的 resolve() 收尾时都应该过一下这个函数。
_CAPTCHA_MARKERS = (
    "验证码", "安全验证", "captcha", "verify", "滑动验证",
    "请完成验证", "访问异常", "unusual traffic", "robot check",
    "just a moment", "checking your browser", "访问过于频繁",
)


def is_captcha_page(*texts: str) -> bool:
    """判断抓到的是不是风控页而不是视频页。

    抖音/快手/小红书在未登录或高频访问时都会返回验证页。
    特征是**标题有内容但内容是验证提示** —— 所以不能靠判空发现。

    用法::

        if is_captcha_page(title, desc):
            raise FetchError("平台要求验证", hint="请在设置里填 Cookie")
    """
    for raw in texts:
        t = (raw or "").strip().lower()
        if not t:
            continue
        if any(m in t for m in _CAPTCHA_MARKERS):
            return True
    return False


class PlatformAdapter:
    """所有平台适配器的父类。"""

    #: 注册表 key，全局唯一
    key: str = ""
    #: 中文显示名
    name: str = ""
    #: 域名片段，命中任意一个即视为本平台
    domains: tuple = ()
    #: 额外正则（匹配 URL 全串即可）
    patterns: tuple = ()
    #: 支持等级
    support: str = SUPPORT_METADATA
    #: 给用户的补充说明（是否需要 Cookie 等）
    note: str = ""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.log = ctx.log
        self.http = ctx.http

    # ---------------- 识别 ----------------
    @classmethod
    def match(cls, url: str) -> bool:
        u = (url or "").lower()
        if not u:
            return False
        for d in cls.domains:
            if d in u:
                return True
        for p in cls.patterns:
            if re.search(p, url, re.I):
                return True
        return False

    @classmethod
    def extract_id(cls, url: str) -> Optional[str]:   # pragma: no cover - 子类实现
        return None

    # ---------------- 解析 ----------------
    def normalize_url(self, url: str) -> str:
        """把分享短链还原成长链（默认：跟随一次重定向）。"""
        try:
            final = self.http.final_url(url)
            return final or url
        except Exception:
            return url

    def resolve(self, url: str) -> VideoMeta:
        """解析出视频元数据。子类必须实现。"""
        raise NotImplementedError

    # ---------------- 可选能力 ----------------
    def fetch_subtitle(self, meta: VideoMeta) -> Optional[Transcript]:
        """返回官方/AI 字幕。平台无字幕时返回 None，流水线会降级到 ASR。"""
        return None

    def fetch_audio_source(self, meta: VideoMeta) -> Optional[str]:
        """返回可直接喂给 ffmpeg 的音频/视频直链。返回 None 则走 yt-dlp。"""
        return None

    def supplement_text(self, meta: VideoMeta) -> str:
        """补充语料：弹幕、热门评论等。B 站这类数据很有价值。"""
        return ""

    # ---------------- 工具 ----------------
    @staticmethod
    def clean_text(text: str) -> str:
        """去掉多余空白和控制字符。"""
        if not text:
            return ""
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        return re.sub(r"[ \t]+", " ", text).strip()

    def meta_only_transcript(self, meta: VideoMeta, reason: str = "") -> Transcript:
        """构造「只有元信息」的兜底转写结果。"""
        parts = [f"标题：{meta.title}"]
        if meta.author:
            parts.append(f"作者：{meta.author}")
        if meta.description:
            parts.append(f"简介：{meta.description[:1500]}")
        extra = self.supplement_text(meta)
        if extra:
            parts.append("补充信息：" + extra[:3000])
        notes = ["未获取到视频字幕或语音内容，以下结论仅基于标题/简介等元信息推断。"]
        if reason:
            notes.append(reason)
        return Transcript(source="metadata", text="\n".join(parts),
                          engine=self.key, notes=notes)
