import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.models.whatsapp import WhatsAppConversation, WhatsAppMessage
from app.providers.twilio_whatsapp import (
    TwilioWhatsAppProvider,
    get_twilio_whatsapp_provider,
)
from app.schemas.whatsapp import (
    WhatsAppConversationData,
    WhatsAppConversationLinkRequest,
    WhatsAppConversationPageData,
    WhatsAppConversationPageResponse,
    WhatsAppConversationResponse,
    WhatsAppMediaMetadata,
    WhatsAppMessageData,
    WhatsAppMessagePageData,
    WhatsAppMessagePageResponse,
    WhatsAppReadData,
    WhatsAppReadResponse,
    WhatsAppRecoveryData,
    WhatsAppRecoveryResponse,
    WhatsAppReplyData,
    WhatsAppReplyRequest,
    WhatsAppReplyResponse,
)
from app.services.whatsapp_service import (
    WhatsAppChannelNotConfiguredError,
    WhatsAppEligibilityError,
    WhatsAppIdempotencyConflictError,
    WhatsAppLinkTargetError,
    WhatsAppNotFoundError,
    WhatsAppServiceWindowClosedError,
    WhatsAppWebhookRejectedError,
    conversation_contact_name,
    get_conversation_page,
    get_message_page,
    link_conversation,
    mark_read,
    page_count,
    process_inbound_webhook,
    process_status_webhook,
    recover_operation,
    send_reply,
    service_window,
)


router = APIRouter()
webhook_router = APIRouter()


def _message_data(message: WhatsAppMessage) -> WhatsAppMessageData:
    return WhatsAppMessageData(
        id=message.id,
        conversation_id=message.conversation_id,
        direction=message.direction,
        body=message.body,
        media=[WhatsAppMediaMetadata.model_validate(item) for item in message.media_metadata],
        status=message.status,
        provider_message_sid=message.provider_message_sid,
        provider_error_code=message.provider_error_code,
        failure_reason=message.failure_reason,
        requested_by_user_id=message.requested_by_user_id,
        occurred_at=message.occurred_at,
        completed_at=message.completed_at,
    )


def _conversation_data(
    conversation: WhatsAppConversation,
    *,
    contact_name: str | None,
    unread_count: int,
) -> WhatsAppConversationData:
    allowed, expires_at = service_window(conversation)
    return WhatsAppConversationData(
        id=conversation.id,
        external_address=conversation.external_address,
        lead_id=conversation.lead_id,
        customer_id=conversation.customer_id,
        contact_name=contact_name,
        last_message_at=conversation.last_message_at,
        last_message_preview=conversation.last_message_preview,
        unread_count=unread_count,
        free_form_reply_allowed=allowed,
        service_window_expires_at=expires_at,
    )


def _not_found(exception: Exception) -> None:
    raise HTTPException(status_code=404, detail="WhatsApp conversation not found") from exception


