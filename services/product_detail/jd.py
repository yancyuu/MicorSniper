# -*- coding: utf-8 -*-
"""京东商品详情提取。"""

from .base import ProductDetailService


JD_DETAIL_JS = """
() => {
    const result = {};

    function cleanText(text) {
        return String(text || '').replace(/\\s+/g, ' ').trim();
    }

    function firstText(selectors) {
        for (const selector of selectors) {
            const el = document.querySelector(selector);
            const text = el ? cleanText(el.textContent) : '';
            if (text) return text;
        }
        return '';
    }

    function normalizePrice(text) {
        const m = String(text || '').replace(/,/g, '').match(/\\d+(?:\\.\\d+)?/);
        return m ? m[0] : '';
    }

    function fullUrl(url) {
        if (!url) return '';
        return url.startsWith('//') ? 'https:' + url : url;
    }

    const bodyText = document.body ? cleanText(document.body.innerText) : '';

    // 标题
    result.title = firstText(['.itemInfo-wrap .sku-name', '.sku-name', '#name .sku-name', 'h1']).slice(0, 500);
    if (!result.title || result.title.includes('最小单价计算器')) {
        const docTitle = cleanText(document.title)
            .replace(/【图片.*$/g, '')
            .replace(/-京东.*$/g, '')
            .replace(/京东.*$/g, '');
        if (docTitle && !docTitle.includes('最小单价计算器')) result.title = docTitle.slice(0, 500);
    }
    if (!result.title || result.title.includes('最小单价计算器')) {
        const m = bodyText.match(/商品名称[:：]?\\s*(.{8,200}?)\\s+(京东价|店铺|配送)/);
        result.title = m ? cleanText(m[1]).slice(0, 500) : '';
    }

    // 价格
    result.price = normalizePrice(firstText(['#jd-price', '.summary-price .p-price .price', '.p-price .price', '[class*="J-p-"]']));
    if (!result.price) {
        const m = bodyText.match(/(?:京东价|秒杀价|到手价|券后价)?\\s*[¥￥]\\s*([\\d,.]+)/);
        result.price = m ? normalizePrice(m[1]) : '';
    }

    // 原价
    result.original_price = '';
    const origEl = document.querySelector('.p-price del, [class*="orig-price"], [class*="J-original-price"], del[class*="price"]');
    if (origEl) {
        result.original_price = normalizePrice(origEl.textContent);
    }
    if (!result.original_price) {
        const priceM = bodyText.match(/(?:参考价|原价|定价|定价|吊牌价|厂商指导价|京东价)[：:\\s]*[¥￥]?\\s*([\\d,.]+)/);
        if (priceM) result.original_price = normalizePrice(priceM[1]);
    }
    // 如果价格旁边有划线价
    if (!result.original_price && result.price) {
        const allPrices = bodyText.match(/[¥￥]\\s*([\\d,.]+)/g);
        if (allPrices && allPrices.length >= 2) {
            const prices = allPrices.map(p => normalizePrice(p)).filter(Boolean).sort((a, b) => Number(b) - Number(a));
            if (prices.length >= 2 && Number(prices[0]) > Number(result.price)) {
                result.original_price = prices[0];
            }
        }
    }

    // 销量
    result.sales = firstText([
        '#comment-count a',
        '#comment-count',
        '.comment-count a',
        '.percent-con',
        '[class*="comment-count"]',
        '[class*="CommentCount"]',
        '[clstag*="comment"]',
    ]);
    if (!result.sales) {
        const m = bodyText.match(/(\\d[\\d,]*\\+?)\\s*(万)?\\s*(人?评价|条评价|评价|人?购买|人?收货)/) ||
            bodyText.match(/累计评价\\s*([\\d,.万+]+)/) ||
            bodyText.match(/评论数\\s*([\\d,.万+]+)/);
        result.sales = m ? cleanText(m[0]) : '';
    }

    // 店铺
    result.shop_name = firstText([
        '.J-hove-wrap .name a',
        '.popbox .name a',
        '[class*="shopName"] a',
        '[class*="shop-name"] a',
        'a[href*="mall.jd.com"]',
        'a[href*="shop.jd.com"]',
        '[class*="seller"] a',
        '[class*="shop"] a',
        '[class*="Store"]',
    ]).slice(0, 200);
    if (!result.shop_name) {
        const m = bodyText.match(/店铺[:：]?\\s*(.{2,60}?)(?:\\s+联系客服|\\s+进入店铺|\\s+关注店铺)/);
        result.shop_name = m ? cleanText(m[1]).slice(0, 200) : '';
    }
    // Fallback: match from body text patterns like "XXX京东自营旗舰店"
    if (!result.shop_name) {
        const m2 = bodyText.match(/([^\\s]{2,40}?(?:旗舰店|专营店|官方店|专卖店|自营旗舰店|自营店))/);
        if (m2) result.shop_name = m2[1].slice(0, 200);
    }
    // 清理店铺名：去掉"进店逛逛"等后缀
    result.shop_name = result.shop_name.replace(/[\\s]*进店逛逛.*/, '').replace(/[\\s]*关注店铺.*/, '').replace(/[\\s]*精选镇店.*/, '').replace(/[\\s]*联系客服.*/, '').trim();
    const shopLink = document.querySelector('[class*="shopName"] a, [class*="shop-name"] a, .J-hove-wrap .name a, a[href*="shop.jd"], a[href*="mall.jd"]');
    result.shop_url = shopLink ? shopLink.href : '';

    // 主图
    const mainImages = [];
    document.querySelectorAll('#spec-img, #preview img, #spec-list img, .spec-list img, .lh img').forEach(img => {
        const src = img.getAttribute('data-origin') || img.getAttribute('data-url') || img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && !src.includes('spacer') && !src.includes('1x1')) {
            let full = fullUrl(src);
            if (full && !full.startsWith('http') && full.includes('jfs/')) full = 'https://img14.360buyimg.com/n1/' + full.replace(/^\\/+/, '');
            if (!mainImages.includes(full)) mainImages.push(full);
        }
    });
    result.image = mainImages[0] || '';
    result.main_images = mainImages;

    // SKU
    const skuItems = [];
    document.querySelectorAll('#choose-attrs [id^="choose-attr"], [id^="choose-attr"]').forEach(group => {
        const label = cleanText(
            group.querySelector('.dt, .label, [class*="label"]')?.textContent ||
            group.getAttribute('data-type') ||
            group.getAttribute('id') ||
            ''
        ).replace(/[:：]$/, '');
        const values = [];
        group.querySelectorAll('.item, a, button, [class*="item"]').forEach(el => {
            const text = cleanText(
                el.getAttribute('title') ||
                el.getAttribute('data-value') ||
                el.getAttribute('data-name') ||
                el.textContent ||
                ''
            );
            if (text && text !== label && !values.includes(text)) values.push(text);
        });
        if (values.length) {
            skuItems.push(label ? `${label}: ${values.join(' / ')}` : values.join(' / '));
        }
    });
    if (skuItems.length === 0) {
        document.querySelectorAll('#choose-attrs .item, [id^="choose-attr"] .item, .choose-attrs .item, .summary-attrs .item, [class*="sku-item"], [class*="J-sku-item"]').forEach(el => {
            const text = cleanText(el.textContent || el.getAttribute('title') || el.getAttribute('data-value') || '');
            if (text && !skuItems.includes(text)) skuItems.push(text);
        });
    }
    if (skuItems.length === 0) {
        const skuText = bodyText.match(/(颜色|版本|规格|尺码|套装|型号)[:：]?\\s*(.{2,160}?)(?:\\s+配送|\\s+增值|\\s+白条|\\s+服务)/);
        if (skuText) skuItems.push(cleanText(`${skuText[1]}: ${skuText[2]}`));
    }
    result.sku_info = skuItems;

    // 发货地
    result.location = '';

    result.detail_images = [];

    return result;
}
"""


