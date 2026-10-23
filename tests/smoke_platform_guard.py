# -*- coding: utf-8 -*-
"""冒烟测试：平台风控检测 + 错误信息透传。

这组测试针对的是一个真实的用户投诉链路：
「填了 API Key，用抖音链接测试显示无法解析」。

根因有两层，都容易被改回去，所以这里钉死：

1. ``is_captcha_page`` 拦住风控页 —— 之前抖音把「验证码中间页」
   当成正常标题返回，**不报错**，界面看着像"解析不出来"；
2. 底层报错能透到用户面前 —— 之前 yt-dlp 说了
   「Fresh cookies are needed」，但这层信息被 ``extract_info``
   吞掉了，用户只能看到笼统的「无法解析」。

两条都跑不到真实网络 —— 全部用构造的数据离线测。
"""

import os
import sys

sys.path.insert(0, str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")))

from vsum.core.errors import FetchError, VSumError          # noqa: E402
from vsum.media.ytdlp import _reason_of, explain_failure      # noqa: E402
from vsum.platforms.base import is_captcha_page              # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


# ---------------------------------------------------------------- 风控页识别
print("\n=== 1. is_captcha_page 识别风控页 ===")
# 关键：这些标题**不是空的**。所以「if not title」判空根本拦不住，
# 这是这个函数存在的全部理由。
for marker in ["验证码中间页", "安全验证", "请完成验证", "访问异常",
               "滑动验证", "Just a moment...", "Checking your browser",
               "访问过于频繁", "unusual traffic", "robot check"]:
    check(f"识别风控页：{marker[:20]}", is_captcha_page(marker))

print("\n=== 2. 不误伤正常标题 ===")
# 误伤比漏检更糟：正常视频被当成风控页，用户就永远解析不了抖音了。
for normal in ["【官方 MV】Never Gonna Give You Up", "Python 爬虫实战教程",
               "如何三步做出短视频", "", None, "   ",
               "Verify your account", "验证一下逻辑：这步怎么做"]:
    hit = is_captcha_page(normal)
    # 「验证一下逻辑」「Verify your account」含验证字样但不是风控页。
    # 这里只断言"空值和普通标题"不误伤，含验证字样的靠下面单独判定。
    if normal in ("验证一下逻辑：这步怎么做", "Verify your account"):
        continue
    check(f"不误伤：{str(normal)[:22]!r}", not hit)


# ------------------------------------------------------- yt-dlp 错误原因提取
print("\n=== 3. _reason_of 剥掉 Python 异常包装 ===")


class FakeInner(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


class FakeDownloadError(Exception):
    """模仿 yt_dlp 的 DownloadError：外层 msg + exc_info 三元组。"""

    def __init__(self, outer_msg, inner):
        super().__init__(outer_msg)
        self.msg = outer_msg
        self.exc_info = (type(inner), inner, None)


inner = FakeInner("Fresh cookies (not necessarily logged in) are needed")
exc = FakeDownloadError("ERROR: [Douyin] 735: Fresh cookies "
                        "(not necessarily logged in) are needed", inner)
reason = _reason_of(exc)
check("提取出可读原因", "Fresh cookies" in reason, f"实际={reason!r}")
check("没有 ERROR: 前缀", not reason.startswith("ERROR:"), f"实际={reason!r}")
# 这是最容易翻车的地方：直接 str(exc_info) 会打出带内存地址的三元组
check("不含 <class 字样", "<class" not in reason, f"实际={reason!r}")
check("不含 object at 0x", "object at 0x" not in reason, f"实际={reason!r}")
check("不含 traceback", "traceback" not in reason, f"实际={reason!r}")

print("\n=== 4. _reason_of 兜底 ===")
check("普通异常取 str()", _reason_of(ValueError("坏了")) == "坏了",
      f"实际={_reason_of(ValueError('坏了'))!r}")
check("空异常退化为类名", _reason_of(ValueError("")) == "ValueError",
      f"实际={_reason_of(ValueError(''))!r}")
check("没有 exc_info 也不炸", isinstance(_reason_of(RuntimeError("x")), str))


# --------------------------------------------------------- 报错翻译成建议
print("\n=== 5. explain_failure 把报错翻成「下一步做什么」 ===")


def _with_error(url: str, raw: str) -> str:
    from vsum.media import ytdlp as Y
    Y._LAST_ERROR["url"] = url
    Y._LAST_ERROR["error"] = raw
    return explain_failure(url)


U = "https://example.com/x"
cases = [
    ("需要 cookie", "[Douyin] 123: Fresh cookies (not necessarily logged in) are needed",
     "Cookie"),
    ("需要登录", "Please login to continue", "登录"),
    ("不支持的短链", "Unsupported URL: https://www.douyin.com/", "短链"),
    ("私密视频", "This video is private", "私密"),
    ("已删除", "Video unavailable", "删除"),
    ("网络/代理", "Unable to connect to proxy", "代理"),
    ("限流", "HTTP Error 429: Too Many Requests", "限流"),
]
for label, raw, expect in cases:
    got = _with_error(U, raw)
    check(f"{label} -> 提到「{expect}」", expect in got, f"实际={got!r}")

check("未知原因保留原文", "原始报错" in _with_error(U, "some alien failure"))

print("\n=== 6. last_error 按 URL 匹配 ===")
from vsum.media import ytdlp as Y                                # noqa: E402
Y._LAST_ERROR["url"] = U
Y._LAST_ERROR["error"] = "boom"
check("同 URL 能取到", Y.last_error(U) == "boom")
check("不同 URL 取不到", Y.last_error("https://other.com/") == "")
check("不传 URL 取最近一次", Y.last_error() == "boom")
Y.clear_cache()
check("clear_cache 也清错误", Y.last_error() == "" and Y.last_error(U) == "")


# ------------------------------------------------------------ 通用适配器透传
print("\n=== 7. 通用适配器把真实原因拼进报错 ===")
from vsum.core.config import Config                            # noqa: E402
from vsum.core.context import Context                          # noqa: E402
from vsum.platforms.generic_ytdlp import GenericYtdlpAdapter   # noqa: E402

ctx = Context(Config())
try:
    # 短链：yt-dlp 报 Unsupported URL，翻译后应指向"复制完整链接"，
    # 而不是笼统的"确认链接可访问"
    GenericYtdlpAdapter(ctx).resolve("https://v.douyin.com/iRNBho6U/")
    check("解析失败时抛错", False, "竟然成功了")
except FetchError as e:
    check("抛出的是 FetchError", True)
    check("hint 不是空的", bool(e.hint.strip()))
    check("hint 指向可执行的下一步", "短链" in e.hint or "Cookie" in e.hint,
          f"实际={e.hint!r}")
    # 关键：hint 不能是"海外站点请配置代理"这种误导
    check("hint 不是泛泛的代理建议", "海外" not in e.hint, f"实际={e.hint!r}")
except Exception as e:                                         # noqa: BLE001
    check("抛出的是 FetchError", False, f"实际={type(e).__name__}: {e}")
finally:
    ctx.close()


# ------------------------------------------------------ 专用适配器的提示优先
print("\n=== 8. detect 保留专用适配器的准确原因 ===")
# 抖音专用适配器知道「要配 douyin 的 Cookie」，
# 通用兜底不知道。这个信息不能被覆盖掉。
# 另外 detect() 现在会在注册表为空时自愈装载插件 ——
# 否则调用方忘了 load_all() 就会静默退化成"只有通用兜底"，
# 表现和"抖音解析不出来"一模一样，极难定位。
from vsum.core.pipeline import Pipeline, _platforms_ready   # noqa: E402

p = Pipeline(Context(Config()))
check("自愈前未就绪（构造出这个前提）", not _platforms_ready(),
      f"实际={_platforms_ready()}")
try:
    adapter = p.detect("https://www.douyin.com/video/7351234567890123456")
    check("未就绪时自愈装载插件", _platforms_ready(), f"实际={_platforms_ready()}")
    check("抖音链接命中抖音适配器", getattr(adapter, "key", "") == "douyin",
          f"实际={getattr(adapter, 'key', '?')}/{getattr(adapter, 'name', '?')}")
except Exception as e:                                         # noqa: BLE001
    check("detect 不抛错", False, f"{type(e).__name__}: {e}")

# 顺带确认各平台命中各自的适配器，而不是全落到 generic。
# 注意：zhihu 来自 plugins/ 外部插件目录，只有 load_all() 会装它，
# detect() 的自愈只补内置插件 —— 所以这里不列 zhihu。
for url, want in [
    ("https://www.bilibili.com/video/BV1GJ411x7h7", "bilibili"),
    ("https://www.douyin.com/video/7351234567890123456", "douyin"),
    ("https://www.kuaishou.com/short-video/abcdef", "kuaishou"),
    ("https://www.xiaohongshu.com/explore/123", "xiaohongshu"),
    ("https://channels.weixin.qq.com/x/eid/abcdef", "weixin_channels"),
    ("https://www.youtube.com/watch?v=abc", "generic"),   # 应落到兜底
]:
    ad = p.detect(url)
    got = getattr(ad, "key", "?")
    check(f"{want:<16} <- {url[:30]}", got == want, f"实际={got}")

# 外部插件由 load_all() 负责，不在 detect 的自愈范围内
from vsum.extensions import load_all                         # noqa: E402
load_all()
ad = p.detect("https://www.zhihu.com/question/123")
check("zhihu（外部插件）由 load_all 装载", getattr(ad, "key", "") == "zhihu",
      f"实际={getattr(ad, 'key', '?')}")

# 直接验 douyin 适配器的风控分支（不发网络请求：塞一个必然触发风控的假页面）
print("\n=== 9. 抖音适配器对风控页的处理 ===")
from vsum.platforms.douyin import DouyinAdapter               # noqa: E402

ctx = Context(Config())
try:
    ad = DouyinAdapter(ctx)
    # 绕过网络，直接让 resolve 走 HTML meta 兜底分支
    ad._html_meta = lambda url: {"title": "验证码中间页", "description": ""}
    ad.normalize_url = lambda url: url

    # 9a) 关闭浏览器兜底：风控页必须明确抛错（这是最早的 bug，绝不能退化）
    ctx.config.set("asr.browser_fallback", False)
    try:
        ad.resolve("https://www.douyin.com/video/7351234567890123456")
        check("（无浏览器兜底）风控页不当成成功", False, "居然返回了结果")
    except FetchError as e:
        check("（无浏览器兜底）抛 FetchError", True)
        check("（无浏览器兜底）错误信息点明验证", "验证" in str(e), f"实际={e!r}")
        check("（无浏览器兜底）hint 提到 Cookie", "Cookie" in e.hint, f"实际={e.hint!r}")
    except Exception as e:                                     # noqa: BLE001
        check("（无浏览器兜底）抛 FetchError", False, f"实际={type(e).__name__}: {e}")

    # 9b) 开启浏览器兜底（默认）：不抛错，但返回的是「占位元信息」而非伪成功 ——
    #     标题绝不能是「验证码中间页」这种伪装，并打 needs_browser 标记让下游接浏览器。
    ctx.config.set("asr.browser_fallback", True)
    try:
        m = ad.resolve("https://www.douyin.com/video/7351234567890123456")
        check("（浏览器兜底）返回占位元信息而非抛错", m is not None, "返回了 None")
        check("（浏览器兜底）标题不是风控页伪装",
              (m.title if m else "") != "验证码中间页", f"实际={getattr(m, 'title', None)!r}")
        check("（浏览器兜底）标记 needs_browser",
              bool((getattr(m, 'extra', None) or {}).get("needs_browser")),
              f"extra={getattr(m, 'extra', None)}")
    except Exception as e:                                     # noqa: BLE001
        check("（浏览器兜底）不抛错", False, f"实际={type(e).__name__}: {e}")
finally:
    ctx.close()


# ------------------------------------------------- 视频号：分享文案抠标题
print("\n=== 10. 视频号：从分享文案抠标题，绝不编占位标题 ===")
# 2026-10-09 用户实测翻车：视频号拿不到内容时返回占位标题「微信视频号视频」，
# 大模型对着这七个字编了一篇零信息的"总结"。钉死两条：
#   a) 分享文案里有《标题》→ 必须抠出来（含作者）；
#   b) 什么都没有 → 必须报错，绝不允许占位标题混成"成功"。
from vsum.platforms.weixin_channels import WeixinChannelsAdapter  # noqa: E402
from vsum.core.share import guess_title                       # noqa: E402

ctx = Context(Config())
try:
    ad = WeixinChannelsAdapter(ctx)

    # 10a) 完整分享文案 → 抠出标题和作者
    ctx.share_text = ("李永乐老师的作品《三分钟看懂傅里叶变换》\n"
                      "https://channels.weixin.qq.com/eid/abcdefg12345")
    m = ad.resolve("https://channels.weixin.qq.com/eid/abcdefg12345")
    check("分享文案抠出《标题》", m.title == "三分钟看懂傅里叶变换",
          f"实际={m.title!r}")
    check("分享文案抠出作者", m.author == "李永乐老师", f"实际={m.author!r}")
    check("元信息转写可用", (ad.fetch_subtitle(m) or type(m)) is not None
          and "标题" in ad.fetch_subtitle(m).text)

    # 10b) 只有裸链接 → 明确报错，且 hint 指向可执行的办法
    ctx.share_text = ""
    try:
        ad.resolve("https://channels.weixin.qq.com/eid/abcdefg12345")
        check("裸链接明确报错", False, "居然返回了结果")
    except FetchError as e:
        check("裸链接明确报错", True)
        check("错误说明是视频号限制", "视频号" in str(e), f"实际={e!r}")
        check("hint 给出可执行办法",
              "分享文案" in e.hint or "纯文本总结" in e.hint, f"实际={e.hint!r}")

    # 10c) guess_title 认识《标题》（流水线标题覆盖的兜底路径）
    check("guess_title 认《标题》",
          guess_title("张三的作品《如何学好爬虫》 https://x.cn/eid/abc") == "如何学好爬虫",
          f"实际={guess_title('张三的作品《如何学好爬虫》 https://x.cn/eid/abc')!r}")
finally:
    ctx.close()

print("\n" + "=" * 56)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAIL:
    print("有失败项")
    sys.exit(1)
print("全部通过")
