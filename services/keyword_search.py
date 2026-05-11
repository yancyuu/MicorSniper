# -*- coding: utf-8 -*-
"""关键词搜索批次编排：管理分批状态（not_before_at、PENDING 状态切换）。
脚本只负责浏览器操作和数据提取，批次间隔和续跑由此层管理。"""

import random
from datetime import datetime, timedelta
from typing import Any

from models.context import BrowserContext
from models.task import Task, TaskStatus
from utils.logger import logger


def wrap_keyword_search(platform: str, script_runner):
    """返回一个适配 task_runner 的 wrapper，在脚本执行后处理分批续跑。

    Args:
        platform: 平台标识 (jd/taobao/tmall)
        script_runner: 脚本 run 函数，签名为 async (task, ctx) -> dict | None
    """

    async def _wrapped(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
        result = await script_runner(task, ctx)

        if result is None:
            return None

        # 脚本已正常返回，判断是否需要分批续跑
        params = task.params or {}
        keywords = _normalize_keywords(params.get("keywords"))
        limit = int(params.get("limit") or 0)
        interval_minutes = int(params.get("batch_interval_minutes") or 5)
        pages_per_batch = int(params.get("pages_per_batch") or 2)

        keyword_index = result.get("keyword_index", 0)
        keyword_count = result.get("keyword_count", 0)
        keyword_done = result.get("keyword_done", True)
        total = result.get("total", 0)

        has_more = False
        if not keyword_done:
            has_more = True
        elif keyword_index < len(keywords) - 1:
            has_more = True
        elif limit and total < limit:
            has_more = True

        if has_more and keyword_count > 0:
            delay = interval_minutes + int(random.uniform(0, 10))
            params["_batch_keyword_index"] = keyword_index if not keyword_done else keyword_index + 1
            params["pages_per_batch"] = pages_per_batch
            params["batch_interval_minutes"] = interval_minutes
            task.params = params
            task.not_before_at = datetime.now() + timedelta(minutes=delay)
            task.status = TaskStatus.PENDING.value
            max_limit = 1000
            task.progress = min(95, int(total / (limit or max_limit) * 100)) if limit else min(95, 5 + total // 10)
            base_step = len(task.logs or [])
            await task.log_step(
                base_step + 100,
                "分批暂停，等待下次执行",
                {"keyword_index": keyword_index, "total": total, "platform": platform},
                {"delay_minutes": delay},
                "completed",
            )
            await task.save()
            return {**result, "queued_next": True}

        return result

    return _wrapped


def _normalize_keywords(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
