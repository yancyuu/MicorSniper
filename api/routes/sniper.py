# -*- coding: utf-8 -*-
"""任务管理 API"""

from sanic import Blueprint, Request
from sanic.response import json
import asyncio

from models.task import Task, TaskStatus
from models.context import BrowserContext, ContextStatus
from services.task_service import TaskService
from utils.logger import logger
from config.settings import global_settings

sniper_bp = Blueprint("sniper", url_prefix="/api/tasks")


@sniper_bp.post("/")
async def create_task(request: Request):
    """创建任务"""
    data = request.json or {}
    task_type = data.get("task_type")
    context_id = data.get("context_id")
    params = data.get("params", {})

    if not task_type:
        return json({"success": False, "error": "task_type is required"}, status=400)

    # 检查上下文（如果需要）
    if context_id:
        try:
            ctx = await BrowserContext.get(id=context_id)
        except Exception:
            return json({"success": False, "error": "Context not found"}, status=404)

        if not ctx.is_available():
            return json({"success": False, "error": f"Context status is {ctx.status}, expected logged_in"}, status=400)

        # 锁定上下文
        ctx.status = ContextStatus.IN_USE.value
        await ctx.save()

    task = await Task.create(
        source="api",
        source_id="default",
        task_type=task_type,
        context_id=context_id,
        params=params,
    )

    # 如果有上下文，后台执行任务
    if context_id and task_type == "taobao_link_search":
        from scripts.taobao_link_search import run_taobao_link_search
        asyncio.create_task(_run_task_with_context(str(task.id), str(ctx.id), run_taobao_link_search, params))
    elif context_id and task_type == "taobao_keyword_search":
        from scripts.taobao_search import run_taobao_search
        asyncio.create_task(_run_task_with_context(str(task.id), str(ctx.id), run_taobao_search, params))

    return json({
        "success": True,
        "data": {
            "task_id": str(task.id),
            "task_type": task_type,
            "context_id": context_id,
            "status": task.status,
        }
    })


async def _run_task_with_context(task_id: str, ctx_id: str, coro_fn, params: dict):
    """带上下文的任务执行器"""
    task = await Task.get(id=task_id)
    ctx = await BrowserContext.get(id=ctx_id)
    try:
        await task.start()
        result = await coro_fn(task, ctx)
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
        ctx.status = ContextStatus.LOGGED_IN.value
        await ctx.save()


@sniper_bp.get("/")
async def list_tasks(request: Request):
    """获取任务列表"""
    limit = int(request.args.get("limit", 20))
    status = request.args.get("status")
    task_type = request.args.get("task_type")

    query = Task.all()
    if status:
        query = query.filter(status=status)
    if task_type:
        query = query.filter(task_type=task_type)

    tasks = await query.order_by("-created_at").limit(limit)
    return json({
        "success": True,
        "data": [t.to_agent_readable() for t in tasks],
    })


@sniper_bp.get("/<task_id:str>")
async def get_task(request: Request, task_id: str):
    """获取任务详情"""
    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)
    return json({"success": True, "data": task.to_agent_readable()})


@sniper_bp.post("/<task_id:str>/cancel")
async def cancel_task(request: Request, task_id: str):
    """取消任务"""
    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)

    if task.status not in [TaskStatus.PENDING.value, TaskStatus.RUNNING.value]:
        return json({"success": False, "error": f"Cannot cancel task with status {task.status}"}, status=400)

    await task.cancel()

    # 释放上下文
    if task.context_id:
        try:
            ctx = await BrowserContext.get(id=task.context_id)
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()
        except Exception:
            pass

    return json({"success": True, "data": {"task_id": str(task.id), "status": "cancelled"}})


@sniper_bp.get("/<task_id:str>/logs")
async def get_logs(request: Request, task_id: str):
    """获取任务日志"""
    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)

    offset = int(request.args.get("offset", 0))
    logs = task.logs[offset:]
    return json({
        "success": True,
        "data": {
            "logs": logs,
            "total": len(task.logs),
        }
    })


@sniper_bp.get("/<task_id:str>/screenshot")
async def get_screenshot(request: Request, task_id: str):
    """获取任务最新截图"""
    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)

    return json({
        "success": True,
        "data": {"screenshot_url": task.screenshot_url}
    })
