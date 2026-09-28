"""Handoff tickets: everything the bot passes to a person, and the conversation that follows.

A ticket is opened by the bot (emergency, clinical question, booking request, "talk to a
person", or an answer it wouldn't guess). Desk staff work tickets on the dashboard (/desk):
claim, reply on WhatsApp, resolve. Once a staff member replies, the ticket is "live": the
patient's further messages are added to the ticket instead of going to the bot, until the
ticket is resolved or the patient types "menu".

The thread keeps message text as sent, because staff need it to help the patient; access is
limited to signed-in desk staff. Agree the retention period with the DPO.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, and_, case, func, insert, select, update,
)
from sqlalchemy.engine import Engine

from app.safety import redact

metadata = MetaData()

handoffs = Table(
    "handoffs", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("wa_id", String(20), nullable=False, index=True),  # needed so a person can call/message back
    Column("role", String(16), nullable=False),
    Column("reason", String(32), nullable=False),
    Column("summary", Text),  # redacted preview for the queue
    Column("status", String(16), nullable=False, default="open"),  # open / claimed / resolved
    Column("assigned_to", String(80)),
    Column("live", Boolean, nullable=False, default=False),
    Column("last_inbound_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("resolved_at", DateTime(timezone=True)),
    Column("resolution_note", Text),
)

handoff_messages = Table(
    "handoff_messages", metadata,
    Column("id", Integer, primary_key=True),
    Column("handoff_id", Integer, nullable=False, index=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("direction", String(8), nullable=False),  # "in" (from the person) / "out" (from staff) / "note"
    Column("author", String(80)),
    Column("body", Text, nullable=False),
)

# Lower number = shown first.
PRIORITY = {"emergency": 0, "clinical": 1, "needs_human": 2, "booking": 3, "requested": 4}
REASON_LABEL = {
    "emergency": "Emergency",
    "clinical": "Medical question",
    "needs_human": "Needs a person",
    "booking": "Booking request",
    "requested": "Asked for a person",
}
WHATSAPP_WINDOW = timedelta(hours=24)


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything we store is UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Ticket:
    id: int
    ts: datetime
    wa_id: str
    role: str
    reason: str
    summary: str
    status: str
    assigned_to: str | None
    live: bool
    last_inbound_at: datetime | None
    resolved_at: datetime | None
    resolution_note: str | None

    @property
    def reason_label(self) -> str:
        return REASON_LABEL.get(self.reason, self.reason)

    def can_reply(self, now: datetime | None = None) -> bool:
        """WhatsApp only allows free-form replies within 24h of the person's last message."""
        now = now or datetime.now(timezone.utc)
        return self.last_inbound_at is not None and now - self.last_inbound_at < WHATSAPP_WINDOW

    @classmethod
    def from_row(cls, row) -> "Ticket":
        m = row._mapping
        return cls(
            id=m["id"], ts=_utc(m["ts"]), wa_id=m["wa_id"], role=m["role"], reason=m["reason"],
            summary=m["summary"] or "", status=m["status"], assigned_to=m["assigned_to"], live=bool(m["live"]),
            last_inbound_at=_utc(m["last_inbound_at"]), resolved_at=_utc(m["resolved_at"]),
            resolution_note=m["resolution_note"],
        )


@dataclass
class Message:
    ts: datetime
    direction: str
    author: str | None
    body: str


