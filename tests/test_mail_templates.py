import asyncio
import base64
from contextlib import contextmanager
from collections.abc import Generator
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, ROLE_PERMISSIONS, RoleKey
from app.core.storage import ObjectStorage, StoredObject, get_object_storage
from app.main import app
from app.models.mail_template import MailTemplate, MailTemplateAsset, MailTemplateVersion
from app.models.membership import TenantMembership
from app.models.role import Permission, Role, RolePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.mail_template_repository import list_version_assets
from app.services.mail_composition_service import compose_approved_template_message
from app.utils.password import hash_password

PASSWORD = "12345678"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PDF = b"%PDF-1.7\n% safe test document\n"


class MemoryStorage(ObjectStorage):
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = StoredObject(data=data, content_type=content_type)

    async def get(self, key: str) -> StoredObject:
        return self.objects[key]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@contextmanager
def use_storage(storage: MemoryStorage) -> Generator[None, None, None]:
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_object_storage, None)


def seed_roles(database_session: Session) -> dict[RoleKey, Role]:
    roles = {
        key: Role(key=key.value, name=key.value.title(), is_system=True)
        for key in RoleKey
    }
    permissions = {
        key.value: Permission(key=key.value, description=key.value)
        for key in PermissionKey
    }
    database_session.add_all([*roles.values(), *permissions.values()])
    database_session.flush()
    database_session.add_all(
        RolePermission(
            role_id=roles[role_key].id,
            permission_id=permissions[permission.value].id,
        )
        for role_key, role_permissions in ROLE_PERMISSIONS.items()
        for permission in role_permissions
    )
    database_session.commit()
    return roles


def make_member(
    database_session: Session,
    role: Role,
    suffix: str,
    tenant: Tenant | None = None,
) -> tuple[Tenant, User]:
    tenant = tenant or Tenant(name=f"Tenant {suffix}", slug=f"templates-{suffix}")
    user = User(
        name=f"User {suffix}",
        email=f"template.{suffix}@example.com",
        login_id=f"template.{suffix}",
        password_hash=hash_password(PASSWORD),
    )
    database_session.add_all([tenant, user])
    database_session.flush()
    database_session.add(
        TenantMembership(tenant_id=tenant.id, user_id=user.id, role_id=role.id)
    )
    database_session.commit()
    return tenant, user


def login(client: TestClient, user: User) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"identifier": user.login_id, "password": PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def setup_owner(
    client: TestClient,
    database_session: Session,
) -> tuple[dict[RoleKey, Role], Tenant, User, dict[str, str]]:
    roles = seed_roles(database_session)
    tenant, owner = make_member(database_session, roles[RoleKey.OWNER], "owner")
    return roles, tenant, owner, login(client, owner)