class JdProductDetailService(ProductDetailService):
    platform = "jd"
    detail_js = JD_DETAIL_JS
    detail_images_js = """
async () => {
    const imgs = [];
    function fullUrl(url) {
        if (!url) return '';
        return url.startsWith('//') ? 'https:' + url : url;
    }

    function cleanImageUrl(url) {
        let full = fullUrl(String(url || '').trim());
        full = full.split(')')[0].split('}')[0].split(';')[0].replace(/^url\\(["']?/, '').replace(/["']$/, '');
        return full;
    }

    function isDetailImage(url) {
        if (!url || !url.startsWith('http')) return false;
        if (!/\.(jpg|jpeg|png|webp|avif|gif)(\.\w+)?(\?|$)/i.test(url)) return false;
        const blocked = ['blank', 'spacer', 'shaidan', 's300x300', 's228x228', 's64x64', 's48x48', 's128x128', 's200x200', 's100x100', 's50x50', 'default.image', 'avatar', 'logo', 'icon', 'badge'];
        if (blocked.some(key => url.includes(key))) return false;
        if (url.includes('popWareDetail') || url.includes('/wareDetail/') || url.includes('/sku/')) return true;
        if (url.includes('/jfs/')) return true;
        return false;
    }

    async function wait(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    for (const y of [600, 1200, 2000, 3200, 4800, 6500, document.body.scrollHeight]) {
        window.scrollTo(0, y);
        await wait(500);
    }

    const detailImageSelectors = [
        '#detail img',
        '#J-detail-content img',
        '.detail-content img',
        '[class*="detail"] img',
        '[class*="desc"] img',
        '[class*="Detail"] img',
        '[class*="Desc"] img',
    ].join(',');

    function addImg(img) {
        const src = img.getAttribute('data-lazyload') || img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('src') || '';
        const full = cleanImageUrl(src);
        const w = img.naturalWidth || img.width || 0;
        const h = img.naturalHeight || img.height || 0;
        if (isDetailImage(full) && !imgs.includes(full) && (w > 300 || h > 300)) {
            imgs.push(full);
        }
    }

    document.querySelectorAll(detailImageSelectors).forEach(addImg);

    // 全页面扫描大尺寸 jfs 图片作为补充
    if (imgs.length < 3) {
        document.querySelectorAll('img').forEach(addImg);
    }

    for (const frame of document.querySelectorAll('iframe')) {
        try {
            const doc = frame.contentDocument;
            if (!doc) continue;
            doc.querySelectorAll('img').forEach(addImg);
        } catch (e) {}
    }

    if (imgs.length === 0) {
        const html = document.documentElement.innerHTML;
        const imageUrlPattern = new RegExp('https?://[^"\\\\\\'<>\\\\s]+(?:popWareDetail|wareDetail|sku|jfs/t)[^"\\\\\\'<>\\\\s]+\\\\.(?:jpg|jpeg|png|webp|avif|gif)(?:\\\\.\\\\w+)?', 'g');
        const matches = html.match(imageUrlPattern) || [];
        for (const src of matches) {
            const full = cleanImageUrl(src);
            if (isDetailImage(full) && !imgs.includes(full)) {
                imgs.push(full);
            }
        }
    }

    return imgs;
}
"""
