from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.models.lead_activity import LeadActivity
from app.models.lead_assignment import LeadAssignmentHistory
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


def _task_payload(assigned_user_id: uuid.UUID, title: str) -> dict[str, object]:
    return {
        "assigned_user_id": str(assigned_user_id),
        "task_type": "review",
        "title": title,
        "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "priority": "normal",
    }


def _reassign_payload(
    lead: dict,
    new_assignee_id: uuid.UUID,
    disposition: str,
    *,
    reason: str = "Balance the active workload",
) -> dict[str, object]:
    return {
        "row_version": lead["row_version"],
        "assigned_user_id": str(new_assignee_id),
        "assignment_reason": reason,
        "pending_task_disposition": disposition,
    }


def test_lead_and_task_priorities_use_low_normal_high_urgent(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, owner, headers = owner_setup(client, database_session)
    created = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload("priority@example.com", priority="low"),
    )
    assert created.status_code == 201
    lead = created.json()["data"]
    assert lead["priority"] == "low"

    for priority in ("normal", "high", "urgent"):
        updated = client.patch(
            f"/api/leads/{lead['id']}",
            headers=headers,
            json={"row_version": lead["row_version"], "priority": priority},
        )
        assert updated.status_code == 200
        lead = updated.json()["data"]
        assert lead["priority"] == priority

    legacy_task = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=headers,
        json={**_task_payload(owner.id, "Legacy priority"), "priority": "medium"},
    )
    assert legacy_task.status_code == 201
    assert legacy_task.json()["data"]["priority"] == "normal"


def test_assignment_change_detection_covers_omitted_unchanged_assign_reassign_and_unassign(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, _, headers = owner_setup(client, database_session)
    first = create_user(database_session, "assignment.change.first")
    second = create_user(database_session, "assignment.change.second")
    add_membership(database_session, first, tenant, roles[RoleKey.STAFF])
    add_membership(database_session, second, tenant, roles[RoleKey.STAFF])
    created = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload("assignment-change@example.com"),
    )
    assert created.status_code == 201
    lead = created.json()["data"]
    assert lead["assigned_user_id"] is None

    omitted = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={"row_version": lead["row_version"], "company": "Preserved Assignment"},
    )
    assert omitted.status_code == 200
    lead = omitted.json()["data"]
    assert lead["assigned_user_id"] is None

    assigned = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={
            "row_version": lead["row_version"],
            "assigned_user_id": str(first.id),
            "assignment_reason": "Initial workload assignment",
            "pending_task_disposition": "retain",
        },
    )
    assert assigned.status_code == 200, assigned.text
    lead = assigned.json()["data"]
    assert lead["assigned_user_id"] == str(first.id)

    unchanged = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={
            "row_version": lead["row_version"],
            "assigned_user_id": str(first.id).upper(),
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    unchanged_lead = unchanged.json()["data"]
    assert unchanged_lead["assigned_user_id"] == str(first.id)
    assert unchanged_lead["row_version"] == lead["row_version"]
    assert database_session.scalar(
        select(func.count(LeadAssignmentHistory.id)).where(
            LeadAssignmentHistory.lead_id == uuid.UUID(lead["id"])
        )
    ) == 1

    staff_headers = login_headers(client, first)
    unchanged_without_assign_permission = client.patch(
        f"/api/leads/{lead['id']}",
        headers=staff_headers,
        json={
            "row_version": lead["row_version"],
            "assigned_user_id": str(first.id),
            "company": "Updated by assigned staff",
        },
    )
    assert unchanged_without_assign_permission.status_code == 200
    lead = unchanged_without_assign_permission.json()["data"]
    assert lead["assigned_user_id"] == str(first.id)
    assert lead["company"] == "Updated by assigned staff"
    assert database_session.scalar(
        select(func.count(LeadAssignmentHistory.id)).where(
            LeadAssignmentHistory.lead_id == uuid.UUID(lead["id"])
        )
    ) == 1

    omitted_after_assignment = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={"row_version": lead["row_version"], "industry": "Technology"},
    )
    assert omitted_after_assignment.status_code == 200
    lead = omitted_after_assignment.json()["data"]
    assert lead["assigned_user_id"] == str(first.id)

    reassigned = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={
            "row_version": lead["row_version"],
            "assigned_user_id": str(second.id),
            "assignment_reason": "Coverage rotation",
            "pending_task_disposition": "retain",
        },
    )
    assert reassigned.status_code == 200
    lead = reassigned.json()["data"]
    assert lead["assigned_user_id"] == str(second.id)

    unassigned = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={
            "row_version": lead["row_version"],
            "assigned_user_id": None,
            "assignment_reason": "Return lead to the unassigned queue",
            "pending_task_disposition": "retain",
        },
    )
    assert unassigned.status_code == 200, unassigned.text
    lead = unassigned.json()["data"]
    assert lead["assigned_user_id"] is None

    history_response = client.get(
        f"/api/leads/{lead['id']}/assignment-history?page=1&size=10",
        headers=headers,
    )
    history = history_response.json()["data"]
    assert history["total"] == 3
    newest = history["items"][0]
    assert newest["previous_assignee_id"] == str(second.id)
    assert newest["new_assignee_id"] is None
    assert newest["reason"] == "Return lead to the unassigned queue"


