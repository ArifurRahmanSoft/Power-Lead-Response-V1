from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead, LeadCustomerAction
from app.models.lead_activity import LeadActivity
from app.models.lead_booking import LeadAutomationStopEvent
from app.models.lead_task import LeadTask
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
    engaged = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={"row_version": lead["row_version"], "status": "engaged"},
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

    listing = client.get("/api/customers?search=converted.customer", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1
    detail = client.get(f"/api/customers/{customer['id']}", headers=headers)
    assert detail.status_code == 200
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
    assigned_lead = create_engaged_lead(
        client,
        owner_headers,
        "staff.visible.customer@example.com",
        assigned_user_id=str(staff.id),
    )
    hidden_lead = create_engaged_lead(
        client,
        owner_headers,
        "staff.hidden.customer@example.com",
    )
    assigned_customer = client.post(
        f"/api/leads/{assigned_lead['id']}/convert",
        headers=owner_headers,
        json=conversion_payload(assigned_lead),
    ).json()["data"]["customer"]
    hidden_customer = client.post(
        f"/api/leads/{hidden_lead['id']}/convert",
        headers=owner_headers,
        json=conversion_payload(hidden_lead),
    ).json()["data"]["customer"]

    forbidden_conversion = client.post(
        f"/api/leads/{assigned_lead['id']}/convert",
        headers=staff_headers,
        json=conversion_payload(assigned_lead),
    )
    assert forbidden_conversion.status_code == 403
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
