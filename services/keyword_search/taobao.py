# -*- coding: utf-8 -*-
"""淘宝关键词搜索 service。"""

import asyncio
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from .base import KeywordSearchService, _agent_act


_EXTRACT_PRODUCT_LINKS_JS = """
() => {
    const seen = new Set();
    const links = [];

    function normalizeHref(href) {
        try {
            const url = new URL(href, window.location.href);
            url.hash = '';
            return url.href;
        } catch (e) {
            return '';
        }
    }

    function isProductUrl(url) {
        try {
            const u = new URL(url);
            const host = u.hostname;
            const path = u.pathname;
            const isTaobao = host.endsWith('taobao.com') || host.endsWith('tmall.com') || host.endsWith('tmall.hk');
            if (!isTaobao) return false;
            return path.includes('item.htm') || path.includes('item_o.htm') || host.startsWith('detail.');
        } catch (e) {
            return false;
        }
    }

    function extractPrice(el) {
        const priceEl = el.querySelector('[class*="price"], [class*="Price"]');
        if (!priceEl) return '';
        const m = priceEl.textContent.match(/[\\d,.]+/);
        return m ? m[0] : '';
    }

    function extractSales(el) {
        const text = el.textContent || '';
        const m = text.match(/(\\d[\\d,]*\\+?)\\s*(人付款|人收货|人付款|月销|已售)/);
        return m ? m[1] + m[2] : '';
    }

    function extractShop(el) {
        const shopEl = el.querySelector('[class*="shop"], [class*="Shop"], [class*="store"], [class*="Store"]');
        return shopEl ? shopEl.textContent.trim().slice(0, 60) : '';
    }

    function extractTitle(el) {
        const titleEl = el.querySelector('[class*="title"], [class*="Title"]');
        return titleEl ? titleEl.textContent.trim().slice(0, 120) : '';
    }

    function extractImage(el) {
        const imgWrap = el.querySelector('[class*="img"], [class*="Img"], [class*="pic"], [class*="Pic"], [class*="mainPic"], [class*="MainPic"]');
        if (imgWrap) {
            const img = imgWrap.querySelector('img');
            if (img) {
                const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
                if (src && !src.includes('spacer') && !src.includes('1x1')) return src.startsWith('//') ? 'https:' + src : src;
            }
        }
        for (const img of el.querySelectorAll('img')) {
            const w = parseInt(img.getAttribute('width') || img.naturalWidth || '0');
            const h = parseInt(img.getAttribute('height') || img.naturalHeight || '0');
            if (w > 0 && w < 50) continue;
            if (h > 0 && h < 50) continue;
            const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
            if (src && !src.includes('spacer') && !src.includes('1x1') && !src.includes('tps-')) return src.startsWith('//') ? 'https:' + src : src;
        }
        return '';
    }

    for (const a of document.querySelectorAll('a[href]')) {
        const url = normalizeHref(a.getAttribute('href'));
        if (!url || seen.has(url) || !isProductUrl(url)) continue;
        seen.add(url);

        const card = a.closest('[class*="Card"], [class*="card"], [class*="Content"], [class*="item"]');
        links.push({
            url,
            title: extractTitle(card || a) || (a.textContent || '').trim().slice(0, 80),
            price: extractPrice(card || a),
            sales: extractSales(card || a),
            shop: extractShop(card || a),
            image: extractImage(card || a),
            card_text: card ? (card.textContent || '').trim().slice(0, 200) : '',
        });
    }

    return {
        url: window.location.href,
        title: document.title,
        links,
        link_count: links.length,
        scroll_y: window.scrollY,
        scroll_height: document.documentElement.scrollHeight || document.body.scrollHeight,
    };
}
"""


_CLICK_NEXT_PAGE_JS = """
() => {
    const candidates = Array.from(document.querySelectorAll('a, button, span, div')).filter((el) => {
        const text = (el.textContent || '').trim();
        const aria = el.getAttribute('aria-label') || '';
        return text === '下一页' || text.includes('下一页') || aria.includes('下一页');
    });

    for (const raw of candidates) {
        const el = raw.closest('a, button') || raw;
        const cls = el.className ? String(el.className) : '';
        const disabled = el.getAttribute('disabled') !== null ||
            el.getAttribute('aria-disabled') === 'true' ||
            cls.includes('disabled') ||
            cls.includes('Disabled');
        if (disabled) continue;
        if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' });
        if (typeof el.click === 'function') {
            el.click();
        } else {
            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        }
        return true;
    }
    return false;
}
"""


class TaobaoKeywordSearchService(KeywordSearchService):
    platform = "taobao"

    def build_search_url(self, keyword: str) -> str:
        return f"https://s.taobao.com/search?q={quote(keyword)}&sort=sale-desc&tab=pc_taobao"

    def canonicalize_url(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.netloc:
            return ""
        query = parse_qs(parsed.query)
        item_id = (query.get("id") or query.get("item_id") or [""])[0]
        if not item_id:
            return ""
        host = parsed.netloc.lower()
        if "tmall" in host:
            canonical_host = "detail.tmall.com"
        else:
            canonical_host = "item.taobao.com"
        return urlunparse(("https", canonical_host, "/item.htm", "", urlencode({"id": item_id}), ""))

    async def get_page(self, browser_context, fallback_url: str):
        await asyncio.sleep(1)
        for page in browser_context.pages:
            try:
                if page.is_closed():
                    continue
                if "taobao.com" in page.url or "tmall.com" in page.url:
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
                    const links = document.querySelectorAll('a[href*="item.htm"], a[href*="item_o.htm"], a[href*="detail.tmall"]');
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
        await _agent_act(agent, "点击销量按钮按照销量排序。")
