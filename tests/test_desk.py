import re

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_app
from tests.conftest import NURSE_WA, PATIENT_WA, SAMPLES, tap, text

# ------------------------------------------------------------------ bot side


async def consented(bot, wa=PATIENT_WA):
    await bot.router.handle(text(wa, "hi"))
    await bot.router.handle(tap(wa, "consent_yes"))


async def test_patient_replies_go_to_live_ticket_not_the_bot(bot):
    await consented(bot)
    await bot.router.handle(tap(PATIENT_WA, "human"))
    tid = int(re.search(r"ref #(\d+)", bot.sender.last_body()).group(1))
    bot.handoffs.reply(tid, "Nurse A", "Hi, this is the front desk.")
    bot.sender.sent.clear()

    await bot.router.handle(text(PATIENT_WA, "what are the visiting hours"))
    await bot.router.handle(text(PATIENT_WA, "and parking?"))
    assert bot.llm.calls == []  # never reached the AI
    assert len(bot.sender.sent) == 1 and "Passed to our team" in bot.sender.last_body()  # one ack only
    assert [m.body for m in bot.handoffs.messages(tid) if m.direction == "in"][-2:] == [
        "what are the visiting hours", "and parking?"]

    await bot.router.handle(text(PATIENT_WA, "menu"))  # back to the bot
    assert bot.handoffs.live_ticket_for(PATIENT_WA) is None
    assert bot.sender.sent[-1]["type"] == "list"


async def test_emergency_still_intercepted_during_live_ticket(bot):
    await consented(bot)
    await bot.router.handle(tap(PATIENT_WA, "human"))
    tid = int(re.search(r"ref #(\d+)", bot.sender.last_body()).group(1))
    bot.handoffs.reply(tid, "Nurse A", "Hello")
    await bot.router.handle(text(PATIENT_WA, "he has collapsed and is not breathing"))
    assert "EMERG-NUM" in bot.sender.last_body()
    assert bot.handoffs.queue()[0].reason == "emergency"


async def test_desk_command_only_for_desk_staff(bot):
    await bot.router.handle(text(NURSE_WA, "desk"))
    assert "Desk access is for authorised staff" in bot.sender.last_body()  # not logged in yet
    await bot.router.handle(text(NURSE_WA, "login"))
    await bot.router.handle(text(NURSE_WA, "LH1001"))
    await bot.router.handle(text(NURSE_WA, "desk"))
    assert re.search(r"/desk/login\?token=\S+", bot.sender.last_body())


async def test_logged_in_staff_without_desk_flag_is_refused(bot):
    await bot.router.handle(text("919000000002", "login"))
    await bot.router.handle(text("919000000002", "LH2002"))
    await bot.router.handle(text("919000000002", "desk"))
    assert "Desk access is for authorised staff" in bot.sender.last_body()


# ------------------------------------------------------------------ dashboard (HTTP)


async def sign_in(client, bot):
    await bot.router.handle(text(NURSE_WA, "login"))
    await bot.router.handle(text(NURSE_WA, "LH1001"))
    await bot.router.handle(text(NURSE_WA, "desk"))
    token = re.search(r"token=(\S+)", bot.sender.last_body()).group(1)
    page = client.get(f"/desk/login?token={token}")
    assert page.status_code == 200 and "Continue" in page.text
    resp = client.post("/desk/login", data={"token": token}, follow_redirects=False)
    assert resp.status_code == 303
    return token


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_desk_requires_sign_in(desk):
    client, _ = desk
    resp = client.get("/desk", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/desk/login"
    assert client.get("/desk/api/summary").status_code == 401


async def test_sign_in_link_is_single_use(desk):
    client, bot = desk
    token = await sign_in(client, bot)
    other = TestClient(client.app)
    resp = other.post("/desk/login", data={"token": token}, follow_redirects=False)
    assert resp.status_code == 200 and "expired or was already used" in resp.text


async def test_work_a_ticket_end_to_end(desk):
    client, bot = desk
    tid = bot.handoffs.open(PATIENT_WA, "patient", "requested", "<script>alert(1)</script> please call")
    await sign_in(client, bot)

    queue = client.get("/desk")
    assert queue.status_code == 200 and f"#{tid}" in queue.text
    assert "<script>alert(1)</script>" not in queue.text  # patient text is escaped
    assert queue.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert queue.headers["Cache-Control"] == "no-store"

    page = client.get(f"/desk/t/{tid}")
    token = csrf_of(page.text)
    assert "&lt;script&gt;" in page.text

    assert client.post(f"/desk/t/{tid}/claim", data={"csrf": "wrong"}).status_code == 403
    client.post(f"/desk/t/{tid}/claim", data={"csrf": token})
    assert bot.handoffs.get(tid).assigned_to == "Test Nurse"

    client.post(f"/desk/t/{tid}/reply", data={"csrf": token, "body": "Hello from the front desk"})
    assert bot.sender.sent[-1] == {"to": PATIENT_WA, "type": "text", "body": "Hello from the front desk"}
    assert bot.handoffs.get(tid).live

    client.post(f"/desk/t/{tid}/resolve", data={"csrf": token, "note": "Called back"})
    t = bot.handoffs.get(tid)
    assert t.status == "resolved" and t.resolution_note == "Called back" and not t.live
    assert f"#{tid}" in client.get("/desk?view=resolved").text


async def test_failed_whatsapp_send_is_reported_and_not_recorded(desk):
    client, bot = desk
    tid = bot.handoffs.open(PATIENT_WA, "patient", "requested", "hi")
    await sign_in(client, bot)
    token = csrf_of(client.get(f"/desk/t/{tid}").text)
    bot.sender.fail_next = True
    page = client.post(f"/desk/t/{tid}/reply", data={"csrf": token, "body": "Hello"})
    assert "Not sent" in page.text
    assert not bot.handoffs.get(tid).live
    assert all(m.direction != "out" for m in bot.handoffs.messages(tid))


async def test_no_reply_to_resolved_ticket(desk):
    client, bot = desk
    tid = bot.handoffs.open(PATIENT_WA, "patient", "requested", "hi")
    bot.handoffs.resolve(tid, "Someone", "")
    await sign_in(client, bot)
    token = csrf_of(client.get(f"/desk/t/{tid}").text)
    before = len(bot.sender.sent)
    client.post(f"/desk/t/{tid}/reply", data={"csrf": token, "body": "late reply"})
    assert len(bot.sender.sent) == before


async def test_emergency_banner(desk):
    client, bot = desk
    bot.handoffs.open(PATIENT_WA, "patient", "emergency", "chest pain")
    await sign_in(client, bot)
    assert "emergency ticket is open" in client.get("/desk").text
    assert client.get("/desk/api/summary").json()["emergency"] == 1


async def test_logout_ends_session(desk):
    client, bot = desk
    await sign_in(client, bot)
    token = csrf_of(client.get("/desk").text)
    client.post("/desk/logout", data={"csrf": token})
    assert client.get("/desk/api/summary").status_code == 401


def test_network_allowlist(tmp_path):
    settings = Settings(knowledge_dir=SAMPLES, database_url=f"sqlite:///{tmp_path}/n.db", redis_url="",
                        desk_allowed_cidrs="10.0.0.0/8", _env_file=None)
    client = TestClient(build_app(settings))  # TestClient connects from "testclient", not a 10.x address
    assert client.get("/desk/login").status_code == 403
    assert client.get("/health").status_code == 200
