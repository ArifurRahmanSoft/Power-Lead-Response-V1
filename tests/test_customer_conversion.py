from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead, LeadCustomerAction, LeadEvent
from app.models.lead_activity import LeadActivity
from app.models.lead_booking import LeadAutomationStopEvent
from app.models.lead_task import LeadTask
from app.repositories.lead_repository import list_authorized_lead_ids
from app.schemas.lead import LeadFilterParams
from tests.test_leads import (
    add_membership,
    create_tenant,
    create_user,
    lead_payload,
    login_headers,
    owner_setup,
    seed_access,
)


def create_engaged_lead(
    client: TestClient,
    headers: dict[str, str],
    email: str,
    **overrides: object,
) -> dict:
    created = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload(email, **overrides),
    )
    assert created.status_code == 201, created.text
    lead = created.json()["data"]
    contacted = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={"row_version": lead["row_version"], "status": "contacted"},
    )
    assert contacted.status_code == 200, contacted.text
    lead = contacted.json()["data"]
    engaged = client.post(
        f"/api/leads/{lead['id']}/engage",
        headers=headers,
        json={
            "row_version": lead["row_version"],
            "reason": "Prospect demonstrated explicit purchase interest",
        },
    )
    assert engaged.status_code == 200, engaged.text
    return engaged.json()["data"]


def conversion_payload(lead: dict, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "row_version": lead["row_version"],
        "purchased_service": "Website Design",
        "reference": "ORDER-1042",
        "reason": "Signed service agreement",
        "duplicate_resolution": "create_new",
        "pending_acquisition_task_disposition": "cancel",
        "pending_task_reason": "Acquisition completed after conversion",
    }
    payload.update(overrides)
    return payload


def create_lead_at_status(
    client: TestClient,
    headers: dict[str, str],
    email: str,
    status: str,
) -> dict:
    created = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload(email),
    )
    assert created.status_code == 201, created.text
    lead = created.json()["data"]
    if status == "new":
        return lead
    target = "contacted" if status == "engaged" else status
    transitioned = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={"row_version": lead["row_version"], "status": target},
    )
    assert transitioned.status_code == 200, transitioned.text
    lead = transitioned.json()["data"]
    if status != "engaged":
        return lead
    engaged = client.post(
        f"/api/leads/{lead['id']}/engage",
        headers=headers,
        json={
            "row_version": lead["row_version"],
            "reason": "Prospect is engaged",
        },
    )
    assert engaged.status_code == 200, engaged.text
    return engaged.json()["data"]