@router.get("/operations/recover", response_model=WhatsAppRecoveryResponse)
def recover_whatsapp_operation(
    idempotency_key: Annotated[
        str,
        Query(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"),
    ],
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MESSAGES_SEND)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> WhatsAppRecoveryResponse:
    try:
        result = recover_operation(database_session, context, idempotency_key)
    except WhatsAppNotFoundError as exception:
        _not_found(exception)
    return WhatsAppRecoveryResponse(
        data=WhatsAppRecoveryData(
            idempotency_key=idempotency_key,
            found=result.message is not None,
            operation_state=result.operation_state,
            message=_message_data(result.message) if result.message else None,
        )
    )


@router.get("/conversations", response_model=WhatsAppConversationPageResponse)
def list_whatsapp_conversations(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MESSAGES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> WhatsAppConversationPageResponse:
    result = get_conversation_page(
        database_session,
        context,
        search=search,
        page=page,
        size=size,
    )
    return WhatsAppConversationPageResponse(
        data=WhatsAppConversationPageData(
            items=[
                _conversation_data(
                    item.conversation,
                    contact_name=item.lead_name or item.customer_name,
                    unread_count=item.unread_count,
                )
                for item in result.items
            ],
            page=page,
            size=size,
            total=result.total,
            pages=page_count(result.total, size),
        )
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=WhatsAppMessagePageResponse,
)
def list_whatsapp_messages(
    conversation_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MESSAGES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> WhatsAppMessagePageResponse:
    try:
        conversation, result = get_message_page(
            database_session,
            context,
            conversation_id,
            page=page,
            size=size,
        )
    except WhatsAppNotFoundError as exception:
        _not_found(exception)
    return WhatsAppMessagePageResponse(
        data=WhatsAppMessagePageData(
            conversation=_conversation_data(
                conversation,
                contact_name=conversation_contact_name(database_session, conversation),
                unread_count=0,
            ),
            items=[_message_data(item) for item in result.items],
            page=page,
            size=size,
            total=result.total,
            pages=page_count(result.total, size),
        )
    )


@router.post(
    "/conversations/{conversation_id}/reply",
    response_model=WhatsAppReplyResponse,
)
async def reply_to_whatsapp_conversation(
    conversation_id: uuid.UUID,
    payload: WhatsAppReplyRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MESSAGES_SEND)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[TwilioWhatsAppProvider, Depends(get_twilio_whatsapp_provider)],
) -> WhatsAppReplyResponse:
    try:
        result = await send_reply(
            database_session,
            context,
            conversation_id,
            payload,
            provider,
        )
    except WhatsAppNotFoundError as exception:
        _not_found(exception)
    except WhatsAppChannelNotConfiguredError as exception:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "whatsapp_channel_not_configured",
                "message": "The workspace WhatsApp Sandbox channel is not configured",
            },
        ) from exception
    except WhatsAppServiceWindowClosedError as exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "approved_template_required",
                "message": "The 24-hour customer-service window is closed; an approved WhatsApp template is required",
                "service_window_expires_at": (
                    exception.expires_at.isoformat() if exception.expires_at else None
                ),
            },
        ) from exception
    except WhatsAppIdempotencyConflictError as exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "message": "The idempotency key was already used for a different request",
            },
        ) from exception
    except WhatsAppEligibilityError as exception:
        raise HTTPException(
            status_code=429 if exception.code == "rate_limit_exceeded" else 409,
            detail={"code": exception.code, "message": exception.message},
        ) from exception
    operation_state = (
        "in_progress"
        if result.message.submission_state == "processing"
        else "completed"
    )
    return WhatsAppReplyResponse(
        data=WhatsAppReplyData(
            message=_message_data(result.message),
            replayed=result.replayed,
            operation_state=operation_state,
        )
    )


@router.post(
    "/conversations/{conversation_id}/read",
    response_model=WhatsAppReadResponse,
)
def mark_whatsapp_conversation_read(
    conversation_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MESSAGES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> WhatsAppReadResponse:
    try:
        cursor = mark_read(database_session, context, conversation_id)
    except WhatsAppNotFoundError as exception:
        _not_found(exception)
    return WhatsAppReadResponse(
        data=WhatsAppReadData(
            conversation_id=conversation_id,
            unread_count=0,
            last_read_at=cursor.last_read_at,
        )
    )


@router.put(
    "/conversations/{conversation_id}/link",
    response_model=WhatsAppConversationResponse,
)
def link_whatsapp_conversation(
    conversation_id: uuid.UUID,
    payload: WhatsAppConversationLinkRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MESSAGES_SEND)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> WhatsAppConversationResponse:
    try:
        conversation = link_conversation(
            database_session,
            context,
            conversation_id,
            payload,
        )
    except WhatsAppNotFoundError as exception:
        _not_found(exception)
    except WhatsAppLinkTargetError as exception:
        raise HTTPException(
            status_code=404,
            detail="The link target is unavailable or outside the authorized scope",
        ) from exception
    return WhatsAppConversationResponse(
        message="WhatsApp conversation linked",
        data=_conversation_data(
            conversation,
            contact_name=conversation_contact_name(database_session, conversation),
            unread_count=0,
        ),
    )


async def _validated_form(
    request: Request,
    provider: TwilioWhatsAppProvider,
) -> dict[str, str]:
    form_data = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not provider.validate_signature(
        request_url=str(request.url),
        form_values=form_data,
        signature=signature,
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")
    return {str(key): str(value) for key, value in form_data.items()}


@webhook_router.post("/api/webhooks/twilio/whatsapp/inbound")
async def twilio_whatsapp_inbound(
    request: Request,
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[TwilioWhatsAppProvider, Depends(get_twilio_whatsapp_provider)],
) -> Response:
    values = await _validated_form(request, provider)
    try:
        process_inbound_webhook(database_session, provider, values)
    except WhatsAppWebhookRejectedError as exception:
        raise HTTPException(status_code=403, detail="Unknown WhatsApp channel") from exception
    return Response(content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>", media_type="application/xml")


@webhook_router.post("/api/webhooks/twilio/whatsapp/status")
async def twilio_whatsapp_status(
    request: Request,
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[TwilioWhatsAppProvider, Depends(get_twilio_whatsapp_provider)],
) -> Response:
    values = await _validated_form(request, provider)
    try:
        process_status_webhook(database_session, provider, values)
    except WhatsAppWebhookRejectedError as exception:
        raise HTTPException(status_code=403, detail="Unknown WhatsApp channel") from exception
    return Response(status_code=200)
