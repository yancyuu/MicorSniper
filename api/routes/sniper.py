# -*- coding: utf-8 -*-
"""任务管理 API"""

from sanic import Blueprint, Request
from sanic.response import json
from tortoise.expressions import Q

from models.task import Task, TaskStatus
from models.context import BrowserContext, ContextStatus
from models.product_detail import ProductDetail
from models.product_link import ProductLink, ProductLinkMonitorStatus, ProductLinkSourceType
from services.task_runner import cancel_running_task, dispatch_task
from services.sniper_tasks import (
    ServiceError,
    create_ecommerce_link_monitor_business_task as create_ecommerce_link_monitor_business_task_service,
    create_keyword_search_business_task as create_keyword_search_business_task_service,
    create_task as create_task_service,
    import_products as import_products_service,
)

sniper_bp = Blueprint("sniper", url_prefix="/api/tasks")
products_bp = Blueprint("products", url_prefix="/api/products")


async def _merge_product_details(links: list[ProductLink]) -> list[dict]:
    if not links:
        return []
    urls = [item.url for item in links]
    details = await ProductDetail.filter(url__in=urls)
    detail_map = {item.url: item.to_dict() for item in details}
    result = []
    for link in links:
        data = link.to_dict()
        detail = detail_map.get(link.url)
        if detail:
            data.update({k: v for k, v in detail.items() if k not in {"id", "task_id", "created_at"}})
            data["detail_id"] = detail["id"]
            data["detail_task_id"] = detail["task_id"]
            data["detail_updated_at"] = detail.get("updated_at")
        result.append(data)
    return result


@sniper_bp.post("/")
async def create_task(request: Request):
    """创建任务"""
    try:
        data = await create_task_service(request.json or {})
    except ServiceError as e:
        return json(e.payload, status=e.status)
    return json({"success": True, "data": data})


@sniper_bp.post("/business/keyword-search")
async def create_keyword_search_business_task(request: Request):
    """业务任务：选择关键词和平台，自动校验上下文并拆分平台搜索任务。"""
    try:
        data = await create_keyword_search_business_task_service(request.json or {})
    except ServiceError as e:
        return json(e.payload, status=e.status)
    return json({"success": True, "data": data})


@sniper_bp.post("/business/ecommerce-link-monitor")
async def create_ecommerce_link_monitor_business_task(request: Request):
    """业务任务：导入电商链接，按平台分组并自动拆分详情监控任务。"""
    try:
        data = await create_ecommerce_link_monitor_business_task_service(request.json or {})
    except ServiceError as e:
        return json(e.payload, status=e.status)
    return json({"success": True, "data": data})


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
        "data": [await t.to_agent_readable() for t in tasks],
    })


