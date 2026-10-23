"""主流水线：把一套链接变成一份总结。

::

    分享文案 ──▶ 提取链接 ──▶ 匹配平台插件 ──▶ 解析元数据
                ┌──────── 字幕 ────────┐
                │  命中？               │
                │  否 ──▶ 下载音频 ──▶ ASR
                └──────────┬───────────┘
                           ▼
                    大模型摘要（失败则本地抽取式兜底）
                           ▼
                      SummaryResult

每一步都能单独失败并降级，最终**一定**产出结果（除非链接本身无效）。
"""

from __future__ import annotations

import os
import time
import traceback
from typing import Callable, Optional
from urllib.parse import urlparse

from .context import Context
from .errors import VSumError
from .models import SummaryResult, Transcript
from .registry import all_platforms
from .share import extract_url, guess_title


#: 平台插件是否已就绪所必需的关键 key。
#: 少了任何一个都说明装载不完整，此时所有链接都会退到通用兜底。
_REQUIRED_PLATFORMS = ("bilibili", "douyin", "kuaishou",
                       "xiaohongshu", "weixin_channels", "generic")


def _platforms_ready() -> bool:
    """内置平台插件是否都已注册。

    注意这里**检查具体 key 而不是「注册表非空」** ——
    只导入了 ``generic_ytdlp`` 时注册表同样非空，但那意味着
    抖音/B 站等适配器全都不在，每个平台链接都会静默走 yt-dlp。
    """
    have = all_platforms()
    return all(k in have for k in _REQUIRED_PLATFORMS)


#: 这些主机的链接是「文章 / 图文」，优先交给 ArticleAdapter 抽正文，
#: 而不是 generic（yt-dlp）。**不在表里的**（含 youtube / vimeo / tiktok 等视频站、
#: 以及未知站点）一律走 generic —— 这符合「视频总结」的产品定位：
#: YouTube 这类链接应当被 yt-dlp 下载成视频来总结，而不是被 ArticleAdapter
#: 当博客页面去抽「文章正文」（那样只能拿到页面描述，丢掉了视频本身）。
_ARTICLE_HOST_HINTS = (
    "mp.weixin.qq.com", "weixin.qq.com",
    "zhihu.com",
    "juejin.cn", "csdn.net", "blog.csdn.net", "cnblogs.com",
    "toutiao.com", "weibo.com",
    "xiaohongshu.com",
    "jianshu.com", "baidu.com",
    "36kr.com", "sspai.com", "huxiu.com", "geektime.cn", "thepaper.cn",
    "medium.com", "substack.com", "wordpress.com", "blog.",
    "arxiv.org", "github.io", "gitee.io",
)


def _is_article_host(host: str) -> bool:
    """该主机更像「文章/图文页」而非视频站吗？"""
    host = (host or "").lower().strip(".")
    for d in _ARTICLE_HOST_HINTS:
        if host == d or host.endswith("." + d):
            return True
        # 形如 "blog." 的前缀型提示：blog.example.com 也算文章站
        if d.endswith(".") and host.startswith(d):
            return True
    return False


