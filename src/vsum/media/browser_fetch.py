"""
浏览器兜底 —— 用真实浏览器拿视频直链，解决强风控平台（抖音/快手/小红书）。

为什么需要它
------------
抖音拦的是「像浏览器的 HTTP 请求」，判据包括 ``s_v_web_id`` 这类
**由浏览器 JS 动态生成**的凭证（实测：服务器既不在响应里下发，
页面 HTML 里也没有这段配置，纯粹是 JS 跑出来的）。
所以 ``requests`` 无论怎么伪造 Headers 都过不去 ——
实测把 Cookie 里的 ``verify_`` 去掉、整个删掉该字段、只留三个关键字段，
yt-dlp 三种改法**全都失败**。

而 Playwright 启动的是**真实的 Chromium 内核**（系统 Edge 或自带的 Chromium），
JS 正常执行、凭证正常生成，抖音认它就像认你手动打开浏览器一样。

**这不是破解**：不绕验证码、不解密 DRM、不批量抓取，
只是「让浏览器自己去请求，然后站在旁边听响儿」——
和你手动打开视频播放是一回事，只是自动化了。

两个实测踩出来的关键点（决定实现方式的那两个）
------------------------------------------------
1. **直链必须带 Referer 才能用 requests 下载。**
   早先对``douyinvod.com`` 直链直接发请求得到 **403**；
   加上页面 URL 作 Referer 后立刻 HTTP 200。
   所以正确流程是「浏览器抓直链 → requests + Referer 下载」，
   **不需要**从响应体重建 mp4 —— 那条路会卡在 fMP4 的空``trex`` 上，
   是自己绕出来的弯路。
2. **抖音同时下多档清晰度**，抓回的响应里混着特效素材（``effectcdn``）、
   埋点、图片。必须按 CDN 主机筛出真正的视频流，并选体积最大的那条。

v18：内核也打进 exe 了
----------------------
此前 exe 里只有**驱动**（playwright 包 + node，约 100 MB），浏览器本体在
``%LOCALAPPDATA%\\ms-playwright``（271 MB），**从来没进过包**。
于是把 exe 发给别人时会出现这种怪事：我们这边抖音一切正常，
对方点下去就是失败 —— 他那台机器没有 Edge / Chrome，也没下过内核。

v18（B1 方案）把**精简后的 headless shell 一并打进 exe**：
剔掉 220 种语言包、许可证、西文断字词典和 SwiftShader Vulkan 图形栈，
271 MB → 219 MB，压缩后给 exe 增加约 100 MB。
实测该精简版能正常启动（3.25s，Chromium 153.0.8010.12）且**拿到
``s_v_web_id``**，即照样过抖音风控。

代价是 exe 从 206 MB 涨到约 310 MB，且 onefile 每次启动都要把整个归档
解压到 ``%TEMP%``，冷启动会慢几秒 —— 换来的是"发给谁都能用"。

注意内置的是 ``chrome-headless-shell``，**只能无头跑**，没有窗口。
所以「无头抓不到 → 切有头重试」这条退路只在**系统浏览器**存在时才有意义；
内置通道只在 ``headless=True`` 时参与。
"""

from __future__ import annotations

import os
import sys
from typing import Callable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

#: 与浏览器一致的 UA，避免被识别成自动化工具
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

#: 真正的视频 CDN 主机特征
_CDN_HINTS = ("douyinvod.com", "douyin.com/video/tos", "kuaishou.com",
              "kwaixiaodian.com", "xhscdn.com", "xiaohongshu.com",
              "ibytedtos.com")

#: 明确排除 —— 特效素材 / 埋点 / 图片，不是视频内容
_SKIP_HINTS = ("effectcdn", "bytegoofy", "byteimg.com", "ibytedapm",
               "slardar", "bdms_", "pstatp", "lf3-short", "lf-security",
               "static.zhimg", "picasso", "ies.fe.effect",
               "uuu_",  # 抖音网页版自带的占位视频（不是内容）
               )

#: 允许直接下载的视频扩展名
_VIDEO_EXT = (".mp4", ".m4s", ".flv", ".webm")


