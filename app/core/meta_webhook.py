import hashlib
import hmac


def validate_meta_challenge(
    *,
    expected_token: str,
    mode: str,
    supplied_token: str,
) -> bool:
    return bool(
        expected_token
        and mode == "subscribe"
        and hmac.compare_digest(supplied_token, expected_token)
    )


def validate_meta_signature(
    *,
    app_secret: str,
    raw_body: bytes,
    signature: str,
) -> bool:
    if not app_secret or not signature.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:].casefold(), expected.casefold())
