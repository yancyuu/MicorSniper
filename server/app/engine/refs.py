"""Resolve `{{node_id.dotted.path}}` references in node configs (FR-5).

A reference root is a node id; the remaining dotted path indexes into that
node's *full* result dict (`{success, data, screenshot}`), so `{{n3.data}}`
yields the data and `{{n3.data[].href}}` projects the `href` column out of a
list under `data`. Missing references resolve to `None` and append a warning.
"""
from __future__ import annotations
import json
import re
from typing import Any

# {{ node_id.path.to[].value }}
_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+(?:\[\d*\])?)*)\s*\}\}")
_SEG_RE = re.compile(r"^([A-Za-z0-9_\-]+)(\[\d*\])?$")


def resolve_config(
    config: dict, node_results: dict[str, dict]
) -> tuple[dict, list[str]]:
    """Recursively resolve all references in a node config.

    Returns the resolved config and a list of human-readable warnings for any
    references that could not be resolved.
    """
    warnings: list[str] = []
    resolved = _resolve_value(config, node_results, warnings)
    return resolved, warnings


def _resolve_value(value: Any, node_results: dict[str, dict], warnings: list[str]) -> Any:
    if isinstance(value, str):
        return _resolve_string(value, node_results, warnings)
    if isinstance(value, dict):
        return {k: _resolve_value(v, node_results, warnings) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, node_results, warnings) for v in value]
    return value


def _resolve_string(s: str, node_results: dict[str, dict], warnings: list[str]) -> Any:
    matches = list(_REF_RE.finditer(s))
    if not matches:
        return s

    # Whole-string single reference → return the raw resolved value (may be a
    # list/dict) so downstream config can consume structured data directly.
    if len(matches) == 1 and matches[0].group(0).strip() == s.strip():
        return _lookup(matches[0].group(1), node_results, warnings)

    # Otherwise interpolate each reference into the surrounding string.
    def repl(m: re.Match) -> str:
        val = _lookup(m.group(1), node_results, warnings)
        if val is None:
            return ""
        if isinstance(val, (list, dict)):
            return json.dumps(val, ensure_ascii=False)
        return str(val)

    return _REF_RE.sub(repl, s)


def _lookup(path: str, node_results: dict[str, dict], warnings: list[str]) -> Any:
    tokens = _tokenize(path)
    node_id, op = tokens[0]
    if node_id not in node_results:
        warnings.append(f"引用的节点 '{node_id}' 暂无结果,已置空")
        return None

    cur: Any = _apply_op(node_results[node_id], op)
    for key, key_op in tokens[1:]:
        cur = _get_key(cur, key)
        cur = _apply_op(cur, key_op)
        if cur is None:
            warnings.append(f"引用路径 '{{{{{path}}}}}' 解析为空")
            return None
    return cur


def _tokenize(path: str) -> list[tuple[str, str | int | None]]:
    tokens: list[tuple[str, str | int | None]] = []
    for raw in path.split("."):
        m = _SEG_RE.match(raw)
        if not m:
            tokens.append((raw, None))
            continue
        key, bracket = m.group(1), m.group(2)
        if bracket is None:
            tokens.append((key, None))
        elif bracket == "[]":
            tokens.append((key, "all"))
        else:
            tokens.append((key, int(bracket[1:-1])))
    return tokens


def _get_key(cur: Any, key: str) -> Any:
    # Projection: applying a key to a list maps it over each element.
    if isinstance(cur, list):
        out = [item.get(key) for item in cur if isinstance(item, dict)]
        return out
    if isinstance(cur, dict):
        return cur.get(key)
    return None


def _apply_op(cur: Any, op: str | int | None) -> Any:
    if op is None or op == "all":
        return cur
    if isinstance(op, int) and isinstance(cur, list):
        return cur[op] if -len(cur) <= op < len(cur) else None
    return cur
