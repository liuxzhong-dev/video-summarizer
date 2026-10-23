"""快手适配器（含快手极速版）。"""

from __future__ import annotations

import re

from ..core.registry import register_platform
from .share_page import SharePageAdapter


@register_platform("kuaishou")
class KuaishouAdapter(SharePageAdapter):
    """快手。分享短链形如 ``https://v.kuaishou.com/xxxxx``。"""

    key = "kuaishou"
    name = "快手"
    domains = ("kuaishou.com", "chenzhongtech.com", "gifshow.com", "kuaishou.cn")
    id_pattern = re.compile(
        r"(?:/short-video/|/f/|/u/[^/]+/|photoId=|photo_id=|vid=)([0-9A-Za-z_-]{8,32})")
    note = ("无 Cookie 时一般只能拿到标题；填入 kuaishou.com 的 Cookie "
            "（含 did、kuaishou.server.web_st）后可显著提升成功率。")