@pytest.mark.parametrize("status", ["new", "qualified", "contacted", "engaged"])
def test_minimal_direct_conversion_from_each_active_status(
    client: TestClient,
    database_session: Session,
    status: str,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_lead_at_status(
        client,
        headers,
        f"direct-{status}@example.com",
        status,
    )
    assert lead["can_convert_to_customer"] is True
    assert lead["conversion_blocking_reason"] is None
    task_id: uuid.UUID | None = None
    if status == "new":
        task_response = client.post(
            f"/api/leads/{lead['id']}/tasks",
            headers=headers,
            json={
                "assigned_user_id": str(owner.id),
                "task_type": "follow_up",
                "title": "Pending acquisition follow-up",
                "due_at": (
                    datetime.now(timezone.utc) + timedelta(days=1)
                ).isoformat(),
                "priority": "normal",
            },
        )
        assert task_response.status_code == 201, task_response.text
        task_id = uuid.UUID(task_response.json()["data"]["id"])

    response = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json={"row_version": lead["row_version"]},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["idempotent_replay"] is False
    assert data["customer"]["source_type"] == "lead"
    assert data["customer"]["source_lead_id"] == lead["id"]
    assert data["conversion"]["purchased_service"] is None
    assert data["conversion"]["reference"] is None
    assert data["conversion"]["reason"] is None
    assert data["conversion"]["duplicate_resolution"] == "create_new"
    assert data["conversion"]["pending_task_disposition"] == "cancel"
    assert data["conversion"]["pending_task_reason"] == (
        "Lead converted to customer"
    )
    if task_id is not None:
        database_session.expire_all()
        task = database_session.get(LeadTask, task_id)
        assert task is not None
        assert task.status == "cancelled"
        assert task.cancellation_reason == "Lead converted to customer"


def test_direct_conversion_rejects_disqualified_archived_unauthorized_and_cross_tenant(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, _, headers = owner_setup(client, database_session)
    disqualified = create_lead_at_status(
        client,
        headers,
        "direct-disqualified@example.com",
        "disqualified",
    )
    assert disqualified["can_convert_to_customer"] is False
    assert disqualified["conversion_blocking_reason"] == "lead_disqualified"
    rejected = client.post(
        f"/api/leads/{disqualified['id']}/convert",
        headers=headers,
        json={"row_version": disqualified["row_version"]},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "Disqualified leads cannot be converted"

    archived = create_lead_at_status(
        client,
        headers,
        "direct-archived@example.com",
        "new",
    )
    archived_response = client.delete(
        f"/api/leads/{archived['id']}",
        headers=headers,
        params={"row_version": archived["row_version"]},
    )
    assert archived_response.status_code == 200
    rejected = client.post(
        f"/api/leads/{archived['id']}/convert",
        headers=headers,
        json={"row_version": archived["row_version"] + 1},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "Archived leads cannot be converted"

    staff = create_user(database_session, "direct.conversion.staff")
    add_membership(database_session, staff, tenant, roles[RoleKey.STAFF])
    unauthorized = create_lead_at_status(
        client,
        headers,
        "direct-unassigned@example.com",
        "new",
    )
    rejected = client.post(
        f"/api/leads/{unauthorized['id']}/convert",
        headers=login_headers(client, staff),
        json={"row_version": unauthorized["row_version"]},
    )
    assert rejected.status_code == 403

    foreign_tenant = create_tenant(database_session, "direct-conversion-foreign")
    foreign_owner = create_user(database_session, "direct.conversion.foreign")
    add_membership(
        database_session,
        foreign_owner,
        foreign_tenant,
        roles[RoleKey.OWNER],
    )
    rejected = client.post(
        f"/api/leads/{unauthorized['id']}/convert",
        headers=login_headers(client, foreign_owner),
        json={"row_version": unauthorized["row_version"]},
    )
    assert rejected.status_code == 404


def test_conversion_is_transactional_idempotent_and_preserves_history_and_consent(
    client: TestClient,
    database_session: Session,
) -> None:
    _, tenant, owner, headers = owner_setup(client, database_session)
    lead = create_engaged_lead(
        client,
        headers,
        "converted.customer@example.com",
        assigned_user_id=str(owner.id),
        whatsapp_number="+8801712345678",
        consent_status="granted",
        consent_evidence="Prospect opted in on the acquisition form",
    )
    task_response = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=headers,
        json={
            "assigned_user_id": str(owner.id),
            "task_type": "follow_up",
            "title": "Acquisition follow-up",
            "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "priority": "normal",
        },
    )
    assert task_response.status_code == 201
    task_id = uuid.UUID(task_response.json()["data"]["id"])
    activity_response = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json={
            "activity_type": "note",
            "channel": "other",
            "direction": "internal",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "performed_by": str(owner.id),
            "notes": "Original activity remains linked to the lead.",
        },
    )
    assert activity_response.status_code == 201
    activity_id = uuid.UUID(activity_response.json()["data"]["id"])
    pending_job = LeadCustomerAction(
        tenant_id=tenant.id,
        lead_id=uuid.UUID(lead["id"]),
        action_type="nurture_email",
        status="pending",
    )
    database_session.add(pending_job)
    database_session.commit()

    converted = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json=conversion_payload(lead),
    )
    assert converted.status_code == 200, converted.text
    body = converted.json()
    assert body["data"]["idempotent_replay"] is False
    customer = body["data"]["customer"]
    conversion = body["data"]["conversion"]
    assert customer["communication_purpose"] == "customer_relationship"
    assert customer["communication_consent_status"] == "unknown"
    assert customer["channel_restrictions"] == []
    assert customer["whatsapp_number"] == "+8801712345678"
    assert customer["source_type"] == "lead"
    assert customer["source_lead_id"] == lead["id"]
    assert customer["source_lead_label"] == "Jane Doe"
    assert customer["converted_at"] == conversion["converted_at"]
    assert conversion["source_consent_status"] == "granted"
    assert conversion["source_consent_evidence"] == (
        "Prospect opted in on the acquisition form"
    )
    assert conversion["cancelled_nurture_job_count"] == 1
    assert conversion["affected_task_count"] == 1

    database_session.expire_all()
    stored_lead = database_session.get(Lead, uuid.UUID(lead["id"]))
    assert stored_lead is not None
    assert stored_lead.status == "converted"
    stored_task = database_session.get(LeadTask, task_id)
    assert stored_task is not None
    assert stored_task.status == "cancelled"
    assert stored_task.cancellation_reason == "Acquisition completed after conversion"
    stored_job = database_session.get(LeadCustomerAction, pending_job.id)
    assert stored_job is not None
    assert stored_job.status == "cancelled"
    assert stored_job.cancelled_by == owner.id
    assert stored_job.cancellation_reason == "Lead converted to customer"
    assert database_session.get(LeadActivity, activity_id) is not None
    conversion_activities = list(
        database_session.scalars(
            select(LeadActivity).where(
                LeadActivity.lead_id == uuid.UUID(lead["id"]),
                LeadActivity.is_system.is_(True),
                LeadActivity.outcome == "converted_to_customer",
            )
        ).all()
    )
    assert len(conversion_activities) == 1
    stop_keys = set(
        database_session.scalars(
            select(LeadAutomationStopEvent.stop_key).where(
                LeadAutomationStopEvent.lead_id == uuid.UUID(lead["id"])
            )
        ).all()
    )
    assert stop_keys == {"lead_nurturing", "recovery_follow_up"}

    replay = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json=conversion_payload(lead),
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["idempotent_replay"] is True
    assert replay.json()["data"]["conversion"]["id"] == conversion["id"]
    assert database_session.scalar(select(func.count(Customer.id))) == 1
    assert database_session.scalar(select(func.count(LeadConversion.id))) == 1
    assert database_session.scalar(
        select(func.count(LeadActivity.id)).where(
            LeadActivity.lead_id == uuid.UUID(lead["id"]),
            LeadActivity.is_system.is_(True),
            LeadActivity.outcome == "converted_to_customer",
        )
    ) == 1

    active_leads = client.get("/api/leads", headers=headers)
    assert active_leads.status_code == 200
    assert active_leads.json()["data"]["total"] == 0
    historical_lead = client.get(f"/api/leads/{lead['id']}", headers=headers)
    assert historical_lead.status_code == 200
    assert historical_lead.json()["data"]["can_convert_to_customer"] is False
    assert historical_lead.json()["data"]["conversion_blocking_reason"] == (
        "already_converted"
    )
    selected_ids, selected_total = list_authorized_lead_ids(
        database_session,
        tenant_id=tenant.id,
        filters=LeadFilterParams(),
        assigned_scope_user_id=None,
        limit=100,
    )
    assert selected_total == 0
    assert selected_ids == []

    listing = client.get("/api/customers?search=converted.customer", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1
    listed_customer = listing.json()["data"]["items"][0]
    assert listed_customer["source_type"] == "lead"
    assert listed_customer["source_lead_id"] == lead["id"]
    assert listed_customer["source_lead_label"] == "Jane Doe"
    assert listed_customer["converted_at"] == conversion["converted_at"]
    detail = client.get(f"/api/customers/{customer['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["source_type"] == "lead"
    assert detail.json()["data"]["conversions"][0]["lead_id"] == lead["id"]


def test_duplicate_rule_requires_explicit_link_and_blocks_cross_tenant_link(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, owner, headers = owner_setup(client, database_session)
    existing = Customer(
        tenant_id=tenant.id,
        first_name="Existing",
        last_name="Customer",
        email="duplicate.customer@example.com",
        created_by=owner.id,
    )
    database_session.add(existing)
    database_session.commit()
    legacy_detail = client.get(f"/api/customers/{existing.id}", headers=headers)
    assert legacy_detail.status_code == 200
    assert legacy_detail.json()["data"]["source_type"] is None
    assert legacy_detail.json()["data"]["source_lead_id"] is None
    assert legacy_detail.json()["data"]["converted_at"] is None
    lead = create_engaged_lead(
        client,
        headers,
        "duplicate.customer@example.com",
    )

    duplicate = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json=conversion_payload(lead),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["existing_customer_id"] == str(existing.id)

    other_tenant = create_tenant(database_session, "conversion-other-tenant")
    other_owner = create_user(database_session, "conversion.other.owner")
    add_membership(database_session, other_owner, other_tenant, roles[RoleKey.OWNER])
    foreign_customer = Customer(
        tenant_id=other_tenant.id,
        first_name="Foreign",
        last_name="Customer",
        email="foreign.customer@example.com",
        created_by=other_owner.id,
    )
    database_session.add(foreign_customer)
    database_session.commit()
    cross_tenant = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json=conversion_payload(
            lead,
            duplicate_resolution="link_existing",
            existing_customer_id=str(foreign_customer.id),
        ),
    )
    assert cross_tenant.status_code == 404

    linked = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json=conversion_payload(
            lead,
            duplicate_resolution="link_existing",
            existing_customer_id=str(existing.id),
        ),
    )
    assert linked.status_code == 200
    assert linked.json()["data"]["customer"]["id"] == str(existing.id)


def test_conversion_requires_explicit_endpoint_version_and_task_disposition(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_engaged_lead(
        client,
        headers,
        "retain.tasks@example.com",
        assigned_user_id=str(owner.id),
    )
    generic = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={"row_version": lead["row_version"], "status": "converted"},
    )
    assert generic.status_code == 400
    assert generic.json()["detail"] == "Use the explicit lead conversion endpoint"

    task_response = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=headers,
        json={
            "assigned_user_id": str(owner.id),
            "task_type": "contact",
            "title": "Intentional human contact",
            "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "priority": "high",
        },
    )
    task = task_response.json()["data"]
    stale = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json={
            **conversion_payload(
                lead,
                pending_acquisition_task_disposition="retain",
                pending_task_reason="Human follow-up remains intentional",
            ),
            "row_version": lead["row_version"] - 1,
        },
    )
    assert stale.status_code == 409

    retained = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json=conversion_payload(
            lead,
            pending_acquisition_task_disposition="retain",
            pending_task_reason="Human follow-up remains intentional",
        ),
    )
    assert retained.status_code == 200
    assert retained.json()["data"]["conversion"]["affected_task_count"] == 1
    database_session.expire_all()
    task_row = database_session.get(LeadTask, uuid.UUID(task["id"]))
    assert task_row is not None
    assert task_row.status == "pending"


