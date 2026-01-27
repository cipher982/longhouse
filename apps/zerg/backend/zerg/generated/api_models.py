# AUTO-GENERATED - DO NOT EDIT
# Generated from api-schema.yml

from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from pydantic import BaseModel


class Position(BaseModel):
    x: float
    y: float


class WorkflowNode(BaseModel):
    id: str
    type: str
    position: Position
    config: Optional[Dict[str, Any]] = None


class WorkflowEdge(BaseModel):
    from_node_id: str
    to_node_id: str
    config: Optional[Dict[str, Any]] = None


class WorkflowCanvas(BaseModel):
    nodes: List[WorkflowNode]
    edges: List[WorkflowEdge]


class Workflow(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    canvas_data: Optional[WorkflowCanvas] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class Fiche(BaseModel):
    id: int
    name: str
    system_instructions: str
    task_instructions: Optional[str] = None
    model: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Thread(BaseModel):
    id: int
    title: str
    fiche_id: int
    created_at: Optional[datetime] = None


class Message(BaseModel):
    id: int
    role: str
    content: str
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None


class CreateWorkflowRequest(BaseModel):
    name: str
    description: Optional[str] = None


class CreateAgentRequest(BaseModel):
    name: str
    system_instructions: str
    task_instructions: Optional[str] = None
    model: Optional[str] = None


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: Optional[Dict[str, Any]] = None
