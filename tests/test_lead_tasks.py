from datetime import datetime, time, timedelta, timezone
import uuid
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.models.lead import Lead
from app.models.lead_activity import LeadActivity
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
        "/api/leads",
        headers=headers,
        json=lead_payload(email, **overrides),
    )
    assert response.status_code == 201
    return response.json()["data"]


def task_payload(assigned_user_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "assigned_user_id": str(assigned_user_id),
        "task_type": "review",
        "title": " Review lead requirements ",
        "notes": "Confirm the project scope.",
        "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "priority": "medium",
    }
    payload.update(overrides)
    return payload


def create_task(
    client: TestClient,
    headers: dict[str, str],
    lead_id: str,
    assigned_user_id: uuid.UUID,
    **overrides: object,
) -> dict:
    response = client.post(
        f"/api/leads/{lead_id}/tasks",
        headers=headers,
        json=task_payload(assigned_user_id, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_create_update_cancel_and_paginated_list(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "task-crud@example.com")
    task = create_task(client, headers, lead["id"], owner.id)
    assert task["title"] == "Review lead requirements"
    assert task["status"] == "pending"
    assert task["row_version"] == 1

    updated = client.patch(
        f"/api/tasks/{task['id']}",
        headers=headers,
        json={
            "row_version": task["row_version"],
            "title": "Updated review",
            "status": "in_progress",
            "priority": "high",
        },
    )
    assert updated.status_code == 200
    updated_task = updated.json()["data"]
    assert updated_task["status"] == "in_progress"
    assert updated_task["row_version"] == 2

    listing = client.get(
        f"/api/tasks?lead_id={lead['id']}&page=1&size=1",
        headers=headers,
    )
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1
    assert len(listing.json()["data"]["items"]) == 1

    cancelled = client.post(
        f"/api/tasks/{task['id']}/cancel",
        headers=headers,
        json={"row_version": updated_task["row_version"]},
    )
    assert cancelled.status_code == 200
    cancelled_task = cancelled.json()["data"]
    assert cancelled_task["status"] == "cancelled"
    assert cancelled_task["cancelled_by"] == str(owner.id)
    assert cancelled_task["cancelled_at"].endswith("Z")

    terminal_update = client.patch(
        f"/api/tasks/{task['id']}",
        headers=headers,
        json={"row_version": cancelled_task["row_version"], "title": "Not allowed"},
    )
    assert terminal_update.status_code == 409


def test_contact_completion_requires_activity_and_atomic_creation(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "contact-task@example.com")
    task = create_task(
        client,
        headers,
        lead["id"],
        owner.id,
        task_type="contact",
        title="Contact prospect",
    )

    missing_activity = client.post(
        f"/api/tasks/{task['id']}/complete",
        headers=headers,
        json={"row_version": task["row_version"]},
    )
    assert missing_activity.status_code == 400

    before_count = database_session.scalar(select(func.count(LeadActivity.id))) or 0
    completed = client.post(
        f"/api/tasks/{task['id']}/complete",
        headers=headers,
        json={
            "row_version": task["row_version"],
            "activity": {
                "activity_type": "initial_contact",
                "channel": "phone",
                "direction": "outbound",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "performed_by": str(owner.id),
                "outcome": "Spoke with the prospect",
                "notes": "Requested a proposal.",
            },
        },
    )
    assert completed.status_code == 200, completed.text
    completed_task = completed.json()["data"]
    assert completed_task["status"] == "completed"
    assert completed_task["completed_by"] == str(owner.id)
    assert completed_task["related_activity_id"] is not None
    assert (
        database_session.scalar(select(func.count(LeadActivity.id))) or 0
    ) == before_count + 1
    activity = database_session.get(
        LeadActivity,
        uuid.UUID(completed_task["related_activity_id"]),
    )
    assert activity is not None
    assert activity.lead_id == uuid.UUID(lead["id"])

    unchanged_lead = database_session.get(Lead, uuid.UUID(lead["id"]))
    assert unchanged_lead is not None
    assert unchanged_lead.status == lead["status"]


def test_non_contact_task_completes_without_activity(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    lead = create_lead(client, headers, "review-task@example.com")
    task = create_task(client, headers, lead["id"], owner.id, task_type="review")
    response = client.post(
        f"/api/tasks/{task['id']}/complete",
        headers=headers,
        json={"row_version": task["row_version"]},
    )
    assert response.status_code == 200
    assert response.json()["data"]["related_activity_id"] is None


def test_my_tasks_due_today_overdue_and_timezone_filters(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, owner, headers = owner_setup(client, database_session)
    colleague = create_user(database_session, "task.colleague")
    add_membership(database_session, colleague, tenant, roles[RoleKey.OPERATOR])
    lead = create_lead(client, headers, "filter-task@example.com")
    zone = ZoneInfo("Asia/Dhaka")
    local_today = datetime.now(timezone.utc).astimezone(zone).date()
    today_noon = datetime.combine(local_today, time(hour=23, minute=59), zone)

    create_task(
        client,
        headers,
        lead["id"],
        owner.id,
        title="Due today",
        due_at=today_noon.isoformat(),
    )
    create_task(
        client,
        headers,
        lead["id"],
        owner.id,
        title="Overdue",
        due_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    )
    create_task(
        client,
        headers,
        lead["id"],
        colleague.id,
        title="Colleague task",
        due_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    )

    mine = client.get("/api/tasks?assigned_to_me=true", headers=headers)
    assert mine.status_code == 200
    assert mine.json()["data"]["total"] == 2

    today = client.get(
        "/api/tasks?due_today=true&timezone=Asia%2FDhaka",
        headers=headers,
    )
    assert today.status_code == 200
    assert [item["title"] for item in today.json()["data"]["items"]] == ["Due today"]

    overdue = client.get(
        "/api/tasks?overdue=true&timezone=Asia%2FDhaka",
        headers=headers,
    )
    assert overdue.status_code == 200
    assert [item["title"] for item in overdue.json()["data"]["items"]] == ["Overdue"]

    invalid_timezone = client.get(
        "/api/tasks?due_today=true&timezone=Mars%2FOlympus",
        headers=headers,
    )
    assert invalid_timezone.status_code == 422

    conflicting = client.get(
        "/api/tasks?due_today=true&overdue=true",
        headers=headers,
    )
    assert conflicting.status_code == 422


def test_assignment_staff_scope_and_tenant_isolation(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_access(database_session)
    tenant = create_tenant(database_session, "task-scope")
    owner = create_user(database_session, "task.scope.owner")
    staff = create_user(database_session, "task.scope.staff")
    add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    add_membership(database_session, staff, tenant, roles[RoleKey.STAFF])
    owner_headers = login_headers(client, owner)
    staff_headers = login_headers(client, staff)
    lead = create_lead(
        client,
        owner_headers,
        "staff-task@example.com",
        assigned_user_id=str(staff.id),
    )

    self_task = create_task(client, staff_headers, lead["id"], staff.id)
    unauthorized_assignment = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=staff_headers,
        json=task_payload(owner.id),
    )
    assert unauthorized_assignment.status_code == 403

    other_tenant = create_tenant(database_session, "other-task-scope")
    other_owner = create_user(database_session, "other.task.owner")
    add_membership(database_session, other_owner, other_tenant, roles[RoleKey.OWNER])
    other_headers = login_headers(client, other_owner)
    cross_tenant = client.patch(
        f"/api/tasks/{self_task['id']}",
        headers=other_headers,
        json={"row_version": self_task["row_version"], "title": "Forbidden"},
    )
    assert cross_tenant.status_code == 404

    foreign_assignment = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=owner_headers,
        json=task_payload(other_owner.id),
    )
    assert foreign_assignment.status_code == 400
