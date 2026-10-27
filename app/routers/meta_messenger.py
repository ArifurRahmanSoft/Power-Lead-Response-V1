import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.models.whatsapp import WhatsAppChannel, WhatsAppConversation, WhatsAppMessage
from app.providers.meta_messenger import (
    MetaMessengerError,
    MetaMessengerProvider,
    get_meta_messenger_provider,
)
from app.schemas.meta_messenger import (
    MessengerChannelData,
    MessengerChannelResponse,
    MessengerConversationData,
    MessengerConversationLinkRequest,
    MessengerConversationPageData,
    MessengerConversationPageResponse,
    MessengerConversationResponse,
    MessengerEligibilityData,
    MessengerLinkedRecord,
    MessengerMediaMetadata,
    MessengerMessageData,
    MessengerMessagePageData,
    MessengerMessagePageResponse,
    MessengerProfileRefreshRequest,
    MessengerProfileRefreshResponse,
    MessengerReadData,
    MessengerReadResponse,
    MessengerRecoveryData,
    MessengerRecoveryResponse,
    MessengerReplyData,
    MessengerReplyRequest,
    MessengerReplyResponse,
)
from app.services.meta_messenger_service import (
    MetaMessengerChannelNotConfiguredError,
    MetaMessengerEligibilityError,
    MetaMessengerIdempotencyConflictError,
    MetaMessengerLinkTargetError,
    MetaMessengerNotFoundError,
    MetaMessengerPayloadError,
    MetaMessengerWebhookRejectedError,
    calculate_eligibility,
    channel_for_context,
    conversation_display_name,
    conversation_unread_count,
    get_messenger_conversation_page,
    get_messenger_message_page,
    link_conversation,
    mark_read,
    page_count,
    page_display_name,
    process_messenger_webhook_payload,
    recover_operation,
    refresh_display_names,
    send_reply,
)
from app.services.whatsapp_service import conversation_contact_name


router = APIRouter()
webhook_router = APIRouter()


def _message_data(message: WhatsAppMessage) -> MessengerMessageData:
    source = "contact" if message.direction == "inbound" else (
        "crm" if message.idempotency_key else "page_inbox"
    )
    return MessengerMessageData(
        id=message.id,
        conversation_id=message.conversation_id,
        direction=message.direction,
        source=source,
        actor_type=("contact" if message.direction == "inbound" else (
            "crm_user" if message.requested_by_user_id else "page"
        )),
        actor_user_id=message.requested_by_user_id,
        body=message.body,
        media=[MessengerMediaMetadata.model_validate(item) for item in message.media_metadata],
        status=message.status,
        provider_message_id=message.provider_message_sid,
        provider_error_code=message.provider_error_code,
        failure_reason=message.failure_reason,
        requested_by_user_id=message.requested_by_user_id,
        occurred_at=message.occurred_at,
        completed_at=message.completed_at,
    )


def _conversation_data(
    database_session: Session,
    conversation: WhatsAppConversation,
    *,
    contact_name: str | None,
    page_name: str | None,
    unread_count: int,
) -> MessengerConversationData:
    display_name, available, display_source = conversation_display_name(
        database_session, conversation, linked_name=contact_name
    )
    linked: MessengerLinkedRecord | None = None
    if conversation.lead_id is not None:
        linked = MessengerLinkedRecord(type="lead", id=conversation.lead_id, display_name=display_name)
    elif conversation.customer_id is not None:
        linked = MessengerLinkedRecord(type="customer", id=conversation.customer_id, display_name=display_name)
    return MessengerConversationData(
        id=conversation.id,
        psid=conversation.external_address,
        contact_display_name=display_name,
        display_name_available=available,
        contact_display_name_source=display_source,
        page_display_name=page_name or "Facebook Page",
        lead_id=conversation.lead_id,
        customer_id=conversation.customer_id,
        linked_record=linked,
        last_message_at=conversation.last_message_at,
        last_message_preview=conversation.last_message_preview,
        last_inbound_at=conversation.last_inbound_at,
        unread_count=unread_count,
        eligibility=calculate_eligibility(database_session, conversation),
    )


