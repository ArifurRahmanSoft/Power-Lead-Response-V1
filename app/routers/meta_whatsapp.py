import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.models.whatsapp import WhatsAppConversation, WhatsAppMessage
from app.providers.meta_whatsapp import (
    MetaWhatsAppError,
    MetaWhatsAppProvider,
    get_meta_whatsapp_provider,
)
from app.schemas.meta_whatsapp import (
    MetaChannelData,
    MetaChannelResponse,
    MetaConversationData,
    MetaConversationLinkRequest,
    MetaConversationPageData,
    MetaConversationPageResponse,
    MetaConversationResponse,
    MetaMediaMetadata,
    MetaMessageData,
    MetaMessagePageData,
    MetaMessagePageResponse,
    MetaOpenConversationRequest,
    MetaReadData,
    MetaReadResponse,
    MetaRecoveryData,
    MetaRecoveryResponse,
    MetaSendData,
    MetaSendRequest,
    MetaSendResponse,
    MetaTemplatePageData,
    MetaTemplatePageResponse,
    MetaWhatsAppActivationRequest,
    MetaWhatsAppCancelRequest,
    MetaWhatsAppCompleteRequest,
    MetaWhatsAppLaunchData,
    MetaWhatsAppLaunchResponse,
    MetaWhatsAppOperationData,
    MetaWhatsAppOperationResponse,
    MetaWhatsAppReadinessData,
    MetaWhatsAppReadinessResponse,
    MetaWhatsAppSettingReadiness,
)
from app.services.meta_whatsapp_oauth_service import (
    MetaWhatsAppAssetConflictError,
    MetaWhatsAppOAuthStateError,
    activate_meta_whatsapp_oauth,
    begin_meta_whatsapp_oauth,
    cancel_meta_whatsapp_oauth,
    complete_meta_whatsapp_oauth,
    get_meta_whatsapp_oauth_operation,
)
from app.services.meta_whatsapp_service import (
    MetaWhatsAppChannelNotConfiguredError,
    MetaWhatsAppEligibilityError,
    MetaWhatsAppIdempotencyConflictError,
    MetaWhatsAppLinkTargetError,
    MetaWhatsAppNotFoundError,
    MetaWhatsAppWebhookRejectedError,
    calculate_eligibility,
    channel_for_context,
    conversation_contact_name,
    get_meta_conversation_page,
    get_meta_message_page,
    link_conversation,
    mark_read,
    normalize_templates,
    open_conversation,
    page_count,
    process_webhook_payload,
    provider_for_channel,
    recover_operation,
    send_message,
)


router = APIRouter()
webhook_router = APIRouter()


def _oauth_operation_data(operation) -> MetaWhatsAppOperationData:
    return MetaWhatsAppOperationData(
        operation_id=operation.id,
        status=operation.status,
        reason_code=operation.reason_code,
        expires_at=operation.expires_at,
        used_at=operation.used_at,
        connection_mode=operation.connection_mode,
        waba_id=operation.discovered_waba_id,
        phone_number_id=operation.discovered_phone_number_id,
        sender_address=operation.discovered_sender_address,
        display_name=operation.discovered_display_name,
        subscription_succeeded=operation.subscription_succeeded,
        channel_id=operation.channel_id,
        confirmation_required=operation.status == "confirmation_required",
        credential_present=bool(operation.credential_ciphertext),
    )


def _oauth_error(exception: Exception) -> None:
    if isinstance(exception, MetaWhatsAppOAuthStateError):
        raise HTTPException(
            status_code=400,
            detail={"code": exception.code, "message": exception.message},
        ) from exception
    if isinstance(exception, MetaWhatsAppAssetConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": exception.code, "message": exception.message},
        ) from exception
    if isinstance(exception, MetaWhatsAppError):
        raise HTTPException(
            status_code=503 if exception.http_status is None else 400,
            detail={"code": exception.code, "message": exception.safe_message},
        ) from exception
    raise exception


@webhook_router.get("/api/integrations/meta/whatsapp/oauth/callback")
def meta_whatsapp_oauth_callback_landing() -> dict[str, object]:
    """Safe registration landing; the documented JS SDK returns code to JS."""
    return {
        "success": True,
        "message": (
            "Meta WhatsApp callback URI is ready; complete Embedded Signup "
            "through the authenticated code handoff endpoint"
        ),
        "data": {
            "callback_ready": True,
            "code_handoff_endpoint": (
                "/api/messages/meta/whatsapp/connect/complete"
            ),
        },
    }


