
# AUTO-GENERATED - DO NOT EDIT
# Generated from api-schema.yml

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class Fiche(BaseModel):
    id: int
    name: str
    system_instructions: str
    task_instructions: str
    model: str
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


class CreateFicheRequest(BaseModel):
    system_instructions: str
    task_instructions: str
    model: str


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: Optional[Dict[str, Any]] = None
