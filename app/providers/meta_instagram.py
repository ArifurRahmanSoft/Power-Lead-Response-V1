import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.meta_webhook import validate_meta_challenge, validate_meta_signature


META_OBJECT_ID_PATTERN = re.compile(r"^[0-9]{1,32}$")
GRAPH_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+$")


class MetaInstagramError(Exception):
    code = "meta_instagram_error"
    safe_message = "Meta Instagram request failed"
    http_status: int | None = None

    def __init__(
        self,
        *,
        code: str | None = None,
        safe_message: str | None = None,
        http_status: int | None = None,
    ) -> None:
        if code is not None:
            self.code = code
        if safe_message is not None:
            self.safe_message = safe_message
        self.http_status = http_status
        super().__init__(self.safe_message)


class MetaInstagramConfigurationError(MetaInstagramError):
    code = "meta_instagram_not_configured"
    safe_message = "Meta Instagram Unified Login is not configured"


class MetaInstagramRejectedError(MetaInstagramError):
    code = "meta_rejected"
    safe_message = "Meta rejected the Instagram reply"


class MetaInstagramAcceptanceUnknownError(MetaInstagramError):
    code = "acceptance_unknown"
    safe_message = "Instagram message acceptance could not be determined"


@dataclass(frozen=True)
class MetaInstagramResult:
    message_id: str
    recipient_id: str | None = None
    status: str = "accepted"


def safe_instagram_error(code: object) -> str:
    normalized = str(code or "meta_rejected")
    exact = {
        "100:33": (
            "The configured Meta token cannot access the Instagram professional "
            "account; reconnect with Unified Login and share this account"
        ),
    }
    if normalized in exact:
        return exact[normalized]
    base_code = normalized.split(":", 1)[0]
    known = {
        "3": "The Meta app cannot use this Instagram messaging operation",
        "4": "Meta rate-limited the Instagram request",
        "10": "The access token lacks the required Instagram permission",
        "17": "Meta rate-limited the Instagram request",
        "32": "Meta rate-limited the Instagram request",
        "100": "The Instagram account or recipient is unavailable",
        "190": "The Instagram access token is invalid or expired",
        "200": "Instagram messaging access is not authorized",
        "551": "The Instagram recipient is unavailable",
        "613": "Meta rate-limited the Instagram request",
        "80004": "Meta rate-limited the Instagram request",
    }
    return known.get(base_code, f"Meta rejected the Instagram reply (code {normalized})")


class MetaInstagramProvider:
    """Instagram API with Unified Login provider.

    Unified Login uses a Business Integration System User Access Token and
    graph.facebook.com. It must not be configured with an Instagram User token.
    """

    def __init__(self, configuration: Settings | None = None) -> None:
        config = configuration or get_settings()
        self.configuration_id = (config.meta_instagram_config_id or "").strip()
        self.account_id = (config.meta_instagram_account_id or "").strip()
        self.access_token = (
            config.meta_instagram_access_token.get_secret_value()
            if config.meta_instagram_access_token
            else ""
        )
        self.verify_token = (
            config.meta_instagram_verify_token.get_secret_value()
            if config.meta_instagram_verify_token
            else ""
        )
        self.app_secret = (
            config.meta_instagram_app_secret.get_secret_value()
            if config.meta_instagram_app_secret
            else ""
        )
        self.app_secret_source = (
            "META_INSTAGRAM_APP_SECRET"
            if config.meta_instagram_app_secret
            else "missing"
        )
        version = (config.meta_graph_api_version or "").strip()
        self.graph_api_version = version if GRAPH_VERSION_PATTERN.fullmatch(version) else ""
        self.timeout_seconds = config.meta_instagram_timeout_seconds

    @property
    def webhook_configured(self) -> bool:
        return bool(
            META_OBJECT_ID_PATTERN.fullmatch(self.account_id)
            and self.verify_token
            and self.app_secret
        )

    @property
    def send_api_configured(self) -> bool:
        return bool(
            META_OBJECT_ID_PATTERN.fullmatch(self.configuration_id)
            and META_OBJECT_ID_PATTERN.fullmatch(self.account_id)
            and self.access_token
            and self.graph_api_version
        )

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"

    def validate_challenge(self, *, mode: str, verify_token: str) -> bool:
        return validate_meta_challenge(
            expected_token=self.verify_token,
            mode=mode,
            supplied_token=verify_token,
        )

    def validate_signature(self, raw_body: bytes, signature: str) -> bool:
        return validate_meta_signature(
            app_secret=self.app_secret,
            raw_body=raw_body,
            signature=signature,
        )

    @staticmethod
    def _provider_error(response: httpx.Response) -> MetaInstagramRejectedError:
        code: str | None = None
        subcode: str | None = None
        try:
            document = response.json()
            error = document.get("error", {}) if isinstance(document, dict) else {}
            if isinstance(error, dict):
                code = str(error.get("code")) if error.get("code") is not None else None
                subcode = (
                    str(error.get("error_subcode"))
                    if error.get("error_subcode") is not None
                    else None
                )
        except (TypeError, ValueError):
            pass
        normalized = ":".join(part for part in (code, subcode) if part) or f"http_{response.status_code}"
        return MetaInstagramRejectedError(
            code=normalized,
            safe_message=safe_instagram_error(normalized),
            http_status=response.status_code,
        )

    async def send_text(self, *, to: str, body: str) -> MetaInstagramResult:
        if not self.send_api_configured:
            raise MetaInstagramConfigurationError()
        if not META_OBJECT_ID_PATTERN.fullmatch(to):
            raise MetaInstagramRejectedError(
                code="invalid_recipient",
                safe_message="The Instagram recipient identifier is invalid",
                http_status=400,
            )
        payload: dict[str, Any] = {
            "recipient": {"id": to},
            "message": {"text": body},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/{self.account_id}/messages",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaInstagramAcceptanceUnknownError() from exception
        except Exception as exception:
            raise MetaInstagramAcceptanceUnknownError() from exception
        if response.status_code >= 400:
            raise self._provider_error(response)
        try:
            document = response.json()
            message_id = str(document["message_id"])
            recipient_id = (
                str(document["recipient_id"])
                if document.get("recipient_id") is not None
                else None
            )
            if not message_id:
                raise ValueError("missing message id")
        except (KeyError, TypeError, ValueError) as exception:
            raise MetaInstagramAcceptanceUnknownError(
                safe_message=(
                    "Meta responded, but its Instagram acceptance response could not be recorded"
                )
            ) from exception
        return MetaInstagramResult(message_id=message_id, recipient_id=recipient_id)


def get_meta_instagram_provider() -> MetaInstagramProvider:
    return MetaInstagramProvider(get_settings())
