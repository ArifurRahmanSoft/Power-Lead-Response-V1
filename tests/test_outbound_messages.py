import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.permissions import PermissionKey, ROLE_PERMISSIONS, RoleKey
from app.core.storage import ObjectStorage, StoredObject
from app.models.lead import Lead
from app.models.customer import Customer, LeadConversion
from app.models.lead_activity import LeadActivity
from app.models.mail_template import MailTemplate, MailTemplateVersion
from app.models.membership import TenantMembership
from app.models.outbound_message import OutboundMessage, OutboundMessageEvent, OutboundMessageJob
from app.models.role import Permission, Role, RolePermission
from app.models.sender_account import SenderAccount
from app.models.tenant import Tenant
from app.models.user import User
from app.providers.smtp import SmtpAmbiguousDeliveryError, SmtpDeliveryConfig, SmtpProvider
from app.services.outbound_worker_service import claim_jobs, process_claimed_job
from app.utils.credential_encryption import encrypt_credential
from app.utils.password import hash_password

PASSWORD = "outbound-test-password"


class EmptyStorage(ObjectStorage):
    async def put(self, key: str, data: bytes, content_type: str) -> None: pass
    async def get(self, key: str) -> StoredObject: raise KeyError(key)
    async def delete(self, key: str) -> None: pass


class RecordingProvider(SmtpProvider):
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.ambiguous = ambiguous
        self.messages = []

    async def send_message(self, config: SmtpDeliveryConfig, message) -> None:
        self.messages.append((config, message))
        if self.ambiguous:
            raise SmtpAmbiguousDeliveryError


def setup(
    client: TestClient,
    session: Session,
    *,
    role_key: RoleKey = RoleKey.OWNER,
) -> tuple[Tenant, User, Lead, SenderAccount, MailTemplate, MailTemplateVersion, dict[str, str]]:
    roles = {key: Role(key=key.value, name=key.value.title(), is_system=True) for key in RoleKey}
    permissions = {key: Permission(key=key.value, description=key.value) for key in PermissionKey}
    session.add_all([*roles.values(), *permissions.values()])
    session.flush()
    session.add_all(RolePermission(role_id=roles[role].id, permission_id=permissions[key].id)
                    for role, keys in ROLE_PERMISSIONS.items() for key in keys)
    tenant = Tenant(name="Outbound Workspace", slug=f"outbound-{uuid.uuid4().hex[:8]}")
    user = User(name="Mail User", email=f"mail-{uuid.uuid4().hex[:8]}@example.com",
                login_id=f"mail.{uuid.uuid4().hex[:8]}", password_hash=hash_password(PASSWORD))
    session.add_all([tenant, user])
    session.flush()
    session.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role_id=roles[role_key].id))
    lead = Lead(
        tenant_id=tenant.id, tracking_id=f"LD-{uuid.uuid4().hex[:12].upper()}",
        first_name="Arif", last_name="Rahman", email="arif@example.com",
        email_original="arif@example.com", email_status="valid", source="manual",
        status="new", priority="normal", consent_status="granted",
        consent_evidence="Recorded opt-in", assigned_user_id=user.id,
        created_by=user.id, updated_by=user.id,
    )
    sender = SenderAccount(
        tenant_id=tenant.id, display_name="Sales", sender_email="sales@example.com",
        provider="custom_smtp", authentication_type="password", smtp_host="smtp.example.com",
        smtp_port=587, encryption="STARTTLS", smtp_username="sales@example.com",
        credential_ciphertext=encrypt_credential("secret"), reply_to="reply@example.com",
        is_active=True, is_default=True, created_by_user_id=user.id, updated_by_user_id=user.id,
    )
    template = MailTemplate(
        tenant_id=tenant.id, name="Approved", normalized_name="approved", enabled=True,
        current_version=1, created_by_user_id=user.id, updated_by_user_id=user.id,
    )
    session.add_all([lead, sender, template])
    session.flush()
    version = MailTemplateVersion(
        template_id=template.id, tenant_id=tenant.id, version=1,
        subject="Hello {{first_name}}", body_html="<p>Hi {{first_name}}</p>", body_text="Hi {{first_name}}",
        signature_html="<p>Regards</p>", signature_text="Regards", approval_status="approved",
        created_by_user_id=user.id, approved_by_user_id=user.id,
    )
    session.add(version)
    session.commit()
    login = client.post("/api/auth/login", json={"identifier": user.login_id, "password": PASSWORD})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    return tenant, user, lead, sender, template, version, headers


