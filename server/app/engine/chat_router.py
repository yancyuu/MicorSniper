"""Chat router — delegate everything to the session-based agent.

The agent decides whether to generate, modify, or re-run based on
the user message and current canvas state. No manual intent routing.
"""
from __future__ import annotations
import logging
from app.engine.agent import agent_run

logger = logging.getLogger("chat_router")


async def chat_handle(
    message: str,
    workflow: dict | None = None,
    last_result: dict | None = None,
    session_id: str = "default",
    on_event: Any = None,
) -> dict:
    """Handle a chat message by running the agent.

    Args:
        on_event: Async callback (event_type, data) for real-time SSE streaming.
    """
    current_nodes = []
    current_edges = []
    if workflow:
        current_nodes = workflow.get("nodes", [])
        current_edges = workflow.get("edges", [])

    result = await agent_run(
        message=message,
        session_id=session_id,
        current_nodes=current_nodes,
        current_edges=current_edges,
        on_event=on_event,
    )

    return result
