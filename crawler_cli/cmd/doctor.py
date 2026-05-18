# -*- coding: utf-8 -*-
"""Top-level doctor commands."""

from crawler_cli.internal.doctor import doctor_subject
from crawler_cli.internal.io import print_json
from crawler_cli.internal.runtime import db_runtime


async def cmd_doctor(args) -> None:
    async with db_runtime():
        print_json(await doctor_subject(args.subject, args.context))


def register(subparsers) -> None:
    doctor = subparsers.add_parser("doctor", help="系统诊断")
    doctor.add_argument("subject", choices=["env", "agentbay", "redis", "db", "context", "login-session"])
    doctor.add_argument("context", nargs="?")
    doctor.set_defaults(func=cmd_doctor)
