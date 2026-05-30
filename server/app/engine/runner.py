"""Workflow runner — topological sort + sequential node execution with crawl4ai."""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime
from typing import Any, Callable, Awaitable
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from app.store import STORE
from app.config import settings
from app.proxy_helper import get_proxy_url
from app.models import RunResult
from app.engine import refs
from app.engine.nodes import (
    execute_crawl,
    execute_batch_crawl,
    execute_css_extract,
    execute_ai_extract,
    execute_js_execute,
    execute_paginate,
)


def _topological_sort(nodes: list[dict], edges: list[dict]) -> list[str]:
    """Return node IDs in execution order."""
    in_degree: dict[str, int] = {}
    adjacency: dict[str, list[str]] = {}

    for node in nodes:
        nid = node["id"]
        in_degree[nid] = 0
        adjacency[nid] = []

    for edge in edges:
        adjacency[edge["source"]].append(edge["target"])
        in_degree[edge["target"]] = in_degree.get(edge["target"], 0) + 1

    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    sorted_ids: list[str] = []

    while queue:
        current = queue.pop(0)
        sorted_ids.append(current)
        for neighbor in adjacency.get(current, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return sorted_ids


def _paginated_child_ids(nodes: list[dict], edges: list[dict], node_map: dict) -> set[str]:
    """IDs of extract nodes owned by a paginate node via an out-edge (FR-4).

    These run *inside* `execute_paginate` once per page, so the main topo loop
    must skip them to avoid a duplicate final-page execution.
    """
    child_ids: set[str] = set()
    for n in nodes:
        if n.get("data", {}).get("type") != "paginate":
            continue
        for e in edges:
            if e["source"] != n["id"]:
                continue
            child = node_map.get(e["target"])
            if child and child.get("data", {}).get("type") in ("css_extract", "ai_extract"):
                child_ids.add(child["id"])
    return child_ids


def _collect_upstream_urls(
    node_id: str,
    edges: list[dict],
    node_map: dict,
    node_results: dict[str, dict],
) -> list[str] | None:
    """Collect URLs from upstream node results (e.g. links from a Crawl node)."""
    incoming = [e for e in edges if e["target"] == node_id]
    urls = []
    for e in incoming:
        parent_result = node_results.get(e["source"], {})
        data = parent_result.get("data", {})
        if isinstance(data, dict):
            links = data.get("links", [])
            if isinstance(links, list):
                for link in links:
                    if isinstance(link, dict) and "href" in link:
                        urls.append(link["href"])
                    elif isinstance(link, str):
                        urls.append(link)
    return urls if urls else None


def _build_browser_config(workflow_id: str, nodes: list[dict], proxy_url: str | None = None) -> tuple[BrowserConfig | None, str | None]:
    """Resolve a BrowserConfig from an optional context node (FR-3, T015/T016).

    Returns (config, error). When error is set the caller should abort and
    broadcast it. Precedence:
      1. context node with an existing profile dir → persistent context
      2. context node, no profile but stored cookies → cookie injection
      3. context node, neither → error (session invalid)
      4. no context node → ephemeral run-{workflow_id} profile (US1 baseline)
    """
    session_name = None
    for n in nodes:
        if n.get("data", {}).get("type") == "context":
            session_name = n["data"].get("config", {}).get("session_name")
            break

    common = dict(
        headless=True,
        viewport_width=1280,
        viewport_height=800,
        extra_args=["--disable-blink-features=AutomationControlled"],
        enable_stealth=True,
        user_agent_mode="random",
    )
    if proxy_url:
        common["proxy"] = proxy_url

    if not session_name:
        profile_dir = STORE.profiles_dir / f"run-{workflow_id}"
        return BrowserConfig(
            use_persistent_context=True, user_data_dir=str(profile_dir), **common
        ), None

    profile_dir = STORE.profiles_dir / session_name
    if profile_dir.is_dir():
        return BrowserConfig(
            use_persistent_context=True, user_data_dir=str(profile_dir), **common
        ), None

    return session_name, None  # sentinel: caller resolves cookies asynchronously


async def run_workflow(
    workflow_id: str,
    nodes: list[dict],
    edges: list[dict],
    on_status: Callable[[dict], Awaitable[None]],
):
    """Execute a workflow by running nodes in topological order using crawl4ai."""
    if not nodes:
        await on_status({"node_id": "root", "status": "completed", "log": "No nodes to execute"})
        return

    # Resolve proxy and browser config from an optional context node (FR-3).
    proxy_url = await get_proxy_url()
    cfg_or_session, _ = _build_browser_config(workflow_id, nodes, proxy_url)
    if isinstance(cfg_or_session, str):
        # context node selected but profile dir missing → fall back to cookies (T015)
        session_name = cfg_or_session
        session = await STORE.load_session(session_name)
        if not session or not session.cookies:
            await on_status({
                "node_id": "root", "status": "failed", "level": "error",
                "log": f"会话 '{session_name}' 不存在或已失效(无 profile 且无 cookie),请重新登录该上下文",
            })
            return
        await on_status({
            "node_id": "root", "status": "running", "level": "info",
            "log": f"会话 '{session_name}' 无 profile 目录,改用已存 {len(session.cookies)} 个 cookie 注入",
        })
        browser_config = BrowserConfig(
            headless=True,
            viewport_width=1280,
            viewport_height=800,
            extra_args=["--disable-blink-features=AutomationControlled"],
            cookies=session.cookies,
            proxy=proxy_url,
            enable_stealth=True,
            user_agent_mode="random",
        )
    else:
        browser_config = cfg_or_session

    session_id = f"wf-{workflow_id}"
    run_id = uuid.uuid4().hex
    started_at = datetime.now().isoformat()
    node_results: dict[str, dict] = {}
    final_status = "completed"

    async def _finalize(status: str):
        """Persist a RunResult summarizing this run (FR-6)."""
        node_outputs = {nid: r.get("data") for nid, r in node_results.items()}
        await STORE.save_result(RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=status,
            started_at=started_at,
            finished_at=datetime.now().isoformat(),
            node_outputs=node_outputs,
            items=_primary_items(nodes, node_map, node_results),
        ))

    node_map = {n["id"]: n for n in nodes}

    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            execution_order = _topological_sort(nodes, edges)
            skip_ids = _paginated_child_ids(nodes, edges, node_map)  # FR-4

            for node_id in execution_order:
                node = node_map.get(node_id)
                if not node:
                    continue

                data = node.get("data", {})
                node_type = data.get("type")
                node_name = data.get("label", node_type)

                # Context nodes carry no executable action; extract children of a
                # paginate node run inside execute_paginate (skip to avoid double-run).
                if node_type == "context" or node_id in skip_ids:
                    continue

                # Resolve {{node.path}} references in this node's config (FR-5).
                raw_config = data.get("config", {})
                node_config, warns = refs.resolve_config(raw_config, node_results)
                for w in warns:
                    await on_status({
                        "node_id": node_id, "status": "running", "level": "warn",
                        "log": w, "node_name": node_name,
                    })

                await on_status({
                    "node_id": node_id,
                    "status": "running",
                    "log": f"Executing: {node_name}",
                    "node_name": node_name,
                    "level": "info",
                })

                try:
                    result: dict = {}

                    if node_type == "crawl":
                        result = await execute_crawl(crawler, session_id, node_config)

                    elif node_type == "batch_crawl":
                        upstream_urls = _collect_upstream_urls(node_id, edges, node_map, node_results)
                        result = await execute_batch_crawl(crawler, session_id, node_config, upstream_urls)

                    elif node_type == "css_extract":
                        result = await execute_css_extract(crawler, session_id, node_config)

                    elif node_type == "ai_extract":
                        result = await execute_ai_extract(crawler, session_id, node_config)

                    elif node_type == "js_execute":
                        result = await execute_js_execute(crawler, session_id, node_config)

                    elif node_type == "paginate":
                        extract_fn = _resolve_paginate_child(
                            node_id, edges, node_map, node_results, crawler, session_id
                        )
                        result = await execute_paginate(crawler, session_id, node_config, extract_fn)

                    await on_status({
                        "node_id": node_id,
                        "status": "success",
                        "screenshot": result.get("screenshot", ""),
                        "result": result.get("data"),
                        "log": f"Completed: {node_name}",
                        "node_name": node_name,
                        "level": "info",
                    })
                    node_results[node_id] = result

                except asyncio.CancelledError:
                    raise

                except Exception as err:
                    error_screenshot = await _safe_error_screenshot(crawler, session_id)
                    await on_status({
                        "node_id": node_id,
                        "status": "error",
                        "screenshot": error_screenshot,
                        "error": str(err),
                        "log": f"Failed: {node_name} - {err}",
                        "node_name": node_name,
                        "level": "error",
                    })
                    await on_status({
                        "node_id": "root",
                        "status": "failed",
                        "log": "Workflow failed",
                        "level": "error",
                    })
                    final_status = "failed"
                    await _finalize(final_status)
                    return

            await on_status({
                "node_id": "root",
                "status": "completed",
                "log": "Workflow completed successfully",
                "level": "info",
            })
            final_status = "completed"
            await _finalize(final_status)

    except asyncio.CancelledError:
        # Stopped via /stop (FR-9) — broadcast and persist the partial run.
        await on_status({
            "node_id": "root", "status": "failed", "log": "Workflow stopped", "level": "error",
        })
        try:
            await _finalize("stopped")
        except Exception:
            pass
        raise

    except Exception as err:
        await on_status({
            "node_id": "root",
            "status": "failed",
            "log": f"Workflow error: {err}",
            "level": "error",
        })
        try:
            await _finalize("failed")
        except Exception:
            pass


