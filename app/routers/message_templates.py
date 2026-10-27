import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.models.message_template import MessageTemplate
from app.schemas.message_template import (
    MessagePlaceholderCatalogResponse,
    MessagePlaceholderDefinition,
    MessageTemplateActionResponse,
    MessageTemplateCreateRequest,
    MessageTemplateData,
    MessageTemplateListData,
    MessageTemplateListResponse,
    MessageTemplatePreviewData,
    MessageTemplatePreviewRequest,
    MessageTemplatePreviewResponse,
    MessageTemplateRenderIssue,
    MessageTemplateResponse,
    MessageTemplateUpdateRequest,
)
from app.services.message_template_service import (
    DuplicateMessageTemplateNameError,
    MessageTemplateLeadNotFoundError,
    MessageTemplateNotFoundError,
    archive_message_template,
    create_message_template,
    get_message_template_record,
    get_message_templates,
    preview_message_template,
    update_message_template,
)
from app.utils.mail_template import PLACEHOLDER_FIELDS, UnknownPlaceholderError

router = APIRouter()


def _template_data(template: MessageTemplate) -> MessageTemplateData:
    return MessageTemplateData(
        id=template.id,
        tenant_id=template.tenant_id,
        name=template.name,
        body=template.body,
        archived=template.archived_at is not None,
        created_by_user_id=template.created_by_user_id,
        updated_by_user_id=template.updated_by_user_id,
        archived_by_user_id=template.archived_by_user_id,
        created_at=template.created_at,
        updated_at=template.updated_at,
        archived_at=template.archived_at,
    )


def _raise_template_error(exception: Exception) -> None:
    if isinstance(exception, MessageTemplateNotFoundError):
        raise HTTPException(status_code=404, detail="Message template not found") from exception
    if isinstance(exception, MessageTemplateLeadNotFoundError):
        raise HTTPException(
            status_code=404,
            detail="Lead not found or not authorized",
        ) from exception
    if isinstance(exception, DuplicateMessageTemplateNameError):
        raise HTTPException(
            status_code=409,
            detail="An active message template with this name already exists",
        ) from exception
    if isinstance(exception, UnknownPlaceholderError):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Unknown template placeholders",
                "placeholders": sorted(exception.names),
            },
        ) from exception
    raise exception


@router.get("/placeholders", response_model=MessagePlaceholderCatalogResponse)
def message_placeholder_catalog(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
) -> MessagePlaceholderCatalogResponse:
    del context
    return MessagePlaceholderCatalogResponse(
        data=[
            MessagePlaceholderDefinition(key=key, label=label)
            for key, label in PLACEHOLDER_FIELDS.items()
        ]
    )


@router.get("", response_model=MessageTemplateListResponse)
def list_message_template_records(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=200)] = None,
    include_archived: bool = False,
) -> MessageTemplateListResponse:
    result = get_message_templates(
        database_session,
        context,
        page=page,
        size=size,
        search=search,
        include_archived=include_archived,
    )
    return MessageTemplateListResponse(
        data=MessageTemplateListData(
            items=[_template_data(item) for item in result.items],
            page=page,
            size=size,
            total=result.total,
        )
    )


@router.post(
    "",
    response_model=MessageTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_message_template_record(
    payload: MessageTemplateCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MessageTemplateResponse:
    try:
        template = create_message_template(database_session, context, payload)
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MessageTemplateResponse(
        message="Message template created",
        data=_template_data(template),
    )


@router.get("/{template_id}", response_model=MessageTemplateResponse)
def get_message_template_by_id(
    template_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MessageTemplateResponse:
    try:
        template = get_message_template_record(
            database_session, context, template_id
        )
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MessageTemplateResponse(data=_template_data(template))


@router.patch("/{template_id}", response_model=MessageTemplateResponse)
def update_message_template_record(
    template_id: uuid.UUID,
    payload: MessageTemplateUpdateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MessageTemplateResponse:
    try:
        template = update_message_template(
            database_session, context, template_id, payload
        )
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MessageTemplateResponse(
        message="Message template updated",
        data=_template_data(template),
    )


@router.delete(
    "/{template_id}", response_model=MessageTemplateActionResponse
)
def archive_message_template_record(
    template_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MessageTemplateActionResponse:
    try:
        archive_message_template(database_session, context, template_id)
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MessageTemplateActionResponse(message="Message template archived")


@router.post(
    "/{template_id}/preview",
    response_model=MessageTemplatePreviewResponse,
)
def preview_message_template_record(
    template_id: uuid.UUID,
    payload: MessageTemplatePreviewRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MessageTemplatePreviewResponse:
    try:
        result = preview_message_template(
            database_session, context, template_id, payload
        )
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MessageTemplatePreviewResponse(
        data=MessageTemplatePreviewData(
            template_id=result["template"].id,
            rendered_body=result["rendered_body"],
            missing_values=[
                MessageTemplateRenderIssue(
                    placeholder=name,
                    message="No value was supplied for this placeholder",
                )
                for name in result["missing"]
            ],
            warnings=result["warnings"],
        )
    )
