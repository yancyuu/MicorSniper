# -*- coding: utf-8 -*-
"""测试淘宝搜索结果的相关性过滤。

用法:
  poetry run python -m scripts.test_taobao_filter "SKG" --limit 20
"""

import asyncio
import argparse
import json
from urllib.parse import quote

from agentbay import (
    ActOptions,
    AsyncAgentBay,
    BrowserContext as AgentBayContext,
    BrowserFingerprint,
    BrowserOption,
    BrowserScreen,
    CreateSessionParams,
)
from config.settings import global_settings
from utils.logger import logger


async def _agent_act(agent, instruction: str, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            ret = await agent.act(ActOptions(action=instruction))
            return bool(getattr(ret, "success", False))
        except Exception as e:
            logger.warning(f"[test] agent.act failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return False


_EXTRACT_JS = """
() => {
    const seen = new Set();
    const links = [];

    function normalizeHref(href) {
        try { return new URL(href, window.location.href).href; } catch (e) { return ''; }
    }
    function isProductUrl(url) {
        try {
            const u = new URL(url);
            const host = u.hostname;
            const path = u.pathname;
            const isTaobao = host.endsWith('taobao.com') || host.endsWith('tmall.com') || host.endsWith('tmall.hk');
            if (!isTaobao) return false;
            return path.includes('item.htm') || path.includes('item_o.htm') || host.startsWith('detail.');
        } catch (e) { return false; }
    }
    function extractPrice(el) {
        const priceEl = el.querySelector('[class*="price"], [class*="Price"]');
        if (!priceEl) return '';
        const m = priceEl.textContent.match(/[\\d,.]+/);
        return m ? m[0] : '';
    }
    function extractSales(el) {
        const text = el.textContent || '';
        const m = text.match(/(\\d[\\d,]*\\+?)\\s*(人付款|人收货|月销|已售)/);
        return m ? m[1] + m[2] : '';
    }
    function extractTitle(el) {
        const titleEl = el.querySelector('[class*="title"], [class*="Title"]');
        return titleEl ? titleEl.textContent.trim().slice(0, 120) : '';
    }
    function extractShop(el) {
        const shopEl = el.querySelector('[class*="shop"], [class*="Shop"], [class*="store"], [class*="Store"]');
        return shopEl ? shopEl.textContent.trim().slice(0, 60) : '';
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
            card_text: card ? (card.textContent || '').trim().slice(0, 200) : '',
        });
    }
    return links;
}
"""

_FILTER_PROMPT = """分析以下淘宝商品列表，筛选出与关键词"{keyword}"强相关的商品。

强相关标准：
1. 商品本身就是该品牌/型号的产品
2. 或是该品牌产品的专用配件（如保护套、替换件）
3. 排除：蹭关键词但实际无关的商品（如情趣用品、不相关品类）

商品列表：
{items_json}

请返回一个 JSON 数组，只包含相关商品的序号（从0开始）。
格式：[0, 2, 5]
只返回 JSON 数组，不要其他内容。"""


async def test_filter(keyword: str, limit: int = 20, context_key: str = ""):
    from playwright.async_api import async_playwright

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    context_result = await agent_bay.context.get(context_key, create=False)
    if not context_result.success:
        raise RuntimeError(f"Context not found: {context_key}")

    session_result = await agent_bay.create(
        CreateSessionParams(
            image_id="browser_latest",
            browser_context=AgentBayContext(context_result.context.id, auto_upload=False),
        )
    )
    if not session_result.success:
        raise RuntimeError(f"Failed to create session: {session_result.error_message}")

    session = session_result.session
    pw = None
    browser = None

    try:
        await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
            )
        )

        endpoint = await session.browser.get_endpoint_url()
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(endpoint)
        bc = browser.contexts[0] if browser.contexts else await browser.new_context()
        bc.on("dialog", lambda d: d.dismiss())

        agent = session.browser.agent
        search_url = f"https://s.taobao.com/search?q={quote(keyword)}&sort=sale-desc&tab=pc_taobao"
        await agent.navigate(search_url)
        await asyncio.sleep(8)
        await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗。")
        await _agent_act(agent, "点击销量按钮按照销量排序。")
        await _agent_act(agent, "将页面直接滚动到最底部")
        await asyncio.sleep(3)

        page = None
        for p in bc.pages:
            if not p.is_closed() and ("taobao.com" in p.url or "tmall.com" in p.url):
                page = p
                break
        if not page:
            print("No page found")
            return

        # 提取商品
        raw_links = await page.evaluate(_EXTRACT_JS)
        print(f"\n提取到 {len(raw_links)} 个商品链接")

        # 按关键词过滤：用 agent 判断相关性
        if raw_links:
            # 构造商品摘要给 agent 判断
            items_summary = []
            for i, item in enumerate(raw_links):
                items_summary.append(f"[{i}] {item['title']} | 价格:{item['price']} | 销量:{item['sales']} | 店铺:{item['shop']}")

            filter_instruction = _FILTER_PROMPT.format(
                keyword=keyword,
                items_json="\n".join(items_summary),
            )

            # 用 agent 执行过滤判断
            try:
                ret = await agent.act(ActOptions(action=filter_instruction))
                result_text = getattr(ret, "result", "") or ""
                logger.info(f"[test] Agent filter result: {result_text}")

                # 尝试从 agent 返回结果中提取 JSON 数组
                import re
                json_match = re.search(r'\[[\d\s,]+\]', result_text)
                if json_match:
                    relevant_indices = json.loads(json_match.group())
                    relevant_links = [raw_links[i] for i in relevant_indices if i < len(raw_links)]
                else:
                    logger.warning("[test] Could not parse filter result, keeping all")
                    relevant_links = raw_links
            except Exception as e:
                logger.warning(f"[test] Agent filter failed: {e}, keeping all")
                relevant_links = raw_links

            print(f"\n=== 过滤结果（关键词: {keyword}）===")
            print(f"原始: {len(raw_links)} 个商品")
            print(f"相关: {len(relevant_links)} 个商品")
            print(f"过滤掉: {len(raw_links) - len(relevant_links)} 个不相关商品")

            print(f"\n--- 保留的商品 ---")
            for item in relevant_links[:limit]:
                print(f"  [{item['title'][:50]}] ¥{item['price']} {item['sales']} | {item['shop']}")

            filtered_out = [raw_links[i] for i in range(len(raw_links)) if i not in (relevant_indices if json_match else [])]
            if filtered_out:
                print(f"\n--- 过滤掉的商品 ---")
                for item in filtered_out[:10]:
                    print(f"  [{item['title'][:50]}] ¥{item['price']} {item['sales']} | {item['shop']}")

    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        if pw:
            await pw.stop()
        await agent_bay.delete(session, sync_context=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Taobao relevance filtering")
    parser.add_argument("keyword", help="搜索关键词")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--context-key", default="taobao-context:default:3a4d9c23-c364-465e-90af-5bcbfbbcc73e")
    args = parser.parse_args()

    asyncio.run(test_filter(args.keyword, args.limit, args.context_key))
