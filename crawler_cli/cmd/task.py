# -*- coding: utf-8 -*-
"""Task commands."""

from models.context import BrowserContext
from models.task import Task, TaskStatus

from crawler_cli.cmd.run import KEYWORD_TASK_TYPES
from crawler_cli.internal.io import print_json, split_csv
from crawler_cli.internal.runtime import app_runtime, db_runtime, resolve_context
from crawler_cli.internal.tasking import run_task_once


async def cmd_task_list(args) -> None:
    async with db_runtime():
        query = Task.all()
        if args.status:
            query = query.filter(status=args.status)
        tasks = await query.order_by("-created_at").limit(args.limit)
        print_json([await task.to_agent_readable() for task in tasks])


async def cmd_task_show(args) -> None:
    async with db_runtime():
        task = await Task.get(id=args.task_id)
        print_json(await task.to_agent_readable())


async def cmd_task_logs(args) -> None:
    async with db_runtime():
        task = await Task.get(id=args.task_id)
        if args.json:
            print_json(task.logs)
        else:
            print(task.get_logs_summary())


async def cmd_task_cancel(args) -> None:
    async with db_runtime():
        task = await Task.get(id=args.task_id)
        await task.cancel()
        print_json({"task_id": str(task.id), "status": task.status})


async def cmd_task_retry(args) -> None:
    async with app_runtime(with_playwright=True):
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
        result = await run_task_once(task, ctx)
        print_json({"task_id": str(task.id), "status": (await Task.get(id=task.id)).status, "result": result})


async def cmd_task_create_keyword(args) -> None:
    platform = args.platform
    if platform not in KEYWORD_TASK_TYPES:
        raise SystemExit(f"Unsupported keyword platform: {platform}")
    async with db_runtime():
        ctx = await resolve_context(args.context, platform=platform)
        task = await Task.create(
            source="cli",
            source_id="craw",
            task_type=KEYWORD_TASK_TYPES[platform],
            context_id=ctx.id,
            params={"keywords": split_csv(args.keywords), "limit": args.limit, "max_pages": args.max_pages},
            schedule=str(args.schedule or ""),
        )
        print_json({"task_id": str(task.id), "status": task.status, "task_type": task.task_type})


def register(subparsers) -> None:
    task = subparsers.add_parser("task", help="任务管理")
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
