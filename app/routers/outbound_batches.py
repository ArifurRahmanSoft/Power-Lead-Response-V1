import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.core.storage import ObjectStorage, get_object_storage
from app.models.outbound_message import OutboundMessage, OutboundMessageBatch
from app.providers.smtp import SmtpProvider, get_smtp_provider
from app.repositories.outbound_message_repository import (
    all_batch_recipients,
    pending_worker_job_count,
)
from app.schemas.outbound_message import (
    BulkBatchData,
    BulkCancelResponse,
    BulkConfirmRequest,
    BulkPreviewPageData,
    BulkPreviewRequest,
    BulkPreviewResponse,
    BulkRecipientData,
    DirectBatchRecipientData,
    DirectBatchSendData,
    DirectBatchSendRequest,
    DirectBatchSendResponse,
    MessageEventData,
    OutboundCancelRequest,
    SenderSnapshotData,
)
from app.services.bulk_outbound_service import (
    BulkBatchNotFoundError,
    BulkSelectionLimitError,
    cancel_bulk_batch,
    confirm_bulk_batch,
    create_bulk_preview,
    deliver_bulk_direct,
    get_bulk_batch,
    get_bulk_recipients,
    prepare_bulk_direct,
)
from app.services.outbound_message_service import (
    OutboundIdempotencyConflictError,
    OutboundMessageEligibilityError,
    OutboundPreviewExpiredError,
    OutboundPreviewStaleError,
)
from app.services.sender_account_service import get_sender_for_context

router = APIRouter()


def _raise_error(exception: Exception) -> None:
    if isinstance(exception, BulkBatchNotFoundError):
        raise HTTPException(status_code=404, detail="Email batch not found") from exception
    if isinstance(exception, BulkSelectionLimitError):
        raise HTTPException(status_code=422, detail={
            "message": "Selection exceeds the configured bulk email limit",
            "selection_count": exception.count,
            "maximum": exception.maximum,
        }) from exception
    if isinstance(exception, OutboundMessageEligibilityError):
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    if isinstance(exception, OutboundPreviewExpiredError):
        raise HTTPException(status_code=409, detail="Batch preview expired; create a new preview") from exception
    if isinstance(exception, OutboundPreviewStaleError):
        raise HTTPException(status_code=409, detail="Batch preview is no longer current; create a new preview") from exception
    if isinstance(exception, OutboundIdempotencyConflictError):
        raise HTTPException(status_code=409, detail="Idempotency key was already used for different batch content") from exception
    raise exception


def _batch_data(session: Session, context: WorkspaceContext, batch: OutboundMessageBatch) -> BulkBatchData:
    sender = get_sender_for_context(session, context, batch.sender_account_id)
    counts = {key: 0 for key in ("queued", "processing", "sent", "failed", "unknown", "cancelled", "skipped")}
    for recipient in all_batch_recipients(session, batch.id):
        if recipient.message_id:
            message = session.get(OutboundMessage, recipient.message_id)
            if message and message.status in counts:
                counts[message.status] += 1
    status_value = batch.status
    active = counts["queued"] + counts["processing"]
    terminal = (
        counts["sent"] + counts["failed"] + counts["unknown"]
        + counts["cancelled"] + counts["skipped"]
    )
    if batch.confirmed_at is not None:
        if active:
            status_value = "processing" if counts["processing"] else "queued"
        elif terminal:
            status_value = "completed" if counts["sent"] == terminal else (
                "cancelled" if counts["cancelled"] == terminal else "partially_completed"
            )
    return BulkBatchData(
        id=batch.id,
        status=status_value,
        selection_mode=batch.selection_mode,
        selection_count=batch.selection_count,
        eligible_count=batch.eligible_count,
        blocked_count=batch.blocked_count,
        preview_hash=batch.preview_hash,
        expires_at=batch.expires_at,
        sender=SenderSnapshotData(
            display_name=sender.display_name,
            sender_email=sender.sender_email,
            reply_to=sender.reply_to,
        ),
        template_id=batch.template_id,
        template_version=batch.template_version,
        contact_stage=batch.contact_stage,
        queued_count=counts["queued"], processing_count=counts["processing"],
        sent_count=counts["sent"], failed_count=counts["failed"],
        unknown_count=counts["unknown"], cancelled_count=counts["cancelled"],
        skipped_count=counts["skipped"],
    )


def _page_response(session: Session, context: WorkspaceContext, batch: OutboundMessageBatch, *, page: int, size: int, message: str | None = None) -> BulkPreviewResponse:
    result = get_bulk_recipients(session, context, batch.id, page=page, size=size)
    items: list[BulkRecipientData] = []
    for item in result.items:
        preview = getattr(item, "preview", None)
        outbound = getattr(item, "message", None)
        items.append(BulkRecipientData(
            id=item.id,
            lead_id=item.lead_id,
            requested_lead_id=item.requested_lead_id,
            recipient_email=item.recipient_email,
            preview_status=item.preview_status,
            exclusion_code=item.exclusion_code,
            exclusion_reason=item.exclusion_reason,
            warnings=item.warnings or [],
            preview_id=item.preview_id,
            message_id=item.message_id,
            message_status=outbound.status if outbound else None,
            subject=preview.subject if preview else (outbound.subject if outbound else None),
            body_html=preview.body_html if preview else None,
            body_text=preview.body_text if preview else None,
            events=[MessageEventData.model_validate(event) for event in getattr(item, "message_events", [])],
        ))
    return BulkPreviewResponse(message=message, data=BulkPreviewPageData(
        batch=_batch_data(session, context, batch), items=items,
        page=page, size=size, total=result.total,
    ))


