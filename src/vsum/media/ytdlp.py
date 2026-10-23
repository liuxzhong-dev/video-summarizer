"""yt-dlp 封装：信息提取 / 音频直链 / 字幕轨。

yt-dlp 是整个项目的「万能后手」——任何平台只要它有 extractor 就能被解析。
这里统一了代理、Cookie、格式选择策略，并对结果做了一层缓存。
"""

from __future__ import annotations

import os
import re
import threading
from typing import Optional

from ..core.context import Context
from ..core.errors import MediaError
from ..core.ffmpeg import extract_audio

_INFO_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

#: 最近一次 extract_info 失败的原始原因。
#:
#: **必须留着。** yt-dlp 的报错往往精确到能直接照着做 ——
#: 抖音的「Fresh cookies (not necessarily logged in) are needed」
#: 明确告诉用户「去配 Cookie」，而如果只返回 None，
#: 这层信息就彻底丢了，上层只能笼统地报「无法解析该链接」，
#: 用户完全不知道自己该干什么。URL 一起存，因为不同链接原因不同。
_LAST_ERROR: dict = {}


def last_error(url: str = "") -> str:
    """取 yt-dlp 最近一次的报错原文（没有则返回空串）。

    ``url`` 为空时返回最近一次；传了 url 则只在该 url 没有记录时返回空串。
    """
    if not url:
        return _LAST_ERROR.get("error", "")
    return _LAST_ERROR.get("error", "") if _LAST_ERROR.get("url") == url else ""


def _clear_error(url: str = "") -> None:
    if not url or _LAST_ERROR.get("url") == url:
        _LAST_ERROR.clear()


def _base_opts(ctx: Context) -> dict:
    cfg = ctx.config
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "skip_download": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    proxy = cfg.get("media.proxy", "") or os.environ.get("HTTPS_PROXY") or ""
    if proxy:
        opts["proxy"] = proxy
    return opts


def extract_info(url: str, ctx: Context, use_cache: bool = True) -> Optional[dict]:
    """提取视频信息（不下载）。失败返回 None，不抛异常。

    失败原因记在 :func:`last_error` 里供上层取用 —— 静默返回 None
    会让 yt-dlp 那句精准的提示（比如「需要 Cookie」）消失得无影无踪。
    """
    if not url:
        return None
    with _CACHE_LOCK:
        if use_cache and url in _INFO_CACHE:
            return _INFO_CACHE[url]
    _clear_error(url)
    try:
        import yt_dlp
        opts = _base_opts(ctx)
        opts["format"] = "bestaudio/best"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None
        if info:
            with _CACHE_LOCK:
                _INFO_CACHE[url] = info
        return info
    except Exception as exc:
        # yt-dlp 的 DownloadError 把真正的原因包在 .exc_info 里，
        # str(exc) 只会得到一句干巴巴的 "ERROR: ..."，要往下再扒一层。
        reason = _reason_of(exc)
        # 「Unable to handle request: N unexpected error(s)」是最没用的一句报错 ——
        # 它只说「好几个网络处理器都挂了」，**不说是 DNS、证书还是代理挂的**。
        # 用户（和排查的人）拿到这句话完全无从下手，实测就有人拿着它来问
        # 「换了台电脑、改了个设置就变成这样」。这里补一次最小网络自检，
        # 把真实卡点问出来。
        low = reason.lower()
        if "unable to handle request" in low or "unexpected error" in low:
            sc = _net_selfcheck(url, ctx.config.get("media.proxy", "") or "")
            if sc:
                reason = f"{reason} ｜ 网络自检：{sc}"
        _LAST_ERROR["url"] = url
        _LAST_ERROR["error"] = reason
        ctx.log.warning("yt-dlp 解析失败 %s: %s", url, reason)
        return None


