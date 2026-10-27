from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.models.authorization_audit import AuthorizationAudit
from app.models.role import Role
from tests.test_mail_templates import (
    create_payload as email_template_payload,
    login,
    make_member,
    setup_owner,
)
from tests.test_outbound_messages import setup as setup_outbound


def message_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Bangla Follow-up",
        "body": "হ্যালো {{first_name}},\nআপনার কোম্পানি {{company}}।",
    }
    payload.update(overrides)
    return payload


def test_message_template_crud_search_unicode_preview_and_archive(
    client: TestClient,
    database_session: Session,
) -> None:
    _, tenant, owner, auth = setup_owner(client, database_session)
    created = client.post(
        "/api/message-templates",
        headers=auth,
        json=message_payload(name="  Bangla   Follow-up  "),
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    template_id = data["id"]
    assert data["tenant_id"] == str(tenant.id)
    assert data["name"] == "Bangla Follow-up"
    assert data["body"] == "হ্যালো {{first_name}},\nআপনার কোম্পানি {{company}}।"
    assert data["category"] == "message"
    assert data["created_by_user_id"] == str(owner.id)
    assert data["created_at"] and data["updated_at"]

    preview = client.post(
        f"/api/message-templates/{template_id}/preview",
        headers=auth,
        json={"sample_values": {"first_name": "আরিফ", "company": "পাওয়ারলিড"}},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["rendered_body"] == (
        "হ্যালো আরিফ,\nআপনার কোম্পানি পাওয়ারলিড।"
    )
    assert preview.json()["data"]["missing_values"] == []

    updated_body = "Hi Arif\nHi {{first_name}}"
    updated = client.patch(
        f"/api/message-templates/{template_id}",
        headers=auth,
        json={"body": updated_body},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["body"] == updated_body

    missing = client.post(
        f"/api/message-templates/{template_id}/preview",
        headers=auth,
        json={"sample_values": {}},
    )
    assert missing.status_code == 200
    assert missing.json()["data"]["rendered_body"] == updated_body
    assert missing.json()["data"]["missing_values"] == [
        {
            "placeholder": "first_name",
            "message": "No value was supplied for this placeholder",
        }
    ]

    listed = client.get(
        "/api/message-templates",
        headers=auth,
        params={"search": "bangla", "page": 1, "size": 10},
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1

    archived = client.delete(
        f"/api/message-templates/{template_id}", headers=auth
    )
    assert archived.status_code == 200
    assert client.get(
        f"/api/message-templates/{template_id}", headers=auth
    ).status_code == 404
    archived_list = client.get(
        "/api/message-templates?include_archived=true", headers=auth
    )
    assert archived_list.json()["data"]["items"][0]["archived"] is True

    actions = set(
        database_session.scalars(
            select(AuthorizationAudit.action).where(
                AuthorizationAudit.tenant_id == tenant.id
            )
        ).all()
    )
    assert {
        "message_template.created",
        "message_template.updated",
        "message_template.archived",
    } <= actions


def test_message_template_validation_and_case_insensitive_uniqueness(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = setup_owner(client, database_session)
    assert client.post(
        "/api/message-templates",
        headers=auth,
        json=message_payload(name="   "),
    ).status_code == 422
    assert client.post(
        "/api/message-templates",
        headers=auth,
        json=message_payload(body=" \n\t "),
    ).status_code == 422
    assert client.post(
        "/api/message-templates",
        headers=auth,
        json=message_payload(body="x" * 10_001),
    ).status_code == 422
    unknown = client.post(
        "/api/message-templates",
        headers=auth,
        json=message_payload(body="Hi {{arbitrary_expression}}"),
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["placeholders"] == ["arbitrary_expression"]

    first = client.post(
        "/api/message-templates", headers=auth, json=message_payload()
    )
    assert first.status_code == 201
    duplicate = client.post(
        "/api/message-templates",
        headers=auth,
        json=message_payload(name="  BANGLA follow-UP "),
    )
    assert duplicate.status_code == 409


def test_message_template_lead_preview_tenant_scope_and_permissions(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, lead, _, _, _, owner_auth = setup_outbound(
        client, database_session
    )
    created = client.post(
        "/api/message-templates",
        headers=owner_auth,
        json=message_payload(body="Hi {{first_name}} from {{country}}"),
    )
    assert created.status_code == 201, created.text
    template_id = created.json()["data"]["id"]
    lead.country = "Bangladesh"
    database_session.commit()
    preview = client.post(
        f"/api/message-templates/{template_id}/preview",
        headers=owner_auth,
        json={"lead_id": str(lead.id)},
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["rendered_body"] == "Hi Arif from Bangladesh"

    owner_role = database_session.scalar(
        select(Role).where(Role.key == RoleKey.OWNER.value)
    )
    readonly_role = database_session.scalar(
        select(Role).where(Role.key == RoleKey.READONLY.value)
    )
    other_tenant, other_owner = make_member(
        database_session, owner_role, "message-other"
    )
    _, readonly_user = make_member(
        database_session, readonly_role, "message-readonly", tenant=tenant
    )
    other_auth = login(client, other_owner)
    readonly_auth = login(client, readonly_user)
    assert other_tenant.id != tenant.id
    assert client.get(
        f"/api/message-templates/{template_id}", headers=other_auth
    ).status_code == 404
    assert client.post(
        f"/api/message-templates/{template_id}/preview",
        headers=other_auth,
        json={"lead_id": str(lead.id)},
    ).status_code == 404
    assert client.get(
        "/api/message-templates", headers=readonly_auth
    ).status_code == 403
    assert client.post(
        "/api/message-templates",
        headers=readonly_auth,
        json=message_payload(name="Denied"),
    ).status_code == 403


def test_existing_email_template_api_remains_compatible(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = setup_owner(client, database_session)
    message_response = client.post(
        "/api/message-templates",
        headers=auth,
        json=message_payload(name="Initial Outreach"),
    )
    assert message_response.status_code == 201, message_response.text
    response = client.post(
        "/api/mail/templates", headers=auth, json=email_template_payload()
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["subject"] == "Hello {{first_name}}"