def test_customer_permissions_and_staff_scope(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_access(database_session)
    tenant = create_tenant(database_session, "customer-scope")
    owner = create_user(database_session, "customer.scope.owner")
    staff = create_user(database_session, "customer.scope.staff")
    add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    add_membership(database_session, staff, tenant, roles[RoleKey.STAFF])
    owner_headers = login_headers(client, owner)
    staff_headers = login_headers(client, staff)
    assigned_created = client.post(
        "/api/leads",
        headers=owner_headers,
        json=lead_payload(
            "staff.visible.customer@example.com",
            assigned_user_id=str(staff.id),
        ),
    )
    assert assigned_created.status_code == 201
    assigned_lead = assigned_created.json()["data"]
    assigned_contacted = client.patch(
        f"/api/leads/{assigned_lead['id']}",
        headers=owner_headers,
        json={"row_version": assigned_lead["row_version"], "status": "contacted"},
    )
    assert assigned_contacted.status_code == 200
    assigned_lead = assigned_contacted.json()["data"]
    hidden_lead = create_engaged_lead(
        client,
        owner_headers,
        "staff.hidden.customer@example.com",
    )
    staff_capability = client.get(
        f"/api/leads/{assigned_lead['id']}", headers=staff_headers
    )
    assert staff_capability.status_code == 200
    assert staff_capability.json()["data"]["can_mark_engaged"] is True
    assert staff_capability.json()["data"]["engagement_blocking_reason"] is None
    staff_engaged = client.post(
        f"/api/leads/{assigned_lead['id']}/engage",
        headers=staff_headers,
        json={
            "row_version": assigned_lead["row_version"],
            "reason": "Assigned staff confirmed active buying interest",
        },
    )
    assert staff_engaged.status_code == 200, staff_engaged.text
    assigned_lead = staff_engaged.json()["data"]
    assert assigned_lead["can_convert_to_customer"] is True

    assigned_conversion = client.post(
        f"/api/leads/{assigned_lead['id']}/convert",
        headers=staff_headers,
        json=conversion_payload(assigned_lead),
    )
    assert assigned_conversion.status_code == 200, assigned_conversion.text
    assigned_customer = assigned_conversion.json()["data"]["customer"]
    hidden_customer = client.post(
        f"/api/leads/{hidden_lead['id']}/convert",
        headers=owner_headers,
        json=conversion_payload(hidden_lead),
    ).json()["data"]["customer"]

    forbidden_conversion = client.post(
        f"/api/leads/{hidden_lead['id']}/convert",
        headers=staff_headers,
        json=conversion_payload(hidden_lead),
    )
    assert forbidden_conversion.status_code == 403
    forbidden_engagement = client.post(
        f"/api/leads/{hidden_lead['id']}/engage",
        headers=staff_headers,
        json={
            "row_version": hidden_lead["row_version"],
            "reason": "Should not be authorized",
        },
    )
    assert forbidden_engagement.status_code == 403
    listing = client.get("/api/customers", headers=staff_headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1
    assert listing.json()["data"]["items"][0]["id"] == assigned_customer["id"]
    assert (
        client.get(
            f"/api/customers/{hidden_customer['id']}", headers=staff_headers
        ).status_code
        == 404
    )


def test_explicit_engagement_transition_capability_audit_and_guards(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, _, owner, headers = owner_setup(client, database_session)
    lead_response = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload("explicit-engagement@example.com"),
    )
    assert lead_response.status_code == 201
    lead = lead_response.json()["data"]
    assert lead["status"] == "new"
    assert lead["can_mark_engaged"] is False
    assert lead["engagement_blocking_reason"] == "lead_must_be_contacted"

    invalid_state = client.post(
        f"/api/leads/{lead['id']}/engage",
        headers=headers,
        json={"row_version": lead["row_version"], "reason": "Too early"},
    )
    assert invalid_state.status_code == 409
    assert invalid_state.json()["detail"] == (
        "Only a contacted lead can be marked engaged"
    )

    contacted_response = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={"row_version": lead["row_version"], "status": "contacted"},
    )
    assert contacted_response.status_code == 200
    contacted = contacted_response.json()["data"]
    assert contacted["can_mark_engaged"] is True
    assert contacted["engagement_blocking_reason"] is None

    stale = client.post(
        f"/api/leads/{lead['id']}/engage",
        headers=headers,
        json={
            "row_version": contacted["row_version"] - 1,
            "reason": "Stale browser request",
        },
    )
    assert stale.status_code == 409

    engaged_response = client.post(
        f"/api/leads/{lead['id']}/engage",
        headers=headers,
        json={
            "row_version": contacted["row_version"],
            "reason": "  Prospect confirmed an active purchasing discussion.  ",
        },
    )
    assert engaged_response.status_code == 200, engaged_response.text
    engaged = engaged_response.json()["data"]
    assert engaged["status"] == "engaged"
    assert engaged["can_mark_engaged"] is False
    assert engaged["engagement_blocking_reason"] == "already_engaged"
    assert engaged["can_convert_to_customer"] is True
    status_events = database_session.scalars(
        select(LeadEvent).where(
            LeadEvent.lead_id == uuid.UUID(lead["id"]),
            LeadEvent.event_type == "lead.status_changed",
        )
    ).all()
    event = next(
        item
        for item in status_events
        if item.details.get("transition_source") == "explicit_engagement"
    )
    assert event.actor_user_id == owner.id
    assert event.details["previous"] == "contacted"
    assert event.details["current"] == "engaged"
    assert event.details["reason"] == (
        "Prospect confirmed an active purchasing discussion."
    )
    assert event.details["changed_by"] == str(owner.id)
    assert event.details["changed_at"]
    assert event.details["transition_source"] == "explicit_engagement"
    assert database_session.scalar(
        select(func.count(LeadActivity.id)).where(
            LeadActivity.lead_id == uuid.UUID(lead["id"])
        )
    ) == 0

    repeat = client.post(
        f"/api/leads/{lead['id']}/engage",
        headers=headers,
        json={"row_version": engaged["row_version"], "reason": "Repeated action"},
    )
    assert repeat.status_code == 409
    assert repeat.json()["detail"] == "Lead is already engaged"

    converted = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json=conversion_payload(engaged),
    )
    assert converted.status_code == 200
    converted_lead = client.get(f"/api/leads/{lead['id']}", headers=headers).json()[
        "data"
    ]
    reset_converted = client.post(
        f"/api/leads/{lead['id']}/engage",
        headers=headers,
        json={
            "row_version": converted_lead["row_version"],
            "reason": "Must not reset a converted lead",
        },
    )
    assert reset_converted.status_code == 409
    assert reset_converted.json()["detail"] == (
        "Converted leads cannot be marked engaged"
    )

    archived_created = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload("archived-engagement@example.com"),
    ).json()["data"]
    archived_contacted = client.patch(
        f"/api/leads/{archived_created['id']}",
        headers=headers,
        json={
            "row_version": archived_created["row_version"],
            "status": "contacted",
        },
    ).json()["data"]
    archived = client.delete(
        f"/api/leads/{archived_created['id']}",
        headers=headers,
        params={"row_version": archived_contacted["row_version"]},
    )
    assert archived.status_code == 200
    reset_archived = client.post(
        f"/api/leads/{archived_created['id']}/engage",
        headers=headers,
        json={
            "row_version": archived_contacted["row_version"] + 1,
            "reason": "Must not reset an archived lead",
        },
    )
    assert reset_archived.status_code == 404

    other_tenant = create_tenant(database_session, "engagement-other-tenant")
    other_owner = create_user(database_session, "engagement.other.owner")
    add_membership(database_session, other_owner, other_tenant, roles[RoleKey.OWNER])
    cross_tenant = client.post(
        f"/api/leads/{lead['id']}/engage",
        headers=login_headers(client, other_owner),
        json={"row_version": engaged["row_version"], "reason": "Cross tenant"},
    )
    assert cross_tenant.status_code == 404