def _reason_of(exc: Exception) -> str:
    """把 yt-dlp 异常压成一句人能看懂的话。

    难点在于 yt-dlp 的异常层层包装：``DownloadError.exc_info`` 是个
    **三元组** ``(异常类, 异常实例, traceback)``，而外层 ``msg``
    只是把内层原因重新说一遍。两种情况都得试 —— 顺序上先外层
    ``msg``（它通常是内层原因的复述，往往更完整），再拆 ``exc_info``。

    **绝不能对 exc_info 直接 str()** —— 那样会打出
    ``(<class '...'>, UnsupportedError('...'), <traceback object at 0x...>)``
    这种带内存地址的元组表示，用户完全看不懂。
    """
    for candidate in (exc, _inner_of(exc)):
        if candidate is None:
            continue
        msg = (getattr(candidate, "msg", None) or str(candidate) or "").strip()
        msg = re.sub(r"^ERROR:\s*", "", msg).strip()
        # 过滤掉元组/对象表示：它们以 "(" 开头且含 "<class" 或 "object at"
        if msg and "<class" not in msg and "object at 0x" not in msg:
            return msg
    return exc.__class__.__name__


def _inner_of(exc: Exception):
    """从 ``exc_info`` 三元组里取出内层异常实例；取不到返回 None。"""
    info = getattr(exc, "exc_info", None)
    if isinstance(info, tuple) and len(info) >= 2 and isinstance(info[1], BaseException):
        return info[1]
    return None


def _net_selfcheck(url: str, proxy: str = "") -> str:
    """解析失败时做一次最小网络自检，把「究竟卡在哪一步」问出来。

    只做两件事，不依赖 yt-dlp、不依赖第三方库：

    * DNS 解析（``getaddrinfo``）
    * 到 443 的 TCP 连接 + TLS 握手

    结论会拼进给用户看的报错里。之所以值得多花这几百毫秒，是因为
    yt-dlp 的 ``Unable to handle request`` 完全没告诉人卡点在哪：
    DNS 挂了、证书过期、代理不通，三种情况的**修法完全不同**，
    让用户对着同一句报错猜，是在浪费他的时间。
    """
    import socket
    import ssl
    from urllib.parse import urlsplit

    host = ""
    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        host = ""
    if not host:
        return ""

    parts: list[str] = []
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ip = infos[0][4][0] if infos else "?"
        parts.append(f"DNS 正常（{host} → {ip}）")
    except Exception as exc:
        parts.append(f"DNS 解析失败（{type(exc).__name__}: {exc}）"
                     "—— 多半是网络不通或 DNS 被污染，与程序设置无关")
        return "；".join(parts)

    try:
        ctx_ssl = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=8) as sock:
            with ctx_ssl.wrap_socket(sock, server_hostname=host) as tls:
                tls.getpeercert()
        parts.append("TLS 握手正常")
    except Exception as exc:
        parts.append(f"TLS/连接失败（{type(exc).__name__}: {exc}）"
                     "—— 常见于防火墙/安全软件拦截、系统根证书过旧、或代理不通")

    if proxy:
        parts.append(f"注意：当前配置了代理 {proxy}，请确认它在这台机器上可用")
    else:
        parts.append("未配置代理")
    return "；".join(parts)


