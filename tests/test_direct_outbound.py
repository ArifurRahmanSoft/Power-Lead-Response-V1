import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.storage import get_object_storage
from app.main import app
from app.models.lead_activity import LeadActivity
from app.models.outbound_message import (
    OutboundMessage,
    OutboundMessageBatch,
    OutboundMessageJob,
)
from app.providers.smtp import (
    SmtpAmbiguousDeliveryError,
    SmtpConnectionError,
    get_smtp_provider,
)
from tests.test_bulk_outbound import add_lead
from tests.test_outbound_messages import (
    EmptyStorage,
    RecordingProvider,
    preview,
    setup,
)


class MixedOutcomeProvider(RecordingProvider):
    async def send_message(self, config, message) -> None:
        self.messages.append((config, message))
        recipient = str(message["To"])
        if recipient.startswith("failed-"):
            raise SmtpConnectionError
        if recipient.startswith("unknown-"):
            raise SmtpAmbiguousDeliveryError


@pytest.fixture(autouse=True)
def mocked_direct_transport():
    provider = RecordingProvider()
    storage = EmptyStorage()
    app.dependency_overrides[get_smtp_provider] = lambda: provider
    app.dependency_overrides[get_object_storage] = lambda: storage
    yield provider
    app.dependency_overrides.pop(get_smtp_provider, None)
    app.dependency_overrides.pop(get_object_storage, None)


def direct_payload(
    sender_id,
    template_id,
    *,
    lead_ids=None,
    filters=None,
    key=None,
    contact_type="initial",
    supersede_queued=False,
):
    return {
        "selection_mode": "selected" if lead_ids is not None else "all_matching",
        "lead_ids": [str(value) for value in lead_ids] if lead_ids is not None else None,
        "filters": filters,
        "sender_account_id": str(sender_id),
        "template_id": str(template_id),
        "template_version": 1,
        "contact_type": contact_type,
        "supersede_queued": supersede_queued,
        "idempotency_key": key or f"direct-{uuid.uuid4()}",
    }


def queue_worker_message(client, headers, lead, sender, template):
    draft = preview(client, headers, lead, sender, template)
    response = client.post(
        "/api/mail/messages/send",
        headers=headers,
        json={
            "preview_id": draft["preview_id"],
            "preview_hash": draft["preview_hash"],
            "idempotency_key": f"queued-{uuid.uuid4()}",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]


def test_direct_single_send_and_idempotent_replay(
    client: TestClient,
    database_session: Session,
    mocked_direct_transport: RecordingProvider,
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    payload = direct_payload(
        sender.id,
        template.id,
        lead_ids=[lead.id],
        key="direct-single-0001",
    )

    first = client.post("/api/mail/batches/send", headers=headers, json=payload)
    replay = client.post("/api/mail/batches/send", headers=headers, json=payload)

    assert first.status_code == replay.status_code == 200
    first_data = first.json()["data"]
    replay_data = replay.json()["data"]
    assert first_data["batch_id"] == replay_data["batch_id"]
    assert first_data["sent_count"] == 1
    assert first_data["queued_count"] == first_data["processing_count"] == 0
    assert first_data["skipped_count"] == 0
    assert first_data["pending_worker_job_count"] == 0
    assert first_data["replayed"] is False
    assert replay_data["replayed"] is True
    assert len(mocked_direct_transport.messages) == 1
    assert database_session.scalar(select(func.count(OutboundMessage.id))) == 1
    assert database_session.scalar(select(func.count(OutboundMessageJob.id))) == 0

    message = database_session.scalar(select(OutboundMessage))
    assert message is not None
    assert message.subject == "Hello Arif"
    assert message.status == "sent"
    assert message.contact_stage == "initial"
    activity = database_session.scalar(
        select(LeadActivity).where(LeadActivity.outbound_message_id == message.id)
    )
    assert activity is not None
    assert activity.outcome == "sent"
    assert activity.contact_stage == "initial"
    status_lookup = client.get(
        f"/api/mail/batches/{first_data['batch_id']}", headers=headers
    )
    assert status_lookup.status_code == 200
    assert status_lookup.json()["data"]["batch"]["sent_count"] == 1

    message_detail = client.get(
        f"/api/mail/messages/{message.id}", headers=headers
    )
    assert message_detail.status_code == 200
    assert message_detail.json()["data"]["job_id"] is None
    assert message_detail.json()["data"]["status"] == "sent"

    message_history = client.get(
        "/api/mail/messages",
        params={"lead_id": str(lead.id)},
        headers=headers,
    )
    assert message_history.status_code == 200
    assert message_history.json()["data"]["items"][0]["job_id"] is None


def test_direct_ten_recipient_limit_and_personalization(
    client: TestClient,
    database_session: Session,
    mocked_direct_transport: RecordingProvider,
) -> None:
    tenant, user, first, sender, template, _, headers = setup(
        client, database_session
    )
    leads = [first]
    for index in range(2, 11):
        leads.append(add_lead(
            database_session,
            tenant.id,
            user.id,
            first_name=f"Person{index}",
            email=f"person-{index}@example.com",
            consent="unknown",
        ))
    response = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(
            sender.id, template.id, lead_ids=[lead.id for lead in leads]
        ),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["sent_count"] == 10
    assert len(mocked_direct_transport.messages) == 10
    messages = list(database_session.scalars(select(OutboundMessage)).all())
    expected_subjects = {"Hello Arif"}
    expected_subjects.update(f"Hello Person{index}" for index in range(2, 11))
    assert {item.subject for item in messages} == expected_subjects

    extra = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Eleven",
        email="person-11@example.com",
    )
    too_many = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(
            sender.id,
            template.id,
            lead_ids=[lead.id for lead in leads] + [extra.id],
        ),
    )
    assert too_many.status_code == 422
    assert too_many.json()["detail"]["maximum"] == 10


