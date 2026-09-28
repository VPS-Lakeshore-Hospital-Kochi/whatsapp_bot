import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from app.alerts import Alerts
from app.audit import Audit
from app.roster import IST, Roster, RosterError, RosterStore, parse_roster
from tests.conftest import NURSE_WA, PATIENT_WA, tap, text
from tests.test_desk import csrf_of, sign_in

ROSTER = """day,start,end,tier,phone,name
2026-10-01,08:00,20:00,1,919000000001,Day Nurse
2026-10-01,20:00,08:00,1,9000000002,Night Nurse
thu,09:00,17:00,2,919000000010,Dr Thursday
daily,00:00,24:00,3,919000000020,Nursing Supt
"""


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=IST)


def names(tiers):
    return [[p.name for p in people] for people in tiers]


# ------------------------------------------------------------------ parsing & timing

def test_on_duty_handles_overnight_weekday_and_daily():
    r = Roster(parse_roster(ROSTER))  # 2026-10-01 is a Thursday
    assert {t: [p.name for p in ps] for t, ps in r.on_duty(at("2026-10-01T10:00")).items()} == {
        1: ["Day Nurse"], 2: ["Dr Thursday"], 3: ["Nursing Supt"]}
    assert [p.name for p in r.on_duty(at("2026-10-02T03:00"))[1]] == ["Night Nurse"]  # overnight
    assert 1 not in r.on_duty(at("2026-10-02T08:00"))  # night shift ended at 08:00 sharp
    assert list(r.on_duty(at("2026-10-03T23:59"))) == [3]  # only the daily 24h shift


def test_ten_digit_numbers_get_country_code():
    assert parse_roster(ROSTER)[1].phone == "919000000002"


def test_gap_edges_are_exact_even_off_the_quarter_hour():
    r = Roster(parse_roster("day,start,end,tier,phone,name\ndaily,08:07,20:00,1,919000000001,A\n"))
    gaps = r.gaps(at("2026-10-01T07:59"), hours=1)
    assert [(a.astimezone(IST).strftime("%H:%M"), b.astimezone(IST).strftime("%H:%M")) for a, b in gaps] == [
        ("07:59", "08:07")]


def test_gaps_in_tier_one():
    r = Roster(parse_roster(ROSTER))
    gaps = r.gaps(at("2026-10-01T06:00").astimezone(timezone.utc), hours=30, tier=1)
    assert [(a.astimezone(IST).strftime("%d %H:%M"), b.astimezone(IST).strftime("%d %H:%M")) for a, b in gaps] == [
        ("01 06:00", "01 08:00"), ("02 08:00", "02 12:00")]


def test_upcoming_lists_shifts_in_order():
    r = Roster(parse_roster(ROSTER))
    up = r.upcoming(at("2026-10-01T19:00"), hours=2)
    assert [(s.name, b.astimezone(IST).strftime("%H:%M")) for b, _, s in up] == [
        ("Nursing Supt", "00:00"), ("Day Nurse", "08:00"), ("Night Nurse", "20:00")]  # Dr Thursday ended 17:00


def test_excel_bom_is_accepted():
    assert len(parse_roster("﻿" + ROSTER)) == 4


def test_every_problem_is_reported_with_its_line():
    bad = "day,start,end,tier,phone,name\nfunday,08:00,20:00,1,919000000001,A\n2026-10-01,8am,20:00,1,919000000001,B\n2026-10-01,08:00,20:00,0,123,\n"
    with pytest.raises(RosterError) as e:
        parse_roster(bad)
    assert len(e.value.problems) == 3
    assert [p.split(":")[0] for p in e.value.problems] == ["Line 2", "Line 3", "Line 4"]


def test_missing_columns_and_empty_file():
    with pytest.raises(RosterError, match="Missing column"):
        parse_roster("date,phone\n2026-10-01,919000000001\n")
    with pytest.raises(RosterError, match="no shifts"):
        parse_roster("day,start,end,tier,phone,name\n")


def test_store_keeps_versions_and_rejects_bad_uploads(tmp_path):
    store = RosterStore(Audit(f"sqlite:///{tmp_path}/r.db", "s").engine)
    assert store.current() is None
    v1 = store.upload(ROSTER, "Manager")
    with pytest.raises(RosterError):
        store.upload("nonsense", "Manager")
    assert store.latest().id == v1.id  # bad upload saved nothing
    v2 = store.upload(ROSTER.replace("Day Nurse", "Relief Nurse"), "Manager")
    assert v2.id > v1.id and "Relief Nurse" in store.current().on_duty(at("2026-10-01T10:00"))[1][0].name