def explain_failure(url: str) -> str:
    """把 yt-dlp 的报错翻译成「下一步做什么」。

    只看关键词，不做精确匹配 —— yt-dlp 各站点文案不统一，
    但「需要 cookie」这个意思到处都是，说法可以认。
    """
    raw = last_error(url)
    low = raw.lower()
    if not raw:
        return ""
    # 网络层根本没把请求发出去。放在最前面：这类报错里没有站点特征词，
    # 掉到后面的分支只会得到一句「原始报错：…」，等于没提示。
    if "unable to handle request" in low or "unexpected error" in low:
        return ("请求没能发出去（yt-dlp 的几个网络处理器都异常退出了，"
                "与链接、Cookie 无关）。按顺序排查："
                "1) 这台机器能否正常打开该网站；"
                "2) 是否开了代理/VPN/加速器 —— 填错地址会让全部请求都失败，"
                "可在「设置 → 媒体 → HTTP 代理」清空后重试；"
                "3) 防火墙/安全软件是否拦截了本程序；"
                "4) 系统时间与根证书是否正常。"
                "（报错后面附了本次的 DNS/TLS 自检结果，可直接对照）")
    if "cookie" in low or "login" in low or "sign in" in low or "登录" in raw:
        return ("该平台要求 Cookie / 登录态才能解析。"
                "在「设置 → 平台 Cookie」里填对应站点的 Cookie 后重试")
    if "unsupported url" in low:
        return ("yt-dlp 不认识这个链接（短链需要先展开成完整链接）。"
                "请复制浏览器地址栏里的完整网址再试")
    if "private" in low or "friends" in low:
        return "该视频是私密/仅好友可见，换一个公开视频"
    if "unavailable" in low or "removed" in low or "deleted" in low:
        return "该视频已被作者删除或设为不可见，换一个试试"
    if "proxy" in low or "timed out" in low or "connection" in low:
        return ("连不上服务器。海外站点请在「设置 → 媒体 → 代理」里填代理；"
                "国内站点则检查网络/防火墙")
    if "429" in raw or "too many requests" in low:
        return "请求太频繁被限流了，等几分钟再试"
    return "原始报错：" + raw


def pick_audio(info: dict) -> Optional[dict]:
    """在 formats 里挑最合适的一条音频流（纯音频优先，其次低码率视频）。"""
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    if not formats:
        if info.get("url"):
            return {"url": info["url"], "http_headers": info.get("http_headers") or {}}
        return None
    audio = [f for f in formats if f.get("vcodec") in (None, "none")]
    if audio:
        # 按音频码率排序，取最高
        audio.sort(key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0))
        return audio[-1]
    # 没有纯音频流，就挑最小的视频流（反正只取音轨）
    video = [f for f in formats if f.get("acodec") not in (None, "none")]
    if video:
        video.sort(key=lambda f: (f.get("tbr") or 0))
        return video[0]
    return formats[-1]


def headers_arg(fmt: dict, default_referer: str = "") -> str:
    """把 http_headers 拼成 ffmpeg ``-headers`` 需要的多行字符串。"""
    h = dict(fmt.get("http_headers") or {})
    if default_referer and "Referer" not in h:
        h["Referer"] = default_referer
    if "User-Agent" not in h:
        h.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0")
    return "".join(f"{k}: {v}\r\n" for k, v in h.items())


def download_audio(url_or_info, ctx: Context, out_path: str,
                   max_minutes: float = 90) -> str:
    """把视频的音轨抽成 16k 单声道 wav。

    ``url_or_info`` 可以是链接字符串，也可以是 ``extract_info`` 的结果。
    """
    if isinstance(url_or_info, dict):
        info = url_or_info
    else:
        info = extract_info(url_or_info, ctx) or {}
    fmt = pick_audio(info)
    if not fmt:
        raise MediaError("未能从该视频中解析出可用的音视频流",
                         hint="该站点可能已改版或需要登录 Cookie；可在设置里填写 Cookie 后重试")

    ffmpeg = ctx.ffmpeg()
    referer = info.get("webpage_url") or ""
    source = fmt["url"]
    headers = headers_arg(fmt, referer)

    # ffmpeg 的 -headers 只对 http(s) 协议生效；这里统一走 headers 传参
    return _extract_with_headers(ffmpeg, source, out_path, headers, max_minutes)


def _extract_with_headers(ffmpeg: str, source: str, out_path: str,
                          headers: str, max_minutes: float) -> str:
    import subprocess
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-headers", headers,
        "-i", source,
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
    ]
    if max_minutes and max_minutes > 0:
        cmd += ["-t", str(int(max_minutes * 60))]
    cmd.append(out_path)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        err = proc.stderr.decode("utf-8", "ignore")[-600:]
        raise MediaError(f"音频提取失败：{err}",
                         hint="若提示 403，通常是需要 Cookie 或 Referer；可在设置里填写后重试")
    return out_path


def clear_cache() -> None:
    with _CACHE_LOCK:
        _INFO_CACHE.clear()
    _LAST_ERROR.clear()