class BrowserFetchError(Exception):
    """浏览器兜底抓取失败。``hint`` 给用户看下一步该做什么。

    **必须自己收 ``hint``**：内置 ``Exception`` 不接受关键字参数，
    少了这个 ``__init__``，``BrowserFetchError("x", hint="y")`` 会当场抛
    ``TypeError``，把真正的失败原因盖掉 —— 外层只看到一句
    "浏览器兜底异常"，用户再也拿不到该给的提示。
    （2026-10-10 修：这条路径原先一直是坏的，只因它只在抖音/快手/小红书
    走到，离线测试覆盖不到。）
    """

    def __init__(self, msg: str, hint: str = ""):
        super().__init__(msg)
        self.hint = hint


def _missing_playwright_msg() -> Tuple[str, str]:
    """没装 playwright 时的 ``(提示语, 下一步建议)``。

    分两种口径，因为**用户能做的事完全不同**：

    * **冻结版（打包后的 exe）**：包里没带的东西，用户自己补不上 ——
      把 playwright 装进系统 Python，exe 也不会去读。所以这里绝不能说
      「去 pip install」，那只会让人按着做一遍、然后发现毫无变化。
    * **源码运行**：直接告诉他装哪个包就行。
    """
    if getattr(sys, "frozen", False):
        return (
            "此版本未内置浏览器兜底（Playwright）",
            "这是打包时的遗漏，不是你操作的问题。\n"
            "        临时办法：① 在「设置 → 平台 Cookie」填该平台的 Cookie 后重试；\n"
            "        ② 把视频标题/简介粘进输入框，走「纯文本总结」。",
        )
    return (
        "没装 Playwright，浏览器兜底不可用",
        "在项目目录执行：.venv\\Scripts\\pip install playwright\n"
        "        （随后再执行 python -m playwright install chromium-headless-shell "
        "下载内核，约 130 MB；不下载则只能试系统已装的 Edge/Chrome）",
    )


def _is_video_stream(url: str, content_type: str) -> bool:
    """判断一个响应是不是「视频内容流」（不是素材/埋点/图片）。"""
    low = url.lower()
    if any(h in low for h in _SKIP_HINTS):
        return False
    if any(h in low for h in _CDN_HINTS):
        return True
    if (content_type or "").lower().startswith("video/"):
        return True
    path = unquote(urlparse(url).path).lower()
    return path.endswith(_VIDEO_EXT)


def grab_stream_urls(url: str,
                     timeout_ms: int = 25000,
                     wait_ms: int = 10000,
                     progress: Optional[Callable[[str], None]] = None,
                     headless: bool = True) -> Tuple[List[str], str]:
    """用真实浏览器打开页面，返回 ``(视频直链列表, 页面标题)``。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _msg, _hint = _missing_playwright_msg()
        raise BrowserFetchError(_msg, hint=_hint)

    say = progress or (lambda _s: None)
    last_err = ""

    # 先无头，抓不到再切有头（用户能看到浏览器在跑，便于确认是否有验证码）
    for hl in ((True, False) if headless else (False,)):
        say("启动浏览器…" if hl else "无头模式没抓到，切换有头模式重试")
        try:
            got, title = _one_page(url, timeout_ms, wait_ms, hl, say)
        except BrowserFetchError:
            raise
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            continue
        if got:
            return got, title

    raise BrowserFetchError(
        f"浏览器没抓到视频流{last_err and '（' + last_err[:60] + '）'}",
        hint="可能原因：① 视频需要登录或付费；② 页面要求人机验证"
             "（有头模式下能看到窗口，看有没有验证码）；"
             "③ 网络慢，播放器还没发请求。\n"
             "        临时办法：把视频标题简介粘进输入框走「纯文本总结」。")


def _bundled_shell() -> str:
    """打包进 exe 的精简 headless shell 绝对路径；没有则返回空串。

    **只在冻结环境（``sys._MEIPASS``）下才可能存在** —— 源码运行时
    内核在 ``%LOCALAPPDATA%``，走的是下面「默认 Chromium」通道。

    版本号目录不写死：直接扫 ``pw-browsers/`` 下 ``chromium_headless_shell-*``，
    取 revision 最大的那个。写死 1243 的后果是升级 playwright 后内核目录改名，
    这里静默返回空、内置兜底悄悄消失 —— 正是我们最怕的那类"静默降级"。
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return ""
    root = os.path.join(base, "pw-browsers")
    if not os.path.isdir(root):
        return ""
    best = ("", "")
    for name in os.listdir(root):
        if not name.startswith("chromium_headless_shell-"):
            continue
        exe = os.path.join(root, name, "chrome-headless-shell-win64",
                           "chrome-headless-shell.exe")
        if os.path.isfile(exe):
            rev = name.rsplit("-", 1)[-1]
            if rev > best[0]:
                best = (rev, exe)
    return best[1]


