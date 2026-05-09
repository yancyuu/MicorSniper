# -*- coding: utf-8 -*-
"""商品详情 provider service 工厂。"""

from .base import ProductDetailService
from .generic import GenericProductDetailService
from .jd import JdProductDetailService
from .taobao import TaobaoProductDetailService
from .tmall import TmallProductDetailService


_PROVIDERS: dict[str, type[ProductDetailService]] = {
    "taobao": TaobaoProductDetailService,
    "tmall": TmallProductDetailService,
    "jd": JdProductDetailService,
}


def get_provider_service(platform: str) -> ProductDetailService:
    """按平台名称返回商品详情提取服务，未知平台使用兜底服务。"""
    service_cls = _PROVIDERS.get(platform, GenericProductDetailService)
    return service_cls()
