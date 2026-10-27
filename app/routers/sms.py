import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.models.sms_message import SmsBatch, SmsMessage
from app.providers.twilio_sms import (
    TwilioSmsProvider,
    get_twilio_sms_provider,
    safe_twilio_rejection_message,
)
from app.repositories.sms_repository import list_batch_messages
from app.schemas.sms import (
    SmsBatchData,
    SmsBatchResponse,
    SmsMessageData,
    SmsMessageEventData,
    SmsMessageResponse,
    SmsRecipientResult,
    SmsRecoveryData,
    SmsRecoveryResponse,
    SmsSendRequest,
)
from app.services.sms_service import (
    SmsBatchLimitError,
    SmsBatchNotFoundError,
    SmsIdempotencyConflictError,
    SmsMessageNotFoundError,
    SmsStatusRefreshError,
    SmsTemplateNotFoundError,
    get_sms_batch_record,
    get_sms_message_record,
    refresh_sms_status,
    recover_sms_batch_by_idempotency,
    send_sms_batch,
    sms_batch_recipients,
    sms_message_events,
)

router = APIRouter()


def _service_error(exception: Exception) -> None:
    if isinstance(exception, SmsBatchLimitError):
        raise HTTPException(
            status_code=422,
            detail="SMS selection exceeds the configured direct-send batch limit",
        ) from exception
    if isinstance(exception, SmsTemplateNotFoundError):
        raise HTTPException(status_code=404, detail="Message template not found") from exception
    if isinstance(exception, SmsIdempotencyConflictError):
        raise HTTPException(
            status_code=409,
            detail="Idempotency key was already used for a different SMS request",
        ) from exception
    if isinstance(exception, (SmsBatchNotFoundError, SmsMessageNotFoundError)):
        raise HTTPException(status_code=404, detail="SMS batch or message not found") from exception
    if isinstance(exception, SmsStatusRefreshError):
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    raise exception


def _message_data(database_session: Session, message: SmsMessage) -> SmsMessageData:
    failure_reason = _safe_persisted_twilio_reason(
        message.provider_error_code, message.failure_reason
    )
    return SmsMessageData(
        id=message.id,
        batch_id=message.batch_id,
        lead_id=message.lead_id,
        message_template_id=message.message_template_id,
        template_updated_at=message.template_updated_at,
        contact_type=message.contact_type,
        recipient_phone=message.recipient_phone,
        rendered_body=message.rendered_body,
        status=message.status,
        provider_message_sid=message.provider_message_sid,
        provider_error_code=message.provider_error_code,
        failure_reason=failure_reason,
        requested_by_user_id=message.requested_by_user_id,
        attempted_at=message.attempted_at,
        accepted_at=message.accepted_at,
        sent_at=message.sent_at,
        delivered_at=message.delivered_at,
        failed_at=message.failed_at,
        last_status_checked_at=message.last_status_checked_at,
        created_at=message.created_at,
        updated_at=message.updated_at,
        events=[
            SmsMessageEventData(
                status=event.status,
                provider_error_code=event.provider_error_code,
                safe_reason=_safe_persisted_twilio_reason(
                    event.provider_error_code, event.safe_reason
                ),
                occurred_at=event.occurred_at,
            )
            for event in sms_message_events(database_session, message)
        ],
    )


def _safe_persisted_twilio_reason(
    reason_code: str | None, stored_reason: str | None
) -> str | None:
    if reason_code and reason_code.isdecimal():
        return safe_twilio_rejection_message(reason_code)
    return stored_reason


def _batch_data(
    database_session: Session,
    batch: SmsBatch,
    *,
    replayed: bool,
) -> SmsBatchData:
    recipients = sms_batch_recipients(database_session, batch)
    messages = {
        message.id: message for message in list_batch_messages(database_session, batch.id)
    }
    counts = {
        value: sum(recipient.status == value for recipient in recipients)
        for value in (
            "accepted",
            "queued",
            "sent",
            "delivered",
            "failed",
            "undelivered",
            "unknown",
            "skipped",
        )
    }
    return SmsBatchData(
        batch_id=batch.id,
        status=batch.status,
        selection_count=batch.selection_count,
        contact_type=batch.contact_type,
        replayed=replayed,
        recipients=[
            SmsRecipientResult(
                requested_lead_id=recipient.requested_lead_id,
                lead_id=recipient.lead_id,
                message_id=recipient.sms_message_id,
                status=recipient.status,
                provider_message_sid=(
                    messages[recipient.sms_message_id].provider_message_sid
                    if recipient.sms_message_id in messages
                    else None
                ),
                reason_code=recipient.reason_code,
                reason=_safe_persisted_twilio_reason(
                    recipient.reason_code, recipient.reason
                ),
                warnings=recipient.warnings or [],
            )
            for recipient in recipients
        ],
        **{f"{key}_count": value for key, value in counts.items()},
    )


