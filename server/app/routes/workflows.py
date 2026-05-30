from fastapi import APIRouter, HTTPException
from datetime import datetime
from sse_starlette.sse import EventSourceResponse
from app.models import Workflow, SaveWorkflowRequest, RunWorkflowRequest, GenerateRequest
from app.store import STORE
from app.engine.runner import run_workflow
from app.engine.agent import agent_run
from app.ws.handlers import manager
import asyncio
import json
import uuid

router = APIRouter(prefix="/api/workflows")

# Active runs: workflow_id -> asyncio.Task (FR-9)
_RUNNING: dict[str, asyncio.Task] = {}


@router.get("/")
async def list_workflows():
    return await STORE.list_workflows()


@router.get("/latest")
async def get_latest():
    wf = await STORE.get_latest_workflow()
    if not wf:
        raise HTTPException(404, "No workflows found")
    return wf


@router.get("/{wf_id}")
async def get_workflow(wf_id: str):
    wf = await STORE.load_workflow(wf_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    return wf


@router.post("/")
async def create_workflow(req: SaveWorkflowRequest):
    now = datetime.now().isoformat()
    wf = Workflow(
        id=str(uuid.uuid4()),
        name=req.name or "Untitled Workflow",
        nodes=req.nodes or [],
        edges=req.edges or [],
        created_at=now,
        updated_at=now,
    )
    await STORE.save_workflow(wf)
    return {"success": True, "id": wf.id}


@router.put("/{wf_id}")
async def update_workflow(wf_id: str, req: SaveWorkflowRequest):
    wf = await STORE.load_workflow(wf_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    if req.name is not None:
        wf.name = req.name
    if req.nodes is not None:
        wf.nodes = req.nodes
    if req.edges is not None:
        wf.edges = req.edges
    wf.updated_at = datetime.now().isoformat()
    await STORE.save_workflow(wf)
    return {"success": True}


@router.delete("/{wf_id}")
async def delete_workflow(wf_id: str):
    ok = await STORE.delete_workflow(wf_id)
    if not ok:
        raise HTTPException(404, "Workflow not found")
    return {"success": True}


@router.post("/run")
async def run_workflow_endpoint(req: RunWorkflowRequest):
    wf_id = str(uuid.uuid4())

    async def _on_status(msg: dict):
        await manager.broadcast(wf_id, msg)

    async def _run():
        try:
            await run_workflow(
                workflow_id=wf_id,
                nodes=[n.model_dump() for n in req.nodes],
                edges=[e.model_dump() for e in req.edges],
                on_status=_on_status,
            )
        finally:
            _RUNNING.pop(wf_id, None)

    task = asyncio.ensure_future(_run())
    _RUNNING[wf_id] = task
    return {"success": True, "workflow_id": wf_id}


@router.post("/generate")
async def generate_workflow_endpoint(req: GenerateRequest):
    """AI-generate a node graph via tool-calling agent, streamed as SSE."""
    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def _on_event(evt_type: str, data: dict):
            """Agent callback: push each event to SSE queue."""
            await queue.put({"event": evt_type, "data": json.dumps(data, ensure_ascii=False)})

        async def _run():
            try:
                result = await agent_run(
                    message=f"爬取 {req.url} 的 {req.goal}",
                    session_id=f"gen-{uuid.uuid4().hex[:8]}",
                    session_name=req.session_name,
                    on_event=_on_event,
                )
                if result.get("need_session"):
                    await queue.put({"event": "need_session", "data": json.dumps(
                        result, ensure_ascii=False
                    )})
                else:
                    await queue.put({"event": "done", "data": json.dumps(
                        {"summary": result.get("summary", ""), "item_count": len(result.get("items", []))},
                        ensure_ascii=False
                    )})
            except Exception as e:
                await queue.put({"event": "error", "data": json.dumps(
                    {"error": str(e)}, ensure_ascii=False
                )})
            finally:
                await queue.put(None)  # sentinel

        task = asyncio.ensure_future(_run())

        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=120)
                if item is None:
                    break
                yield item
        except asyncio.TimeoutError:
            yield {"event": "error", "data": json.dumps({"error": "生成超时"})}
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(event_stream())


@router.post("/smart-generate")
async def smart_generate_endpoint(body: dict):
    """Natural-language driven generation: auto-extract URL + match session."""
    prompt = (body or {}).get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    from app.engine.smart_generate import smart_generate
    try:
        return await smart_generate(prompt)
    except Exception as e:
        raise HTTPException(500, f"智能生成失败: {e}")


@router.post("/chat")
async def chat_endpoint(body: dict):
    """Chat SSE — agent streams events in real-time (nodes, updates, removals)."""
    message = ((body or {}).get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message is required")
    workflow = (body or {}).get("workflow")
    last_result = (body or {}).get("last_result")
    session_id = (body or {}).get("session_id") or "default"

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def _run():
            try:
                from app.engine.chat_router import chat_handle
                result = await chat_handle(message, workflow, last_result, session_id)

                # Stream all collected events
                for evt in result.get("events", []):
                    await queue.put(evt)

                # Items event
                items = result.get("items", [])
                if items:
                    await queue.put({"event": "items", "data": json.dumps(
                        {"items": items[:10], "count": len(items)}, ensure_ascii=False
                    )})

                await queue.put({"event": "done", "data": json.dumps(
                    {"summary": result.get("summary", ""), "item_count": len(items)},
                    ensure_ascii=False
                )})
            except Exception as e:
                import traceback
                traceback.print_exc()
                await queue.put({"event": "error", "data": json.dumps(
                    {"error": str(e)}, ensure_ascii=False
                )})
            finally:
                await queue.put(None)

        task = asyncio.ensure_future(_run())
        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=120)
                if item is None:
                    break
                yield item
        except asyncio.TimeoutError:
            yield {"event": "error", "data": json.dumps({"error": "超时"})}
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(event_stream())


@router.post("/{wf_id}/stop")
async def stop_workflow(wf_id: str):
    """Cancel a running workflow (FR-9). Runner broadcasts the stopped frame."""
    task = _RUNNING.get(wf_id)
    if not task or task.done():
        raise HTTPException(404, "Workflow not running")
    task.cancel()
    return {"success": True}
