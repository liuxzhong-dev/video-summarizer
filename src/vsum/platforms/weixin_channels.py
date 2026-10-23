"""微信视频号适配器。

视频号没有任何公开可用的无鉴权接口（分享链接只能在微信内打开），
因此这里只能做到「标题级」解析：**从分享文案里抠标题和作者**。

为什么绝不能编占位标题
--------------------
2026-10-09 用户实测翻车：拿不到信息时返回占位标题「微信视频号视频」，
大模型对着这七个字编出一篇看似完整、实则零信息的「总结」——
比直接报错更有害（用户以为程序正常工作，还浪费了 API token）。
所以现在的行为是：

* 分享文案里有标题 → 用标题 + 作者出一份元信息摘要（至少切题）；
* 什么都没有 → 明确报错，并告诉用户两条可执行的路
  （连分享文案一起粘贴 / 直接粘正文走「纯文本总结」）。
"""

from __future__ import annotations

import re
from typing import Tuple

from ..core.errors import FetchError
from ..core.models import Transcript, VideoMeta
from ..core.registry import register_platform
from .share_page import SharePageAdapter

#: 视频号分享文案（微信里长按转发消息复制）的几种真实形态：
#:
#:     李永乐老师的作品《三分钟看懂傅里叶变换》
#:     https://channels.weixin.qq.com/eid/xxxx
#:
#: 也可能是 【标题】、#标题#、或「的作品：标题」（无书名号）。
_TITLE_PATTERNS = (
    # 「xxx的作品《标题》」/「xxx的作品【标题】」—— 最常见，优先
    re.compile(r"的作品\s*[《【]([^《》【】]{2,60})[》】]"),
    # 裸书名号标题
    re.compile(r"[《]([^《》]{2,60})[》]"),
    # 话题式（老版分享口令）
    re.compile(r"#([^#\n]{4,60})#"),
    # 「xxx的作品：标题」（无书名号，冒号后到行尾）
    re.compile(r"的作品[:：]\s*([^\n#《》【】]{2,60})"),
)
#: 「张三的作品」→ 抠出作者名（限制在无空白的一段，防止吞半句话）
_AUTHOR_RE = re.compile(r"([^\s，。,；;#《》【】]{1,30})的作品")


@register_platform("weixin_channels")
class WeixinChannelsAdapter(SharePageAdapter):
    """微信视频号。"""

    key = "weixin_channels"
    name = "微信视频号"
    domains = ("channels.weixin.qq.com", "weixin.qq.com", "finder.video.qq.com")
    id_pattern = re.compile(r"(?:eid=|/feed\?[^#]*?id=)([A-Za-z0-9_%\-]{8,64})")
    note = "视频号链接只能在微信内打开；请连分享文案（含《标题》）一起粘贴。"

    def resolve(self, url: str) -> VideoMeta:
        # 视频号页面在微信外打不开，不做网络请求，避免长时间等待
        vid = self.extract_id(url) or ""
        title, author = self._parse_share()
        if not title:
            # 【不能编占位标题】编一个「微信视频号视频」出去，大模型会
            # 对着这七个字正儿八经写一篇零信息的"总结"，浪费 token 还误导
            # 用户以为程序正常工作。明确报错 + 给出可执行的下一步。
            raise FetchError(
                "视频号链接只能拿到链接本身，抓不到标题和内容",
                hint="视频号的分享链接只能在微信里打开，程序无法直接读取视频。\n"
                     "        【办法一】把**完整的分享文案**连链接一起粘贴进来\n"
                     "        （微信里长按转发消息 → 复制，通常形如：\n"
                     "        「张三的作品《视频标题》」），程序会用标题生成摘要；\n"
                     "        【办法二・效果最好】把视频里的文字内容直接粘进输入框，\n"
                     "        用「纯文本总结」，能得到真正的正文总结。")
        return VideoMeta(
            platform=self.key, platform_name=self.name, video_id=vid,
            url=url, title=title, author=author,
            extra={"via": "share-text"},
        )

    def fetch_subtitle(self, meta: VideoMeta) -> Transcript | None:
        if not (meta.title or "").strip():
            return None
        head = f"标题：{meta.title}"
        if meta.author:
            head += f"\n作者：{meta.author}"
        return Transcript(
            source="metadata",
            text=head,
            engine="weixin-share", language="zh",
            notes=["视频号无公开解析接口，内容仅来自分享文案里的标题，"
                   "拿不到视频正文或语音。"])

    # ---------------- 内部 ----------------
    def _parse_share(self) -> Tuple[str, str]:
        """从完整分享文案里抠 ``(标题, 作者)``。

        resolve 只能拿到 URL，但标题在**分享文案**里而不是链接里 ——
        所以流水线在 resolve 之前会把用户粘贴的原始文本放进
        ``ctx.share_text``，这里从那里取。
        """
        text = (getattr(self.ctx, "share_text", "") or "").strip()
        if not text:
            return "", ""
        title = ""
        for pat in _TITLE_PATTERNS:
            m = pat.search(text)
            if m:
                title = m.group(1).strip()
                break
        author = ""
        m = _AUTHOR_RE.search(text)
        if m:
            author = m.group(1).strip()
        return title, author