def _not_found(exception: Exception) -> None:
    raise HTTPException(status_code=404, detail="Messenger conversation not found") from exception


def _channel_unavailable(exception: Exception) -> None:
    raise HTTPException(status_code=503, detail={
        "code": "meta_messenger_channel_not_configured",
        "message": "The workspace Messenger Page channel is not configured",
    }) from exception


@router.get("/channel", response_model=MessengerChannelResponse)
def get_channel(
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaMessengerProvider, Depends(get_meta_messenger_provider)],
) -> MessengerChannelResponse:
    try:
        channel = channel_for_context(database_session, context, provider)
    except MetaMessengerChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    return MessengerChannelResponse(data=MessengerChannelData(
        id=channel.id,
        page_id=channel.provider_channel_id or "",
        page_display_name=page_display_name(channel),
        display_name_available=bool(channel.display_name),
        profile_refreshed_at=channel.profile_refreshed_at,
        active=channel.is_active,
    ))


@router.post("/profiles/refresh", response_model=MessengerProfileRefreshResponse)
async def refresh_profiles(
    payload: MessengerProfileRefreshRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaMessengerProvider, Depends(get_meta_messenger_provider)],
) -> MessengerProfileRefreshResponse:
    try:
        data = await refresh_display_names(database_session, context, payload, provider)
    except MetaMessengerNotFoundError as exception:
        _not_found(exception)
    except MetaMessengerChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    except MetaMessengerEligibilityError as exception:
        raise HTTPException(
            status_code=429 if exception.code == "profile_refresh_rate_limited" else 422,
            detail={"code": exception.code, "message": exception.message},
        ) from exception
    return MessengerProfileRefreshResponse(data=data)


@router.get("/operations/recover", response_model=MessengerRecoveryResponse)
def recover_reply(
    idempotency_key: Annotated[str, Query(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")],
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))],
    database_session: Annotated[Session, Depends(get_db)],
) -> MessengerRecoveryResponse:
    result = recover_operation(database_session, context, idempotency_key)
    return MessengerRecoveryResponse(data=MessengerRecoveryData(
        idempotency_key=idempotency_key,
        found=result.message is not None,
        operation_state=result.operation_state,
        message=_message_data(result.message) if result.message else None,
    ))


@router.get("/conversations", response_model=MessengerConversationPageResponse)
def list_conversations(
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> MessengerConversationPageResponse:
    result = get_messenger_conversation_page(database_session, context, search=search, page=page, size=size)
    return MessengerConversationPageResponse(data=MessengerConversationPageData(
        items=[_conversation_data(database_session, item.conversation,
            contact_name=item.lead_name or item.customer_name,
            page_name=item.channel_display_name,
            unread_count=item.unread_count) for item in result.items],
        page=page, size=size, total=result.total, pages=page_count(result.total, size),
    ))


@router.get("/conversations/{conversation_id}/messages", response_model=MessengerMessagePageResponse)
def list_messages(
    conversation_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessengerMessagePageResponse:
    try:
        conversation, result = get_messenger_message_page(
            database_session, context, conversation_id, page=page, size=size
        )
    except MetaMessengerNotFoundError as exception:
        _not_found(exception)
    return MessengerMessagePageResponse(data=MessengerMessagePageData(
        conversation=_conversation_data(database_session, conversation,
            contact_name=conversation_contact_name(database_session, conversation),
            page_name=page_display_name(
                database_session.get(WhatsAppChannel, conversation.channel_id)
            ),
            unread_count=conversation_unread_count(database_session, context, conversation)),
        items=[_message_data(item) for item in result.items],
        page=page, size=size, total=result.total, pages=page_count(result.total, size),
    ))


@router.post("/conversations/{conversation_id}/reply", response_model=MessengerReplyResponse)
async def reply(
    conversation_id: uuid.UUID,
    payload: MessengerReplyRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaMessengerProvider, Depends(get_meta_messenger_provider)],
) -> MessengerReplyResponse:
    try:
        result = await send_reply(database_session, context, conversation_id, payload, provider)
    except MetaMessengerNotFoundError as exception:
        _not_found(exception)
    except MetaMessengerChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    except MetaMessengerIdempotencyConflictError as exception:
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict",
            "message": "The idempotency key was already used for a different request"}) from exception
    except MetaMessengerEligibilityError as exception:
        detail: dict[str, object] = {"code": exception.code, "message": exception.message}
        detail.update(exception.details)
        raise HTTPException(status_code=429 if exception.code == "rate_limit_exceeded" else 409,
                            detail=detail) from exception
    except MetaMessengerError as exception:
        raise HTTPException(status_code=503, detail={"code": exception.code,
            "message": exception.safe_message}) from exception
    return MessengerReplyResponse(data=MessengerReplyData(
        message=_message_data(result.message), replayed=result.replayed,
        operation_state="in_progress" if result.message.submission_state == "processing" else "completed",
    ))


