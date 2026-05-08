# -*- coding: utf-8 -*-
"""Sanic 应用配置"""
from sanic import Sanic
from sanic.config import Config
from sanic.request import Request
from sanic_cors import CORS
from sanic_ext import Extend
from playwright.async_api import async_playwright
from config.settings import settings, create_db_config
from utils.logger import logger
from tortoise import Tortoise
from types import SimpleNamespace
import asyncio


APP_SESSION_LABEL = "micro-sniper"


def create_app() -> Sanic:
    app: Sanic[Config, SimpleNamespace] = Sanic(settings.app.name)
    app.config.REQUEST_MAX_SIZE = 1024 * 1024 * 200
    app.config.REQUEST_TIMEOUT = 300
    app.config.RESPONSE_TIMEOUT = 300
    app.ctx.settings = settings

    app.static('/static', './static', name='static_files', index='index.html')
    app.static('/', './static/index.html', name='index')

    Extend(app)
    CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)
    app.enable_websocket()

    # 中间件
    from middleware.request_context import RequestContextMiddleware
    RequestContextMiddleware(app)
    from middleware.auth import AuthMiddleware
    AuthMiddleware(app)
    from middleware.exception_handler import ExceptionHandlerMiddleware
    ExceptionHandlerMiddleware(app)

    register_routes(app)
    setup_database(app)
    setup_playwright(app)
    setup_task_scheduler(app)

    return app


def register_routes(app: Sanic):
    @app.route("/health")
    async def health_check(request: Request):
        return {"status": "ok", "service": "micro-sniper"}

    from api.routes.contexts import contexts_bp
    from api.routes.sniper import products_bp, sniper_bp

    app.blueprint(contexts_bp)
    app.blueprint(sniper_bp)
    app.blueprint(products_bp)


def setup_database(app: Sanic):
    @app.before_server_start
    async def create_db(app: Sanic):
        await Tortoise.init(config=create_db_config())
        await Tortoise.generate_schemas()
        await ensure_runtime_schema()
        logger.info("Database initialized")
        await cleanup_startup_resources()

    @app.after_server_stop
    async def close_db(app: Sanic):
        await Tortoise.close_connections()
        logger.info("Database connections closed")


async def ensure_runtime_schema():
    """补齐旧库缺失字段；generate_schemas 不会修改已存在的表。"""
    conn = Tortoise.get_connection("default")
    await conn.execute_script(
        """
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS schedule VARCHAR(100) NOT NULL DEFAULT '';
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_run_at TIMESTAMP NULL;
        """
    )


def setup_playwright(app: Sanic):
    @app.before_server_start
    async def init_playwright(app: Sanic):
        logger.info("Initializing Playwright...")
        app.ctx.playwright = await async_playwright().start()
        logger.info("Playwright initialized")

    @app.before_server_stop
    async def cleanup_playwright(app: Sanic):
        logger.info("Cleaning up Playwright...")
        if hasattr(app.ctx, 'playwright'):
            await app.ctx.playwright.stop()
            logger.info("Playwright cleaned up")


def setup_task_scheduler(app: Sanic):
    @app.before_server_start
    async def start_task_scheduler(app: Sanic):
        from services.task_scheduler import scheduled_task_loop

        app.ctx.schedule_timer_task = asyncio.create_task(scheduled_task_loop())
        logger.info("Task scheduler timer started")

    @app.before_server_stop
    async def stop_task_scheduler(app: Sanic):
        scheduler_task = getattr(app.ctx, "schedule_timer_task", None)
        if scheduler_task:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
            logger.info("Task scheduler timer stopped")


async def cleanup_startup_resources():
    """应用启动时清理上一次进程遗留的任务和云浏览器会话。"""
    from agentbay import AsyncAgentBay
    from models.context import BrowserContext, ContextStatus
    from models.task import Task, TaskStatus

    active_statuses = [
        TaskStatus.PENDING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.WAITING_HUMAN_INPUT.value,
    ]

    tasks = await Task.filter(status__in=active_statuses)
    for task in tasks:
        await task.cancel()
    if tasks:
        logger.warning(f"Startup cleanup cancelled {len(tasks)} stale task(s)")

    released = await BrowserContext.filter(status=ContextStatus.IN_USE.value).update(
        status=ContextStatus.LOGGED_IN.value
    )
    if released:
        logger.warning(f"Startup cleanup released {released} in-use browser context(s)")

    if not settings.agentbay.api_key:
        logger.warning("Startup cleanup skipped AgentBay session cleanup: missing API key")
        return

    agent_bay = AsyncAgentBay(api_key=settings.agentbay.api_key)
    deleted = 0
    page = 1
    limit = 50

    while True:
        result = await agent_bay.list(labels={"app": APP_SESSION_LABEL}, page=page, limit=limit)
        if not result.success:
            logger.warning(f"Startup cleanup failed to list AgentBay sessions: {result.error_message}")
            break

        session_ids = result.session_ids or []
        for session_id in session_ids:
            try:
                session_result = await agent_bay.get(session_id)
                if session_result.success and session_result.session:
                    await agent_bay.delete(session_result.session, sync_context=False)
                    deleted += 1
            except Exception as e:
                logger.warning(f"Startup cleanup failed to delete AgentBay session {session_id}: {e}")

        if len(session_ids) < limit or deleted >= result.total_count:
            break
        page += 1

    if deleted:
        logger.warning(f"Startup cleanup deleted {deleted} AgentBay session(s)")