def test_bad_seed_file_does_not_stop_the_app(tmp_path):
    seed = tmp_path / "duty_roster.csv"
    seed.write_text("garbage")
    assert RosterStore(Audit(f"sqlite:///{tmp_path}/r.db", "s").engine, seed_csv=seed).latest() is None


# ------------------------------------------------------------------ alerts use the roster

@pytest.fixture
def rostered(bot, tmp_path):
    store = RosterStore(bot.audit.engine)
    store.upload(ROSTER, "Manager")
    settings = bot.settings.model_copy(update={"alert_tiers": "919000000099", "alert_template": "emergency_alert",
                                               "alert_escalate_minutes": 3})
    bot.alerts = Alerts(settings, bot.store, bot.sender, bot.handoffs, roster=store)
    bot.router.alerts = bot.alerts
    bot.roster = store
    return bot


def test_tiers_come_from_roster_then_fallback(rostered):
    assert names(rostered.alerts.tiers_at(at("2026-10-01T10:00"))) == [
        ["Day Nurse"], ["Dr Thursday"], ["Nursing Supt"], ["Fallback +91 ••••• 0099"]]


def test_fallback_numbers_already_on_roster_are_not_repeated(rostered):
    rostered.alerts.tiers = [["919000000001"]]
    assert names(rostered.alerts.tiers_at(at("2026-10-01T10:00"))) == [["Day Nurse"], ["Dr Thursday"], ["Nursing Supt"]]


async def test_escalation_follows_whoever_is_on_shift(rostered):
    tid = rostered.handoffs.open(PATIENT_WA, "patient", "emergency", "chest pain")
    t = rostered.handoffs.get(tid)
    opened = at("2026-10-01T19:58").astimezone(timezone.utc)
    t.ts = opened
    await rostered.alerts.check_ticket(t, now=opened)
    await rostered.alerts.check_ticket(t, now=opened + timedelta(minutes=3))  # 20:01: shift changed
    sent = [m["to"] for m in rostered.sender.sent if m["type"] == "template"]
    # Day nurse at 19:58; at 20:01 tier 2 is due, and tier 2 is now the Nursing Supt (Dr Thursday left at 17:00).
    assert sent == ["919000000001", "919000000020"]


async def test_person_alerted_can_ack_even_after_their_shift(rostered):
    await rostered.router.handle(text(PATIENT_WA, "chest pain"))
    tid = rostered.handoffs.queue()[0].id
    await rostered.store.set(f"alerted_to:{tid}", ["919000000001"])
    await rostered.router.handle(tap("919000000001", f"ack:{tid}"))
    assert rostered.handoffs.get(tid).status == "claimed"


async def test_nobody_on_duty_is_recorded_on_the_ticket(bot):
    settings = bot.settings.model_copy(update={"alert_tiers": "", "alert_template": "emergency_alert"})
    bot.alerts = Alerts(settings, bot.store, bot.sender, bot.handoffs, roster=None)
    bot.router.alerts = bot.alerts
    await bot.router.handle(text(PATIENT_WA, "chest pain"))
    tid = bot.handoffs.queue()[0].id
    assert any("No one to alert" in m.body for m in bot.handoffs.messages(tid))


# ------------------------------------------------------------------ desk roster page

def upload(client, token, content):
    return client.post("/desk/roster", data={"csrf": token},
                       files={"file": ("roster.csv", content.encode(), "text/csv")})


async def test_roster_page_and_upload_permissions(desk, tmp_path):
    client, bot = desk
    await sign_in(client, bot)
    page = client.get("/desk/roster")
    assert page.status_code == 200 and "Only roster managers can upload" in page.text
    assert upload(client, csrf_of(page.text), ROSTER).status_code == 403  # Test Nurse has desk=yes, roster=no


async def test_roster_manager_uploads(desk):
    client, bot = desk
    bot.directory._by_phone[NURSE_WA] = dataclasses.replace(bot.directory.lookup(NURSE_WA), roster=True)
    await sign_in(client, bot)
    token = csrf_of(client.get("/desk/roster").text)

    bad = upload(client, token, "day,start,end,tier,phone,name\nfunday,08:00,20:00,1,919000000001,A\n")
    assert "wasn't uploaded" in bad.text and "Line 2" in bad.text
    assert bot.roster.latest() is None

    ok = upload(client, token, ROSTER)
    assert ok.status_code == 200 and "Roster uploaded" in ok.text
    assert bot.roster.latest().uploaded_by == "Test Nurse (LH1001)"
    csv = client.get("/desk/roster.csv")
    assert csv.status_code == 200 and "Night Nurse" in csv.text
    assert "csrf" in client.get("/desk").text  # queue still renders with the duty line


async def test_queue_warns_when_nobody_is_on_duty(desk):
    client, bot = desk
    await sign_in(client, bot)
    assert "Nobody is on duty for emergency alerts right now" in client.get("/desk").text
