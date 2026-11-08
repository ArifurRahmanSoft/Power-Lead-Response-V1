import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet

from app.core.config import Settings, get_settings
from app.core.meta_webhook import validate_meta_challenge, validate_meta_signature
from app.utils.phone import normalize_e164_phone


GRAPH_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+$")
META_OBJECT_ID_PATTERN = re.compile(r"^[0-9]{1,32}$")
META_WHATSAPP_EMBEDDED_SIGNUP_VERSIONS = frozenset({"v4", "v5"})
META_WHATSAPP_COEXISTENCE_FEATURE = "whatsapp_business_app_onboarding"


class MetaWhatsAppError(Exception):
    code = "meta_whatsapp_error"
    safe_message = "Meta WhatsApp operation failed"

    def __init__(
        self,
        *,
        code: str | None = None,
        safe_message: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code or self.code
        self.safe_message = safe_message or self.safe_message
        self.http_status = http_status
        super().__init__(self.safe_message)


class MetaWhatsAppConfigurationError(MetaWhatsAppError):
    code = "meta_whatsapp_not_configured"
    safe_message = "Meta WhatsApp Cloud API is not configured"


class MetaWhatsAppRejectedError(MetaWhatsAppError):
    code = "meta_rejected"
    safe_message = "Meta rejected the WhatsApp request"


class MetaWhatsAppAcceptanceUnknownError(MetaWhatsAppError):
    code = "acceptance_unknown"
    safe_message = "Meta WhatsApp acceptance could not be determined"


class MetaWhatsAppOAuthError(MetaWhatsAppError):
    code = "meta_whatsapp_oauth_failed"
    safe_message = "Meta WhatsApp authorization failed"


@dataclass(frozen=True)
class MetaWhatsAppResult:
    message_id: str
    status: str = "accepted"


@dataclass(frozen=True)
class MetaWhatsAppOAuthToken:
    access_token: str
    token_type: str | None
    expires_in: int | None


@dataclass(frozen=True)
class MetaWhatsAppDiscoveredAssets:
    business_id: str | None
    waba_id: str
    phone_number_id: str
    sender_address: str
    display_name: str | None
    is_on_biz_app: bool
    platform_type: str | None


@dataclass(frozen=True)
class MetaWhatsAppSettingDiagnostic:
    name: str
    present: bool
    format_valid: bool


@dataclass(frozen=True)
class MetaWhatsAppOAuthConfigurationDiagnostic:
    settings: tuple[MetaWhatsAppSettingDiagnostic, ...]
    missing_settings: tuple[str, ...]
    invalid_settings: tuple[str, ...]
    ready: bool


def normalize_meta_recipient(value: object) -> str:
    """Return Meta's digits-only international recipient format."""
    return normalize_e164_phone(value).lstrip("+")


def safe_meta_error(code: object) -> str:
    normalized = str(code or "meta_rejected")
    base_code = normalized.split(":", 1)[0]
    known = {
        "131026": "Meta could not deliver to this WhatsApp recipient",
        "131030": "The recipient is not in the permitted test-recipient list",
        "131047": "An approved WhatsApp template is required outside the service window",
        "131049": "Meta did not deliver this marketing message to preserve ecosystem quality",
        "132000": "The approved template parameters do not match the template definition",
        "132001": "The selected Meta WhatsApp template is unavailable for this language",
        "190": "Meta access-token authentication failed",
        "4": "Meta rate-limited the WhatsApp request",
        "80007": "Meta rate-limited the WhatsApp request",
    }
    return known.get(
        base_code,
        f"Meta rejected the WhatsApp request (code {normalized})",
    )


class MetaWhatsAppProvider:
    def __init__(self, configuration: Settings | None = None) -> None:
        config = configuration or get_settings()
        self.access_token = (
            config.meta_whatsapp_access_token.get_secret_value()
            if config.meta_whatsapp_access_token
            else ""
        )
        self.phone_number_id = (config.meta_whatsapp_phone_number_id or "").strip()
        self.waba_id = (config.meta_whatsapp_waba_id or "").strip()
        self.app_id = (config.meta_whatsapp_app_id or "").strip()
        self.embedded_signup_config_id = (
            config.meta_whatsapp_embedded_signup_config_id or ""
        ).strip()
        self.embedded_signup_version = (
            config.meta_whatsapp_embedded_signup_version or ""
        ).strip().casefold()
        self.oauth_redirect_uri = (config.meta_whatsapp_oauth_redirect_uri or "").strip()
        self.app_secret = (
            config.meta_app_secret.get_secret_value() if config.meta_app_secret else ""
        )
        self.verify_token = (
            config.meta_webhook_verify_token.get_secret_value()
            if config.meta_webhook_verify_token
            else ""
        )
        version = (config.meta_graph_api_version or "").strip()
        self.graph_api_version_raw = version
        self.graph_api_version = version if GRAPH_VERSION_PATTERN.fullmatch(version) else ""
        self.timeout_seconds = config.meta_whatsapp_timeout_seconds
        encryption_key = (
            config.smtp_credential_encryption_key.get_secret_value().strip()
            if config.smtp_credential_encryption_key
            else ""
        )
        self.credential_encryption_configured = bool(encryption_key)
        try:
            Fernet(encryption_key.encode("ascii"))
            self.credential_encryption_format_valid = True
        except (ValueError, UnicodeEncodeError):
            self.credential_encryption_format_valid = False

    @property
    def oauth_configured(self) -> bool:
        return self.oauth_configuration_diagnostic().ready

    def oauth_configuration_diagnostic(
        self,
    ) -> MetaWhatsAppOAuthConfigurationDiagnostic:
        redirect = urlsplit(self.oauth_redirect_uri)
        redirect_valid = bool(
            redirect.scheme == "https"
            and redirect.netloc
            and not redirect.query
            and not redirect.fragment
        )
        values = (
            MetaWhatsAppSettingDiagnostic(
                "META_WHATSAPP_APP_ID",
                bool(self.app_id),
                bool(META_OBJECT_ID_PATTERN.fullmatch(self.app_id)),
            ),
            MetaWhatsAppSettingDiagnostic(
                "META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID",
                bool(self.embedded_signup_config_id),
                bool(META_OBJECT_ID_PATTERN.fullmatch(self.embedded_signup_config_id)),
            ),
            MetaWhatsAppSettingDiagnostic(
                "META_WHATSAPP_EMBEDDED_SIGNUP_VERSION",
                bool(self.embedded_signup_version),
                self.embedded_signup_version
                in META_WHATSAPP_EMBEDDED_SIGNUP_VERSIONS,
            ),
            MetaWhatsAppSettingDiagnostic(
                "META_WHATSAPP_OAUTH_REDIRECT_URI",
                bool(self.oauth_redirect_uri),
                redirect_valid,
            ),
            MetaWhatsAppSettingDiagnostic(
                "META_APP_SECRET", bool(self.app_secret), bool(self.app_secret)
            ),
            MetaWhatsAppSettingDiagnostic(
                "SMTP_CREDENTIAL_ENCRYPTION_KEY",
                self.credential_encryption_configured,
                self.credential_encryption_format_valid,
            ),
            MetaWhatsAppSettingDiagnostic(
                "META_GRAPH_API_VERSION",
                bool(self.graph_api_version_raw),
                bool(self.graph_api_version),
            ),
        )
        missing = tuple(item.name for item in values if not item.present)
        invalid = tuple(
            item.name for item in values if item.present and not item.format_valid
        )
        return MetaWhatsAppOAuthConfigurationDiagnostic(
            settings=values,
            missing_settings=missing,
            invalid_settings=invalid,
            ready=not missing and not invalid,
        )

    def embedded_signup_options(self) -> dict[str, object]:
        if not self.embedded_signup_version:
            raise MetaWhatsAppConfigurationError(
                code="meta_whatsapp_embedded_signup_version_missing",
                safe_message="Meta WhatsApp Embedded Signup version is not configured",
            )
        if (
            self.embedded_signup_version
            not in META_WHATSAPP_EMBEDDED_SIGNUP_VERSIONS
        ):
            raise MetaWhatsAppConfigurationError(
                code="meta_whatsapp_embedded_signup_version_unsupported",
                safe_message="Meta WhatsApp Embedded Signup version is unsupported",
            )
        if not self.oauth_configured:
            raise MetaWhatsAppConfigurationError(
                safe_message="Meta WhatsApp Embedded Signup is not configured"
            )
        extras: dict[str, object] = {"version": self.embedded_signup_version}
        if self.embedded_signup_version == "v4":
            extras.update(
                {
                    "setup": {},
                    "sessionInfoVersion": "3",
                    "featureType": META_WHATSAPP_COEXISTENCE_FEATURE,
                }
            )
        return {
            "app_id": self.app_id,
            "configuration_id": self.embedded_signup_config_id,
            "graph_api_version": self.graph_api_version,
            "response_type": "code",
            "override_default_response_type": True,
            "extras": extras,
        }

    def with_channel_credentials(
        self,
        *,
        access_token: str,
        waba_id: str,
        phone_number_id: str,
    ) -> "MetaWhatsAppProvider":
        provider = object.__new__(MetaWhatsAppProvider)
        provider.__dict__ = self.__dict__.copy()
        provider.access_token = access_token
        provider.waba_id = waba_id
        provider.phone_number_id = phone_number_id
        return provider

    @property
    def configured(self) -> bool:
        return bool(
            self.access_token
            and self.phone_number_id
            and self.waba_id
            and self.graph_api_version
        )

    @property
    def webhook_configured(self) -> bool:
        return bool(self.app_secret and self.verify_token)

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

    def _headers(self) -> dict[str, str]:
        if not self.configured:
            raise MetaWhatsAppConfigurationError()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def exchange_embedded_signup_code(
        self,
        code: str,
    ) -> MetaWhatsAppOAuthToken:
        if not self.oauth_configured:
            raise MetaWhatsAppConfigurationError(
                safe_message="Meta WhatsApp Embedded Signup is not configured"
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
            raise MetaWhatsAppOAuthError(
                code="oauth_exchange_unavailable",
                safe_message="Meta WhatsApp authorization exchange is temporarily unavailable",
            ) from exception
        if response.status_code >= 400:
            error = self._provider_error(response)
            raise MetaWhatsAppOAuthError(
                code=error.code,
                safe_message="Meta rejected the WhatsApp authorization code",
                http_status=error.http_status,
            )
        try:
            document = response.json()
            access_token = str(document["access_token"])
            if not access_token:
                raise ValueError
            expires = document.get("expires_in")
            return MetaWhatsAppOAuthToken(
                access_token=access_token,
                token_type=(str(document.get("token_type")) if document.get("token_type") else None),
                expires_in=(int(expires) if expires is not None else None),
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise MetaWhatsAppOAuthError(
                code="oauth_exchange_invalid_response",
                safe_message="Meta returned an invalid WhatsApp authorization response",
            ) from exception

    async def discover_coexistence_assets(
        self,
        *,
        access_token: str,
        business_id: str | None,
        waba_id: str,
        phone_number_id: str,
    ) -> MetaWhatsAppDiscoveredAssets:
        if not all(
            META_OBJECT_ID_PATTERN.fullmatch(value)
            for value in (waba_id, phone_number_id)
        ):
            raise MetaWhatsAppOAuthError(
                code="invalid_embedded_signup_assets",
                safe_message="Meta returned invalid WhatsApp asset identifiers",
                http_status=400,
            )
        if business_id and not META_OBJECT_ID_PATTERN.fullmatch(business_id):
            raise MetaWhatsAppOAuthError(
                code="invalid_embedded_signup_assets",
                safe_message="Meta returned an invalid business identifier",
                http_status=400,
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                debug_response = await client.get(
                    f"{self.base_url}/debug_token",
                    params={
                        "input_token": access_token,
                        "access_token": f"{self.app_id}|{self.app_secret}",
                    },
                )
                waba_response = await client.get(
                    f"{self.base_url}/{waba_id}",
                    params={"fields": "id,name", "access_token": access_token},
                )
                phone_response = await client.get(
                    f"{self.base_url}/{waba_id}/phone_numbers",
                    params={
                        "fields": (
                            "id,display_phone_number,verified_name,platform_type,"
                            "is_on_biz_app,code_verification_status"
                        ),
                        "limit": 100,
                        "access_token": access_token,
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaWhatsAppOAuthError(
                code="asset_verification_unavailable",
                safe_message="Meta WhatsApp asset verification is temporarily unavailable",
            ) from exception
        for response in (debug_response, waba_response, phone_response):
            if response.status_code >= 400:
                error = self._provider_error(response)
                raise MetaWhatsAppOAuthError(
                    code=error.code,
                    safe_message="The authorized credential cannot access the selected WhatsApp assets",
                    http_status=error.http_status,
                )
        try:
            token_data = debug_response.json().get("data", {})
            scopes = {str(value) for value in token_data.get("scopes", [])}
            required = {"whatsapp_business_management", "whatsapp_business_messaging"}
            if not token_data.get("is_valid"):
                raise MetaWhatsAppOAuthError(
                    code="oauth_token_invalid",
                    safe_message="Meta returned an invalid WhatsApp authorization token",
                    http_status=403,
                )
            if str(token_data.get("app_id") or "") != self.app_id:
                raise MetaWhatsAppOAuthError(
                    code="oauth_app_mismatch",
                    safe_message="The WhatsApp authorization belongs to a different Meta app",
                    http_status=403,
                )
            if not required.issubset(scopes):
                raise MetaWhatsAppOAuthError(
                    code="oauth_permissions_missing",
                    safe_message="The WhatsApp authorization is missing required permissions",
                    http_status=403,
                )
            if str(waba_response.json().get("id") or "") != waba_id:
                raise MetaWhatsAppOAuthError(
                    code="waba_access_mismatch",
                    safe_message="Meta authorized a different WhatsApp Business Account",
                    http_status=403,
                )
            items = phone_response.json().get("data", [])
            phone = next(
                item
                for item in items
                if isinstance(item, dict) and str(item.get("id") or "") == phone_number_id
            )
            sender_address = normalize_e164_phone(phone.get("display_phone_number"))
        except MetaWhatsAppOAuthError:
            raise
        except (AttributeError, KeyError, StopIteration, TypeError, ValueError) as exception:
            raise MetaWhatsAppOAuthError(
                code="phone_asset_not_accessible",
                safe_message="The selected WhatsApp phone number is not accessible",
                http_status=403,
            ) from exception
        return MetaWhatsAppDiscoveredAssets(
            business_id=business_id,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            sender_address=sender_address,
            display_name=(str(phone.get("verified_name")) if phone.get("verified_name") else None),
            is_on_biz_app=phone.get("is_on_biz_app") is True,
            platform_type=(str(phone.get("platform_type")) if phone.get("platform_type") else None),
        )

    async def subscribe_waba(self, *, access_token: str, waba_id: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/{waba_id}/subscribed_apps",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaWhatsAppOAuthError(
                code="subscription_unavailable",
                safe_message="Meta WhatsApp webhook subscription is temporarily unavailable",
            ) from exception
        if response.status_code >= 400:
            error = self._provider_error(response)
            raise MetaWhatsAppOAuthError(
                code=error.code,
                safe_message="Meta rejected the WhatsApp webhook subscription",
                http_status=error.http_status,
            )
        try:
            if response.json().get("success") not in (True, "true"):
                raise ValueError
        except (AttributeError, TypeError, ValueError) as exception:
            raise MetaWhatsAppOAuthError(
                code="subscription_invalid_response",
                safe_message="Meta returned an invalid WhatsApp subscription response",
            ) from exception

    @staticmethod
    def _provider_error(response: httpx.Response) -> MetaWhatsAppRejectedError:
        code: str | None = None
        subcode: str | None = None
        try:
            error = response.json().get("error", {})
            code = str(error.get("code")) if error.get("code") is not None else None
            subcode = (
                str(error.get("error_subcode"))
                if error.get("error_subcode") is not None
                else None
            )
        except (TypeError, ValueError):
            code = None
            subcode = None
        normalized = ":".join(
            value for value in (code, subcode) if value
        ) or f"http_{response.status_code}"
        return MetaWhatsAppRejectedError(
            code=normalized,
            safe_message=safe_meta_error(normalized),
            http_status=response.status_code,
        )

    async def _send(self, payload: dict[str, Any]) -> MetaWhatsAppResult:
        url = f"{self.base_url}/{self.phone_number_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, headers=self._headers(), json=payload)
        except MetaWhatsAppConfigurationError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaWhatsAppAcceptanceUnknownError() from exception
        except Exception as exception:
            raise MetaWhatsAppAcceptanceUnknownError() from exception
        if response.status_code >= 400:
            raise self._provider_error(response)
        try:
            message_id = str(response.json()["messages"][0]["id"])
            if not message_id:
                raise ValueError("missing message id")
        except (KeyError, IndexError, TypeError, ValueError) as exception:
            raise MetaWhatsAppAcceptanceUnknownError(
                safe_message="Meta responded, but its WhatsApp acceptance response could not be recorded"
            ) from exception
        return MetaWhatsAppResult(message_id=message_id)

    async def send_text(self, *, to: str, body: str) -> MetaWhatsAppResult:
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": normalize_meta_recipient(to),
                "type": "text",
                "text": {"preview_url": False, "body": body},
            }
        )

    async def send_template(
        self,
        *,
        to: str,
        name: str,
        language: str,
        components: list[dict[str, Any]],
    ) -> MetaWhatsAppResult:
        template: dict[str, Any] = {
            "name": name,
            "language": {"code": language},
        }
        if components:
            template["components"] = components
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": normalize_meta_recipient(to),
                "type": "template",
                "template": template,
            }
        )

    async def list_templates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        url = f"{self.base_url}/{self.waba_id}/message_templates"
        bounded_limit = max(1, min(limit, 2000))
        output: list[dict[str, Any]] = []
        after: str | None = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                while len(output) < bounded_limit:
                    params = {
                        "fields": "id,name,status,language,category,components",
                        "limit": str(min(100, bounded_limit - len(output))),
                    }
                    if after:
                        params["after"] = after
                    response = await client.get(
                        url, headers=self._headers(), params=params
                    )
                    if response.status_code >= 400:
                        raise self._provider_error(response)
                    document = response.json()
                    data = document.get("data", [])
                    if not isinstance(data, list):
                        raise TypeError("template data must be a list")
                    output.extend(item for item in data if isinstance(item, dict))
                    cursors = (document.get("paging") or {}).get("cursors") or {}
                    next_after = cursors.get("after")
                    if not next_after or not data or next_after == after:
                        break
                    after = str(next_after)
        except (MetaWhatsAppConfigurationError, MetaWhatsAppRejectedError):
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaWhatsAppError(
                code="meta_unavailable",
                safe_message="Meta WhatsApp template information is temporarily unavailable",
            ) from exception
        except (TypeError, ValueError) as exception:
            raise MetaWhatsAppError(
                code="invalid_meta_response",
                safe_message="Meta returned an invalid template response",
            ) from exception
        return output[:bounded_limit]


def get_meta_whatsapp_provider() -> MetaWhatsAppProvider:
    return MetaWhatsAppProvider(get_settings())
