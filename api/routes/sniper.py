# -*- coding: utf-8 -*-
"""任务管理 API"""

from sanic import Blueprint, Request
from sanic.response import json

from models.task import Task, TaskStatus
from models.context import BrowserContext, ContextStatus
from models.product_link import ProductLink
from services.task_runner import cancel_running_task, dispatch_task

sniper_bp = Blueprint("sniper", url_prefix="/api/tasks")
products_bp = Blueprint("products", url_prefix="/api/products")


def _normalize_schedule(raw_schedule) -> str:
    """归一化定时规则。当前只支持空值、预设名和秒数。"""
    if raw_schedule is None:
        return ""
    schedule = str(raw_schedule).strip()
    if not schedule:
        return ""
    aliases = {
        "hourly": "3600",
        "every_hour": "3600",
        "every_5_hours": "18000",
        "daily": "86400",
    }
    schedule = aliases.get(schedule, schedule)
    if not schedule.isdigit() or int(schedule) <= 0:
        raise ValueError("schedule must be empty or positive seconds")
    return schedule


@sniper_bp.post("/")
async def create_task(request: Request):
    """创建任务"""
    data = request.json or {}
    task_type = data.get("task_type")
    context_id = data.get("context_id")
    params = data.get("params", {})
    try:
        schedule = _normalize_schedule(data.get("schedule") or params.get("schedule"))
    except ValueError as e:
        return json({"success": False, "error": str(e)}, status=400)

    if not task_type:
        return json({"success": False, "error": "task_type is required"}, status=400)

    # 检查上下文（如果需要）
    if context_id:
        claimed = await BrowserContext.filter(
            id=context_id,
            status=ContextStatus.LOGGED_IN.value,
        ).update(status=ContextStatus.IN_USE.value)
        if not claimed:
            exists = await BrowserContext.filter(id=context_id).first()
            if not exists:
                return json({"success": False, "error": "Context not found"}, status=404)
            return json({"success": False, "error": f"Context status is {exists.status}, expected logged_in"}, status=400)
        ctx = await BrowserContext.get(id=context_id)

    task = await Task.create(
        source="api",
        source_id="default",
        task_type=task_type,
        context_id=context_id,
        params=params,
        schedule=schedule,
    )

    # 如果有上下文，后台执行任务
    if context_id:
        try:
            await dispatch_task(task, ctx)
        except ValueError as e:
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()
            await task.fail(str(e))
            return json({"success": False, "error": str(e)}, status=400)

    return json({
        "success": True,
        "data": {
            "task_id": str(task.id),
            "task_type": task_type,
            "context_id": context_id,
            "status": task.status,
            "schedule": task.schedule,
        }
    })


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

    if task.status not in [TaskStatus.PENDING.value, TaskStatus.RUNNING.value, TaskStatus.WAITING_HUMAN_INPUT.value]:
        return json({"success": False, "error": f"Cannot cancel task with status {task.status}"}, status=400)

    await task.cancel()

    # 取消正在运行的 asyncio 任务
    cancel_running_task(task_id)

    # 释放上下文
    if task.context_id:
        try:
            ctx = await BrowserContext.get(id=task.context_id)
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()
        except Exception:
            pass

    return json({"success": True, "data": {"task_id": str(task.id), "status": "cancelled"}})


@sniper_bp.post("/<task_id:str>/retry")
async def retry_task(request: Request, task_id: str):
    """断点续跑：复用同一个 task，跳过已采集的链接继续"""
    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)

    if task.status not in [TaskStatus.FAILED.value, TaskStatus.CANCELLED.value]:
        return json({"success": False, "error": f"Cannot retry task with status {task.status}"}, status=400)

    if not task.context_id:
        return json({"success": False, "error": "Task has no context_id"}, status=400)

    # 检查并锁定上下文
    claimed = await BrowserContext.filter(
        id=task.context_id,
        status=ContextStatus.LOGGED_IN.value,
    ).update(status=ContextStatus.IN_USE.value)
    if not claimed:
        ctx = await BrowserContext.filter(id=task.context_id).first()
        if not ctx:
            return json({"success": False, "error": "Context not found"}, status=404)
        return json({"success": False, "error": f"Context status is {ctx.status}, expected logged_in"}, status=400)
    ctx = await BrowserContext.get(id=task.context_id)

    try:
        await dispatch_task(task, ctx, reset=True, clear_logs=True)
    except ValueError as e:
        ctx.status = ContextStatus.LOGGED_IN.value
        await ctx.save()
        return json({"success": False, "error": str(e)}, status=400)

    return json({"success": True, "data": {"task_id": str(task.id), "status": "pending"}})


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


@sniper_bp.get("/<task_id:str>/results")
async def get_results(request: Request, task_id: str):
    """获取任务采集的商品链接（分页）"""
    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)

    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 100))
    platform = request.args.get("platform")
    keyword = request.args.get("keyword")

    query = ProductLink.filter(task_id=task_id)
    if platform:
        query = query.filter(platform=platform)
    if keyword:
        query = query.filter(keyword=keyword)

    total = await query.count()
    items = await query.order_by("created_at").offset(offset).limit(limit)

    return json({
        "success": True,
        "data": {
            "items": [item.to_dict() for item in items],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    })


@products_bp.get("/")
@sniper_bp.get("/products/")
async def list_products(request: Request):
    """全局商品链接查询（跨任务）"""
    offset = int(request.args.get("offset", 0))
    limit = min(int(request.args.get("limit", 50)), 200)
    platform = request.args.get("platform")
    keyword = request.args.get("keyword")
    task_id = request.args.get("task_id")

    query = ProductLink.all()
    if platform:
        query = query.filter(platform=platform)
    if keyword:
        query = query.filter(keyword__icontains=keyword)
    if task_id:
        query = query.filter(task_id=task_id)

    total = await query.count()
    items = await query.order_by("-created_at").offset(offset).limit(limit)

    return json({
        "success": True,
        "data": {
            "items": [item.to_dict() for item in items],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    })


@sniper_bp.get("/<task_id:str>/results/download")
async def download_results(request: Request, task_id: str):
    """下载任务采集的全部商品链接为 JSON 文件"""
    import json as json_mod
    from sanic.response import raw

    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)

    platform = request.args.get("platform")
    keyword = request.args.get("keyword")
    query = ProductLink.filter(task_id=task_id)
    if platform:
        query = query.filter(platform=platform)
    if keyword:
        query = query.filter(keyword=keyword)

    items = await query.order_by("created_at")
    data = json_mod.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2)

    filename = f"{task.task_type}_{task_id[:8]}.json"
    return raw(
        data.encode("utf-8"),
        content_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
