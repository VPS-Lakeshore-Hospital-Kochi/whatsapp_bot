from datetime import date

from app.knowledge.loader import load_knowledge
from app.knowledge.retriever import Retriever
from tests.conftest import SAMPLES


def test_expired_documents_are_not_loaded(chunks):
    titles = {c.doc_title for c in chunks}
    assert "SAMPLE Visiting hours" in titles
    assert "SAMPLE Old canteen timings" not in titles


def test_documents_expire_on_their_review_date():
    later = load_knowledge(SAMPLES, today=date(2100, 1, 1))
    assert later == []


def test_patients_cannot_retrieve_staff_documents(chunks):
    r = Retriever(chunks)
    assert r.search("casual leave carried forward", "patient") == []
    staff_hits = r.search("casual leave carried forward", "staff")
    assert staff_hits and staff_hits[0][0].doc_title == "SAMPLE Leave policy"


def test_clinician_only_content(chunks):
    r = Retriever(chunks)
    assert r.search("code blue extension", "patient") == []
    assert r.search("code blue extension", "clinician")[0][0].doc_title == "SAMPLE Code Blue activation"
