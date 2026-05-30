"""Session-based tool-calling agent.

The agent maintains conversation history per session. Each user message
appends to the history, so the LLM has full context across turns.

System prompt dynamically includes current node graph state.

Two modes:
  - Generate: user asks for something new → LLM calls create tools
  - Modify: user gives feedback → LLM calls update_node / re_run_node etc.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

from app.config import settings
from app.engine.generate import detect_login_wall, _browser_config_for
from app.engine.tools import (
    TOOL_SCHEMAS,
    TOOL_EXECUTORS,
    AgentContext,
    tool_to_node,
)

logger = logging.getLogger("agent")

MAX_TURNS = 12

# ── Session store: session_id → message history ──────────────────────

_sessions: dict[str, list[dict]] = {}


def get_or_create_session(session_id: str) -> list[dict]:
    """Get or create message history for a session."""
    if session_id not in _sessions:
        _sessions[session_id] = []
    return _sessions[session_id]


def clear_session(session_id: str) -> None:
    """Clear a session's history."""
    _sessions.pop(session_id, None)


def _build_system_prompt(current_nodes: list[dict], current_edges: list[dict]) -> str:
    """Build system prompt with current node graph state."""
    base = """你是一个专业的网页爬虫工作流管理器。你可以通过调用tool来生成和修改工作流。

常见网站：淘宝→taobao.com, 京东→jd.com, 小红书→xiaohongshu.com, 抖音→douyin.com, B站→bilibili.com, 小米→xiaomi.com

工具分类：
- 生成类：open_page, generate_schema, css_extract, ai_extract, paginate, js_execute
- 修改类：update_node（改节点配置）, re_run_node（重跑单个节点）, add_node（插入节点）, remove_node（删节点）
- 完成：finish

规则：
- 每个tool返回真实执行结果，看清结果再决定下一步
- 优先 css_extract，不行再 ai_extract
- 不要重复调相同参数的tool
- 完成后必须调 finish"""

    if not current_nodes:
        return base + "\n\n当前画布为空，用户需要生成新的工作流。"

    # Describe current node graph
    lines = ["当前画布上的节点："]
    for n in current_nodes:
        nd = n.get("data", {})
        nid = n.get("id", "?")
        ntype = nd.get("type", "?")
        label = nd.get("label", "?")
        status = nd.get("status", "idle")
        config = nd.get("config", {})
        result_preview = ""
        if nd.get("result"):
            r = nd["result"]
            if isinstance(r, list):
                result_preview = f" → {len(r)}条数据"
            elif isinstance(r, dict):
                result_preview = f" → {len(r)}个字段"

        info = ""
        if ntype == "crawl":
            info = config.get("url", "")
        elif ntype == "css_extract":
            info = config.get("base_selector", "")
        elif ntype == "ai_extract":
            info = nd.get("error", "") or (config.get("instruction", "")[:30] if config.get("instruction") else "")

        status_icon = "✅" if status == "success" else "❌" if status == "error" else "⏳" if status == "running" else "⬜"
        lines.append(f"  {status_icon} {nid}: [{ntype}] {label} | {info}{result_preview}")

    # Describe edges
    for e in current_edges:
        lines.append(f"  → {e.get('source', '?')} → {e.get('target', '?')}")

    lines.append("\n根据用户反馈，使用 update_node/re_run_node 等工具修改对应节点。如果用户要求全新任务，使用生成类工具。")

    return base + "\n\n" + "\n".join(lines)


