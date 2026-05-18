# -*- coding: utf-8 -*-
"""Doctor helpers for DB/Redis/AgentBay context diagnostics."""

from typing import Any

from agentbay import AsyncAgentBay

from config.settings import global_settings
from models.context import BrowserContext
from models.task import Task
from utils.cache import get_redis

from .runtime import get_agentbay_context, resolve_context


async def doctor_context(ctx: BrowserContext) -> dict[str, Any]:
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
    context_result = await get_agentbay_context(agent_bay, ctx, create=False)
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


async def doctor_subject(subject: str, context: str | None = None) -> dict[str, Any]:
    if subject == "env":
        return {
            "app_port": global_settings.app.port,
            "database": f"{global_settings.database.host}:{global_settings.database.port}/{global_settings.database.name}",
            "redis": f"{global_settings.redis.host}:{global_settings.redis.port}/{global_settings.redis.db}",
            "agentbay_image_id": global_settings.agentbay.image_id,
            "agentbay_api_key_configured": bool(global_settings.agentbay.api_key),
        }
    if subject == "db":
        return {"contexts": await BrowserContext.all().count(), "tasks": await Task.all().count()}
    if subject == "redis":
        redis = await get_redis()
        keys = await redis.keys("login_session:*")
        await redis.aclose()
        return {"login_sessions": [key.decode() if isinstance(key, bytes) else key for key in keys]}
    if subject == "agentbay":
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        probe = await agent_bay.context.get("crawler-cli-doctor-probe", create=True)
        payload = {"context_get": probe.success, "context_id": getattr(probe.context, "id", None), "error": probe.error_message}
        if probe.success and probe.context:
            try:
                await agent_bay.context.delete(probe.context)
            except Exception:
                pass
        return payload
    if subject == "context":
        if not context:
            raise SystemExit("context is required")
        return await doctor_context(await resolve_context(context))
    if subject == "login-session":
        if not context:
            raise SystemExit("context is required")
        ctx = await resolve_context(context)
        redis = await get_redis()
        key = f"login_session:{ctx.id}"
        raw = await redis.get(key)
        payload = {"key": key, "value": raw.decode() if raw else None, "ttl": await redis.ttl(key) if raw else None}
        await redis.aclose()
        return payload
    raise SystemExit(f"Unknown doctor subject: {subject}")
