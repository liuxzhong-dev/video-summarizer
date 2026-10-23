"""HTTP 客户端封装：统一 UA、重试、Cookie、代理。

国内平台大多有风控，所以这里默认：

* 移动端优先的 UA（比桌面 UA 更容易拿到数据）
* 自动跟随重定向并记录最终 URL（分享短链靠这个还原）
* 失败重试 + 指数退避
* 可插拔的 Cookie / 代理
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

import requests

from .errors import NetworkError

log = logging.getLogger(__name__)

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)


class HttpClient:
    """薄封装的 requests.Session。"""

    def __init__(self, timeout: int = 20, proxy: str = "", retries: int = 2):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA_DESKTOP,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "*/*",
        })
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
            log.info("使用代理: %s", proxy)

    # ---- 基础 ----
    def set_ua(self, kind: str = "desktop") -> None:
        self.session.headers["User-Agent"] = {
            "mobile": UA_MOBILE, "android": UA_ANDROID,
        }.get(kind, UA_DESKTOP)

    def set_cookie(self, cookie: str, referer: str = "") -> None:
        if not cookie:
            return
        self.session.headers["Cookie"] = cookie.strip()
        if referer:
            self.session.headers["Referer"] = referer

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)

    def request(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        kw.setdefault("allow_redirects", True)
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(method, url, **kw)
                resp.raise_for_status()
                return resp
            except Exception as exc:
                last = exc
                if attempt < self.retries:
                    sleep = 0.8 * (2 ** attempt) + random.uniform(0, 0.4)
                    log.debug("请求失败(%s)，%.1fs 后重试: %s", exc, sleep, url)
                    time.sleep(sleep)
        raise NetworkError(f"网络请求失败：{last}", hint="检查网络/代理设置，或确认该平台是否需要登录 Cookie")

    def get_text(self, url: str, encoding: str | None = None, **kw) -> str:
        resp = self.get(url, **kw)
        if encoding:
            resp.encoding = encoding
        elif not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def get_json(self, url: str, **kw) -> dict:
        return self.get(url, **kw).json()

    def final_url(self, url: str, **kw) -> str:
        """只跟随重定向拿最终 URL（分享短链还原用），不下载正文。"""
        kw.setdefault("timeout", self.timeout)
        kw["allow_redirects"] = True
        try:
            resp = self.session.head(url, **kw)
            return resp.url
        except Exception:
            kw["method"] = "GET"
            resp = self.session.get(url, stream=True, **kw)
            resp.close()
            return resp.url

    def close(self) -> None:
        self.session.close()
