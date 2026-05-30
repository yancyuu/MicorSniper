from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field
from datetime import datetime
import uuid


NodeType = Literal["context", "crawl", "batch_crawl", "css_extract", "ai_extract", "js_execute", "paginate"]
NodeStatus = Literal["idle", "running", "success", "error"]
WorkflowStatus = Literal["idle", "running", "completed", "failed"]


class WorkflowNodeData(BaseModel):
    label: str
    type: NodeType
    status: NodeStatus = "idle"
    config: dict[str, Any] = {}
    result: Optional[Any] = None
    error: Optional[str] = None
    screenshot: Optional[str] = None


class WorkflowNode(BaseModel):
    id: str
    type: str = "workflowNode"
    position: dict[str, float]
    data: WorkflowNodeData


class WorkflowEdge(BaseModel):
    id: str
    source: str
    target: str


class Workflow(BaseModel):
    id: str = ""
    name: str = "Untitled Workflow"
    nodes: list[WorkflowNode] = []
    edges: list[WorkflowEdge] = []
    status: WorkflowStatus = "idle"
    created_at: str = ""
    updated_at: str = ""

    def new(**kwargs) -> Workflow:
        now = datetime.now().isoformat()
        return Workflow(
            id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            **kwargs,
        )


class BrowserSession(BaseModel):
    name: str
    url: str = ""
    cookies: list[dict] = []
    created_at: str = ""


# Request models
class SaveWorkflowRequest(BaseModel):
    name: Optional[str] = None
    nodes: Optional[list[WorkflowNode]] = None
    edges: Optional[list[WorkflowEdge]] = None


class RunWorkflowRequest(BaseModel):
    nodes: list[WorkflowNode] = []
    edges: list[WorkflowEdge] = []


class LoginRequest(BaseModel):
    session_name: str
    url: str


# Run results (FR-6)
class RunResult(BaseModel):
    workflow_id: str
    run_id: str
    status: Literal["completed", "failed", "stopped"] = "completed"
    started_at: str = ""
    finished_at: str = ""
    node_outputs: dict[str, Any] = {}
    items: list[Any] = []


# AI node-graph generation (FR-7, FR-8)
class GenerateRequest(BaseModel):
    url: str
    goal: str
    session_name: Optional[str] = None


class GenerateResponse(BaseModel):
    nodes: list[WorkflowNode] = []
    edges: list[WorkflowEdge] = []
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    sample: list[Any] = []
    low_confidence: bool = False
    # Login-wall branch (FR-8)
    need_session: bool = False
    message: Optional[str] = None

    model_config = {"populate_by_name": True}
