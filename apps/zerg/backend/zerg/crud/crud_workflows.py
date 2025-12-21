"""CRUD operations for Workflows and Workflow Templates."""

from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from zerg.models import Workflow
from zerg.models import WorkflowExecution
from zerg.models import WorkflowTemplate

# -------------------------------------------------------------------------
# Workflows
# -------------------------------------------------------------------------


def create_workflow(db: Session, *, owner_id: int, name: str, description: Optional[str] = None, canvas: Dict[str, Any]):
    """Create a new workflow."""

    # Check for existing active workflow with the same name for the same owner
    existing_workflow = (
        db.query(Workflow)
        .filter(
            Workflow.owner_id == owner_id,
            Workflow.name == name,
            Workflow.is_active.is_(True),
        )
        .first()
    )
    if existing_workflow:
        raise HTTPException(status_code=409, detail="A workflow with this name already exists.")

    db_workflow = Workflow(
        owner_id=owner_id,
        name=name,
        description=description,
        canvas=canvas,
    )
    db.add(db_workflow)
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


def get_workflows(
    db: Session,
    *,
    owner_id: int,
    skip: int = 0,
    limit: int = 100,
):
    """Return active workflows owned by *owner_id*."""

    return db.query(Workflow).filter_by(owner_id=owner_id, is_active=True).offset(skip).limit(limit).all()


def get_workflow(db: Session, workflow_id: int):
    return db.query(Workflow).filter_by(id=workflow_id).first()


# -------------------------------------------------------------------------
# Workflow Executions
# -------------------------------------------------------------------------


def get_workflow_execution(db: Session, execution_id: int):
    return db.query(WorkflowExecution).filter_by(id=execution_id).first()


def get_workflow_executions(db: Session, workflow_id: int, skip: int = 0, limit: int = 100):
    return db.query(WorkflowExecution).filter_by(workflow_id=workflow_id).offset(skip).limit(limit).all()


def get_waiting_execution_for_workflow(db: Session, workflow_id: int):
    """Get the first waiting execution for a workflow, if any exists."""

    return db.query(WorkflowExecution).filter_by(workflow_id=workflow_id, phase="waiting").first()


def create_workflow_execution(db: Session, *, workflow_id: int, phase: str = "running", triggered_by: str = "manual", result: str = None):
    """Create a new workflow execution record."""
    from datetime import datetime
    from datetime import timezone

    # Validate phase/result consistency
    if phase == "finished" and result is None:
        raise ValueError("result parameter is required when phase='finished'")
    if phase != "finished" and result is not None:
        raise ValueError("result parameter should only be provided when phase='finished'")

    execution = WorkflowExecution(
        workflow_id=workflow_id,
        phase=phase,
        result=result,
        started_at=datetime.now(timezone.utc),
        triggered_by=triggered_by,
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


# -------------------------------------------------------------------------
# Workflow Templates
# -------------------------------------------------------------------------


def create_workflow_template(
    db: Session,
    *,
    created_by: int,
    name: str,
    description: Optional[str] = None,
    category: str,
    canvas: Dict[str, Any],
    tags: Optional[List[str]] = None,
    preview_image_url: Optional[str] = None,
    is_public: bool = True,
):
    """Create a new workflow template."""

    db_template = WorkflowTemplate(
        created_by=created_by,
        name=name,
        description=description,
        category=category,
        canvas=canvas,
        tags=tags or [],
        preview_image_url=preview_image_url,
        is_public=is_public,
    )
    db.add(db_template)
    db.commit()
    db.refresh(db_template)
    return db_template


def get_workflow_templates(
    db: Session,
    *,
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    created_by: Optional[int] = None,
    public_only: bool = True,
):
    """Get workflow templates with optional filtering."""

    query = db.query(WorkflowTemplate)

    if public_only and created_by is None:
        query = query.filter(WorkflowTemplate.is_public.is_(True))
    elif created_by is not None:
        # If user is specified, show their templates regardless of public status
        query = query.filter(WorkflowTemplate.created_by == created_by)

    if category:
        query = query.filter(WorkflowTemplate.category == category)

    return query.offset(skip).limit(limit).all()


def get_workflow_template(db: Session, template_id: int):
    """Get a specific workflow template by ID."""

    return db.query(WorkflowTemplate).filter_by(id=template_id).first()


def get_workflow_template_by_name(db: Session, template_name: str):
    """Get a specific workflow template by name."""

    return db.query(WorkflowTemplate).filter_by(name=template_name, is_public=True).first()


def get_template_categories(db: Session):
    """Get all unique template categories."""

    result = db.query(WorkflowTemplate.category).distinct().all()
    return [r[0] for r in result]


def deploy_workflow_template(
    db: Session, *, template_id: int, owner_id: int, name: Optional[str] = None, description: Optional[str] = None
):
    """Deploy a template as a new workflow for the user."""

    # Get the template
    template = db.query(WorkflowTemplate).filter_by(id=template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if not template.is_public and template.created_by != owner_id:
        raise HTTPException(status_code=403, detail="Access denied to this template")

    # Create workflow from template
    workflow_name = name or f"{template.name} (Copy)"
    workflow_description = description or template.description

    return create_workflow(db=db, owner_id=owner_id, name=workflow_name, description=workflow_description, canvas=template.canvas)
