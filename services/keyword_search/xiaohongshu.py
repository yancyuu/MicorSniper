# -*- coding: utf-8 -*-
"""小红书关键词搜索 service。"""

import asyncio
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .base import KeywordSearchService, _agent_act
from config.crawl_profile import get_profile

_P = get_profile("xiaohongshu")


_XHS_EXTRACT_JS = """
() => {
    const links = [];

    // 小红书商品卡片结构：.note-card .cover[nail] 或 shop-card
    // 提取商品标题、链接、店铺名、价格
    function extractTitle(el) {
        const titleEl = el.querySelector('[class*="title"], [class*="name"], .title, .name');
        if (titleEl) return titleEl.textContent.trim().slice(0, 120);
        // fallback: 取 alt 属性或 aria-label
        const img = el.querySelector('img[alt]');
        if (img) return img.getAttribute('alt').trim().slice(0, 120);
        return el.textContent.trim().slice(0, 80);
    }

    function extractPrice(el) {
        const priceEl = el.querySelector('[class*="price"], [class*="Price"]');
        if (priceEl) {
            const text = priceEl.textContent || '';
            const m = text.match(/[\\d,.]+/);
            if (m) return m[0];
        }
        const text = el.textContent || '';
        const m = text.match(/¥\\s*([\\d,.]+)/);
        return m ? m[1] : '';
    }

    function extractShop(el) {
        const shopEl = el.querySelector('[class*="shop"], [class*="author"], [class*="user"]');
        if (shopEl) return shopEl.textContent.trim().slice(0, 60);
        const authorEl = el.querySelector('[class*="authorName"], [class*="nickname"]');
        if (authorEl) return authorEl.textContent.trim().slice(0, 60);
        return '';
    }

    function extractImage(el) {
        const img = el.querySelector('img[src], img[data-src]');
        if (img) {
            const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
            if (src && !src.includes('spacer') && !src.includes('loading')) {
                if (src.startsWith('//')) return 'https:' + src;
                return src;
            }
        }
        const nailImg = el.querySelector('img[nail]');
        if (nailImg) {
            const src = nailImg.getAttribute('nail') || '';
            if (src.startsWith('//')) return 'https:' + src;
            return src;
        }
        return '';
    }

    function extractUrl(el) {
        // 商品卡片链接在 <a href="/discovery/item/xxx"> 或父级
        const linkEl = el.querySelector('a[href*="/discovery/item/"]');
        if (linkEl) return linkEl.getAttribute('href');
        // 查找包含 item 的链接
        const allLinks = el.querySelectorAll('a[href]');
        for (const a of allLinks) {
            const href = a.getAttribute('href') || '';
            if (href.includes('/discovery/item/') || href.includes('/goods/')) {
                return href;
            }
        }
        return '';
    }

    // 方案1: .note-card 商品卡片
    for (const el of document.querySelectorAll('.note-card, .search-card, [class*="commodity"], [class*="product"]')) {
        const href = extractUrl(el);
        if (!href) continue;
        const title = extractTitle(el);
        const price = extractPrice(el);
        const shop = extractShop(el);
        const image = extractImage(el);
        links.push({
            url: href.startsWith('/') ? 'https://www.xiaohongshu.com' + href : href,
            title: title,
            price: price,
            shop: shop,
            image: image,
        });
    }

    // 方案2: 通用搜索结果容器内的商品链接
    if (links.length === 0) {
        for (const el of document.querySelectorAll('[class*="item"], [class*="goods"]')) {
            const href = extractUrl(el);
            if (!href) continue;
            const title = extractTitle(el);
            const price = extractPrice(el);
            const shop = extractShop(el);
            const image = extractImage(el);
            if (title || price) {
                links.push({
                    url: href.startsWith('/') ? 'https://www.xiaohongshu.com' + href : href,
                    title: title,
                    price: price,
                    shop: shop,
                    image: image,
                });
            }
        }
    }

    return { links };
}
"""


class XiaohongshuKeywordSearchService(KeywordSearchService):
    platform = "xiaohongshu"

    def build_search_url(self, keyword: str) -> str:
        return f"https://www.xiaohongshu.com/search_result?keyword={quote(keyword)}&source=web_explore_feed"

    def canonicalize_url(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.netloc and not parsed.path:
            return ""
        # 小红书商品链接: /discovery/item/xxx 或 /goods/xxx
        path = parsed.path
        m = re.search(r"/(discovery/item|goods)/([a-zA-Z0-9]+)", path)
        if m:
            return f"https://www.xiaohongshu.com/discovery/item/{m.group(2)}"
        # 已有完整 URL 直接返回
        if parsed.netloc:
            return url
        return ""

    async def get_page(self, browser_context, fallback_url: str):
        await asyncio.sleep(1)
        try:
            for page in browser_context.pages:
                try:
                    if page.is_closed():
                        continue
                    if "xiaohongshu.com" in page.url:
                        return page
                except Exception:
                    continue
        except Exception:
            pass
        try:
            page = await browser_context.new_page()
            await page.goto(fallback_url, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            return page
        except Exception as e:
            raise

    async def wait_page_ready(self, page) -> None:
        try:
            await page.wait_for_function(
                """() => {
                    const body = document.body ? document.body.innerText : '';
                    const hasContent = document.querySelectorAll('.note-card, .search-card, [class*="commodity"], [class*="product"], [class*="item"]').length > 0;
                    return hasContent || body.includes('没有找到') || body.includes('登录');
                }""",
                timeout=20000,
            )
        except Exception:
            pass

    async def scroll_and_extract(self, agent, page, browser_context=None, fallback_url="") -> list[dict[str, Any]]:
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight || document.body.scrollHeight)")
            await asyncio.sleep(_P["search_scroll_delay"])

        state = await page.evaluate(_XHS_EXTRACT_JS)
        seen: set[str] = set()
        links: list[dict[str, Any]] = []
        for item in state.get("links", []):
            url = item.get("url", "")
            if url and url not in seen:
                seen.add(url)
                links.append(item)
        return links

    async def go_next_page(self, agent, page) -> bool:
        try:
            # 小红书分页：查找下一页按钮
            await page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll('a, span, div, button'));
                const next = els.find(el => (el.textContent || '').trim() === '下一页' || (el.getAttribute('aria-label') || '').includes('下一页'));
                if (next) { next.scrollIntoView({block: 'center'}); next.click(); return true; }
                return false;
            }
            """)
            await asyncio.sleep(_P["search_next_page_delay"])
            return True
        except Exception:
            pass
        try:
            await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            await asyncio.sleep(_P["search_fallback_scroll_delay"])
        except Exception:
            pass
        return False

    async def after_navigate(self, agent) -> None:
        await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗。")