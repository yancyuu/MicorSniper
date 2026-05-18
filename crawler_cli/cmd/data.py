# -*- coding: utf-8 -*-
"""Data query and export commands."""

import json
from pathlib import Path

from models.product_detail import ProductDetail
from models.product_link import ProductLink
from models.task import Task

from crawler_cli.internal.io import print_json, read_lines
from crawler_cli.internal.runtime import db_runtime


async def cmd_data_links(args) -> None:
    async with db_runtime():
        query = ProductLink.all()
        if args.task:
            query = query.filter(task_id=args.task)
        if args.platform:
            query = query.filter(platform=args.platform)
        rows = await query.order_by("-created_at").limit(args.limit)
        print_json([row.to_dict() for row in rows])


async def cmd_data_details(args) -> None:
    async with db_runtime():
        query = ProductDetail.all()
        if args.platform:
            query = query.filter(platform=args.platform)
        if args.task:
            query = query.filter(task_id=args.task)
        rows = await query.order_by("-updated_at").limit(args.limit)
        print_json([row.to_dict() for row in rows])


async def cmd_data_export(args) -> None:
    async with db_runtime():
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
    urls = read_lines(args.file)
    if not urls:
        raise SystemExit("No URLs found")
    async with db_runtime():
        items = [
            ProductLink(task_id=args.task_id, platform=args.platform, keyword=args.keyword or "", url=url, raw_url=url)
            for url in urls
        ]
        await ProductLink.upsert_bulk(items)
        print_json({"imported": len(items), "platform": args.platform})


def register(subparsers) -> None:
    data = subparsers.add_parser("data", help="数据查看与导出")
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
