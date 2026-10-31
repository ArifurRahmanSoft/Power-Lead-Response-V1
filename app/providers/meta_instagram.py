import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

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


class MetaInstagramOAuthError(MetaInstagramError):
    code = "meta_instagram_oauth_failed"
    safe_message = "Meta Instagram authorization failed"


@dataclass(frozen=True)
class MetaInstagramResult:
    message_id: str
    recipient_id: str | None = None
    status: str = "accepted"


@dataclass(frozen=True)
class MetaInstagramOAuthToken:
    access_token: str
    token_type: str | None
    expires_in: int | None


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
        self.app_id = (getattr(config, "meta_instagram_app_id", None) or "").strip()
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
        self.oauth_redirect_uri = (
            getattr(config, "meta_instagram_oauth_redirect_uri", None) or ""
        ).strip()

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
    def oauth_configured(self) -> bool:
        return bool(
            META_OBJECT_ID_PATTERN.fullmatch(self.app_id)
            and META_OBJECT_ID_PATTERN.fullmatch(self.configuration_id)
            and META_OBJECT_ID_PATTERN.fullmatch(self.account_id)
            and self.app_secret
            and self.oauth_redirect_uri.startswith("https://")
            and self.graph_api_version
        )

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"

    def with_access_token(self, access_token: str) -> "MetaInstagramProvider":
        provider = object.__new__(MetaInstagramProvider)
        provider.__dict__ = self.__dict__.copy()
        provider.access_token = access_token
        return provider

    def authorization_url(self, *, state: str) -> str:
        if not self.oauth_configured:
            raise MetaInstagramConfigurationError(
                safe_message="Meta Instagram Unified Login OAuth is not configured"
            )
        return "https://www.facebook.com/dialog/oauth?" + urlencode(
            {
                "client_id": self.app_id,
                "config_id": self.configuration_id,
                "redirect_uri": self.oauth_redirect_uri,
                "response_type": "code",
                "display": "popup",
                "prompt": "login",
                "state": state,
            }
        )

    async def exchange_code(self, code: str) -> MetaInstagramOAuthToken:
        if not self.oauth_configured:
            raise MetaInstagramConfigurationError(
                safe_message="Meta Instagram Unified Login OAuth is not configured"
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    f"{self.base_url}/oauth/access_token",
                    params={
                        "client_id": self.app_id,
                        "client_secret": self.app_secret,
                        "code": code,
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaInstagramOAuthError(
                code="oauth_exchange_unavailable",
                safe_message="Meta Instagram authorization exchange is temporarily unavailable",
            ) from exception
        if response.status_code >= 400:
            error = self._provider_error(response)
            raise MetaInstagramOAuthError(
                code=error.code,
                safe_message="Meta rejected the Instagram authorization code",
                http_status=error.http_status,
            )
        try:
            document = response.json()
            access_token = str(document["access_token"])
            if not access_token:
                raise ValueError("missing token")
            expires = document.get("expires_in")
            return MetaInstagramOAuthToken(
                access_token=access_token,
                token_type=str(document["token_type"]) if document.get("token_type") else None,
                expires_in=int(expires) if expires is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise MetaInstagramOAuthError(
                code="oauth_exchange_invalid_response",
                safe_message="Meta returned an invalid Instagram authorization response",
            ) from exception

    async def verify_account_access(self, access_token: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                debug_response = await client.get(
                    f"{self.base_url}/debug_token",
                    params={
                        "input_token": access_token,
                        "access_token": f"{self.app_id}|{self.app_secret}",
                    },
                )
                assigned_response = await client.get(
                    f"{self.base_url}/me/assigned_instagram_accounts",
                    params={"fields": "id", "limit": 100, "access_token": access_token},
                )
                account_response = await client.get(
                    f"{self.base_url}/{self.account_id}",
                    params={"fields": "id,username", "access_token": access_token},
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaInstagramOAuthError(
                code="oauth_verification_unavailable",
                safe_message="Meta Instagram account verification is temporarily unavailable",
            ) from exception
        if debug_response.status_code >= 400:
            error = self._provider_error(debug_response)
            raise MetaInstagramOAuthError(
                code=error.code,
                safe_message="Meta could not validate the Instagram authorization token",
                http_status=error.http_status,
            )
        try:
            token_data = debug_response.json().get("data", {})
            token_scopes = {str(item) for item in token_data.get("scopes", [])}
        except (TypeError, ValueError, AttributeError) as exception:
            raise MetaInstagramOAuthError(
                code="oauth_debug_invalid_response",
                safe_message="Meta returned an invalid token inspection response",
            ) from exception
        if not token_data.get("is_valid"):
            raise MetaInstagramOAuthError(
                code="oauth_token_invalid",
                safe_message="Meta returned an invalid Instagram authorization token",
                http_status=403,
            )
        if str(token_data.get("app_id") or "") != self.app_id:
            raise MetaInstagramOAuthError(
                code="oauth_app_mismatch",
                safe_message="The Instagram authorization token belongs to a different Meta app",
                http_status=403,
            )
        if str(token_data.get("type") or "").upper() != "SYSTEM_USER":
            raise MetaInstagramOAuthError(
                code="oauth_token_type_incompatible",
                safe_message="Meta did not return a Unified Login system-user token",
                http_status=403,
            )
        required_scopes = {"instagram_basic", "instagram_manage_messages"}
        if not required_scopes.issubset(token_scopes):
            raise MetaInstagramOAuthError(
                code="oauth_permissions_missing",
                safe_message="The Instagram authorization is missing required permissions",
                http_status=403,
            )
        if assigned_response.status_code >= 400:
            error = self._provider_error(assigned_response)
            raise MetaInstagramOAuthError(
                code=error.code,
                safe_message="The authorized Meta token cannot list assigned Instagram accounts",
                http_status=error.http_status,
            )
        try:
            assigned = assigned_response.json().get("data", [])
            assigned_ids = {
                str(item.get("id")) for item in assigned if isinstance(item, dict)
            }
        except (TypeError, ValueError, AttributeError) as exception:
            raise MetaInstagramOAuthError(
                code="oauth_assignment_invalid_response",
                safe_message="Meta returned an invalid Instagram account assignment response",
            ) from exception
        if self.account_id not in assigned_ids:
            raise MetaInstagramOAuthError(
                code="instagram_account_not_assigned",
                safe_message=(
                    "The configured Instagram professional account was not shared "
                    "during Unified Login"
                ),
                http_status=403,
            )
        if account_response.status_code >= 400:
            error = self._provider_error(account_response)
            raise MetaInstagramOAuthError(
                code=error.code,
                safe_message="The authorized Meta token cannot access the configured Instagram account",
                http_status=error.http_status,
            )
        try:
            account_document = account_response.json()
        except ValueError as exception:
            raise MetaInstagramOAuthError(
                code="oauth_account_invalid_response",
                safe_message="Meta returned an invalid Instagram account response",
            ) from exception
        if str(account_document.get("id") or "") != self.account_id:
            raise MetaInstagramOAuthError(
                code="instagram_account_mismatch",
                safe_message="Meta authorized a different Instagram professional account",
                http_status=403,
            )

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
