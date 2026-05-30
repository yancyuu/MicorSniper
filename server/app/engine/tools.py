"""Tool definitions for the workflow-generation agent.

Each tool has:
  - an OpenAI function-calling schema (TOOL_SCHEMAS)
  - an executor that runs against the live crawler session
  - a mapping from tool call → workflow node (tool_to_node)
"""
from __future__ import annotations

import json
from typing import Any

from crawl4ai import (
    AsyncWebCrawler,
    CrawlerRunConfig,
    CacheMode,
    JsonCssExtractionStrategy,
    LLMExtractionStrategy,
    LLMConfig,
)

from app.config import settings
from app.engine.generate import detect_login_wall

# ── Tool schemas (OpenAI function-calling format) ─────────────────────

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "open_page",
            "description": "打开一个网页，获取页面标题和内容摘要。返回页面基本信息供后续 tool 使用。通常这是第一个调用的 tool。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要打开的目标URL",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_schema",
            "description": "根据用户的采集目标和页面HTML，自动生成CSS选择器schema。会返回schema定义和试跑样例数据。如果样例为空说明schema不准，需要调整或改用ai_extract。",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "用户想从页面提取什么数据（如：商品名、价格、评论数）",
                    },
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "css_extract",
            "description": "用CSS选择器从页面提取结构化数据。适用于页面结构规律、数据有固定CSS类名的场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "base_selector": {
                        "type": "string",
                        "description": "列表项的基础选择器，如 '.product-item' 或 'div.goods-list > ul > li'",
                    },
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "字段名"},
                                "selector": {"type": "string", "description": "字段选择器"},
                                "type": {"type": "string", "description": "取值类型: text, attribute, html, regex"},
                            },
                            "required": ["name", "selector"],
                        },
                        "description": "要提取的字段列表",
                    },
                },
                "required": ["base_selector", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ai_extract",
            "description": "用AI大模型从页面提取结构化数据。适用于页面结构不规律或CSS选择器难以匹配的场景。比css_extract更灵活但更慢。",
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "告诉AI要提取什么数据的自然语言描述",
                    },
                    "schema_json": {
                        "type": "string",
                        "description": "可选，期望的输出JSON Schema",
                    },
                },
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "paginate",
            "description": "检测并设置翻页采集。自动检测页面上的分页控件（下一页按钮），设置翻页参数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "next_js": {
                        "type": "string",
                        "description": "点击下一页的JavaScript代码，如 document.querySelector('.next')?.click()",
                    },
                    "max_pages": {
                        "type": "integer",
                        "description": "最大翻页数，默认3",
                        "default": 3,
                    },
                },
                "required": ["next_js"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "js_execute",
            "description": "在页面上执行自定义JavaScript代码。适用于需要交互操作（如滚动、点击、等待加载）的场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "要执行的JavaScript代码",
                    },
                    "delay": {
                        "type": "integer",
                        "description": "执行后等待毫秒数，默认1000",
                        "default": 1000,
                    },
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "完成工作流生成。当你认为工作流已经完整时调用此tool。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "简要说明生成的工作流做了什么",
                    },
                },
                "required": ["summary"],
            },
        },
    },
    # ── Modify tools (for multi-turn modification) ──────────────────
    {
        "type": "function",
        "function": {
            "name": "update_node",
            "description": "修改已有节点的类型或配置。根据当前画布上的节点状态，选择需要修改的节点，更新其配置。修改后会自动重新执行该节点。",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "要修改的节点ID（如 node_1, node_2）",
                    },
                    "updates": {
                        "type": "object",
                        "description": "要修改的配置字段，如 {\"type\": \"ai_extract\", \"instruction\": \"...\"} 或 {\"base_selector\": \".item\"}",
                    },
                },
                "required": ["node_id", "updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "re_run_node",
            "description": "重新执行某一个节点（用真实浏览器），返回最新结果。适用于用户说'重试'、'没数据'、'再跑一次'等场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "要重新执行的节点ID",
                    },
                },
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_node",
            "description": "在已有节点后插入一个新节点。适用于用户要求增加步骤（如'加个翻页'、'先滚动一下'）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["css_extract", "ai_extract", "js_execute", "paginate", "crawl"],
                        "description": "新节点的类型",
                    },
                    "label": {
                        "type": "string",
                        "description": "节点标签，如'AI提取'、'翻页'",
                    },
                    "config": {
                        "type": "object",
                        "description": "节点配置",
                    },
                    "after_node_id": {
                        "type": "string",
                        "description": "插在哪个节点后面（接在其下方）",
                    },
                },
                "required": ["type", "label", "config", "after_node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_node",
            "description": "删除一个已有节点。适用于用户要求去掉某个步骤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "要删除的节点ID",
                    },
                },
                "required": ["node_id"],
            },
        },
    },
]


