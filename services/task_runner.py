# -*- coding: utf-8 -*-
"""任务执行分发器：路由、定时器和重试共用同一套运行入口。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from models.context import BrowserContext, ContextStatus
from models.task import Task, TaskStatus
from utils.logger import logger

TaskRunner = Callable[[Task, BrowserContext], Awaitable[dict[str, Any] | None]]

_running_asyncio_tasks: dict[str, asyncio.Task] = {}


async def _context_has_active_work(ctx_id: str, exclude_task_id: str | None = None) -> bool:
    active_statuses = [TaskStatus.RUNNING.value, TaskStatus.WAITING_HUMAN_INPUT.value]
    running_exists = await Task.filter(context_id=ctx_id, status__in=active_statuses).exclude(id=exclude_task_id).exists()
    if running_exists:
        return True
    pending_immediate_exists = (
        await Task.filter(context_id=ctx_id, status=TaskStatus.PENDING.value, not_before_at=None)
        .exclude(id=exclude_task_id)
        .exists()
    )
    return pending_immediate_exists


def get_running_task(task_id: str) -> asyncio.Task | None:
    task = _running_asyncio_tasks.get(task_id)
    if task and task.done():
        _running_asyncio_tasks.pop(task_id, None)
        return None
    return task


def cancel_running_task(task_id: str) -> bool:
    task = _running_asyncio_tasks.pop(task_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


def _get_runner(task_type: str) -> TaskRunner:
    if task_type == "taobao_link_search":
        from scripts.taobao_link_search import run_taobao_link_search
        from services.keyword_search import wrap_keyword_search
        return wrap_keyword_search("taobao", run_taobao_link_search)
    if task_type == "jd_link_search":
        from scripts.jd_link_search import run_jd_link_search
        from services.keyword_search import wrap_keyword_search
        return wrap_keyword_search("jd", run_jd_link_search)
    if task_type == "tmall_link_search":
        from scripts.tmall_link_search import run_tmall_link_search
        from services.keyword_search import wrap_keyword_search
        return wrap_keyword_search("tmall", run_tmall_link_search)
    if task_type == "taobao_keyword_search":
        from scripts.taobao_search import run_taobao_search

        return run_taobao_search
    if task_type == "search_by_urls":
        from scripts.search_by_urls import run_search_by_urls

        return run_search_by_urls
    if task_type == "product_detail_fetch":
        from scripts.product_detail_fetch import run_product_detail_fetch

        return run_product_detail_fetch

    raise ValueError(f"Unknown task type: {task_type}")


async def prepare_task_for_run(task: Task, clear_logs: bool = False) -> None:
    task.status = TaskStatus.PENDING.value
    task.error = None
    task.progress = 0
    task.result = None
    task.screenshot_url = ""
    task.browser_url = ""
    task.started_at = None
    task.completed_at = None
    task.not_before_at = None
    params = task.params or {}
    if "current_offset" in params:
        params["current_offset"] = 0
    if "batch_index" in params:
        params["batch_index"] = 1
    task.params = params
    if clear_logs:
        task.logs = []
    task._normalize_datetime_fields()
    await task.save()


async def dispatch_task(task: Task, ctx: BrowserContext, reset: bool = False, clear_logs: bool = False) -> bool:
    """启动后台任务。调用方需先抢占/锁定浏览器上下文。"""
    task_id = str(task.id)
    if get_running_task(task_id):
        return False

    runner = _get_runner(task.task_type)
    if reset:
        await prepare_task_for_run(task, clear_logs=clear_logs)

    async_task = asyncio.create_task(_run_task_with_context(task_id, str(ctx.id), runner))
    _running_asyncio_tasks[task_id] = async_task
    return True


async def _run_task_with_context(task_id: str, ctx_id: str, runner: TaskRunner) -> None:
    """带上下文的任务执行器。"""
    task = await Task.get(id=task_id)
    ctx = await BrowserContext.get(id=ctx_id)
    try:
        await task.start()
        result = await runner(task, ctx)
        task = await Task.get(id=task_id)
        if task.status == TaskStatus.PENDING.value and task.not_before_at:
            return
        if task.status not in [
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.COMPLETED.value,
        ]:
            await task.complete(result or {"message": "Task completed"})
    except asyncio.CancelledError:
        await task.cancel()
    except Exception as e:
        logger.error(f"Task {task_id} failed: {e}")
        await task.fail(str(e))
    finally:
        _running_asyncio_tasks.pop(task_id, None)

        now = datetime.now()
        task = await Task.get(id=task_id)
        if task.schedule:
            task.last_run_at = now
        task._normalize_datetime_fields()
        await task.save()

        if not await _context_has_active_work(str(ctx.id), task_id):
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()
