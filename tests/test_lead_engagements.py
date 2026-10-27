from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.models.lead import Lead, LeadCustomerAction
from app.models.lead_activity import LeadActivity
from app.models.lead_booking import (
    LeadAutomationStopEvent,
)
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


def create_lead(client: TestClient, headers: dict[str, str], email: str, **overrides: object) -> dict:
    response = client.post(
        "/api/leads", headers=headers, json=lead_payload(email, **overrides)
    )
    assert response.status_code == 201
    return response.json()["data"]


def create_outbound_activity(
    client: TestClient,
    headers: dict[str, str],
    lead_id: str,
) -> dict:
    response = client.post(
        f"/api/leads/{lead_id}/activities",
        headers=headers,
        json={
            "activity_type": "initial_contact",
            "channel": "email",
            "direction": "outbound",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "Initial message sent externally",
        },
    )
    assert response.status_code == 201
    return response.json()["data"]


def booking_payload(user_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "timezone": "Asia/Dhaka",
        "responsible_user_id": str(user_id),
        "source": "phone",
        "status": "scheduled",
        "notes": "Discovery call",
    }
    payload.update(overrides)
    return payload


def test_manual_reply_uses_activity_and_stops_recovery_without_changing_lead(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "manual-reply@example.com")
    outbound = create_outbound_activity(client, headers, lead["id"])
    lead_row = database_session.get(Lead, uuid.UUID(lead["id"]))
    assert lead_row is not None
    pending_action = LeadCustomerAction(
        tenant_id=lead_row.tenant_id,
        lead_id=uuid.UUID(lead["id"]),
        action_type="intentional_human_follow_up",
        status="pending",
    )
    database_session.add(pending_action)
    database_session.commit()

    response = client.post(
        f"/api/leads/{lead['id']}/replies",
        headers=headers,
        json={
            "channel": "whatsapp",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "summary": "  Prospect replied and asked for a meeting.  ",
            "related_outbound_activity_id": outbound["id"],
        },
    )
    assert response.status_code == 201, response.text
    reply = response.json()["data"]
    assert reply["summary"] == "Prospect replied and asked for a meeting."
    assert reply["related_outbound_activity_id"] == outbound["id"]
    assert reply["provider_verified"] is False

    activity = database_session.get(LeadActivity, uuid.UUID(reply["id"]))
    assert activity is not None
    assert activity.activity_type == "inbound_reply"
    assert activity.direction == "inbound"
    assert activity.externally_performed is True
    assert activity.provider_verified is False
    assert database_session.scalar(
        select(LeadAutomationStopEvent).where(
            LeadAutomationStopEvent.lead_id == uuid.UUID(lead["id"]),
            LeadAutomationStopEvent.stop_key == "recovery_follow_up",
            LeadAutomationStopEvent.trigger_type == "manual_reply",
        )
    ) is not None
    assert database_session.get(LeadCustomerAction, pending_action.id).status == "pending"
    assert database_session.get(Lead, uuid.UUID(lead["id"])).status == lead["status"]


