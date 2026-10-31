import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.models.whatsapp import WhatsAppChannel, WhatsAppConversation, WhatsAppMessage
from app.providers.meta_instagram import (
    MetaInstagramError,
    MetaInstagramProvider,
    get_meta_instagram_provider,
)
from app.schemas.meta_instagram import (
    InstagramChannelData,
    InstagramChannelResponse,
    InstagramConversationData,
    InstagramConversationLinkRequest,
    InstagramConversationPageData,
    InstagramConversationPageResponse,
    InstagramConversationResponse,
    InstagramLinkedRecord,
    InstagramMediaMetadata,
    InstagramMessageData,
    InstagramMessagePageData,
    InstagramMessagePageResponse,
    InstagramReadData,
    InstagramReadResponse,
    InstagramRecoveryData,
    InstagramRecoveryResponse,
    InstagramReplyData,
    InstagramReplyRequest,
    InstagramReplyResponse,
)
from app.services.meta_instagram_service import (
    MetaInstagramChannelNotConfiguredError,
    MetaInstagramEligibilityError,
    MetaInstagramIdempotencyConflictError,
    MetaInstagramLinkTargetError,
    MetaInstagramNotFoundError,
    MetaInstagramPayloadError,
    MetaInstagramWebhookRejectedError,
    account_display_name,
    calculate_eligibility,
    channel_for_context,
    conversation_display_name,
    conversation_unread_count,
    get_instagram_conversation_page,
    get_instagram_message_page,
    link_conversation,
    mark_read,
    page_count,
    process_instagram_webhook_payload,
    recover_operation,
    send_reply,
)
from app.services.whatsapp_service import conversation_contact_name


router = APIRouter()
webhook_router = APIRouter()
logger = logging.getLogger(__name__)


def _message_data(message: WhatsAppMessage) -> InstagramMessageData:
    source = "contact" if message.direction == "inbound" else (
        "crm" if message.requested_by_user_id else "instagram_inbox"
    )
    return InstagramMessageData(
        id=message.id,
        conversation_id=message.conversation_id,
        direction=message.direction,
        source=source,
        actor_type=(
            "contact"
            if message.direction == "inbound"
            else ("crm_user" if message.requested_by_user_id else "instagram_account")
        ),
        actor_user_id=message.requested_by_user_id,
        body=message.body,
        media=[InstagramMediaMetadata.model_validate(item) for item in message.media_metadata],
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
    account_name: str | None,
    unread_count: int,
) -> InstagramConversationData:
    display_name, available, display_source = conversation_display_name(
        database_session,
        conversation,
        linked_name=contact_name,
    )
    linked: InstagramLinkedRecord | None = None
    if conversation.lead_id is not None:
        linked = InstagramLinkedRecord(
            type="lead",
            id=conversation.lead_id,
            display_name=display_name,
        )
    elif conversation.customer_id is not None:
        linked = InstagramLinkedRecord(
            type="customer",
            id=conversation.customer_id,
            display_name=display_name,
        )
    return InstagramConversationData(
        id=conversation.id,
        igsid=conversation.external_address,
        contact_display_name=display_name,
        display_name_available=available,
        contact_display_name_source=display_source,
        account_display_name=account_name or "Instagram account",
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
    raise HTTPException(status_code=404, detail="Instagram conversation not found") from exception


def _channel_unavailable(exception: Exception) -> None:
    raise HTTPException(
        status_code=503,
        detail={
            "code": "meta_instagram_channel_not_configured",
            "message": "The workspace Instagram account channel is not configured",
        },
    ) from exception


@router.get("/channel", response_model=InstagramChannelResponse)
def get_channel(
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaInstagramProvider, Depends(get_meta_instagram_provider)],
) -> InstagramChannelResponse:
    try:
        channel = channel_for_context(database_session, context, provider)
    except MetaInstagramChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    return InstagramChannelResponse(
        data=InstagramChannelData(
            id=channel.id,
            account_id=channel.provider_channel_id or "",
            account_display_name=account_display_name(channel),
            configuration_id=provider.configuration_id or None,
            ready=provider.send_api_configured,
            active=channel.is_active,
        )
    )


@router.get("/operations/recover", response_model=InstagramRecoveryResponse)
def recover_reply(
    idempotency_key: Annotated[
        str,
        Query(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"),
    ],
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))],
    database_session: Annotated[Session, Depends(get_db)],
) -> InstagramRecoveryResponse:
    result = recover_operation(database_session, context, idempotency_key)
    return InstagramRecoveryResponse(
        data=InstagramRecoveryData(
            idempotency_key=idempotency_key,
            found=result.message is not None,
            operation_state=result.operation_state,
            message=_message_data(result.message) if result.message else None,
        )
    )


