"""Pydantic AI agent for crawl workflow generation and modification.

Uses pydantic-ai-slim for the agent loop with built-in tool calling.
Streaming follows the chat_app.py pattern: NDJSON over StreamingResponse.
Tool functions push events to asyncio.Queue for real-time frontend updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from crawl4ai import (
    AsyncWebCrawler,
    CacheMode,
    CrawlerRunConfig,
    JsonCssExtractionStrategy,
    LLMExtractionStrategy,
    LLMConfig,
)
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
)
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import settings
from app.engine.generate import detect_login_wall, _browser_config_for
from app.engine.tools import tool_to_node

logger = logging.getLogger("pydantic_agent")


# ── Dependencies ────────────────────────────────────────────────────────

@dataclass
class CrawlDeps:
    """Injected into every tool call via RunContext[CrawlDeps]."""
    crawler: AsyncWebCrawler
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    # Page state (mutated by tools)
    html: str = ""
    final_url: str = ""
    page_title: str = ""
    has_login_wall: bool = False
    # Generated schema state
    generated_schema: dict | None = None
    sample_data: list = field(default_factory=list)
    # Node graph (from frontend)
    current_nodes: list[dict] = field(default_factory=list)
    current_edges: list[dict] = field(default_factory=list)
    # Collected during this run
    collected_nodes: list[dict] = field(default_factory=list)
    collected_edges: list[dict] = field(default_factory=list)
    collected_items: list[dict] = field(default_factory=list)
    # Available browser sessions (with cookies)
    available_sessions: list[dict] = field(default_factory=list)
    # Internal tracking (compatible with tool_to_node)
    _node_index: int = 0
    _last_node_id: str | None = None

    def __post_init__(self):
        self.init_node_index()

    def init_node_index(self):
        if self.current_nodes:
            max_id = 0
            for n in self.current_nodes:
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


# ── Model creation (follows customer-agent pattern) ─────────────────────

def _create_model() -> OpenAIModel:
    """Create an OpenAI-compatible model from settings."""
    model_name = (
        settings.LLM_PROVIDER.split("/", 1)[-1]
        if "/" in settings.LLM_PROVIDER
        else "gpt-4o-mini"
    )
    provider = OpenAIProvider(
        base_url=settings.LLM_BASE_URL or None,
        api_key=settings.OPENAI_API_KEY,
    )
    return OpenAIModel(model_name=model_name, provider=provider)


# ── Session store (Pydantic AI message format) ──────────────────────────

_sessions: dict[str, bytes] = {}


def get_session_history(session_id: str) -> list[ModelMessage]:
    if session_id not in _sessions:
        return []
    return ModelMessagesTypeAdapter.validate_json(_sessions[session_id])


def save_session_history(session_id: str, messages_json: bytes) -> None:
    _sessions[session_id] = messages_json


def clear_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


# ── System prompt ───────────────────────────────────────────────────────

_SYSTEM_PROMPT_BASE = """你是一个专业的网页爬虫工作流管理器。你可以通过调用tool来生成和修改工作流。

常见网站：淘宝→taobao.com, 京东→jd.com, 小红书→xiaohongshu.com, 抖音→douyin.com, B站→bilibili.com, 小米→xiaomi.com

工具分类：
- 生成类：open_page, generate_schema, css_extract, ai_extract, paginate, js_execute
- 修改类：update_node（改节点配置）, re_run_node（重跑单个节点）, add_node（插入节点）, remove_node（删节点）
- 完成：finish

