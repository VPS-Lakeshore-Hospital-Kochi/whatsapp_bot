import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest

from app.alerts import Alerts, parse_tiers
from tests.conftest import NURSE_WA, PATIENT_WA, tap, text

TIER1 = [NURSE_WA, "919000000003"]
TIER2 = ["919000000010"]


@pytest.fixture
def paged(bot):
    """The shared bot fixture, with two alert tiers and a WhatsApp template configured."""
    settings = bot.settings.model_copy(update={
        "alert_tiers": ",".join(TIER1) + ";" + ",".join(TIER2),
        "alert_template": "emergency_alert",
        "alert_escalate_minutes": 3,
        "public_base_url": "https://bot.example.org",
    })
    bot.alerts = Alerts(settings, bot.store, bot.sender, bot.handoffs)
    bot.router.alerts = bot.alerts
    bot.router.s = settings
    return bot


def templates(bot):
    return [m for m in bot.sender.sent if m["type"] == "template"]


def test_parse_tiers():
    assert parse_tiers(" +91 90000 00001, 919000000002 ; ;919000000010 ") == [
        ["919000000001", "919000000002"], ["919000000010"]]
    assert parse_tiers("") == []


async def test_emergency_alerts_tier_one_immediately(paged):
    await paged.router.handle(text(PATIENT_WA, "my father has chest pain"))
    sent = templates(paged)
    assert [m["to"] for m in sent] == TIER1  # tier 2 not yet
    tid = paged.handoffs.queue()[0].id
    assert sent[0]["name"] == "emergency_alert"
    assert sent[0]["params"][:3] == [str(tid), "Emergency", f"+{PATIENT_WA}"]
    assert sent[0]["payload"] == f"ack:{tid}"
    assert "Alert sent to 2 of 2 duty staff, tier 1." in [m.body for m in paged.handoffs.messages(tid)]


async def test_escalates_to_next_tier_when_nobody_takes_it(paged):
    await paged.router.handle(text(PATIENT_WA, "he collapsed"))
    t = paged.handoffs.queue()[0]
    await paged.alerts.check_all(now=t.ts + timedelta(minutes=2))
    assert len(templates(paged)) == 2  # not yet
    await paged.alerts.check_all(now=t.ts + timedelta(minutes=3, seconds=1))
    assert [m["to"] for m in templates(paged)] == TIER1 + TIER2
    await paged.alerts.check_all(now=t.ts + timedelta(minutes=30))
    assert len(templates(paged)) == 3  # each tier alerted once; nothing past the last tier


async def test_im_on_it_claims_ticket_and_stops_escalation(paged):
    await paged.router.handle(text(PATIENT_WA, "she is unconscious"))
    t = paged.handoffs.queue()[0]
    await paged.router.handle(tap(NURSE_WA, f"ack:{t.id}"))
    reply = paged.sender.last_body()
    assert f"Ticket #{t.id} is yours" in reply and f"+{PATIENT_WA}" in reply
    assert f"https://bot.example.org/desk/t/{t.id}" in reply
    assert paged.handoffs.get(t.id).assigned_to == "Test Nurse"
    await paged.alerts.check_all(now=t.ts + timedelta(minutes=10))
    assert [m["to"] for m in templates(paged)] == TIER1  # tier 2 never paged

    await paged.router.handle(tap("919000000003", f"ack:{t.id}"))  # second person taps late
    assert "already taken by Test Nurse" in paged.sender.last_body()


async def test_claiming_on_the_desk_also_stops_escalation(paged):
    await paged.router.handle(text(PATIENT_WA, "chest pain"))
    t = paged.handoffs.queue()[0]
    paged.handoffs.claim(t.id, "Front Desk")
    await paged.alerts.check_all(now=t.ts + timedelta(minutes=10))
    assert len(templates(paged)) == 2


async def test_ack_from_number_not_on_the_rota_is_ignored(paged):
    await paged.router.handle(text(PATIENT_WA, "chest pain"))
    t = paged.handoffs.queue()[0]
    await paged.router.handle(tap("919111111111", f"ack:{t.id}"))
    assert paged.handoffs.get(t.id).status == "open"


async def test_non_emergency_tickets_do_not_page(paged):
    await paged.router.handle(text(PATIENT_WA, "hi"))
    await paged.router.handle(tap(PATIENT_WA, "consent_yes"))
    await paged.router.handle(tap(PATIENT_WA, "human"))
    assert templates(paged) == []


async def test_undelivered_alerts_are_recorded_on_the_ticket(paged):
    paged.sender.fail_numbers = {"919000000003"}
    await paged.router.handle(text(PATIENT_WA, "chest pain"))
    tid = paged.handoffs.queue()[0].id
    notes = [m.body for m in paged.handoffs.messages(tid) if m.direction == "note"]
    assert any("Alert sent to 1 of 2" in n and "Not delivered to +91 ••••• 0003" in n for n in notes)


async def test_alert_failure_never_blocks_the_patient_reply(paged):
    async def broken(*a, **k):
        raise RuntimeError("provider down")
    paged.sender.template = broken
    await paged.router.handle(text(PATIENT_WA, "chest pain"))
    assert "EMERG-NUM" in paged.sender.last_body()


async def test_webhook_is_signed(paged):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200)

    settings = paged.alerts.settings.model_copy(update={
        "alert_webhook_url": "https://pager.example.org/hook", "alert_webhook_secret": "s3cret"})
    paged.alerts = Alerts(settings, paged.store, paged.sender, paged.handoffs,
                          http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    paged.router.alerts = paged.alerts
    await paged.router.handle(text(PATIENT_WA, "chest pain"))
    assert len(seen) == 1
    body = seen[0].content
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert seen[0].headers["X-Lakeshore-Signature"] == expected
    payload = json.loads(body)
    assert payload["tier"] == 1 and payload["recipients"] == TIER1 and payload["caller"] == f"+{PATIENT_WA}"
    tid = paged.handoffs.queue()[0].id
    assert any("Phone/pager bridge notified" in m.body for m in paged.handoffs.messages(tid))


async def test_second_worker_does_not_double_alert(paged):
    await paged.router.handle(text(PATIENT_WA, "chest pain"))
    other_worker = Alerts(paged.alerts.settings, paged.store, paged.sender, paged.handoffs)
    await other_worker.check_all()
    assert len(templates(paged)) == 2