def test_missing_reason_is_rejected_only_for_an_actual_assignment_change(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, _, headers = owner_setup(client, database_session)
    assignee = create_user(database_session, "assignment.reason.assignee")
    add_membership(database_session, assignee, tenant, roles[RoleKey.STAFF])
    created = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload("assignment-reason@example.com"),
    ).json()["data"]

    missing = client.patch(
        f"/api/leads/{created['id']}",
        headers=headers,
        json={
            "row_version": created["row_version"],
            "assigned_user_id": str(assignee.id),
            "pending_task_disposition": "retain",
        },
    )
    assert missing.status_code == 400
    assert missing.json()["detail"] == "assignment_reason is required"

    database_session.expire_all()
    assert database_session.scalar(
        select(func.count(LeadAssignmentHistory.id)).where(
            LeadAssignmentHistory.lead_id == uuid.UUID(created["id"])
        )
    ) == 0


def test_reassignment_transfers_tasks_records_history_and_preserves_performer(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_access(database_session)
    tenant = create_tenant(database_session, "assignment-transfer")
    owner = create_user(database_session, "assignment.transfer.owner")
    previous = create_user(database_session, "assignment.transfer.previous")
    replacement = create_user(database_session, "assignment.transfer.replacement")
    add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    add_membership(database_session, previous, tenant, roles[RoleKey.STAFF])
    add_membership(database_session, replacement, tenant, roles[RoleKey.STAFF])
    headers = login_headers(client, owner)

    created = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload(
            "transfer@example.com",
            assigned_user_id=str(previous.id),
        ),
    )
    lead = created.json()["data"]
    task_response = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=headers,
        json=_task_payload(previous.id, "Pending transfer task"),
    )
    assert task_response.status_code == 201
    task = task_response.json()["data"]
    activity_response = client.post(
        f"/api/leads/{lead['id']}/activities",
        headers=headers,
        json={
            "activity_type": "note",
            "channel": "other",
            "direction": "internal",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "performed_by": str(previous.id),
            "notes": "Historical performer must remain unchanged.",
        },
    )
    assert activity_response.status_code == 201
    activity_id = uuid.UUID(activity_response.json()["data"]["id"])

    reassigned = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json=_reassign_payload(lead, replacement.id, "transfer"),
    )
    assert reassigned.status_code == 200, reassigned.text
    reassigned_lead = reassigned.json()["data"]
    assert reassigned_lead["assigned_user_id"] == str(replacement.id)
    assert reassigned_lead["row_version"] == lead["row_version"] + 1

    database_session.expire_all()
    stored_task = database_session.get(LeadTask, uuid.UUID(task["id"]))
    assert stored_task is not None
    assert stored_task.assigned_user_id == replacement.id
    assert stored_task.row_version == task["row_version"] + 1
    stored_activity = database_session.get(LeadActivity, activity_id)
    assert stored_activity is not None
    assert stored_activity.performed_by == previous.id

    history_response = client.get(
        f"/api/leads/{lead['id']}/assignment-history?page=1&size=10",
        headers=headers,
    )
    assert history_response.status_code == 200
    history_page = history_response.json()["data"]
    assert history_page["total"] == 1
    history = history_page["items"][0]
    assert history["previous_assignee_id"] == str(previous.id)
    assert history["new_assignee_id"] == str(replacement.id)
    assert history["actor_user_id"] == str(owner.id)
    assert history["reason"] == "Balance the active workload"
    assert history["task_disposition"] == "transfer"
    assert history["affected_task_count"] == 1


