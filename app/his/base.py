"""What the bot needs from the Hospital Information System. Read-only for v1.

Keeping this interface small is deliberate: every method is something the bot answers with
data from the HIS, never from the model. Write actions (booking, cancelling) come later and
must each go through an explicit "Confirm? Yes / No" step.
"""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Patient:
    uhid: str
    name: str


@dataclass(frozen=True)
class Appointment:
    when: datetime
    doctor: str
    department: str
    location: str


@dataclass(frozen=True)
class Report:
    name: str
    ready: bool
    ordered_on: date


@dataclass(frozen=True)
class Doctor:
    id: str
    name: str
    department: str
    op_days: str  # e.g. "Mon, Wed, Fri 9:00–13:00"


class HIS:
    async def verify_patient(self, uhid: str, dob: date) -> Patient | None:
        """Return the patient only if the UHID and date of birth both match."""
        raise NotImplementedError

    async def upcoming_appointments(self, uhid: str) -> list[Appointment]:
        raise NotImplementedError

    async def report_status(self, uhid: str) -> list[Report]:
        raise NotImplementedError

    async def departments(self) -> list[str]:
        raise NotImplementedError

    async def doctors(self, department: str) -> list[Doctor]:
        raise NotImplementedError
