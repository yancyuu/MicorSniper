# -*- coding: utf-8 -*-
"""Context commands."""

from agentbay import AsyncAgentBay

from config.settings import global_settings
from models.context import BrowserContext, ContextStatus
from utils.cache import get_redis
from utils.login_check import check_login_status

from crawler_cli.internal.agent_ops import browser_option
from crawler_cli.internal.doctor import doctor_context
from crawler_cli.internal.io import print_json
from crawler_cli.internal.runtime import (
    context_key,
    create_browser_session,
    db_runtime,
    app_runtime,
    resolve_context,
)


async def cmd_context_list(args) -> None:
    async with db_runtime():
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
        print_json(data)
        return
    for item in data:
        print(f"{item['platform']:<12} {item['status']:<10} {item['id']}  {item['name']}  {item['context_id']}")


async def cmd_context_show(args) -> None:
    async with db_runtime():
        ctx = await resolve_context(args.context)
        print_json(
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
    async with db_runtime():
        ctx = await BrowserContext.create(
            platform=args.platform,
            name=args.name or "",
            target_url=args.target_url or f"{args.platform}.com",
            status=ContextStatus.PENDING.value,
        )
        ctx.context_id = context_key(ctx.platform, str(ctx.id))
        await ctx.save()
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await create_browser_session(agent_bay, ctx, kind="cli-login", auto_upload=True)
        if not session_result.success or not session_result.session:
            raise SystemExit(f"Failed to create session: {session_result.error_message}")
        session = session_result.session

        target_url = ctx.target_url or f"{ctx.platform}.com"
        if not target_url.startswith("http"):
            target_url = f"https://{target_url}"
        ok = await session.browser.initialize(browser_option())
        if not ok:
            raise SystemExit("Failed to initialize browser")
        await session.browser.agent.navigate(target_url)

        redis = await get_redis()
        await redis.setex(f"login_session:{ctx.id}", 600, f"{session.session_id}|{ctx.context_id}")
        await redis.aclose()

        print_json(
            {
                "context_id": str(ctx.id),
                "platform": ctx.platform,
                "browser_url": session.resource_url,
                "confirm_command": f"craw context confirm {ctx.id}",
            }
        )


async def cmd_context_confirm(args) -> None:
    async with db_runtime():
        ctx = await resolve_context(args.context)
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
        print_json({"context_id": str(ctx.id), "status": ctx.status, "context_key": ctx.context_id})


async def cmd_context_release(args) -> None:
    async with db_runtime():
        ctx = await resolve_context(args.context)
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
        print_json({"context_id": str(ctx.id), "status": ctx.status, "login_session_cleared": bool(raw)})


async def cmd_context_status(args) -> None:
    async with db_runtime():
        ctx = await resolve_context(args.context)
        if args.set:
            allowed = {item.value for item in ContextStatus}
            if args.set not in allowed:
                raise SystemExit(f"Invalid status: {args.set}. Allowed: {', '.join(sorted(allowed))}")
            ctx.status = args.set
            await ctx.save()
        print_json(
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
    async with app_runtime(with_playwright=True):
        ctx = await resolve_context(args.context, platform=args.platform or None)
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await create_browser_session(agent_bay, ctx, kind="cli-context-verify", auto_upload=False)
        if not session_result.success or not session_result.session:
            raise SystemExit(f"Failed to create session: {session_result.error_message}")
        session = session_result.session
        try:
            await session.browser.initialize(browser_option())
            result = await check_login_status(session.browser.agent, args.platform or ctx.platform)
            print_json({"context_id": str(ctx.id), "platform": result.platform, "logged_in": result.logged_in, "reason": result.reason})
        finally:
            await agent_bay.delete(session, sync_context=False)


async def cmd_context_cookies(args) -> None:
    async with app_runtime(with_playwright=True):
        ctx = await resolve_context(args.context)
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await create_browser_session(agent_bay, ctx, kind="cli-context-cookies", auto_upload=False)
        if not session_result.success or not session_result.session:
            raise SystemExit(f"Failed to create session: {session_result.error_message}")
        session = session_result.session
        try:
            await session.browser.initialize(browser_option())
            endpoint = await session.browser.get_endpoint_url()
            from sanic import Sanic

            app = Sanic.get_app()
            browser = await app.ctx.playwright.chromium.connect_over_cdp(endpoint)
            try:
                browser_ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                cookies = await browser_ctx.cookies()
                if args.json:
                    print_json(cookies)
                else:
                    print("; ".join(f"{cookie.get('name')}={cookie.get('value')}" for cookie in cookies))
            finally:
                await browser.close()
        finally:
            await agent_bay.delete(session, sync_context=False)


async def cmd_context_doctor(args) -> None:
    async with db_runtime():
        print_json(await doctor_context(await resolve_context(args.context)))


def register(subparsers) -> None:
    context = subparsers.add_parser("context", help="上下文管理")
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