def test_reassignment_requires_reason_disposition_and_eligible_assignee(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, owner, headers = owner_setup(client, database_session)
    current = create_user(database_session, "assignment.validation.current")
    replacement = create_user(database_session, "assignment.validation.replacement")
    outsider = create_user(database_session, "assignment.validation.outsider")
    add_membership(database_session, current, tenant, roles[RoleKey.STAFF])
    replacement_membership = add_membership(
        database_session,
        replacement,
        tenant,
        roles[RoleKey.STAFF],
    )
    lead_response = client.post(
        "/api/leads",
        headers=headers,
        json=lead_payload(
            "assignment-validation@example.com",
            assigned_user_id=str(current.id),
        ),
    )
    lead = lead_response.json()["data"]

    missing_reason = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={
            "row_version": lead["row_version"],
            "assigned_user_id": str(replacement.id),
            "pending_task_disposition": "retain",
        },
    )
    assert missing_reason.status_code == 400
    assert missing_reason.json()["detail"] == "assignment_reason is required"

    missing_disposition = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json={
            "row_version": lead["row_version"],
            "assigned_user_id": str(replacement.id),
            "assignment_reason": "Coverage change",
        },
    )
    assert missing_disposition.status_code == 400
    assert "pending_task_disposition" in missing_disposition.json()["detail"]

    cross_tenant = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json=_reassign_payload(lead, outsider.id, "retain"),
    )
    assert cross_tenant.status_code == 400
    assert "active member" in cross_tenant.json()["detail"]

    replacement_membership.is_active = False
    database_session.commit()
    inactive = client.patch(
        f"/api/leads/{lead['id']}",
        headers=headers,
        json=_reassign_payload(lead, replacement.id, "retain"),
    )
    assert inactive.status_code == 400
    assert "active member" in inactive.json()["detail"]

    database_session.expire_all()
    assert database_session.scalar(select(LeadAssignmentHistory.id)) is None


def test_pending_task_retain_cancel_conflict_and_assignment_permission(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_access(database_session)
    tenant = create_tenant(database_session, "assignment-dispositions")
    owner = create_user(database_session, "assignment.dispositions.owner")
    first = create_user(database_session, "assignment.dispositions.first")
    second = create_user(database_session, "assignment.dispositions.second")
    third = create_user(database_session, "assignment.dispositions.third")
    add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    add_membership(database_session, first, tenant, roles[RoleKey.STAFF])
    add_membership(database_session, second, tenant, roles[RoleKey.STAFF])
    add_membership(database_session, third, tenant, roles[RoleKey.STAFF])
    owner_headers = login_headers(client, owner)

    lead_response = client.post(
        "/api/leads",
        headers=owner_headers,
        json=lead_payload(
            "dispositions@example.com",
            assigned_user_id=str(first.id),
        ),
    )
    lead = lead_response.json()["data"]
    retained_task_response = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=owner_headers,
        json=_task_payload(first.id, "Retained task"),
    )
    retained_task = retained_task_response.json()["data"]

    retained = client.patch(
        f"/api/leads/{lead['id']}",
        headers=owner_headers,
        json=_reassign_payload(lead, second.id, "retain"),
    )
    assert retained.status_code == 200
    lead = retained.json()["data"]
    database_session.expire_all()
    retained_row = database_session.get(LeadTask, uuid.UUID(retained_task["id"]))
    assert retained_row is not None
    assert retained_row.assigned_user_id == first.id
    assert retained_row.status == "pending"
    assert retained_row.row_version == retained_task["row_version"]

    cancellable_response = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=owner_headers,
        json=_task_payload(second.id, "Task to cancel"),
    )
    cancellable = cancellable_response.json()["data"]
    cancelled = client.patch(
        f"/api/leads/{lead['id']}",
        headers=owner_headers,
        json=_reassign_payload(lead, third.id, "cancel"),
    )
    assert cancelled.status_code == 200
    lead = cancelled.json()["data"]
    database_session.expire_all()
    for task_id in (retained_task["id"], cancellable["id"]):
        row = database_session.get(LeadTask, uuid.UUID(task_id))
        assert row is not None
        assert row.status == "cancelled"
        assert row.cancelled_by == owner.id
        assert row.cancelled_at is not None

    conflict_task_response = client.post(
        f"/api/leads/{lead['id']}/tasks",
        headers=owner_headers,
        json=_task_payload(third.id, "Must survive stale reassignment"),
    )
    conflict_task = conflict_task_response.json()["data"]
    stale = client.patch(
        f"/api/leads/{lead['id']}",
        headers=owner_headers,
        json={
            **_reassign_payload(lead, second.id, "transfer"),
            "row_version": lead["row_version"] - 1,
        },
    )
    assert stale.status_code == 409
    database_session.expire_all()
    conflict_task_row = database_session.get(
        LeadTask,
        uuid.UUID(conflict_task["id"]),
    )
    assert conflict_task_row is not None
    assert conflict_task_row.assigned_user_id == third.id
    assert conflict_task_row.status == "pending"
    assert conflict_task_row.row_version == conflict_task["row_version"]

    third_headers = login_headers(client, third)
    forbidden = client.patch(
        f"/api/leads/{lead['id']}",
        headers=third_headers,
        json=_reassign_payload(lead, second.id, "retain"),
    )
    assert forbidden.status_code == 403