async def _llm_chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Call the OpenAI-compatible chat API with optional tool use."""
    base_url = settings.LLM_BASE_URL or "https://api.openai.com/v1"
    api_key = settings.OPENAI_API_KEY
    model = settings.LLM_PROVIDER.split("/", 1)[-1] if "/" in settings.LLM_PROVIDER else "gpt-4o-mini"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def agent_run(
    message: str,
    session_id: str,
    current_nodes: list[dict] | None = None,
    current_edges: list[dict] | None = None,
    session_name: str | None = None,
    on_event: Any = None,
) -> dict:
    """Run one turn of the session-based agent.

    Args:
        message: User's chat message.
        session_id: Session ID for persistent history.
        current_nodes: Current nodes on the canvas (from frontend).
        current_edges: Current edges on the canvas (from frontend).
        session_name: Optional browser session for login.
        on_event: Async callback for SSE events (node, node_update, node_remove, etc.)

    Returns:
        Dict with final result summary.
    """
    current_nodes = current_nodes or []
    current_edges = current_edges or []

    # Get or create session history
    history = get_or_create_session(session_id)

    # Build system prompt with current state
    system_prompt = _build_system_prompt(current_nodes, current_edges)

    # Prepare messages for this LLM call
    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": message}]

    # Resolve browser session
    browser_config = await _browser_config_for(session_name)

    collected_items: list[dict] = []
    events: list[dict] = []

    async def _emit(event_type: str, data: dict):
        events.append({"event": event_type, "data": json.dumps(data, ensure_ascii=False)})
        if on_event:
            await on_event(event_type, data)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        ctx = AgentContext(
            crawler=crawler,
            current_nodes=current_nodes,
            current_edges=current_edges,
        )
        collected_nodes: list[dict] = []
        collected_edges: list[dict] = []

        # If this is the first message and canvas is empty, auto-fetch the URL
        url = _extract_url(message)
        if not current_nodes and url:
            print(f"[Agent] Auto-fetching: {url}", flush=True)
            fetch_cfg = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                wait_until="domcontentloaded",
                screenshot=True,
            )
            try:
                res = await crawler.arun(url=url, config=fetch_cfg)
                ctx.html = res.html or ""
                ctx.final_url = res.url or url
                import re
                m = re.search(r"<title[^>]*>(.*?)</title>", ctx.html, re.S | re.I)
                ctx.page_title = m.group(1).strip() if m else ""

                page_result = {
                    "title": ctx.page_title,
                    "final_url": ctx.final_url,
                    "html_length": len(ctx.html),
                    "has_login_wall": detect_login_wall(ctx.html, ctx.final_url),
                }
                node, edge = tool_to_node("open_page", {"url": url}, page_result, ctx)
                if node:
                    collected_nodes.append(node)
                    if edge:
                        collected_edges.append(edge)
                    await _emit("node", {"node": node, "edge": edge})

                html_summary = ctx.html[:4000] if len(ctx.html) > 4000 else ctx.html
                # Inject page context into user message
                messages[-1]["content"] += (
                    f"\n\n[系统自动获取了页面信息]\n"
                    f"标题: {ctx.page_title}\n"
                    f"需要登录: {page_result['has_login_wall']}\n"
                    f"HTML片段:\n{html_summary}"
                )
            except Exception as e:
                print(f"[Agent] Auto-fetch failed: {e}", flush=True)

        # Agent loop
        for turn in range(MAX_TURNS):
            print(f"[Agent] Turn {turn + 1}/{MAX_TURNS}", flush=True)
            try:
                response = await _llm_chat(messages, tools=TOOL_SCHEMAS)
            except Exception as e:
                logger.error(f"Agent LLM failed: {e}")
                raise RuntimeError(f"LLM 调用失败: {e}") from e

            tool_calls = response.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
            assistant_msg = response["choices"][0]["message"]
            messages.append(assistant_msg)
            history.append({"role": "assistant", "content": assistant_msg.get("content") or ""})

            if not tool_calls:
                break

            for tc in tool_calls:
                fn = tc["function"]
                tool_name = fn["name"]
                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_args = {}

                print(f"[Agent] Tool: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:120]})", flush=True)

                executor = TOOL_EXECUTORS.get(tool_name)
                if not executor:
                    tool_result = {"error": f"Unknown tool: {tool_name}"}
                else:
                    try:
                        tool_result = await executor(ctx, tool_args)
                    except Exception as e:
                        tool_result = {"error": str(e)}

                print(f"[Agent] Result: {json.dumps(tool_result, ensure_ascii=False)[:200]}", flush=True)

                # Feed back to LLM
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(tool_result, ensure_ascii=False)})

                # Collect extracted data
                if tool_result.get("data"):
                    collected_items.extend(tool_result["data"] if isinstance(tool_result["data"], list) else [tool_result["data"]])

                # Finish → done
                if tool_name == "finish":
                    # Save history
                    history.append({"role": "user", "content": message})
                    return {
                        "events": events,
                        "nodes": collected_nodes,
                        "edges": collected_edges,
                        "items": collected_items,
                        "summary": tool_result.get("summary", ""),
                    }

                # Handle different tool result types
                action = tool_result.get("action")

                if action == "update":
                    await _emit("node_update", {"node": tool_result["node"]})
                elif action == "re_run":
                    await _emit("node_update", {"node": tool_result["node"]})
                elif action == "add":
                    n = tool_result["node"]
                    collected_nodes.append(n)
                    if tool_result.get("edge"):
                        collected_edges.append(tool_result["edge"])
                    await _emit("node", {
                        "node": n,
                        "edge": tool_result.get("edge"),
                        "reconnect_edge": tool_result.get("reconnect_edge"),
                    })
                elif action == "remove":
                    await _emit("node_remove", {
                        "node_id": tool_result["node_id"],
                        "remove_edges": tool_result.get("remove_edges", []),
                        "new_edge": tool_result.get("new_edge"),
                    })
                else:
                    # Create tools → new node
                    node, edge = tool_to_node(tool_name, tool_args, tool_result, ctx)
                    if node:
                        collected_nodes.append(node)
                        if edge:
                            collected_edges.append(edge)
                        await _emit("node", {"node": node, "edge": edge})

    history.append({"role": "user", "content": message})
    return {
        "events": events,
        "nodes": collected_nodes,
        "edges": collected_edges,
        "items": collected_items,
        "summary": "完成",
    }


def _extract_url(text: str) -> str | None:
    """Extract a URL from user message text."""
    import re
    m = re.search(r'https?://[^\s<>"\']+', text)
    return m.group(0) if m else None
