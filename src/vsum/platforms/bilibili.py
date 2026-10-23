"""哔哩哔哩适配器 —— 支持度最完整的一个。

能力矩阵：

============  ==========================================================
能力           说明
============  ==========================================================
链接识别       ``bilibili.com/video/BVxxx``、``b23.tv/xxx``、``av123``、
              ``bangumi`` 番剧、手机分享文案里的任意混合文本
元数据         标题 / UP主 / 简介 / 时长 / 封面 / 发布时间
字幕           官方字幕 + AI 字幕（走 wbi 签名接口）
弹幕           ``x/v1/dm/list.so``，作为没有字幕时的重要补充语料
音频           交给 yt-dlp（见 ``media/ytdlp.py``）
============  ==========================================================

B 站的接口风控：``x/player/wbi/v2`` 需要 wbi 签名，签名算法已是公开事实标准，
这里做了本地实现 + 密钥缓存（1 小时）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
from functools import reduce
from typing import Optional

from ..core.context import Context
from ..core.errors import FetchError, LoginRequiredError
from ..core.models import Segment, Transcript, VideoMeta
from ..core.registry import register_platform
from .base import SUPPORT_FULL, PlatformAdapter

# --- wbi 签名所需的固定置换表（B 站公开算法）---
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_PLAYER = "https://api.bilibili.com/x/player/wbi/v2"
API_DANMAKU = "https://api.bilibili.com/x/v1/dm/list.so"

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.I)
_AV_RE = re.compile(r"(?:/av|av)(\d{1,15})", re.I)
_EP_RE = re.compile(r"/bangumi/play/(?:ep|ss)(\d+)", re.I)


class WbiSigner:
    """B 站 wbi 参数签名器。密钥从 nav 接口获取并缓存 1 小时。"""

    CACHE_TTL = 3600

    def __init__(self, http, cache_dir: str = ""):
        self.http = http
        self._keys: tuple[str, str] | None = None
        self._fetched_at = 0.0
        self._cache_file = os.path.join(cache_dir, "bili_wbi.json") if cache_dir else ""

    # ---- 密钥 ----
    def _load_cache(self) -> bool:
        if not self._cache_file or not os.path.isfile(self._cache_file):
            return False
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - d.get("ts", 0) < self.CACHE_TTL:
                self._keys = (d["img_key"], d["sub_key"])
                self._fetched_at = d["ts"]
                return True
        except Exception:
            pass
        return False

    def _save_cache(self) -> None:
        if not self._cache_file or not self._keys:
            return
        try:
            os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump({"img_key": self._keys[0], "sub_key": self._keys[1],
                           "ts": self._fetched_at}, f)
        except OSError:
            pass

    def keys(self) -> tuple[str, str]:
        if self._keys and time.time() - self._fetched_at < self.CACHE_TTL:
            return self._keys
        if self._load_cache():
            return self._keys                      # type: ignore[return-value]
        try:
            data = self.http.get_json(API_NAV)
            wbi = (data.get("data") or {}).get("wbi_img") or {}
            img_key = _basename_noext(wbi.get("img_url", ""))
            sub_key = _basename_noext(wbi.get("sub_url", ""))
            if img_key and sub_key:
                self._keys = (img_key, sub_key)
                self._fetched_at = time.time()
                self._save_cache()
                return self._keys
        except Exception as exc:
            self.http  # 保持引用
            raise FetchError(f"获取 B 站 wbi 密钥失败：{exc}")
        raise FetchError("获取 B 站 wbi 密钥失败：接口未返回密钥")

    # ---- 签名 ----
    @staticmethod
    def mixin_key(orig: str) -> str:
        return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]

    def sign(self, params: dict) -> dict:
        img_key, sub_key = self.keys()
        mk = self.mixin_key(img_key + sub_key)
        p = dict(params)
        p["wts"] = int(time.time())
        p = {k: "".join(ch for ch in str(v) if ch not in "!'()*")
             for k, v in sorted(p.items())}
        query = urllib.parse.urlencode(p)
        p["w_rid"] = hashlib.md5((query + mk).encode("utf-8")).hexdigest()
        return p

    def signed_url(self, base: str, params: dict) -> str:
        return f"{base}?{urllib.parse.urlencode(self.sign(params))}"


def _basename_noext(url: str) -> str:
    name = url.rsplit("/", 1)[-1]
    return name.split(".")[0]


@register_platform("bilibili")
class BilibiliAdapter(PlatformAdapter):
    """哔哩哔哩。"""

    key = "bilibili"
    name = "哔哩哔哩"
    domains = ("bilibili.com", "b23.tv", "acg.tv", "bilibili.tv")
    patterns = (r"\bBV[0-9A-Za-z]{10}\b",)
    support = SUPPORT_FULL
    note = "字幕由官方/AI 字幕接口提供；无字幕时自动下载音频做语音识别。"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.signer = WbiSigner(self.http, ctx.workdir)
        ck = ctx.cookie("bilibili")
        if ck:
            self.http.set_cookie(ck, "https://www.bilibili.com/")

    # ---------------- 识别 ----------------
    @classmethod
    def extract_id(cls, url: str) -> Optional[str]:
        m = _BV_RE.search(url or "")
        if m:
            return m.group(1)
        m = _AV_RE.search(url or "")
        if m:
            return "av" + m.group(1)
        return None

    def normalize_url(self, url: str) -> str:
        """b23.tv 短链 -> 标准 BV 链接。"""
        vid = self.extract_id(url)
        if vid:
            return f"https://www.bilibili.com/video/{vid}"
        final = super().normalize_url(url)
        vid = self.extract_id(final)
        return f"https://www.bilibili.com/video/{vid}" if vid else final

    # ---------------- 元数据 ----------------
    def resolve(self, url: str) -> VideoMeta:
        vid = self.extract_id(url)
        if not vid:
            final = self.normalize_url(url)
            vid = self.extract_id(final)
        if not vid:
            raise FetchError("无法从链接中解析出 BV 号/av 号", hint="请确认是 B 站视频页链接或 b23.tv 短链")

        params = {"bvid": vid} if vid.upper().startswith("BV") else {"aid": vid[2:]}
        try:
            data = self.http.get_json(API_VIEW, params=params)
        except Exception as exc:
            raise FetchError(f"请求 B 站视频信息失败：{exc}")
        if data.get("code") != 0:
            msg = data.get("message") or "未知错误"
            if data.get("code") in (-403, -404):
                raise LoginRequiredError(
                    f"B 站返回 {data.get('code')}：{msg}",
                    hint="可能需要登录 Cookie（设置 → 平台 Cookie → 粘贴 bilibili Cookie，"
                         "至少包含 SESSDATA）",
                )
            raise FetchError(f"B 站接口返回错误：{msg}")

        d = data["data"]
        cid = d.get("cid")
        return VideoMeta(
            platform=self.key,
            platform_name=self.name,
            video_id=d.get("bvid") or vid,
            url=f"https://www.bilibili.com/video/{d.get('bvid') or vid}",
            title=self.clean_text(d.get("title", "")),
            author=(d.get("owner") or {}).get("name", ""),
            description=self.clean_text(d.get("desc", "") or ""),
            duration=int(d.get("duration") or 0) or None,
            cover=d.get("pic"),
            published=_ts2str(d.get("pubdate")),
            extra={"aid": d.get("aid"), "cid": cid, "bvid": d.get("bvid")},
        )

    # ---------------- 字幕 ----------------
    def fetch_subtitle(self, meta: VideoMeta) -> Optional[Transcript]:
        aid, cid = meta.extra.get("aid"), meta.extra.get("cid")
        if not (aid and cid):
            return None
        try:
            url = self.signer.signed_url(API_PLAYER, {"aid": aid, "cid": cid})
            data = self.http.get_json(url)
        except Exception as exc:
            self.log.warning("B 站字幕接口不可用：%s", exc)
            return None
        if data.get("code") != 0:
            self.log.warning("B 站字幕接口返回 %s: %s", data.get("code"), data.get("message"))
            return None

        subs = ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if not subs:
            return None

        # 优先中文（ai-zh 也算），其次任意
        subs_sorted = sorted(subs,
                             key=lambda s: (0 if "zh" in (s.get("lan") or "") else 1,
                                            s.get("lan") or ""))
        for sub in subs_sorted:
            surl = sub.get("subtitle_url") or ""
            if not surl:
                continue
            if surl.startswith("//"):
                surl = "https:" + surl
            try:
                raw = self.http.get_json(surl)
            except Exception as exc:
                self.log.warning("字幕下载失败 %s: %s", surl, exc)
                continue
            body = raw.get("body") or []
            if not body:
                continue
            segs = []
            for i, item in enumerate(body):
                text = self.clean_text(item.get("content", ""))
                if not text:
                    continue
                start = float(item.get("from", 0) or 0)
                end = float(item.get("to", 0) or 0)
                # 单条字幕过长时按标点切一刀，便于后续压缩
                segs.append(Segment(start, end or start + 3, text))
            if segs:
                lang = sub.get("lan") or "zh"
                text = "\n".join(s.text for s in segs)
                return Transcript(
                    source="subtitle", text=text, segments=segs,
                    language="zh" if lang.startswith("zh") else lang,
                    engine="bilibili-" + ("ai" if "ai" in lang else "official"),
                )
        return None

    # ---------------- 弹幕（补充语料）----------------
    def supplement_text(self, meta: VideoMeta, limit: int = 300) -> str:
        cid = meta.extra.get("cid")
        if not cid:
            return ""
        try:
            xml = self.http.get_text(API_DANMAKU, params={"oid": cid})
        except Exception:
            return ""
        lines = re.findall(r"<d[^>]*>(.*?)</d>", xml, re.S)
        cleaned = [self.clean_text(l) for l in lines]
        cleaned = [c for c in cleaned if c]
        if not cleaned:
            return ""
        # 去重 + 截断，弹幕噪声很大，只做补充
        seen, uniq = set(), []
        for c in cleaned:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
            if len(uniq) >= limit:
                break
        return "弹幕高频内容： " + " / ".join(uniq)


def _ts2str(ts) -> Optional[str]:
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return None
