# -*- coding: utf-8 -*-
"""浏览器上下文管理 API"""

from sanic import Blueprint
from sanic.request import Request
from sanic.response import json
from tortoise.exceptions import DoesNotExist
from agentbay import CreateSessionParams, BrowserContext as AgentBayContext, BrowserOption, BrowserScreen, BrowserFingerprint
import asyncio

from models.context import BrowserContext, ContextStatus
from models.task import Task, TaskStatus
from utils.logger import logger
from utils.exceptions import ContextNotFoundException, SessionCreationException

contexts_bp = Blueprint("contexts", url_prefix="/api/contexts")


@contexts_bp.get("/")
async def list_contexts(request: Request):
    """获取所有上下文，按渠道分组"""
    await release_stale_contexts()
    contexts = await BrowserContext.all().order_by("-created_at")
    grouped = {}
    for ctx in contexts:
        platform = (ctx.platform or "").strip() or "unknown"
        if platform not in grouped:
            grouped[platform] = []
        grouped[platform].append({
            "id": str(ctx.id),
            "platform": ctx.platform,
            "name": ctx.name,
            "context_id": ctx.context_id,
            "target_url": ctx.target_url,
            "status": ctx.status,
            "created_at": ctx.created_at.isoformat() if ctx.created_at else None,
            "updated_at": ctx.updated_at.isoformat() if ctx.updated_at else None,
        })
    return json({"success": True, "data": grouped})


async def release_stale_contexts():
    """释放没有运行中任务占用的上下文。"""
    active_statuses = [
        TaskStatus.PENDING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.WAITING_HUMAN_INPUT.value,
    ]
    contexts = await BrowserContext.filter(status=ContextStatus.IN_USE.value)
    for ctx in contexts:
        has_active_task = await Task.filter(context_id=ctx.id, status__in=active_statuses).exists()
        if not has_active_task:
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()


@contexts_bp.post("/")
async def create_context(request: Request):
    """创建新的浏览器上下文"""
    data = request.json or {}
    platform = data.get("platform")
    name = data.get("name", "")
    target_url = data.get("target_url", "")

    if not platform:
        return json({"success": False, "error": "platform is required"}, status=400)

    ctx = await BrowserContext.create(
        platform=platform,
        name=name,
        target_url=target_url,
        status=ContextStatus.PENDING.value,
    )
    return json({
        "success": True,
        "data": {
            "id": str(ctx.id),
            "platform": ctx.platform,
            "name": ctx.name,
            "status": ctx.status,
        }
    })


@contexts_bp.put("/<context_id:str>")
async def update_context(request: Request, context_id: str):
    """更新上下文（备注名称等）"""
    data = request.json or {}
    try:
        ctx = await BrowserContext.get(id=context_id)
    except DoesNotExist:
        return json({"success": False, "error": "Context not found"}, status=404)

    if "name" in data:
        ctx.name = data["name"]
    if "status" in data:
        status = data["status"]
        if status not in [ContextStatus.PENDING.value, ContextStatus.LOGGED_IN.value, ContextStatus.DISABLED.value]:
            return json({"success": False, "error": "Invalid status"}, status=400)
        if ctx.status == ContextStatus.IN_USE.value:
            return json({"success": False, "error": "Context is in use"}, status=400)
        ctx.status = status
    await ctx.save()

    return json({"success": True, "data": {"id": str(ctx.id), "name": ctx.name, "status": ctx.status}})


@contexts_bp.delete("/<context_id:str>")
async def delete_context(request: Request, context_id: str):
    """删除上下文"""
    try:
        ctx = await BrowserContext.get(id=context_id)
    except DoesNotExist:
        return json({"success": False, "error": "Context not found"}, status=404)

    if ctx.status == ContextStatus.IN_USE.value:
        return json({"success": False, "error": "Context is in use, cannot delete"}, status=400)

    # 如果有 AgentBay context，尝试删除
    if ctx.context_id:
        try:
            from agentbay import AsyncAgentBay
            from config.settings import global_settings
            agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
            agent_bay_context = await agent_bay.context.get(ctx.context_id, create=False)
            if agent_bay_context.success and agent_bay_context.context:
                await agent_bay.context.delete(agent_bay_context.context)
        except Exception as e:
            logger.warning(f"Failed to delete AgentBay context: {e}")

    await ctx.delete()
    return json({"success": True})


