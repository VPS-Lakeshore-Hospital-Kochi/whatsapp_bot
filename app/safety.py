"""Rule-based safety checks that run BEFORE any AI call.

These are deliberately simple and deterministic: an emergency must never depend on a model
call succeeding. The word lists are a starting point and must be reviewed and extended by the
Emergency Department and Quality team (including Malayalam and Manglish phrasing actually seen
in logs) before go-live.
"""

import re

_EMERGENCY_TERMS = [
    # English
    r"chest pain", r"heart attack", r"can'?t breathe", r"cannot breathe", r"not breathing",
    r"breathless(ness)?", r"unconscious", r"not responding", r"collapsed?", r"fainted",
    r"seizure", r"fits?\b", r"convulsion", r"stroke", r"face drooping", r"slurred speech",
    r"severe bleeding", r"bleeding heavily", r"accident", r"poison(ed|ing)?", r"overdose",
    r"suicid(e|al)", r"kill myself", r"end my life", r"snake ?bite", r"burns?\b.*severe",
    r"emergency", r"ambulance",
    # Manglish (romanised Malayalam) — to be reviewed by ED
    r"nenju ?vedana", r"shwasam (kittunnilla|muttal)", r"bodham (illa|poyi)",
    # Malayalam script — to be reviewed by ED
    r"നെഞ്ചുവേദന", r"ശ്വാസം കിട്ടുന്നില്ല", r"ബോധം", r"ആംബുലൻസ്",
]
_EMERGENCY_RE = re.compile("|".join(_EMERGENCY_TERMS), re.IGNORECASE)

# Questions a patient-facing bot must not answer itself (Telemedicine Practice Guidelines 2020:
# an AI tool cannot counsel or prescribe). These are routed to a nurse/doctor callback.
_CLINICAL_TERMS = [
    r"\bdose\b", r"\bdosage\b", r"how (much|many) (tablet|mg|ml)", r"\bmg\b", r"should i (take|stop|start)",
    r"can i (take|stop|eat|drink)", r"side ?effects?", r"is it (normal|serious|dangerous)",
    r"what does my (report|result|scan|test)", r"my (report|result|scan) (says|shows|mean)",
    r"\bsymptoms?\b", r"\bdiagnos", r"\btreatment for\b", r"\bpain\b", r"\bfever\b", r"\bvomit",
    r"\binfection\b", r"\bpregnan", r"\bmedicine\b", r"\btablet\b", r"\binsulin\b", r"\bbp\b",
    r"\bsugar level", r"\bdiabet",
]
_CLINICAL_RE = re.compile("|".join(_CLINICAL_TERMS), re.IGNORECASE)


def is_emergency(text: str) -> bool:
    return bool(_EMERGENCY_RE.search(text))


def is_clinical_question(text: str) -> bool:
    return bool(_CLINICAL_RE.search(text))


_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s-]{8,}\d)(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_AADHAAR_RE = re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")


def redact(text: str) -> str:
    """Mask obvious identifiers before text is written to logs."""
    text = _AADHAAR_RE.sub("[ID]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    return _EMAIL_RE.sub("[EMAIL]", text)
