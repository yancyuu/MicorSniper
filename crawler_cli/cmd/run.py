# -*- coding: utf-8 -*-
"""One-shot run commands."""

from models.task import Task

from crawler_cli.internal.agent_ops import agent_crawl
from crawler_cli.internal.io import print_json, read_lines, split_csv
from crawler_cli.internal.runtime import app_runtime, db_runtime, resolve_context
from crawler_cli.internal.tasking import run_task_once


KEYWORD_TASK_TYPES = {
    "taobao": "taobao_link_search",
    "tmall": "tmall_link_search",
    "jd": "jd_link_search",
    "xiaohongshu": "xiaohongshu_link_search",
}


async def cmd_run_keyword(args) -> None:
    platform = args.platform
    if platform not in KEYWORD_TASK_TYPES:
        raise SystemExit(f"Unsupported keyword platform: {platform}")
    async with app_runtime(with_playwright=True):
        ctx = await resolve_context(args.context, platform=platform)
        params = {
            "keywords": split_csv(args.keywords),
            "limit": args.limit,
            "max_pages": args.max_pages,
            "pages_per_batch": args.pages_per_batch,
            "batch_interval_minutes": 0,
        }
        task = await Task.create(source="cli", source_id="craw", task_type=KEYWORD_TASK_TYPES[platform], context_id=ctx.id, params=params)
        result = await run_task_once(task, ctx)
        print_json({"task_id": str(task.id), "result": result, "status": (await Task.get(id=task.id)).status})


async def cmd_run_urls(args) -> None:
    urls = read_lines(args.urls)
    if not urls:
        raise SystemExit("No URLs provided")
    async with app_runtime(with_playwright=True):
        ctx = await resolve_context(args.context)
        task = await Task.create(
            source="cli",
            source_id="craw",
            task_type="search_by_urls",
            context_id=ctx.id,
            params={"urls": urls, "batch_size": args.batch_size, "batch_interval_minutes": 0},
        )
        result = await run_task_once(task, ctx)
        print_json({"task_id": str(task.id), "result": result, "status": (await Task.get(id=task.id)).status})


async def cmd_run_detail(args) -> None:
    urls = read_lines(args.urls)
    if not urls:
        raise SystemExit("No URLs provided")
    async with app_runtime(with_playwright=True):
        ctx = await resolve_context(args.context)
        task = await Task.create(
            source="cli",
            source_id="craw",
            task_type="product_detail_fetch",
            context_id=ctx.id,
            params={"urls": urls, "batch_size": args.batch_size, "batch_interval_minutes": 0},
        )
        result = await run_task_once(task, ctx)
        print_json({"task_id": str(task.id), "result": result, "status": (await Task.get(id=task.id)).status})


async def cmd_run_page(args) -> None:
    async with db_runtime():
        ctx = await resolve_context(args.context)
        result = await agent_crawl(ctx, args.url, args.goal, args.max_steps, args.extract_timeout, args.extract_mode)
        print_json(result)


def register(subparsers) -> None:
    run = subparsers.add_parser("run", help="一次性采集")
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
    p.add_argument("--extract-timeout", type=int, default=120)
    p.add_argument("--extract-mode", choices=["text", "vision", "both"], default="both")
    p.set_defaults(func=cmd_run_page)