@router.post("/conversations/{conversation_id}/read", response_model=MessengerReadResponse)
def mark_conversation_read(
    conversation_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
) -> MessengerReadResponse:
    try:
        cursor = mark_read(database_session, context, conversation_id)
    except MetaMessengerNotFoundError as exception:
        _not_found(exception)
    return MessengerReadResponse(data=MessengerReadData(
        conversation_id=conversation_id, last_read_at=cursor.last_read_at
    ))


@router.put("/conversations/{conversation_id}/link", response_model=MessengerConversationResponse)
def link(
    conversation_id: uuid.UUID,
    payload: MessengerConversationLinkRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))],
    database_session: Annotated[Session, Depends(get_db)],
) -> MessengerConversationResponse:
    try:
        conversation = link_conversation(database_session, context, conversation_id, payload)
    except MetaMessengerNotFoundError as exception:
        _not_found(exception)
    except MetaMessengerLinkTargetError as exception:
        raise HTTPException(status_code=404,
            detail="The link target is unavailable or outside the authorized scope") from exception
    return MessengerConversationResponse(message="Messenger conversation linked",
        data=_conversation_data(database_session, conversation,
            contact_name=conversation_contact_name(database_session, conversation),
            page_name=page_display_name(
                database_session.get(WhatsAppChannel, conversation.channel_id)
            ),
            unread_count=0))


@webhook_router.get("/api/webhooks/meta/messenger")
def verify_messenger_webhook(
    request: Request,
    provider: Annotated[MetaMessengerProvider, Depends(get_meta_messenger_provider)],
    mode: Annotated[str, Query(alias="hub.mode")],
    verify_token: Annotated[str, Query(alias="hub.verify_token")],
    challenge: Annotated[str, Query(alias="hub.challenge")],
) -> Response:
    if not provider.webhook_configured:
        raise HTTPException(status_code=503, detail="Meta Messenger webhook is not configured")
    supplied_token = getattr(request.state, "meta_webhook_verify_token", verify_token)
    if not provider.validate_challenge(mode=mode, verify_token=supplied_token):
        raise HTTPException(status_code=403, detail="Invalid Meta Messenger webhook verification")
    return Response(content=challenge, media_type="text/plain")


@webhook_router.post("/api/webhooks/meta/messenger")
async def receive_messenger_webhook(
    request: Request,
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaMessengerProvider, Depends(get_meta_messenger_provider)],
) -> Response:
    raw_body = await request.body()
    if not provider.validate_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Invalid Meta Messenger webhook signature")
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError) as exception:
        raise HTTPException(status_code=400, detail="Invalid Meta Messenger webhook JSON") from exception
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Meta Messenger webhook JSON")
    try:
        process_messenger_webhook_payload(database_session, provider, payload)
    except MetaMessengerWebhookRejectedError as exception:
        database_session.rollback()
        raise HTTPException(status_code=403, detail="Unknown Meta Messenger Page channel") from exception
    except MetaMessengerPayloadError as exception:
        database_session.rollback()
        raise HTTPException(status_code=400, detail="Invalid Meta Messenger webhook payload") from exception
    except SQLAlchemyError as exception:
        database_session.rollback()
        raise HTTPException(status_code=503, detail="Meta Messenger webhook persistence failed") from exception
    return Response(status_code=status.HTTP_200_OK)
