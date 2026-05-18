# -*- coding: utf-8 -*-
"""AgentBay-native act/extract/crawl commands."""

from agentbay import ActOptions, AsyncAgentBay, ExtractOptions

from config.settings import global_settings
from models.task import Task

from crawler_cli.internal.agent_ops import agent_crawl, agent_extract, browser_option
from crawler_cli.internal.io import print_json
from crawler_cli.internal.runtime import create_browser_session, db_runtime, resolve_context


async def cmd_agent_act(args) -> None:
    async with db_runtime():
        ctx = await resolve_context(args.context)
        agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
        session_result = await create_browser_session(agent_bay, ctx, kind="cli-agent-act", auto_upload=False)
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
            await session.browser.initialize(browser_option())
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
            print_json(result)
        except Exception as exc:
            await task.fail(str(exc))
            raise
        finally:
            if not args.keep_session:
                await agent_bay.delete(session, sync_context=False)


async def cmd_agent_extract(args) -> None:
    async with db_runtime():
        ctx = await resolve_context(args.context) if args.context else None
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
            session_result = await create_browser_session(agent_bay, ctx, kind="cli-agent-extract", auto_upload=False)
            if not session_result.success or not session_result.session:
                raise SystemExit(f"Failed to create session: {session_result.error_message}")
            session = session_result.session
            created_session = True
            await session.browser.initialize(browser_option())
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
            try:
                ok, _payload, result = await agent_extract(
                    session.browser.agent,
                    goal=args.goal,
                    timeout=args.timeout,
                    mode=args.extract_mode,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "suggestion": "try --timeout 90 or --extract-mode both"}
                await task.log_step(2, "执行 Agent 抽取异常", {"goal": args.goal, "timeout": args.timeout}, result, "failed")
                await task.fail(str(exc))
                print_json({"task_id": str(task.id), "session_id": session.session_id, "result": result})
                return
            await task.log_step(2, "执行 Agent 抽取", {"goal": args.goal}, result, "completed" if ok else "failed")
            if ok:
                total = len(result.get("records") or []) if isinstance(result, dict) else 0
                await task.complete({"task_id": str(task.id), "total": total, "success": total, **result})
            else:
                await task.fail("Agent extract failed")
            print_json({"task_id": str(task.id), "session_id": session.session_id, "result": result})
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
        print_json({"session_id": args.session_id, "closed": False, "error": session_result.error_message})
        return
    delete_result = await agent_bay.delete(session_result.session, sync_context=args.sync_context)
    print_json({"session_id": args.session_id, "closed": delete_result.success, "error": delete_result.error_message})


async def cmd_agent_crawl(args) -> None:
    async with db_runtime():
        ctx = await resolve_context(args.context)
        result = await agent_crawl(ctx, args.start_url, args.goal, args.max_steps, args.extract_timeout, args.extract_mode)
        print_json(result)


async def cmd_agent_inspect(args) -> None:
    async with db_runtime():
        task = await Task.get(id=args.run_id)
        print_json(await task.to_agent_readable())


async def cmd_agent_recover(args) -> None:
    async with db_runtime():
        task = await Task.get(id=args.run_id)
        params = task.params or {}
        ctx = await resolve_context(str(task.context_id))
        result = await agent_crawl(
            ctx,
            params.get("start_url", ""),
            params.get("goal", ""),
            args.max_steps or params.get("max_steps", 5),
            args.extract_timeout,
            args.extract_mode,
        )
        print_json(result)


def register(subparsers) -> None:
    agent = subparsers.add_parser("agent", help="AgentBay 原生自愈采集")
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
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--extract-mode", choices=["text", "vision", "both"], default="both")
    p.add_argument("--keep-session", action="store_true")
    p.set_defaults(func=cmd_agent_extract)
    p = agent_sub.add_parser("crawl")
    p.add_argument("--context", required=True)
    p.add_argument("--start-url", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--max-steps", type=int, default=5)
    p.add_argument("--extract-timeout", type=int, default=120)
    p.add_argument("--extract-mode", choices=["text", "vision", "both"], default="both")
    p.set_defaults(func=cmd_agent_crawl)
    p = agent_sub.add_parser("inspect")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_agent_inspect)
    p = agent_sub.add_parser("recover")
    p.add_argument("run_id")
    p.add_argument("--max-steps", type=int, default=0)
    p.add_argument("--extract-timeout", type=int, default=120)
    p.add_argument("--extract-mode", choices=["text", "vision", "both"], default="both")
    p.set_defaults(func=cmd_agent_recover)
    p = agent_sub.add_parser("replay")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_agent_inspect)
    p = agent_sub.add_parser("close")
    p.add_argument("session_id")
    p.add_argument("--sync-context", action="store_true")
    p.set_defaults(func=cmd_agent_close)
