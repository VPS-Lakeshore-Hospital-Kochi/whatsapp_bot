"""Append-only audit trail of every conversation turn.

Phone numbers are stored as a keyed hash (pseudonymised), and message text is redacted of
obvious identifiers. This is what the weekly quality review reads, and what you show an
auditor. Set a retention period with your DPO and purge older rows on a schedule.
"""

import hashlib
import hmac
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, insert

from app.safety import redact

metadata = MetaData()

audit_events = Table(
    "audit_events", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("user_hash", String(64), nullable=False, index=True),
    Column("role", String(16), nullable=False),
    Column("route", String(32), nullable=False),  # emergency / menu / kb_answer / handoff / ...
    Column("outcome", String(32), nullable=False),  # answered / no_sources / unsupported / ...
    Column("inbound", Text),
    Column("outbound", Text),
    Column("sources", Text),
)


class Audit:
    def __init__(self, database_url: str, hash_secret: str):
        if database_url.startswith("sqlite:///"):
            from pathlib import Path

            Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(database_url)
        metadata.create_all(self.engine)
        self._secret = hash_secret.encode()

    def user_hash(self, wa_id: str) -> str:
        return hmac.new(self._secret, wa_id.encode(), hashlib.sha256).hexdigest()

    def log(self, wa_id: str, role: str, route: str, outcome: str, inbound: str = "",
            outbound: str = "", sources: list[str] | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(audit_events).values(
                ts=datetime.now(timezone.utc), user_hash=self.user_hash(wa_id), role=role,
                route=route, outcome=outcome, inbound=redact(inbound), outbound=redact(outbound),
                sources=",".join(sources or []),
            ))
