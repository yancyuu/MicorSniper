# -*- coding: utf-8 -*-
"""Small IO helpers for CLI commands."""

import json
from pathlib import Path
from typing import Any


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def read_lines(path_or_values: str | None) -> list[str]:
    if not path_or_values:
        return []
    path = Path(path_or_values)
    if path.exists():
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return split_csv(path_or_values)
