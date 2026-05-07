# -*- coding: utf-8 -*-
"""
淘宝提取方式对比测试: Agent Extract vs 直接JS
测试维度: 速度、准确率、数据完整性
"""
import asyncio
import time
import json
import base64
from agentbay import AsyncAgentBay, CreateSessionParams, BrowserContext as AgentBayContext, BrowserOption, BrowserScreen, BrowserFingerprint, ExtractOptions
from pydantic import BaseModel
from typing import List
from playwright.async_api import async_playwright
from config.settings import global_settings

CONTEXT_KEY = "taobao-context:default:3a4d9c23-c364-465e-90af-5bcbfbbcc73e"
SEARCH_URL = "https://s.taobao.com/search?q=AI%E5%B7%A5%E5%85%B7&sort=sale-desc"


# ===== JS 提取脚本 =====

# 搜索页 JS (从 taobao_search.py 借用)
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
        const imgEl = card.querySelector('img[src*="alicdn"], img[src*="taobaocdn"]');
        items.push({
            url: href,
            title: titleEl ? titleEl.textContent.trim() : '',
            price: (priceEl ? priceEl.textContent.trim() : '') + (priceFloatEl ? priceFloatEl.textContent.trim() : ''),
            sales: salesEl ? salesEl.textContent.trim() : '',
            image: imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '',
        });
    });
    if (items.length === 0) {
        const cards = document.querySelectorAll('[class*="Card--doubleCardWrapper"], [class*="Content--contentInner"]');
        cards.forEach(card => {
            const link = card.querySelector('a[href]');
            const href = link ? link.href : '';
            if (!href || seen.has(href)) return;
            seen.add(href);
            const titleEl = card.querySelector('[class*="Title"] span, [class*="title"]');
            const priceEl = card.querySelector('[class*="Price"] span, [class*="price"]');
            const salesEl = card.querySelector('[class*="Sales"] span, [class*="sell"]');
            items.push({
                url: href,
                title: titleEl ? titleEl.textContent.trim() : '',
                price: priceEl ? priceEl.textContent.trim() : '',
                sales: salesEl ? salesEl.textContent.trim() : '',
                image: '',
            });
        });
    }
    return { items, total_links: seen.size };
}
"""

# 搜索页 - 直接从 window 页面数据提取
JS_EXTRACT_SEARCH_DATA_LAYER = """
() => {
    // 尝试从页面的数据层直接提取
    const results = { raw_data_available: false, items: [] };

    // 方法1: window.__INITIAL_DATA__ (搜索页可能没有)
    if (window.__INITIAL_DATA__) {
        results.raw_data_available = true;
        results.source = '__INITIAL_DATA__';
    }

    // 方法2: 从 script 标签找 JSON 数据
    const scripts = document.querySelectorAll('script');
    for (const s of scripts) {
        const text = s.textContent || '';
        if (text.includes('g_page_config') || text.includes('auctions') || text.includes('items')) {
            try {
                // 尝试提取 JSON
                const match = text.match(/g_page_config\\s*=\\s*({.+?});/s) ||
                              text.match(/"auctions"\\s*:\\s*\\[.+?\\]/s);
                if (match) {
                    results.raw_data_available = true;
                    results.source = 'g_page_config';
                    results.raw_snippet = text.substring(0, 500);
                }
            } catch(e) {}
        }
    }

    // 方法3: 检查 React/Vue 的内部状态
    const rootEl = document.querySelector('#root') || document.querySelector('#app');
    if (rootEl) {
        const reactKey = Object.keys(rootEl).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
        results.react_fiber = !!reactKey;
    }

    return results;
}
"""

# 详情页 JS - 优先从 window.__INITIAL_DATA__ 提取
JS_EXTRACT_DETAIL_DATALAYER = """
() => {
    const r = { title: '', price: '', sales: '', main_images: [], shop_name: '', source: 'dom' };

    // 优先: 从 window.__INITIAL_DATA__ 提取 (最可靠)
    try {
        if (window.__INITIAL_DATA__) {
            const d = window.__INITIAL_DATA__;
            r.source = '__INITIAL_DATA__';
            if (d.item) {
                if (d.item.title) r.title = d.item.title;
                if (d.item.priceInfo) {
                    r.price = d.item.priceInfo.price || '';
                    r.original_price = d.item.priceInfo.originalPrice || '';
                }
                if (d.item.sellCount) r.sales = String(d.item.sellCount);
                if (d.item.images) r.main_images = d.item.images.map(u => u.startsWith('//') ? 'https:' + u : u);
            }
            if (d.seller) {
                r.shop_name = d.seller.shopName || '';
                r.shop_url = d.seller.shopUrl || '';
            }
            return r;
        }
    } catch(e) {}

    // 兜底: DOM选择器
    const titleEl = document.querySelector('[class*="Title--title"] span, h1[class*="title"]');
    if (titleEl) r.title = titleEl.textContent.trim();
    const priceEl = document.querySelector('[class*="Price--priceInt"]');
    const priceFloat = document.querySelector('[class*="Price--priceFloat"]');
    if (priceEl) r.price = priceEl.textContent.trim();
    if (priceFloat) r.price += priceFloat.textContent.trim();
    const salesEl = document.querySelector('[class*="SalesPoint--sales"] span, [class*="sellCount"]');
    if (salesEl) r.sales = salesEl.textContent.trim();
    const imgs = document.querySelectorAll('[class*="PicGallery--mainImage"] img');
    imgs.forEach(img => {
        const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
        if (src) r.main_images.push(src.startsWith('//') ? 'https:' + src : src);
    });

    return r;
}
"""


# ===== Agent Extract Schema =====

class SearchItem(BaseModel):
    title: str
    price: str
    sales: str
    url: str

class SearchResult(BaseModel):
    items: List[SearchItem]

class DetailItem(BaseModel):
    title: str
    price: str
    sales: str
    main_images: List[str]
    shop_name: str

class DetailResult(BaseModel):
    item: DetailItem


async def main():
    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    # 获取上下文
    ctx = await agent_bay.context.get(CONTEXT_KEY, create=False)
    print(f"Context: {ctx.context.id}")

    # 创建会话
    s = await agent_bay.create(
        CreateSessionParams(
            image_id="browser_latest",
            browser_context=AgentBayContext(ctx.context.id, auto_upload=False)
        )
    )
    session = s.session
    print(f"Session: {session.session_id}")

    await session.browser.initialize(
        BrowserOption(
            screen=BrowserScreen(width=1920, height=1080),
            solve_captchas=True,
            use_stealth=True,
            fingerprint=BrowserFingerprint(
                devices=["desktop"],
                operating_systems=["windows"],
                locales=["zh-CN"],
            ),
        )
    )

    # 连接 CDP
    pw = await async_playwright().start()
    endpoint = await session.browser.get_endpoint_url()
    browser = await pw.chromium.connect_over_cdp(endpoint)
    context = browser.contexts[0]

    try:
        # ============================================================
        # 测试一: 搜索页提取对比
        # ============================================================
        print("\n" + "=" * 60)
        print("测试一: 搜索页提取对比")
        print("=" * 60)

        # 先导航到搜索页
        print(f"\n导航到: {SEARCH_URL}")
        await session.browser.agent.navigate(SEARCH_URL)
        await asyncio.sleep(8)

        # 截图
        screenshot = await session.browser.agent.screenshot(full_page=False)
        if isinstance(screenshot, str) and screenshot.startswith("data:"):
            _, encoded = screenshot.split(",", 1)
            with open("/tmp/taobao_compare_search.png", "wb") as f:
                f.write(base64.b64decode(encoded))
            print("搜索页截图: /tmp/taobao_compare_search.png")

        # 找到淘宝页面
        pages = context.pages
        target_page = None
        for p in pages:
            if 'taobao.com' in p.url:
                target_page = p
                break
        if not target_page:
            target_page = pages[0]
            await target_page.goto(SEARCH_URL, timeout=60000, wait_until="networkidle")
            await asyncio.sleep(8)

        # --- 方法A: 直接JS提取 ---
        print("\n--- 方法A: 直接JS提取 ---")
        t0 = time.time()
        js_result = await target_page.evaluate(JS_EXTRACT_SEARCH)
        js_time = time.time() - t0
        js_items = js_result.get("items", [])
        print(f"  耗时: {js_time:.3f}s")
        print(f"  商品数: {len(js_items)}")
        print(f"  总链接数: {js_result.get('total_links', 0)}")
        for item in js_items[:5]:
            print(f"    - {item.get('title', 'N/A')[:40]} | ¥{item.get('price', '?')} | {item.get('sales', '?')}")

        # --- 方法A2: 数据层探测 ---
        print("\n--- 数据层探测 ---")
        data_layer = await target_page.evaluate(JS_EXTRACT_SEARCH_DATA_LAYER)
        print(f"  数据层可用: {data_layer.get('raw_data_available', False)}")
        print(f"  React Fiber: {data_layer.get('react_fiber', False)}")
        if data_layer.get('source'):
            print(f"  数据源: {data_layer['source']}")

        # --- 方法B: Agent Extract ---
        print("\n--- 方法B: Agent Extract (AI视觉) ---")
        t0 = time.time()
        try:
            agent_result = await session.browser.agent.extract(
                ExtractOptions(
                    instruction="提取当前页面上所有商品的信息，包括标题、价格、销量、商品链接URL。只提取真实的商品，不要包含广告。",
                    use_vision=True,
                    schema=SearchResult,
                )
            )
            agent_time = time.time() - t0
            agent_items = agent_result[1].items if len(agent_result) > 1 and agent_result[1] else []
            print(f"  耗时: {agent_time:.3f}s")
            print(f"  商品数: {len(agent_items)}")
            for item in agent_items[:5]:
                title = item.title if hasattr(item, 'title') else item.get('title', 'N/A')
                price = item.price if hasattr(item, 'price') else item.get('price', '?')
                sales = item.sales if hasattr(item, 'sales') else item.get('sales', '?')
                url = item.url if hasattr(item, 'url') else item.get('url', '')
                print(f"    - {str(title)[:40]} | ¥{price} | {sales}")
        except Exception as e:
            agent_time = time.time() - t0
            agent_items = []
            print(f"  失败 ({agent_time:.3f}s): {e}")

        # --- 搜索页对比总结 ---
        print("\n--- 搜索页对比 ---")
        print(f"  JS提取:     {len(js_items)}个商品, {js_time:.3f}s")
        print(f"  Agent提取:  {len(agent_items)}个商品, {agent_time:.3f}s")
        print(f"  速度比:     {agent_time/js_time:.1f}x 慢" if js_time > 0 else "")

        # ============================================================
        # 测试二: 详情页提取对比
        # ============================================================
        print("\n" + "=" * 60)
        print("测试二: 详情页提取对比")
        print("=" * 60)

        # 取第一个商品链接
        detail_url = None
        for item in js_items:
            if item.get("url") and ("item.taobao.com" in item["url"] or "detail.tmall.com" in item["url"]):
                detail_url = item["url"]
                break

        if not detail_url:
            # 从 agent 结果取
            for item in agent_items:
                url = item.url if hasattr(item, 'url') else item.get('url', '')
                if url and ("item.taobao.com" in url or "detail.tmall.com" in url):
                    detail_url = url
                    break

        if detail_url:
            print(f"\n详情页URL: {detail_url}")
            await target_page.goto(detail_url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(5)

            # 截图
            await target_page.screenshot(path="/tmp/taobao_compare_detail.png")
            print("详情页截图: /tmp/taobao_compare_detail.png")

            # --- 方法A: JS直接提取 (数据层优先) ---
            print("\n--- 方法A: JS直接提取 (数据层优先) ---")
            t0 = time.time()
            js_detail = await target_page.evaluate(JS_EXTRACT_DETAIL_DATALAYER)
            js_detail_time = time.time() - t0
            print(f"  耗时: {js_detail_time:.3f}s")
            print(f"  数据源: {js_detail.get('source', 'unknown')}")
            print(f"  标题: {js_detail.get('title', 'N/A')[:50]}")
            print(f"  价格: ¥{js_detail.get('price', 'N/A')}")
            print(f"  销量: {js_detail.get('sales', 'N/A')}")
            print(f"  头图数: {len(js_detail.get('main_images', []))}")
            print(f"  店铺: {js_detail.get('shop_name', 'N/A')}")

            # --- 方法B: Agent Extract ---
            print("\n--- 方法B: Agent Extract (AI视觉) ---")
            t0 = time.time()
            try:
                agent_detail = await session.browser.agent.extract(
                    ExtractOptions(
                        instruction="提取这个商品详情页的信息：标题、价格、销量、所有主图URL、店铺名称。",
                        use_vision=True,
                        schema=DetailResult,
                    )
                )
                agent_detail_time = time.time() - t0
                ad = agent_detail[1] if len(agent_detail) > 1 and agent_detail[1] else None
                if ad:
                    item = ad.item if hasattr(ad, 'item') else ad
                    title = item.title if hasattr(item, 'title') else 'N/A'
                    price = item.price if hasattr(item, 'price') else 'N/A'
                    sales = item.sales if hasattr(item, 'sales') else 'N/A'
                    imgs = item.main_images if hasattr(item, 'main_images') else []
                    shop = item.shop_name if hasattr(item, 'shop_name') else 'N/A'
                    print(f"  耗时: {agent_detail_time:.3f}s")
                    print(f"  标题: {str(title)[:50]}")
                    print(f"  价格: ¥{price}")
                    print(f"  销量: {sales}")
                    print(f"  头图数: {len(imgs)}")
                    print(f"  店铺: {shop}")
                else:
                    print(f"  无数据 ({agent_detail_time:.3f}s)")
            except Exception as e:
                agent_detail_time = time.time() - t0
                print(f"  失败 ({agent_detail_time:.3f}s): {e}")

            # --- 详情页对比 ---
            print("\n--- 详情页对比 ---")
            print(f"  JS提取:     {js_detail_time:.3f}s, 数据源={js_detail.get('source')}")
            print(f"  Agent提取:  {agent_detail_time:.3f}s")

            # 字段完整性对比
            js_fields = sum(1 for k in ['title', 'price', 'sales', 'shop_name'] if js_detail.get(k))
            print(f"  JS字段完整性: {js_fields}/4")
        else:
            print("\n没有找到可用的详情页链接")

        # ============================================================
        # 最终结论
        # ============================================================
        print("\n" + "=" * 60)
        print("结论")
        print("=" * 60)
        print("""
搜索页:
  - JS快100x+，适合滚动加载场景
  - Agent适合CSS选择器失效时的兜底

详情页:
  - window.__INITIAL_DATA__ 是最可靠数据源
  - JS提取速度快、零成本
  - Agent Extract 仅作为 fallback

推荐策略:
  搜索页: JS提取 (page.evaluate)
  详情页: __INITIAL_DATA__ 数据层 > DOM选择器 > Agent Extract
  容错:   当JS提取结果为空时，fallback到Agent Extract
        """)

    finally:
        try:
            cdp = await browser.new_browser_cdp_session()
            await cdp.send('Browser.close')
        except:
            pass
        await browser.close()
        await pw.stop()
        await agent_bay.delete(session, sync_context=False)
        print("\nDone.")


asyncio.run(main())