def _direct_response(
    session: Session,
    context: WorkspaceContext,
    batch: OutboundMessageBatch,
    *,
    replayed: bool,
) -> DirectBatchSendResponse:
    recipients: list[DirectBatchRecipientData] = []
    counts = {
        key: 0
        for key in (
            "queued", "processing", "sent", "failed", "unknown", "skipped"
        )
    }
    for recipient in all_batch_recipients(session, batch.id):
        message = (
            session.get(OutboundMessage, recipient.message_id)
            if recipient.message_id
            else None
        )
        recipient_status = message.status if message else "skipped"
        if recipient_status in counts:
            counts[recipient_status] += 1
        skip_code = None
        skip_reason = None
        if message is None:
            skip_code = recipient.exclusion_code
            skip_reason = recipient.exclusion_reason
        elif message.status == "skipped":
            skip_code = message.failure_code or "not_eligible"
            skip_reason = message.failure_reason
        recipients.append(DirectBatchRecipientData(
            requested_lead_id=recipient.requested_lead_id,
            lead_id=recipient.lead_id,
            message_id=recipient.message_id,
            status=recipient_status,
            skip_code=skip_code,
            skip_reason=skip_reason,
            warnings=recipient.warnings or [],
        ))
    batch_state = _batch_data(session, context, batch).status
    return DirectBatchSendResponse(
        message=(
            "Existing email batch returned"
            if replayed
            else (
                "No eligible recipients; no SMTP attempts were made"
                if batch.eligible_count == 0
                else "Direct email processing completed"
            )
        ),
        data=DirectBatchSendData(
            batch_id=batch.id,
            status=batch_state,
            selection_count=batch.selection_count,
            queued_count=counts["queued"],
            processing_count=counts["processing"],
            sent_count=counts["sent"],
            failed_count=counts["failed"],
            unknown_count=counts["unknown"],
            skipped_count=counts["skipped"],
            pending_worker_job_count=pending_worker_job_count(
                session, tenant_id=context.tenant.id
            ),
            contact_type=batch.contact_stage,
            replayed=replayed,
            recipients=recipients,
        ),
    )


@router.post("/preview", response_model=BulkPreviewResponse, status_code=status.HTTP_201_CREATED)
def preview_batch(
    payload: BulkPreviewRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_SEND))],
    session: Annotated[Session, Depends(get_db)],
) -> BulkPreviewResponse:
    try:
        batch = create_bulk_preview(session, context, payload)
    except Exception as exception:
        _raise_error(exception)
        raise
    return _page_response(session, context, batch, page=payload.page, size=payload.size, message="Bulk preview created")


@router.post(
    "/send",
    response_model=DirectBatchSendResponse,
    status_code=status.HTTP_200_OK,
)
async def send_batch_directly(
    payload: DirectBatchSendRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_SEND)),
    ],
    session: Annotated[Session, Depends(get_db)],
    provider: Annotated[SmtpProvider, Depends(get_smtp_provider)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> DirectBatchSendResponse:
    try:
        batch, replayed = prepare_bulk_direct(session, context, payload)
        if not replayed and batch.eligible_count:
            factory = sessionmaker(
                bind=session.get_bind(),
                expire_on_commit=False,
                class_=Session,
            )
            await deliver_bulk_direct(
                batch.id,
                provider=provider,
                storage=storage,
                session_factory=factory,
            )
            session.expire_all()
            batch = get_bulk_batch(session, context, batch.id)
    except Exception as exception:
        _raise_error(exception)
        raise
    return _direct_response(session, context, batch, replayed=replayed)


@router.post("/{batch_id}/confirm", response_model=BulkPreviewResponse, status_code=status.HTTP_202_ACCEPTED)
def confirm_batch(
    batch_id: uuid.UUID,
    payload: BulkConfirmRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_SEND))],
    session: Annotated[Session, Depends(get_db)],
) -> BulkPreviewResponse:
    try:
        batch, replay = confirm_bulk_batch(session, context, batch_id, payload)
    except Exception as exception:
        _raise_error(exception)
        raise
    return _page_response(session, context, batch, page=1, size=settings.mail_bulk_preview_page_size,
                          message="Existing batch returned" if replay else "Bulk email queued")


@router.get("/{batch_id}", response_model=BulkPreviewResponse)
def read_batch(
    batch_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_VIEW))],
    session: Annotated[Session, Depends(get_db)],
) -> BulkPreviewResponse:
    try:
        batch = get_bulk_batch(session, context, batch_id)
    except Exception as exception:
        _raise_error(exception)
        raise
    return _page_response(session, context, batch, page=1, size=settings.mail_bulk_preview_page_size)


@router.get("/{batch_id}/recipients", response_model=BulkPreviewResponse)
def read_batch_recipients(
    batch_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_VIEW))],
    session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BulkPreviewResponse:
    try:
        batch = get_bulk_batch(session, context, batch_id)
    except Exception as exception:
        _raise_error(exception)
        raise
    return _page_response(session, context, batch, page=page, size=size)


@router.post("/{batch_id}/cancel", response_model=BulkCancelResponse)
def cancel_batch(
    batch_id: uuid.UUID,
    payload: OutboundCancelRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_CANCEL))],
    session: Annotated[Session, Depends(get_db)],
) -> BulkCancelResponse:
    try:
        counts = cancel_bulk_batch(session, context, batch_id, payload.reason)
    except Exception as exception:
        _raise_error(exception)
        raise
    return BulkCancelResponse(
        message="Batch cancellation processed; in-flight provider calls cannot be recalled",
        data=counts,
    )
