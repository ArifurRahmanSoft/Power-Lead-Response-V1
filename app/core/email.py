from typing import Protocol
from urllib.parse import quote

from app.core.config import settings


class EmailDeliveryError(Exception):
    """Raised by production email adapters when delivery cannot be submitted."""


class EmailProvider(Protocol):
    def send_password_reset_email(
        self,
        recipient_email: str,
        reset_url: str,
    ) -> None: ...


class DevelopmentEmailProvider:
    """No-op development adapter; it never logs email addresses or reset tokens."""

    def send_password_reset_email(
        self,
        recipient_email: str,
        reset_url: str,
    ) -> None:
        del recipient_email, reset_url


email_provider: EmailProvider = DevelopmentEmailProvider()


def send_password_reset_email(recipient_email: str, reset_token: str) -> None:
    reset_url = (
        f"{settings.frontend_url}{settings.password_reset_path}"
        f"?token={quote(reset_token, safe='')}"
    )
    email_provider.send_password_reset_email(recipient_email, reset_url)
