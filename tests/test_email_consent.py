import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.email_consent import (
    email_consent_denial_message,
    is_email_consent_denied,
    normalize_email_consent_status,
)
from app.models.customer import Customer, LeadConversion
from tests.test_bulk_outbound import add_lead, bulk_preview
from tests.test_outbound_messages import setup


def test_consent_normalization_blocks_only_explicit_refusal_or_withdrawal() -> None:
    for value in (None, "", "   ", "unknown", "not_requested", "granted", "legacy"):
        assert is_email_consent_denied(value) is False

    assert is_email_consent_denied(" DENIED ") is True
    assert is_email_consent_denied("Withdrawn") is True
    assert normalize_email_consent_status(" Not Requested ") == "not_requested"
    assert email_consent_denial_message("Lead", "denied") == (
        "Lead email consent is explicitly denied"
    )
    assert email_consent_denial_message("Lead", "withdrawn") == (
        "Lead email consent was withdrawn"
    )


def test_single_preview_allows_unknown_and_blank_but_blocks_denied_and_withdrawn(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    request = {
        "context_type": "lead",
        "context_id": str(lead.id),
        "sender_account_id": str(sender.id),
        "template_id": str(template.id),
        "template_version": 1,
        "purpose": "lead_acquisition",
    }

    for value in ("unknown", "not_requested", "granted", ""):
        lead.consent_status = value
        database_session.commit()
        response = client.post("/api/mail/messages/preview", headers=headers, json=request)
        assert response.status_code == 200, (value, response.text)

    for value, message in (
        ("denied", "explicitly denied"),
        ("withdrawn", "withdrawn"),
    ):
        lead.consent_status = value
        database_session.commit()
        response = client.post("/api/mail/messages/preview", headers=headers, json=request)
        assert response.status_code == 409
        assert message in response.json()["detail"].lower()


def test_customer_service_preview_uses_the_same_consent_rule(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, sender, template, _, headers = setup(client, database_session)
    customer = Customer(
        tenant_id=tenant.id,
        first_name="Customer",
        last_name="Example",
        email="customer-consent@example.com",
        communication_purpose="customer_relationship",
        communication_consent_status="unknown",
        channel_restrictions=[],
        created_by=user.id,
    )
    database_session.add(customer)
    database_session.flush()
    database_session.add(LeadConversion(
        tenant_id=tenant.id,
        lead_id=lead.id,
        customer_id=customer.id,
        converted_by=user.id,
        purchased_service="Website Design",
        reason="Consent eligibility test",
        duplicate_resolution="create_new",
        pending_task_disposition="retain",
        pending_task_reason="Test fixture",
        source_consent_status=lead.consent_status,
    ))
    database_session.commit()
    request = {
        "context_type": "customer",
        "context_id": str(customer.id),
        "sender_account_id": str(sender.id),
        "template_id": str(template.id),
        "template_version": 1,
        "purpose": "customer_service",
    }

    allowed = client.post("/api/mail/messages/preview", headers=headers, json=request)
    assert allowed.status_code == 200, allowed.text

    customer.communication_consent_status = "denied"
    database_session.commit()
    denied = client.post("/api/mail/messages/preview", headers=headers, json=request)
    assert denied.status_code == 409
    assert "explicitly denied" in denied.json()["detail"].lower()


def test_single_send_allows_unknown_consent_without_changing_it(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    lead.consent_status = "unknown"
    database_session.commit()
    request = {
        "context_type": "lead",
        "context_id": str(lead.id),
        "sender_account_id": str(sender.id),
        "template_id": str(template.id),
        "template_version": 1,
        "purpose": "lead_acquisition",
    }
    preview = client.post(
        "/api/mail/messages/preview", headers=headers, json=request
    )
    assert preview.status_code == 200
    draft = preview.json()["data"]

    queued = client.post("/api/mail/messages/send", headers=headers, json={
        "preview_id": draft["preview_id"],
        "preview_hash": draft["preview_hash"],
        "idempotency_key": f"unknown-consent-{uuid.uuid4()}",
    })
    assert queued.status_code == 202, queued.text
    database_session.refresh(lead)
    assert lead.consent_status == "unknown"


def test_bulk_preview_reports_only_explicitly_denied_recipient_as_blocked(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, granted, sender, template, _, headers = setup(client, database_session)
    unknown = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Unknown",
        email="unknown-consent@example.com",
        consent="unknown",
    )
    blank = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Blank",
        email="blank-consent@example.com",
        consent="",
    )
    denied = add_lead(
        database_session,
        tenant.id,
        user.id,
        first_name="Denied",
        email="denied-consent@example.com",
        consent="denied",
    )

    data = bulk_preview(
        client,
        headers,
        sender.id,
        template.id,
        lead_ids=[str(granted.id), str(unknown.id), str(blank.id), str(denied.id)],
    )

    assert data["batch"]["eligible_count"] == 3
    assert data["batch"]["blocked_count"] == 1
    by_lead = {item["lead_id"]: item for item in data["items"]}
    assert by_lead[str(denied.id)]["preview_status"] == "blocked"
    assert "explicitly denied" in by_lead[str(denied.id)]["exclusion_reason"].lower()


def test_bulk_confirmation_rechecks_consent_change(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    lead.consent_status = "unknown"
    database_session.commit()
    data = bulk_preview(
        client, headers, sender.id, template.id, lead_ids=[str(lead.id)]
    )

    lead.consent_status = "denied"
    database_session.commit()
    response = client.post(
        f"/api/mail/batches/{data['batch']['id']}/confirm",
        headers=headers,
        json={
            "preview_hash": data["batch"]["preview_hash"],
            "idempotency_key": f"consent-change-{uuid.uuid4()}",
        },
    )
    assert response.status_code == 409
    assert "no longer current" in response.json()["detail"].lower()
