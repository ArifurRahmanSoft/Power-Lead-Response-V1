import re
import uuid

LOGIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{5,50}$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_login_id(login_id: str) -> str:
    return login_id.strip().lower()


def generate_login_id() -> str:
    return f"user-{uuid.uuid4().hex}"
