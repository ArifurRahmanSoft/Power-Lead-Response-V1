import uuid
from datetime import timezone

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.security import WorkspaceContext
from app.models.lead_activity import LeadActivity
from app.repositories.lead_activity_repository import (
    LeadActivityPage,
    active_tenant_user_exists,
    get_activity,
    list_activities,
)
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.lead_activity import (
    LeadActivityAmendRequest,
    LeadActivityChannel,
    LeadActivityCreateRequest,
    LeadActivityValues,
)
from app.services.lead_service import get_lead_for_context
from app.repositories.outbound_message_repository import get_message, list_events


class LeadActivityNotFoundError(Exception):
    pass


class LeadActivityReferenceError(Exception):
    pass


class LeadActivityValidationError(Exception):
    pass


def _validate_performer(
    database_session: Session,
    context: WorkspaceContext,
    performed_by: uuid.UUID | None,
) -> None:
    if performed_by is None:
        return
    if not active_tenant_user_exists(
        database_session,
        tenant_id=context.tenant.id,
        user_id=performed_by,
    ):
        raise LeadActivityReferenceError("performed_by must be an active workspace member")


def get_lead_activity_timeline(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> LeadActivityPage:
    get_lead_for_context(database_session, context, lead_id)
    result = list_activities(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        page=page,
        size=size,
    )
    for activity in result.items:
        if activity.outbound_message_id is None:
            continue
        message = get_message(database_session, context.tenant.id, activity.outbound_message_id)
        if message is None:
            continue
        activity.message_status = message.status
        activity.message_events = [
            {
                "status": event.status,
                "safe_reason": event.safe_reason,
                "occurred_at": event.occurred_at,
            }
            for event in list_events(database_session, message.id)
        ]
    return result


def create_manual_lead_activity(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    payload: LeadActivityCreateRequest,
    *,
    commit: bool = True,
) -> LeadActivity:
    get_lead_for_context(database_session, context, lead_id)
    _validate_performer(database_session, context, payload.performed_by)
    if payload.related_activity_id is not None:
        related = get_activity(
            database_session,
            tenant_id=context.tenant.id,
            lead_id=lead_id,
            activity_id=payload.related_activity_id,
        )
        if related is None:
            raise LeadActivityReferenceError(
                "related_activity_id must reference an activity for this lead"
            )

    activity = LeadActivity(
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        **payload.model_dump(exclude={"related_activity_id"}, mode="python"),
        related_activity_id=payload.related_activity_id,
        recorded_by=context.user.id,
        is_system=False,
        is_amendment=False,
        externally_performed=(
            payload.activity_type.value == "inbound_reply"
            or payload.channel == LeadActivityChannel.EMAIL
        ),
        provider_verified=False,
    )
    database_session.add(activity)
    database_session.flush()
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.activity.created",
        target_membership_id=context.membership.id,
        details={
            "lead_id": str(lead_id),
            "activity_id": str(activity.id),
            "activity_type": activity.activity_type,
            "channel": activity.channel,
        },
    )
    if commit:
        database_session.commit()
        database_session.refresh(activity)
    return activity


def amend_manual_lead_activity(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    activity_id: uuid.UUID,
    payload: LeadActivityAmendRequest,
) -> LeadActivity:
    get_lead_for_context(database_session, context, lead_id)
    original = get_activity(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        activity_id=activity_id,
    )
    if original is None:
        raise LeadActivityNotFoundError
    if original.is_system:
        raise LeadActivityReferenceError("System activities cannot be amended")

    changes = payload.model_dump(
        exclude_unset=True,
        exclude={"amendment_reason"},
        mode="python",
    )
    values = {
        "activity_type": original.activity_type,
        "channel": original.channel,
        "direction": original.direction,
        "occurred_at": (
            original.occurred_at.replace(tzinfo=timezone.utc)
            if original.occurred_at.tzinfo is None
            else original.occurred_at.astimezone(timezone.utc)
        ),
        "performed_by": original.performed_by,
        "outcome": original.outcome,
        "notes": original.notes,
    }
    values.update(changes)
    try:
        validated = LeadActivityValues.model_validate(values)
    except ValidationError as exception:
        raise LeadActivityValidationError(str(exception)) from exception
    _validate_performer(database_session, context, validated.performed_by)

    amendment = LeadActivity(
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        **validated.model_dump(mode="python"),
        related_activity_id=original.id,
        recorded_by=context.user.id,
        is_system=False,
        is_amendment=True,
        externally_performed=validated.channel == LeadActivityChannel.EMAIL,
        provider_verified=False,
        amendment_reason=payload.amendment_reason,
    )
    database_session.add(amendment)
    database_session.flush()
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.activity.amended",
        target_membership_id=context.membership.id,
        details={
            "lead_id": str(lead_id),
            "activity_id": str(amendment.id),
            "amends_activity_id": str(original.id),
            "changed_fields": sorted(changes),
        },
    )
    database_session.commit()
    database_session.refresh(amendment)
    return amendment