@router.get("/conversations", response_model=InstagramConversationPageResponse)
def list_conversations(
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> InstagramConversationPageResponse:
    result = get_instagram_conversation_page(
        database_session,
        context,
        search=search,
        page=page,
        size=size,
    )
    return InstagramConversationPageResponse(
        data=InstagramConversationPageData(
            items=[
                _conversation_data(
                    database_session,
                    item.conversation,
                    contact_name=item.lead_name or item.customer_name,
                    account_name=item.channel_display_name,
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
    response_model=InstagramMessagePageResponse,
)
def list_messages(
    conversation_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> InstagramMessagePageResponse:
    try:
        conversation, result = get_instagram_message_page(
            database_session,
            context,
            conversation_id,
            page=page,
            size=size,
        )
    except MetaInstagramNotFoundError as exception:
        _not_found(exception)
    channel = database_session.get(WhatsAppChannel, conversation.channel_id)
    return InstagramMessagePageResponse(
        data=InstagramMessagePageData(
            conversation=_conversation_data(
                database_session,
                conversation,
                contact_name=conversation_contact_name(database_session, conversation),
                account_name=account_display_name(channel),
                unread_count=conversation_unread_count(database_session, context, conversation),
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
    response_model=InstagramReplyResponse,
)
async def reply(
    conversation_id: uuid.UUID,
    payload: InstagramReplyRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaInstagramProvider, Depends(get_meta_instagram_provider)],
) -> InstagramReplyResponse:
    try:
        result = await send_reply(
            database_session,
            context,
            conversation_id,
            payload,
            provider,
        )
    except MetaInstagramNotFoundError as exception:
        _not_found(exception)
    except MetaInstagramChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    except MetaInstagramIdempotencyConflictError as exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "message": "The idempotency key was already used for a different Instagram operation",
            },
        ) from exception
    except MetaInstagramEligibilityError as exception:
        detail: dict[str, object] = {"code": exception.code, "message": exception.message}
        detail.update(exception.details)
        raise HTTPException(
            status_code=429 if exception.code == "rate_limit_exceeded" else 409,
            detail=detail,
        ) from exception
    except MetaInstagramError as exception:
        raise HTTPException(
            status_code=503,
            detail={"code": exception.code, "message": exception.safe_message},
        ) from exception
    return InstagramReplyResponse(
        data=InstagramReplyData(
            message=_message_data(result.message),
            replayed=result.replayed,
            operation_state=(
                "in_progress"
                if result.message.submission_state == "processing"
                else "completed"
            ),
        )
    )


@router.post(
    "/conversations/{conversation_id}/read",
    response_model=InstagramReadResponse,
)
def mark_conversation_read(
    conversation_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))],
    database_session: Annotated[Session, Depends(get_db)],
) -> InstagramReadResponse:
    try:
        cursor = mark_read(database_session, context, conversation_id)
    except MetaInstagramNotFoundError as exception:
        _not_found(exception)
    return InstagramReadResponse(
        data=InstagramReadData(
            conversation_id=conversation_id,
            last_read_at=cursor.last_read_at,
            provider_receipt_sent=False,
        )
    )


@router.put(
    "/conversations/{conversation_id}/link",
    response_model=InstagramConversationResponse,
)
def link(
    conversation_id: uuid.UUID,
    payload: InstagramConversationLinkRequest,
    context: Annotated[WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))],
    database_session: Annotated[Session, Depends(get_db)],
) -> InstagramConversationResponse:
    try:
        conversation = link_conversation(
            database_session,
            context,
            conversation_id,
            payload,
        )
    except MetaInstagramNotFoundError as exception:
        _not_found(exception)
    except MetaInstagramLinkTargetError as exception:
        raise HTTPException(
            status_code=404,
            detail="The link target is unavailable or outside the authorized scope",
        ) from exception
    channel = database_session.get(WhatsAppChannel, conversation.channel_id)
    return InstagramConversationResponse(
        message="Instagram conversation linked",
        data=_conversation_data(
            database_session,
            conversation,
            contact_name=conversation_contact_name(database_session, conversation),
            account_name=account_display_name(channel),
            unread_count=0,
        ),
    )


@webhook_router.get("/api/webhooks/meta/instagram")
def verify_instagram_webhook(
    request: Request,
    provider: Annotated[MetaInstagramProvider, Depends(get_meta_instagram_provider)],
    mode: Annotated[str, Query(alias="hub.mode")],
    verify_token: Annotated[str, Query(alias="hub.verify_token")],
    challenge: Annotated[str, Query(alias="hub.challenge")],
) -> Response:
    if not provider.webhook_configured:
        raise HTTPException(status_code=503, detail="Meta Instagram webhook is not configured")
    supplied_token = getattr(request.state, "meta_webhook_verify_token", verify_token)
    if not provider.validate_challenge(mode=mode, verify_token=supplied_token):
        logger.warning(
            "meta_instagram_webhook_rejected reason_code=verification_challenge_invalid"
        )
        raise HTTPException(status_code=403, detail="Invalid Meta Instagram webhook verification")
    return Response(content=challenge, media_type="text/plain")


@webhook_router.post("/api/webhooks/meta/instagram")
async def receive_instagram_webhook(
    request: Request,
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaInstagramProvider, Depends(get_meta_instagram_provider)],
) -> Response:
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not provider.validate_signature(raw_body, signature):
        reason_code = "signature_missing" if not signature else "signature_invalid"
        logger.warning(
            "meta_instagram_webhook_rejected reason_code=%s app_secret_source=%s",
            reason_code,
            provider.app_secret_source,
        )
        raise HTTPException(status_code=403, detail="Invalid Meta Instagram webhook signature")
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError) as exception:
        raise HTTPException(status_code=400, detail="Invalid Meta Instagram webhook JSON") from exception
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Meta Instagram webhook JSON")
    try:
        process_instagram_webhook_payload(database_session, provider, payload)
    except MetaInstagramWebhookRejectedError as exception:
        database_session.rollback()
        logger.warning(
            "meta_instagram_webhook_rejected reason_code=%s",
            exception.reason_code,
        )
        raise HTTPException(status_code=403, detail="Unknown Meta Instagram account channel") from exception
    except MetaInstagramPayloadError as exception:
        database_session.rollback()
        raise HTTPException(status_code=400, detail="Invalid Meta Instagram webhook payload") from exception
    except SQLAlchemyError as exception:
        database_session.rollback()
        raise HTTPException(status_code=503, detail="Meta Instagram webhook persistence failed") from exception
    return Response(status_code=status.HTTP_200_OK)
