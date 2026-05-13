# -*- coding: utf-8 -*-
"""任务与商品链接业务编排服务。"""

import asyncio
import uuid
from typing import Any

from agentbay import (
    AsyncAgentBay,
    BrowserContext as AgentBayContext,
    BrowserFingerprint,
    BrowserOption,
    BrowserScreen,
    CreateSessionParams,
)
from sanic import Sanic

from config.settings import global_settings
from models.context import BrowserContext, ContextStatus
from models.product_link import ProductLink, ProductLinkMonitorStatus, ProductLinkSourceType
from models.intel_link import IntelLink
from models.task import Task, TaskStatus
from services.task_runner import _get_runner, dispatch_task
from utils.logger import logger


class ServiceError(Exception):
    """业务服务错误，供路由层转换为 HTTP 响应。"""

    def __init__(self, payload: dict[str, Any], status: int = 400):
        super().__init__(str(payload.get("error", "Service error")))
        self.payload = payload
        self.status = status


_KEYWORD_SEARCH_TASK_TYPES = {
    "taobao": "taobao_link_search",
    "tmall": "tmall_link_search",
    "jd": "jd_link_search",
}

_CONTEXT_PLATFORM_CANDIDATES = {
    "taobao": ["taobao"],
    "tmall": ["taobao"],
    "jd": ["jd"],
}

_ECOMMERCE_CONTEXT_CANDIDATES = {
    "taobao": ["taobao"],
    "tmall": ["taobao"],
    "jd": ["jd"],
    "1688": ["1688", "taobao"],
    "xiaohongshu": ["xiaohongshu"],
    "douyin": ["douyin"],
    "pdd": ["pdd"],
    "unknown": ["unknown", "other"],
    "other": ["other", "unknown"],
}

_ACTIVE_TASK_STATUSES = [
    TaskStatus.RUNNING.value,
    TaskStatus.WAITING_HUMAN_INPUT.value,
]


async def _active_context_ids() -> set[str]:
    tasks = await Task.filter(status__in=_ACTIVE_TASK_STATUSES)
    pending_tasks = await Task.filter(status=TaskStatus.PENDING.value, not_before_at=None)
    return {str(task.context_id) for task in [*tasks, *pending_tasks] if task.context_id}


def normalize_schedule(raw_schedule) -> str:
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
        raise ServiceError({"success": False, "error": "schedule must be empty or positive seconds"})
    return schedule


def detect_platform_from_url(url: str) -> str:
    value = (url or "").lower()
    if "jd.com" in value:
        return "jd"
    if "tmall.com" in value or "tmall.hk" in value:
        return "tmall"
    if "taobao.com" in value:
        return "taobao"
    if "1688.com" in value:
        return "1688"
    if "xiaohongshu.com" in value or "xhslink.com" in value:
        return "xiaohongshu"
    if "douyin.com" in value or "iesdouyin.com" in value:
        return "douyin"
    if "pinduoduo.com" in value or "yangkeduo.com" in value:
        return "pdd"
    return "unknown"


def _normalize_link_items(raw_links) -> list[dict[str, str]]:
    if not isinstance(raw_links, list):
        return []

    seen = set()
    normalized = []
    for item in raw_links:
        if isinstance(item, str):
            url = item.strip()
            platform = ""
            keyword = ""
        elif isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            platform = str(item.get("platform") or "").strip()
            keyword = str(item.get("keyword") or item.get("channel") or "").strip()
        else:
            continue
        if not url or not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        normalized.append({
            "url": ProductLink.canonicalize_url(url),
            "platform": platform or detect_platform_from_url(url),
            "keyword": keyword,
        })
    return normalized