# ── Agent context: shared state during one generation run ─────────────

class AgentContext:
    """Holds the mutable state for one agent generation run."""

    def __init__(self, crawler: AsyncWebCrawler, html: str = "", final_url: str = "",
                 current_nodes: list[dict] | None = None, current_edges: list[dict] | None = None):
        self.crawler = crawler
        self.html = html
        self.final_url = final_url
        self.page_title = ""
        self.has_login_wall = False
        self.generated_schema: dict | None = None
        self.sample_data: list = []
        # Current node graph (from frontend, for modify operations)
        self.current_nodes: list[dict] = current_nodes or []
        self.current_edges: list[dict] = current_edges or []
        # Node tracking for graph building
        self._node_index = 0
        self._last_node_id: str | None = None

        # Initialize node index from existing nodes
        if current_nodes:
            max_id = 0
            for n in current_nodes:
                nid = n.get("id", "")
                num = int(nid.replace("node_", "")) if nid.startswith("node_") else 0
                max_id = max(max_id, num)
            self._node_index = max_id

    def next_node_id(self) -> str:
        self._node_index += 1
        return f"node_{self._node_index}"

    @property
    def last_node_id(self) -> str | None:
        return self._last_node_id

    def set_last_node(self, nid: str) -> None:
        self._last_node_id = nid


# ── Tool executors ────────────────────────────────────────────────────

def _truncate_html(html: str, max_chars: int = 8000) -> str:
    """Return a concise summary of HTML for LLM context."""
    if len(html) <= max_chars:
        return html
    return html[:max_chars] + "\n... (truncated)"


async def execute_open_page(ctx: AgentContext, args: dict) -> dict:
    """Open a URL and return page summary."""
    url = args["url"]
    fetch_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",
        screenshot=True,
    )
    res = await ctx.crawler.arun(url=url, config=fetch_cfg)
    ctx.html = res.html or ""
    ctx.final_url = res.url or url

    # Extract title
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", ctx.html, re.S | re.I)
    ctx.page_title = m.group(1).strip() if m else ""

    ctx.has_login_wall = detect_login_wall(ctx.html, ctx.final_url)

    return {
        "title": ctx.page_title,
        "final_url": ctx.final_url,
        "html_length": len(ctx.html),
        "html_summary": _truncate_html(ctx.html, 4000),
        "has_login_wall": ctx.has_login_wall,
    }


async def execute_generate_schema(ctx: AgentContext, args: dict) -> dict:
    """Use LLM to generate CSS extraction schema."""
    import asyncio

    goal = args["goal"]
    llm_config = LLMConfig(
        provider=settings.LLM_PROVIDER,
        api_token=settings.OPENAI_API_KEY,
        base_url=settings.LLM_BASE_URL or None,
    )
    schema = await asyncio.to_thread(
        JsonCssExtractionStrategy.generate_schema,
        html=ctx.html,
        query=goal,
        llm_config=llm_config,
    )
    schema = schema or {}
    ctx.generated_schema = schema

    # Dry-run the schema
    if schema and schema.get("fields"):
        strategy = JsonCssExtractionStrategy(schema, verbose=False)
        run_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            extraction_strategy=strategy,
        )
        res = await ctx.crawler.arun(url="raw://" + ctx.html, config=run_cfg)
        if res.extracted_content:
            try:
                data = json.loads(res.extracted_content)
                ctx.sample_data = data if isinstance(data, list) else [data]
            except json.JSONDecodeError:
                ctx.sample_data = []

    return {
        "schema": schema,
        "sample": ctx.sample_data[:5],
        "sample_count": len(ctx.sample_data),
        "fields": [f["name"] for f in schema.get("fields", [])] if schema else [],
    }


