from datetime import datetime, timezone
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.models.authorization_audit import AuthorizationAudit
from app.models.lead_activity import LeadActivity
from tests.test_leads import (
    add_membership,
    create_tenant,
    create_user,
    lead_payload,
    login_headers,
    owner_setup,
    seed_access,
)


def create_lead(client: TestClient, headers: dict[str, str], email: str) -> dict:
    response = client.post("/api/leads", headers=headers, json=lead_payload(email))
    assert response.status_code == 201
    return response.json()["data"]


def activity_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "activity_type": "initial_contact",
        "channel": "email",
        "direction": "outbound",
        "occurred_at": "2026-10-12T10:30:00+06:00",
        "outcome": "Replied later",
        "notes": "Introductory email sent outside PowerLead.",
    }
    payload.update(overrides)
    return payload


def test_create_email_activity_and_paginated_timeline(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "activity@example.com")

    created = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json=activity_payload(performed_by=str(owner.id)),
    )
    assert created.status_code == 201
    data = created.json()["data"]
    assert data["recorded_by"] == str(owner.id)
    assert data["performed_by"] == str(owner.id)
    assert data["occurred_at"].endswith("Z")
    assert data["externally_performed"] is True
    assert data["email_delivery_verified"] is False
    assert data["is_system"] is False

    second = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json=activity_payload(
            activity_type="note",
            channel="other",
            direction="internal",
            occurred_at="2026-10-13T08:00:00Z",
            outcome=None,
            notes="Internal qualification note.",
        ),
    )
    assert second.status_code == 201

    page = client.get(
        f"/api/leads/{lead['id']}/activities?page=1&size=1",
        headers=headers,
    )
    assert page.status_code == 200
    assert page.json()["data"]["total"] == 2
    assert len(page.json()["data"]["items"]) == 1
    assert page.json()["data"]["items"][0]["activity_type"] == "note"


def test_activity_validation_and_tenant_scoped_references(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, _, _, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "reference@example.com")
    other_tenant = create_tenant(database_session, "other-activity-workspace")
    other_user = create_user(database_session, "other.activity.user")
    add_membership(database_session, other_user, other_tenant, roles[RoleKey.OWNER])

    naive_time = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json=activity_payload(occurred_at="2026-10-12T10:30:00"),
    )
    assert naive_time.status_code == 422

    wrong_direction = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json=activity_payload(direction="inbound"),
    )
    assert wrong_direction.status_code == 422

    foreign_performer = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json=activity_payload(performed_by=str(other_user.id)),
    )
    assert foreign_performer.status_code == 400
    assert "active workspace member" in foreign_performer.json()["detail"]

    missing_related = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json=activity_payload(related_activity_id="00000000-0000-0000-0000-000000000001"),
    )
    assert missing_related.status_code == 400

    second_lead = create_lead(client, headers, "second-reference@example.com")
    second_activity = client.post(
        f"/api/leads/{second_lead['id']}/activities",
        headers=headers,
        json=activity_payload(),
    )
    cross_lead_relation = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json=activity_payload(
            related_activity_id=second_activity.json()["data"]["id"]
        ),
    )
    assert cross_lead_relation.status_code == 400


def test_amendment_preserves_original_and_audits_change(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "amendment@example.com")
    original_response = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json=activity_payload(notes="Original note"),
    )
    original = original_response.json()["data"]

    amended = client.post(
        f"/api/leads/{lead['id']}/activities/{original['id']}/amendments",
        headers=headers,
        json={
            "amendment_reason": "Corrected the documented outcome",
            "outcome": "Customer requested a follow-up",
            "notes": "Original note with corrected context",
        },
    )
    assert amended.status_code == 201
    amendment = amended.json()["data"]
    assert amendment["is_amendment"] is True
    assert amendment["related_activity_id"] == original["id"]
    assert amendment["amendment_reason"] == "Corrected the documented outcome"

    original_row = database_session.get(LeadActivity, uuid.UUID(original["id"]))
    assert original_row is not None
    assert original_row.notes == "Original note"
    assert database_session.scalar(
        select(AuthorizationAudit).where(
            AuthorizationAudit.action == "lead.activity.amended"
        )
    ) is not None


def test_system_activity_cannot_be_amended(
    client: TestClient,
    database_session: Session,
) -> None:
    _, tenant, owner, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "system-event@example.com")
    system_activity = LeadActivity(
        tenant_id=tenant.id,
        lead_id=uuid.UUID(lead["id"]),
        activity_type="note",
        channel="other",
        direction="internal",
        occurred_at=datetime.now(timezone.utc),
        recorded_by=owner.id,
        notes="Automated system event",
        is_system=True,
    )
    database_session.add(system_activity)
    database_session.commit()

    response = client.post(
        f"/api/leads/{lead['id']}/activities/{system_activity.id}/amendments",
        headers=headers,
        json={"amendment_reason": "Should fail", "notes": "Changed"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "System activities cannot be amended"


def test_permissions_staff_scope_and_cross_tenant_visibility(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_access(database_session)
    tenant = create_tenant(database_session, "activity-scope")
    owner = create_user(database_session, "activity.scope.owner")
    staff = create_user(database_session, "activity.scope.staff")
    readonly = create_user(database_session, "activity.scope.readonly")
    add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    add_membership(database_session, staff, tenant, roles[RoleKey.STAFF])
    add_membership(database_session, readonly, tenant, roles[RoleKey.READONLY])
    owner_headers = login_headers(client, owner)
    staff_headers = login_headers(client, staff)
    readonly_headers = login_headers(client, readonly)
    unassigned = create_lead(client, owner_headers, "unassigned.activity@example.com")
    assigned = create_lead(
        client,
        owner_headers,
        "assigned.activity@example.com",
    )
    updated = client.patch(
        f"/api/leads/{assigned['id']}",
        headers=owner_headers,
        json={
            "row_version": assigned["row_version"],
            "assigned_user_id": str(staff.id),
            "assignment_reason": "Assign lead for activity handling",
            "pending_task_disposition": "retain",
        },
    )
    assert updated.status_code == 200

    denied_scope = client.get(
        f"/api/leads/{unassigned['id']}/activities",
        headers=staff_headers,
    )
    assert denied_scope.status_code == 403

    allowed = client.post(
        f"/api/leads/{assigned['id']}/activities",
        headers=staff_headers,
        json=activity_payload(channel="phone"),
    )
    assert allowed.status_code == 201

    readonly_denied = client.post(
        f"/api/leads/{assigned['id']}/activities",
        headers=readonly_headers,
        json=activity_payload(),
    )
    assert readonly_denied.status_code == 403

    other_tenant = create_tenant(database_session, "other-activity-scope")
    other_owner = create_user(database_session, "other.activity.owner")
    add_membership(database_session, other_owner, other_tenant, roles[RoleKey.OWNER])
    other_headers = login_headers(client, other_owner)
    cross_tenant = client.get(
        f"/api/leads/{assigned['id']}/activities",
        headers=other_headers,
    )
    assert cross_tenant.status_code == 404