async def _import_product_links(
    normalized: list[dict[str, str]],
    name: str = "",
    source_type: ProductLinkSourceType = ProductLinkSourceType.CSV_IMPORT,
    monitor_status: ProductLinkMonitorStatus = ProductLinkMonitorStatus.MONITORED,
    tags: list[str] | None = None,
) -> dict[str, int]:
    links = [
        ProductLink(
            task_id=uuid.UUID(int=0),
            platform=item["platform"],
            keyword=item["keyword"],
            url=item["url"],
            raw_url=item["url"],
            source_type=source_type.value,
            monitor_status=monitor_status.value,
            tags=tags or [],
        )
        for item in normalized
    ]
    await ProductLink.upsert_bulk(links)

    grouped: dict[str, int] = {}
    for item in normalized:
        grouped[item["platform"]] = grouped.get(item["platform"], 0) + 1

    return grouped


def _login_check_url(platform: str) -> str:
    urls = {
        "taobao": "https://www.taobao.com",
        "tmall": "https://www.tmall.com",
        "jd": "https://www.jd.com",
        "1688": "https://www.1688.com",
        "xiaohongshu": "https://www.xiaohongshu.com",
        "douyin": "https://www.douyin.com",
        "pdd": "https://mobile.yangkeduo.com",
    }
    return urls.get(platform, "https://www.taobao.com")


def _has_login_cookie(platform: str, cookies: list[dict]) -> bool:
    names = {cookie.get("name", "") for cookie in cookies}
    markers = {
        "taobao": {"unb", "_nk_", "tracknick", "lgc", "cookie17"},
        "tmall": {"unb", "_nk_", "tracknick", "lgc", "cookie17"},
        "1688": {"member_login", "ali_apache_id", "_csrf_token"},
        "jd": {"pin", "thor", "unick", "ceshi3.com"},
        "xiaohongshu": {"web_session", "webId", "xsecappid"},
        "douyin": {"sessionid", "sid_guard", "uid_tt"},
        "pdd": {"pdd_user_id", "PDDAccessToken", "api_uid"},
        "unknown": set(),
        "other": set(),
    }
    expected = markers.get(platform, set())
    return not expected or bool(names & expected)


async def validate_context_login(ctx: BrowserContext) -> bool:
    """验证持久化上下文里是否仍有平台登录态。"""
    if not ctx.context_id:
        return False

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    context_result = await agent_bay.context.get(ctx.context_id, create=False)
    if not context_result.success or not context_result.context:
        return False

    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "login-check", "context_id": str(ctx.id)},
            image_id=global_settings.agentbay.image_id,
            browser_context=AgentBayContext(context_result.context.id, auto_upload=False),
        )
    )
    if not session_result.success:
        return False

    session = session_result.session
    browser = None
    try:
        await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1280, height=800),
                solve_captchas=False,
                use_stealth=True,
                fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
            )
        )
        endpoint = await session.browser.get_endpoint_url()
        app = Sanic.get_app()
        browser = await app.ctx.playwright.chromium.connect_over_cdp(endpoint)
        browser_ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        cookies = await browser_ctx.cookies(_login_check_url(ctx.platform or ""))
        return _has_login_cookie(ctx.platform or "", cookies)
    except Exception as e:
        logger.warning(f"[context-login-check] failed context={ctx.id}: {e}")
        return False
    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            await agent_bay.delete(session, sync_context=False)
        except Exception as e:
            logger.warning(f"[context-login-check] cleanup failed context={ctx.id}: {e}")


async def _valid_logged_in_contexts() -> list[BrowserContext]:
    """返回可参与分配的上下文。

    业务任务创建阶段不能做重型浏览器登录态校验，否则会卡住创建流程；
    登录态失效由实际执行任务时暴露，并由用户重新登录/禁用账号处理。
    """
    active_context_ids = await _active_context_ids()
    contexts = await BrowserContext.filter(status=ContextStatus.LOGGED_IN.value).order_by("-updated_at")
    available = []
    for ctx in contexts:
        if str(ctx.id) in active_context_ids:
            # 状态可能因异常/重启没有及时变回 in_use，这里按活跃任务兜底。
            ctx.status = ContextStatus.IN_USE.value
            await ctx.save()
            continue
        available.append(ctx)
    return available


