"""Outbound WhatsApp messages via the Cloud API.

Only session messages (replies inside the 24-hour customer-service window) are sent here.
Bot-initiated messages (reminders, report-ready alerts) must use Meta-approved templates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)


@dataclass
class Button:
    id: str
    title: str  # max 20 chars


@dataclass
class ListRow:
    id: str
    title: str  # max 24 chars
    description: str = ""  # max 72 chars


class Sender:
    """Interface the router talks to. Tests use RecordingSender."""

    async def text(self, to: str, body: str) -> bool: ...  # True if WhatsApp accepted it
    async def buttons(self, to: str, body: str, buttons: list[Button]) -> None: ...
    async def list(self, to: str, body: str, button_label: str, rows: list[ListRow]) -> None: ...
    async def template(self, to: str, name: str, lang: str, params: list[str],
                       button_payload: str = "") -> bool: ...  # True if WhatsApp accepted it


class CloudApiSender(Sender):
    def __init__(self, access_token: str, phone_number_id: str, graph_version: str):
        self._dry_run = not access_token
        self._url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
        self._http = httpx.AsyncClient(
            timeout=10, headers={"Authorization": f"Bearer {access_token}"}
        )

    async def _send(self, to: str, payload: dict) -> bool:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **payload}
        if self._dry_run:
            log.info("DRY-RUN outbound: %s", body)
            return True
        try:
            resp = await self._http.post(self._url, json=body)
        except httpx.HTTPError as e:
            log.error("WhatsApp send failed: %s", e)
            return False
        if resp.status_code >= 400:
            log.error("WhatsApp send failed %s: %s", resp.status_code, resp.text)
            return False
        return True

    async def text(self, to: str, body: str) -> bool:
        return await self._send(to, {"type": "text", "text": {"body": body[:4096], "preview_url": False}})

    async def template(self, to: str, name: str, lang: str, params: list[str], button_payload: str = "") -> bool:
        """A Meta-approved template: the only way to message someone who hasn't written in 24 hours."""
        components = [{"type": "body", "parameters": [{"type": "text", "text": p[:1000]} for p in params]}]
        if button_payload:
            components.append({"type": "button", "sub_type": "quick_reply", "index": "0",
                               "parameters": [{"type": "payload", "payload": button_payload}]})
        return await self._send(to, {"type": "template", "template": {
            "name": name, "language": {"code": lang}, "components": components}})

    async def buttons(self, to: str, body: str, buttons: list[Button]) -> None:
        await self._send(to, {
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": b.id, "title": b.title[:20]}} for b in buttons[:3]
                ]},
            },
        })

    async def list(self, to: str, body: str, button_label: str, rows: list[ListRow]) -> None:
        await self._send(to, {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body[:1024]},
                "action": {
                    "button": button_label[:20],
                    "sections": [{"title": "Options", "rows": [
                        {"id": r.id, "title": r.title[:24], "description": r.description[:72]}
                        for r in rows[:10]
                    ]}],
                },
            },
        })


@dataclass
class RecordingSender(Sender):
    """Collects outbound messages in memory (tests, evals, local CLI)."""

    sent: list[dict] = field(default_factory=list)

    fail_next: bool = False  # tests: simulate WhatsApp rejecting the next text

    async def text(self, to, body):
        if self.fail_next:
            self.fail_next = False
            return False
        self.sent.append({"to": to, "type": "text", "body": body})
        return True

    async def buttons(self, to, body, buttons):
        self.sent.append({"to": to, "type": "buttons", "body": body, "ids": [b.id for b in buttons]})

    async def list(self, to, body, button_label, rows):
        self.sent.append({"to": to, "type": "list", "body": body, "ids": [r.id for r in rows]})

    fail_numbers: set = field(default_factory=set)  # tests: template sends to these numbers fail

    async def template(self, to, name, lang, params, button_payload=""):
        if to in self.fail_numbers:
            return False
        self.sent.append({"to": to, "type": "template", "name": name, "params": params, "payload": button_payload})
        return True

    def last_body(self) -> str:
        return self.sent[-1]["body"] if self.sent else ""
