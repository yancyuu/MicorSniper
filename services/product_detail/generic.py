# -*- coding: utf-8 -*-
"""通用商品详情兜底提取。"""

from .base import ProductDetailService


GENERIC_DETAIL_JS = """
() => {
    const result = {};
    result.title = document.title || '';
    const m = document.body.innerText.match(/¥\\s*([\\d,.]+)/);
    result.price = m ? m[1] : '';
    result.original_price = '';
    result.sales = '';
    result.shop_name = '';
    result.shop_url = '';
    result.image = '';
    result.main_images = [];
    result.sku_info = [];
    result.location = '';
    result.detail_images = [];
    return result;
}
"""


class GenericProductDetailService(ProductDetailService):
    platform = "generic"
    detail_js = GENERIC_DETAIL_JS