async def execute_css_extract(ctx: AgentContext, args: dict) -> dict:
    """Actually extract data using CSS selectors via crawl4ai."""
    base_selector = args["base_selector"]
    fields = args["fields"]
    if not base_selector or not fields:
        return {"error": "需要 base_selector 和 fields", "data": []}

    schema = {"name": "items", "baseSelector": base_selector, "fields": fields}
    strategy = JsonCssExtractionStrategy(schema, verbose=False)
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        extraction_strategy=strategy,
    )
    # Dry-run against the already-fetched HTML
    res = await ctx.crawler.arun(url="raw://" + ctx.html, config=run_cfg)
    data = []
    if res.extracted_content:
        try:
            parsed = json.loads(res.extracted_content)
            data = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            data = [{"raw": res.extracted_content}]

    return {
        "base_selector": base_selector,
        "fields": fields,
        "data": data,
        "count": len(data),
        "sample": data[:3],
    }


async def execute_ai_extract(ctx: AgentContext, args: dict) -> dict:
    """Actually extract data using AI/LLM via crawl4ai."""
    instruction = args["instruction"]
    schema_raw = args.get("schema_json")

    schema = None
    if schema_raw:
        try:
            schema = json.loads(schema_raw) if isinstance(schema_raw, str) else schema_raw
        except json.JSONDecodeError:
            pass

    llm_config = LLMConfig(
        provider=settings.LLM_PROVIDER,
        api_token=settings.OPENAI_API_KEY,
        base_url=settings.LLM_BASE_URL or None,
    )
    strategy = LLMExtractionStrategy(
        llm_config=llm_config,
        schema=schema,
        extraction_type="schema" if schema else "block",
        instruction=instruction,
        input_format="markdown",
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        extraction_strategy=strategy,
    )
    res = await ctx.crawler.arun(url="raw://" + ctx.html, config=run_cfg)
    data = []
    if res.extracted_content:
        try:
            parsed = json.loads(res.extracted_content)
            data = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            data = [{"raw": res.extracted_content}]

    return {
        "instruction": instruction,
        "data": data,
        "count": len(data),
        "sample": data[:3],
    }


async def execute_paginate(ctx: AgentContext, args: dict) -> dict:
    """Pagination node — config only, actual pagination runs at workflow time."""
    return {
        "next_js": args["next_js"],
        "max_pages": args.get("max_pages", 3),
        "note": "翻页在运行时执行",
    }


async def execute_js_execute(ctx: AgentContext, args: dict) -> dict:
    """JS execution node — config only, runs at workflow time."""
    return {
        "script": args["script"],
        "delay": args.get("delay", 1000),
        "note": "JS在运行时执行",
    }


async def execute_finish(ctx: AgentContext, args: dict) -> dict:
    """Finish signal."""
    return {"done": True, "summary": args.get("summary", "")}


# ── Modify tool executors ─────────────────────────────────────────────

def _find_node(ctx: AgentContext, node_id: str) -> dict | None:
    """Find a node in the current graph by ID."""
    for n in ctx.current_nodes:
        if n.get("id") == node_id:
            return n
    return None


async def execute_update_node(ctx: AgentContext, args: dict) -> dict:
    """Modify an existing node's type or config."""
    node_id = args["node_id"]
    updates = args["updates"]
    node = _find_node(ctx, node_id)
    if not node:
        return {"error": f"节点 {node_id} 不存在", "current_nodes": [n.get("id") for n in ctx.current_nodes]}

    data = node.get("data", {})
    config = data.get("config", {})

    # Apply type change
    if "type" in updates:
        data["type"] = updates.pop("type")
    # Apply label change
    if "label" in updates:
        data["label"] = updates.pop("label")
    # Apply config updates
    config.update(updates)
    data["config"] = config
    data["status"] = "idle"
    data.pop("error", None)
    node["data"] = data

    return {
        "action": "update",
        "node_id": node_id,
        "node": node,
        "message": f"已更新节点 {node_id}",
    }


