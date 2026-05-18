# -*- coding: utf-8 -*-
"""Root parser for the craw CLI."""

from __future__ import annotations

import argparse
import asyncio

from . import agent, context, data, doctor, run, task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="craw", description="Micro-Sniper 通用爬虫 CLI")
    subparsers = parser.add_subparsers(dest="domain", required=True)
    context.register(subparsers)
    run.register(subparsers)
    agent.register(subparsers)
    task.register(subparsers)
    data.register(subparsers)
    doctor.register(subparsers)
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