@sniper_bp.get("/<task_id:str>")
async def get_task(request: Request, task_id: str):
    """获取任务详情"""
    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)
    return json({"success": True, "data": await task.to_agent_readable()})


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
        params = task.params or {}
        params["current_offset"] = 0
        params["batch_index"] = 1
        task.params = params
        task.progress = 0
        task.result = None
        task.error = None
        task.logs = []
        task.not_before_at = None
        await task.save()
        await dispatch_task(task, ctx, reset=False, clear_logs=False)
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
    from models.intel_link import IntelLink

    task = await Task.filter(id=task_id).first()
    if not task:
        return json({"success": False, "error": "Task not found"}, status=404)

    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 100))
    platform = request.args.get("platform")
    keyword = request.args.get("keyword")

    is_intel = (task.params or {}).get("source") == "intel"
    LinkModel = IntelLink if is_intel else ProductLink
    merge_fn = _merge_intel_details if is_intel else _merge_product_details

    query = LinkModel.filter(task_id=task_id)
    if platform:
        query = query.filter(platform=platform)
    if keyword:
        query = query.filter(title__icontains=keyword)

    total = await query.count()
    items = await query.order_by("created_at").offset(offset).limit(limit)

    return json({
        "success": True,
        "data": {
            "items": await merge_fn(items),
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
    monitor_status = request.args.get("monitor_status")
    sort = request.args.get("sort") or "-created_at"

    query = ProductLink.all()
    if platform:
        query = query.filter(platform=platform)
    if keyword:
        query = query.filter(
            Q(keyword__icontains=keyword)
            | Q(title__icontains=keyword)
            | Q(shop_name__icontains=keyword)
            | Q(url__icontains=keyword)
            | Q(raw_url__icontains=keyword)
            | Q(platform__icontains=keyword)
            | Q(price__icontains=keyword)
            | Q(sales__icontains=keyword)
        )
    if task_id:
        query = query.filter(task_id=task_id)
    if monitor_status:
        query = query.filter(monitor_status=monitor_status)

    total = await query.count()
    if sort == "sales_desc":
        all_items = await query

        def sales_value(item: ProductLink) -> float:
            raw = item.sales or ""
            number = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
            value = float(number or 0)
            return value * 10000 if "万" in raw else value

        items = sorted(all_items, key=sales_value, reverse=True)[offset : offset + limit]
    elif sort == "price_asc":
        items = await query.order_by("price", "-created_at").offset(offset).limit(limit)
    elif sort == "price_desc":
        items = await query.order_by("-price", "-created_at").offset(offset).limit(limit)
    else:
        order_field = sort if sort in ["created_at", "-created_at"] else "-created_at"
        items = await query.order_by(order_field).offset(offset).limit(limit)
    global_total = await ProductLink.all().count()
    candidate_total = await ProductLink.filter(monitor_status=ProductLinkMonitorStatus.CANDIDATE.value).count()
    monitored_total = await ProductLink.filter(monitor_status=ProductLinkMonitorStatus.MONITORED.value).count()
    invalid_total = await ProductLink.filter(monitor_status=ProductLinkMonitorStatus.INVALID.value).count()
    keyword_total = await ProductLink.filter(source_type=ProductLinkSourceType.KEYWORD_SEARCH.value).count()

    return json({
        "success": True,
        "data": {
            "items": await _merge_product_details(items),
            "total": total,
            "offset": offset,
            "limit": limit,
            "stats": {
                "total": global_total,
                "candidate": candidate_total,
                "monitored": monitored_total,
                "invalid": invalid_total,
                "keyword_search": keyword_total,
            },
        }
    })


@products_bp.post("/import")
async def import_products(request: Request):
    """导入外部链接表到商品链接库，并返回本批导入任务 ID。"""
    try:
        data = await import_products_service(request.json or {})
    except ServiceError as e:
        return json(e.payload, status=e.status)
    return json({"success": True, "data": data})


@products_bp.patch("/<link_id:str>/monitor-status")
async def update_product_monitor_status(request: Request, link_id: str):
    """更新链接监控状态。"""
    data = request.json or {}
    status = data.get("monitor_status")
    allowed = {item.value for item in ProductLinkMonitorStatus}
    if status not in allowed:
        return json({"success": False, "error": "Invalid monitor_status"}, status=400)

    updated = await ProductLink.filter(id=link_id).update(monitor_status=status)
    if not updated:
        return json({"success": False, "error": "Product link not found"}, status=404)
    link = await ProductLink.get(id=link_id)
    return json({"success": True, "data": link.to_dict()})


@products_bp.delete("/<link_id:str>")
async def delete_product(request: Request, link_id: str):
    """删除商品链接及其详情。"""
    link = await ProductLink.filter(id=link_id).first()
    if not link:
        return json({"success": False, "error": "Product link not found"}, status=404)
    await ProductDetail.filter(url=link.url).delete()
    await link.delete()
    return json({"success": True})


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

    from models.intel_link import IntelLink

    is_intel = (task.params or {}).get("source") == "intel"
    LinkModel = IntelLink if is_intel else ProductLink
    merge_fn = _merge_intel_details if is_intel else _merge_product_details

    query = LinkModel.filter(task_id=task_id)
    if platform:
        query = query.filter(platform=platform)
    if keyword:
        query = query.filter(title__icontains=keyword)

    items = await query.order_by("created_at")
    if items:
        payload = await merge_fn(items)
    else:
        result = task.result if isinstance(task.result, dict) else {}
        payload = result.get("results") or result.get("items") or []
    data = json_mod.dumps(payload, ensure_ascii=False, indent=2)

    filename = f"{task.task_type}_{task_id[:8]}.json"
    return raw(
        data.encode("utf-8"),
        content_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===== 情报库 API =====

intel_bp = Blueprint("intel", url_prefix="/api/intel")


async def _merge_intel_details(links: list) -> list[dict]:
    if not links:
        return []
    urls = [item.url for item in links]
    details = await ProductDetail.filter(url__in=urls, source="intel")
    detail_map = {item.url: item.to_dict() for item in details}
    result = []
    for link in links:
        data = link.to_dict()
        detail = detail_map.get(link.url)
        if detail:
            data.update({k: v for k, v in detail.items() if k not in {"id", "task_id", "created_at"}})
            data["detail_id"] = detail["id"]
            data["detail_task_id"] = detail["task_id"]
            data["detail_updated_at"] = detail.get("updated_at")
        result.append(data)
    return result


@intel_bp.get("/")
async def list_intel_links(request: Request):
    """情报库链接列表"""
    from models.intel_link import IntelLink

    offset = int(request.args.get("offset", 0))
    limit = min(int(request.args.get("limit", 50)), 200)
    platform = request.args.get("platform")
    keyword = request.args.get("keyword")
    monitor_status = request.args.get("monitor_status")
    sort = request.args.get("sort") or "-created_at"

    query = IntelLink.all()
    if platform:
        query = query.filter(platform=platform)
    if keyword:
        query = query.filter(
            Q(title__icontains=keyword)
            | Q(shop_name__icontains=keyword)
            | Q(url__icontains=keyword)
        )
    if monitor_status:
        query = query.filter(monitor_status=monitor_status)

    total = await query.count()
    allowed_sorts = {
        "created_at", "-created_at",
        "price", "-price",
        "sales", "-sales",
        "rank", "-rank",
        "intel_date", "-intel_date",
    }
    order_field = sort if sort in allowed_sorts else "-created_at"
    items = await query.order_by(order_field).offset(offset).limit(limit)
    global_total = await IntelLink.all().count()

    return json({
        "success": True,
        "data": {
            "items": await _merge_intel_details(items),
            "total": total,
            "offset": offset,
            "limit": limit,
            "stats": {"total": global_total},
        },
    })


@intel_bp.post("/import")
async def import_intel_links(request: Request):
    """导入链接到情报库"""
    import uuid as uuid_mod
    from models.intel_link import IntelLink
    from services.sniper_tasks import _normalize_link_items, detect_platform_from_url

    raw_links = (request.json or {}).get("links") or []
    if not raw_links:
        return json({"success": False, "error": "links is required"}, status=400)

    normalized = _normalize_link_items(raw_links)
    if not normalized:
        return json({"success": False, "error": "No valid links found"}, status=400)

    links = [
        IntelLink(
            task_id=uuid_mod.UUID(int=0),
            platform=item["platform"],
            url=item["url"],
            raw_url=item["url"],
            source_type=ProductLinkSourceType.QBT_IMPORT.value,
            monitor_status=ProductLinkMonitorStatus.MONITORED.value,
        )
        for item in normalized
    ]
    await IntelLink.upsert_bulk(links)

    grouped: dict[str, int] = {}
    for item in normalized:
        grouped[item["platform"]] = grouped.get(item["platform"], 0) + 1

    return json({"success": True, "data": {"total": len(normalized), "platforms": grouped}})


@intel_bp.delete("/<link_id:str>")
async def delete_intel_link(request: Request, link_id: str):
    """删除情报库链接"""
    from models.intel_link import IntelLink

    link = await IntelLink.filter(id=link_id).first()
    if not link:
        return json({"success": False, "error": "Intel link not found"}, status=404)
    await ProductDetail.filter(url=link.url, source="intel").delete()
    await link.delete()
    return json({"success": True})
