from urllib.parse import parse_qsl, urlencode

from starlette.types import ASGIApp, Receive, Scope, Send


class SensitiveQueryRedactionMiddleware:
    """Redact Meta's verification token before the server access log is emitted."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path")
            in {
                "/api/webhooks/meta/whatsapp",
                "/api/webhooks/meta/messenger",
                "/api/webhooks/meta/instagram",
            }
        ):
            query = parse_qsl(
                scope.get("query_string", b"").decode("utf-8", errors="replace"),
                keep_blank_values=True,
            )
            original_token: str | None = None
            redacted: list[tuple[str, str]] = []
            for key, value in query:
                if key == "hub.verify_token":
                    original_token = value
                    redacted.append((key, "[REDACTED]"))
                else:
                    redacted.append((key, value))
            if original_token is not None:
                scope.setdefault("state", {})[
                    "meta_webhook_verify_token"
                ] = original_token
                scope["query_string"] = urlencode(redacted, doseq=True).encode("utf-8")
        await self.app(scope, receive, send)