@router.get(
    "/connect/readiness",
    response_model=MetaWhatsAppReadinessResponse,
)
def meta_whatsapp_connect_readiness(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_INTEGRATIONS_VIEW)),
    ],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
) -> MetaWhatsAppReadinessResponse:
    del context
    diagnostic = provider.oauth_configuration_diagnostic()
    return MetaWhatsAppReadinessResponse(
        data=MetaWhatsAppReadinessData(
            settings=[
                MetaWhatsAppSettingReadiness(
                    name=item.name,
                    present=item.present,
                    format_valid=item.format_valid,
                )
                for item in diagnostic.settings
            ],
            missing_settings=list(diagnostic.missing_settings),
            invalid_settings=list(diagnostic.invalid_settings),
            oauth_configuration_ready=diagnostic.ready,
        )
    )


@router.post(
    "/connect/initiate",
    response_model=MetaWhatsAppLaunchResponse,
    status_code=status.HTTP_201_CREATED,
)
def initiate_meta_whatsapp_connection(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_INTEGRATIONS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
) -> MetaWhatsAppLaunchResponse:
    try:
        result = begin_meta_whatsapp_oauth(
            database_session,
            context,
            provider,
            expire_minutes=settings.meta_whatsapp_oauth_state_expire_minutes,
        )
    except MetaWhatsAppError as exception:
        _oauth_error(exception)
    return MetaWhatsAppLaunchResponse(
        data=MetaWhatsAppLaunchData(
            operation_id=result.operation.id,
            state=result.state,
            expires_at=result.operation.expires_at,
            app_id=str(result.launch["app_id"]),
            configuration_id=str(result.launch["configuration_id"]),
            graph_api_version=str(result.launch["graph_api_version"]),
            extras=dict(result.launch["extras"]),
        )
    )


@router.post(
    "/connect/complete",
    response_model=MetaWhatsAppOperationResponse,
)
async def complete_meta_whatsapp_connection(
    payload: MetaWhatsAppCompleteRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_INTEGRATIONS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
) -> MetaWhatsAppOperationResponse:
    try:
        operation = await complete_meta_whatsapp_oauth(
            database_session, context, provider, payload
        )
    except (MetaWhatsAppOAuthStateError, MetaWhatsAppAssetConflictError, MetaWhatsAppError) as exception:
        database_session.rollback()
        _oauth_error(exception)
    return MetaWhatsAppOperationResponse(
        message="Meta WhatsApp onboarding completion processed",
        data=_oauth_operation_data(operation),
    )


