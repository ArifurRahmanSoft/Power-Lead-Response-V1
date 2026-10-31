from urllib.parse import parse_qsl, urlencode

from starlette.types import ASGIApp, Receive, Scope, Send


class SensitiveQueryRedactionMiddleware:
    """Redact Meta verification and OAuth values before access logging."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_webhook_verification = (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path")
            in {
                "/api/webhooks/meta/whatsapp",
                "/api/webhooks/meta/messenger",
                "/api/webhooks/meta/instagram",
            }
        )
        is_instagram_oauth_callback = (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path")
            == "/api/integrations/meta/instagram/oauth/callback"
        )
        if is_webhook_verification or is_instagram_oauth_callback:
            query = parse_qsl(
                scope.get("query_string", b"").decode("utf-8", errors="replace"),
                keep_blank_values=True,
            )
            original_token: str | None = None
            oauth_values: dict[str, str] = {}
            redacted: list[tuple[str, str]] = []
            for key, value in query:
                if key == "hub.verify_token":
                    original_token = value
                    redacted.append((key, "[REDACTED]"))
                elif is_instagram_oauth_callback and key in {"code", "state"}:
                    oauth_values[key] = value
                    redacted.append((key, "[REDACTED]"))
                else:
                    redacted.append((key, value))
            if original_token is not None:
                scope.setdefault("state", {})[
                    "meta_webhook_verify_token"
                ] = original_token
            if oauth_values:
                scope.setdefault("state", {})[
                    "meta_instagram_oauth_query"
                ] = oauth_values
            if original_token is not None or oauth_values:
                scope["query_string"] = urlencode(redacted, doseq=True).encode("utf-8")
        await self.app(scope, receive, send)