async def execute_re_run_node(ctx: AgentContext, args: dict) -> dict:
    """Re-execute a single node using crawl4ai and return fresh results."""
    node_id = args["node_id"]
    node = _find_node(ctx, node_id)
    if not node:
        return {"error": f"节点 {node_id} 不存在"}

    from app.engine.nodes import (
        execute_css_extract, execute_ai_extract, execute_crawl,
    )

    nd = node.get("data", {})
    ntype = nd.get("type", "")
    config = nd.get("config", {})
    result = {"action": "re_run", "node_id": node_id, "success": False}

    try:
        if ntype == "css_extract":
            r = await execute_css_extract(ctx.crawler, "", config)
            result["success"] = r.get("success", True)
            result["data"] = r.get("data", [])
            nd["result"] = r.get("data", [])[:5]
        elif ntype == "ai_extract":
            r = await execute_ai_extract(ctx.crawler, "", config)
            result["success"] = r.get("success", True)
            result["data"] = r.get("data", [])
            nd["result"] = r.get("data", [])[:5]
        elif ntype == "crawl":
            r = await execute_crawl(ctx.crawler, "", config)
            result["success"] = r.get("success", True)
            result["data"] = r.get("data", {})
            nd["result"] = r.get("data", {})
        else:
            result["message"] = f"节点类型 {ntype} 不支持单独重跑"
    except Exception as e:
        result["error"] = str(e)
        nd["error"] = str(e)

    nd["status"] = "success" if result["success"] else "error"
    node["data"] = nd
    result["node"] = node
    return result


async def execute_add_node_tool(ctx: AgentContext, args: dict) -> dict:
    """Insert a new node after an existing node."""
    ntype = args["type"]
    label = args["label"]
    config = args.get("config", {})
    after_id = args["after_node_id"]

    # Find position
    after_node = _find_node(ctx, after_id)
    if not after_node:
        return {"error": f"节点 {after_id} 不存在"}

    nid = ctx.next_node_id()
    after_pos = after_node.get("position", {"x": 0, "y": 0})
    # Place below the after_node, shift others down
    new_node = {
        "id": nid,
        "type": "workflowNode",
        "position": {"x": after_pos.get("x", 0), "y": after_pos.get("y", 0) + 200},
        "data": {"type": ntype, "label": label, "status": "idle", "config": config},
    }

    # Create edge from after_node to new node
    edge = {"id": f"e_{after_id}_{nid}", "source": after_id, "target": nid}

    # Check if after_node had an outgoing edge — reconnect through new node
    reconnect_edge = None
    for e in ctx.current_edges:
        if e.get("source") == after_id:
            reconnect_edge = e
            break

    return {
        "action": "add",
        "node": new_node,
        "edge": edge,
        "reconnect_edge": reconnect_edge,  # frontend needs to update this
        "message": f"已添加节点 {label}",
    }


async def execute_remove_node(ctx: AgentContext, args: dict) -> dict:
    """Remove a node from the graph."""
    node_id = args["node_id"]
    node = _find_node(ctx, node_id)
    if not node:
        return {"error": f"节点 {node_id} 不存在"}

    # Find edges connected to this node
    connected_edges = [e for e in ctx.current_edges
                       if e.get("source") == node_id or e.get("target") == node_id]

    # Find predecessor and successor for reconnection
    pred_edge = next((e for e in ctx.current_edges if e.get("target") == node_id), None)
    succ_edge = next((e for e in ctx.current_edges if e.get("source") == node_id), None)

    new_edge = None
    if pred_edge and succ_edge:
        new_edge = {
            "id": f"e_{pred_edge['source']}_{succ_edge['target']}",
            "source": pred_edge["source"],
            "target": succ_edge["target"],
        }

    return {
        "action": "remove",
        "node_id": node_id,
        "remove_edges": [e.get("id") for e in connected_edges],
        "new_edge": new_edge,
        "message": f"已删除节点 {node.get('data', {}).get('label', node_id)}",
    }