def test_conversion_rolls_back_customer_status_activity_and_history_on_failure(
    client: TestClient,
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, headers = owner_setup(client, database_session)
    lead = create_engaged_lead(
        client,
        headers,
        "conversion.rollback@example.com",
        whatsapp_number="+14155552671",
    )

    def fail_before_commit(*args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("forced conversion rollback")

    monkeypatch.setattr(
        "app.services.customer_service.add_authorization_audit",
        fail_before_commit,
    )
    with pytest.raises(RuntimeError, match="forced conversion rollback"):
        client.post(
            f"/api/leads/{lead['id']}/convert",
            headers=headers,
            json=conversion_payload(lead),
        )

    database_session.expire_all()
    stored_lead = database_session.get(Lead, uuid.UUID(lead["id"]))
    assert stored_lead is not None
    assert stored_lead.status == "engaged"
    assert database_session.scalar(select(func.count(Customer.id))) == 0
    assert database_session.scalar(select(func.count(LeadConversion.id))) == 0
    assert database_session.scalar(
        select(func.count(LeadActivity.id)).where(
            LeadActivity.lead_id == stored_lead.id,
            LeadActivity.outcome == "converted_to_customer",
        )
    ) == 0


def test_concurrent_conversion_unique_conflict_recovers_existing_link(
    client: TestClient,
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, tenant, owner, headers = owner_setup(client, database_session)
    lead = create_engaged_lead(
        client,
        headers,
        "conversion.concurrent@example.com",
    )
    customer = Customer(
        tenant_id=tenant.id,
        first_name="Concurrent",
        last_name="Customer",
        email="conversion.concurrent@example.com",
        created_by=owner.id,
    )
    database_session.add(customer)
    database_session.flush()
    conversion = LeadConversion(
        tenant_id=tenant.id,
        lead_id=uuid.UUID(lead["id"]),
        customer_id=customer.id,
        converted_by=owner.id,
        purchased_service="Website Design",
        reference="RACE-WINNER",
        reason="Concurrent winning transaction",
        duplicate_resolution="link_existing",
        pending_task_disposition="cancel",
        pending_task_reason="Concurrent conversion completed",
        source_consent_status="unknown",
    )
    database_session.add(conversion)
    database_session.commit()

    from app.repositories.customer_repository import get_conversion_by_lead as lookup

    calls = 0

    def stale_then_current(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return lookup(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.customer_service.get_conversion_by_lead",
        stale_then_current,
    )
    response = client.post(
        f"/api/leads/{lead['id']}/convert",
        headers=headers,
        json=conversion_payload(
            lead,
            duplicate_resolution="link_existing",
            existing_customer_id=str(customer.id),
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["idempotent_replay"] is True
    assert response.json()["data"]["conversion"]["id"] == str(conversion.id)
    assert database_session.scalar(select(func.count(Customer.id))) == 1
    assert database_session.scalar(select(func.count(LeadConversion.id))) == 1