【重要规则 - 必须严格遵守】
- 你必须通过调用 tool 来执行操作！禁止只是描述你要做什么！
- 收到用户请求后，立即调用 open_page 打开目标网页，不要解释计划
- 每个tool返回真实执行结果，看清结果再决定下一步
- 优先 css_extract，不行再 ai_extract
- 不要重复调相同参数的tool
- 完成后必须调 finish
- 不要输出任何文字说明，直接调用tool即可"""


def _build_system_prompt(nodes: list[dict], edges: list[dict]) -> str:
    if not nodes:
        return _SYSTEM_PROMPT_BASE + "\n\n当前画布为空，用户需要生成新的工作流。"

    lines = [f"{_SYSTEM_PROMPT_BASE}\n\n当前画布上的节点："]
    for n in nodes:
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
            info = nd.get("error", "") or (
                config.get("instruction", "")[:30] if config.get("instruction") else ""
            )

        status_icon = (
            "✅" if status == "success" else
            "❌" if status == "error" else
            "⏳" if status == "running" else "⬜"
        )
        lines.append(f"  {status_icon} {nid}: [{ntype}] {label} | {info}{result_preview}")

    for e in edges:
        lines.append(f"  → {e.get('source', '?')} → {e.get('target', '?')}")

    lines.append("\n根据用户反馈，使用 update_node/re_run_node 等工具修改对应节点。如果用户要求全新任务，使用生成类工具。")
    return "\n".join(lines)


def _truncate_html(html: str, max_chars: int = 4000) -> str:
    if len(html) <= max_chars:
        return html
    return html[:max_chars] + "\n... (truncated)"


def _find_node(deps: CrawlDeps, node_id: str) -> dict | None:
    for n in deps.current_nodes:
        if n.get("id") == node_id:
            return n
    return None


def _extract_url(text: str) -> str | None:
    m = re.search(r'https?://[^\s<>"\']+', text)
    return m.group(0) if m else None


# ── Agent definition ────────────────────────────────────────────────────

def create_agent() -> Agent[CrawlDeps]:
    """Create the crawl workflow agent with all tools."""
    agent = Agent(
        model=_create_model(),
        deps_type=CrawlDeps,
        system_prompt=_SYSTEM_PROMPT_BASE,
        retries=5,
    )

    # ── Dynamic system prompt with canvas state ─────────────────────

    @agent.system_prompt
    async def inject_canvas_state(ctx: RunContext[CrawlDeps]) -> str:
        prompt = _build_system_prompt(ctx.deps.current_nodes, ctx.deps.current_edges)
        # Inject available sessions info
        if ctx.deps.available_sessions:
            sessions_text = "\n\n可用的浏览器登录态：\n"
            for s in ctx.deps.available_sessions:
                sessions_text += f"  - {s.get('name', '?')}: {s.get('url', '(无URL)')}\n"
            sessions_text += "如果用户提到的目标网站在已有登录态中，使用 open_page 打开对应URL。"
            prompt += sessions_text
        return prompt

    # ── Generate tools ──────────────────────────────────────────────

    @agent.tool
    async def open_page(ctx: RunContext[CrawlDeps], url: str) -> str:
        """打开一个网页，获取页面标题和内容摘要。返回页面基本信息供后续 tool 使用。通常这是第一个调用的 tool。"""
        await ctx.deps.event_queue.put({"type": "thinking", "content": f"正在打开 {url}..."})

        fetch_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            wait_until="domcontentloaded",
            screenshot=True,
        )
        res = await ctx.deps.crawler.arun(url=url, config=fetch_cfg)
        ctx.deps.html = res.html or ""
        ctx.deps.final_url = res.url or url

        m = re.search(r"<title[^>]*>(.*?)</title>", ctx.deps.html, re.S | re.I)
        ctx.deps.page_title = m.group(1).strip() if m else ""
        ctx.deps.has_login_wall = detect_login_wall(ctx.deps.html, ctx.deps.final_url)

        result = {
            "title": ctx.deps.page_title,
            "final_url": ctx.deps.final_url,
            "html_length": len(ctx.deps.html),
            "has_login_wall": ctx.deps.has_login_wall,
        }

        node, edge = tool_to_node("open_page", {"url": url}, result, ctx.deps)
        if node:
            ctx.deps.collected_nodes.append(node)
            if edge:
                ctx.deps.collected_edges.append(edge)
            await ctx.deps.event_queue.put({"type": "node", "node": node, "edge": edge})

        return json.dumps(result, ensure_ascii=False)

    @agent.tool
    async def generate_schema(ctx: RunContext[CrawlDeps], goal: str) -> str:
        """根据用户的采集目标和页面HTML，自动生成CSS选择器schema。会返回schema定义和试跑样例数据。如果样例为空说明schema不准，需要调整或改用ai_extract。"""
        await ctx.deps.event_queue.put({"type": "thinking", "content": f"正在生成 Schema: {goal}..."})

        llm_config = LLMConfig(
            provider=settings.LLM_PROVIDER,
            api_token=settings.OPENAI_API_KEY,
            base_url=settings.LLM_BASE_URL or None,
        )
        schema = await asyncio.to_thread(
            JsonCssExtractionStrategy.generate_schema,
            html=ctx.deps.html,
            query=goal,
            llm_config=llm_config,
        )
        schema = schema or {}
        ctx.deps.generated_schema = schema

        # Dry-run the schema
        sample_data: list = []
        if schema and schema.get("fields"):
            strategy = JsonCssExtractionStrategy(schema, verbose=False)
            run_cfg = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                extraction_strategy=strategy,
            )
            res = await ctx.deps.crawler.arun(url="raw://" + ctx.deps.html, config=run_cfg)
            if res.extracted_content:
                try:
                    data = json.loads(res.extracted_content)
                    sample_data = data if isinstance(data, list) else [data]
                except json.JSONDecodeError:
                    sample_data = []
        ctx.deps.sample_data = sample_data

        result = {
            "schema": schema,
            "sample": sample_data[:5],
            "sample_count": len(sample_data),
            "fields": [f["name"] for f in schema.get("fields", [])] if schema else [],
        }

        node, edge = tool_to_node("generate_schema", {"goal": goal}, result, ctx.deps)
        if node:
            if sample_data:
                node["data"]["result"] = sample_data[:3]
            ctx.deps.collected_nodes.append(node)
            if edge:
                ctx.deps.collected_edges.append(edge)
            await ctx.deps.event_queue.put({"type": "node", "node": node, "edge": edge})

        return json.dumps(result, ensure_ascii=False)

    @agent.tool
    async def css_extract(ctx: RunContext[CrawlDeps], base_selector: str, fields: str) -> str:
        """用CSS选择器从页面提取结构化数据。适用于页面结构规律、数据有固定CSS类名的场景。fields为JSON数组字符串，每项有name和selector。"""
        await ctx.deps.event_queue.put({"type": "thinking", "content": f"正在CSS提取: {base_selector}..."})

        try:
            fields_list = json.loads(fields) if isinstance(fields, str) else fields
        except json.JSONDecodeError:
            return json.dumps({"error": "fields 格式错误，需要JSON数组"}, ensure_ascii=False)

        if not base_selector or not fields_list:
            return json.dumps({"error": "需要 base_selector 和 fields", "data": []}, ensure_ascii=False)

        schema = {"name": "items", "baseSelector": base_selector, "fields": fields_list}
        strategy = JsonCssExtractionStrategy(schema, verbose=False)
        run_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            extraction_strategy=strategy,
        )
        res = await ctx.deps.crawler.arun(url="raw://" + ctx.deps.html, config=run_cfg)
        data: list = []
        if res.extracted_content:
            try:
                parsed = json.loads(res.extracted_content)
                data = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                data = [{"raw": res.extracted_content}]

        result = {
            "base_selector": base_selector,
            "fields": fields_list,
            "data": data,
            "count": len(data),
            "sample": data[:3],
        }

        node, edge = tool_to_node("css_extract", {"base_selector": base_selector, "fields": fields_list}, result, ctx.deps)
        if node:
            if data:
                node["data"]["result"] = data[:5]
            ctx.deps.collected_nodes.append(node)
            if edge:
                ctx.deps.collected_edges.append(edge)
            await ctx.deps.event_queue.put({"type": "node", "node": node, "edge": edge})

        if data:
            ctx.deps.collected_items.extend(data)

        return json.dumps(result, ensure_ascii=False)

    @agent.tool
    async def ai_extract(ctx: RunContext[CrawlDeps], instruction: str, schema_json: str = "") -> str:
        """用AI大模型从页面提取结构化数据。适用于页面结构不规律或CSS选择器难以匹配的场景。比css_extract更灵活但更慢。"""
        await ctx.deps.event_queue.put({"type": "thinking", "content": f"正在AI提取: {instruction[:30]}..."})

        schema = None
        if schema_json:
            try:
                schema = json.loads(schema_json) if isinstance(schema_json, str) else schema_json
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
        res = await ctx.deps.crawler.arun(url="raw://" + ctx.deps.html, config=run_cfg)
        data: list = []
        if res.extracted_content:
            try:
                parsed = json.loads(res.extracted_content)
                data = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                data = [{"raw": res.extracted_content}]

        result = {
            "instruction": instruction,
            "data": data,
            "count": len(data),
            "sample": data[:3],
        }

        node, edge = tool_to_node("ai_extract", {"instruction": instruction, "schema_json": schema_json}, result, ctx.deps)
        if node:
            if data:
                node["data"]["result"] = data[:5]
            ctx.deps.collected_nodes.append(node)
            if edge:
                ctx.deps.collected_edges.append(edge)
            await ctx.deps.event_queue.put({"type": "node", "node": node, "edge": edge})

        if data:
            ctx.deps.collected_items.extend(data)

        return json.dumps(result, ensure_ascii=False)

    @agent.tool
    async def paginate(ctx: RunContext[CrawlDeps], next_js: str, max_pages: int = 3) -> str:
        """检测并设置翻页采集。传入点击下一页的JS代码和最大页数。"""
        result = {
            "next_js": next_js,
            "max_pages": max_pages,
            "note": "翻页在运行时执行",
        }

        node, edge = tool_to_node("paginate", {"next_js": next_js, "max_pages": max_pages}, result, ctx.deps)
        if node:
            ctx.deps.collected_nodes.append(node)
            if edge:
                ctx.deps.collected_edges.append(edge)
            await ctx.deps.event_queue.put({"type": "node", "node": node, "edge": edge})

        return json.dumps(result, ensure_ascii=False)

    @agent.tool
    async def js_execute(ctx: RunContext[CrawlDeps], script: str, delay: int = 1000) -> str:
        """在页面上执行自定义JavaScript代码。适用于需要交互操作（如滚动、点击、等待加载）的场景。"""
        result = {
            "script": script,
            "delay": delay,
            "note": "JS在运行时执行",
        }

        node, edge = tool_to_node("js_execute", {"script": script, "delay": delay}, result, ctx.deps)
        if node:
            ctx.deps.collected_nodes.append(node)
            if edge:
                ctx.deps.collected_edges.append(edge)
            await ctx.deps.event_queue.put({"type": "node", "node": node, "edge": edge})

        return json.dumps(result, ensure_ascii=False)

    @agent.tool
    async def finish(ctx: RunContext[CrawlDeps], summary: str) -> str:
        """完成工作流生成。当你认为工作流已经完整时调用此tool，传入总结说明。"""
        await ctx.deps.event_queue.put({"type": "finish", "summary": summary})
        return json.dumps({"done": True, "summary": summary}, ensure_ascii=False)

    # ── Modify tools ────────────────────────────────────────────────

    @agent.tool
    async def update_node(ctx: RunContext[CrawlDeps], node_id: str, updates: str) -> str:
        """修改已有节点的配置。updates为JSON字符串，包含要修改的字段。修改后会自动重新执行该节点。"""
        try:
            updates_dict = json.loads(updates) if isinstance(updates, str) else updates
        except json.JSONDecodeError:
            return json.dumps({"error": "updates 格式错误"}, ensure_ascii=False)

        node = _find_node(ctx.deps, node_id)
        if not node:
            return json.dumps({"error": f"节点 {node_id} 不存在"}, ensure_ascii=False)

        data = node.get("data", {})
        config = data.get("config", {})
        if "type" in updates_dict:
            data["type"] = updates_dict.pop("type")
        if "label" in updates_dict:
            data["label"] = updates_dict.pop("label")
        config.update(updates_dict)
        data["config"] = config
        data["status"] = "idle"
        data.pop("error", None)
        node["data"] = data

        await ctx.deps.event_queue.put({"type": "node_update", "node": node})
        return json.dumps({"action": "update", "node_id": node_id, "node": node, "message": f"已更新节点 {node_id}"}, ensure_ascii=False)

    @agent.tool
    async def re_run_node(ctx: RunContext[CrawlDeps], node_id: str) -> str:
        """重新执行某一个节点（用真实浏览器），返回最新结果。"""
        await ctx.deps.event_queue.put({"type": "thinking", "content": f"正在重跑节点 {node_id}..."})

        node = _find_node(ctx.deps, node_id)
        if not node:
            return json.dumps({"error": f"节点 {node_id} 不存在"}, ensure_ascii=False)

        from app.engine.nodes import execute_css_extract, execute_ai_extract, execute_crawl

        nd = node.get("data", {})
        ntype = nd.get("type", "")
        config = nd.get("config", {})
        result = {"action": "re_run", "node_id": node_id, "success": False}

        try:
            if ntype == "css_extract":
                r = await execute_css_extract(ctx.deps.crawler, "", config)
                result["success"] = r.get("success", True)
                result["data"] = r.get("data", [])
                nd["result"] = r.get("data", [])[:5]
            elif ntype == "ai_extract":
                r = await execute_ai_extract(ctx.deps.crawler, "", config)
                result["success"] = r.get("success", True)
                result["data"] = r.get("data", [])
                nd["result"] = r.get("data", [])[:5]
            elif ntype == "crawl":
                r = await execute_crawl(ctx.deps.crawler, "", config)
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

        await ctx.deps.event_queue.put({"type": "node_update", "node": node})
        return json.dumps(result, ensure_ascii=False)

    @agent.tool
    async def add_node(ctx: RunContext[CrawlDeps], type: str, label: str, config: str, after_node_id: str) -> str:
        """在已有节点后插入一个新节点。config为JSON字符串。"""
        try:
            config_dict = json.loads(config) if isinstance(config, str) else config
        except json.JSONDecodeError:
            config_dict = {}

        after_node = _find_node(ctx.deps, after_node_id)
        if not after_node:
            return json.dumps({"error": f"节点 {after_node_id} 不存在"}, ensure_ascii=False)

        nid = ctx.deps.next_node_id()
        after_pos = after_node.get("position", {"x": 0, "y": 0})
        new_node = {
            "id": nid,
            "type": "workflowNode",
            "position": {"x": after_pos.get("x", 0), "y": after_pos.get("y", 0) + 200},
            "data": {"type": type, "label": label, "status": "idle", "config": config_dict},
        }
        edge = {"id": f"e_{after_node_id}_{nid}", "source": after_node_id, "target": nid}

        # Check if after_node had an outgoing edge
        reconnect_edge = None
        for e in ctx.deps.current_edges:
            if e.get("source") == after_node_id:
                reconnect_edge = e
                break

        await ctx.deps.event_queue.put({
            "type": "node",
            "node": new_node,
            "edge": edge,
            "reconnect_edge": reconnect_edge,
        })
        return json.dumps({"action": "add", "node": new_node, "edge": edge, "reconnect_edge": reconnect_edge}, ensure_ascii=False)

    @agent.tool
    async def remove_node(ctx: RunContext[CrawlDeps], node_id: str) -> str:
        """删除一个已有节点。"""
        node = _find_node(ctx.deps, node_id)
        if not node:
            return json.dumps({"error": f"节点 {node_id} 不存在"}, ensure_ascii=False)

        connected_edges = [
            e for e in ctx.deps.current_edges
            if e.get("source") == node_id or e.get("target") == node_id
        ]
        pred_edge = next((e for e in ctx.deps.current_edges if e.get("target") == node_id), None)
        succ_edge = next((e for e in ctx.deps.current_edges if e.get("source") == node_id), None)

        new_edge = None
        if pred_edge and succ_edge:
            new_edge = {
                "id": f"e_{pred_edge['source']}_{succ_edge['target']}",
                "source": pred_edge["source"],
                "target": succ_edge["target"],
            }

        await ctx.deps.event_queue.put({
            "type": "node_remove",
            "node_id": node_id,
            "remove_edges": [e.get("id") for e in connected_edges],
            "new_edge": new_edge,
        })
        return json.dumps({"action": "remove", "node_id": node_id, "remove_edges": [e.get("id") for e in connected_edges], "new_edge": new_edge}, ensure_ascii=False)

    return agent


# ── Singleton agent instance ────────────────────────────────────────────

_agent: Agent[CrawlDeps] | None = None


def get_agent() -> Agent[CrawlDeps]:
    global _agent
    if _agent is None:
        _agent = create_agent()
    return _agent


# ── Streaming entry point ──────────────────────────────────────────────

async def stream_chat(
    message: str,
    session_id: str,
    current_nodes: list[dict] | None = None,
    current_edges: list[dict] | None = None,
    session_name: str | None = None,
    available_sessions: list[dict] | None = None,
) -> AsyncIterator:
    """Run the agent and yield NDJSON events.

    Follows the pydantic-ai chat_app.py pattern:
    - agent.run_stream() handles tool-calling loop
    - tool functions push events to deps.event_queue
    - text streamed via stream_text()
    - everything merged into a single NDJSON stream
    """
    from collections.abc import AsyncIterator

    current_nodes = current_nodes or []
    current_edges = current_edges or []

    # Resolve browser config
    browser_config = await _browser_config_for(session_name)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        deps = CrawlDeps(
            crawler=crawler,
            current_nodes=current_nodes,
            current_edges=current_edges,
            available_sessions=available_sessions or [],
        )

        # Auto-fetch URL if canvas is empty
        enhanced_message = message
        url = _extract_url(message)
        if not current_nodes and url:
            logger.info(f"[PydanticAgent] Auto-fetching: {url}")
            await deps.event_queue.put({"type": "thinking", "content": f"正在打开 {url}..."})

            fetch_cfg = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                wait_until="domcontentloaded",
                screenshot=True,
            )
            try:
                res = await crawler.arun(url=url, config=fetch_cfg)
                deps.html = res.html or ""
                deps.final_url = res.url or url
                m = re.search(r"<title[^>]*>(.*?)</title>", deps.html, re.S | re.I)
                deps.page_title = m.group(1).strip() if m else ""
                deps.has_login_wall = detect_login_wall(deps.html, deps.final_url)

                page_result = {
                    "title": deps.page_title,
                    "final_url": deps.final_url,
                    "html_length": len(deps.html),
                    "has_login_wall": deps.has_login_wall,
                }
                node, edge = tool_to_node("open_page", {"url": url}, page_result, deps)
                if node:
                    deps.collected_nodes.append(node)
                    if edge:
                        deps.collected_edges.append(edge)
                    await deps.event_queue.put({"type": "node", "node": node, "edge": edge})

                html_summary = _truncate_html(deps.html)
                enhanced_message += (
                    f"\n\n[系统自动获取了页面信息]\n"
                    f"标题: {deps.page_title}\n"
                    f"需要登录: {deps.has_login_wall}\n"
                    f"HTML片段:\n{html_summary}"
                )
            except Exception as e:
                logger.error(f"[PydanticAgent] Auto-fetch failed: {e}")

        # Get session history
        message_history = get_session_history(session_id)

        # Run agent in background, merge events from queue + text stream
        agent = get_agent()
        queue = deps.event_queue

        async def _run_agent():
            try:
                async with agent.run_stream(
                    enhanced_message,
                    message_history=message_history,
                    deps=deps,
                ) as result:
                    async for text in result.stream_text(delta=False, debounce_by=0.01):
                        await queue.put({"type": "text", "content": text})

                # Save session history
                save_session_history(session_id, result.new_messages_json())
                await queue.put({"type": "done", "summary": "完成"})
            except Exception as e:
                logger.exception(f"[PydanticAgent] Agent run failed: {e}")
                await queue.put({"type": "error", "error": str(e)})
            finally:
                await queue.put(None)  # sentinel

        task = asyncio.create_task(_run_agent())

        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=120)
                if item is None:
                    break
                yield item
        except asyncio.TimeoutError:
            yield {"type": "error", "error": "生成超时"}
        finally:
            if not task.done():
                task.cancel()
