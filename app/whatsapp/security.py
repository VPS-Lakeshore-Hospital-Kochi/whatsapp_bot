import hashlib
import hmac


def verify_signature(raw_body: bytes, header_value: str | None, app_secret: str) -> bool:
    """Check Meta's X-Hub-Signature-256 header (HMAC-SHA256 of the raw body with the app secret)."""
    if not app_secret or not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix("sha256="))
