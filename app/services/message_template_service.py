import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import WorkspaceContext
from app.models.message_template import MessageTemplate
from app.repositories.message_template_repository import (
    MessageTemplatePage,
    get_message_template,
    list_message_templates,
    message_template_name_exists,
    normalize_message_template_name,
)
from app.repositories.workspace_repository import add_authorization_audit, lock_tenant
from app.schemas.message_template import (
    MessageTemplateCreateRequest,
    MessageTemplatePreviewRequest,
    MessageTemplateUpdateRequest,
)
from app.services.lead_service import (
    LeadNotFoundError,
    LeadPermissionError,
    LeadScopeError,
    get_lead_for_context,
)
from app.utils.mail_template import (
    PLACEHOLDER_FIELDS,
    UnknownPlaceholderError,
    lead_placeholder_values,
    render_text_template,
    validate_placeholders,
)


class MessageTemplateNotFoundError(Exception):
    pass


class DuplicateMessageTemplateNameError(Exception):
    pass


class MessageTemplateLeadNotFoundError(Exception):
    pass


def _audit(
    database_session: Session,
    context: WorkspaceContext,
    action: str,
    template_id: uuid.UUID,
    details: dict[str, object] | None = None,
) -> None:
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action=action,
        target_membership_id=None,
        details={
            "message_template_id": str(template_id),
            **(details or {}),
        },
    )


def get_message_templates(
    database_session: Session,
    context: WorkspaceContext,
    *,
    page: int,
    size: int,
    search: str | None,
    include_archived: bool,
) -> MessageTemplatePage:
    return list_message_templates(
        database_session,
        context.tenant.id,
        page=page,
        size=size,
        search=search,
        include_archived=include_archived,
    )


def get_message_template_record(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    *,
    include_archived: bool = False,
) -> MessageTemplate:
    template = get_message_template(
        database_session,
        context.tenant.id,
        template_id,
        include_archived=include_archived,
    )
    if template is None:
        raise MessageTemplateNotFoundError
    return template


def create_message_template(
    database_session: Session,
    context: WorkspaceContext,
    payload: MessageTemplateCreateRequest,
) -> MessageTemplate:
    validate_placeholders(payload.body)
    lock_tenant(database_session, context.tenant.id)
    normalized_name = normalize_message_template_name(payload.name)
    if message_template_name_exists(
        database_session, context.tenant.id, normalized_name
    ):
        database_session.rollback()
        raise DuplicateMessageTemplateNameError
    template = MessageTemplate(
        tenant_id=context.tenant.id,
        name=payload.name,
        normalized_name=normalized_name,
        body=payload.body,
        created_by_user_id=context.user.id,
        updated_by_user_id=context.user.id,
    )
    database_session.add(template)
    database_session.flush()
    _audit(database_session, context, "message_template.created", template.id)
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateMessageTemplateNameError from exception
    database_session.refresh(template)
    return template


def update_message_template(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    payload: MessageTemplateUpdateRequest,
) -> MessageTemplate:
    if payload.body is not None:
        validate_placeholders(payload.body)
    lock_tenant(database_session, context.tenant.id)
    template = get_message_template(
        database_session,
        context.tenant.id,
        template_id,
        for_update=True,
    )
    if template is None:
        database_session.rollback()
        raise MessageTemplateNotFoundError

    changed_fields: list[str] = []
    if payload.name is not None and payload.name != template.name:
        normalized_name = normalize_message_template_name(payload.name)
        if message_template_name_exists(
            database_session,
            context.tenant.id,
            normalized_name,
            exclude_id=template.id,
        ):
            database_session.rollback()
            raise DuplicateMessageTemplateNameError
        template.name = payload.name
        template.normalized_name = normalized_name
        changed_fields.append("name")
    if payload.body is not None and payload.body != template.body:
        template.body = payload.body
        changed_fields.append("body")
    template.updated_by_user_id = context.user.id
    _audit(
        database_session,
        context,
        "message_template.updated",
        template.id,
        {"changed_fields": changed_fields},
    )
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateMessageTemplateNameError from exception
    database_session.refresh(template)
    return template


def archive_message_template(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
) -> None:
    lock_tenant(database_session, context.tenant.id)
    template = get_message_template(
        database_session,
        context.tenant.id,
        template_id,
        for_update=True,
    )
    if template is None:
        database_session.rollback()
        raise MessageTemplateNotFoundError
    template.archived_at = datetime.now(timezone.utc)
    template.archived_by_user_id = context.user.id
    template.updated_by_user_id = context.user.id
    _audit(database_session, context, "message_template.archived", template.id)
    database_session.commit()


def preview_message_template(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    payload: MessageTemplatePreviewRequest,
) -> dict[str, object]:
    template = get_message_template_record(
        database_session, context, template_id
    )
    validate_placeholders(template.body)
    values: dict[str, object] = {}
    warnings: list[str] = []
    if payload.lead_id is not None:
        try:
            lead = get_lead_for_context(
                database_session, context, payload.lead_id
            )
        except (LeadNotFoundError, LeadPermissionError, LeadScopeError) as exception:
            raise MessageTemplateLeadNotFoundError from exception
        values = lead_placeholder_values(lead)
    elif payload.sample_values is not None:
        unknown = set(payload.sample_values) - PLACEHOLDER_FIELDS.keys()
        if unknown:
            raise UnknownPlaceholderError(unknown)
        values = dict(payload.sample_values)
    else:
        warnings.append(
            "No lead or sample values were supplied; placeholders remain visible."
        )
    rendered_body, missing = render_text_template(
        template.body, values, escape_html=False
    )
    return {
        "template": template,
        "rendered_body": rendered_body,
        "missing": sorted(missing),
        "warnings": warnings,
    }
