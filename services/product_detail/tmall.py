# -*- coding: utf-8 -*-
"""天猫商品详情提取。"""

from .base import ProductDetailService


TMALL_DETAIL_JS = """
() => {
    const result = {};

    function fullUrl(url) {
        if (!url) return '';
        return url.startsWith('//') ? 'https:' + url : url;
    }

    function cleanText(text) {
        return String(text || '').replace(/\\s+/g, ' ').trim();
    }

    function normalizePrice(text) {
        const m = String(text || '').replace(/,/g, '').match(/\\d+(?:\\.\\d+)?/);
        return m ? m[0] : '';
    }

    function firstText(selectors) {
        for (const selector of selectors) {
            const el = document.querySelector(selector);
            const text = el ? cleanText(el.textContent) : '';
            if (text) return text;
        }
        return '';
    }

    const bodyText = document.body ? cleanText(document.body.innerText) : '';
    const ssrRes = window.__ICE_APP_CONTEXT__?.loaderData?.home?.data?.res || {};
    const item = ssrRes.item || {};
    const seller = ssrRes.seller || {};
    const sku2info = ssrRes.skuCore?.sku2info || {};
    const defaultSku = sku2info['0'] || {};

    // 标题：新版天猫详情页的 class 名会把价格标签误命中，优先用 SSR 数据。
    result.title = cleanText(item.title);
    if (!result.title) {
        result.title = firstText(['h1', '[class*="ItemHeader--"] [class*="title"]']).slice(0, 500);
    }
    if (!result.title) {
        const titleMatch = bodyText.match(/图文详情\\s+(.{8,200}?)\\s+(已售|券后|优惠促销)/);
        result.title = titleMatch ? cleanText(titleMatch[1]).slice(0, 500) : '';
    }

    // 价格
    result.price = normalizePrice(defaultSku.subPrice?.priceText || defaultSku.price?.priceText);
    if (!result.price) {
        const priceEl = document.querySelector('[class*="priceText"], [class*="Price--priceText"], [class*="tm-price"]');
        result.price = normalizePrice(priceEl ? priceEl.textContent : '');
    }
    if (!result.price) {
        const m = bodyText.match(/券后\\s*￥\\s*([\\d,.]+)/) || bodyText.match(/¥\\s*([\\d,.]+)/);
        result.price = m ? normalizePrice(m[1]) : '';
    }

    // 原价
    result.original_price = normalizePrice(defaultSku.price?.priceText);
    if (!result.original_price) {
        const origEl = document.querySelector('[class*="originalPrice"], [class*="Price--original"]');
        result.original_price = normalizePrice(origEl ? origEl.textContent : '');
    }

    // 销量
    result.sales = item.vagueSellCount ? `已售 ${item.vagueSellCount}` : '';
    if (!result.sales) {
        const salesEl = document.querySelector('[class*="Sold--"], [class*="soldContent"], [class*="tm-count"]');
        result.sales = salesEl ? cleanText(salesEl.textContent) : '';
    }
    if (!result.sales) {
        const m = bodyText.match(/已售\\s*([\\d,.万+]+)/) || bodyText.match(/(\\d[\\d,]*\\+?)\\s*(人付款|人收货|月销|已售)/);
        result.sales = m ? (m[2] ? m[1] + m[2] : `已售 ${m[1]}`) : '';
    }

    // 店铺
    result.shop_name = cleanText(seller.shopName || seller.sellerNick).slice(0, 200);
    if (!result.shop_name) {
        const shopEl = document.querySelector('[class*="shopName"], [class*="ShopName--"], [class*="ShopHeader"] a[title]');
        result.shop_name = shopEl ? cleanText(shopEl.getAttribute('title') || shopEl.textContent).slice(0, 200) : '';
    }
    const shopLink = document.querySelector('a[href*="shop"], a[href*="store"]');
    result.shop_url = fullUrl(seller.pcShopUrl || (shopLink ? shopLink.href : ''));

    // 主图
    const mainImages = (Array.isArray(item.images) ? item.images : []).map(fullUrl).filter(Boolean);
    document.querySelectorAll('[class*="PicGallery--"] img, [class*="mainPic"] img').forEach(img => {
        const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && !src.includes('spacer') && !src.includes('1x1')) {
            const full = fullUrl(src);
            if (!mainImages.includes(full)) mainImages.push(full);
        }
    });
    if (mainImages.length === 0) {
        for (const img of document.querySelectorAll('img')) {
            const src = (img.getAttribute('data-src') || img.getAttribute('src') || '');
            if (src.includes('imgextra') || src.includes('alicdn')) {
                const full = fullUrl(src);
                if (!mainImages.includes(full)) mainImages.push(full);
            }
        }
    }
    result.image = mainImages[0] || '';
    result.main_images = mainImages;

    // SKU
    const skuItems = [];
    document.querySelectorAll('[class*="skuItem"], [class*="SKUItem"]').forEach(el => {
        skuItems.push(el.textContent.trim());
    });
    result.sku_info = skuItems;

    // 发货地
    const locEl = document.querySelector('[class*="locText"], [class*="Loc--"]');
    result.location = locEl ? locEl.textContent.trim() : '';
    if (!result.location) {
        const locMatch = bodyText.match(/快递:\\s*[^\\s]+\\s+([^\\s]+)\\s+至/);
        result.location = locMatch ? locMatch[1] : '';
    }

    // 详情图
    const detailImages = [];
    document.querySelectorAll('#description img, [class*="desc"] img').forEach(img => {
        const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && src.startsWith('http')) detailImages.push(src);
    });
    result.detail_images = detailImages;

    return result;
}
"""


class TmallProductDetailService(ProductDetailService):
    platform = "tmall"
    detail_js = TMALL_DETAIL_JS
