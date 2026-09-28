"""Emergency alerts to duty staff, with escalation.

When an emergency ticket opens, everyone in tier 1 gets a WhatsApp alert (a Meta-approved
template, because staff won't have messaged the bot in the last 24 hours) with an "I'm on it"
button. If nobody has taken the ticket after ALERT_ESCALATE_MINUTES, tier 2 is alerted, and so
on through the tiers. Tapping "I'm on it" (or claiming the ticket on the desk) stops escalation.

Optionally every alert is also POSTed, signed, to ALERT_WEBHOOK_URL so a phone-call or paging
system (e.g. an Exotel/Twilio call flow, or the hospital's own PA/pager bridge) can ring people:
a WhatsApp message alone can go unnoticed on a silent phone.

Who is in each tier comes from the duty roster (app/roster.py): whoever is on shift at the moment
each tier is due. The fixed ALERT_TIERS numbers are added after the roster's tiers as a last
resort, and are the only tiers if there is no roster or nobody is on shift.

Each (ticket, tier) is sent at most once, even with several app workers, because the send is
guarded by a set-if-absent key in the shared store (Redis in production).
"""

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings
from app.handoffs import Handoffs, Ticket
from app.roster import OnDuty, RosterStore
from app.safety import redact
from app.store import Store
from app.whatsapp.client import Sender

log = logging.getLogger(__name__)

ACK_PREFIX = "ack:"


def parse_tiers(raw: str) -> list[list[str]]:
    tiers = []
    for tier in raw.split(";"):
        numbers = ["".join(ch for ch in n if ch.isdigit()) for n in tier.split(",")]
        numbers = [n for n in numbers if n]
        if numbers:
            tiers.append(numbers)
    return tiers


def masked(wa_id: str) -> str:
    return f"+{wa_id[:2]} ••••• {wa_id[-4:]}" if len(wa_id) > 6 else wa_id


