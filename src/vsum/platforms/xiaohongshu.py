"""小红书适配器。

小红书的笔记详情页强依赖登录态与 ``x-s`` 签名，本适配器只做**无侵入**解析：
短链还原 + HTML meta + 页面内 JSON。需要完整正文时请填 Cookie。
"""

from __future__ import annotations

import re

from ..core.registry import register_platform
from .share_page import SharePageAdapter


@register_platform("xiaohongshu")
class XiaohongshuAdapter(SharePageAdapter):
    """小红书。链接形如 ``https://www.xiaohongshu.com/explore/<24位ID>``。"""

    key = "xiaohongshu"
    name = "小红书"
    domains = ("xiaohongshu.com", "xhslink.com", "xiaohongshu.cn")
    id_pattern = re.compile(r"(?:/explore/|/discovery/item/|/search_result/)([0-9a-fA-F]{16,32})")
    note = "图文/视频笔记均可解析标题；正文与视频通常需 Cookie。"
