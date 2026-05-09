# -*- coding: utf-8 -*-
"""商品详情 provider service 基类。"""

from typing import Any


DETAIL_IMAGES_JS = """
() => {
    const imgs = [];
    document.querySelectorAll('#description img, [class*="desc"] img, #detail img, #J-detail-content img').forEach(img => {
        const src = img.getAttribute('data-lazyload') || img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && src.startsWith('http')) imgs.push(src);
    });
    return imgs;
}
"""


class ProductDetailService:
    """给定已打开的商品详情页，提取平台专属商品信息。"""

    platform = "generic"
    detail_js = ""
    detail_images_js = DETAIL_IMAGES_JS

    async def extract(self, page) -> dict[str, Any]:
        """提取商品详情字段，返回 ProductLink 兼容结构。"""
        return await page.evaluate(self.detail_js)

    async def extract_detail_images(self, page) -> list[str]:
        """滚动加载后补采详情图。"""
        return await page.evaluate(self.detail_images_js)
