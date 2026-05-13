# -*- coding: utf-8 -*-
"""天猫关键词搜索 service — 复用淘宝搜索页面，tab=mall 筛选天猫商品。"""

from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from .taobao import TaobaoKeywordSearchService


class TmallKeywordSearchService(TaobaoKeywordSearchService):
    platform = "tmall"

    def build_search_url(self, keyword: str) -> str:
        return f"https://s.taobao.com/search?q={quote(keyword)}&sort=sale-desc&tab=mall"

    def canonicalize_url(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.netloc:
            return ""
        host = parsed.netloc.lower()
        # 搜索结果在 taobao.com 域名下，只保留 tmall.com 商品链接
        if "tmall" not in host:
            return ""
        query = parse_qs(parsed.query)
        item_id = (query.get("id") or query.get("item_id") or [""])[0]
        if not item_id:
            return url
        return urlunparse(("https", "detail.tmall.com", "/item.htm", "", urlencode({"id": item_id}), ""))
