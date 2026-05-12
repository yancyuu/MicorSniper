# -*- coding: utf-8 -*-
"""天猫关键词搜索 service。"""

import asyncio
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from .base import KeywordSearchService, _agent_act
from .taobao import _EXTRACT_PRODUCT_LINKS_JS, _CLICK_NEXT_PAGE_JS


class TmallKeywordSearchService(KeywordSearchService):
    platform = "tmall"

    def build_search_url(self, keyword: str) -> str:
        return f"https://list.tmall.com/search_product.htm?q={quote(keyword)}&sort=sale-desc"

    def canonicalize_url(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.netloc:
            return ""
        host = parsed.netloc.lower()
        if "tmall" not in host:
            return ""
        query = parse_qs(parsed.query)
        item_id = (query.get("id") or query.get("item_id") or [""])[0]
        if not item_id:
            return url
        return urlunparse(("https", "detail.tmall.com", "/item.htm", "", urlencode({"id": item_id}), ""))

    async def get_page(self, browser_context, fallback_url: str):
        await asyncio.sleep(1)
        for page in browser_context.pages:
            try:
                if page.is_closed():
                    continue
                if "tmall.com" in page.url or "tmall.hk" in page.url or "taobao.com" in page.url:
                    return page
            except Exception:
                continue
        page = await browser_context.new_page()
        await page.goto(fallback_url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        return page

    async def wait_page_ready(self, page) -> None:
        try:
            await page.wait_for_function(
                """() => {
                    const links = document.querySelectorAll('a[href*="item.htm"], a[href*="detail.tmall"]');
                    const body = document.body ? document.body.innerText : '';
                    return links.length > 0 || body.includes('没有找到') || body.includes('登录');
                }""",
                timeout=15000,
            )
        except Exception:
            pass

    async def scroll_and_extract(self, agent, page, browser_context=None, fallback_url="") -> list[dict[str, Any]]:
        await _agent_act(agent, "将页面直接滚动到最底部")
        await asyncio.sleep(3)

        if browser_context:
            fresh = await self.get_page(browser_context, fallback_url)
            if fresh:
                page = fresh

        state = await page.evaluate(_EXTRACT_PRODUCT_LINKS_JS)
        seen: set[str] = set()
        links: list[dict[str, Any]] = []
        for item in state.get("links", []):
            url = item.get("url")
            if url and url not in seen:
                seen.add(url)
                links.append(item)
        return links

    async def go_next_page(self, agent, page) -> bool:
        try:
            if await _agent_act(agent, "点击搜索结果列表底部的下一页按钮，进入下一页"):
                return True
        except Exception:
            pass
        try:
            return bool(await page.evaluate(_CLICK_NEXT_PAGE_JS))
        except Exception:
            return False

    async def after_navigate(self, agent) -> None:
        await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗。")
        await _agent_act(agent, "点击销量进行销量排序。")