@contexts_bp.post("/<context_id:str>/login")
async def start_login(request: Request, context_id: str):
    """启动云浏览器登录流程"""
    try:
        ctx = await BrowserContext.get(id=context_id)
    except DoesNotExist:
        return json({"success": False, "error": "Context not found"}, status=404)

    if ctx.status == ContextStatus.IN_USE.value:
        return json({"success": False, "error": "Context is in use"}, status=400)

    from agentbay import AsyncAgentBay
    from config.settings import global_settings

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    # 构建持久化 context key
    context_key = f"{ctx.platform}-context:default:{context_id}"

    # 获取或创建 AgentBay context
    context_result = await agent_bay.context.get(context_key, create=True)
    if not context_result.success:
        return json({"success": False, "error": f"Failed to create context: {context_result.error_message}"}, status=500)

    # 创建临时 session
    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "login", "context_id": str(ctx.id)},
            image_id="browser_latest",
            browser_context=AgentBayContext(context_result.context.id, auto_upload=False)
        )
    )
    if not session_result.success:
        return json({"success": False, "error": f"Failed to create session: {session_result.error_message}"}, status=500)

    session = session_result.session

    try:
        target_url = ctx.target_url or f"https://www.{ctx.platform}.com"
        if not target_url.startswith("http"):
            target_url = f"https://{target_url}"

        browser_url = session.resource_url

        # 保存 session 信息到 Redis（用于后续确认）
        from utils.cache import get_redis
        redis = await get_redis()
        await redis.setex(
            f"login_session:{context_id}",
            300,
            f"{session.session_id}|{context_result.context.id}|{context_key}"
        )

        asyncio.create_task(_initialize_login_browser(agent_bay, session, target_url, context_id))

        return json({
            "success": True,
            "data": {
                "browser_url": browser_url,
                "context_id": str(ctx.id),
            }
        })

    except Exception as e:
        logger.error(f"Failed to start login: {e}")
        try:
            await agent_bay.delete(session, sync_context=False)
        except:
            pass
        return json({"success": False, "error": str(e)}, status=500)


async def _initialize_login_browser(agent_bay, session, target_url: str, context_id: str):
    """后台初始化云浏览器，避免登录弹窗等待完整导航流程。"""
    try:
        ok = await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(
                    devices=["desktop"],
                    operating_systems=["windows"],
                    locales=["zh-CN"],
                ),
            )
        )
        if not ok:
            raise RuntimeError("Failed to initialize browser")

        await session.browser.agent.navigate(target_url)
        await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"Failed to initialize login browser: {e}")
        try:
            from utils.cache import get_redis
            redis = await get_redis()
            await redis.delete(f"login_session:{context_id}")
        except Exception as redis_error:
            logger.warning(f"Failed to cleanup login session cache: {redis_error}")
        try:
            await agent_bay.delete(session, sync_context=False)
        except Exception as cleanup_error:
            logger.warning(f"Failed to cleanup login browser session: {cleanup_error}")


