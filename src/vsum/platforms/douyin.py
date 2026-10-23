"""抖音适配器。

抖音是风控最严的一家，因此这里做了**三级降级**：

1. 分享短链还原 + 页面 JSON 提取（``_ROUTER_DATA`` / ``RENDER_DATA``）
2. yt-dlp 解析（能拿到标题、作者、时长、直链，通常够用）
3. 只从分享文案/HTML meta 里抠标题，输出「元信息摘要」

填上 Cookie 后 1、2 的成功率会显著提升。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Optional

from ..core.context import Context
from ..core.errors import FetchError
from ..core.models import Transcript, VideoMeta
from ..core.net import UA_MOBILE
from ..core.registry import register_platform
from .base import SUPPORT_PARTIAL, PlatformAdapter, is_captcha_page

_VID_RE = re.compile(r"(?:/video/|/share/video/|modal_id=|item_ids=|aweme_id=)(\d{6,25})")
_RAW_ID = re.compile(r"\b(\d{17,20})\b")


@register_platform("douyin")
class DouyinAdapter(PlatformAdapter):
    """抖音（含抖音极速版 / 火山）。"""

    key = "douyin"
    name = "抖音"
    domains = ("douyin.com", "iesdouyin.com", "snssdk1128", "amemv.com")
    support = SUPPORT_PARTIAL
    note = "无 Cookie 时通常只能拿到标题/简介；填 Cookie 后可下载音频做语音识别。"

    #: Cookie 里这个字段的值若是 verify_ 开头，说明这台设备**还没通过人机验证**。
    #: 实测（2026-10-06）：把 verify_ 前缀去掉、整个删掉该字段、或只留 4 个关键
    #: 字段，yt-dlp **全都还是失败** —— 说明 verify_ 只是伴随现象，
    #: 真正的拦路虎是 IP 段（抖音对家庭宽带/机房 IP 的风控等级不同）。
    #: 所以提示里不能说「去掉 verify_ 就能用」，那是骗人。
    _VERIFY_FLAG = "s_v_web_id=verify_"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.cookie = ctx.cookie("douyin")

    @property
    def cookie_is_pending_verify(self) -> bool:
        """已填 Cookie 但它本身是「待验证」状态。"""
        return bool(self.cookie) and self._VERIFY_FLAG in self.cookie

    def _verify_pending_error(self) -> FetchError:
        return FetchError(
            "抖音：Cookie 已填，但它仍是「待验证」状态",
            hint="你复制到的 Cookie 里 s_v_web_id 以 verify_ 开头，"
                 "这是抖音发给未验证客户端的待验证凭证。\n"
                 "        【实测结论】只靠改 Cookie 解决不了 —— 实测去掉 verify_ 前缀、"
                 "删掉该字段、只留 4 个关键字段，yt-dlp 全都仍然失败。\n"
                 "        所以真正的原因是 **IP/网络环境被风控**（家庭宽带、机房 IP "
                 "的风控等级与手机流量不同）。\n"
                 "        【可试】① 用手机热点（换 IP）；② 在设置里配 HTTP 代理；"
                 "③ 同一内容找 B站/YouTube 版本（B 站实测能直接拿官方字幕）。\n"
                 "        【临时】把标题简介粘进输入框走「纯文本总结」。"
        )

    # ---------------- 识别 ----------------
    @classmethod
    def extract_id(cls, url: str) -> Optional[str]:
        m = _VID_RE.search(url or "")
        if m:
            return m.group(1)
        m = _RAW_ID.search(url or "")
        return m.group(1) if m else None

    # ---------------- 解析 ----------------
    def resolve(self, url: str) -> VideoMeta:
        # 【重要】这里**不能**因为「Cookie 是待验证状态」就硬拦。
        # 实测（2026-10-07）：Playwright 启动真实浏览器后完全能抓到视频流，
        # 跟 Cookie 是不是 verify_ 状态无关 —— 浏览器会自己生成有效凭证。
        # 所以只记一条日志继续往下走，让流水线有机会走浏览器兜底。
        # 拦在这里等于「有解法却不让试」。
        if self.cookie_is_pending_verify:
            self.log.info("Cookie 处于待验证状态，将尝试浏览器兜底"
                          "（真实浏览器能绕过这个限制）")

        # 短链必须还原，否则拿不到 video_id
        final = url
        if self.extract_id(url) is None:
            final = self._follow(url)
        video_id = self.extract_id(final) or self.extract_id(url) or ""
        canon = f"https://www.douyin.com/video/{video_id}" if video_id else final

        title = author = desc = cover = ""
        duration = None

        # 1) 页面 JSON
        payload = self._page_payload(final)
        if payload:
            title = self.clean_text(payload.get("desc") or payload.get("title") or "")
            author = self.clean_text(
                ((payload.get("author") or {}).get("nickname")) or payload.get("nickname") or "")
            cover = (payload.get("cover") or payload.get("poster") or "")
            if isinstance(cover, dict):
                cover = (cover.get("url_list") or [""])[0]
            dur = payload.get("duration")
            if isinstance(dur, (int, float)):
                duration = int(dur / 1000) if dur > 1000 else int(dur)

        # 2) HTML meta 兜底
        if not title:
            meta = self._html_meta(final)
            title = meta.get("title", "")
            desc = meta.get("description", "")
        # 3) 反风控兜底。
        #    抖音未登录访问会返回「验证码中间页」，标题不是空的，
        #    所以上面那个 `if not title` 拦不住它 —— 结果是拿一句
        #    「验证码中间页」当视频标题去写摘要，界面看着像"解析不出来"。
        #    这里必须单独识别。
        #    关键：HTTP 路线走到这里说明拿不到标题，但**不代表整个流程失败** ——
        #    浏览器兜底只需要一个能打开的 URL，标题可以后面补。
        #    所以开着浏览器兜底时返回占位元信息让流水线继续，
        #    而不是抛错把整条路堵死。
        browser_ok = bool(self.ctx.config.get("asr.browser_fallback", True))
        if is_captcha_page(title, desc):
            if browser_ok:
                self.log.info("页面是验证中间页，将由浏览器兜底接管")
                return self._placeholder_meta(canon, video_id)
            raise FetchError(
                "抖音要求验证后才能访问（未登录 IP 会被风控）",
                hint="抖音风控是平台级的，不是程序问题。\n"
                     "        【根治】在「设置 → 平台 Cookie」填 douyin.com 的 Cookie。\n"
                     "        拿法：浏览器登录抖音 → F12 → Network → 刷新 → "
                     "点任意请求 → Request Headers → 复制 cookie 整行的值。\n"
                     "        注意：Cookie ≈ 登录凭证，别转发给别人；有效期几周，过期后\n"
                     "        再遇同样报错就重新复制一次。\n"
                     "        【临时】视频标题和简介已拿到的话，直接粘进输入框走「纯文本总结」，\n"
                     "        或者只要视频本身的要点，打开抖音手动记两条要点更快。"
            )

        if not title:
            if browser_ok:
                self.log.info("未取到标题，将由浏览器兜底接管")
                return self._placeholder_meta(canon, video_id)
            raise FetchError(
                "未能解析抖音视频信息（大概率触发了风控验证）",
                hint="在「设置 → 平台 Cookie」填 douyin.com 的 Cookie 后重试；"
                     "或把视频标题粘进输入框用「纯文本总结」功能")

        return VideoMeta(
            platform=self.key, platform_name=self.name, video_id=video_id,
            url=canon, title=title or "抖音视频", author=author,
            description=desc or title, duration=duration, cover=cover or None,
            extra={"final_url": final, "has_cookie": bool(self.cookie)},
        )

    def fetch_subtitle(self, meta: VideoMeta) -> Optional[Transcript]:
        """抖音暂无公开字幕接口。有标题/简介时凑一个元信息转写，交给流水线判断。"""
        text = meta.description or meta.title
        if not text:
            return None
        return Transcript(source="metadata", text=f"标题：{meta.title}\n简介：{text[:1200]}",
                          engine="douyin-page", language="zh",
                          notes=["抖音未开放字幕接口，当前内容来自页面标题/简介。"])

    # ---------------- 内部 ----------------
    @staticmethod
    def _placeholder_meta(canon: str, video_id: str) -> VideoMeta:
        """浏览器兜底用的占位元信息。

        标题先占位，摘要阶段会用音频转写内容补上真正的要点；
        总比在这里抛错、整条流程走不下去好。
        """
        return VideoMeta(
            platform="douyin", platform_name="抖音（浏览器兜底）",
            video_id=video_id, url=canon,
            title="抖音视频（标题待浏览器补全）",
            author="", description="", duration=None, cover=None,
            extra={"needs_browser": True},
        )

    def _follow(self, url: str) -> str:
        """跟随短链重定向，移动端 UA 通过率更高。"""
        try:
            sess = self.http.session
            old = sess.headers.get("User-Agent")
            sess.headers["User-Agent"] = UA_MOBILE
            try:
                resp = sess.get(url, timeout=self.http.timeout,
                                allow_redirects=False, stream=True)
                loc = resp.headers.get("Location")
                resp.close()
                if loc:
                    return urllib.parse.urljoin(url, loc)
                return self.http.final_url(url)
            finally:
                if old:
                    sess.headers["User-Agent"] = old
        except Exception:
            return url

    def _page_payload(self, url: str) -> dict:
        """从页面里的 _ROUTER_DATA / RENDER_DATA 抠出视频对象。"""
        if not url:
            return {}
        try:
            html = self.http.get_text(url, headers={"User-Agent": UA_MOBILE})
        except Exception:
            return {}

        # 抖音 SSR 数据（URL 编码的 JSON）
        m = re.search(r'<script id="RENDER_DATA"[^>]*>(.*?)</script>', html, re.S)
        if m:
            try:
                data = json.loads(urllib.parse.unquote(m.group(1)))
                for key, val in (data or {}).items():
                    if isinstance(val, dict) and "videoInfoRes" in val:
                        items = (val["videoInfoRes"] or {}).get("item_list") or []
                        if items:
                            return items[0]
                    if isinstance(val, dict) and "aweme" in val:
                        detail = (val["aweme"] or {}).get("detail")
                        if detail:
                            return detail
            except Exception:
                pass

        m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})</script>", html, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
                items = ((((data.get("loaderData") or {})
                           .get("video_(id)/(id)") or {})
                          .get("videoInfoRes") or {}).get("item_list") or [])
                if items:
                    return items[0]
            except Exception:
                pass
        return {}

    def _html_meta(self, url: str) -> dict:
        """抠 og: / twitter: 等 meta 标签。"""
        out = {}
        try:
            html = self.http.get_text(url, headers={"User-Agent": UA_MOBILE})
        except Exception:
            return out
        for prop in ("og:title", "twitter:title", "og:description", "description"):
            m = re.search(
                r'(?:property|name)=["\']%s["\']\s+content=["\'](.*?)["\']' % re.escape(prop),
                html, re.S | re.I)
            if not m:
                m = re.search(
                    r'content=["\'](.*?)["\']\s+(?:property|name)=["\']%s["\']' % re.escape(prop),
                    html, re.S | re.I)
            if m:
                val = self.clean_text(_unescape(m.group(1)))
                if val:
                    out["title" if "title" in prop else "description"] = val
        if not out.get("title"):
            m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
            if m:
                out["title"] = self.clean_text(_unescape(m.group(1)))
        return out


def _unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s or "")