def _launch_channels(headless: bool = True) -> List[Tuple[str, dict]]:
    """浏览器启动通道，**按优先级**排列。

    为什么不能写死一个
    ------------------
    Playwright 的驱动和浏览器本体是**两回事**：
    驱动（node + playwright 内核）随包走，但浏览器本体默认下载到
    ``%LOCALAPPDATA%\\ms-playwright\\``（本机实测 707 MB），
    **不在 exe 里**。

    所以「打包了 playwright」≠「用户机器上一定有浏览器」。
    新电脑上大概率三种情况都有缺口：
      * 精简版系统镜像 / 国产预装机 —— 常常没有 Edge，更没有 Chrome；
      * 装了 Edge 但版本被组织策略禁用、或者用户手动卸载过浏览器；
      * 有浏览器但不在 Playwright 认识的 channel 列表里。

    写死 ``channel="msedge"`` 等于把「抖音能不能用」押在一台机器上，
    换台电脑就翻车。实测（2026-10-10）各通道差异：

    ==========  ==================  ====================
    通道        B站抓流条数        抖音 s_v_web_id
    ==========  ==================  ====================
    系统 Edge   26 条✓             ✓ 有，过了风控
    系统 Chrome  0 条（首页可开）  未测
    内置Chromium 19 条✓✓           ✓ 有，过了风控
    ==========  ==================  ====================

    **内置 Chromium 与系统 Edge 一样能过抖音风控**（两者同为 Chromium 内核，
    JS 生成的凭证逻辑一致），所以把它作为主力是安全的 ——
    省掉 707 MB 浏览器下载，且在任何一台 Windows 上都能跑。

    顺序含义：先试**免费且已装好**的系统浏览器（启动快、体积为零），
    再退到 v18 起**随 exe 自带的精简内核**（这才是"换台电脑也能用"的保底），
    最后才是本机 ``%LOCALAPPDATA%`` 里的内核（源码运行时主要走这条）。

    为什么不把内置内核放第一位：系统 Edge 启动更快、内存更省，
    实测抓 B站 26 条（内置 19 条）。内置内核的定位是"没有系统浏览器时兜住"，
    不是替代 —— 两者都能过抖音风控，没必要为此牺牲常用路径的速度。

    **「默认 Chromium」必须放在「内置 Chromium」后面**，且顺序不能换 ——
    实测（2026-10-10，把 ms-playwright 目录移走后逐个试）：
        channel=msedge     → ✓ 154.0.4258.37（系统浏览器，与内置目录无关）
        channel=chromium   → ✗ Executable doesn't exist at ...\\chromium-1243\\chrome-win64\\chrome.exe
        不传 channel        → ✓ 153.0.8010.12（走 chromium_headless_shell-1243）
    两个"内置"通道要的**不是同一个目录**：``channel="chromium"`` 找完整版
    chromium-1243（433 MB），不传 channel 只要 headless_shell-1243（271 MB）。
    我们是无头抓流，用后者就够，所以把不传 channel 放在前面先试 ——
    这样只下了 headless_shell 的机器也能兜住，不会白等一次 433MB 的完整版。
    """
    chans: List[Tuple[str, dict]] = [
        ("系统 Edge", {"channel": "msedge"}),        # Windows 自带，命中率最高
        ("系统 Chrome", {"channel": "chrome"}),      # 装了 Chrome 的机器
    ]
    shell = _bundled_shell()
    if shell and headless:
        # 内置的是 chrome-headless-shell，**没有窗口**，
        # 所以有头模式（headless=False）不能拿它顶上。
        chans.append(("内置精简内核", {"executable_path": shell}))
    chans += [
        ("默认 Chromium", {}),                       # 本机 ms-playwright 的 headless shell
        ("内置 Chromium", {"channel": "chromium"}),  # 需完整 chromium-1243，兜底
    ]
    return chans


