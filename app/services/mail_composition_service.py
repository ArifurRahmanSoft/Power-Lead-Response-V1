from email.message import EmailMessage
from email.utils import formataddr
import hashlib
import uuid

from app.core.storage import ObjectStorage
from app.models.mail_template import MailTemplateAsset, MailTemplateVersion
from app.utils.mail_template import (
    extract_asset_ids,
    html_to_plain_text,
    render_text_template,
    replace_asset_images_with_cids,
)


class MailCompositionError(Exception):
    pass


async def compose_message_snapshot(
    *,
    subject: str,
    body_html: str,
    body_text: str,
    asset_manifest: list[dict[str, object]],
    sender_name: str,
    sender_email: str,
    recipient_email: str,
    reply_to: str | None,
    storage: ObjectStorage,
) -> EmailMessage:
    """Compose the immutable, already-rendered snapshot stored at enqueue time."""
    if "\r" in subject or "\n" in subject:
        raise MailCompositionError("Subject contains an unsafe header value")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender_email))
    message["To"] = recipient_email
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body_text)
    message.add_alternative(replace_asset_images_with_cids(body_html), subtype="html")
    html_part = message.get_payload()[1]
    for item in asset_manifest:
        asset_id = uuid.UUID(str(item["id"]))
        stored = await storage.get(str(item["storage_key"]))
        expected_size = int(item["size_bytes"])
        expected_sha256 = str(item["sha256"])
        if (
            len(stored.data) != expected_size
            or hashlib.sha256(stored.data).hexdigest() != expected_sha256
        ):
            raise MailCompositionError("A snapshotted template asset has changed")
        maintype, subtype = str(item["content_type"]).split("/", 1)
        kind = str(item["kind"])
        filename = str(item["filename"])
        if kind in {"inline_body", "signature_image"}:
            html_part.add_related(
                stored.data,
                maintype=maintype,
                subtype=subtype,
                cid=f"<template-asset-{asset_id}>",
                filename=filename,
                disposition="inline",
            )
        elif kind == "attachment":
            message.add_attachment(
                stored.data,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )
    return message


async def compose_approved_template_message(
    *,
    version: MailTemplateVersion,
    assets: list[MailTemplateAsset],
    values: dict[str, object],
    sender_name: str,
    sender_email: str,
    recipient_email: str,
    reply_to: str | None,
    storage: ObjectStorage,
) -> EmailMessage:
    """Compose only; callers must enforce consent, suppression and idempotency."""
    if version.approval_status != "approved":
        raise MailCompositionError("Only an approved template version can be composed")
    subject, subject_missing = render_text_template(
        version.subject, values, escape_html=False
    )
    body_html, body_missing = render_text_template(
        version.body_html, values, escape_html=True
    )
    signature_html, signature_missing = render_text_template(
        version.signature_html, values, escape_html=True
    )
    missing = subject_missing | body_missing | signature_missing
    if missing:
        raise MailCompositionError(
            "Missing required template values: " + ", ".join(sorted(missing))
        )

    combined_html = body_html + (
        f'<div class="mail-signature">{signature_html}</div>'
        if signature_html
        else ""
    )
    referenced = extract_asset_ids(combined_html)
    asset_by_id = {asset.id: asset for asset in assets}
    if not referenced <= asset_by_id.keys():
        raise MailCompositionError("Template references unavailable inline images")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender_email))
    message["To"] = recipient_email
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(html_to_plain_text(combined_html))
    message.add_alternative(replace_asset_images_with_cids(combined_html), subtype="html")
    html_part = message.get_payload()[1]

    for asset in assets:
        stored = await storage.get(asset.storage_key)
        maintype, subtype = asset.content_type.split("/", 1)
        if asset.kind in {"inline_body", "signature_image"}:
            if asset.id not in referenced:
                continue
            html_part.add_related(
                stored.data,
                maintype=maintype,
                subtype=subtype,
                cid=f"<template-asset-{asset.id}>",
                filename=asset.original_filename,
                disposition="inline",
            )
        elif asset.kind == "attachment":
            message.add_attachment(
                stored.data,
                maintype=maintype,
                subtype=subtype,
                filename=asset.original_filename,
            )
    return message