def _resolve_paginate_child(
    node_id: str,
    edges: list[dict],
    node_map: dict,
    node_results: dict[str, dict],
    crawler: AsyncWebCrawler,
    session_id: str,
) -> Callable[[], Awaitable[dict]] | None:
    """Build the per-page extraction callable for a paginate node."""
    for e in edges:
        if e["source"] != node_id:
            continue
        child = node_map.get(e["target"])
        if not child:
            continue
        ctype = child["data"].get("type")
        if ctype not in ("css_extract", "ai_extract"):
            continue
        raw_cfg = child["data"].get("config", {})
        cfg, _ = refs.resolve_config(raw_cfg, node_results)
        if ctype == "css_extract":
            return lambda c=cfg: execute_css_extract(crawler, session_id, c)
        return lambda c=cfg: execute_ai_extract(crawler, session_id, c)
    return None


def _primary_items(nodes: list[dict], node_map: dict, node_results: dict[str, dict]) -> list[Any]:
    """Pick the terminal extraction node's data as the workflow's main items."""
    items: list[Any] = []
    for n in nodes:
        ntype = n.get("data", {}).get("type")
        if ntype in ("css_extract", "ai_extract", "paginate"):
            data = node_results.get(n["id"], {}).get("data")
            if isinstance(data, list):
                items = data
            elif data is not None:
                items = [data]
    return items


async def _safe_error_screenshot(crawler: AsyncWebCrawler, session_id: str) -> str:
    try:
        run_cfg = CrawlerRunConfig(
            session_id=session_id,
            cache_mode=CacheMode.BYPASS,
            js_only=True,
            screenshot=True,
        )
        r = await crawler.arun(url="", config=run_cfg)
        if hasattr(r, "screenshot") and r.screenshot:
            return r.screenshot
    except Exception:
        pass
    return ""
