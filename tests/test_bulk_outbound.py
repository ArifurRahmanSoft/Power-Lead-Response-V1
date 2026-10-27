import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.permissions import RoleKey
from app.models.lead import Lead
from app.models.outbound_message import OutboundMessage, OutboundMessageBatch
from app.services.outbound_worker_service import claim_jobs, process_claimed_job
from tests.test_outbound_messages import EmptyStorage, RecordingProvider, setup


def add_lead(session: Session, tenant_id: uuid.UUID, user_id: uuid.UUID, *, first_name: str, email: str, consent: str = "granted", company: str | None = None) -> Lead:
    lead = Lead(
        tenant_id=tenant_id,
        tracking_id=f"LD-{uuid.uuid4().hex[:12].upper()}",
        first_name=first_name,
        last_name="Recipient",
        email=email,
        email_original=email,
        company=company,
        email_status="valid",
        source="manual",
        status="new",
        priority="normal",
        consent_status=consent,
        consent_evidence="Recorded opt-in" if consent == "granted" else None,
        assigned_user_id=user_id,
        created_by=user_id,
        updated_by=user_id,
    )
    session.add(lead)
    session.commit()
    return lead


def bulk_preview(client: TestClient, headers: dict[str, str], sender_id, template_id, **overrides):
    payload = {
        "selection_mode": "selected",
        "lead_ids": overrides.pop("lead_ids"),
        "sender_account_id": str(sender_id),
        "template_id": str(template_id),
        "template_version": 1,
        "contact_stage": "initial",
        "page": 1,
        "size": 50,
    }
    payload.update(overrides)
    response = client.post("/api/mail/batches/preview", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_bulk_personalization_confirmation_idempotency_and_lead_summary(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, first, sender, template, _, headers = setup(client, database_session)
    second = add_lead(database_session, tenant.id, user.id, first_name="Nadia", email="nadia@example.com")
    data = bulk_preview(
        client, headers, sender.id, template.id,
        lead_ids=[str(first.id), str(second.id)],
    )
    assert data["batch"]["selection_count"] == 2
    assert data["batch"]["eligible_count"] == 2
    subjects = {item["recipient_email"]: item["subject"] for item in data["items"]}
    assert subjects == {
        "arif@example.com": "Hello Arif",
        "nadia@example.com": "Hello Nadia",
    }
    request = {"preview_hash": data["batch"]["preview_hash"], "idempotency_key": "bulk-confirm-0001"}
    url = f"/api/mail/batches/{data['batch']['id']}/confirm"
    first_submit = client.post(url, headers=headers, json=request)
    replay = client.post(url, headers=headers, json=request)
    assert first_submit.status_code == replay.status_code == 202
    assert first_submit.json()["data"]["batch"]["id"] == replay.json()["data"]["batch"]["id"]
    assert database_session.scalar(select(OutboundMessageBatch)).contact_stage == "initial"
    messages = list(database_session.scalars(select(OutboundMessage).order_by(OutboundMessage.recipient_email)).all())
    assert len(messages) == 2
    assert all(message.batch_id is not None and message.contact_stage == "initial" for message in messages)

    listed = client.get("/api/leads?email_state=queued&email_stage=initial", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["total"] == 2
    assert all(item["email_summary"]["state"] == "queued" for item in listed.json()["data"]["items"])


def test_all_matching_is_server_resolved_and_reports_blocked_leads(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, first, sender, template, _, headers = setup(client, database_session)
    first.company = "Acme"
    blocked = add_lead(database_session, tenant.id, user.id, first_name="Blocked", email="blocked@example.com", consent="withdrawn", company="Acme")
    add_lead(database_session, tenant.id, user.id, first_name="Other", email="other@example.com", company="Other")
    database_session.commit()
    response = client.post("/api/mail/batches/preview", headers=headers, json={
        "selection_mode": "all_matching",
        "filters": {"company": "Acme"},
        "sender_account_id": str(sender.id),
        "template_id": str(template.id),
        "template_version": 1,
        "contact_stage": "follow_up",
    })
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["batch"]["selection_count"] == 2
    assert data["batch"]["eligible_count"] == 1
    assert data["batch"]["blocked_count"] == 1
    excluded = next(item for item in data["items"] if item["lead_id"] == str(blocked.id))
    assert excluded["exclusion_code"] == "not_eligible"


def test_duplicate_recipient_and_inaccessible_selection_are_explicit(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, first, sender, template, _, headers = setup(client, database_session)
    duplicate = add_lead(database_session, tenant.id, user.id, first_name="Duplicate", email="ARIF@example.com")
    unknown_id = uuid.uuid4()
    data = bulk_preview(
        client, headers, sender.id, template.id,
        lead_ids=[str(first.id), str(duplicate.id), str(unknown_id)],
    )
    assert data["batch"]["selection_count"] == 3
    assert data["batch"]["eligible_count"] == 0
    assert data["batch"]["blocked_count"] == 3
    codes = {item["requested_lead_id"]: item["exclusion_code"] for item in data["items"]}
    assert codes[str(first.id)] == "duplicate_recipient"
    assert codes[str(duplicate.id)] == "duplicate_recipient"
    assert codes[str(unknown_id)] == "not_accessible"


def test_stale_confirmation_and_safe_batch_cancellation(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    data = bulk_preview(client, headers, sender.id, template.id, lead_ids=[str(lead.id)])
    lead.first_name = "Changed"
    database_session.commit()
    stale = client.post(f"/api/mail/batches/{data['batch']['id']}/confirm", headers=headers, json={
        "preview_hash": data["batch"]["preview_hash"], "idempotency_key": "bulk-stale-0001",
    })
    assert stale.status_code == 409

    fresh = bulk_preview(client, headers, sender.id, template.id, lead_ids=[str(lead.id)])
    confirmed = client.post(f"/api/mail/batches/{fresh['batch']['id']}/confirm", headers=headers, json={
        "preview_hash": fresh["batch"]["preview_hash"], "idempotency_key": "bulk-cancel-0001",
    })
    assert confirmed.status_code == 202
    cancelled = client.post(f"/api/mail/batches/{fresh['batch']['id']}/cancel", headers=headers, json={"reason": "Campaign withdrawn"})
    assert cancelled.status_code == 200
    assert cancelled.json()["data"] == {"cancelled": 1, "in_flight": 0, "already_terminal": 0}


def test_bulk_preview_permission_and_no_email_history_summary(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(
        client, database_session, role_key=RoleKey.READONLY
    )
    denied = client.post("/api/mail/batches/preview", headers=headers, json={
        "selection_mode": "selected",
        "lead_ids": [str(lead.id)],
        "sender_account_id": str(sender.id),
        "template_id": str(template.id),
        "template_version": 1,
        "contact_stage": "initial",
    })
    assert denied.status_code == 403

    listed = client.get("/api/leads?email_state=not_contacted_by_email", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1
    assert listed.json()["data"]["items"][0]["email_summary"] == {
        "state": "not_contacted_by_email",
        "stage": None,
        "attempted_at": None,
        "sent_at": None,
        "last_successful_email_at": None,
        "attempt_count": 0,
    }


def test_batch_worker_rechecks_opt_out_before_send(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    data = bulk_preview(client, headers, sender.id, template.id, lead_ids=[str(lead.id)])
    confirmed = client.post(
        f"/api/mail/batches/{data['batch']['id']}/confirm",
        headers=headers,
        json={
            "preview_hash": data["batch"]["preview_hash"],
            "idempotency_key": "bulk-worker-optout-0001",
        },
    )
    assert confirmed.status_code == 202

    lead.consent_status = "denied"
    database_session.commit()
    claimed = claim_jobs(database_session, worker_id="bulk-worker-optout", limit=1)
    factory = sessionmaker(
        bind=database_session.get_bind(), expire_on_commit=False, class_=Session
    )
    result = asyncio.run(process_claimed_job(
        claimed[0][0],
        claimed[0][1],
        provider=RecordingProvider(),
        storage=EmptyStorage(),
        session_factory=factory,
    ))
    assert result == "cancelled"

    batch_status = client.get(
        f"/api/mail/batches/{data['batch']['id']}", headers=headers
    )
    assert batch_status.status_code == 200
    assert batch_status.json()["data"]["batch"]["cancelled_count"] == 1
