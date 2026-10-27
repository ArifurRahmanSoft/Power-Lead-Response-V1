import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.schemas.lead_task import (
    LeadTaskCompleteRequest,
    LeadTaskCreateRequest,
    LeadTaskData,
    LeadTaskListData,
    LeadTaskListFilters,
    LeadTaskListResponse,
    LeadTaskResponse,
    LeadTaskTransitionRequest,
    LeadTaskUpdateRequest,
)
from app.services.lead_service import (
    LeadNotFoundError,
    LeadPermissionError,
    LeadScopeError,
)
from app.services.lead_task_service import (
    LeadTaskAssignmentError,
    LeadTaskConflictError,
    LeadTaskNotFoundError,
    LeadTaskPermissionError,
    LeadTaskReferenceError,
    LeadTaskStateError,
    cancel_lead_task,
    complete_lead_task,
    create_lead_task,
    get_lead_tasks,
    update_lead_task,
)

lead_task_router = APIRouter()
task_router = APIRouter()


def _raise_task_error(exception: Exception) -> None:
    if isinstance(exception, (LeadTaskNotFoundError, LeadNotFoundError)):
        raise HTTPException(status_code=404, detail="Task or lead not found") from exception
    if isinstance(exception, LeadTaskConflictError):
        raise HTTPException(status_code=409, detail="Task was modified by another request") from exception
    if isinstance(exception, LeadTaskStateError):
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    if isinstance(exception, LeadTaskAssignmentError):
        raise HTTPException(
            status_code=400,
            detail="Assignee must be an active member of this workspace",
        ) from exception
    if isinstance(exception, LeadTaskReferenceError):
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    if isinstance(
        exception,
        (LeadTaskPermissionError, LeadPermissionError, LeadScopeError),
    ):
        raise HTTPException(status_code=403, detail="Task operation is not authorized") from exception
    raise exception


@lead_task_router.post(
    "/{lead_id}/tasks",
    response_model=LeadTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_lead_task_route(
    lead_id: uuid.UUID,
    payload: LeadTaskCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadTaskResponse:
    try:
        task = create_lead_task(database_session, context, lead_id, payload)
    except Exception as exception:
        _raise_task_error(exception)
        raise
    return LeadTaskResponse(
        success=True,
        message="Lead task created",
        data=LeadTaskData.model_validate(task),
    )


@task_router.get("", response_model=LeadTaskListResponse)
def list_lead_tasks_route(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    lead_id: uuid.UUID | None = None,
    assigned_to_me: bool = False,
    due_today: bool = False,
    overdue: bool = False,
    timezone_name: Annotated[str, Query(alias="timezone")] = "UTC",
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    task_type: str | None = None,
    priority: str | None = None,
) -> LeadTaskListResponse:
    try:
        filters = LeadTaskListFilters(
            lead_id=lead_id,
            assigned_to_me=assigned_to_me,
            due_today=due_today,
            overdue=overdue,
            timezone_name=timezone_name,
            status=status_filter,
            task_type=task_type,
            priority=priority,
        )
        result = get_lead_tasks(
            database_session,
            context,
            page=page,
            size=size,
            filters=filters,
        )
    except ValidationError as exception:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Invalid task filters",
                "errors": [
                    {
                        "field": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"].removeprefix("Value error, "),
                    }
                    for error in exception.errors()
                ],
            },
        ) from exception
    except Exception as exception:
        _raise_task_error(exception)
        raise
    return LeadTaskListResponse(
        success=True,
        data=LeadTaskListData(
            items=[LeadTaskData.model_validate(task) for task in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@task_router.patch("/{task_id}", response_model=LeadTaskResponse)
def update_lead_task_route(
    task_id: uuid.UUID,
    payload: LeadTaskUpdateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadTaskResponse:
    try:
        task = update_lead_task(database_session, context, task_id, payload)
    except Exception as exception:
        _raise_task_error(exception)
        raise
    return LeadTaskResponse(
        success=True, message="Lead task updated", data=LeadTaskData.model_validate(task)
    )


@task_router.post("/{task_id}/cancel", response_model=LeadTaskResponse)
def cancel_lead_task_route(
    task_id: uuid.UUID,
    payload: LeadTaskTransitionRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadTaskResponse:
    try:
        task = cancel_lead_task(
            database_session,
            context,
            task_id,
            row_version=payload.row_version,
        )
    except Exception as exception:
        _raise_task_error(exception)
        raise
    return LeadTaskResponse(
        success=True, message="Lead task cancelled", data=LeadTaskData.model_validate(task)
    )


@task_router.post("/{task_id}/complete", response_model=LeadTaskResponse)
def complete_lead_task_route(
    task_id: uuid.UUID,
    payload: LeadTaskCompleteRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadTaskResponse:
    try:
        task = complete_lead_task(database_session, context, task_id, payload)
    except Exception as exception:
        _raise_task_error(exception)
        raise
    return LeadTaskResponse(
        success=True, message="Lead task completed", data=LeadTaskData.model_validate(task)
    )