# Map tool name → executor
TOOL_EXECUTORS: dict[str, callable] = {
    "open_page": execute_open_page,
    "generate_schema": execute_generate_schema,
    "css_extract": execute_css_extract,
    "ai_extract": execute_ai_extract,
    "paginate": execute_paginate,
    "js_execute": execute_js_execute,
    "finish": execute_finish,
    "update_node": execute_update_node,
    "re_run_node": execute_re_run_node,
    "add_node": execute_add_node_tool,
    "remove_node": execute_remove_node,
}


# ── Tool call → workflow node mapping ────────────────────────────────

_NODE_LABELS: dict[str, str] = {
    "open_page": "爬取页面",
    "generate_schema": "生成 Schema",
    "css_extract": "CSS 提取",
    "ai_extract": "AI 提取",
    "paginate": "翻页采集",
    "js_execute": "JS 执行",
}

_NODE_TYPES: dict[str, str] = {
    "open_page": "crawl",
    "generate_schema": "css_extract",
    "css_extract": "css_extract",
    "ai_extract": "ai_extract",
    "paginate": "paginate",
    "js_execute": "js_execute",
}


def tool_to_node(
    tool_name: str,
    tool_args: dict,
    tool_result: dict,
    ctx: AgentContext,
) -> tuple[dict | None, dict | None]:
    """Convert a tool call into a workflow node (+ optional edge).

    Returns (node_dict_or_None, edge_dict_or_None).
    """
    # finish doesn't produce a node
    if tool_name == "finish":
        return None, None

    nid = ctx.next_node_id()
    ntype = _NODE_TYPES.get(tool_name, "crawl")
    label = _NODE_LABELS.get(tool_name, tool_name)
    config = _build_node_config(tool_name, tool_args, tool_result)

    # Layout: wrap after 3 nodes per row
    idx = ctx._node_index - 1
    col = idx % 3
    row = idx // 3

    node = {
        "id": nid,
        "type": "workflowNode",
        "position": {"x": col * 280, "y": row * 200},
        "data": {
            "type": ntype,
            "label": label,
            "status": "idle",
            "config": config,
        },
    }

    # Attach sample data to the node if available
    if tool_name == "generate_schema" and ctx.sample_data:
        node["data"]["result"] = ctx.sample_data[:3]

    # Attach real extraction results to the node
    if tool_name == "css_extract" and tool_result.get("data"):
        node["data"]["result"] = tool_result["data"][:5]
    if tool_name == "ai_extract" and tool_result.get("data"):
        node["data"]["result"] = tool_result["data"][:5]

    # Build edge from previous node
    edge = None
    if ctx.last_node_id:
        edge = {
            "id": f"e_{ctx.last_node_id}_{nid}",
            "source": ctx.last_node_id,
            "target": nid,
        }

    ctx.set_last_node(nid)
    return node, edge


def _build_node_config(tool_name: str, args: dict, result: dict) -> dict:
    """Build the node config dict for a given tool call."""
    if tool_name == "open_page":
        return {"url": args["url"]}

    if tool_name == "generate_schema":
        schema = result.get("schema", {})
        return {
            "base_selector": schema.get("baseSelector", ""),
            "fields": json.dumps(schema.get("fields", []), ensure_ascii=False, indent=2),
        }

    if tool_name == "css_extract":
        return {
            "base_selector": args["base_selector"],
            "fields": json.dumps(args["fields"], ensure_ascii=False, indent=2),
        }

    if tool_name == "ai_extract":
        cfg: dict = {"instruction": args["instruction"]}
        if args.get("schema_json"):
            cfg["schema"] = args["schema_json"]
        return cfg

    if tool_name == "paginate":
        return {
            "next_js": args["next_js"],
            "max_pages": args.get("max_pages", 3),
        }

    if tool_name == "js_execute":
        return {
            "js_code": args["script"],
            "delay": args.get("delay", 1000),
        }

    return {}
