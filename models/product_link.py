# -*- coding: utf-8 -*-
"""商品链接模型 - 独立存储每条采集到的商品链接，结构化字段便于查询和同步"""

import uuid
from enum import Enum

from tortoise.models import Model
from tortoise.fields import (
    CharField, IntField, DatetimeField, TextField, UUIDField, JSONField,
)


class ProductLinkSourceType(str, Enum):
    KEYWORD_SEARCH = "keyword_search"
    CSV_IMPORT = "csv_import"
    MANUAL = "manual"


class ProductLinkMonitorStatus(str, Enum):
    CANDIDATE = "candidate"
    MONITORED = "monitored"
    IGNORED = "ignored"
    INVALID = "invalid"


class ProductLink(Model):
    """
    商品链接 - 每条采集到的商品独立一行

    设计理念：
    - task.result 只存元信息（总数、关键词等），不再存完整列表
    - 每条商品链接独立存到此表，结构化字段便于查询、去重、同步
    """

    id = UUIDField(pk=True, default=uuid.uuid4, description="唯一标识")
    task_id = UUIDField(description="最近一次关联的任务ID")

    # 渠道和搜索信息
    platform = CharField(50, description="采集渠道：taobao, xiaohongshu 等")
    keyword = CharField(200, default="", description="搜索关键词")
    page = IntField(default=0, description="采集页码")
    source_type = CharField(
        50,
        default=ProductLinkSourceType.KEYWORD_SEARCH.value,
        description="链接来源：keyword_search, csv_import, manual",
    )
    monitor_status = CharField(
        50,
        default=ProductLinkMonitorStatus.CANDIDATE.value,
        description="监控状态：candidate, monitored, ignored, invalid",
    )
    tags = JSONField(default=list, description="标签缓存，可由飞书同步")

    # 商品基本信息
    url = TextField(description="商品链接（标准化后的）")
    raw_url = TextField(default="", description="原始链接")
    title = CharField(500, default="", description="商品标题")
    price = CharField(50, default="", description="价格")
    original_price = CharField(50, default="", description="原价")
    sales = CharField(100, default="", description="销量")

    # 店铺信息
    shop_name = CharField(200, default="", description="店铺名称")
    shop_url = TextField(default="", description="店铺链接")

    # 图片
    image = TextField(default="", description="头图 URL")
    main_images = JSONField(default=list, description="主图列表")

    # 详情
    location = CharField(200, default="", description="发货地")
    sku_info = JSONField(default=list, description="SKU 信息")
    detail_images = JSONField(default=list, description="详情图列表")

    # 同步状态（后续飞书同步用）
    synced = IntField(default=0, description="是否已同步：0 未同步, 1 已同步")

    created_at = DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:
        table = "product_links"
        unique_together = [("url",)]
        indexes = [
            ("task_id",),
            ("platform",),
            ("keyword",),
            ("source_type",),
            ("monitor_status",),
            ("synced",),
        ]

    @classmethod
    async def upsert_bulk(cls, links: list["ProductLink"]):
        """批量写入：按 URL 全局去重。已有则更新，否则创建。"""
        if not links:
            return
        urls = [l.url for l in links]
        existing = await cls.filter(url__in=urls)
        existing_map = {l.url: l for l in existing}

        update_fields = [
            "task_id",
            "platform",
            "title",
            "price",
            "sales",
            "shop_name",
            "image",
            "main_images",
            "keyword",
            "page",
            "raw_url",
            "source_type",
            "monitor_status",
            "tags",
        ]
        to_create = []
        to_update = []
        for link in links:
            if link.url in existing_map:
                old = existing_map[link.url]
                for f in update_fields:
                    new_val = getattr(link, f)
                    if new_val:
                        setattr(old, f, new_val)
                to_update.append(old)
            else:
                to_create.append(link)

        if to_create:
            await cls.bulk_create(to_create)
        if to_update:
            await cls.bulk_update(to_update, update_fields)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "platform": self.platform,
            "keyword": self.keyword,
            "page": self.page,
            "source_type": self.source_type,
            "monitor_status": self.monitor_status,
            "tags": self.tags,
            "url": self.url,
            "raw_url": self.raw_url,
            "title": self.title,
            "price": self.price,
            "original_price": self.original_price,
            "sales": self.sales,
            "shop_name": self.shop_name,
            "shop_url": self.shop_url,
            "image": self.image,
            "main_images": self.main_images,
            "location": self.location,
            "sku_info": self.sku_info,
            "detail_images": self.detail_images,
            "synced": self.synced,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