def preview(client: TestClient, headers: dict[str, str], lead: Lead, sender: SenderAccount, template: MailTemplate) -> dict:
    response = client.post("/api/mail/messages/preview", headers=headers, json={
        "context_type": "lead", "context_id": str(lead.id),
        "sender_account_id": str(sender.id), "template_id": str(template.id),
        "template_version": 1, "purpose": "lead_acquisition",
    })
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_preview_enqueue_idempotency_and_timeline(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    draft = preview(client, headers, lead, sender, template)
    assert draft["recipient"] == "arif@example.com"
    assert draft["subject"] == "Hello Arif"
    request = {"preview_id": draft["preview_id"], "preview_hash": draft["preview_hash"], "idempotency_key": "send-00000001"}
    first = client.post("/api/mail/messages/send", headers=headers, json=request)
    replay = client.post("/api/mail/messages/send", headers=headers, json=request)
    assert first.status_code == replay.status_code == 202
    assert first.json()["data"]["id"] == replay.json()["data"]["id"]
    assert database_session.scalar(select(OutboundMessage).where(OutboundMessage.lead_id == lead.id)) is not None
    activity = database_session.scalar(select(LeadActivity).where(LeadActivity.lead_id == lead.id))
    assert activity is not None and activity.is_system and activity.outbound_message_id is not None


def test_changed_recipient_requires_new_preview(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session)
    draft = preview(client, headers, lead, sender, template)
    lead.email = "changed@example.com"
    database_session.commit()
    response = client.post("/api/mail/messages/send", headers=headers, json={
        "preview_id": draft["preview_id"], "preview_hash": draft["preview_hash"], "idempotency_key": "send-changed-1",
    })
    assert response.status_code == 409
    assert "new preview" in response.json()["detail"].lower()


def test_permission_denied_for_readonly_preview(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, sender, template, _, headers = setup(client, database_session, role_key=RoleKey.READONLY)
    response = client.post("/api/mail/messages/preview", headers=headers, json={
        "context_type": "lead", "context_id": str(lead.id), "sender_account_id": str(sender.id),
        "template_id": str(template.id), "template_version": 1, "purpose": "lead_acquisition",
    })
    assert response.status_code == 403


def test_authorized_customer_without_email_history_returns_empty_page(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, _, _, _, _, headers = setup(client, database_session)
    customer = Customer(
        tenant_id=tenant.id,
        first_name="Empty",
        last_name="History",
        email="empty-history@example.com",
        communication_purpose="customer_relationship",
        communication_consent_status="unknown",
        channel_restrictions=[],
        created_by=user.id,
    )
    database_session.add(customer)
    database_session.commit()

    response = client.get(
        f"/api/mail/messages?page=1&size=100&customer_id={customer.id}",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {"items": [], "page": 1, "size": 100, "total": 0},
    }


def test_customer_history_filter_enforces_tenant_isolation(
    client: TestClient, database_session: Session
) -> None:
    _, user, _, _, _, _, headers = setup(client, database_session)
    foreign_tenant = Tenant(name="Foreign", slug=f"foreign-{uuid.uuid4().hex[:8]}")
    database_session.add(foreign_tenant)
    database_session.flush()
    foreign_customer = Customer(
        tenant_id=foreign_tenant.id,
        first_name="Foreign",
        last_name="Customer",
        email="foreign-customer@example.com",
        communication_purpose="customer_relationship",
        communication_consent_status="unknown",
        channel_restrictions=[],
        created_by=user.id,
    )
    database_session.add(foreign_customer)
    database_session.commit()

    response = client.get(
        f"/api/mail/messages?customer_id={foreign_customer.id}",
        headers=headers,
    )

    assert response.status_code == 404


def test_staff_customer_history_uses_assigned_source_lead_scope(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(
        client,
        database_session,
        role_key=RoleKey.STAFF,
    )
    customer = Customer(
        tenant_id=tenant.id,
        first_name="Scoped",
        last_name="Customer",
        email="scoped-customer@example.com",
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
        reason="Test conversion",
        duplicate_resolution="create_new",
        pending_task_disposition="retain",
        pending_task_reason="Retain test tasks",
        source_consent_status=lead.consent_status,
    ))
    database_session.commit()

    allowed = client.get(
        f"/api/mail/messages?customer_id={customer.id}",
        headers=headers,
    )
    assert allowed.status_code == 200
    assert allowed.json()["data"]["total"] == 0

    lead.assigned_user_id = None
    database_session.commit()
    denied = client.get(
        f"/api/mail/messages?customer_id={customer.id}",
        headers=headers,
    )
    assert denied.status_code == 404


def _enqueue(client: TestClient, session: Session):
    tenant, user, lead, sender, template, _, headers = setup(client, session)
    draft = preview(client, headers, lead, sender, template)
    response = client.post("/api/mail/messages/send", headers=headers, json={
        "preview_id": draft["preview_id"], "preview_hash": draft["preview_hash"], "idempotency_key": f"worker-{uuid.uuid4()}",
    })
    assert response.status_code == 202
    return tenant, user, lead, uuid.UUID(response.json()["data"]["id"])


def test_worker_claim_is_atomic_and_records_smtp_acceptance(
    client: TestClient, database_session: Session
) -> None:
    _, _, _, message_id = _enqueue(client, database_session)
    first = claim_jobs(database_session, worker_id="worker-a", limit=10)
    second = claim_jobs(database_session, worker_id="worker-b", limit=10)
    assert len(first) == 1 and second == []
    provider = RecordingProvider()
    factory = sessionmaker(bind=database_session.get_bind(), expire_on_commit=False, class_=Session)
    result = asyncio.run(process_claimed_job(first[0][0], first[0][1], provider=provider,
        storage=EmptyStorage(), session_factory=factory))
    assert result == "sent" and len(provider.messages) == 1
    database_session.expire_all()
    message = database_session.get(OutboundMessage, message_id)
    assert message.status == "sent" and message.sent_at is not None
    statuses = list(database_session.scalars(select(OutboundMessageEvent.status).where(
        OutboundMessageEvent.message_id == message_id
    )).all())
    assert statuses == ["queued", "processing", "sent"]


def test_worker_rechecks_consent_before_send(
    client: TestClient, database_session: Session
) -> None:
    _, _, lead, message_id = _enqueue(client, database_session)
    lead.consent_status = "withdrawn"
    database_session.commit()
    claimed = claim_jobs(database_session, worker_id="worker-optout", limit=1)
    factory = sessionmaker(bind=database_session.get_bind(), expire_on_commit=False, class_=Session)
    assert asyncio.run(process_claimed_job(claimed[0][0], claimed[0][1], provider=RecordingProvider(),
        storage=EmptyStorage(), session_factory=factory)) == "cancelled"
    database_session.expire_all()
    assert database_session.get(OutboundMessage, message_id).status == "cancelled"



def test_ambiguous_smtp_outcome_is_not_retried(
    client: TestClient, database_session: Session
) -> None:
    _, _, _, ambiguous_id = _enqueue(client, database_session)
    claimed = claim_jobs(database_session, worker_id="worker-unknown", limit=1)
    factory = sessionmaker(bind=database_session.get_bind(), expire_on_commit=False, class_=Session)
    assert asyncio.run(process_claimed_job(claimed[0][0], claimed[0][1], provider=RecordingProvider(ambiguous=True),
        storage=EmptyStorage(), session_factory=factory)) == "unknown"
    database_session.expire_all()
    assert database_session.get(OutboundMessage, ambiguous_id).status == "unknown"
    assert database_session.scalar(select(OutboundMessageJob).where(
        OutboundMessageJob.message_id == ambiguous_id
    )).status == "unknown"


def test_expired_lease_after_provider_start_becomes_unknown(
    client: TestClient, database_session: Session
) -> None:
    _, _, _, message_id = _enqueue(client, database_session)
    job = database_session.scalar(select(OutboundMessageJob).where(
        OutboundMessageJob.message_id == message_id
    ))
    message = database_session.get(OutboundMessage, message_id)
    job.status = "processing"
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    message.status = "processing"
    message.provider_attempt_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    database_session.commit()
    assert claim_jobs(database_session, worker_id="recovery-worker", limit=1) == []
    database_session.expire_all()
    assert database_session.get(OutboundMessage, message_id).status == "unknown"
    assert database_session.get(OutboundMessageJob, job.id).status == "unknown"
