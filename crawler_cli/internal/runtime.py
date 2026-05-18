# -*- coding: utf-8 -*-
"""Database, app, context, and AgentBay session helpers."""

from contextlib import asynccontextmanager

from agentbay import (
    AsyncAgentBay,
    BrowserContext as AgentBayContext,
    CreateSessionParams,
)
from sanic import Sanic
from tortoise import Tortoise

from config.settings import create_db_config, global_settings
from models.context import BrowserContext, ContextStatus


def context_key(platform: str, context_id: str) -> str:
    return f"{platform}-context:default:{context_id}"


@asynccontextmanager
async def db_runtime():
    await Tortoise.init(config=create_db_config())
    try:
        yield
    finally:
        await Tortoise.close_connections()


@asynccontextmanager
async def app_runtime(with_playwright: bool = False):
    """Provide Sanic.get_app().ctx.playwright for existing service runners."""
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


async def resolve_context(value: str | None, platform: str | None = None) -> BrowserContext:
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


async def get_agentbay_context(agent_bay: AsyncAgentBay, ctx: BrowserContext, create: bool = False):
    key = ctx.context_id or context_key(ctx.platform, str(ctx.id))
    result = await agent_bay.context.get(key, create=create)
    if result.success and result.context and not ctx.context_id:
        ctx.context_id = key
        await ctx.save()
    return result


async def create_browser_session(
    agent_bay: AsyncAgentBay,
    ctx: BrowserContext,
    *,
    kind: str,
    auto_upload: bool,
    labels: dict[str, str] | None = None,
):
    context_result = await get_agentbay_context(agent_bay, ctx, create=True)
    if not context_result.success or not context_result.context:
        raise RuntimeError(f"AgentBay context not found: {context_result.error_message}")
    return await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": kind, "context_id": str(ctx.id), **(labels or {})},
            image_id=global_settings.agentbay.image_id,
            browser_context=AgentBayContext(context_result.context.id, auto_upload=auto_upload),
        )
    )