def create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Initial Outreach",
        "subject": "Hello {{first_name}}",
        "body_html": '<p style="color: #112233; text-align: left">Hi {{first_name}}, welcome to <strong>{{company}}</strong>.</p>',
        "signature_html": "<p>Regards,<br>Sales Team</p>",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_crud_preview_placeholders_and_html_escaping(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = setup_owner(client, database_session)
    created = client.post("/api/mail/templates", headers=auth, json=create_payload())
    assert created.status_code == 201
    template_id = created.json()["data"]["id"]
    assert created.json()["data"]["approval_status"] == "draft"
    assert created.json()["data"]["body_text"].startswith("Hi {{first_name}}")

    preview = client.post(
        f"/api/mail/templates/{template_id}/preview",
        headers=auth,
        json={"sample_values": {"first_name": "<Arif>", "company": "A & B"}},
    )
    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["rendered_subject"] == "Hello <Arif>"
    assert "&lt;Arif&gt;" in data["rendered_html"]
    assert "A &amp; B" in data["rendered_html"]
    assert data["missing_values"] == []

    literal = client.patch(
        f"/api/mail/templates/{template_id}",
        headers=auth,
        json={"subject": "Hi Arif", "body_html": "<p>Literal text is valid.</p>"},
    )
    assert literal.status_code == 200
    listed = client.get("/api/mail/templates?search=outreach", headers=auth)
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1


def test_unknown_placeholders_header_injection_and_unsafe_html_are_rejected(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = setup_owner(client, database_session)
    unknown = client.post(
        "/api/mail/templates",
        headers=auth,
        json=create_payload(body_html="<p>Hello {{arbitrary_code}}</p>"),
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["placeholders"] == ["arbitrary_code"]

    header = client.post(
        "/api/mail/templates",
        headers=auth,
        json=create_payload(subject="Safe\nBcc: attacker@example.com"),
    )
    assert header.status_code == 422

    dangerous = client.post(
        "/api/mail/templates",
        headers=auth,
        json=create_payload(body_html='<p onclick="steal()">Hello</p>'),
    )
    assert dangerous.status_code == 422

    remote_image = client.post(
        "/api/mail/templates",
        headers=auth,
        json=create_payload(body_html='<img src="https://example.com/tracker.png">'),
    )
    assert remote_image.status_code == 422

    expression = client.post(
        "/api/mail/templates",
        headers=auth,
        json=create_payload(body_html="<p>{{first_name.upper()}}</p>"),
    )
    assert expression.status_code == 422


def test_permissions_and_tenant_isolation(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, _, _, owner_auth = setup_owner(client, database_session)
    created = client.post(
        "/api/mail/templates", headers=owner_auth, json=create_payload()
    )
    template_id = created.json()["data"]["id"]

    _, other_owner = make_member(database_session, roles[RoleKey.OWNER], "other")
    other_auth = login(client, other_owner)
    assert client.get(
        f"/api/mail/templates/{template_id}", headers=other_auth
    ).status_code == 404

    _, readonly = make_member(database_session, roles[RoleKey.READONLY], "readonly")
    readonly_auth = login(client, readonly)
    assert client.get("/api/mail/templates", headers=readonly_auth).status_code == 403
    assert client.post(
        "/api/mail/templates", headers=readonly_auth, json=create_payload()
    ).status_code == 403


def test_approved_version_is_immutable_and_edit_creates_new_draft(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = setup_owner(client, database_session)
    created = client.post(
        "/api/mail/templates", headers=auth, json=create_payload()
    ).json()["data"]
    template_id = created["id"]
    approved = client.post(f"/api/mail/templates/{template_id}/approve", headers=auth)
    assert approved.status_code == 200
    assert approved.json()["data"]["version"] == 1
    assert approved.json()["data"]["approval_status"] == "approved"

    edited = client.patch(
        f"/api/mail/templates/{template_id}",
        headers=auth,
        json={"body_html": "<p>New draft content</p>"},
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["version"] == 2
    assert edited.json()["data"]["approval_status"] == "draft"

    old = client.get(f"/api/mail/templates/{template_id}?version=1", headers=auth)
    assert old.status_code == 200
    assert "welcome" in old.json()["data"]["body_html"]
    assert old.json()["data"]["approval_status"] == "approved"


def test_delegated_template_permissions_do_not_bypass_owner_approval(
    client: TestClient,
    database_session: Session,
) -> None:
    _, tenant, _, owner_auth = setup_owner(client, database_session)
    template_id = client.post(
        "/api/mail/templates", headers=owner_auth, json=create_payload()
    ).json()["data"]["id"]
    delegated_role = Role(
        tenant_id=tenant.id,
        key=f"custom-template-manager-{uuid.uuid4().hex}",
        name="Template Manager",
        normalized_name="template manager",
        is_system=False,
    )
    database_session.add(delegated_role)
    database_session.flush()
    delegated_permissions = database_session.scalars(
        select(Permission).where(
            Permission.key.in_(
                {
                    PermissionKey.MAIL_TEMPLATES_VIEW.value,
                    PermissionKey.MAIL_TEMPLATES_MANAGE.value,
                    PermissionKey.MAIL_TEMPLATES_APPROVE.value,
                }
            )
        )
    ).all()
    database_session.add_all(
        RolePermission(role_id=delegated_role.id, permission_id=permission.id)
        for permission in delegated_permissions
    )
    _, manager = make_member(
        database_session,
        delegated_role,
        "delegated-manager",
        tenant=tenant,
    )
    manager_auth = login(client, manager)
    response = client.post(
        f"/api/mail/templates/{template_id}/approve",
        headers=manager_auth,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Only a workspace Owner can approve templates"


def test_assets_validation_authorized_access_and_archive_preservation(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, _, _, auth = setup_owner(client, database_session)
    template_id = client.post(
        "/api/mail/templates", headers=auth, json=create_payload()
    ).json()["data"]["id"]
    storage = MemoryStorage()
    with use_storage(storage):
        uploaded = client.post(
            f"/api/mail/templates/{template_id}/assets",
            headers=auth,
            data={"kind": "signature_image"},
            files={"file": ("logo.png", PNG, "image/svg+xml")},
        )
        assert uploaded.status_code == 201
        asset = uploaded.json()["data"]
        assert asset["content_type"] == "image/png"
        assert "data-asset-id" in asset["placeholder_html"]

        updated = client.patch(
            f"/api/mail/templates/{template_id}",
            headers=auth,
            json={"signature_html": asset["placeholder_html"]},
        )
        assert updated.status_code == 200
        assert client.post(
            f"/api/mail/templates/{template_id}/approve", headers=auth
        ).status_code == 200

        content = client.get(asset["content_url"], headers=auth)
        assert content.status_code == 200
        assert content.content == PNG
        assert content.headers["x-content-type-options"] == "nosniff"

        blocked = client.post(
            f"/api/mail/templates/{template_id}/assets",
            headers=auth,
            data={"kind": "attachment"},
            files={"file": ("payload.svg", b"<svg><script>x</script></svg>", "image/svg+xml")},
        )
        assert blocked.status_code == 400

        assert client.delete(f"/api/mail/templates/{template_id}", headers=auth).status_code == 200
        assert database_session.scalar(select(MailTemplateAsset)) is not None
        assert storage.objects

        _, other_owner = make_member(database_session, roles[RoleKey.OWNER], "asset-other")
        other_auth = login(client, other_owner)
        assert client.get(asset["content_url"], headers=other_auth).status_code == 404


def test_preview_reports_missing_values_without_inventing_replacements(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = setup_owner(client, database_session)
    template_id = client.post(
        "/api/mail/templates", headers=auth, json=create_payload()
    ).json()["data"]["id"]
    preview = client.post(
        f"/api/mail/templates/{template_id}/preview",
        headers=auth,
        json={"sample_values": {"first_name": "Arif"}},
    )
    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["missing_values"] == [
        {
            "placeholder": "company",
            "message": "A value is required before this template can be sent",
        }
    ]
    assert "{{company}}" in data["rendered_html"]


def test_unreferenced_draft_asset_is_cleaned_from_storage(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = setup_owner(client, database_session)
    template_id = client.post(
        "/api/mail/templates", headers=auth, json=create_payload()
    ).json()["data"]["id"]
    storage = MemoryStorage()
    with use_storage(storage):
        uploaded = client.post(
            f"/api/mail/templates/{template_id}/assets",
            headers=auth,
            data={"kind": "attachment"},
            files={"file": ("proposal.pdf", PDF, "application/pdf")},
        )
        asset_id = uploaded.json()["data"]["id"]
        assert storage.objects
        removed = client.delete(
            f"/api/mail/templates/{template_id}/assets/{asset_id}",
            headers=auth,
        )
        assert removed.status_code == 200
        assert storage.objects == {}
        assert database_session.get(MailTemplateAsset, uuid.UUID(asset_id)) is None


def test_mime_composition_has_plain_html_cid_and_attachment(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = setup_owner(client, database_session)
    template_id = client.post(
        "/api/mail/templates", headers=auth, json=create_payload()
    ).json()["data"]["id"]
    storage = MemoryStorage()
    with use_storage(storage):
        inline = client.post(
            f"/api/mail/templates/{template_id}/assets",
            headers=auth,
            data={"kind": "inline_body"},
            files={"file": ("hero.png", PNG, "image/png")},
        ).json()["data"]
        client.post(
            f"/api/mail/templates/{template_id}/assets",
            headers=auth,
            data={"kind": "attachment"},
            files={"file": ("proposal.pdf", PDF, "application/pdf")},
        )
        client.patch(
            f"/api/mail/templates/{template_id}",
            headers=auth,
            json={"body_html": f'<p>Hello {{{{first_name}}}}</p>{inline["placeholder_html"]}'},
        )
        client.post(f"/api/mail/templates/{template_id}/approve", headers=auth)

        template = database_session.get(MailTemplate, uuid.UUID(template_id))
        version = database_session.scalar(
            select(MailTemplateVersion).where(
                MailTemplateVersion.template_id == template.id,
                MailTemplateVersion.version == template.current_version,
            )
        )
        assets = list_version_assets(database_session, version.id)
        message = asyncio.run(
            compose_approved_template_message(
                version=version,
                assets=assets,
                values={"first_name": "Arif", "company": "PowerLead"},
                sender_name="Sales",
                sender_email="sales@example.com",
                recipient_email="lead@example.com",
                reply_to=None,
                storage=storage,
            )
        )
        serialized = message.as_string()
        assert "multipart/mixed" in serialized
        assert "multipart/alternative" in serialized
        assert "text/plain" in serialized
        assert "text/html" in serialized
        html_part = next(
            part for part in message.walk() if part.get_content_type() == "text/html"
        )
        assert f"cid:template-asset-{inline['id']}" in html_part.get_content()
        assert any(
            part.get("Content-ID") == f"<template-asset-{inline['id']}>"
            for part in message.walk()
        )
        assert 'filename="proposal.pdf"' in serialized
