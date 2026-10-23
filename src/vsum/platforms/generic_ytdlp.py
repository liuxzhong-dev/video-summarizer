"""通用适配器 —— 兜底 + 白送一大堆平台。

凡是 yt-dlp 支持的站点（YouTube、西瓜视频、微博视频、知乎视频、Twitter/X、
腾讯视频、优酷、爱奇艺、Vimeo、TikTok 等上千个），走这个适配器都能用，
包括**自动字幕**。它同时是其他平台拿不到字幕/直链时的兜底路径。

国内站点访问 YouTube 等需要代理，在「设置 → 媒体 → 代理」里填即可。
"""

from __future__ import annotations

import re
from typing import Optional

from ..core.errors import FetchError
from ..core.models import Segment, Transcript, VideoMeta
from ..core.registry import register_platform
from .base import SUPPORT_PARTIAL, PlatformAdapter


@register_platform("generic")
class GenericYtdlpAdapter(PlatformAdapter):
    """通用（yt-dlp 兜底）。"""

    key = "generic"
    name = "通用链接（yt-dlp）"
    domains = ()          # 兜底：不匹配任何特定域名
    support = SUPPORT_PARTIAL
    note = "通过 yt-dlp 解析，支持上千个站点；有字幕时优先用字幕。"

    # ---------------- 识别 ----------------
    @classmethod
    def match(cls, url: str) -> bool:
        return bool(re.match(r"^https?://", (url or "").strip(), re.I))

    # ---------------- 解析 ----------------
    def resolve(self, url: str) -> VideoMeta:
        from ..media.ytdlp import explain_failure, extract_info, last_error

        info = extract_info(url, self.ctx)
        if not info:
            # **必须把 yt-dlp 的原始报错拼进来**。它上面那句
            # 「Fresh cookies (not necessarily logged in) are needed」
            # 直接告诉了用户该去配 Cookie；只报「无法解析该链接」
            # 等于把诊断线索全销毁，用户只能自己瞎猜。
            raw = last_error(url)
            hint = explain_failure(url)
            raise FetchError(
                "无法解析该链接" + (f"：{raw}" if raw else ""),
                hint=hint or "确认链接完整、可公开访问；海外站点需配置代理",
            )
        return VideoMeta(
            platform=info.get("extractor_key") or self.key,
            platform_name=info.get("extractor") or self.name,
            video_id=str(info.get("id") or ""),
            url=info.get("webpage_url") or url,
            title=self.clean_text(info.get("title") or ""),
            author=self.clean_text(info.get("uploader") or info.get("channel") or ""),
            description=self.clean_text((info.get("description") or "")[:2000]),
            duration=int(info.get("duration") or 0) or None,
            cover=info.get("thumbnail"),
            published=self.clean_text(info.get("upload_date") or ""),
            extra={"extractor": info.get("extractor_key")},
        )

    def fetch_subtitle(self, meta: VideoMeta) -> Optional[Transcript]:
        """从 yt-dlp 拿到的字幕轨里挑中文。"""
        from ..media.ytdlp import extract_info

        info = extract_info(meta.url, self.ctx)
        if not info:
            return None
        subs = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        # 中文优先：zh-Hans / zh-CN / zh / 其次任意
        for table, tag in ((subs, "official"), (auto, "auto")):
            if not table:
                continue
            keys = sorted(table.keys(),
                          key=lambda k: (0 if re.search(r"zh(-|_)?(hans|cn|hant)?", k, re.I) else 1, k))
            for k in keys:
                tracks = table.get(k) or []
                url = None
                for t in tracks:
                    if (t.get("ext") or "") in ("json3", "vtt", "srv3", "srt", "ass"):
                        url = t.get("url")
                        break
                if not url and tracks:
                    url = tracks[-1].get("url")
                if not url:
                    continue
                segs = self._download_subtitle(url, k)
                if segs:
                    return Transcript(
                        source="subtitle",
                        text="\n".join(s.text for s in segs),
                        segments=segs, language=k,
                        engine=f"ytdlp-{tag}",
                    )
        return None

    # ---------------- 内部 ----------------
    def _download_subtitle(self, url: str, lang: str) -> list:
        import json as _json
        import re as _re

        try:
            raw = self.http.get_text(url)
        except Exception:
            return []
        segs: list[Segment] = []
        # vtt / srt
        if ".vtt" in url or raw.lstrip().startswith("WEBVTT") or _re.search(r"\d{2}:\d{2}:\d{2}", raw[:400]):
            blocks = _re.split(r"\n\s*\n", raw)
            for b in blocks:
                m = _re.search(
                    r"(\d{2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{1,3})", b)
                if not m:
                    continue
                lines = [l for l in b.splitlines()[1:] if l.strip()]
                text = " ".join(lines).strip()
                text = _re.sub(r"<[^>]+>", "", text)
                if text:
                    segs.append(Segment(_t2s(m.group(1)), _t2s(m.group(2)), self.clean_text(text)))
            return segs
        # json3
        try:
            data = _json.loads(raw)
            for ev in data.get("events") or []:
                txt = "".join((s.get("utf8") or "") for s in (ev.get("segs") or []))
                txt = self.clean_text(txt.replace("\n", " "))
                if txt:
                    start = (ev.get("tStartMs") or 0) / 1000.0
                    dur = (ev.get("dDurationMs") or 0) / 1000.0
                    segs.append(Segment(start, start + dur, txt))
        except Exception:
            return []
        return segs


def _t2s(t: str) -> float:
    h, m, rest = t.split(":")
    s, ms = rest.split(",") if "," in rest else rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / (10 ** len(ms))