@dataclass
class Alerts:
    settings: Settings
    store: Store
    sender: Sender
    handoffs: Handoffs
    http: httpx.AsyncClient | None = None
    roster: RosterStore | None = None

    def __post_init__(self):
        self.tiers = parse_tiers(self.settings.alert_tiers)  # fixed fallback tiers
        self.reasons = {r.strip() for r in self.settings.alert_reasons.split(",") if r.strip()}
        self.escalate = timedelta(minutes=self.settings.alert_escalate_minutes)
        if not self.tiers and not (self.roster and self.roster.latest()):
            log.error("No duty roster and ALERT_TIERS is empty: emergency tickets will NOT alert anyone "
                      "outside the desk page")
        elif not self.tiers:
            log.warning("ALERT_TIERS is empty: if nobody is on the roster, emergencies will alert no one")
        if not self.settings.alert_template:
            log.error("ALERT_TEMPLATE is empty: WhatsApp alerts to duty staff cannot be sent")

    def tiers_at(self, now: datetime) -> list[list[OnDuty]]:
        """Roster tiers on shift now, in order, then the fixed ALERT_TIERS as a last resort."""
        tiers: list[list[OnDuty]] = []
        seen: set[str] = set()
        roster = self.roster.current() if self.roster else None
        if roster:
            for people in roster.on_duty(now).values():
                tiers.append(people)
                seen.update(p.phone for p in people)
        for numbers in self.tiers:
            label = "Fallback " if roster else ""  # with no roster these are simply the tiers
            extra = [OnDuty(n, f"{label}{masked(n)}") for n in numbers if n not in seen]
            if extra:
                tiers.append(extra)
                seen.update(numbers)
        return tiers

    async def can_ack(self, wa_id: str, ticket_id: int) -> bool:
        """Only people this ticket was actually sent to, or who are on duty now, can take it from an alert."""
        if wa_id in ((await self.store.get(f"alerted_to:{ticket_id}")) or []):
            return True
        return any(wa_id == p.phone for tier in self.tiers_at(datetime.now(timezone.utc)) for p in tier)

    # ------------------------------------------------------------ sending
    async def check_ticket(self, ticket: Ticket, now: datetime | None = None) -> None:
        """Send every tier that is due for this ticket and hasn't been sent yet."""
        if ticket.reason not in self.reasons or ticket.status != "open":
            return
        now = now or datetime.now(timezone.utc)
        tiers = self.tiers_at(now)  # whoever is on shift now, so a shift change mid-escalation is picked up
        if not tiers:
            if await self.store.set_if_absent(f"alerted:{ticket.id}:none", 7 * 86400):
                log.error("Emergency ticket #%s: nobody on the duty roster and no ALERT_TIERS", ticket.id)
                await asyncio.to_thread(self.handoffs.note, ticket.id, "Alerts",
                                        "No one to alert: nobody is on the duty roster right now and no "
                                        "fallback numbers are set.")
            return
        for index, people in enumerate(tiers):
            due = ticket.ts + self.escalate * index
            if now < due:
                break
            if await self.store.set_if_absent(f"alerted:{ticket.id}:{index}", 7 * 86400):
                await self._send_tier(ticket, index, people)

    async def check_all(self, now: datetime | None = None) -> None:
        tickets = await asyncio.to_thread(self.handoffs.unclaimed, self.reasons)
        for ticket in tickets:
            await self.check_ticket(ticket, now)

    async def _send_tier(self, ticket: Ticket, index: int, people: list[OnDuty]) -> None:
        numbers = [p.phone for p in people]
        alerted = (await self.store.get(f"alerted_to:{ticket.id}")) or []
        await self.store.set(f"alerted_to:{ticket.id}", sorted(set(alerted) | set(numbers)), 7 * 86400)
        preview = redact(ticket.summary)[:120] if self.settings.alert_include_preview else ""
        params = [
            str(ticket.id),
            ticket.reason_label,
            "+" + ticket.wa_id,  # staff need the full number to call back
            preview or "-",
        ]
        delivered, failed = [], []
        if self.settings.alert_template:
            for number in numbers:
                ok = await self.sender.template(number, self.settings.alert_template, self.settings.alert_template_lang,
                                                params, button_payload=f"{ACK_PREFIX}{ticket.id}")
                (delivered if ok else failed).append(number)
        else:
            failed = list(numbers)
        webhook_ok = await self._post_webhook(ticket, index, numbers, preview)

        tier_label = f"tier {index + 1}" + (" (escalated: nobody had taken it)" if index else "")
        names = {p.phone: p.name for p in people}
        note = f"Alert sent to {len(delivered)} of {len(numbers)} duty staff, {tier_label}"
        note += f": {', '.join(names[n] for n in delivered)}." if delivered else "."
        if failed:
            note += f" Not delivered to {', '.join(names[n] for n in failed)}."
        if webhook_ok is not None:
            note += " Phone/pager bridge notified." if webhook_ok else " Phone/pager bridge FAILED."
        await asyncio.to_thread(self.handoffs.note, ticket.id, "Alerts", note)
        if not delivered and not webhook_ok:
            log.error("Emergency ticket #%s: %s alert reached nobody", ticket.id, tier_label)

    async def _post_webhook(self, ticket: Ticket, index: int, numbers: list[str], preview: str) -> bool | None:
        url = self.settings.alert_webhook_url
        if not url:
            return None
        body = json.dumps({
            "ticket_id": ticket.id, "reason": ticket.reason, "tier": index + 1, "recipients": numbers,
            "caller": "+" + ticket.wa_id, "preview": preview, "opened_at": ticket.ts.isoformat(),
        }).encode()
        signature = hmac.new(self.settings.alert_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        http = self.http or httpx.AsyncClient(timeout=10)
        try:
            resp = await http.post(url, content=body, headers={
                "Content-Type": "application/json", "X-Lakeshore-Signature": f"sha256={signature}"})
            return resp.status_code < 400
        except httpx.HTTPError as e:
            log.error("Alert webhook failed: %s", e)
            return False
        finally:
            if self.http is None:
                await http.aclose()

    # ------------------------------------------------------------ background loop
    async def run_forever(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:
                log.exception("Escalation check failed")
            await asyncio.sleep(self.settings.alert_check_seconds)
