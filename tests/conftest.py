from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.answer import Answerer, DraftAnswer, Verdict
from app.audit import Audit
from app.config import Settings
from app.handoffs import Handoffs
from app.his.mock import MockHIS
from app.identity import CLINICIAN, STAFF, Identity, StaffDirectory, StaffMember
from app.knowledge.loader import load_knowledge
from app.knowledge.retriever import Retriever
from app.router import Router
from app.store import MemoryStore
from app.whatsapp.client import RecordingSender
from app.whatsapp.parse import Inbound

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "examples" / "knowledge"

PATIENT_WA = "919999999999"
NURSE_WA = "919000000001"
DOCTOR_WA = "919000000002"


class FakeLLM:
    """Returns queued responses in order, recording the prompts it was given."""

    def __init__(self):
        self.queue: list = []
        self.calls: list[tuple[str, str]] = []

    def will_return(self, parsed, stop_reason="end_turn"):
        self.queue.append(SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason))

    async def parse(self, system, user, schema):
        self.calls.append((system, user))
        return self.queue.pop(0)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        knowledge_dir=SAMPLES,
        database_url=f"sqlite:///{tmp_path}/audit.db",
        emergency_phone="EMERG-NUM",
        front_desk_phone="DESK-NUM",
        retrieval_min_score=0.5,
        _env_file=None,
    )


@pytest.fixture
def chunks():
    return load_knowledge(SAMPLES, today=date(2026, 9, 27))


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def bot(settings, chunks, llm):
    store = MemoryStore()
    directory = StaffDirectory([
        StaffMember(NURSE_WA, "LH1001", "Test Nurse", STAFF, "Nursing", desk=True),
        StaffMember(DOCTOR_WA, "LH2002", "Dr. Test", CLINICIAN, "Cardiology"),
    ])
    sender = RecordingSender()
    audit = Audit(settings.database_url, "test-secret")
    handoffs = Handoffs(audit.engine)
    router = Router(
        settings=settings, store=store, sender=sender,
        identity=Identity(directory, store, 12, 3, 30),
        answerer=Answerer(Retriever(chunks), llm, settings),
        his=MockHIS(), audit=audit, handoffs=handoffs,
    )
    return SimpleNamespace(router=router, sender=sender, store=store, llm=llm, handoffs=handoffs,
                           settings=settings, audit=audit)


_counter = iter(range(10**9))


def text(wa, body):
    return Inbound(message_id=f"m{next(_counter)}", wa_id=wa, kind="text", text=body)


def tap(wa, reply_id):
    return Inbound(message_id=f"m{next(_counter)}", wa_id=wa, kind="reply", reply_id=reply_id)


__all__ = ["DraftAnswer", "Verdict", "text", "tap", "PATIENT_WA", "NURSE_WA", "DOCTOR_WA"]