@router.post("/send", response_model=SmsBatchResponse)
async def send_sms(
    payload: SmsSendRequest,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_SEND))
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[TwilioSmsProvider, Depends(get_twilio_sms_provider)],
) -> SmsBatchResponse:
    if not provider.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "sms_configuration_unavailable",
                "classification": "pre_provider_failure",
                "provider_request_made": False,
                "message": "Twilio SMS is not configured in the running API process",
            },
        )
    try:
        batch, replayed = await send_sms_batch(
            database_session, context, payload, provider
        )
    except Exception as exception:
        _service_error(exception)
        raise
    return SmsBatchResponse(
        message="Existing SMS batch returned" if replayed else "SMS send completed",
        data=_batch_data(database_session, batch, replayed=replayed),
    )


@router.get("/batches/{batch_id}", response_model=SmsBatchResponse)
def read_sms_batch(
    batch_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SmsBatchResponse:
    try:
        batch = get_sms_batch_record(database_session, context, batch_id)
    except Exception as exception:
        _service_error(exception)
        raise
    return SmsBatchResponse(
        message="SMS batch status",
        data=_batch_data(database_session, batch, replayed=False),
    )


@router.get("/operations/recover", response_model=SmsRecoveryResponse)
def recover_sms_operation(
    idempotency_key: Annotated[str, Query(min_length=8, max_length=120)],
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SmsRecoveryResponse:
    cleaned_key = idempotency_key.strip()
    if not cleaned_key or any(character.isspace() for character in cleaned_key):
        raise HTTPException(
            status_code=422,
            detail="idempotency_key cannot contain whitespace",
        )
    try:
        batch = recover_sms_batch_by_idempotency(
            database_session, context, cleaned_key
        )
    except Exception as exception:
        _service_error(exception)
        raise
    if batch is None:
        return SmsRecoveryResponse(
            message="No SMS operation exists for this idempotency key",
            data=SmsRecoveryData(
                idempotency_key=cleaned_key,
                found=False,
                operation_state="missing",
            ),
        )
    operation_state = (
        "in_progress" if batch.status == "processing" else "completed"
    )
    return SmsRecoveryResponse(
        message=(
            "SMS operation is still in progress"
            if operation_state == "in_progress"
            else "Existing SMS operation returned"
        ),
        data=SmsRecoveryData(
            idempotency_key=cleaned_key,
            found=True,
            operation_state=operation_state,
            batch=_batch_data(database_session, batch, replayed=True),
        ),
    )


@router.get("/messages/{message_id}", response_model=SmsMessageResponse)
def read_sms_message(
    message_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SmsMessageResponse:
    try:
        message = get_sms_message_record(database_session, context, message_id)
    except Exception as exception:
        _service_error(exception)
        raise
    return SmsMessageResponse(data=_message_data(database_session, message))


@router.post(
    "/messages/{message_id}/refresh-status",
    response_model=SmsMessageResponse,
)
async def refresh_message_status(
    message_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MAIL_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[TwilioSmsProvider, Depends(get_twilio_sms_provider)],
) -> SmsMessageResponse:
    if not provider.configured:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "sms_configuration_unavailable",
                "classification": "pre_provider_failure",
                "provider_request_made": False,
                "message": "Twilio SMS is not configured in the running API process",
            },
        )
    try:
        result = await refresh_sms_status(
            database_session, context, message_id, provider
        )
    except Exception as exception:
        _service_error(exception)
        raise
    return SmsMessageResponse(
        message=("SMS status refreshed" if result.refreshed else "Current SMS status returned"),
        refreshed=result.refreshed,
        next_refresh_at=result.next_refresh_at,
        data=_message_data(database_session, result.message),
    )
