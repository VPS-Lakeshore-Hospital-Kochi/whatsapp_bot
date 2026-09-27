"""Turn a WhatsApp Cloud API webhook payload into simple inbound messages."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Inbound:
    message_id: str
    wa_id: str  # sender's phone number in international format, digits only
    kind: str  # "text", "reply" (button/list tap), or "unsupported"
    text: str = ""
    reply_id: str = ""  # id of the tapped button / list row


def parse_webhook(payload: dict) -> list[Inbound]:
    out: list[Inbound] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Delivery/read receipts arrive under "statuses" and are ignored here.
            for msg in value.get("messages", []):
                out.append(_parse_message(msg))
    return out


def _parse_message(msg: dict) -> Inbound:
    base = {"message_id": msg.get("id", ""), "wa_id": msg.get("from", "")}
    mtype = msg.get("type")
    if mtype == "text":
        return Inbound(kind="text", text=msg.get("text", {}).get("body", "").strip(), **base)
    if mtype == "interactive":
        inter = msg.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return Inbound(kind="reply", reply_id=reply.get("id", ""), text=reply.get("title", ""), **base)
    if mtype == "button":  # quick-reply button on a template message
        btn = msg.get("button", {})
        return Inbound(kind="reply", reply_id=btn.get("payload", ""), text=btn.get("text", ""), **base)
    return Inbound(kind="unsupported", **base)
