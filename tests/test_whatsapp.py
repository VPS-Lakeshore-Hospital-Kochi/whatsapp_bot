import hashlib
import hmac

from app.whatsapp.parse import parse_webhook
from app.whatsapp.security import verify_signature


def test_signature_valid_and_invalid():
    body = b'{"entry": []}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, "secret")
    assert not verify_signature(body, sig, "other-secret")
    assert not verify_signature(body + b" ", sig, "secret")
    assert not verify_signature(body, None, "secret")
    assert not verify_signature(body, sig, "")  # no secret configured -> reject everything


def test_parse_text_and_interactive():
    payload = {"entry": [{"changes": [{"value": {
        "messages": [
            {"id": "a", "from": "9199", "type": "text", "text": {"body": " hello "}},
            {"id": "b", "from": "9199", "type": "interactive",
             "interactive": {"type": "list_reply", "list_reply": {"id": "appts", "title": "My appointments"}}},
            {"id": "c", "from": "9199", "type": "image", "image": {}},
        ],
        "statuses": [{"id": "x", "status": "read"}],
    }}]}]}
    msgs = parse_webhook(payload)
    assert [(m.kind, m.text, m.reply_id) for m in msgs] == [
        ("text", "hello", ""), ("reply", "My appointments", "appts"), ("unsupported", "", "")
    ]