def _launch_browser(p, headless: bool, say):
    """依次尝试各通道，返回 ``(browser, 通道名)``；全失败则抛错。"""
    args = ["--mute-audio", "--autoplay-policy=no-user-gesture-required"]
    tried: List[str] = []
    for label, kw in _launch_channels(headless):
        try:
            return p.chromium.launch(headless=headless, args=args, **kw), label
        except Exception as exc:
            tried.append(f"{label}({type(exc).__name__})")
            continue
    raise BrowserFetchError(
        "这台机器上找不到任何可用的浏览器内核",
        hint="程序已尝试：" + "、".join(tried) + "。\n"
             "        打包版自带内核时几乎不会走到这里；若真出现了，多半是"
             "系统浏览器被卸载/禁用，且 exe 内的内核也没能解压出来"
             "（%TEMP% 空间不足或被安全软件清掉了）。\n"
             "        源码运行的话：执行 python -m playwright install "
             "chromium-headless-shell（约 130 MB，一次性）")


def _one_page(url, timeout_ms, wait_ms, headless, say) -> Tuple[List[str], str]:
    from playwright.sync_api import sync_playwright

    captured: List[str] = []
    with sync_playwright() as p:
        browser, chan = _launch_browser(p, headless, say)
        say(f"浏览器已启动（{chan}）")
        try:
            ctx = browser.new_context(user_agent=UA,
                                      viewport={"width": 1280, "height": 800},
                                      locale="zh-CN")
            page = ctx.new_page()

            def on_response(r):
                try:
                    if r.status in (200, 206) and _is_video_stream(
                            r.url, r.headers.get("content-type", "")):
                        captured.append(r.url)
                except Exception:
                    pass

            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                say("页面已打开，等播放器请求视频流…")
            except Exception:
                say("页面加载较慢，继续监听已发出的请求")
            for sel in ("video", ".player", ".bilibili-player"):
                try:
                    page.locator(sel).first.click(timeout=1000)
                    break
                except Exception:
                    continue
            page.wait_for_timeout(wait_ms)
            title = (page.title() or "").strip() or "未命名视频"
        finally:
            try:
                browser.close()
            except Exception:
                pass

    return list(dict.fromkeys(captured)), title


def download_direct(url: str,
                    referer: str,
                    dest: str,
                    progress: Optional[Callable[[str], None]] = None,
                    timeout: int = 60) -> str:
    """用 requests 下载直链。**Referer 必须带上**，否则 CDN 返回 403。"""
    import requests

    headers = {
        "User-Agent": UA,
        "Referer": referer or "https://www.douyin.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    with requests.get(url, headers=headers, stream=True,
                      timeout=(10, timeout)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(262144):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(f"下载视频 {done * 100 // total}%")
    os.replace(tmp, dest)
    return dest


def fetch(url: str,
          out_dir: str,
          filename: str = "browser_video.mp4",
          progress: Optional[Callable[[str], None]] = None,
          headless: bool = True) -> Tuple[str, str]:
    """完整流程：浏览器抓直链 → requests 下载 → 返回 ``(本地路径, 标题)``。"""
    say = progress or (lambda _s: None)
    streams, title = grab_stream_urls(url, progress=say, headless=headless)
    if not streams:
        raise BrowserFetchError("浏览器没抓到视频流")
    say(f"抓到 {len(streams)} 条视频流，开始下载…")
    dest = os.path.join(out_dir, filename)
    download_direct(streams[0], url, dest, progress=say)
    say(f"下载完成：{os.path.getsize(dest) / 1024 / 1024:.1f} MB")
    return dest, title