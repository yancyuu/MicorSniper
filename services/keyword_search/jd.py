# -*- coding: utf-8 -*-
"""京东关键词搜索 service。"""

import asyncio
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .base import KeywordSearchService, _agent_act


_JD_EXTRACT_JS = """
({skus}) => {
    const skip = new Set(skus);
    const seen = new Set();
    const links = [];

    function extractPrice(el) {
        const priceContainer = el.querySelector('[class*="_price_"]');
        if (priceContainer) {
            const spans = priceContainer.querySelectorAll('span');
            for (const sp of spans) {
                const m = sp.textContent.match(/[\\d,.]+/);
                if (m) return m[0];
            }
        }
        const text = el.textContent || '';
        const m = text.match(/¥\\s*([\\d,.]+)/);
        return m ? m[1] : '';
    }

    function extractSales(el) {
        const text = el.textContent || '';
        const m = text.match(/(\\d[\\d,.]*万?\\+?)\\s*(人浏览|人关注|条评价|个评价|万\\+?评价)/);
        return m ? m[1] + m[2] : '';
    }

    function extractShop(el) {
        const shopEl = el.querySelector('[class*="_shopName"], [class*="_storeName"], [class*="shopName"]');
        if (shopEl) return shopEl.textContent.trim().slice(0, 60);
        const selfTag = el.querySelector('img[alt="自营"], [class*="_tag_"] img');
        if (selfTag) return '京东自营';
        return '';
    }

    function extractTitle(el) {
        const titleEl = el.querySelector('[class*="_goods_title"], [class*="_title_"] span[title], span[title]');
        if (titleEl) return (titleEl.getAttribute('title') || titleEl.textContent).trim().slice(0, 120);
        const titleEl2 = el.querySelector('[class*="_newStyle_"]');
        return titleEl2 ? titleEl2.textContent.trim().slice(0, 120) : '';
    }

    function extractImage(el) {
        const img = el.querySelector('img[data-src]');
        if (img) {
            const src = img.getAttribute('data-src') || '';
            if (src && !src.includes('spacer') && !src.includes('loading')) return src.startsWith('//') ? 'https:' + src : src;
        }
        for (const img of el.querySelectorAll('img')) {
            const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
            if (src.includes('360buyimg.com/n2/s') || src.includes('360buyimg.com/n1')) return src.startsWith('//') ? 'https:' + src : src;
        }
        return '';
    }

    // 新版：div[data-sku]
    for (const el of document.querySelectorAll('div[data-sku]')) {
        const sku = el.getAttribute('data-sku');
        if (!sku || seen.has(sku) || skip.has(sku)) continue;
        if (el.closest('[class*="recommend"],[class*="Recommend"],[class*="ad"],[class*="banner"]')) continue;
        seen.add(sku);
        links.push({
            url: 'https://item.jd.com/' + sku + '.html',
            title: extractTitle(el) || el.textContent.trim().slice(0, 80),
            price: extractPrice(el),
            sales: extractSales(el),
            shop: extractShop(el),
            image: extractImage(el),
        });
    }

    // 兼容旧版：li.gl-item
    const mainList = document.querySelector('#J_goodsList');
    if (mainList) {
        for (const el of mainList.querySelectorAll('li.gl-item[data-sku]')) {
            const sku = el.getAttribute('data-sku');
            if (!sku || seen.has(sku) || skip.has(sku)) continue;
            seen.add(sku);
            links.push({
                url: 'https://item.jd.com/' + sku + '.html',
                title: extractTitle(el) || el.textContent.trim().slice(0, 80),
                price: extractPrice(el),
                sales: extractSales(el),
                shop: extractShop(el),
                image: extractImage(el),
            });
        }
    }

    return { links };
}
"""


class JdKeywordSearchService(KeywordSearchService):
    platform = "jd"

    def build_search_url(self, keyword: str) -> str:
        return f"https://search.jd.com/Search?keyword={quote(keyword)}"

    def canonicalize_url(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.netloc:
            return ""
        host = parsed.netloc.lower()
        if "jd.com" not in host:
            return ""
        path = parsed.path
        m = re.search(r"/(\d+)\.html", path)
        if m:
            item_id = m.group(1)
        else:
            query = parse_qs(parsed.query)
            item_id = (query.get("sku") or query.get("id") or [""])[0]
            if not item_id:
                if parsed.fragment and parsed.fragment.isdigit():
                    item_id = parsed.fragment
                else:
                    return url
        return f"https://item.jd.com/{item_id}.html"

    async def get_page(self, browser_context, fallback_url: str):
        await asyncio.sleep(1)
        try:
            for page in browser_context.pages:
                try:
                    if page.is_closed():
                        continue
                    if "jd.com" in page.url:
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
                    const skus = document.querySelectorAll('[data-sku]');
                    const body = document.body ? document.body.innerText : '';
                    return skus.length > 0 || body.includes('没有找到') || body.includes('登录');
                }""",
                timeout=15000,
            )
        except Exception:
            pass

    async def scroll_and_extract(self, agent, page, browser_context=None, fallback_url="") -> list[dict[str, Any]]:
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight || document.body.scrollHeight)")
            await asyncio.sleep(1.2)

        # JD 需要 skip list 去重
        skip_list = []
        state = await page.evaluate(_JD_EXTRACT_JS, skip_list)
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
            await page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll('a, span, div'));
                const next = els.find(el => (el.textContent || '').trim() === '下一页');
                if (next) { next.scrollIntoView({block: 'center'}); next.click(); return true; }
                return false;
            }
            """)
            await asyncio.sleep(5)
            return True
        except Exception:
            pass
        try:
            await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            await asyncio.sleep(4)
        except Exception:
            pass
        return False

    async def after_navigate(self, agent) -> None:
        await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗。")
        for _attempt in range(3):
            ok = await _agent_act(agent, "点击搜索结果顶部的'销量'排序按钮")
            if ok:
                await asyncio.sleep(5)
                break
            await asyncio.sleep(2)
