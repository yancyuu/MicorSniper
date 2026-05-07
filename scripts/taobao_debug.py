# -*- coding: utf-8 -*-
"""淘宝搜索测试 - 用 agent.act 关弹框 + CDP JS 提取"""
import asyncio
import sys
import os
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urllib.parse import quote
from agentbay import (
    AsyncAgentBay, CreateSessionParams,
    BrowserContext as AgentBayContext, BrowserOption,
    BrowserScreen, BrowserFingerprint,
    ActOptions, ExtractOptions,
)
from pydantic import BaseModel, Field
from typing import List, Optional
from playwright.async_api import async_playwright
from config.settings import global_settings

CONTEXT_KEY = "taobao-context:default:3a4d9c23-c364-465e-90af-5bcbfbbcc73e"


# ===== Pydantic Schema for agent.extract fallback =====
class SearchItem(BaseModel):
    title: str = Field(default="", description="商品标题")
    price: str = Field(default="", description="价格")
    sales: str = Field(default="", description="销量")
    url: str = Field(default="", description="商品链接")

class SearchResult(BaseModel):
    items: List[SearchItem] = Field(default_factory=list, description="商品列表")


# ===== JS 提取脚本 =====
JS_EXTRACT_SEARCH = """
() => {
    const items = [];
    const seen = new Set();
    const allLinks = document.querySelectorAll('a[href*="item.htm"], a[href*="item_o.htm"]');
    allLinks.forEach(a => {
        const href = a.href;
        if (!href || seen.has(href)) return;
        if (!href.includes('taobao.com') && !href.includes('tmall.com')) return;
        seen.add(href);
        const card = a.closest('[class*="Card"]') || a.closest('[class*="Content"]') || a.closest('[class*="cardWrapper"]') || a.parentElement;
        const titleEl = card.querySelector('[class*="Title"] span, [class*="title"] span');
        const priceEl = card.querySelector('[class*="Price--priceInt"], [class*="priceInt"]');
        const priceFloatEl = card.querySelector('[class*="Price--priceFloat"], [class*="priceFloat"]');
        const salesEl = card.querySelector('[class*="SalesPoint"] span, [class*="sellCount"], [class*="realSales"]');
        const shopEl = card.querySelector('[class*="ShopName"] span, [class*="shopName"]');
        const imgEl = card.querySelector('img[src*="alicdn"], img[src*="taobaocdn"]');
        items.push({
            url: href,
            title: titleEl ? titleEl.textContent.trim() : '',
            price: (priceEl ? priceEl.textContent.trim() : '') + (priceFloatEl ? priceFloatEl.textContent.trim() : ''),
            sales: salesEl ? salesEl.textContent.trim() : '',
            shop_name: shopEl ? shopEl.textContent.trim() : '',
            image: imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '',
        });
    });
    return items;
}
"""

JS_EXTRACT_DETAIL = """
() => {
    const r = { title: '', price: '', sales: '', main_images: [], shop_name: '', source: 'dom' };
    try {
        if (window.__INITIAL_DATA__) {
            const d = window.__INITIAL_DATA__;
            r.source = '__INITIAL_DATA__';
            if (d.item) {
                r.title = d.item.title || '';
                if (d.item.priceInfo) {
                    r.price = d.item.priceInfo.price || '';
                    r.original_price = d.item.priceInfo.originalPrice || '';
                }
                r.sales = d.item.sellCount ? String(d.item.sellCount) : '';
                if (d.item.images) r.main_images = d.item.images.map(u => u.startsWith('//') ? 'https:' + u : u);
            }
            if (d.seller) r.shop_name = d.seller.shopName || '';
            return r;
        }
    } catch(e) {}
    const t = document.querySelector('[class*="Title--title"] span, h1[class*="title"]');
    if (t) r.title = t.textContent.trim();
    const p = document.querySelector('[class*="Price--priceInt"]');
    if (p) r.price = p.textContent.trim();
    return r;
}
"""


async def act(agent, instruction: str) -> bool:
    print(f"  Act: {instruction}")
    ret = await agent.act(ActOptions(action=instruction))
    return bool(getattr(ret, "success", False))


