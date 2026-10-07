import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.utils.password import verify_password

REGISTER_URL = "/api/auth/register"
VALID_REQUEST = {
    "name": "  John Doe  ",
    "email": "John@Gmail.com",
    "password": "123456",
    "confirm_password": "123456",
    "login_id": "john.doe",
}


def test_successful_registration(
    client: TestClient,
    database_session: Session,
) -> None:
    response = client.post(REGISTER_URL, json=VALID_REQUEST)

    assert response.status_code == 201
    response_body = response.json()
    assert response_body["success"] is True
    assert response_body["message"] == "Registration successful"
    assert uuid.UUID(response_body["data"]["id"])
    assert response_body["data"]["name"] == "John Doe"
    assert response_body["data"]["email"] == "john@gmail.com"
    assert response_body["data"]["login_id"] == "john.doe"
    assert "password" not in response.text.lower()

    user = database_session.scalar(select(User).where(User.email == "john@gmail.com"))
    assert user is not None
    assert user.name == "John Doe"
    assert user.login_id == "john.doe"
    assert user.is_active is True


def test_duplicate_email(client: TestClient) -> None:
    first_response = client.post(REGISTER_URL, json=VALID_REQUEST)
    duplicate_request = {**VALID_REQUEST, "email": "JOHN@GMAIL.COM"}
    duplicate_response = client.post(REGISTER_URL, json=duplicate_request)

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 400
    assert duplicate_response.json() == {
        "success": False,
        "message": "Email already registered",
    }


def test_duplicate_login_id_is_case_insensitive(client: TestClient) -> None:
    first_response = client.post(REGISTER_URL, json=VALID_REQUEST)
    duplicate_response = client.post(
        REGISTER_URL,
        json={
            **VALID_REQUEST,
            "email": "different@gmail.com",
            "login_id": "JOHN.DOE",
        },
    )

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 400
    assert duplicate_response.json() == {
        "success": False,
        "message": "Login ID already registered",
    }


def test_registration_without_login_id_remains_supported(client: TestClient) -> None:
    request = {key: value for key, value in VALID_REQUEST.items() if key != "login_id"}
    response = client.post(REGISTER_URL, json=request)

    assert response.status_code == 201
    assert response.json()["data"]["login_id"].startswith("user-")


def test_public_registration_rejects_role_selection(client: TestClient) -> None:
    response = client.post(
        REGISTER_URL,
        json={**VALID_REQUEST, "role": "owner"},
    )

    assert response.status_code == 422
    assert any(error["field"] == "role" for error in response.json()["errors"])


def test_password_mismatch(client: TestClient) -> None:
    request = {**VALID_REQUEST, "confirm_password": "654321"}

    response = client.post(REGISTER_URL, json=request)

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert "must match" in response.text
    assert VALID_REQUEST["password"] not in response.text


def test_invalid_email(client: TestClient) -> None:
    request = {**VALID_REQUEST, "email": "not-an-email"}

    response = client.post(REGISTER_URL, json=request)

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert any(
        error["field"] == "email" for error in response.json()["errors"]
    )


def test_empty_name(client: TestClient) -> None:
    response = client.post(
        REGISTER_URL,
        json={**VALID_REQUEST, "name": "   "},
    )

    assert response.status_code == 422
    assert response.json()["success"] is False
    fields = {error["field"] for error in response.json()["errors"]}
    assert "name" in fields


def test_password_is_hashed_in_database(
    client: TestClient,
    database_session: Session,
) -> None:
    response = client.post(REGISTER_URL, json=VALID_REQUEST)

    assert response.status_code == 201
    user = database_session.scalar(select(User).where(User.email == "john@gmail.com"))
    assert user is not None
    assert user.password_hash != VALID_REQUEST["password"]
    assert verify_password(VALID_REQUEST["password"], user.password_hash)

