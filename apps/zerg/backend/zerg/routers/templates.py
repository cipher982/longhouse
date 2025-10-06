from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import User
from zerg.schemas.schemas import TemplateDeployRequest
from zerg.schemas.schemas import Workflow
from zerg.schemas.schemas import WorkflowTemplate
from zerg.schemas.schemas import WorkflowTemplateCreate
from zerg.schemas.workflow import WorkflowData

router = APIRouter(
    prefix="/templates",
    tags=["templates"],
    dependencies=[Depends(get_current_user)],
)


@router.post("/", response_model=WorkflowTemplate)
def create_template(
    *,
    db: Session = Depends(get_db),
    template_in: WorkflowTemplateCreate,
    current_user: User = Depends(get_current_user),
):
    """
    Create new workflow template.
    """
    # Validate using WorkflowData schema
    try:
        workflow_data = WorkflowData(**template_in.canvas)
        canvas = workflow_data.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid template data: {e}")

    template = crud.create_workflow_template(
        db=db,
        created_by=current_user.id,
        name=template_in.name,
        description=template_in.description,
        category=template_in.category,
        canvas=canvas,
        tags=template_in.tags,
        preview_image_url=template_in.preview_image_url,
    )
    return template


@router.get("/", response_model=List[WorkflowTemplate])
def list_templates(
    db: Session = Depends(get_db),
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    my_templates: bool = False,
    current_user: User = Depends(get_current_user),
):
    """
    List workflow templates. By default shows public templates.
    Set my_templates=true to see your own templates (public and private).
    """
    if my_templates:
        return crud.get_workflow_templates(
            db=db, category=category, skip=skip, limit=limit, created_by=current_user.id, public_only=False
        )
    else:
        return crud.get_workflow_templates(db=db, category=category, skip=skip, limit=limit, public_only=True)


@router.get("/categories", response_model=List[str])
def list_categories(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get all available template categories.
    """
    return crud.get_template_categories(db=db)


@router.get("/{template_id}", response_model=WorkflowTemplate)
def get_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get a specific template by ID.
    """
    template = crud.get_workflow_template(db=db, template_id=template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Check access permissions
    if not template.is_public and template.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied to this template")

    return template


@router.post("/deploy", response_model=Workflow)
def deploy_template(
    *,
    db: Session = Depends(get_db),
    deploy_request: TemplateDeployRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Deploy a template as a new workflow.
    """
    workflow = crud.deploy_workflow_template(
        db=db,
        template_id=deploy_request.template_id,
        owner_id=current_user.id,
        name=deploy_request.name,
        description=deploy_request.description,
    )
    return workflow
