# -*- coding: utf-8 -*-
"""情报库链接模型 - 存储情报通导入的商品数据。"""

import uuid

from tortoise.fields import (
    CharField, DatetimeField, IntField, JSONField, TextField, UUIDField,
)
from tortoise.models import Model

from models.product_link import ProductLink, ProductLinkMonitorStatus, ProductLinkSourceType


class IntelLink(Model):
    """情报库链接 - 情报通导入的商品数据。"""

    id = UUIDField(pk=True, default=uuid.uuid4)
    task_id = UUIDField(description="最近一次关联的任务ID")

    # 情报通源信息
    intel_url = TextField(default="", description="情报通源链接")
    rank = IntField(default=0, description="情报通排名")
    intel_date = CharField(20, default="", description="情报通数据日期，如 2024-01")
    category = CharField(500, default="", description="情报通分类路径，如 个人护理»暖宫带")

    # 商品基本信息
    platform = CharField(50, description="电商平台：taobao, tmall, jd 等")
    url = TextField(description="商品链接（标准化后的）")
    raw_url = TextField(default="", description="原始商品链接")
    title = CharField(500, default="", description="商品标题")
    price = CharField(50, default="", description="售价")
    original_price = CharField(50, default="", description="原价")
    sales = CharField(100, default="", description="销量")

    # 增长数据
    revenue = CharField(100, default="", description="销售额")
    sales_growth = CharField(50, default="", description="销量增长率，如 75.71%")
    revenue_growth = CharField(50, default="", description="销售额增长率，如 436.2%")

    # 品牌/店铺
    brand = CharField(200, default="", description="品牌")
    shop_name = CharField(200, default="", description="店铺名称")
    location = CharField(200, default="", description="发货地，如 广东东莞")

    # 详情抓取填充（初始为空，巡检后补充）
    image = TextField(default="", description="头图 URL")
    main_images = JSONField(default=list, description="主图列表")
    sku_info = JSONField(default=list, description="SKU 信息")
    detail_images = JSONField(default=list, description="详情图列表")

    # 任务状态
    monitor_status = CharField(50, default=ProductLinkMonitorStatus.MONITORED.value)
    lock_task_id = UUIDField(null=True, description="当前锁定该链接的任务ID")
    locked_at = DatetimeField(null=True, description="链接被任务锁定的时间")
    created_at = DatetimeField(auto_now_add=True)

    class Meta:
        table = "intel_links"
        unique_together = [("url",)]
        indexes = [
            ("task_id",),
            ("platform",),
            ("monitor_status",),
            ("lock_task_id",),
            ("intel_date",),
            ("category",),
        ]

    @classmethod
    def canonicalize_url(cls, url: str) -> str:
        return ProductLink.canonicalize_url(url)

    @classmethod
    async def upsert_bulk(cls, links: list["IntelLink"]):
        if not links:
            return
        _CHUNK = 200
        for link in links:
            link.raw_url = link.raw_url or link.url
            link.url = cls.canonicalize_url(link.url)
        urls = [l.url for l in links]

        existing_map: dict[str, "IntelLink"] = {}
        for i in range(0, len(urls), _CHUNK):
            chunk = urls[i : i + _CHUNK]
            for obj in await cls.filter(url__in=chunk):
                existing_map[obj.url] = obj

        update_fields = [
            "task_id", "platform", "title", "price", "original_price", "sales",
            "revenue", "sales_growth", "revenue_growth", "brand", "shop_name",
            "location", "intel_url", "rank", "intel_date", "category",
            "image", "main_images", "monitor_status",
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

        for i in range(0, len(to_create), _CHUNK):
            await cls.bulk_create(to_create[i : i + _CHUNK])
        for i in range(0, len(to_update), _CHUNK):
            await cls.bulk_update(to_update[i : i + _CHUNK], update_fields)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "platform": self.platform,
            "monitor_status": self.monitor_status,
            "url": self.url,
            "raw_url": self.raw_url,
            "title": self.title,
            "price": self.price,
            "original_price": self.original_price,
            "sales": self.sales,
            "revenue": self.revenue,
            "sales_growth": self.sales_growth,
            "revenue_growth": self.revenue_growth,
            "brand": self.brand,
            "shop_name": self.shop_name,
            "location": self.location,
            "intel_url": self.intel_url,
            "rank": self.rank,
            "intel_date": self.intel_date,
            "category": self.category,
            "image": self.image,
            "main_images": self.main_images,
            "sku_info": self.sku_info,
            "detail_images": self.detail_images,
            "lock_task_id": str(self.lock_task_id) if self.lock_task_id else None,
            "locked_at": self.locked_at.isoformat() if self.locked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
