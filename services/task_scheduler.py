# -*- coding: utf-8 -*-
"""应用内定时任务循环。"""

import asyncio
from datetime import datetime

from models.context import BrowserContext, ContextStatus
from models.task import Task, TaskStatus
from services.task_runner import dispatch_task
from utils.logger import logger

SCAN_INTERVAL_SECONDS = 30
ACTIVE_STATUSES = [
    TaskStatus.PENDING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.WAITING_HUMAN_INPUT.value,
]


def parse_schedule_seconds(schedule: str | None) -> int | None:
    if not schedule:
        return None
    schedule = str(schedule).strip()
    if not schedule.isdigit():
        return None
    seconds = int(schedule)
    return seconds if seconds > 0 else None


def is_due(task: Task, now: datetime) -> bool:
    interval = parse_schedule_seconds(task.schedule)
    if not interval:
        return False

    anchor = task.last_run_at or task.completed_at or task.created_at
    if not anchor:
        return True
    return (now - anchor).total_seconds() >= interval


async def scheduled_task_loop() -> None:
    logger.info("Scheduled task loop started")
    try:
        while True:
            try:
                await scan_and_dispatch_due_tasks()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Scheduled task loop scan failed: {e}")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Scheduled task loop stopped")
        raise


async def scan_and_dispatch_due_tasks() -> int:
    now = datetime.now()
    dispatched = await scan_and_dispatch_queued_tasks(now)

    tasks = await (
        Task.exclude(schedule="")
        .exclude(status__in=ACTIVE_STATUSES)
        .order_by("last_run_at", "created_at")
        .limit(50)
    )

    for task in tasks:
        if not is_due(task, now):
            continue
        if await try_dispatch_scheduled_task(task, now):
            dispatched += 1
    return dispatched


async def scan_and_dispatch_queued_tasks(now: datetime) -> int:
    tasks = await (
        Task.filter(status=TaskStatus.PENDING.value, not_before_at__lte=now)
        .exclude(not_before_at=None)
        .order_by("not_before_at", "created_at")
        .limit(50)
    )
    dispatched = 0
    for task in tasks:
        if await try_dispatch_queued_task(task):
            dispatched += 1
    return dispatched


async def try_dispatch_queued_task(task: Task) -> bool:
    if not task.context_id:
        return False
    ctx_claimed = await BrowserContext.filter(
        id=task.context_id,
        status=ContextStatus.LOGGED_IN.value,
    ).update(status=ContextStatus.IN_USE.value)
    if not ctx_claimed:
        logger.info(f"Queued task {task.id} skipped: context is not available")
        return False
    ctx = await BrowserContext.get(id=task.context_id)
    ok = await dispatch_task(task, ctx)
    if not ok:
        await BrowserContext.filter(id=ctx.id).update(status=ContextStatus.LOGGED_IN.value)
        return False
    logger.info(f"Queued task {task.id} dispatched")
    return True


async def try_dispatch_scheduled_task(task: Task, now: datetime) -> bool:
    if not task.context_id:
        logger.warning(f"Scheduled task {task.id} skipped: missing context_id")
        return False

    original_status = task.status
    claimed = await (
        Task.filter(id=task.id)
        .filter(status=original_status)
        .exclude(status__in=ACTIVE_STATUSES)
        .update(status=TaskStatus.PENDING.value)
    )
    if not claimed:
        return False

    ctx_claimed = await BrowserContext.filter(
        id=task.context_id,
        status=ContextStatus.LOGGED_IN.value,
    ).update(status=ContextStatus.IN_USE.value)
    if not ctx_claimed:
        await Task.filter(id=task.id, status=TaskStatus.PENDING.value).update(status=original_status)
        logger.info(f"Scheduled task {task.id} skipped: context is not available")
        return False

    task = await Task.get(id=task.id)
    ctx = await BrowserContext.get(id=task.context_id)
    try:
        ok = await dispatch_task(task, ctx, reset=True, clear_logs=True)
        if not ok:
            await Task.filter(id=task.id, status=TaskStatus.PENDING.value).update(status=original_status)
            await BrowserContext.filter(id=ctx.id).update(status=ContextStatus.LOGGED_IN.value)
            return False
        logger.info(f"Scheduled task {task.id} dispatched")
        return True
    except Exception as e:
        await Task.filter(id=task.id, status=TaskStatus.PENDING.value).update(status=original_status)
        await BrowserContext.filter(id=ctx.id).update(status=ContextStatus.LOGGED_IN.value)
        logger.error(f"Scheduled task {task.id} dispatch failed: {e}")
        return False