def test_manual_reply_requires_same_lead_outbound_activity(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, headers = owner_setup(client, database_session)
    first = create_lead(client, headers, "first-reply@example.com")
    second = create_lead(client, headers, "second-reply@example.com")
    second_outbound = create_outbound_activity(client, headers, second["id"])

    response = client.post(
        f"/api/leads/{first['id']}/replies",
        headers=headers,
        json={
            "channel": "email",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "summary": "Wrong lead relation",
            "related_outbound_activity_id": second_outbound["id"],
        },
    )
    assert response.status_code == 400
    assert "outbound activity for this lead" in response.json()["detail"]


def test_booking_confirmation_reschedule_cancellation_and_history(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "booking-history@example.com")
    created = client.post(
        f"/api/leads/{lead['id']}/bookings",
        headers=headers,
        json=booking_payload(owner.id),
    )
    assert created.status_code == 201, created.text
    booking = created.json()["data"]
    assert booking["status"] == "scheduled"
    assert booking["provider_verified"] is False

    confirmed = client.post(
        f"/api/leads/{lead['id']}/bookings/{booking['id']}/confirm",
        headers=headers,
        json={"row_version": booking["row_version"], "notes": "Confirmed by phone"},
    )
    assert confirmed.status_code == 200
    booking = confirmed.json()["data"]
    assert booking["status"] == "confirmed"

    new_time = datetime.now(timezone.utc) + timedelta(days=5)
    rescheduled = client.post(
        f"/api/leads/{lead['id']}/bookings/{booking['id']}/reschedule",
        headers=headers,
        json={
            "row_version": booking["row_version"],
            "scheduled_at": new_time.isoformat(),
            "timezone": "UTC",
            "notes": "Customer requested a later date",
        },
    )
    assert rescheduled.status_code == 200
    booking = rescheduled.json()["data"]
    assert booking["status"] == "confirmed"
    assert booking["timezone"] == "UTC"

    cancelled = client.post(
        f"/api/leads/{lead['id']}/bookings/{booking['id']}/cancel",
        headers=headers,
        json={"row_version": booking["row_version"], "notes": "Customer cancelled"},
    )
    assert cancelled.status_code == 200
    booking = cancelled.json()["data"]
    assert booking["status"] == "cancelled"

    history = client.get(
        f"/api/leads/{lead['id']}/bookings/{booking['id']}/history?page=1&size=10",
        headers=headers,
    )
    assert history.status_code == 200
    assert history.json()["data"]["total"] == 4
    assert {item["event_type"] for item in history.json()["data"]["items"]} == {
        "created",
        "confirmed",
        "rescheduled",
        "cancelled",
    }
    assert database_session.scalar(
        select(func.count(LeadAutomationStopEvent.id)).where(
            LeadAutomationStopEvent.lead_id == uuid.UUID(lead["id"])
        )
    ) == 3
    assert database_session.get(Lead, uuid.UUID(lead["id"])).status == lead["status"]

    repeat_cancel = client.post(
        f"/api/leads/{lead['id']}/bookings/{booking['id']}/cancel",
        headers=headers,
        json={"row_version": booking["row_version"]},
    )
    assert repeat_cancel.status_code == 409


def test_booking_pagination_timezone_assignment_and_tenant_scope(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, owner, headers = owner_setup(client, database_session)
    staff = create_user(database_session, "booking.staff")
    add_membership(database_session, staff, tenant, roles[RoleKey.STAFF])
    lead = create_lead(
        client,
        headers,
        "booking-scope@example.com",
        assigned_user_id=str(staff.id),
    )
    staff_headers = login_headers(client, staff)
    first = client.post(
        f"/api/leads/{lead['id']}/bookings",
        headers=staff_headers,
        json=booking_payload(staff.id),
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/leads/{lead['id']}/bookings",
        headers=staff_headers,
        json=booking_payload(staff.id, notes="Second booking"),
    )
    assert second.status_code == 201

    page = client.get(
        f"/api/leads/{lead['id']}/bookings?page=1&size=1",
        headers=staff_headers,
    )
    assert page.status_code == 200
    assert page.json()["data"]["total"] == 2

    unauthorized_assignment = client.post(
        f"/api/leads/{lead['id']}/bookings",
        headers=staff_headers,
        json=booking_payload(owner.id),
    )
    assert unauthorized_assignment.status_code == 403

    invalid_timezone = client.post(
        f"/api/leads/{lead['id']}/bookings",
        headers=headers,
        json=booking_payload(owner.id, timezone="Mars/Olympus"),
    )
    assert invalid_timezone.status_code == 422

    other_tenant = create_tenant(database_session, "other-booking-tenant")
    other_owner = create_user(database_session, "other.booking.owner")
    add_membership(database_session, other_owner, other_tenant, roles[RoleKey.OWNER])
    other_headers = login_headers(client, other_owner)
    cross_tenant = client.get(
        f"/api/leads/{lead['id']}/bookings",
        headers=other_headers,
    )
    assert cross_tenant.status_code == 404


def test_reply_and_booking_do_not_cancel_human_tasks(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "human-task@example.com")
    task_response = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=headers,
        json={
            "assigned_user_id": str(owner.id),
            "task_type": "review",
            "title": "Prepare custom proposal",
            "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
    )
    assert task_response.status_code == 201
    task_id = uuid.UUID(task_response.json()["data"]["id"])
    outbound = create_outbound_activity(client, headers, lead["id"])
    reply = client.post(
        f"/api/leads/{lead['id']}/replies",
        headers=headers,
        json={
            "channel": "phone",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "summary": "Called back",
            "related_outbound_activity_id": outbound["id"],
        },
    )
    assert reply.status_code == 201
    booking = client.post(
        f"/api/leads/{lead['id']}/bookings",
        headers=headers,
        json=booking_payload(owner.id),
    )
    assert booking.status_code == 201
    assert database_session.get(LeadTask, task_id).status == "pending"