def test_direct_all_matching_uses_server_side_filters(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, user, first, sender, template, _, headers = setup(
        client, database_session
    )
    first.company = "Acme"
    second = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="AcmeTwo",
        email="acme-two@example.com",
        company="Acme",
    )
    add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Other",
        email="other-direct@example.com",
        company="Other",
    )
    database_session.commit()

    response = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(sender.id, template.id, filters={"company": "Acme"}),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["selection_count"] == data["sent_count"] == 2
    assert {item["lead_id"] for item in data["recipients"]} == {
        str(first.id), str(second.id)
    }


def test_direct_send_reports_mixed_eligibility_and_duplicate_recipients(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, user, first, sender, template, _, headers = setup(
        client, database_session
    )
    duplicate = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Duplicate",
        email="ARIF@example.com",
    )
    allowed = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Unknown",
        email="unknown-direct@example.com",
        consent="unknown",
    )
    denied = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Denied",
        email="denied-direct@example.com",
        consent="denied",
    )

    response = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(
            sender.id,
            template.id,
            lead_ids=[first.id, duplicate.id, allowed.id, denied.id],
        ),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["sent_count"] == 1
    assert data["skipped_count"] == 3
    by_lead = {item["lead_id"]: item for item in data["recipients"]}
    assert by_lead[str(first.id)]["skip_code"] == "duplicate_recipient"
    assert by_lead[str(duplicate.id)]["skip_code"] == "duplicate_recipient"
    assert by_lead[str(denied.id)]["skip_code"] == "not_eligible"
    assert by_lead[str(allowed.id)]["status"] == "sent"


def test_direct_send_continues_after_mixed_smtp_failures_and_unknown(
    client: TestClient,
    database_session: Session,
) -> None:
    provider = MixedOutcomeProvider()
    app.dependency_overrides[get_smtp_provider] = lambda: provider
    tenant, user, success, sender, template, _, headers = setup(
        client, database_session
    )
    failed = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Failed",
        email="failed-recipient@example.com",
    )
    unknown = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Unknown",
        email="unknown-recipient@example.com",
    )

    response = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(
            sender.id,
            template.id,
            lead_ids=[success.id, failed.id, unknown.id],
        ),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["sent_count"] == 1
    assert data["failed_count"] == 1
    assert data["unknown_count"] == 1
    assert len(provider.messages) == 3
    states = {
        item.recipient_email: item.status
        for item in database_session.scalars(select(OutboundMessage)).all()
    }
    assert states == {
        "arif@example.com": "sent",
        "failed-recipient@example.com": "failed",
        "unknown-recipient@example.com": "unknown",
    }


