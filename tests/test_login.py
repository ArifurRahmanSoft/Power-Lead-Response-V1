import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.jwt import decode_access_token
from app.models.user import User
from app.utils.password import hash_password

LOGIN_URL = "/api/auth/login"
USER_EMAIL = "user@gmail.com"
USER_LOGIN_ID = "john.doe"
USER_PASSWORD = "12345678"


def create_user(database_session: Session) -> User:
    user = User(
        name="John Doe",
        email=USER_EMAIL,
        login_id=USER_LOGIN_ID,
        password_hash=hash_password(USER_PASSWORD),
    )
    database_session.add(user)
    database_session.commit()
    database_session.refresh(user)
    return user


def assert_successful_onboarding_login(response: object, user: User) -> None:
    assert response.status_code == 200
    response_body = response.json()
    assert response_body["success"] is True
    assert response_body["message"] == "Login successful"
    assert response_body["data"]["token_type"] == "bearer"
    assert response_body["data"]["auth_status"] == "onboarding_pending"
    assert response_body["data"]["user"] == {
        "id": str(user.id),
        "login_id": USER_LOGIN_ID,
        "name": "John Doe",
        "email": USER_EMAIL,
    }
    assert response_body["data"]["selected_workspace"] is None
    assert response_body["data"]["permissions"] == []
    assert "password" not in response.text.lower()

    claims = decode_access_token(response_body["data"]["access_token"])
    assert uuid.UUID(claims["sub"]) == user.id
    assert claims["email"] == USER_EMAIL
    assert claims["scope"] == "onboarding"
    assert "exp" in claims


def test_email_identifier_login(
    client: TestClient,
    database_session: Session,
) -> None:
    user = create_user(database_session)
    response = client.post(
        LOGIN_URL,
        json={"identifier": "  USER@GMAIL.COM ", "password": USER_PASSWORD},
    )
    assert_successful_onboarding_login(response, user)


def test_login_id_login(
    client: TestClient,
    database_session: Session,
) -> None:
    user = create_user(database_session)
    response = client.post(
        LOGIN_URL,
        json={"identifier": "  JOHN.DOE ", "password": USER_PASSWORD},
    )
    assert_successful_onboarding_login(response, user)


def test_legacy_email_request_remains_supported(
    client: TestClient,
    database_session: Session,
) -> None:
    user = create_user(database_session)
    response = client.post(
        LOGIN_URL,
        json={"email": USER_EMAIL, "password": USER_PASSWORD},
    )
    assert_successful_onboarding_login(response, user)


def test_identifier_shorter_than_five_characters(client: TestClient) -> None:
    response = client.post(
        LOGIN_URL,
        json={"identifier": "abcd", "password": USER_PASSWORD},
    )
    assert response.status_code == 422
    assert any(error["field"] == "identifier" for error in response.json()["errors"])


def test_wrong_password(
    client: TestClient,
    database_session: Session,
) -> None:
    create_user(database_session)
    response = client.post(
        LOGIN_URL,
        json={"identifier": USER_LOGIN_ID, "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "success": False,
        "message": "Invalid identifier or password",
    }


def test_unknown_identifier_uses_same_error(client: TestClient) -> None:
    response = client.post(
        LOGIN_URL,
        json={"identifier": "unknown.user", "password": USER_PASSWORD},
    )
    assert response.status_code == 401
    assert response.json() == {
        "success": False,
        "message": "Invalid identifier or password",
    }


def test_empty_password(client: TestClient) -> None:
    response = client.post(
        LOGIN_URL,
        json={"identifier": USER_LOGIN_ID, "password": ""},
    )
    assert response.status_code == 422
    assert any(error["field"] == "password" for error in response.json()["errors"])


def test_password_is_not_trimmed_or_modified(
    client: TestClient,
    database_session: Session,
) -> None:
    password_with_spaces = "  exact-password  "
    user = User(
        name="Exact Password",
        email="exact@example.com",
        login_id="exact.user",
        password_hash=hash_password(password_with_spaces),
    )
    database_session.add(user)
    database_session.commit()

    exact_response = client.post(
        LOGIN_URL,
        json={"identifier": user.login_id, "password": password_with_spaces},
    )
    trimmed_response = client.post(
        LOGIN_URL,
        json={"identifier": user.login_id, "password": password_with_spaces.strip()},
    )

    assert exact_response.status_code == 200
    assert trimmed_response.status_code == 401
