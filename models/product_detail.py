# -*- coding: utf-8 -*-
"""商品详情模型 - 存储从商品详情页提取的完整产品信息。"""

import uuid

from tortoise.fields import CharField, DatetimeField, JSONField, TextField, UUIDField
from tortoise.models import Model


class ProductDetail(Model):
    """商品详情，与 ProductLink 通过 URL 关联。"""

    id = UUIDField(pk=True, default=uuid.uuid4)
    task_id = UUIDField(description="最近一次详情抓取任务ID")
    product_link_id = UUIDField(null=True, description="关联的商品链接ID")

    platform = CharField(50, description="渠道：taobao, tmall, jd 等")
    url = TextField(description="商品链接")

    title = CharField(500, default="", description="商品标题")
    price = CharField(50, default="", description="价格")
    original_price = CharField(50, default="", description="原价")
    sales = CharField(100, default="", description="销量")

    shop_name = CharField(200, default="", description="店铺名称")
    shop_url = TextField(default="", description="店铺链接")

    image = TextField(default="", description="头图 URL")
    main_images = JSONField(default=list, description="主图列表")
    location = CharField(200, default="", description="发货地")
    sku_info = JSONField(default=list, description="SKU 信息")
    detail_images = JSONField(default=list, description="详情图列表")

    raw = JSONField(default=dict, description="原始提取结果")
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        table = "product_details"
        unique_together = [("url",)]
        indexes = [
            ("task_id",),
            ("product_link_id",),
            ("platform",),
        ]

    @classmethod
    async def upsert_from_info(cls, *, task_id, product_link_id, platform: str, url: str, info: dict):
        existing = await cls.filter(url=url).first()
        data = {
            "task_id": task_id,
            "product_link_id": product_link_id,
            "platform": platform,
            "url": url,
            "title": info.get("title", ""),
            "price": info.get("price", ""),
            "original_price": info.get("original_price", ""),
            "sales": info.get("sales", ""),
            "shop_name": info.get("shop_name", ""),
            "shop_url": info.get("shop_url", ""),
            "image": info.get("image", ""),
            "main_images": info.get("main_images", []),
            "location": info.get("location", ""),
            "sku_info": info.get("sku_info", []),
            "detail_images": info.get("detail_images", []),
            "raw": info,
        }
        if existing:
            for key, value in data.items():
                setattr(existing, key, value)
            await existing.save()
            return existing
        return await cls.create(**data)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "product_link_id": str(self.product_link_id) if self.product_link_id else None,
            "platform": self.platform,
            "url": self.url,
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
            "raw": self.raw,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