def test_direct_send_with_no_eligible_recipients_is_a_clear_noop(
    client: TestClient,
    database_session: Session,
    mocked_direct_transport: RecordingProvider,
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    lead.consent_status = "denied"
    database_session.commit()
    payload = direct_payload(
        sender.id,
        template.id,
        lead_ids=[lead.id],
        key="direct-none-eligible-0001",
    )

    response = client.post("/api/mail/batches/send", headers=headers, json=payload)
    replay = client.post("/api/mail/batches/send", headers=headers, json=payload)

    assert response.status_code == replay.status_code == 200
    assert response.json()["message"] == (
        "No eligible recipients; no SMTP attempts were made"
    )
    assert response.json()["data"]["sent_count"] == 0
    assert response.json()["data"]["skipped_count"] == 1
    assert replay.json()["data"]["replayed"] is True
    assert len(mocked_direct_transport.messages) == 0
    assert database_session.scalar(select(func.count(OutboundMessage.id))) == 0
    assert database_session.scalar(select(OutboundMessageBatch)).status == "completed"


def test_direct_send_explicitly_supersedes_untouched_queued_worker_job(
    client: TestClient,
    database_session: Session,
    mocked_direct_transport: RecordingProvider,
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    queued = queue_worker_message(client, headers, lead, sender, template)
    old_message_id = uuid.UUID(queued["id"])
    old_job_id = uuid.UUID(queued["job_id"])

    response = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(
            sender.id,
            template.id,
            lead_ids=[lead.id],
            key="replace-queued-direct-0001",
            supersede_queued=True,
        ),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["sent_count"] == 1
    assert data["pending_worker_job_count"] == 0
    assert "cancelled as superseded" in data["recipients"][0]["warnings"][0]
    assert len(mocked_direct_transport.messages) == 1

    database_session.expire_all()
    old_message = database_session.get(OutboundMessage, old_message_id)
    old_job = database_session.get(OutboundMessageJob, old_job_id)
    assert old_message.status == "cancelled"
    assert old_message.provider_attempt_started_at is None
    assert old_job.status == "cancelled"
    replacement = database_session.scalar(
        select(OutboundMessage).where(OutboundMessage.id != old_message_id)
    )
    assert replacement is not None and replacement.status == "sent"
    assert database_session.scalar(
        select(OutboundMessageJob).where(
            OutboundMessageJob.message_id == replacement.id
        )
    ) is None


def test_direct_send_keeps_queued_duplicate_protection_without_opt_in(
    client: TestClient,
    database_session: Session,
    mocked_direct_transport: RecordingProvider,
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    queued = queue_worker_message(client, headers, lead, sender, template)

    response = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(
            sender.id,
            template.id,
            lead_ids=[lead.id],
            key="keep-queued-protection-0001",
        ),
    )

    assert response.status_code == 200, response.text
    recipient = response.json()["data"]["recipients"][0]
    assert recipient["status"] == "skipped"
    assert recipient["skip_code"] == "active_send_conflict"
    assert len(mocked_direct_transport.messages) == 0
    database_session.expire_all()
    assert database_session.get(
        OutboundMessage, uuid.UUID(queued["id"])
    ).status == "queued"
    assert database_session.get(
        OutboundMessageJob, uuid.UUID(queued["job_id"])
    ).status == "queued"


def test_direct_send_does_not_supersede_worker_claim_or_attempt_race(
    client: TestClient,
    database_session: Session,
    mocked_direct_transport: RecordingProvider,
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    queued = queue_worker_message(client, headers, lead, sender, template)
    old_message = database_session.get(OutboundMessage, uuid.UUID(queued["id"]))
    old_job = database_session.get(OutboundMessageJob, uuid.UUID(queued["job_id"]))
    old_message.status = "processing"
    old_message.processing_at = datetime.now(timezone.utc)
    old_job.status = "processing"
    old_job.lease_token = uuid.uuid4()
    old_job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    database_session.commit()

    response = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(
            sender.id,
            template.id,
            lead_ids=[lead.id],
            key="do-not-replace-claimed-0001",
            supersede_queued=True,
        ),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["sent_count"] == 0
    assert data["skipped_count"] == 1
    assert data["recipients"][0]["skip_code"] == "active_send_conflict"
    assert len(mocked_direct_transport.messages) == 0
    database_session.expire_all()
    assert database_session.get(OutboundMessage, old_message.id).status == "processing"
    assert database_session.get(OutboundMessageJob, old_job.id).status == "processing"


def test_direct_manually_selected_follow_up_without_history_is_warning_only(
    client: TestClient,
    database_session: Session,
    mocked_direct_transport: RecordingProvider,
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    response = client.post(
        "/api/mail/batches/send",
        headers=headers,
        json=direct_payload(
            sender.id,
            template.id,
            lead_ids=[lead.id],
            key="manual-follow-up-warning-0001",
            contact_type="follow_up",
        ),
    )

    assert response.status_code == 200, response.text
    recipient = response.json()["data"]["recipients"][0]
    assert recipient["status"] == "sent"
    assert "no initial contact exists" in recipient["warnings"][0].lower()
    assert len(mocked_direct_transport.messages) == 1
