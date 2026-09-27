from app.answer import DraftAnswer, Verdict
from tests.conftest import DOCTOR_WA, NURSE_WA, PATIENT_WA, tap, text


async def consented(bot, wa=PATIENT_WA):
    await bot.router.handle(text(wa, "hi"))
    await bot.router.handle(tap(wa, "consent_yes"))
    bot.sender.sent.clear()


async def test_first_contact_asks_for_consent(bot):
    await bot.router.handle(text(PATIENT_WA, "what are the visiting hours"))
    assert bot.sender.sent[-1]["ids"] == ["consent_yes", "consent_no"]
    assert bot.llm.calls == []


async def test_emergency_bypasses_consent_and_ai(bot):
    await bot.router.handle(text(PATIENT_WA, "my mother collapsed and is not responding"))
    assert "EMERG-NUM" in bot.sender.last_body()
    assert bot.llm.calls == []


async def test_duplicate_webhook_processed_once(bot):
    msg = text(PATIENT_WA, "hi")
    await bot.router.handle(msg)
    await bot.router.handle(msg)
    assert len(bot.sender.sent) == 1


async def test_clinical_question_goes_to_person_not_ai(bot):
    await consented(bot)
    await bot.router.handle(text(PATIENT_WA, "Can I stop my BP tablet before surgery?"))
    assert "can't give medical advice" in bot.sender.last_body()
    assert "ref #" in bot.sender.last_body()
    assert bot.llm.calls == []


async def test_patient_question_answered_with_source(bot):
    await consented(bot)
    bot.llm.will_return(DraftAnswer(status="answered", answer="General ward visiting is 4:00 pm to 7:00 pm.",
                                    cited_source_ids=["patient/visiting-hours#0"]))
    bot.llm.will_return(Verdict(all_claims_supported=True, unsupported_claims=[]))
    await bot.router.handle(text(PATIENT_WA, "visiting hours general ward"))
    assert "Source: SAMPLE Visiting hours" in bot.sender.last_body()


async def test_unknown_question_offers_person(bot):
    await consented(bot)
    await bot.router.handle(text(PATIENT_WA, "do you sell gold coins"))
    assert "won't guess" in bot.sender.last_body()


async def test_patient_lookup_requires_uhid_and_dob(bot):
    await consented(bot)
    await bot.router.handle(tap(PATIENT_WA, "reports"))
    assert "UHID" in bot.sender.last_body()
    await bot.router.handle(text(PATIENT_WA, "LH0000001"))
    await bot.router.handle(text(PATIENT_WA, "01-01-1990"))
    assert "didn't match" in bot.sender.last_body()
    await bot.router.handle(tap(PATIENT_WA, "reports"))
    await bot.router.handle(text(PATIENT_WA, "LH0000001"))
    await bot.router.handle(text(PATIENT_WA, "15-01-1980"))
    assert "Complete Blood Count" in bot.sender.last_body()


async def test_patient_lookup_locks_after_three_failures(bot):
    await consented(bot)
    for _ in range(3):
        await bot.router.handle(tap(PATIENT_WA, "appts"))
        await bot.router.handle(text(PATIENT_WA, "LH0000001"))
        await bot.router.handle(text(PATIENT_WA, "02-02-1902"))
    await bot.router.handle(tap(PATIENT_WA, "appts"))
    assert "Too many attempts" in bot.sender.last_body()


async def test_staff_login_and_staff_content(bot):
    await bot.router.handle(text(NURSE_WA, "staff login"))
    assert "employee ID" in bot.sender.last_body()
    await bot.router.handle(text(NURSE_WA, "lh1001"))
    assert any("Welcome, Test Nurse" in m["body"] for m in bot.sender.sent)
    bot.llm.will_return(DraftAnswer(status="answered", answer="12 days per year, not carried forward.",
                                    cited_source_ids=["staff/leave-policy#0"]))
    bot.llm.will_return(Verdict(all_claims_supported=True, unsupported_claims=[]))
    await bot.router.handle(text(NURSE_WA, "how many casual leave days"))
    assert "SAMPLE Leave policy" in bot.sender.last_body()


async def test_unregistered_number_cannot_log_in(bot):
    await consented(bot)
    await bot.router.handle(text(PATIENT_WA, "login"))
    assert "isn't in the staff directory" in bot.sender.last_body()


async def test_wrong_employee_id_does_not_log_in(bot):
    await bot.router.handle(text(DOCTOR_WA, "login"))
    await bot.router.handle(text(DOCTOR_WA, "LH1001"))  # someone else's ID
    assert "didn't match" in bot.sender.last_body()
    assert await bot.router.identity.role_of(DOCTOR_WA) == "patient"


async def test_clinician_emergency_words_are_not_intercepted(bot):
    await bot.router.handle(text(DOCTOR_WA, "login"))
    await bot.router.handle(text(DOCTOR_WA, "LH2002"))
    bot.llm.will_return(DraftAnswer(status="not_in_sources", answer="", cited_source_ids=[]))
    await bot.router.handle(text(DOCTOR_WA, "chest pain protocol code blue"))
    assert "EMERG-NUM" not in bot.sender.last_body()
    assert bot.llm.calls  # went to the knowledge base, not the emergency reply