@router.post(
    "/connect/cancel",
    response_model=MetaWhatsAppOperationResponse,
)
def cancel_meta_whatsapp_connection(
    payload: MetaWhatsAppCancelRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_INTEGRATIONS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MetaWhatsAppOperationResponse:
    try:
        operation = cancel_meta_whatsapp_oauth(
            database_session,
            context,
            operation_id=payload.operation_id,
            state=payload.state,
        )
    except MetaWhatsAppOAuthStateError as exception:
        database_session.rollback()
        _oauth_error(exception)
    return MetaWhatsAppOperationResponse(
        message="Meta WhatsApp onboarding cancelled",
        data=_oauth_operation_data(operation),
    )


@router.get(
    "/connect/operations/{operation_id}",
    response_model=MetaWhatsAppOperationResponse,
)
def meta_whatsapp_connection_status(
    operation_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_INTEGRATIONS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MetaWhatsAppOperationResponse:
    try:
        operation = get_meta_whatsapp_oauth_operation(
            database_session, context, operation_id
        )
    except MetaWhatsAppOAuthStateError as exception:
        _oauth_error(exception)
    return MetaWhatsAppOperationResponse(data=_oauth_operation_data(operation))


@router.post(
    "/connect/operations/{operation_id}/activate",
    response_model=MetaWhatsAppOperationResponse,
)
async def activate_meta_whatsapp_connection(
    operation_id: uuid.UUID,
    payload: MetaWhatsAppActivationRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_INTEGRATIONS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
) -> MetaWhatsAppOperationResponse:
    try:
        operation = await activate_meta_whatsapp_oauth(
            database_session,
            context,
            provider,
            operation_id=operation_id,
            confirm_replace_existing_sender=payload.confirm_replace_existing_sender,
        )
    except (MetaWhatsAppOAuthStateError, MetaWhatsAppAssetConflictError, MetaWhatsAppError) as exception:
        database_session.rollback()
        _oauth_error(exception)
    return MetaWhatsAppOperationResponse(
        message="Meta WhatsApp onboarding activation processed",
        data=_oauth_operation_data(operation),
    )


def _message_data(message: WhatsAppMessage) -> MetaMessageData:
    return MetaMessageData(
        id=message.id,
        conversation_id=message.conversation_id,
        direction=message.direction,
        mode=message.message_mode,
        purpose=message.purpose,
        body=message.body,
        media=[MetaMediaMetadata.model_validate(item) for item in message.media_metadata],
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
    unread_count: int,
) -> MetaConversationData:
    return MetaConversationData(
        id=conversation.id,
        external_address=conversation.external_address,
        lead_id=conversation.lead_id,
        customer_id=conversation.customer_id,
        contact_name=contact_name,
        last_message_at=conversation.last_message_at,
        last_message_preview=conversation.last_message_preview,
        unread_count=unread_count,
        eligibility=calculate_eligibility(database_session, conversation),
    )


def _not_found(exception: Exception) -> None:
    raise HTTPException(
        status_code=404, detail="Meta WhatsApp conversation not found"
    ) from exception


def _channel_unavailable(exception: Exception) -> None:
    raise HTTPException(
        status_code=503,
        detail={
            "code": "meta_whatsapp_channel_not_configured",
            "message": "The workspace Meta WhatsApp channel is not configured",
        },
    ) from exception


@router.get("/channel", response_model=MetaChannelResponse)
def get_meta_channel(
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
) -> MetaChannelResponse:
    try:
        channel = channel_for_context(database_session, context, provider)
    except MetaWhatsAppChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    return MetaChannelResponse(
        data=MetaChannelData(
            id=channel.id,
            waba_id=channel.provider_account_id or "",
            phone_number_id=channel.provider_channel_id or "",
            sender_address=channel.sender_address,
            active=channel.is_active,
            connection_mode=channel.connection_mode,
            credentials_configured=bool(channel.credential_ciphertext),
            subscription_status=channel.subscription_status,
        )
    )


@router.get("/operations/recover", response_model=MetaRecoveryResponse)
def recover_meta_operation(
    idempotency_key: Annotated[
        str, Query(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    ],
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MetaRecoveryResponse:
    try:
        result = recover_operation(database_session, context, idempotency_key)
    except MetaWhatsAppNotFoundError as exception:
        _not_found(exception)
    return MetaRecoveryResponse(
        data=MetaRecoveryData(
            idempotency_key=idempotency_key,
            found=result.message is not None,
            operation_state=result.operation_state,
            message=_message_data(result.message) if result.message else None,
        )
    )


@router.get("/conversations", response_model=MetaConversationPageResponse)
def list_meta_conversations(
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> MetaConversationPageResponse:
    result = get_meta_conversation_page(
        database_session, context, search=search, page=page, size=size
    )
    return MetaConversationPageResponse(
        data=MetaConversationPageData(
            items=[
                _conversation_data(
                    database_session,
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


@router.post("/conversations/open", response_model=MetaConversationResponse)
def open_meta_conversation(
    payload: MetaOpenConversationRequest,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
) -> MetaConversationResponse:
    try:
        conversation = open_conversation(
            database_session, context, provider, payload.phone_number
        )
    except MetaWhatsAppChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    except MetaWhatsAppEligibilityError as exception:
        raise HTTPException(
            status_code=422,
            detail={"code": exception.code, "message": exception.message},
        ) from exception
    return MetaConversationResponse(
        data=_conversation_data(
            database_session,
            conversation,
            contact_name=conversation_contact_name(database_session, conversation),
            unread_count=0,
        )
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MetaMessagePageResponse,
)
def list_meta_messages(
    conversation_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MetaMessagePageResponse:
    try:
        conversation, result = get_meta_message_page(
            database_session, context, conversation_id, page=page, size=size
        )
    except MetaWhatsAppNotFoundError as exception:
        _not_found(exception)
    return MetaMessagePageResponse(
        data=MetaMessagePageData(
            conversation=_conversation_data(
                database_session,
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


@router.post("/send", response_model=MetaSendResponse)
async def send_meta_message(
    payload: MetaSendRequest,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
) -> MetaSendResponse:
    try:
        result = await send_message(
            database_session, context, payload, provider
        )
    except MetaWhatsAppNotFoundError as exception:
        _not_found(exception)
    except MetaWhatsAppChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    except MetaWhatsAppIdempotencyConflictError as exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "message": "The idempotency key was already used for a different request",
            },
        ) from exception
    except MetaWhatsAppEligibilityError as exception:
        detail: dict[str, object] = {
            "code": exception.code,
            "message": exception.message,
        }
        detail.update(exception.details)
        raise HTTPException(
            status_code=429 if exception.code == "rate_limit_exceeded" else 409,
            detail=detail,
        ) from exception
    except MetaWhatsAppError as exception:
        raise HTTPException(
            status_code=503,
            detail={"code": exception.code, "message": exception.safe_message},
        ) from exception
    state = (
        "in_progress"
        if result.message.submission_state == "processing"
        else "completed"
    )
    return MetaSendResponse(
        data=MetaSendData(
            message=_message_data(result.message),
            replayed=result.replayed,
            operation_state=state,
        )
    )


@router.post(
    "/conversations/{conversation_id}/read", response_model=MetaReadResponse
)
def mark_meta_conversation_read(
    conversation_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_VIEW))
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MetaReadResponse:
    try:
        cursor = mark_read(database_session, context, conversation_id)
    except MetaWhatsAppNotFoundError as exception:
        _not_found(exception)
    return MetaReadResponse(
        data=MetaReadData(
            conversation_id=conversation_id,
            last_read_at=cursor.last_read_at,
        )
    )


@router.put(
    "/conversations/{conversation_id}/link",
    response_model=MetaConversationResponse,
)
def link_meta_conversation(
    conversation_id: uuid.UUID,
    payload: MetaConversationLinkRequest,
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MetaConversationResponse:
    try:
        conversation = link_conversation(
            database_session, context, conversation_id, payload
        )
    except MetaWhatsAppNotFoundError as exception:
        _not_found(exception)
    except MetaWhatsAppLinkTargetError as exception:
        raise HTTPException(
            status_code=404,
            detail="The link target is unavailable or outside the authorized scope",
        ) from exception
    return MetaConversationResponse(
        message="Meta WhatsApp conversation linked",
        data=_conversation_data(
            database_session,
            conversation,
            contact_name=conversation_contact_name(database_session, conversation),
            unread_count=0,
        ),
    )


@router.get("/templates", response_model=MetaTemplatePageResponse)
async def list_meta_templates(
    context: Annotated[
        WorkspaceContext, Depends(require_permission(PermissionKey.MESSAGES_SEND))
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> MetaTemplatePageResponse:
    try:
        channel = channel_for_context(database_session, context, provider)
        selected_provider = provider_for_channel(provider, channel)
        templates = normalize_templates(
            await selected_provider.list_templates(
                limit=settings.meta_whatsapp_template_fetch_limit
            )
        )
    except MetaWhatsAppChannelNotConfiguredError as exception:
        _channel_unavailable(exception)
    except MetaWhatsAppError as exception:
        raise HTTPException(
            status_code=503,
            detail={"code": exception.code, "message": exception.safe_message},
        ) from exception
    normalized_search = " ".join((search or "").strip().split()).casefold()
    if normalized_search:
        templates = [
            item
            for item in templates
            if normalized_search in item.name.casefold()
            or normalized_search in item.language.casefold()
        ]
    total = len(templates)
    items = templates[(page - 1) * size : page * size]
    return MetaTemplatePageResponse(
        data=MetaTemplatePageData(
            items=items,
            page=page,
            size=size,
            total=total,
            pages=page_count(total, size),
        )
    )


@webhook_router.get("/api/webhooks/meta/whatsapp")
def verify_meta_webhook(
    request: Request,
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
    mode: Annotated[str, Query(alias="hub.mode")],
    verify_token: Annotated[str, Query(alias="hub.verify_token")],
    challenge: Annotated[str, Query(alias="hub.challenge")],
) -> Response:
    if not provider.webhook_configured:
        raise HTTPException(status_code=503, detail="Meta webhook is not configured")
    supplied_token = getattr(
        request.state, "meta_webhook_verify_token", verify_token
    )
    if not provider.validate_challenge(mode=mode, verify_token=supplied_token):
        raise HTTPException(status_code=403, detail="Invalid Meta webhook verification")
    return Response(content=challenge, media_type="text/plain")


@webhook_router.post("/api/webhooks/meta/whatsapp")
async def receive_meta_webhook(
    request: Request,
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[MetaWhatsAppProvider, Depends(get_meta_whatsapp_provider)],
) -> Response:
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not provider.validate_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Invalid Meta webhook signature")
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError) as exception:
        raise HTTPException(status_code=400, detail="Invalid Meta webhook JSON") from exception
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Meta webhook JSON")
    try:
        process_webhook_payload(database_session, provider, payload)
    except MetaWhatsAppWebhookRejectedError as exception:
        raise HTTPException(status_code=403, detail="Unknown Meta WhatsApp channel") from exception
    return Response(status_code=status.HTTP_200_OK)
