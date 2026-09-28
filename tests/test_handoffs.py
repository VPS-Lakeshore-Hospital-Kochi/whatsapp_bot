from datetime import datetime, timedelta, timezone

import pytest

from app.audit import Audit
from app.handoffs import Handoffs, handoffs as table


@pytest.fixture
def h(tmp_path):
    return Handoffs(Audit(f"sqlite:///{tmp_path}/h.db", "s").engine)


def test_queue_puts_emergencies_first_then_oldest(h):
    booking = h.open("911", "patient", "booking", "Booking request: Cardiology")
    clinical = h.open("912", "patient", "clinical", "can I take my tablet")
    emergency = h.open("913", "patient", "emergency", "chest pain")
    assert [t.id for t in h.queue()] == [emergency, clinical, booking]


def test_claimed_tickets_sort_after_unclaimed_of_same_priority(h):
    first = h.open("911", "patient", "requested", "")
    second = h.open("912", "patient", "requested", "")
    h.claim(first, "Nurse A")
    assert [t.id for t in h.queue()] == [second, first]
    assert [t.id for t in h.queue("mine", "Nurse A")] == [first]


def test_reply_makes_ticket_live_until_resolved(h):
    tid = h.open("911", "patient", "requested", "please call")
    assert h.live_ticket_for("911") is None
    h.reply(tid, "Nurse A", "Hello, how can we help?")
    live = h.live_ticket_for("911")
    assert live.id == tid and live.status == "claimed" and live.assigned_to == "Nurse A"
    h.add_inbound(tid, "I need a report copy")
    h.resolve(tid, "Nurse A", "Sent report")
    assert h.live_ticket_for("911") is None
    bodies = [(m.direction, m.body) for m in h.messages(tid)]
    assert bodies[0] == ("in", "please call")
    assert ("out", "Hello, how can we help?") in bodies and ("in", "I need a report copy") in bodies
    assert bodies[-1] == ("note", "Resolved by Nurse A: Sent report")


def test_reopen_clears_assignee(h):
    tid = h.open("911", "patient", "requested", "")
    h.claim(tid, "Nurse A")
    h.resolve(tid, "Nurse A", "")
    h.reopen(tid, "Nurse B")
    t = h.get(tid)
    assert t.status == "open" and t.assigned_to is None and t.resolved_at is None


def test_queue_preview_is_redacted_but_thread_is_not(h):
    tid = h.open("911", "patient", "requested", "call me on 9847012345")
    assert "9847012345" not in h.get(tid).summary
    assert h.messages(tid)[0].body == "call me on 9847012345"


def test_whatsapp_24_hour_reply_window(h):
    tid = h.open("911", "patient", "requested", "hi")
    with h.engine.begin() as conn:
        conn.execute(table.update().where(table.c.id == tid).values(
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=25)))
    assert not h.get(tid).can_reply()
    h.add_inbound(tid, "still there?")
    assert h.get(tid).can_reply()


def test_stats(h):
    a = h.open("911", "patient", "emergency", "collapsed")
    b = h.open("912", "patient", "requested", "")
    h.claim(b, "Nurse A")
    c = h.open("913", "patient", "requested", "")
    h.resolve(c, "Nurse A", "")
    s = h.stats()
    assert s == {"active": 2, "unclaimed": 1, "emergency": 1, "oldest_unclaimed_minutes": 0, "resolved_today": 1}
    assert a