class Pipeline:
    """一次总结任务的编排者。"""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.log = ctx.log

    # ---------------- 平台识别 ----------------
    def detect(self, url: str):
        """返回能处理该链接的平台适配器实例（找不到返回通用适配器）。"""
        url = url or ""
        # 插件没装载（或只装了一部分）时**绝不能返回通用兜底** ——
        # 那会让所有平台链接都走 yt-dlp，表现是「抖音解析不出来」，
        # 但根因是插件压根没加载，跟链接本身毫无关系。
        #
        # 判据不能是「注册表是否为空」：只要有任何适配器模块被 import 过
        # （哪怕只是间接引用），注册表就非空了，此时再判断就会误以为
        # 「已装载」，然后静默退化成通用兜底。症状一模一样，极难定位。
        # 所以改成检查**关键平台是否齐全**。
        #
        # 顺序同样重要：必须在 import 任何适配器之前判断，
        # 因为 import 本身会触发注册装饰器。
        if not _platforms_ready():
            from ..extensions import load_builtin
            load_builtin()

        from ..platforms.base import PlatformAdapter
        from ..platforms.generic_ytdlp import GenericYtdlpAdapter

        # 优先级分三档，**顺序不能随意调**：
        #   1. 视频平台（有 domains，长度越长越具体）—— B站/抖音等专用解析
        #   2. 文章 / 纯文本适配器（domains 为空，靠「链接是否可抓正文」区分）
        #   3. 通用兜底 generic（yt-dlp）
        #
        # 关键：ArticleAdapter.match() 接受任意 http 链接，所以 article_like 会
        # 吞掉所有没被专用适配器认领的链接 —— 包括 YouTube 这类视频站。
        # 视频站的链接必须交给 generic（yt-dlp 下载视频），不能当博客抽正文。
        # 所以 article_like 只在「主机确实是文章/图文站」时才优先于 generic；
        # 其余（视频站、未知站点）一律走 generic（见下方决策）。
        specific, article_like, generic = [], [], None
        from ..platforms.article import ArticleAdapter, PlainTextAdapter
        _ARTICLE_KEYS = (ArticleAdapter, PlainTextAdapter)

        for key, cls in all_platforms().items():
            if not issubclass(cls, PlatformAdapter):
                continue
            if cls is GenericYtdlpAdapter:
                generic = cls
                continue
            try:
                matched = cls.match(url)
            except Exception:
                continue
            if not matched:
                continue
            if cls in _ARTICLE_KEYS:
                article_like.append(cls)
            else:
                specific.append(cls)

        # 视频平台：域名越具体越优先
        specific.sort(key=lambda c: -max((len(d) for d in c.domains), default=0))
        if specific:
            return specific[0](self.ctx)

        if article_like:
            # 本地路径/粘贴文本优先于网页文章 —— 它不联网，更快也更准
            order = {PlainTextAdapter: 0, ArticleAdapter: 1}
            article_like.sort(key=lambda c: order.get(c, 9))

            # 纯文本 / 本地文档（含拖拽进来的 PDF、Word）**直接返回**，
            # 不走下面的主机判断 —— 本地路径压根没有主机名（netloc 为空），
            # 拿空主机去比对文章站清单必然为 False，会被错误地判成
            # 「视频站」而转给 generic（yt-dlp）去下载，拖进来的 PDF 就永远解析不了。
            if article_like[0] is PlainTextAdapter:
                return article_like[0](self.ctx)

            # 主机是文章/图文站 → ArticleAdapter 抽正文；
            # 否则（视频站、未知站点）→ generic 下载视频。这是「视频总结」的正确默认。
            host = (urlparse(url).netloc or "").lower()
            if not _is_article_host(host) and generic is not None:
                return generic(self.ctx)
            return article_like[0](self.ctx)

        return generic(self.ctx) if generic else None

    @staticmethod
    def list_platforms() -> list[dict]:
        """给界面用的平台清单（含支持等级）。"""
        from ..platforms.generic_ytdlp import GenericYtdlpAdapter
        rows = []
        for key, cls in sorted(all_platforms().items(), key=lambda kv: kv[0]):
            rows.append({
                "key": key,
                "name": getattr(cls, "name", key),
                "domains": list(getattr(cls, "domains", ())) or ["（任意链接）"],
                "support": getattr(cls, "support", "metadata"),
                "note": getattr(cls, "note", ""),
                "generic": cls is GenericYtdlpAdapter,
            })
        return rows

    # ---------------- 主流程 ----------------
    def run(self, text_or_url: str,
            on_token: Optional[Callable[[str], None]] = None,
            should_cancel: Optional[Callable[[], bool]] = None) -> SummaryResult:
        t0 = time.time()
        cfg = self.ctx.config
        ctx = self.ctx

        def cancelled() -> bool:
            return bool(should_cancel and should_cancel())

        # 1) 提取链接
        ctx.report("正在解析输入…", 5)
        url = extract_url(text_or_url.strip())
        if not url:
            raise VSumError("没有找到有效链接")
        share_title = guess_title(text_or_url)
        # 完整原始输入放到 ctx 上：有的平台标题不在链接里、在分享文案里
        # （典型：视频号「张三的作品《标题》」），适配器要从原文案抠标题。
        ctx.share_text = text_or_url

        # 2) 匹配平台
        adapter = self.detect(url)
        if adapter is None:
            raise VSumError("没有可用于该链接的解析器")
        ctx.report(f"识别为「{adapter.name}」，正在读取内容…", 10)

        # 3) 元数据
        meta = None
        last_err: Optional[Exception] = None
        try:
            meta = adapter.resolve(url)
        except Exception as exc:
            last_err = exc
            # 措辞只说「失败了」。**不要写「尝试通用解析器」** ——
            # 本地文档/纯文本根本不会去走 generic（见下面的分支），
            # 那句话会让用户以为程序在联网解析本地文件，白担心。
            self.log.warning("%s 解析失败：%s", adapter.name, exc)
        if meta is None or not meta.title:
            # 换通用解析器兜底
            from ..platforms.generic_ytdlp import GenericYtdlpAdapter
            from ..platforms.article import PlainTextAdapter
            # **本地文档 / 纯文本失败时不要回退到 yt-dlp**：它会把
            # `C:\x.pdf` 里的 `C:` 当成 URL 协议头，报一句
            # 「Unsupported url scheme: "c"」，既不可能成功，又会在日志里
            # 留下一行吓人的 WARNING，让人以为本地文件走了网络请求。
            if isinstance(adapter, PlainTextAdapter):
                pass
            elif not isinstance(adapter, GenericYtdlpAdapter):
                first_err = last_err
                try:
                    generic = GenericYtdlpAdapter(ctx)
                    meta = generic.resolve(url)
                    adapter = generic
                    last_err = None
                except Exception as exc:
                    last_err = exc
                # 专用适配器的报错通常比通用的更准（它知道该配哪个站的 Cookie），
                # 通用的一失败就把它丢了，用户只能看到笼统的「无法解析」
                if meta is None and first_err is not None:
                    from ..core.errors import FetchError
                    if isinstance(first_err, FetchError) and first_err.hint:
                        self.log.debug("通用解析器也失败，回退到专用适配器的提示")
                        last_err = first_err
        if meta is None:
            # **保留底层异常的 hint**。这里自己拼一句通用提示，
            # 会把适配器写好的针对性建议（比如「去配 douyin 的 Cookie」）
            # 覆盖成「海外站点请配置代理」—— 抖音是国内站点，这句纯误导。
            from ..core.errors import FetchError
            if isinstance(last_err, FetchError):
                raise last_err
            raise VSumError(f"无法解析该链接：{last_err}" if last_err else "无法解析该链接",
                            hint="确认链接有效且可公开访问；海外站点请在设置里配置代理")
        # 分享文案里的标题往往比页面标题更准（带「作者｜某某」这类信息），
        # 所以只在**视频平台**上做这层覆盖。
        # 文章/纯文本场景下 guess_title() 是从整段正文里猜的，会返回正文片段，
        # 拿它覆盖标题反而把「# 为什么需要分两次打包」这种真标题冲掉。
        if share_title and meta.platform not in ("article", "plain"):
            if len(share_title) > len(meta.title or ""):
                meta.title = share_title
        ctx.report(f"已获取：{meta.title[:40]}", 20)
        if cancelled():
            raise VSumError("已取消")

        # 4) 文本来源：字幕 → ASR → 元信息
        transcript = self._build_transcript(adapter, meta, cancelled, on_token)

        # 5) 摘要
        ctx.report("正在生成总结…", 85)
        md, engine = self._summarize(meta, transcript, on_token)

        result = SummaryResult(meta=meta, transcript=transcript,
                               markdown=md, engine=engine,
                               keywords=_extract_keywords(md),
                               elapsed=time.time() - t0)
        ctx.report("完成", 100)

        # 6) 可选：自动入库到 Obsidian。
        #    放在最后且**吞掉所有异常** —— 知识库是附加功能，
        #    入库失败绝不能影响总结结果的交付。
        if bool(cfg.get("obsidian.enabled", False)):
            try:
                from ..obsidian import save as vault_save

                info = vault_save(self.ctx, result)
                result.obsidian = info
                if info.get("ok") and not info.get("skipped"):
                    self.log.info("已入库 Obsidian: %s", info.get("path"))
                elif info.get("ok"):
                    self.log.info("跳过入库: %s", info.get("message"))
            except Exception as exc:
                self.log.warning("Obsidian 入库失败（不影响总结结果）: %s", exc)
                result.obsidian = {"ok": False, "message": f"入库失败：{exc}"}

        return result

    # ---------------- 文本来源 ----------------
    def _build_transcript(self, adapter, meta, cancelled,
                          on_token=None) -> Transcript:
        cfg = self.ctx.config
        mode = cfg.get("asr.engine", "auto")
        ctx = self.ctx

        # 4.0 文章 / 纯文本：**无条件**先取正文，不看 asr.engine。
        #    「文本来源」那个设置是给视频选的（字幕 / 本地识别 / 云端识别），
        #    对文章毫无意义 —— 而且用户选了 cloud 就会走进 4.2 去下音频，
        #    那是纯浪费。放在最前面当成一个独立分支处理，最省事也最不容易错。
        #    视频号也归这里：它根本没有可下载的音频（链接只能在微信内打开），
        #    走 4.2 只会白白等 yt-dlp 报错。
        if meta.platform in ("article", "plain", "weixin_channels"):
            ctx.report("正在抽取正文…", 30)
            tr = None
            try:
                tr = adapter.fetch_subtitle(meta)
            except Exception as exc:
                self.log.warning("正文抽取异常: %s", exc)
            if tr and tr.text:
                ctx.report(f"拿到正文（{tr.char_count} 字）", 45)
                return tr
            ctx.report("未能抽到正文，改用标题/简介做元信息总结", 60)
            return adapter.meta_only_transcript(
                meta, "正文抽取失败或内容过短（少于 180 字符）。")

        # 4.1 字幕
        tr = None
        if mode in ("auto", "subtitle"):
            ctx.report("正在尝试获取字幕/正文…", 30)
            try:
                tr = adapter.fetch_subtitle(meta)
            except Exception as exc:
                self.log.warning("字幕/正文获取异常: %s", exc)
                tr = None
            if tr and tr.text and tr.source == "subtitle":
                ctx.report(f"拿到字幕（{tr.engine}，{tr.char_count} 字）", 45)
                return tr
            if mode == "subtitle":
                ctx.report("该视频没有可用字幕，改用元信息总结", 45)
                return adapter.meta_only_transcript(
                    meta, "当前设置为「仅字幕」模式，未获取到字幕。")

        # 4.2 下载音频 + ASR
        note = ""
        audio = None
        try:
            ctx.report("未拿到字幕，正在下载音频…", 40)
            audio = self._download_audio(adapter, meta)
        except Exception as exc:
            self.log.warning("音频获取失败: %s", exc)
            note = f"音频获取失败：{exc}"

        if audio and os.path.isfile(audio):
            if cancelled():
                raise VSumError("已取消")
            tr, asr_err = self._run_asr(audio, mode)
            if tr and tr.text:
                if not cfg.get("media.keep_audio", False):
                    try:
                        os.remove(audio)
                    except OSError:
                        pass
                return tr
            # 识别失败时**保留音频文件**：重试不必重新下载几百 MB 的视频。
            kept = ""
            if os.path.isfile(audio):
                kept = f"\n音频已保留在 {audio}，直接重试即可，无需重新下载视频。"
            note = (asr_err or "语音识别未返回有效文本") + kept

        # 4.3 兜底：元信息
        ctx.report("无法获取视频内容，改用标题/简介做元信息总结", 60)
        return adapter.meta_only_transcript(meta, note)

    #: 这些平台走「浏览器兜底」。抖音/快手/小红书的凭证由浏览器 JS 动态
    #: 生成（s_v_web_id 服务器根本不下发），HTTP 请求伪造不出来——实测把
    #: Cookie 里 verify_ 去掉、删掉该字段、只留三个关键字段，yt-dlp 三种
    #: 改法全都失败。只有启动真实浏览器才拿得到视频流。
    #: B站/YouTube 不在此列 —— 它们的字幕路径是秒级的，不该为个别平台拖累。
    BROWSER_FALLBACK = ("douyin", "kuaishou", "xiaohongshu")

    def _download_audio(self, adapter, meta) -> Optional[str]:
        from ..media.ytdlp import download_audio, extract_info

        ctx = self.ctx
        max_min = float(ctx.config.get("asr.max_audio_minutes", 90) or 90)
        out = ctx.path(f"{meta.platform}_{meta.video_id or 'audio'}.wav")

        # 平台自带直链优先
        src = None
        try:
            src = adapter.fetch_audio_source(meta)
        except Exception:
            src = None
        if src:
            from ..core.ffmpeg import extract_audio
            try:
                got = extract_audio(ctx.ffmpeg(), src, out, max_min)
                if got:
                    return got
            except Exception as exc:
                self.log.warning("平台直链抽音频失败: %s", exc)

        info = extract_info(meta.url, ctx)
        if info:
            try:
                got = download_audio(info, ctx, out, max_min)
                # **download_audio 失败时是返回 None 而不是抛异常**——
                # 只写 `return download_audio(...)` 的话，失败时直接返回 None，
                # 后面的浏览器兜底永远不会执行。早先就这么漏掉的。
                if got:
                    return got
                self.log.info("yt-dlp 下载音频返回空，继续尝试浏览器兜底")
            except Exception as exc:
                self.log.warning("yt-dlp 下载音频失败: %s", exc)

        # 走到这里说明 HTTP 路线彻底不行 —— 若是强风控平台，改用真实浏览器。
        if meta.platform in self.BROWSER_FALLBACK:
            return self._download_via_browser(meta, out, max_min)
        return None

    def _download_via_browser(self, meta, out_wav: str, max_min: float) -> Optional[str]:
        """浏览器兜底：真实浏览器抓视频流 → ffmpeg 抽音频 → 交给 ASR。"""
        ctx = self.ctx
        try:
            from ..media import browser_fetch
            from ..media.browser_fetch import BrowserFetchError
        except ImportError:
            self.log.warning("browser_fetch 模块不可用")
            return None

        if not ctx.config.get("asr.browser_fallback", True):
            self.log.info("已关闭浏览器兜底（asr.browser_fallback=false）")
            return None

        def say(msg: str) -> None:
            ctx.report(msg, 40)

        say("该平台风控较严，正在用浏览器抓取视频…")
        mp4 = None
        real_title = ""
        try:
            mp4, real_title = browser_fetch.fetch(
                meta.url,
                out_dir=ctx.path(""),
                filename=f"browser_{meta.platform}_{meta.video_id or 'v'}.mp4",
                progress=say)
        except BrowserFetchError as exc:
            ctx.report(str(exc), 42)
            self.log.warning("浏览器兜底失败: %s", exc)
            if getattr(exc, "hint", ""):
                meta.extra = dict(meta.extra or {})
                meta.extra["browser_hint"] = exc.hint
            return None
        except Exception as exc:
            self.log.warning("浏览器兜底异常: %s", exc)
            return None

        if not mp4 or not os.path.isfile(mp4):
            return None
        # 浏览器拿到了真实标题 —— 替换占位标题，摘要里能显示视频名
        if real_title and meta.title.startswith("抖音视频（标题待"):
            meta.title = real_title
        try:
            from ..core.ffmpeg import extract_audio
            wav = extract_audio(ctx.ffmpeg(), mp4, out_wav, max_min)
            if not ctx.config.get("media.keep_audio", False):
                try:
                    os.remove(mp4)
                except OSError:
                    pass
            return wav
        except Exception as exc:
            self.log.warning("浏览器抓到的视频抽音频失败: %s", exc)
            return None

    def _run_asr(self, audio_path: str, mode: str) -> tuple[Optional[Transcript], str]:
        """按引擎顺序识别。返回 ``(结果, 最后一次失败的human可读原因)``。

        **空文本也算失败**：auto 模式下引擎 A 返回空时必须继续试引擎 B，
        不能拿到空 Transcript 就返回 —— 旧实现里 `return engine.transcribe(...)`
        会把空结果直接当成功交回去，另一个引擎永远没机会上场。
        """
        from ..asr.base import AsrEngine
        from ..core.registry import all_asr

        ctx = self.ctx
        lang = ctx.config.get("asr.language", "zh")

        order = []
        if mode == "faster-whisper":
            order = ["faster-whisper"]
        elif mode == "cloud":
            order = ["cloud"]
        else:                       # auto
            order = ["faster-whisper", "cloud"]

        last_err = f"没有可用的语音识别引擎（模式：{mode}）"
        for key in order:
            cls = all_asr().get(key)
            if not cls:
                continue
            engine: AsrEngine = cls(ctx)
            ok, reason = engine.available()
            if not ok:
                self.log.info("ASR 引擎 %s 不可用：%s", key, reason)
                last_err = f"{engine.name} 不可用：{reason}"
                continue
            try:
                ctx.report(f"正在用 {engine.name} 识别语音…", 50)
                tr = engine.transcribe(
                    audio_path, language=lang,
                    on_progress=lambda msg, p: ctx.report(msg, int(50 + p * 30)))
            except Exception as exc:
                self.log.warning("ASR %s 失败: %s", key, exc)
                self.log.debug(traceback.format_exc())
                last_err = f"{engine.name} 失败：{exc}"
                continue
            if tr and (tr.text or "").strip():
                return tr, ""
            last_err = f"{engine.name} 返回了空文本（可能音频里没有人声）"
            self.log.warning("ASR %s 返回空文本，尝试下一个引擎", key)
        return None, last_err

    # ---------------- 摘要 ----------------
    def _summarize(self, meta, transcript: Transcript, on_token=None) -> tuple[str, str]:
        from ..core.registry import all_llm

        ctx = self.ctx
        for key in ("openai-compat", "extractive"):
            cls = all_llm().get(key)
            if not cls:
                continue
            engine = cls(ctx)
            ok, reason = engine.available()
            if not ok:
                if key == "openai-compat":
                    ctx.report(f"未启用大模型（{reason}），改用本地抽取式摘要", 88)
                continue
            try:
                md = engine.summarize(meta, transcript, on_token=on_token)
                if md and md.strip():
                    return md, engine.name
            except Exception as exc:
                self.log.warning("摘要引擎 %s 失败: %s", key, exc)
                if key == "openai-compat":
                    ctx.report(f"大模型调用失败（{exc}），改用本地抽取式摘要", 90)
        return "# 内容总结\n\n（摘要失败，以下为原始文本前 2000 字）\n\n" + \
               (transcript.text[:2000] or "（无内容）"), "fallback"


def _extract_keywords(markdown: str) -> list[str]:
    """从摘要的「关键词」小节里把标签抠出来（界面顶部展示用）。"""
    import re
    m = re.search(r"##\s*关键词\s*\n(.+)", markdown or "")
    if not m:
        return []
    line = m.group(1).strip().lstrip("-").strip()
    parts = re.split(r"[/、,，|]\s*", line)
    return [p.strip(" `#*") for p in parts if p.strip(" `#*")][:10]
