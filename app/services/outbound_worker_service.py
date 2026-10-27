import asyncio
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email_consent import email_consent_denial_message
from app.core.database import SessionLocal
from app.core.storage import ObjectStorage, ObjectStorageError, get_object_storage
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.outbound_message import OutboundMessage, OutboundMessageJob
from app.models.sender_account import SenderAccount
from app.providers.smtp import (
    SmtpAmbiguousDeliveryError,
    SmtpAuthenticationError,
    SmtpConnectionError,
    SmtpProvider,
    SmtpProviderError,
    SmtpSecurityError,
    get_smtp_provider,
)
from app.repositories.outbound_message_repository import add_event, recent_sent_count
from app.services.mail_composition_service import MailCompositionError, compose_message_snapshot
from app.services.sender_account_service import sender_delivery_config
from app.utils.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryptionNotConfiguredError,
)


def claim_jobs(session: Session, *, worker_id: str, limit: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    now = datetime.now(timezone.utc)
    statement = select(OutboundMessageJob).where(
        or_(
            (OutboundMessageJob.status.in_(("queued", "retry"))) & (OutboundMessageJob.available_at <= now),
            (OutboundMessageJob.status == "processing") & (OutboundMessageJob.lease_expires_at < now),
        )
    ).order_by(OutboundMessageJob.available_at, OutboundMessageJob.id).limit(limit).with_for_update(skip_locked=True)
    jobs = list(session.scalars(statement).all())
    claimed: list[tuple[uuid.UUID, uuid.UUID]] = []
    for job in jobs:
        expired_processing = (
            job.status == "processing"
            and job.lease_expires_at is not None
            and job.lease_expires_at < now
        )
        message = session.get(OutboundMessage, job.message_id)
        if message is None or message.status in {"sent", "failed", "unknown", "cancelled"}:
            job.status = "cancelled" if message and message.status == "cancelled" else "failed"
            continue
        if (
            expired_processing
            and message.provider_attempt_started_at is not None
        ):
            message.status = "unknown"
            message.failed_at = now
            message.failure_code = "worker_lease_expired_during_smtp"
            message.failure_reason = "SMTP acceptance is unknown because the worker lease expired"
            job.status = "unknown"
            job.last_error_code = message.failure_code
            job.lease_token = None
            job.lease_expires_at = None
            add_event(session, message, "unknown", safe_reason=message.failure_reason, occurred_at=now)
            continue
        token = uuid.uuid4()
        job.status = "processing"
        job.lease_token = token
        job.lease_expires_at = now + timedelta(seconds=settings.mail_worker_lease_seconds)
        job.worker_id = worker_id
        message.status = "processing"
        message.processing_at = now
        add_event(session, message, "processing", occurred_at=now)
        claimed.append((job.id, token))
    session.commit()
    return claimed


def message_eligibility_error(session: Session, message: OutboundMessage) -> str | None:
    lead = session.get(Lead, message.lead_id) if message.lead_id else None
    if lead is None or lead.tenant_id != message.tenant_id or lead.deleted_at is not None:
        return "Lead is no longer eligible"
    if message.purpose == "lead_acquisition":
        if lead.status == "converted":
            return "Lead converted before send"
        if lead.status == "disqualified":
            return "Lead was closed or disqualified before send"
        consent_denial = email_consent_denial_message("Lead", lead.consent_status)
        if consent_denial:
            return consent_denial
        if lead.email_status in {"invalid", "bounced", "disposable"}:
            return "Lead email is no longer eligible"
    else:
        customer = session.get(Customer, message.customer_id) if message.customer_id else None
        if customer is None or customer.tenant_id != message.tenant_id:
            return "Customer is no longer available"
        consent_denial = email_consent_denial_message(
            "Customer", customer.communication_consent_status
        )
        if consent_denial:
            return consent_denial
        if customer.suppressed_at is not None or "email" in {
            str(value).casefold() for value in (customer.channel_restrictions or [])
        }:
            return "Customer email communication is suppressed"
    return None


def _finish(session: Session, job: OutboundMessageJob, message: OutboundMessage, status: str, *, code: str | None = None, reason: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    message.status = status
    message.failure_code = code
    message.failure_reason = reason
    job.last_error_code = code
    job.lease_token = None
    job.lease_expires_at = None
    if status == "sent":
        message.sent_at = now
        job.status = "completed"
    elif status == "failed":
        message.failed_at = now
        job.status = "failed"
    elif status == "unknown":
        message.failed_at = now
        job.status = "unknown"
    elif status == "cancelled":
        message.cancelled_at = message.cancelled_at or now
        job.status = "cancelled"
    add_event(session, message, status, safe_reason=reason, occurred_at=now)
    session.commit()


def _retry_or_fail(
    session: Session,
    job: OutboundMessageJob,
    message: OutboundMessage,
    *,
    code: str,
    reason: str,
) -> str:
    if job.attempts >= settings.mail_worker_max_attempts:
        _finish(session, job, message, "failed", code=code, reason=reason)
        return "failed"
    job.status = "retry"
    job.available_at = datetime.now(timezone.utc) + timedelta(
        seconds=min(300, 2 ** job.attempts * 5)
    )
    job.lease_token = None
    job.lease_expires_at = None
    job.last_error_code = code
    message.provider_attempt_started_at = None
    message.status = "queued"
    message.failure_code = code
    message.failure_reason = reason
    add_event(session, message, "queued", safe_reason="Retry scheduled after a pre-acceptance failure")
    session.commit()
    return "retry"


async def process_claimed_job(
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    *,
    provider: SmtpProvider,
    storage: ObjectStorage,
    session_factory=SessionLocal,
) -> str:
    session = session_factory()
    try:
        job = session.scalar(select(OutboundMessageJob).where(
            OutboundMessageJob.id == job_id,
            OutboundMessageJob.lease_token == lease_token,
            OutboundMessageJob.status == "processing",
        ))
        if job is None:
            return "lease_lost"
        message = session.get(OutboundMessage, job.message_id)
        if message is None:
            job.status = "failed"
            session.commit()
            return "missing"
        if message.cancel_requested_at is not None:
            _finish(session, job, message, "cancelled", code="cancel_requested", reason=message.cancellation_reason or "Cancelled before send")
            return "cancelled"
        eligibility = message_eligibility_error(session, message)
        if eligibility:
            _finish(session, job, message, "cancelled", code="not_eligible", reason=eligibility)
            return "cancelled"
        sender = session.get(SenderAccount, message.sender_account_id)
        snapshot = message.sender_snapshot
        if (
            sender is None or not sender.is_active
            or sender.sender_email != snapshot["sender_email"]
            or sender.display_name != snapshot["display_name"]
            or sender.reply_to != snapshot.get("reply_to")
        ):
            _finish(session, job, message, "failed", code="sender_changed", reason="Sender configuration changed; create a new preview")
            return "failed"
        since = datetime.now(timezone.utc) - timedelta(minutes=1)
        if recent_sent_count(session, tenant_id=message.tenant_id, since=since) >= settings.mail_worker_workspace_rate_per_minute or recent_sent_count(
            session, tenant_id=message.tenant_id, sender_account_id=sender.id, since=since
        ) >= settings.mail_worker_sender_rate_per_minute:
            job.status = "retry"
            job.available_at = datetime.now(timezone.utc) + timedelta(seconds=10)
            job.lease_token = None
            job.lease_expires_at = None
            message.status = "queued"
            session.commit()
            return "rate_limited"
        job.attempts += 1
        session.commit()
        email_message = await compose_message_snapshot(
            subject=message.subject,
            body_html=message.body_html,
            body_text=message.body_text,
            asset_manifest=message.asset_manifest,
            sender_name=str(snapshot["display_name"]),
            sender_email=str(snapshot["sender_email"]),
            recipient_email=message.recipient_email,
            reply_to=str(snapshot["reply_to"]) if snapshot.get("reply_to") else None,
            storage=storage,
        )
        encoded_size = len(email_message.as_bytes())
        if encoded_size > settings.mail_max_encoded_message_bytes:
            _finish(session, job, message, "failed", code="message_too_large", reason="Encoded message exceeds the configured size limit")
            return "failed"
        # Re-read cancellation immediately before the irreversible provider call.
        session.refresh(message)
        if message.cancel_requested_at is not None or message.status == "cancelled":
            _finish(session, job, message, "cancelled", code="cancel_requested", reason=message.cancellation_reason or "Cancelled before send")
            return "cancelled"
        try:
            message.provider_attempt_started_at = datetime.now(timezone.utc)
            session.commit()
            await provider.send_message(sender_delivery_config(sender), email_message)
        except SmtpAmbiguousDeliveryError as exception:
            _finish(session, job, message, "unknown", code=exception.code, reason=exception.safe_message)
            return "unknown"
        except SmtpConnectionError as exception:
            # The provider only emits this class before SMTP DATA starts.
            return _retry_or_fail(
                session,
                job,
                message,
                code=exception.code,
                reason=exception.safe_message,
            )
        except (SmtpAuthenticationError, SmtpSecurityError, SmtpProviderError) as exception:
            _finish(session, job, message, "failed", code=exception.code, reason=exception.safe_message)
            return "failed"
        _finish(session, job, message, "sent")
        return "sent"
    except MailCompositionError:
        if 'job' in locals() and 'message' in locals():
            _finish(session, job, message, "failed", code="composition_failed", reason="Stored message content or assets are unavailable")
        return "failed"
    except (CredentialDecryptionError, CredentialEncryptionNotConfiguredError):
        if 'job' in locals() and 'message' in locals():
            _finish(session, job, message, "failed", code="credential_unavailable", reason="Sender credential could not be loaded")
        return "failed"
    except ObjectStorageError:
        if 'job' in locals() and 'message' in locals():
            return _retry_or_fail(
                session,
                job,
                message,
                code="asset_storage_unavailable",
                reason="Message assets are temporarily unavailable",
            )
        return "failed"
    finally:
        session.close()


async def run_worker_once(*, worker_id: str | None = None, provider: SmtpProvider | None = None, storage: ObjectStorage | None = None) -> int:
    worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as session:
        claimed = claim_jobs(session, worker_id=worker_id, limit=settings.mail_worker_batch_size)
    semaphore = asyncio.Semaphore(settings.mail_worker_concurrency)

    async def run(item: tuple[uuid.UUID, uuid.UUID]) -> None:
        async with semaphore:
            await process_claimed_job(item[0], item[1], provider=provider or get_smtp_provider(), storage=storage or get_object_storage())

    await asyncio.gather(*(run(item) for item in claimed), return_exceptions=True)
    return len(claimed)
