# -*- coding: utf-8 -*-
"""关键词搜索 provider 工厂。"""

from .base import KeywordSearchService
from .jd import JdKeywordSearchService
from .taobao import TaobaoKeywordSearchService
from .tmall import TmallKeywordSearchService
from .xiaohongshu import XiaohongshuKeywordSearchService

_PROVIDERS: dict[str, type[KeywordSearchService]] = {
    "taobao": TaobaoKeywordSearchService,
    "tmall": TmallKeywordSearchService,
    "jd": JdKeywordSearchService,
    "xiaohongshu": XiaohongshuKeywordSearchService,
}


def get_keyword_search_service(platform: str) -> KeywordSearchService:
    """按平台名称返回关键词搜索 service，未知平台抛出 KeyError。"""
    service_cls = _PROVIDERS[platform]
    return service_cls()