async def _available_session_slots() -> int:
    in_use = {str(ctx.id) for ctx in await BrowserContext.filter(status=ContextStatus.IN_USE.value)}
    active = len(in_use | await _active_context_ids())
    return max(0, global_settings.task.max_concurrent_sessions - active)


def _context_candidates(
    available_contexts: list[BrowserContext],
    platform: str,
    candidates_map: dict[str, list[str]],
) -> list[BrowserContext]:
    candidates = candidates_map.get(platform, [platform])
    return [ctx for ctx in available_contexts if (ctx.platform or "") in candidates]


def _least_loaded_context(contexts: list[BrowserContext], load: dict[str, int], used: set[str], slots: int) -> BrowserContext | None:
    usable = [ctx for ctx in contexts if str(ctx.id) in used or len(used) < slots]
    if not usable:
        return None
    return min(usable, key=lambda ctx: (load.get(str(ctx.id), 0), ctx.updated_at))


def _batch_policy(platforms: list[str], monitor_mode: str) -> tuple[int, int]:
    from config.crawl_profile import get_profile
    platform_set = set(platforms)
    # 取列表中第一个有 profile 的平台作为代表
    key = next((p for p in platforms if p in ("jd", "taobao", "tmall")), "default")
    p = get_profile(key)
    if monitor_mode == "detail":
        return p["detail_batch_size"], p["detail_batch_interval"]
    return p["price_batch_size"], p["price_batch_interval"]


async def _claim_context(ctx: BrowserContext) -> BrowserContext:
    claimed = await BrowserContext.filter(id=ctx.id, status=ContextStatus.LOGGED_IN.value).update(
        status=ContextStatus.IN_USE.value
    )
    if not claimed:
        raise ServiceError({"success": False, "error": "Context is no longer available"}, status=409)
    ctx.status = ContextStatus.IN_USE.value
    return ctx


async def _run_task_sequence_on_context(tasks: list[Task], ctx: BrowserContext) -> None:
    """同一个上下文内串行执行多个原子任务。"""
    try:
        for task in tasks:
            try:
                await task.start()
                runner = _get_runner(task.task_type)
                result = await runner(task, ctx)
                if task.status not in [
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                    TaskStatus.COMPLETED.value,
                ]:
                    await task.complete(result or {"message": "Task completed"})
            except asyncio.CancelledError:
                await task.cancel()
                raise
            except Exception as e:
                logger.error(f"Task {task.id} failed in sequence: {e}")
                await task.fail(str(e))
    finally:
        ctx.status = ContextStatus.LOGGED_IN.value
        await ctx.save()


async def _dispatch_tasks_for_context(tasks: list[Task], ctx: BrowserContext) -> None:
    if len(tasks) == 1:
        await dispatch_task(tasks[0], ctx)
        return
    asyncio.create_task(_run_task_sequence_on_context(tasks, ctx))


async def create_task(data: dict[str, Any]) -> dict[str, Any]:
    task_type = data.get("task_type")
    context_id = data.get("context_id")
    params = data.get("params", {})
    schedule = normalize_schedule(data.get("schedule") or params.get("schedule"))

    if not task_type:
        raise ServiceError({"success": False, "error": "task_type is required"})

    ctx = None
    if context_id:
        ctx = await BrowserContext.filter(id=context_id).first()
        if not ctx:
            raise ServiceError({"success": False, "error": "Context not found"}, status=404)
        if ctx.status != ContextStatus.LOGGED_IN.value:
            raise ServiceError({"success": False, "error": f"Context status is {ctx.status}, expected logged_in"})
        if not await validate_context_login(ctx):
            ctx.status = ContextStatus.PENDING.value
            await ctx.save()
            raise ServiceError({"success": False, "error": "Context login is invalid, please login again"})
        ctx = await _claim_context(ctx)

    task = await Task.create(
        source="api",
        source_id="default",
        task_type=task_type,
        context_id=context_id,
        params=params,
        schedule=schedule,
    )

    if ctx:
        try:
            await dispatch_task(task, ctx)
        except ValueError as e:
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()
            await task.fail(str(e))
            raise ServiceError({"success": False, "error": str(e)})

    return {
        "task_id": str(task.id),
        "task_type": task_type,
        "context_id": context_id,
        "status": task.status,
        "schedule": task.schedule,
    }


async def create_keyword_search_business_task(data: dict[str, Any]) -> dict[str, Any]:
    raw_platforms = data.get("platforms") or []
    raw_keywords = data.get("keywords") or []
    limit = int(data.get("limit") or 20)
    params_extra = data.get("params") or {}

    platforms = []
    for platform in raw_platforms:
        value = str(platform).strip()
        if value and value not in platforms:
            platforms.append(value)

    if isinstance(raw_keywords, str):
        keywords = [k.strip() for k in raw_keywords.replace("\n", ",").split(",") if k.strip()]
    else:
        keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]

    if not platforms:
        raise ServiceError({"success": False, "error": "platforms is required"})
    if not keywords:
        raise ServiceError({"success": False, "error": "keywords is required"})
    if limit <= 0:
        raise ServiceError({"success": False, "error": "limit must be positive"})

    unsupported = [p for p in platforms if p not in _KEYWORD_SEARCH_TASK_TYPES]
    if unsupported:
        raise ServiceError({
            "success": False,
            "error": f"Unsupported platforms for keyword search: {', '.join(unsupported)}",
            "missing_platforms": unsupported,
        })

    schedule = normalize_schedule(data.get("schedule") or params_extra.get("schedule"))
    slots = await _available_session_slots()
    if slots <= 0:
        raise ServiceError({"success": False, "error": "No available browser session slots"})
    available_contexts = await _valid_logged_in_contexts()
    selected: dict[str, BrowserContext] = {}
    context_load: dict[str, int] = {}
    used_context_ids: set[str] = set()
    missing = []

    for platform in platforms:
        candidates = _context_candidates(available_contexts, platform, _CONTEXT_PLATFORM_CANDIDATES)
        ctx = _least_loaded_context(candidates, context_load, used_context_ids, slots)
        if not ctx:
            missing.append(platform)
            continue
        selected[platform] = ctx
        key = str(ctx.id)
        used_context_ids.add(key)
        context_load[key] = context_load.get(key, 0) + 1

    if missing and not selected:
        raise ServiceError({
            "success": False,
            "error": "Insufficient logged-in contexts",
            "missing_platforms": missing,
            "required_platforms": platforms,
        })

    created = []
    claimed_contexts = []
    tasks_by_context: dict[str, dict[str, Any]] = {}
    try:
        for ctx in {str(context.id): context for context in selected.values()}.values():
            claimed_contexts.append(await _claim_context(ctx))

        keyword_tags = data.get("tags") or []

        for platform, ctx in selected.items():
            task_params = {
                "keywords": keywords,
                "limit": limit,
                "total_urls": limit,
                "business_task": "keyword_search",
                "business_platforms": platforms,
                "pages_per_batch": 5,
                "batch_interval_minutes": 5,
                **params_extra,
            }
            if keyword_tags:
                task_params["tags"] = keyword_tags
            task = await Task.create(
                source="api",
                source_id="business_keyword_search",
                task_type=_KEYWORD_SEARCH_TASK_TYPES[platform],
                context_id=ctx.id,
                params=task_params,
                schedule=schedule,
            )
            key = str(ctx.id)
            if key not in tasks_by_context:
                tasks_by_context[key] = {"context": ctx, "tasks": []}
            tasks_by_context[key]["tasks"].append(task)
            created.append({
                "task_id": str(task.id),
                "task_type": task.task_type,
                "platform": platform,
                "context_id": str(ctx.id),
                "status": task.status,
            })

        for group in tasks_by_context.values():
            await _dispatch_tasks_for_context(group["tasks"], group["context"])
    except ValueError as e:
        for ctx in claimed_contexts:
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()
        raise ServiceError({"success": False, "error": str(e)})

    return {
        "business_task": "keyword_search",
        "tasks": created,
        "keywords": keywords,
        "platforms": platforms,
        "missing_platforms": list(dict.fromkeys(missing)),
    }


async def create_ecommerce_link_monitor_business_task(data: dict[str, Any]) -> dict[str, Any]:
    selected_platforms = data.get("platforms") or []
    if isinstance(selected_platforms, str):
        selected_platforms = [selected_platforms]

    normalized = _normalize_link_items(data.get("links") or [])
    if selected_platforms:
        normalized = [
            item for item in normalized
            if item["platform"] in selected_platforms or (item["platform"] == "tmall" and "taobao" in selected_platforms)
        ]

    schedule = normalize_schedule(data.get("schedule"))
    allow_partial = bool(data.get("allow_partial"))
    monitor_mode = data.get("monitor_mode") or data.get("mode") or "price"
    retry_missing = bool(data.get("retry_missing"))
    fetch_mode = "complement" if retry_missing else "overwrite"
    task_type = "product_detail_fetch" if monitor_mode == "detail" else "search_by_urls"
    is_intel = data.get("source") == "intel"
    LinkModel = IntelLink if is_intel else ProductLink
    urls_by_platform: dict[str, list[str]] = {}
    if normalized:
        grouped = await _import_product_links(normalized, data.get("name") or "电商链接监控导入")
        for item in normalized:
            urls_by_platform.setdefault(item["platform"], []).append(item["url"])
    else:
        query = LinkModel.filter(monitor_status=ProductLinkMonitorStatus.MONITORED.value)
        if selected_platforms:
            platforms = list(selected_platforms)
            if "taobao" in platforms and "tmall" not in platforms:
                platforms.append("tmall")
            query = query.filter(platform__in=platforms)
        links = await query.order_by("platform", "created_at")
        if not links:
            raise ServiceError({"success": False, "error": "No monitored links found"})
        grouped = {}
        for link in links:
            grouped[link.platform] = grouped.get(link.platform, 0) + 1
            urls_by_platform.setdefault(link.platform, []).append(link.url)
        normalized = [{"url": link.url, "platform": link.platform, "keyword": getattr(link, "keyword", "")} for link in links]

    slots = await _available_session_slots()
    if slots <= 0:
        raise ServiceError({"success": False, "error": "No available browser session slots"})
    available_contexts = await _valid_logged_in_contexts()
    missing = []
    skipped = []
    task_groups: dict[str, dict] = {}
    context_load: dict[str, int] = {}
    used_context_ids: set[str] = set()
    platform_candidates: dict[str, list[BrowserContext]] = {}
    runnable_platforms = []

    for platform in grouped:
        if platform in {"unknown", "other"}:
            skipped.append(platform)
            continue
        candidates = _context_candidates(available_contexts, platform, _ECOMMERCE_CONTEXT_CANDIDATES)
        if not candidates:
            missing.append(platform)
            continue
        platform_candidates[platform] = candidates
        runnable_platforms.append(platform)

    # 第一阶段：先保证每个可运行渠道至少分到一个上下文，避免某个渠道先占满全部并发槽。
    for platform in runnable_platforms:
        ctx = _least_loaded_context(platform_candidates[platform], context_load, used_context_ids, slots)
        if not ctx:
            missing.append(platform)
            continue
        key = str(ctx.id)
        used_context_ids.add(key)
        context_load.setdefault(key, 0)
        if key not in task_groups:
            task_groups[key] = {"context": ctx, "platforms": [], "shards": {}, "url_count": 0}
        if platform not in task_groups[key]["platforms"]:
            task_groups[key]["platforms"].append(platform)

    # 第二阶段：给链接多的渠道补充更多上下文；任务里只存分片信息，不存大 URL 列表。
    for platform in runnable_platforms:
        candidates = platform_candidates[platform]
        target_contexts = min(len(candidates), slots, max(1, (len(urls_by_platform.get(platform, [])) + _batch_policy([platform], monitor_mode)[0] - 1) // _batch_policy([platform], monitor_mode)[0]))
        while len([g for g in task_groups.values() if platform in g["platforms"]]) < target_contexts:
            ctx = _least_loaded_context(candidates, context_load, used_context_ids, slots)
            if not ctx:
                break
            key = str(ctx.id)
            used_context_ids.add(key)
            context_load.setdefault(key, 0)
            if key not in task_groups:
                task_groups[key] = {"context": ctx, "platforms": [], "shards": {}, "url_count": 0}
            if platform not in task_groups[key]["platforms"]:
                task_groups[key]["platforms"].append(platform)

        platform_groups = [g for g in task_groups.values() if platform in g["platforms"]]
        for index, group in enumerate(platform_groups):
            group["shards"][platform] = {"index": index, "count": len(platform_groups)}
            group["url_count"] += (len(urls_by_platform.get(platform, [])) + len(platform_groups) - 1) // len(platform_groups)
            context_load[str(group["context"].id)] = context_load.get(str(group["context"].id), 0) + group["url_count"]

    missing = list(dict.fromkeys(missing))
    if missing and (not task_groups or not allow_partial):
        raise ServiceError({
            "success": False,
            "error": "Insufficient logged-in contexts",
            "missing_platforms": missing,
            "can_continue": bool(task_groups),
            "total": len(normalized),
            "platforms": grouped,
        })

    created = []
    claimed_contexts = []
    try:
        for group in task_groups.values():
            ctx = group["context"]
            batch_size, interval_minutes = _batch_policy(group["platforms"], monitor_mode)
            task = await Task.create(
                source="api",
                source_id="business_ecommerce_link_monitor",
                task_type=task_type,
                context_id=ctx.id,
                params={
                    "platforms": group["platforms"],
                    "business_task": "ecommerce_link_monitor",
                    "monitor_mode": monitor_mode,
                    "batch_index": 1,
                    "batch_size": batch_size,
                    "batch_interval_minutes": interval_minutes,
                    "monitor_status": ProductLinkMonitorStatus.MONITORED.value,
                    "shards": group["shards"],
                    "total_urls": group["url_count"],
                    "fetch_mode": fetch_mode,
                    **({"retry_missing": True} if retry_missing else {}),
                    **({"source": "intel"} if is_intel else {}),
                },
                schedule=schedule,
            )
            if ctx not in claimed_contexts:
                claimed_contexts.append(await _claim_context(ctx))
            await dispatch_task(task, ctx)
            created.append({
                "task_id": str(task.id),
                "task_type": task.task_type,
                "platforms": group["platforms"],
                "context_id": str(ctx.id),
                "status": task.status,
                "url_count": group["url_count"],
            })
    except ValueError as e:
        for ctx in claimed_contexts:
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()
        raise ServiceError({"success": False, "error": str(e)})

    return {
        "business_task": "ecommerce_link_monitor",
        "total": len(normalized),
        "platforms": grouped,
        "missing_platforms": missing,
        "skipped_platforms": skipped,
        "tasks": created,
    }


async def import_products(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data.get("links"), list) or not data.get("links"):
        raise ServiceError({"success": False, "error": "links is required"})

    normalized = _normalize_link_items(data.get("links") or [])
    if not normalized:
        raise ServiceError({"success": False, "error": "No valid links found"})

    raw_status = data.get("monitor_status") or ProductLinkMonitorStatus.MONITORED.value
    try:
        monitor_status = ProductLinkMonitorStatus(raw_status)
    except ValueError:
        raise ServiceError({"success": False, "error": "Invalid monitor_status"})

    raw_source_type = data.get("source_type") or ProductLinkSourceType.CSV_IMPORT.value
    try:
        source_type = ProductLinkSourceType(raw_source_type)
    except ValueError:
        source_type = ProductLinkSourceType.CSV_IMPORT

    import_tags = data.get("tags") or []

    grouped = await _import_product_links(
        normalized,
        data.get("name") or "",
        source_type=source_type,
        monitor_status=monitor_status,
        tags=import_tags,
    )

    return {
        "total": len(normalized),
        "platforms": grouped,
    }
