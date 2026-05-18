# -*- coding: utf-8 -*-
"""Task runner helpers for CLI commands."""

from typing import Any

from models.context import BrowserContext, ContextStatus
from models.task import Task, TaskStatus
from services.task_runner import _get_runner


async def run_task_once(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    runner = _get_runner(task.task_type)
    await task.start()
    try:
        result = await runner(task, ctx)
        refreshed = await Task.get(id=task.id)
        if refreshed.status not in [
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.COMPLETED.value,
            TaskStatus.PENDING.value,
        ]:
            await refreshed.complete(result or {"message": "Task completed"})
        return result
    except Exception as exc:
        await task.fail(str(exc))
        raise
    finally:
        ctx.status = ContextStatus.LOGGED_IN.value
        await ctx.save()
