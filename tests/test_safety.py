import pytest

from app.safety import is_clinical_question, is_emergency, redact


@pytest.mark.parametrize("msg", [
    "My father has chest pain", "she is UNCONSCIOUS", "need an ambulance", "he can't breathe",
    "I want to kill myself", "achanu nenju vedana", "നെഞ്ചുവേദന ഉണ്ട്",
])
def test_emergencies_detected(msg):
    assert is_emergency(msg)


@pytest.mark.parametrize("msg", ["What are the visiting hours?", "Where do I park?", "OP timings for cardiology"])
def test_non_emergencies(msg):
    assert not is_emergency(msg)


@pytest.mark.parametrize("msg", [
    "What dose of paracetamol should I take?", "Can I stop my BP tablet?", "what does my report mean",
    "I have fever since 3 days",
])
def test_clinical_questions_detected(msg):
    assert is_clinical_question(msg)


def test_redact():
    out = redact("Call me on +91 98470 12345 or mail a.b@x.com, aadhaar 1234 5678 9012")
    assert "98470" not in out and "a.b@x.com" not in out and "9012" not in out
