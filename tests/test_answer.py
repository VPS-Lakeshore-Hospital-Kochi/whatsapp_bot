from app.answer import Answerer, DraftAnswer, Verdict
from app.knowledge.retriever import Retriever


async def test_no_sources_means_no_model_call(settings, chunks, llm):
    a = Answerer(Retriever(chunks), llm, settings)
    result = await a.answer("quantum chromodynamics lecture", "patient")
    assert not result.ok and result.reason == "no_sources"
    assert llm.calls == []


async def test_cited_and_verified_answer(settings, chunks, llm):
    a = Answerer(Retriever(chunks), llm, settings)
    llm.will_return(DraftAnswer(status="answered", answer="ICU visiting is 11:00–11:30 am and 5:00–5:30 pm.",
                                cited_source_ids=["patient/visiting-hours#1"]))
    llm.will_return(Verdict(all_claims_supported=True, unsupported_claims=[]))
    result = await a.answer("ICU visiting hours?", "patient")
    assert result.ok
    assert "Source: SAMPLE Visiting hours (v0.1)" in result.text
    # Staff-only content must never be sent to the model for a patient question.
    assert "leave" not in llm.calls[0][1].lower()


async def test_invented_citation_is_rejected(settings, chunks, llm):
    a = Answerer(Retriever(chunks), llm, settings)
    llm.will_return(DraftAnswer(status="answered", answer="Visiting is 24x7.", cited_source_ids=["made/up#9"]))
    result = await a.answer("visiting hours", "patient")
    assert not result.ok and result.reason == "uncited"


async def test_unsupported_claim_is_rejected(settings, chunks, llm):
    a = Answerer(Retriever(chunks), llm, settings)
    llm.will_return(DraftAnswer(status="answered", answer="Parking is free.",
                                cited_source_ids=["patient/parking-and-directions#0"]))
    llm.will_return(Verdict(all_claims_supported=False, unsupported_claims=["Parking is free"]))
    result = await a.answer("parking charges", "patient")
    assert not result.ok and result.reason == "unsupported"


async def test_refusal_goes_to_human(settings, chunks, llm):
    a = Answerer(Retriever(chunks), llm, settings)
    llm.will_return(None, stop_reason="refusal")
    result = await a.answer("visiting hours", "patient")
    assert not result.ok and result.reason == "refusal"
