"""「分享页」型平台的公共基类。

快手 / 小红书 / 微信视频号这类平台的共同点：

* 分享出去的都是短链，需要跟随重定向还原
* 无 Cookie 时正文基本拿不到，只能抠 HTML meta
* 官方结构化数据接口都要签名/Cookie

所以把「短链还原 + meta 提取 + yt-dlp 兜底」抽成基类，
新增一个同类平台只需要填几个类属性。
"""

from __future__ import annotations

import re
from typing import Optional

from ..core.errors import FetchError
from ..core.models import Transcript, VideoMeta
from ..core.net import UA_MOBILE
from .base import SUPPORT_METADATA, PlatformAdapter, is_captcha_page


class SharePageAdapter(PlatformAdapter):
    """分享页型平台基类。子类至少提供 ``key`` / ``name`` / ``domains``。"""

    support = SUPPORT_METADATA
    #: 从 URL 中提取视频 ID 的正则（第一个捕获组）
    id_pattern: Optional[re.Pattern] = None

    # ---------------- 识别 ----------------
    @classmethod
    def extract_id(cls, url: str) -> Optional[str]:
        if cls.id_pattern:
            m = cls.id_pattern.search(url or "")
            if m:
                return m.group(1)
        return None

    # ---------------- 解析 ----------------
    def resolve(self, url: str) -> VideoMeta:
        final = url if self.extract_id(url) else self._follow(url)
        vid = self.extract_id(final) or self.extract_id(url) or ""
        info = self._html_meta(final)

        title = info.get("title") or ""
        desc = info.get("description") or ""
        # 很多平台标题形如「xxx - 快手」，去掉尾巴
        title = self._strip_suffix(title)

        # 风控页优先于一切。快手/小红书在未登录或高频访问时
        # 同样会返回验证页，它的 <title> 往往就是「安全验证」四个字 ——
        # 不拦住的话，下面那个 if 会给用户编一个「快手视频（xxx）」的假标题，
        # 界面看着像"解析成功了但内容是空的"，比直接报错更难查。
        if is_captcha_page(title, desc):
            raise FetchError(
                f"{self.name}要求验证后才能访问（未登录 IP 会被风控）",
                hint=f"在「设置 → 平台 Cookie」里粘贴 {self.name} 的 Cookie 后重试；\n"
                     f"        拿法：浏览器登录该站 → F12 → Network → 随便点一个请求 → "
                     f"Request Headers → 复制 cookie 整行的值。\n"
                     f"        临时办法：把视频的标题和简介直接粘进来，用「纯文本总结」。"
            )

        if not title and not desc:
            # 到这里还什么都没有 —— 报「抓到了但没数据」，
            # 而不是编一个假标题。假标题会让用户以为成功解析了，
            # 然后拿着一份基于「快手视频」四个字生成的空摘要。
            raise FetchError(
                f"未能从 {self.name} 页面提取到标题和简介",
                hint=f"可能遇到了平台风控或页面改版。可尝试：\n"
                     f"        1) 在「设置 → 平台 Cookie」里填 {self.name} 的 Cookie；\n"
                     f"        2) 换一个视频链接试试；\n"
                     f"        3) 直接把标题和简介粘进来，用「纯文本总结」。"
            )

        return VideoMeta(
            platform=self.key, platform_name=self.name, video_id=vid,
            url=final, title=title, author=info.get("author", ""),
            description=desc, cover=info.get("image") or None,
            extra={"final_url": final, "via": "html-meta"},
        )

    def fetch_subtitle(self, meta: VideoMeta) -> Optional[Transcript]:
        text = (meta.description or "").strip() or meta.title
        if not text:
            return None
        return Transcript(
            source="metadata",
            text=f"标题：{meta.title}\n简介：{text[:1200]}",
            engine=f"{self.key}-page", language="zh",
            notes=[f"{self.name}未开放字幕接口；如需完整内容请填 Cookie 后重试，"
                   f"或使用「纯文本总结」。"])

    # ---------------- 内部 ----------------
    def _follow(self, url: str) -> str:
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
                    return urljoin(url, loc)
                return self.http.final_url(url)
            finally:
                if old:
                    sess.headers["User-Agent"] = old
        except Exception:
            return url

    def _html_meta(self, url: str) -> dict:
        out: dict = {}
        if not url:
            return out
        try:
            html = self.http.get_text(url, headers={"User-Agent": UA_MOBILE})
        except Exception:
            return out
        out.update(_parse_meta(html))
        # 页面内嵌 JSON（快手 INIT_STATE / 小红书 __INITIAL_STATE__）
        blob = _first_json_blob(html)
        if blob:
            author = _deep_find(blob, "userName") or _deep_find(blob, "nickname") \
                or _deep_find(blob, "user_name") or ""
            if author:
                out["author"] = str(author)[:60]
            cap = _deep_find(blob, "caption") or _deep_find(blob, "desc") \
                or _deep_find(blob, "description") or _deep_find(blob, "title")
            if cap and isinstance(cap, str) and not out.get("description"):
                out["description"] = _clean(cap)[:1500]
            dur = _deep_find(blob, "duration")
            if isinstance(dur, (int, float)) and dur > 0:
                out["duration"] = int(dur / 1000) if dur > 10000 else int(dur)
        return out

    @staticmethod
    def _strip_suffix(title: str) -> str:
        for suf in (" - 快手", "-快手", " - 小红书", "-小红书",
                    " - 抖音", "-抖音", " - 微信", " - 知乎", " - 微博"):
            if title.endswith(suf):
                title = title[: -len(suf)].strip()
        return _clean(title)


# ---------------- 模块级小工具 ----------------
from urllib.parse import urljoin  # noqa: E402  (放在类后避免循环阅读负担)


def _clean(s: str) -> str:
    import html as _h
    s = _h.unescape(s or "")
    return re.sub(r"\s+", " ", s).strip()


def _parse_meta(html: str) -> dict:
    out: dict = {}
    for prop, key in (("og:title", "title"), ("twitter:title", "title"),
                      ("og:description", "description"), ("description", "description"),
                      ("og:image", "image"), ("og:video:title", "title")):
        if key in out and out[key]:
            continue
        for pat in (
            r'(?:property|name)=["\']%s["\'][^>]*content=["\'](.*?)["\']' % re.escape(prop),
            r'content=["\'](.*?)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(prop),
        ):
            m = re.search(pat, html, re.S | re.I)
            if m:
                val = _clean(m.group(1))
                if val:
                    out[key] = val
                break
    if not out.get("title"):
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        if m:
            out["title"] = _clean(m.group(1))
    return out


def _first_json_blob(html: str, limit: int = 400_000) -> dict | None:
    """找页面里第一段像 JSON 的初始化数据（INIT_STATE 等）。"""
    for pat in (r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>",
                r"window\.INIT_STATE\s*=\s*(\{.*?\})\s*</script>",
                r"window\.__INITIAL_SSR_STATE__\s*=\s*(\{.*?\})\s*</script>"):
        m = re.search(pat, html[:limit], re.S)
        if m:
            try:
                import json
                return json.loads(m.group(1))
            except Exception:
                return None
    return None


def _deep_find(obj, key: str, depth: int = 6):
    """在嵌套 dict/list 里按 key 找第一个值（限制深度，防爆炸）。"""
    if depth <= 0 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], (str, int, float)):
            return obj[key]
        for v in list(obj.values())[:40]:
            got = _deep_find(v, key, depth - 1)
            if got is not None:
                return got
    else:
        for v in obj[:20]:
            got = _deep_find(v, key, depth - 1)
            if got is not None:
                return got
    return None
