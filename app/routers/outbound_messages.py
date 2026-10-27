import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.models.outbound_message import OutboundMessage, OutboundMessageJob
from app.repositories.outbound_message_repository import list_events
from app.schemas.outbound_message import (
    MessageEventData,
    OutboundCancelRequest,
    OutboundMessageData,
    OutboundMessageListData,
    OutboundMessageListResponse,
    OutboundMessageResponse,
    OutboundPreviewData,
    OutboundPreviewRequest,
    OutboundPreviewResponse,
    OutboundSendRequest,
    SenderSnapshotData,
)
from app.services.outbound_message_service import (
    OutboundIdempotencyConflictError,
    OutboundMessageEligibilityError,
    OutboundMessageNotFoundError,
    OutboundMessagePermissionError,
    OutboundMessageStateError,
    OutboundPreviewExpiredError,
    OutboundPreviewStaleError,
    cancel_outbound_message,
    create_outbound_preview,
    enqueue_outbound_message,
    get_outbound_message,
    get_outbound_messages,
)

router = APIRouter()


def _raise_service_error(exception: Exception) -> None:
    if isinstance(exception, OutboundMessageNotFoundError):
        raise HTTPException(status_code=404, detail="Mail preview, message, or context not found") from exception
    if isinstance(exception, OutboundMessagePermissionError):
        raise HTTPException(status_code=403, detail="Message access is forbidden") from exception
    if isinstance(exception, OutboundMessageEligibilityError):
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    if isinstance(exception, OutboundPreviewExpiredError):
        raise HTTPException(status_code=409, detail="Preview expired; create a new preview") from exception
    if isinstance(exception, OutboundPreviewStaleError):
        raise HTTPException(status_code=409, detail="Preview is no longer current; create a new preview") from exception
    if isinstance(exception, OutboundIdempotencyConflictError):
        raise HTTPException(status_code=409, detail="Idempotency key was already used for different content") from exception
    if isinstance(exception, OutboundMessageStateError):
        raise HTTPException(status_code=409, detail=str(exception) or "Message state does not allow this operation") from exception
    raise exception


def _message_data(
    session: Session,
    message: OutboundMessage,
    job: OutboundMessageJob | None,
) -> OutboundMessageData:
    events = getattr(message, "message_events", None) or list_events(session, message.id)
    return OutboundMessageData(
        id=message.id,
        job_id=job.id if job is not None else None,
        context_type=message.context_type,
        lead_id=message.lead_id,
        customer_id=message.customer_id,
        purpose=message.purpose,
        batch_id=message.batch_id,
        contact_stage=message.contact_stage,
        recipient_email=message.recipient_email,
        sender=SenderSnapshotData.model_validate(message.sender_snapshot),
        template_id=message.template_id,
        template_version=message.template_version,
        subject=message.subject,
        status=message.status,
        created_at=message.created_at,
        processing_at=message.processing_at,
        sent_at=message.sent_at,
        failed_at=message.failed_at,
        cancelled_at=message.cancelled_at,
        failure_reason=message.failure_reason,
        events=[MessageEventData.model_validate(event) for event in events],
    )


@router.post("/preview", response_model=OutboundPreviewResponse)
def preview_outbound_message(
    payload: OutboundPreviewRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_SEND))],
    session: Annotated[Session, Depends(get_db)],
) -> OutboundPreviewResponse:
    try:
        preview = create_outbound_preview(session, context, payload)
    except Exception as exception:
        _raise_service_error(exception)
        raise
    manifest = preview.asset_manifest
    return OutboundPreviewResponse(data=OutboundPreviewData(
        preview_id=preview.id,
        preview_hash=preview.content_hash,
        expires_at=preview.expires_at,
        context_type=preview.context_type,
        context_id=preview.lead_id if preview.context_type == "lead" else preview.customer_id,
        recipient=preview.recipient_email,
        purpose=preview.purpose,
        sender=SenderSnapshotData.model_validate(preview.sender_snapshot),
        template_id=preview.template_id,
        template_version=preview.template_version,
        subject=preview.subject,
        body_html=preview.body_html,
        body_text=preview.body_text,
        attachment_count=sum(item["kind"] == "attachment" for item in manifest),
        inline_asset_count=sum(item["kind"] != "attachment" for item in manifest),
    ))


@router.post("/send", response_model=OutboundMessageResponse, status_code=status.HTTP_202_ACCEPTED)
def send_outbound_message(
    payload: OutboundSendRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_SEND))],
    session: Annotated[Session, Depends(get_db)],
) -> OutboundMessageResponse:
    try:
        message, job, replay = enqueue_outbound_message(session, context, payload)
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return OutboundMessageResponse(
        message="Existing queued message returned" if replay else "Message queued",
        data=_message_data(session, message, job),
    )


@router.get("", response_model=OutboundMessageListResponse)
def list_outbound_messages(
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_VIEW))],
    session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    lead_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status", pattern="^(queued|processing|sent|failed|unknown|cancelled|skipped)$")] = None,
) -> OutboundMessageListResponse:
    try:
        result = get_outbound_messages(
            session,
            context,
            page=page,
            size=size,
            lead_id=lead_id,
            customer_id=customer_id,
            status=status_filter,
        )
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return OutboundMessageListResponse(data=OutboundMessageListData(
        items=[_message_data(session, item, item.outbox_job) for item in result.items],
        page=page, size=size, total=result.total,
    ))


@router.get("/{message_id}", response_model=OutboundMessageResponse)
def read_outbound_message(
    message_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_VIEW))],
    session: Annotated[Session, Depends(get_db)],
) -> OutboundMessageResponse:
    try:
        message, job = get_outbound_message(session, context, message_id)
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return OutboundMessageResponse(data=_message_data(session, message, job))


@router.post("/{message_id}/cancel", response_model=OutboundMessageResponse)
def cancel_outbound_message_route(
    message_id: uuid.UUID,
    payload: OutboundCancelRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_CANCEL))],
    session: Annotated[Session, Depends(get_db)],
) -> OutboundMessageResponse:
    try:
        message, job = cancel_outbound_message(session, context, message_id, payload.reason)
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return OutboundMessageResponse(message="Message cancelled", data=_message_data(session, message, job))