async def main():
    keyword = sys.argv[1] if len(sys.argv) > 1 else "AI工具"
    url = f"https://s.taobao.com/search?q={quote(keyword)}&sort=sale-desc"

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    # 1. 获取上下文 & 创建 session
    ab_ctx = await agent_bay.context.get(CONTEXT_KEY, create=False)
    assert ab_ctx.success and ab_ctx.context, f"Context not found: {CONTEXT_KEY}"
    print(f"Context: {ab_ctx.context.id}")

    session_result = await agent_bay.create(
        CreateSessionParams(
            image_id="browser_latest",
            browser_context=AgentBayContext(ab_ctx.context.id, auto_upload=False),
        )
    )
    assert session_result.success, f"Create session failed: {session_result.error_message}"
    session = session_result.session
    print(f"Session: {session.session_id}")

    try:
        # 2. 初始化浏览器
        await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(
                    devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"],
                ),
            )
        )

        agent = session.browser.agent

        # 3. 导航到搜索页
        print(f"\nNavigating: {url}")
        await agent.navigate(url)
        await asyncio.sleep(5)

        # 4. 用 agent.act 关闭弹框
        await act(agent, "关闭页面上所有弹框、登录提示、广告弹窗")
        await asyncio.sleep(5)

        # 5. 连接 CDP 做 JS 提取
        pw = await async_playwright().start()
        endpoint = await session.browser.get_endpoint_url()
        browser = await pw.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        cookies = await context.cookies()
        taobao_cookies = [c for c in cookies if 'taobao' in (c.get('domain', '') or '')]
        print(f"Cookies: {len(cookies)} total, {len(taobao_cookies)} taobao")

        # 找到已打开的淘宝页面
        pages = context.pages
        page = None
        for p in pages:
            if 'taobao.com' in p.url:
                page = p
                break
        if not page:
            page = await context.new_page()
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")

        # 等数据加载 + 滚动
        await asyncio.sleep(3)
        for i in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)

        # 6. JS 提取
        t0 = time.time()
        items = await page.evaluate(JS_EXTRACT_SEARCH)
        js_elapsed = time.time() - t0
        print(f"\n=== JS 提取: {len(items)} items ({js_elapsed:.3f}s) ===")
        for i, item in enumerate(items[:10]):
            print(f"  [{i+1}] {item.get('title','N/A')[:50]}")
            print(f"       ¥{item.get('price','?')} | {item.get('sales','?')} | {item.get('shop_name','?')}")

        # 7. 如果 JS 提取为空，用 agent.extract 兜底
        if not items:
            print("\nJS 提取为空，尝试 agent.extract...")
            t0 = time.time()
            ok, data = await agent.extract(
                ExtractOptions(
                    instruction="提取页面上所有商品的标题、价格、销量、链接。只提取真实商品。",
                    schema=SearchResult,
                    use_vision=True,
                )
            )
            agent_elapsed = time.time() - t0
            if ok and isinstance(data, SearchResult) and data.items:
                print(f"  agent.extract: {len(data.items)} items ({agent_elapsed:.3f}s)")
                for i, item in enumerate(data.items[:10]):
                    print(f"  [{i+1}] {item.title[:50]} | ¥{item.price} | {item.sales}")
                items = [{"url": item.url, "title": item.title, "price": item.price, "sales": item.sales} for item in data.items]
            else:
                print(f"  agent.extract 也为空 ({agent_elapsed:.3f}s)")

        # 8. 详情页测试
        if items:
            detail_url = items[0].get("url", "")
            if detail_url:
                print(f"\n=== 详情页测试 ===")
                print(f"URL: {detail_url}")
                dp = await context.new_page()
                await dp.goto(detail_url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(3)

                t0 = time.time()
                detail = await dp.evaluate(JS_EXTRACT_DETAIL)
                elapsed = time.time() - t0
                print(f"Source: {detail.get('source')} | {elapsed:.3f}s")
                print(f"标题: {detail.get('title','N/A')[:60]}")
                print(f"价格: ¥{detail.get('price','?')} (原价: ¥{detail.get('original_price','?')})")
                print(f"销量: {detail.get('sales','?')} | 头图: {len(detail.get('main_images',[]))} | 店铺: {detail.get('shop_name','?')}")
                await dp.close()

        # 9. 保存结果
        with open("/tmp/taobao_items.json", "w") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        print(f"\n保存: /tmp/taobao_items.json ({len(items)} items)")

        await page.close()
        try:
            cdp = await browser.new_browser_cdp_session()
            await cdp.send('Browser.close')
        except:
            pass
        await browser.close()
        await pw.stop()

    finally:
        await agent_bay.delete(session, sync_context=False)
        print("Done.")


asyncio.run(main())
