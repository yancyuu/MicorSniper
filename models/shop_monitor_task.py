# -*- coding: utf-8 -*-
"""店铺监控任务模型"""

import uuid
from enum import Enum

from tortoise.models import Model
from tortoise.fields import (
    CharField, IntField, BooleanField, DatetimeField, TextField, UUIDField, JSONField,
)


class MonitorFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"


class ShopMonitorTask(Model):
    """
    店铺监控任务

    用户配置监控目标后，系统定期检查店铺的新品上架、价格变动，
    并将检测结果记录到 MonitorRecord。
    """

    id = UUIDField(pk=True, default=uuid.uuid4, description="唯一标识")
    name = CharField(200, description="任务名称")
    shop_url = TextField(description="店铺主页 URL")
    platform = CharField(
        50,
        description="平台：taobao / tmall / jd / xiaohongshu",
    )
    monitor_types = JSONField(
        default=list,
        description="监控类型列表：['price', 'stock', 'new_arrival']",
    )
    frequency = CharField(
        20,
        default=MonitorFrequency.DAILY.value,
        description="检查频率：daily / weekly",
    )
    is_enabled = BooleanField(default=True, description="是否启用")
    last_run_at = DatetimeField(null=True, description="上次执行时间")
    last_run_status = CharField(20, default="", description="上次执行状态")
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        table = "shop_monitor_tasks"
        indexes = [
            ("platform",),
            ("is_enabled",),
        ]

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "shop_url": self.shop_url,
            "platform": self.platform,
            "monitor_types": self.monitor_types,
            "frequency": self.frequency,
            "is_enabled": self.is_enabled,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_run_status": self.last_run_status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }