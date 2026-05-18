# -*- coding: utf-8 -*-
"""通用爬虫 CLI。

确定性采集复用现有 runner；通用 Agent 采集只使用 AgentBay 的
navigate / act / extract，不在 CLI 中写第三方页面 DOM 规则。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agentbay import (
    ActOptions,
    AsyncAgentBay,
    BrowserContext as AgentBayContext,
    BrowserFingerprint,
    BrowserOption,
    BrowserScreen,
    CreateSessionParams,
    ExtractOptions,
)
from pydantic import BaseModel, Field
from sanic import Sanic
from tortoise import Tortoise

from config.settings import create_db_config, global_settings
from models.context import BrowserContext, ContextStatus
from models.product_detail import ProductDetail
from models.product_link import ProductLink
from models.task import Task, TaskStatus
from services.task_runner import _get_runner
from utils.cache import get_redis
from utils.login_check import check_login_status


_KEYWORD_TASK_TYPES = {
    "taobao": "taobao_link_search",
    "tmall": "tmall_link_search",
    "jd": "jd_link_search",
    "xiaohongshu": "xiaohongshu_link_search",
}


class AgentExtractRecord(BaseModel):
    """Agent 通用抽取记录，可表达商品、笔记、评论、回复等任意页面实体。"""

    record_type: str = Field(default="", description="记录类型，如 product/note/comment/reply/user/link/text")
    record_id: str = Field(default="", description="平台内稳定 ID，如笔记 ID、商品 ID、评论 ID；没有则为空")
    title: str = Field(default="", description="记录标题或核心文本摘要；没有则为空")
    text: str = Field(default="", description="完整可见正文、评论内容或说明文本；没有则为空")
    url: str = Field(default="", description="真实 http(s) URL 或以 / 开头的站内路径；没有则为空，不要填写 dom_index")
    author: str = Field(default="", description="作者、店铺、账号或发布者；没有则为空")
    published_at: str = Field(default="", description="页面可见的发布时间或评论时间；没有则为空")
    metrics: dict[str, Any] = Field(default_factory=dict, description="点赞、评论数、价格、销量等可量化或标签信息")
    fields: dict[str, Any] = Field(default_factory=dict, description="目标要求的其他字段，按页面语义自由补充；DOM 引用可放 dom_ref")
    children: list[dict[str, Any]] = Field(default_factory=list, description="子记录，如评论下的回复")
    source: str = Field(default="", description="该记录来自页面的哪个区域或线索")
    confidence: float = Field(default=0.0, description="0 到 1 的置信度")


class AgentExtractResult(BaseModel):
    """Agent 每轮观察后的结构化输出。"""

    summary: str = Field(default="", description="当前页面和已获取信息摘要")
    records: list[AgentExtractRecord] = Field(default_factory=list, description="本轮抽取到的通用记录")
    items: list[AgentExtractRecord] = Field(default_factory=list, description="兼容旧字段；新逻辑优先使用 records")
    missing_info: list[str] = Field(default_factory=list, description="为了完成目标还缺的信息")
    next_action: str = Field(default="", description="建议下一步在页面上执行的自然语言动作")
    done: bool = Field(default=False, description="目标是否已经完成")


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _read_lines(path_or_values: str | None) -> list[str]:
    if not path_or_values:
        return []
    path = Path(path_or_values)
    if path.exists():
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return _split_csv(path_or_values)


def _context_key(platform: str, context_id: str) -> str:
    return f"{platform}-context:default:{context_id}"


@asynccontextmanager
async def _db_runtime():
    await Tortoise.init(config=create_db_config())
    try:
        yield
    finally:
        await Tortoise.close_connections()


@asynccontextmanager
async def _app_runtime(with_playwright: bool = False):
    """给现有 runner 提供 Sanic.get_app().ctx.playwright。"""
    await Tortoise.init(config=create_db_config())
    app = Sanic("CrawlerCLI")
    playwright = None
    try:
        if with_playwright:
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            app.ctx.playwright = playwright
        yield app
    finally:
        if playwright:
            await playwright.stop()
        await Tortoise.close_connections()
        try:
            Sanic._app_registry.pop(app.name, None)
        except Exception:
            pass


async def _resolve_context(value: str | None, platform: str | None = None) -> BrowserContext:
    query = BrowserContext.all()
    if platform:
        query = query.filter(platform=platform)
    if value:
        ctx = await query.filter(id=value).first()
        if ctx:
            return ctx
        ctx = await query.filter(name=value).first()
        if ctx:
            return ctx
        ctx = await query.filter(context_id=value).first()
        if ctx:
            return ctx
    if platform:
        ctx = await query.filter(status=ContextStatus.LOGGED_IN.value).order_by("-updated_at").first()
        if ctx:
            return ctx
    raise SystemExit(f"Context not found: {value or platform or '<empty>'}")


async def _get_agentbay_context(agent_bay: AsyncAgentBay, ctx: BrowserContext, create: bool = False):
    context_key = ctx.context_id or _context_key(ctx.platform, str(ctx.id))
    result = await agent_bay.context.get(context_key, create=create)
    if result.success and result.context and not ctx.context_id:
        ctx.context_id = context_key
        await ctx.save()
    return result


async def _create_browser_session(
    agent_bay: AsyncAgentBay,
    ctx: BrowserContext,
    *,
    kind: str,
    auto_upload: bool,
    labels: dict[str, str] | None = None,
):
    context_result = await _get_agentbay_context(agent_bay, ctx, create=True)
    if not context_result.success or not context_result.context:
        raise RuntimeError(f"AgentBay context not found: {context_result.error_message}")
    return await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": kind, "context_id": str(ctx.id), **(labels or {})},
            image_id=global_settings.agentbay.image_id,
            browser_context=AgentBayContext(context_result.context.id, auto_upload=auto_upload),
        )
    )


async def cmd_context_list(args) -> None:
    async with _db_runtime():
        rows = await BrowserContext.all().order_by("platform", "-updated_at")
        data = [
            {
                "id": str(ctx.id),
                "platform": ctx.platform,
                "name": ctx.name,
                "status": ctx.status,
                "context_id": ctx.context_id,
                "target_url": ctx.target_url,
                "updated_at": ctx.updated_at.isoformat() if ctx.updated_at else None,
            }
            for ctx in rows
        ]
    if args.json:
        _print_json(data)
        return
    for item in data:
        print(f"{item['platform']:<12} {item['status']:<10} {item['id']}  {item['name']}  {item['context_id']}")


async def cmd_context_show(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context)
        _print_json(
            {
                "id": str(ctx.id),
                "platform": ctx.platform,
                "name": ctx.name,
                "status": ctx.status,
                "context_id": ctx.context_id,
                "target_url": ctx.target_url,
                "created_at": ctx.created_at.isoformat() if ctx.created_at else None,
                "updated_at": ctx.updated_at.isoformat() if ctx.updated_at else None,
            }
        )


async def cmd_context_login(args) -> None:
    async with _db_runtime():
        ctx = await BrowserContext.create(
            platform=args.platform,
            name=args.name or "",
            target_url=args.target_url or f"{args.platform}.com",
            status=ContextStatus.PENDING.value,
        )
        ctx.context_id = _context_key(ctx.platform, str(ctx.id))
        await ctx.save()
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await _create_browser_session(agent_bay, ctx, kind="cli-login", auto_upload=True)
        if not session_result.success or not session_result.session:
            raise SystemExit(f"Failed to create session: {session_result.error_message}")
        session = session_result.session

        target_url = ctx.target_url or f"{ctx.platform}.com"
        if not target_url.startswith("http"):
            target_url = f"https://{target_url}"
        ok = await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
            )
        )
        if not ok:
            raise SystemExit("Failed to initialize browser")
        await session.browser.agent.navigate(target_url)

        redis = await get_redis()
        await redis.setex(f"login_session:{ctx.id}", 600, f"{session.session_id}|{ctx.context_id}")
        await redis.aclose()

        _print_json(
            {
                "context_id": str(ctx.id),
                "platform": ctx.platform,
                "browser_url": session.resource_url,
                "confirm_command": f"craw context confirm {ctx.id}",
            }
        )


async def cmd_context_confirm(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context)
        redis = await get_redis()
        key = f"login_session:{ctx.id}"
        raw = await redis.get(key)
        if not raw:
            raise SystemExit("Login session not found or expired")
        session_id = raw.decode().split("|")[0]
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await agent_bay.get(session_id)
        if not session_result.success or not session_result.session:
            raise SystemExit(f"Login session not found: {session_result.error_message}")
        await agent_bay.delete(session_result.session, sync_context=True)
        ctx.status = ContextStatus.LOGGED_IN.value
        await ctx.save()
        await redis.delete(key)
        await redis.aclose()
        _print_json({"context_id": str(ctx.id), "status": ctx.status, "context_key": ctx.context_id})


async def cmd_context_release(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context)
        ctx.status = ContextStatus.LOGGED_IN.value
        await ctx.save()
        redis = await get_redis()
        key = f"login_session:{ctx.id}"
        raw = await redis.get(key)
        if raw:
            session_id = raw.decode().split("|")[0]
            agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
            session_result = await agent_bay.get(session_id)
            if session_result.success and session_result.session:
                await agent_bay.delete(session_result.session, sync_context=False)
            await redis.delete(key)
        await redis.aclose()
        _print_json({"context_id": str(ctx.id), "status": ctx.status, "login_session_cleared": bool(raw)})


async def cmd_context_status(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context)
        if args.set:
            allowed = {item.value for item in ContextStatus}
            if args.set not in allowed:
                raise SystemExit(f"Invalid status: {args.set}. Allowed: {', '.join(sorted(allowed))}")
            ctx.status = args.set
            await ctx.save()
        _print_json(
            {
                "id": str(ctx.id),
                "platform": ctx.platform,
                "name": ctx.name,
                "status": ctx.status,
                "context_id": ctx.context_id,
                "updated_at": ctx.updated_at.isoformat() if ctx.updated_at else None,
            }
        )


async def cmd_context_verify(args) -> None:
    async with _app_runtime(with_playwright=True):
        ctx = await _resolve_context(args.context, platform=args.platform or None)
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await _create_browser_session(agent_bay, ctx, kind="cli-context-verify", auto_upload=False)
        if not session_result.success or not session_result.session:
            raise SystemExit(f"Failed to create session: {session_result.error_message}")
        session = session_result.session
        try:
            await session.browser.initialize(
                BrowserOption(
                    screen=BrowserScreen(width=1280, height=800),
                    solve_captchas=False,
                    use_stealth=True,
                    fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
                )
            )
            result = await check_login_status(session.browser.agent, args.platform or ctx.platform)
            _print_json({"context_id": str(ctx.id), "platform": result.platform, "logged_in": result.logged_in, "reason": result.reason})
        finally:
            await agent_bay.delete(session, sync_context=False)


async def cmd_context_cookies(args) -> None:
    async with _app_runtime(with_playwright=True):
        ctx = await _resolve_context(args.context)
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await _create_browser_session(agent_bay, ctx, kind="cli-context-cookies", auto_upload=False)
        if not session_result.success or not session_result.session:
            raise SystemExit(f"Failed to create session: {session_result.error_message}")
        session = session_result.session
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
            try:
                browser_ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                cookies = await browser_ctx.cookies()
                if args.json:
                    _print_json(cookies)
                else:
                    print("; ".join(f"{cookie.get('name')}={cookie.get('value')}" for cookie in cookies))
            finally:
                await browser.close()
        finally:
            await agent_bay.delete(session, sync_context=False)


async def _doctor_context(ctx: BrowserContext) -> dict[str, Any]:
    result: dict[str, Any] = {
        "db": {
            "id": str(ctx.id),
            "platform": ctx.platform,
            "name": ctx.name,
            "status": ctx.status,
            "context_id": ctx.context_id,
            "target_url": ctx.target_url,
        }
    }
    redis = await get_redis()
    key = f"login_session:{ctx.id}"
    raw = await redis.get(key)
    result["redis"] = {"key": key, "exists": bool(raw), "ttl": await redis.ttl(key) if raw else None}
    if raw:
        result["redis"]["session_id"] = raw.decode().split("|")[0]
    await redis.aclose()

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    context_result = await _get_agentbay_context(agent_bay, ctx, create=False)
    result["agentbay_context"] = {
        "success": context_result.success,
        "id": getattr(context_result.context, "id", None) if context_result.context else None,
        "name": getattr(context_result.context, "name", None) if context_result.context else None,
        "error": context_result.error_message,
    }
    if raw:
        session_result = await agent_bay.get(raw.decode().split("|")[0])
        session_info: dict[str, Any] = {"success": session_result.success, "error": session_result.error_message}
        if session_result.success and session_result.session:
            session_info["resource_url"] = session_result.session.resource_url
            try:
                endpoint = await session_result.session.browser.get_endpoint_url()
                session_info["browser_initialized"] = bool(endpoint)
            except Exception as exc:
                session_info["browser_initialized"] = False
                session_info["browser_error"] = str(exc)
        result["login_session"] = session_info
    return result


async def cmd_context_doctor(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context)
        _print_json(await _doctor_context(ctx))


async def cmd_doctor(args) -> None:
    async with _db_runtime():
        if args.subject == "env":
            _print_json(
                {
                    "app_port": global_settings.app.port,
                    "database": f"{global_settings.database.host}:{global_settings.database.port}/{global_settings.database.name}",
                    "redis": f"{global_settings.redis.host}:{global_settings.redis.port}/{global_settings.redis.db}",
                    "agentbay_image_id": global_settings.agentbay.image_id,
                    "agentbay_api_key_configured": bool(global_settings.agentbay.api_key),
                }
            )
        elif args.subject == "db":
            rows = await BrowserContext.all().count()
            tasks = await Task.all().count()
            _print_json({"contexts": rows, "tasks": tasks})
        elif args.subject == "redis":
            redis = await get_redis()
            keys = await redis.keys("login_session:*")
            _print_json({"login_sessions": [key.decode() if isinstance(key, bytes) else key for key in keys]})
            await redis.aclose()
        elif args.subject == "agentbay":
            agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
            probe_name = "crawler-cli-doctor-probe"
            probe = await agent_bay.context.get(probe_name, create=True)
            payload = {"context_get": probe.success, "context_id": getattr(probe.context, "id", None), "error": probe.error_message}
            if probe.success and probe.context:
                try:
                    await agent_bay.context.delete(probe.context)
                except Exception:
                    pass
            _print_json(payload)
        elif args.subject == "context":
            if not args.context:
                raise SystemExit("context is required")
            ctx = await _resolve_context(args.context)
            _print_json(await _doctor_context(ctx))
        elif args.subject == "login-session":
            if not args.context:
                raise SystemExit("context is required")
            ctx = await _resolve_context(args.context)
            redis = await get_redis()
            key = f"login_session:{ctx.id}"
            raw = await redis.get(key)
            _print_json({"key": key, "value": raw.decode() if raw else None, "ttl": await redis.ttl(key) if raw else None})
            await redis.aclose()


async def _run_task_once(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    runner = _get_runner(task.task_type)
    await task.start()
    try:
        result = await runner(task, ctx)
        refreshed = await Task.get(id=task.id)
        if refreshed.status not in [TaskStatus.FAILED.value, TaskStatus.CANCELLED.value, TaskStatus.COMPLETED.value, TaskStatus.PENDING.value]:
            await refreshed.complete(result or {"message": "Task completed"})
        return result
    except Exception as exc:
        await task.fail(str(exc))
        raise
    finally:
        ctx.status = ContextStatus.LOGGED_IN.value
        await ctx.save()


async def _agent_recover_action(agent, task: Task, step: int, reason: str) -> None:
    """通用 Agent 自愈动作，不绑定站点 DOM 规则。"""
    action = "检查当前页面状态，关闭遮挡弹窗；如果页面异常则刷新或返回可操作页面，然后继续围绕目标浏览。"
    try:
        await agent.act(ActOptions(action=action))
        await task.log_step(step, "Agent 自愈动作", {"reason": reason, "action": action}, {"status": "ok"}, "completed")
    except Exception as exc:
        await task.log_step(step, "Agent 自愈动作失败", {"reason": reason, "action": action}, {"error": str(exc)}, "failed")


async def cmd_run_keyword(args) -> None:
    platform = args.platform
    if platform not in _KEYWORD_TASK_TYPES:
        raise SystemExit(f"Unsupported keyword platform: {platform}")
    async with _app_runtime(with_playwright=True):
        ctx = await _resolve_context(args.context, platform=platform)
        params = {
            "keywords": _split_csv(args.keywords),
            "limit": args.limit,
            "max_pages": args.max_pages,
            "pages_per_batch": args.pages_per_batch,
            "batch_interval_minutes": 0,
        }
        task = await Task.create(source="cli", source_id="craw", task_type=_KEYWORD_TASK_TYPES[platform], context_id=ctx.id, params=params)
        result = await _run_task_once(task, ctx)
        _print_json({"task_id": str(task.id), "result": result, "status": (await Task.get(id=task.id)).status})


async def cmd_run_urls(args) -> None:
    urls = _read_lines(args.urls)
    if not urls:
        raise SystemExit("No URLs provided")
    async with _app_runtime(with_playwright=True):
        ctx = await _resolve_context(args.context)
        task = await Task.create(
            source="cli",
            source_id="craw",
            task_type="search_by_urls",
            context_id=ctx.id,
            params={"urls": urls, "batch_size": args.batch_size, "batch_interval_minutes": 0},
        )
        result = await _run_task_once(task, ctx)
        _print_json({"task_id": str(task.id), "result": result, "status": (await Task.get(id=task.id)).status})


async def cmd_run_detail(args) -> None:
    urls = _read_lines(args.urls)
    if not urls:
        raise SystemExit("No URLs provided")
    async with _app_runtime(with_playwright=True):
        ctx = await _resolve_context(args.context)
        task = await Task.create(
            source="cli",
            source_id="craw",
            task_type="product_detail_fetch",
            context_id=ctx.id,
            params={"urls": urls, "batch_size": args.batch_size, "batch_interval_minutes": 0},
        )
        result = await _run_task_once(task, ctx)
        _print_json({"task_id": str(task.id), "result": result, "status": (await Task.get(id=task.id)).status})


async def _agent_crawl(ctx: BrowserContext, start_url: str, goal: str, max_steps: int, extract_timeout: int = 45) -> dict[str, Any]:
    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    session_result = await _create_browser_session(agent_bay, ctx, kind="cli-agent-crawl", auto_upload=False)
    if not session_result.success or not session_result.session:
        raise RuntimeError(f"Failed to create session: {session_result.error_message}")
    session = session_result.session
    task = await Task.create(
        source="cli",
        source_id="craw-agent",
        task_type="agent_crawl",
        context_id=ctx.id,
        params={"start_url": start_url, "goal": goal, "max_steps": max_steps},
    )
    outputs: list[dict[str, Any]] = []
    try:
        task.browser_url = session.resource_url or ""
        await task.start()
        await task.save()
        await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
            )
        )
        agent = session.browser.agent
        await agent.navigate(start_url)
        await task.log_step(1, "打开起始页面", {"url": start_url, "goal": goal}, {}, "completed")

        for step in range(1, max_steps + 1):
            instruction = (
                f"目标：{goal}\n"
                "请只依据当前浏览器页面抽取结构化信息。不要编造。"
                "将页面里的商品、笔记、用户、评论、回复、链接或普通文本都表示为 records。"
                "评论和回复请用 record_type=comment/reply，内容放 text，作者放 author，点赞/时间等放 metrics 或 published_at。"
                "url 字段只能填写真实 http(s) URL 或以 / 开头的站内路径；不要把 dom_index 或元素编号填入 url，"
                "如只有元素引用，请放到 fields.dom_ref，并在 next_action 里建议打开该记录以获取真实链接。"
                "如果页面或链接中能识别平台内稳定 ID（如小红书笔记 ID、商品 ID、评论 ID），必须填入 record_id；"
                "如果当前列表页看不到 ID，请留空并在 next_action 中建议打开详情页获取。"
                "如果信息不足，请给出下一步应执行的页面动作；如果目标已完成，done=true。"
            )
            try:
                ok, payload = await agent.extract(
                    ExtractOptions(
                        instruction=instruction,
                        schema=AgentExtractResult,
                        use_text_extract=True,
                        # use_vision=True,
                        timeout=extract_timeout,
                    )
                )
            except Exception as exc:
                await task.log_step(step + 1, "Agent 抽取异常", {"goal": goal, "timeout": extract_timeout}, {"error": str(exc)}, "failed")
                await _agent_recover_action(agent, task, step + 1_000, f"extract exception: {exc}")
                continue
            if not ok or payload is None:
                await task.log_step(step + 1, "Agent 抽取失败", {"goal": goal}, {"ok": ok}, "failed")
                await _agent_recover_action(agent, task, step + 1_000, "extract failed")
                continue

            data = payload.model_dump()
            outputs.append(data)
            await task.log_step(step + 1, f"Agent 抽取第 {step} 轮", {"goal": goal}, data, "completed")
            if payload.done:
                break

            action = payload.next_action or "围绕目标继续浏览当前网站，优先打开可能包含所需信息的链接或区域。"
            try:
                await agent.act(ActOptions(action=action))
            except Exception as exc:
                await task.log_step(step + 1_000, "Agent 自愈动作", {"action": action}, {"error": str(exc)}, "failed")
                await _agent_recover_action(agent, task, step + 2_000, str(exc))

        result = {"task_id": str(task.id), "goal": goal, "rounds": len(outputs), "outputs": outputs}
        await task.complete(result)
        return result
    except Exception as exc:
        await task.fail(str(exc))
        raise
    finally:
        try:
            await agent_bay.delete(session, sync_context=False)
        finally:
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()


async def cmd_agent_act(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context)
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await _create_browser_session(agent_bay, ctx, kind="cli-agent-act", auto_upload=False)
        if not session_result.success or not session_result.session:
            raise SystemExit(f"Failed to create session: {session_result.error_message}")
        session = session_result.session
        task = await Task.create(
            source="cli",
            source_id="craw-agent",
            task_type="agent_act",
            context_id=ctx.id,
            params={"url": args.url, "action": args.action, "keep_session": args.keep_session},
        )
        try:
            task.browser_url = session.resource_url or ""
            await task.start()
            await task.save()
            await session.browser.initialize(
                BrowserOption(
                    screen=BrowserScreen(width=1920, height=1080),
                    solve_captchas=True,
                    use_stealth=True,
                    fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
                )
            )
            if args.url:
                await session.browser.agent.navigate(args.url)
                await task.log_step(1, "打开页面", {"url": args.url}, {}, "completed")
            action_result = await session.browser.agent.act(ActOptions(action=args.action))
            result = {
                "task_id": str(task.id),
                "session_id": session.session_id,
                "browser_url": session.resource_url,
                "kept": args.keep_session,
                "success": bool(getattr(action_result, "success", False)),
                "message": str(action_result),
            }
            await task.log_step(2, "执行 Agent 动作", {"action": args.action}, result, "completed")
            await task.complete(result)
            _print_json(result)
        except Exception as exc:
            await task.fail(str(exc))
            raise
        finally:
            if not args.keep_session:
                await agent_bay.delete(session, sync_context=False)


async def cmd_agent_extract(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context) if args.context else None
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        created_session = False
        if args.session_id:
            session_result = await agent_bay.get(args.session_id)
            if not session_result.success or not session_result.session:
                raise SystemExit(f"Session not found: {session_result.error_message}")
            session = session_result.session
        else:
            if not ctx:
                raise SystemExit("--context is required when --session-id is not provided")
            session_result = await _create_browser_session(agent_bay, ctx, kind="cli-agent-extract", auto_upload=False)
            if not session_result.success or not session_result.session:
                raise SystemExit(f"Failed to create session: {session_result.error_message}")
            session = session_result.session
            created_session = True
            await session.browser.initialize(
                BrowserOption(
                    screen=BrowserScreen(width=1920, height=1080),
                    solve_captchas=True,
                    use_stealth=True,
                    fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
                )
            )
        task = await Task.create(
            source="cli",
            source_id="craw-agent",
            task_type="agent_extract",
            context_id=ctx.id if ctx else None,
            params={"url": args.url, "goal": args.goal, "session_id": session.session_id},
        )
        try:
            task.browser_url = session.resource_url or ""
            await task.start()
            await task.save()
            if args.url:
                await session.browser.agent.navigate(args.url)
                await task.log_step(1, "打开页面", {"url": args.url}, {}, "completed")
            instruction = (
                f"目标：{args.goal}\n"
                "请只依据当前浏览器页面抽取结构化信息。不要编造。"
                "将页面里的商品、笔记、用户、评论、回复、链接或普通文本都表示为 records。"
                "评论和回复请用 record_type=comment/reply，内容放 text，作者放 author，点赞/时间等放 metrics 或 published_at。"
                "url 字段只能填写真实 http(s) URL 或以 / 开头的站内路径；不要把 dom_index 或元素编号填入 url，"
                "如只有元素引用，请放到 fields.dom_ref，并在 next_action 里建议打开该记录以获取真实链接。"
                "如果页面或链接中能识别平台内稳定 ID（如小红书笔记 ID、商品 ID、评论 ID），必须填入 record_id；"
                "如果当前列表页看不到 ID，请留空并在 next_action 中建议打开详情页获取。"
                "如果信息不足，请给出下一步应执行的页面动作；如果目标已完成，done=true。"
            )
            ok, payload = await session.browser.agent.extract(
                ExtractOptions(
                    instruction=instruction,
                    schema=AgentExtractResult,
                    use_text_extract=True,
                    # use_vision=True,
                    timeout=args.timeout,
                )
            )
            result = payload.model_dump() if ok and payload else {"ok": ok, "error": "extract returned empty payload"}
            await task.log_step(2, "执行 Agent 抽取", {"goal": args.goal}, result, "completed" if ok else "failed")
            if ok:
                await task.complete({"task_id": str(task.id), **result})
            else:
                await task.fail("Agent extract failed")
            _print_json({"task_id": str(task.id), "session_id": session.session_id, "result": result})
        except Exception as exc:
            await task.fail(str(exc))
            raise
        finally:
            if created_session and not args.keep_session:
                await agent_bay.delete(session, sync_context=False)


async def cmd_agent_close(args) -> None:
    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    session_result = await agent_bay.get(args.session_id)
    if not session_result.success or not session_result.session:
        _print_json({"session_id": args.session_id, "closed": False, "error": session_result.error_message})
        return
    delete_result = await agent_bay.delete(session_result.session, sync_context=args.sync_context)
    _print_json({"session_id": args.session_id, "closed": delete_result.success, "error": delete_result.error_message})


async def cmd_run_page(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context)
        result = await _agent_crawl(ctx, args.url, args.goal, args.max_steps, args.extract_timeout)
        _print_json(result)


async def cmd_agent_crawl(args) -> None:
    async with _db_runtime():
        ctx = await _resolve_context(args.context)
        result = await _agent_crawl(ctx, args.start_url, args.goal, args.max_steps, args.extract_timeout)
        _print_json(result)


async def cmd_agent_inspect(args) -> None:
    async with _db_runtime():
        task = await Task.get(id=args.run_id)
        _print_json(await task.to_agent_readable())


async def cmd_agent_recover(args) -> None:
    async with _db_runtime():
        task = await Task.get(id=args.run_id)
        params = task.params or {}
        ctx = await _resolve_context(str(task.context_id))
        result = await _agent_crawl(
            ctx,
            params.get("start_url", ""),
            params.get("goal", ""),
            args.max_steps or params.get("max_steps", 5),
            args.extract_timeout,
        )
        _print_json(result)


async def cmd_task_list(args) -> None:
    async with _db_runtime():
        query = Task.all()
        if args.status:
            query = query.filter(status=args.status)
        tasks = await query.order_by("-created_at").limit(args.limit)
        _print_json([await task.to_agent_readable() for task in tasks])


async def cmd_task_show(args) -> None:
    async with _db_runtime():
        task = await Task.get(id=args.task_id)
        _print_json(await task.to_agent_readable())


async def cmd_task_logs(args) -> None:
    async with _db_runtime():
        task = await Task.get(id=args.task_id)
        if args.json:
            _print_json(task.logs)
        else:
            print(task.get_logs_summary())


async def cmd_task_cancel(args) -> None:
    async with _db_runtime():
        task = await Task.get(id=args.task_id)
        await task.cancel()
        _print_json({"task_id": str(task.id), "status": task.status})


async def cmd_task_retry(args) -> None:
    async with _app_runtime(with_playwright=True):
        task = await Task.get(id=args.task_id)
        if not task.context_id:
            raise SystemExit("Task has no context_id")
        ctx = await BrowserContext.get(id=task.context_id)
        task.status = TaskStatus.PENDING.value
        task.error = None
        task.progress = 0
        task.not_before_at = None
        if args.clear_logs:
            task.logs = []
        await task.save()
        result = await _run_task_once(task, ctx)
        _print_json({"task_id": str(task.id), "status": (await Task.get(id=task.id)).status, "result": result})


async def cmd_task_create_keyword(args) -> None:
    platform = args.platform
    if platform not in _KEYWORD_TASK_TYPES:
        raise SystemExit(f"Unsupported keyword platform: {platform}")
    async with _db_runtime():
        ctx = await _resolve_context(args.context, platform=platform)
        task = await Task.create(
            source="cli",
            source_id="craw",
            task_type=_KEYWORD_TASK_TYPES[platform],
            context_id=ctx.id,
            params={"keywords": _split_csv(args.keywords), "limit": args.limit, "max_pages": args.max_pages},
            schedule=str(args.schedule or ""),
        )
        _print_json({"task_id": str(task.id), "status": task.status, "task_type": task.task_type})


async def cmd_data_links(args) -> None:
    async with _db_runtime():
        query = ProductLink.all()
        if args.task:
            query = query.filter(task_id=args.task)
        if args.platform:
            query = query.filter(platform=args.platform)
        rows = await query.order_by("-created_at").limit(args.limit)
        _print_json([row.to_dict() for row in rows])


async def cmd_data_details(args) -> None:
    async with _db_runtime():
        query = ProductDetail.all()
        if args.platform:
            query = query.filter(platform=args.platform)
        if args.task:
            query = query.filter(task_id=args.task)
        rows = await query.order_by("-updated_at").limit(args.limit)
        _print_json([row.to_dict() for row in rows])


async def cmd_data_export(args) -> None:
    async with _db_runtime():
        task = await Task.get(id=args.task)
        links = await ProductLink.filter(task_id=task.id).order_by("created_at")
        details = await ProductDetail.filter(task_id=task.id).order_by("updated_at")
        payload = {
            "task": await task.to_agent_readable(),
            "links": [row.to_dict() for row in links],
            "details": [row.to_dict() for row in details],
        }
    output = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(args.output)
    else:
        print(output)


async def cmd_data_import_urls(args) -> None:
    urls = _read_lines(args.file)
    if not urls:
        raise SystemExit("No URLs found")
    async with _db_runtime():
        items = [
            ProductLink(task_id=args.task_id, platform=args.platform, keyword=args.keyword or "", url=url, raw_url=url)
            for url in urls
        ]
        await ProductLink.upsert_bulk(items)
        _print_json({"imported": len(items), "platform": args.platform})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="craw", description="Micro-Sniper 通用爬虫 CLI")
    sub = parser.add_subparsers(dest="domain", required=True)

    context = sub.add_parser("context", help="上下文管理")
    context_sub = context.add_subparsers(dest="action", required=True)
    p = context_sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_context_list)
    p = context_sub.add_parser("show")
    p.add_argument("context")
    p.set_defaults(func=cmd_context_show)
    p = context_sub.add_parser("login")
    p.add_argument("platform")
    p.add_argument("--name", default="")
    p.add_argument("--target-url", default="")
    p.set_defaults(func=cmd_context_login)
    p = context_sub.add_parser("confirm")
    p.add_argument("context")
    p.set_defaults(func=cmd_context_confirm)
    p = context_sub.add_parser("doctor")
    p.add_argument("context")
    p.set_defaults(func=cmd_context_doctor)
    p = context_sub.add_parser("verify")
    p.add_argument("context")
    p.add_argument("--platform", default="")
    p.set_defaults(func=cmd_context_verify)
    p = context_sub.add_parser("cookies")
    p.add_argument("context")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_context_cookies)
    p = context_sub.add_parser("release")
    p.add_argument("context")
    p.set_defaults(func=cmd_context_release)
    p = context_sub.add_parser("status")
    p.add_argument("context")
    p.add_argument("--set", choices=[item.value for item in ContextStatus], default="")
    p.set_defaults(func=cmd_context_status)

    run = sub.add_parser("run", help="一次性采集")
    run_sub = run.add_subparsers(dest="action", required=True)
    p = run_sub.add_parser("keyword")
    p.add_argument("--platform", required=True)
    p.add_argument("--keywords", required=True)
    p.add_argument("--context", default="")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--pages-per-batch", type=int, default=0)
    p.set_defaults(func=cmd_run_keyword)
    p = run_sub.add_parser("urls")
    p.add_argument("--urls", required=True, help="逗号分隔 URL 或文件路径")
    p.add_argument("--context", required=True)
    p.add_argument("--batch-size", type=int, default=20)
    p.set_defaults(func=cmd_run_urls)
    p = run_sub.add_parser("detail")
    p.add_argument("--urls", required=True, help="逗号分隔 URL 或文件路径")
    p.add_argument("--context", required=True)
    p.add_argument("--batch-size", type=int, default=8)
    p.set_defaults(func=cmd_run_detail)
    p = run_sub.add_parser("page")
    p.add_argument("--url", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--max-steps", type=int, default=5)
    p.add_argument("--extract-timeout", type=int, default=45)
    p.set_defaults(func=cmd_run_page)

    agent = sub.add_parser("agent", help="AgentBay 原生自愈采集")
    agent_sub = agent.add_subparsers(dest="action", required=True)
    p = agent_sub.add_parser("act")
    p.add_argument("--context", required=True)
    p.add_argument("--url", default="")
    p.add_argument("--action", required=True)
    p.add_argument("--keep-session", action="store_true")
    p.set_defaults(func=cmd_agent_act)
    p = agent_sub.add_parser("extract")
    p.add_argument("--context", default="")
    p.add_argument("--session-id", default="")
    p.add_argument("--url", default="")
    p.add_argument("--goal", required=True)
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--keep-session", action="store_true")
    p.set_defaults(func=cmd_agent_extract)
    p = agent_sub.add_parser("crawl")
    p.add_argument("--context", required=True)
    p.add_argument("--start-url", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--max-steps", type=int, default=5)
    p.add_argument("--extract-timeout", type=int, default=45)
    p.set_defaults(func=cmd_agent_crawl)
    p = agent_sub.add_parser("inspect")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_agent_inspect)
    p = agent_sub.add_parser("recover")
    p.add_argument("run_id")
    p.add_argument("--max-steps", type=int, default=0)
    p.add_argument("--extract-timeout", type=int, default=45)
    p.set_defaults(func=cmd_agent_recover)
    p = agent_sub.add_parser("replay")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_agent_inspect)
    p = agent_sub.add_parser("close")
    p.add_argument("session_id")
    p.add_argument("--sync-context", action="store_true")
    p.set_defaults(func=cmd_agent_close)

    task = sub.add_parser("task", help="任务管理")
    task_sub = task.add_subparsers(dest="action", required=True)
    p = task_sub.add_parser("list")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_task_list)
    p = task_sub.add_parser("show")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_show)
    p = task_sub.add_parser("logs")
    p.add_argument("task_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_logs)
    p = task_sub.add_parser("cancel")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_cancel)
    p = task_sub.add_parser("retry")
    p.add_argument("task_id")
    p.add_argument("--clear-logs", action="store_true")
    p.set_defaults(func=cmd_task_retry)
    create = task_sub.add_parser("create")
    create_sub = create.add_subparsers(dest="kind", required=True)
    p = create_sub.add_parser("keyword")
    p.add_argument("--platform", required=True)
    p.add_argument("--keywords", required=True)
    p.add_argument("--context", default="")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--schedule", default="")
    p.set_defaults(func=cmd_task_create_keyword)

    data = sub.add_parser("data", help="数据查看与导出")
    data_sub = data.add_subparsers(dest="action", required=True)
    p = data_sub.add_parser("links")
    p.add_argument("--task", default="")
    p.add_argument("--platform", default="")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_data_links)
    p = data_sub.add_parser("details")
    p.add_argument("--task", default="")
    p.add_argument("--platform", default="")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_data_details)
    p = data_sub.add_parser("export")
    p.add_argument("--task", required=True)
    p.add_argument("--format", choices=["json"], default="json")
    p.add_argument("--output", default="")
    p.set_defaults(func=cmd_data_export)
    p = data_sub.add_parser("import-urls")
    p.add_argument("file")
    p.add_argument("--platform", required=True)
    p.add_argument("--task-id", default="00000000-0000-0000-0000-000000000000")
    p.add_argument("--keyword", default="")
    p.set_defaults(func=cmd_data_import_urls)

    doctor = sub.add_parser("doctor", help="系统诊断")
    doctor.add_argument("subject", choices=["env", "agentbay", "redis", "db", "context", "login-session"])
    doctor.add_argument("context", nargs="?")
    doctor.set_defaults(func=cmd_doctor)
    return parser


async def async_main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    await args.func(args)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
