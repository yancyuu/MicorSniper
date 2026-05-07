# -*- coding: utf-8 -*-
"""浏览器上下文模型"""

from enum import Enum
from tortoise.models import Model
from tortoise.fields import (
    CharField, DatetimeField, UUIDField
)
import uuid


class ContextStatus(str, Enum):
    PENDING = "pending"          # 未登录
    LOGGED_IN = "logged_in"      # 已登录
    IN_USE = "in_use"            # 使用中


class BrowserContext(Model):
    id = UUIDField(pk=True, default=uuid.uuid4)
    platform = CharField(20, description="渠道: xiaohongshu / taobao")
    name = CharField(100, default="", description="备注名称")
    context_id = CharField(200, default="", description="AgentBay context ID")
    target_url = CharField(500, default="", description="登录目标网址")
    status = CharField(20, default=ContextStatus.PENDING.value, description="状态")
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        table = "browser_contexts"
        indexes = [
            ("platform", "status"),
        ]

    def is_available(self) -> bool:
        return self.status == ContextStatus.LOGGED_IN.value
