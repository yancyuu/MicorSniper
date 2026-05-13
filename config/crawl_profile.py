# -*- coding: utf-8 -*-
"""爬虫速率配置 — 所有平台的延迟、批次参数集中管理。修改此文件即可全局调优。"""

_PROFILES: dict[str, dict] = {
    "default": {
        # 详情页滚动（每步间隔 / 最终回顶）
        "detail_scroll_delay": 0.8,
        "detail_scroll_final": 1.0,
        # 详情抓取批次
        "detail_batch_size": 8,
        "detail_batch_interval": 5,
        "detail_batch_min": 5,
        "detail_batch_max": 8,
        # 非保守平台（淘宝/天猫）的详情批次
        "detail_fast_batch_min": 8,
        "detail_fast_batch_max": 12,
        # 关键字搜索
        "search_scroll_delay": 0.8,
        "search_next_page_delay": 5,
        "search_sort_delay": 5,
        "search_fallback_scroll_delay": 4,
        "search_sort_retry_delay": 2,
        "search_pages_per_batch": 5,
        "search_batch_interval": 5,
        # 价格监控批次
        "price_batch_size": 80,
        "price_batch_interval": 15,
        # 批次间隔随机抖动（分钟）
        "batch_jitter_minutes": 10,
    },
    "jd": {
        "detail_scroll_delay": 1.8,
        "detail_scroll_final": 2.0,
        "detail_batch_size": 6,
        "detail_batch_interval": 8,
        "detail_batch_min": 5,
        "detail_batch_max": 8,
        "search_scroll_delay": 1.8,
        "search_next_page_delay": 6,
        "search_sort_delay": 6,
        "search_fallback_scroll_delay": 5,
        "search_sort_retry_delay": 3,
        "price_batch_size": 30,
        "price_batch_interval": 20,
    },
    "taobao": {
        "detail_scroll_delay": 0.8,
        "detail_scroll_final": 1.0,
        "price_batch_size": 120,
        "price_batch_interval": 15,
    },
    "tmall": {},  # 同 taobao，由 get_profile 自动继承
}


def get_profile(platform: str) -> dict:
    """获取平台爬虫参数，未定义的 key 自动从 default 继承。"""
    base = _PROFILES["default"]
    override = _PROFILES.get(platform, {})
    return {**base, **override}