class Handoffs:
    def __init__(self, engine: Engine):
        self.engine = engine
        metadata.create_all(engine)

    # ---------------------------------------------------------------- bot side
    def open(self, wa_id: str, role: str, reason: str, text: str) -> int:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            ticket_id = int(conn.execute(insert(handoffs).values(
                ts=now, wa_id=wa_id, role=role, reason=reason, summary=redact(text)[:500], status="open",
                live=False, last_inbound_at=now, updated_at=now,
            )).inserted_primary_key[0])
            if text.strip():
                conn.execute(insert(handoff_messages).values(
                    handoff_id=ticket_id, ts=now, direction="in", author=None, body=text,
                ))
        return ticket_id

    def live_ticket_for(self, wa_id: str) -> Ticket | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(handoffs).where(and_(handoffs.c.wa_id == wa_id, handoffs.c.live.is_(True),
                                            handoffs.c.status != "resolved"))
                .order_by(handoffs.c.id.desc()).limit(1)
            ).first()
        return Ticket.from_row(row) if row else None

    def add_inbound(self, ticket_id: int, text: str) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(insert(handoff_messages).values(
                handoff_id=ticket_id, ts=now, direction="in", author=None, body=text))
            conn.execute(update(handoffs).where(handoffs.c.id == ticket_id)
                         .values(last_inbound_at=now, updated_at=now))

    def end_live(self, ticket_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(handoffs).where(handoffs.c.id == ticket_id)
                         .values(live=False, updated_at=datetime.now(timezone.utc)))

    def note(self, ticket_id: int, author: str, body: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(handoff_messages).values(
                handoff_id=ticket_id, ts=datetime.now(timezone.utc), direction="note", author=author, body=body))

    def unclaimed(self, reasons: set[str]) -> list[Ticket]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(handoffs).where(and_(
                handoffs.c.status == "open", handoffs.c.reason.in_(reasons))).order_by(handoffs.c.id)).all()
        return [Ticket.from_row(r) for r in rows]

    # ---------------------------------------------------------------- desk side
    def get(self, ticket_id: int) -> Ticket | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(handoffs).where(handoffs.c.id == ticket_id)).first()
        return Ticket.from_row(row) if row else None

    def messages(self, ticket_id: int) -> list[Message]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(handoff_messages).where(handoff_messages.c.handoff_id == ticket_id)
                                .order_by(handoff_messages.c.id)).all()
        return [Message(_utc(r.ts), r.direction, r.author, r.body) for r in rows]

    def queue(self, view: str = "active", staff_name: str = "", limit: int = 200) -> list[Ticket]:
        """view: 'active' (open + claimed), 'mine', 'resolved'."""
        q = select(handoffs)
        if view == "resolved":
            q = q.where(handoffs.c.status == "resolved").order_by(handoffs.c.resolved_at.desc())
        else:
            q = q.where(handoffs.c.status != "resolved")
            if view == "mine":
                q = q.where(handoffs.c.assigned_to == staff_name)
            priority = case({k: v for k, v in PRIORITY.items()}, value=handoffs.c.reason, else_=9)
            # Unclaimed first within the same priority, then oldest first.
            q = q.order_by(priority, case((handoffs.c.status == "open", 0), else_=1), handoffs.c.ts)
        with self.engine.connect() as conn:
            return [Ticket.from_row(r) for r in conn.execute(q.limit(limit)).all()]

    def stats(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        ist_midnight = (now + IST_OFFSET).replace(hour=0, minute=0, second=0, microsecond=0) - IST_OFFSET
        with self.engine.connect() as conn:
            open_rows = conn.execute(select(handoffs.c.reason, handoffs.c.status, handoffs.c.ts)
                                     .where(handoffs.c.status != "resolved")).all()
            resolved_today = conn.execute(select(func.count()).select_from(handoffs).where(and_(
                handoffs.c.status == "resolved", handoffs.c.resolved_at >= ist_midnight))).scalar_one()
        unclaimed = [r for r in open_rows if r.status == "open"]
        oldest = min((_utc(r.ts) for r in unclaimed), default=None)
        return {
            "active": len(open_rows),
            "unclaimed": len(unclaimed),
            "emergency": sum(1 for r in open_rows if r.reason == "emergency"),
            "oldest_unclaimed_minutes": int((now - oldest).total_seconds() // 60) if oldest else None,
            "resolved_today": resolved_today,
        }

    def claim(self, ticket_id: int, staff_name: str) -> None:
        self._set(ticket_id, staff_name, f"Claimed by {staff_name}", status="claimed", assigned_to=staff_name)

    def reply(self, ticket_id: int, staff_name: str, body: str) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(insert(handoff_messages).values(
                handoff_id=ticket_id, ts=now, direction="out", author=staff_name, body=body))
            conn.execute(update(handoffs).where(handoffs.c.id == ticket_id).values(
                live=True, updated_at=now, status="claimed",
                assigned_to=func.coalesce(handoffs.c.assigned_to, staff_name),
            ))

    def resolve(self, ticket_id: int, staff_name: str, note: str) -> None:
        self._set(ticket_id, staff_name, f"Resolved by {staff_name}" + (f": {note}" if note else ""),
                  status="resolved", live=False, resolved_at=datetime.now(timezone.utc), resolution_note=note)

    def reopen(self, ticket_id: int, staff_name: str) -> None:
        self._set(ticket_id, staff_name, f"Reopened by {staff_name}", status="open", assigned_to=None,
                  resolved_at=None)

    def _set(self, ticket_id: int, staff_name: str, note: str, **values) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(update(handoffs).where(handoffs.c.id == ticket_id).values(updated_at=now, **values))
            conn.execute(insert(handoff_messages).values(
                handoff_id=ticket_id, ts=now, direction="note", author=staff_name, body=note))


IST_OFFSET = timedelta(hours=5, minutes=30)