@contexts_bp.get("/<context_id:str>/cookies")
async def get_context_cookies(request: Request, context_id: str):
    """通过 context_id 查询当前上下文的 cookies"""
    try:
        ctx = await BrowserContext.get(id=context_id)
    except DoesNotExist:
        return json({"success": False, "error": "Context not found"}, status=404)

    if not ctx.context_id:
        return json({"success": False, "error": "Context has no AgentBay context, please login first"}, status=400)

    from agentbay import AsyncAgentBay, CreateSessionParams, BrowserContext as AgentBayContext
    from config.settings import global_settings
    from playwright.async_api import async_playwright

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    # 获取持久化 context
    context_result = await agent_bay.context.get(ctx.context_id, create=False)
    if not context_result.success or not context_result.context:
        return json({"success": False, "error": "AgentBay context not found"}, status=404)

    # 创建临时 session
    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "cookies", "context_id": str(ctx.id)},
            image_id="browser_latest",
            browser_context=AgentBayContext(context_result.context.id, auto_upload=False)
        )
    )
    if not session_result.success:
        return json({"success": False, "error": f"Failed to create session: {session_result.error_message}"}, status=500)

    session = session_result.session
    pw = None

    try:
        # 初始化浏览器
        from agentbay import BrowserOption, BrowserScreen, BrowserFingerprint
        ok = await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=False,
                use_stealth=True,
                fingerprint=BrowserFingerprint(
                    devices=["desktop"],
                    operating_systems=["windows"],
                    locales=["zh-CN"],
                ),
            )
        )
        if not ok:
            return json({"success": False, "error": "Failed to initialize browser"}, status=500)

        # 通过 CDP 连接获取 cookies
        endpoint_url = await session.browser.get_endpoint_url()
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(endpoint_url)
        browser_ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        cookies = await browser_ctx.cookies()

        # 关闭 browser 连接
        try:
            cdp = await browser.new_browser_cdp_session()
            await cdp.send('Browser.close')
        except:
            pass
        await browser.close()

        cookies_list = cookies
        cookies_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        return json({
            "success": True,
            "data": {
                "context_id": str(ctx.id),
                "platform": ctx.platform,
                "cookies": cookies_list,
                "cookies_str": cookies_str,
            }
        })

    except Exception as e:
        logger.error(f"Failed to get cookies: {e}")
        return json({"success": False, "error": str(e)}, status=500)

    finally:
        # 清理资源
        try:
            if pw:
                await pw.stop()
        except:
            pass
        try:
            await agent_bay.delete(session, sync_context=False)
        except Exception as e:
            logger.warning(f"Failed to cleanup session: {e}")


@contexts_bp.post("/<context_id:str>/confirm-login")
async def confirm_login(request: Request, context_id: str):
    """确认登录完成，关闭 session 并落盘 cookies"""
    try:
        ctx = await BrowserContext.get(id=context_id)
    except DoesNotExist:
        return json({"success": False, "error": "Context not found"}, status=404)

    from agentbay import AsyncAgentBay
    from config.settings import global_settings
    from utils.cache import get_redis

    redis = await get_redis()
    session_info = await redis.get(f"login_session:{context_id}")

    if not session_info:
        return json({"success": False, "error": "Login session expired, please retry"}, status=400)

    parts = session_info.decode().split("|")
    session_id, ab_context_id, context_key = parts[0], parts[1], parts[2]

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    try:
        session_result = await agent_bay.get(session_id)
        if not session_result.success or not session_result.session:
            return json({
                "success": False,
                "error": f"Login session not found: {session_result.error_message}",
            }, status=400)

        session = session_result.session

        # 先关闭浏览器，让 cookies 从内存刷到磁盘
        try:
            endpoint_url = await session.browser.get_endpoint_url()
            if endpoint_url:
                from sanic import Sanic
                app = Sanic.get_app()
                pw = app.ctx.playwright
                browser = await pw.chromium.connect_over_cdp(endpoint_url)
                try:
                    cdp = await browser.new_browser_cdp_session()
                    await cdp.send('Browser.close')
                    await asyncio.sleep(1)
                except:
                    pass
                await browser.close()
                logger.info(f"Browser closed, cookies flushed to disk")
        except Exception as e:
            logger.warning(f"Failed to close browser before sync: {e}")

        # 关闭浏览器后再 sync + delete，确保 cookies 落盘
        await agent_bay.delete(session, sync_context=True)
        logger.info(f"Session deleted, cookies saved to context: {ab_context_id}")

    except Exception as e:
        logger.error(f"Failed to save context: {e}")
        return json({"success": False, "error": str(e)}, status=500)

    # 更新上下文状态
    ctx.status = ContextStatus.LOGGED_IN.value
    ctx.context_id = context_key
    await ctx.save()

    # 清理 Redis
    await redis.delete(f"login_session:{context_id}")

    return json({
        "success": True,
        "data": {
            "id": str(ctx.id),
            "status": ctx.status,
        }
    })
