import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.models.authorization_audit import AuthorizationAudit
from app.models.customer import Customer, LeadConversion
from tests.test_leads import (
    add_membership,
    create_tenant,
    create_user,
    login_headers,
    owner_setup,
    seed_access,
)


def customer_payload(email: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "first_name": "  Arifur ",
        "last_name": " Rahman  ",
        "email": email,
        "designation": "Director",
        "company": "PowerLead",
        "country": "Bangladesh",
        "phone": "01712345678",
        "whatsapp_number": "01712345678",
        "city": "Dhaka",
        "state": "Dhaka",
        "company_website": "powerlead.example/contact?source=crm",
        "industry": "Software",
        "company_size": "11_50",
        "revenue_amount": "1500.00",
        "revenue_currency": "usd",
        "revenue_period": "annual",
        "linkedin_url": "linkedin.com/in/arifur",
        "facebook_url": "https://facebook.com/powerlead",
        "instagram_url": "instagram.com/powerlead",
        "x_url": "x.com/powerlead",
        "github_url": "github.com/powerlead",
        "service_requested": "Website Design",
        "notes": "Direct customer entry",
        "communication_consent_status": "granted",
    }
    payload.update(overrides)
    return payload


def test_direct_customer_create_update_origin_and_audit(
    client: TestClient,
    database_session: Session,
) -> None:
    _, tenant, owner, headers = owner_setup(client, database_session)
    response = client.post(
        "/api/customers",
        headers=headers,
        json=customer_payload(
            "  Direct.Customer@Example.COM ",
            assigned_user_id=str(owner.id),
        ),
    )
    assert response.status_code == 201, response.text
    customer = response.json()["data"]
    assert customer["first_name"] == "Arifur"
    assert customer["email"] == "direct.customer@example.com"
    assert customer["country"] == "Bangladesh"
    assert customer["phone"] == "+8801712345678"
    assert customer["whatsapp_number"] == "+8801712345678"
    assert customer["company_website"] == (
        "https://powerlead.example/contact?source=crm"
    )
    assert customer["linkedin_url"] == "https://linkedin.com/in/arifur"
    assert customer["revenue_currency"] == "USD"
    assert customer["source_type"] == "direct"
    assert customer["source_lead_id"] is None
    assert customer["converted_at"] is None
    assert customer["communication_consent_status"] == "granted"
    assert customer["consent_evidence"] is None

    detail = client.get(f"/api/customers/{customer['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["conversions"] == []
    listing = client.get("/api/customers?search=Website", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1

    evidence = client.patch(
        f"/api/customers/{customer['id']}",
        headers=headers,
        json={
            "row_version": customer["row_version"],
            "consent_evidence": "Existing customer-service consent record",
        },
    )
    assert evidence.status_code == 200, evidence.text
    updated = evidence.json()["data"]
    assert updated["row_version"] == 2

    edited = client.patch(
        f"/api/customers/{customer['id']}",
        headers=headers,
        json={
            "row_version": updated["row_version"],
            "company": "PowerLead Response",
            "notes": None,
        },
    )
    assert edited.status_code == 200, edited.text
    edited_data = edited.json()["data"]
    assert edited_data["company"] == "PowerLead Response"
    assert edited_data["notes"] is None
    assert edited_data["consent_evidence"] == (
        "Existing customer-service consent record"
    )
    assert edited_data["source_type"] == "direct"

    stale = client.patch(
        f"/api/customers/{customer['id']}",
        headers=headers,
        json={"row_version": 2, "company": "Stale update"},
    )
    assert stale.status_code == 409
    protected_origin = client.patch(
        f"/api/customers/{customer['id']}",
        headers=headers,
        json={"row_version": 3, "source_type": "lead"},
    )
    assert protected_origin.status_code == 422

    actions = set(
        database_session.scalars(
            select(AuthorizationAudit.action).where(
                AuthorizationAudit.tenant_id == tenant.id,
                AuthorizationAudit.action.in_(("customer.created", "customer.updated")),
            )
        ).all()
    )
    assert actions == {"customer.created", "customer.updated"}
    assert database_session.scalar(select(func.count(LeadConversion.id))) == 0


def test_customer_duplicate_validation_and_country_aware_numbers(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, headers = owner_setup(client, database_session)
    created = client.post(
        "/api/customers",
        headers=headers,
        json=customer_payload("duplicate.direct@example.com"),
    )
    assert created.status_code == 201
    customer_id = created.json()["data"]["id"]

    duplicate = client.post(
        "/api/customers",
        headers=headers,
        json=customer_payload(" DUPLICATE.DIRECT@EXAMPLE.COM "),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["existing_customer_id"] == customer_id

    second = client.post(
        "/api/customers",
        headers=headers,
        json=customer_payload("second.direct@example.com"),
    )
    assert second.status_code == 201
    duplicate_update = client.patch(
        f"/api/customers/{second.json()['data']['id']}",
        headers=headers,
        json={
            "row_version": second.json()["data"]["row_version"],
            "email": "DUPLICATE.DIRECT@EXAMPLE.COM",
        },
    )
    assert duplicate_update.status_code == 409
    assert duplicate_update.json()["detail"]["existing_customer_id"] == customer_id

    missing_required = client.post(
        "/api/customers",
        headers=headers,
        json={"last_name": "Required", "email": "missing.name@example.com"},
    )
    assert missing_required.status_code == 422

    ambiguous_phone = client.post(
        "/api/customers",
        headers=headers,
        json=customer_payload(
            "ambiguous.phone@example.com",
            country=None,
            phone="01712345678",
            whatsapp_number=None,
        ),
    )
    assert ambiguous_phone.status_code == 422
    assert "recognized country" in ambiguous_phone.text

    invalid_url = client.post(
        "/api/customers",
        headers=headers,
        json=customer_payload(
            "unsafe.url@example.com",
            company_website="javascript:alert(1)",
        ),
    )
    assert invalid_url.status_code == 422


def test_customer_permission_tenant_and_assigned_visibility(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_access(database_session)
    tenant = create_tenant(database_session, "direct-customer-scope")
    owner = create_user(database_session, "direct.customer.owner")
    staff = create_user(database_session, "direct.customer.staff")
    readonly = create_user(database_session, "direct.customer.readonly")
    add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    add_membership(database_session, staff, tenant, roles[RoleKey.STAFF])
    add_membership(database_session, readonly, tenant, roles[RoleKey.READONLY])
    owner_headers = login_headers(client, owner)
    staff_headers = login_headers(client, staff)
    readonly_headers = login_headers(client, readonly)

    assigned = client.post(
        "/api/customers",
        headers=owner_headers,
        json=customer_payload(
            "assigned.direct@example.com",
            assigned_user_id=str(staff.id),
        ),
    )
    assert assigned.status_code == 201, assigned.text
    customer = assigned.json()["data"]
    unassigned = client.post(
        "/api/customers",
        headers=owner_headers,
        json=customer_payload("unassigned.direct@example.com"),
    )
    assert unassigned.status_code == 201

    staff_list = client.get("/api/customers", headers=staff_headers)
    assert staff_list.status_code == 200
    assert [item["id"] for item in staff_list.json()["data"]["items"]] == [
        customer["id"]
    ]
    assert (
        client.get(
            f"/api/customers/{unassigned.json()['data']['id']}",
            headers=staff_headers,
        ).status_code
        == 404
    )

    forbidden_create = client.post(
        "/api/customers",
        headers=readonly_headers,
        json=customer_payload("forbidden.direct@example.com"),
    )
    assert forbidden_create.status_code == 403
    forbidden_update = client.patch(
        f"/api/customers/{customer['id']}",
        headers=readonly_headers,
        json={"row_version": customer["row_version"], "company": "Forbidden"},
    )
    assert forbidden_update.status_code == 403

    other_tenant = create_tenant(database_session, "direct-customer-other")
    other_owner = create_user(database_session, "direct.customer.other")
    add_membership(database_session, other_owner, other_tenant, roles[RoleKey.OWNER])
    other_headers = login_headers(client, other_owner)
    assert (
        client.get(f"/api/customers/{customer['id']}", headers=other_headers).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/customers/{customer['id']}",
            headers=other_headers,
            json={"row_version": customer["row_version"], "company": "Cross tenant"},
        ).status_code
        == 404
    )

    stored = database_session.get(Customer, uuid.UUID(customer["id"]))
    assert stored is not None
    assert stored.source_type == "direct"
