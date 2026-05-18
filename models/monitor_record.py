# -*- coding: utf-8 -*-
"""监控记录模型 — 存储每次检查的结果（新品、价格、库存）"""

import uuid

from tortoise.models import Model
from tortoise.fields import (
    CharField, IntField, DatetimeField, TextField, UUIDField,
)


class MonitorRecord(Model):
    """
    监控记录 — 每次检查店铺时发现的变化

    设计：price_history = 按 product_url 聚合的 MonitorRecord，
    无需独立表，按 product_url + detected_at 排序即可查询价格走势。
    """

    id = UUIDField(pk=True, default=uuid.uuid4, description="唯一标识")
    task_id = UUIDField(description="关联的监控任务 ID")

    # 商品信息
    product_url = TextField(description="商品链接")
    product_title = CharField(500, default="", description="商品标题")
    image = TextField(default="", description="商品图片")
    price = CharField(50, default="", description="检测时的价格")
    price_changed = IntField(default=0, description="价格是否变动：0 无变化，1 上涨，-1 下跌")
    is_new = IntField(default=0, description="是否新品：0 否，1 是")
    detected_at = DatetimeField(auto_now_add=True, description="检测时间")

    class Meta:
        table = "monitor_records"
        indexes = [
            ("task_id",),
            ("product_url",),
            ("detected_at",),
            ("is_new",),
        ]

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "product_url": self.product_url,
            "product_title": self.product_title,
            "image": self.image,
            "price": self.price,
            "price_changed": self.price_changed,
            "is_new": self.is_new,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
        }