"""Who is messaging: patient (default), staff, or clinician.

Staff and clinicians are verified with two factors:
  1. the WhatsApp number must be in the HR staff directory (possession of the phone), and
  2. they must type their employee ID (knowledge).
Verification lasts STAFF_SESSION_HOURS, then they log in again. Repeated wrong IDs lock the
number for LOCKOUT_MINUTES. Anyone not verified is treated as a patient / member of the public.
"""

import csv
import hmac
import logging
from dataclasses import dataclass
from pathlib import Path

from app.store import Store

log = logging.getLogger(__name__)

PATIENT, STAFF, CLINICIAN = "patient", "staff", "clinician"


@dataclass(frozen=True)
class StaffMember:
    phone: str
    employee_id: str
    name: str
    role: str  # STAFF or CLINICIAN
    department: str
    desk: bool = False  # may sign in to the handoff dashboard
    roster: bool = False  # may upload the duty roster on the desk


def normalise_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:  # Indian mobile without country code
        digits = "91" + digits
    return digits


class StaffDirectory:
    """Loaded from a CSV export of the HRMS: phone,employee_id,name,role,department[,desk,roster].

    `desk` = yes for people who work the handoff dashboard (front office, duty nurses).
    `roster` = yes for people who may upload the duty roster (e.g. ED nurse manager).
    """

    def __init__(self, members: list[StaffMember]):
        self._by_phone = {m.phone: m for m in members}

    @classmethod
    def from_csv(cls, path: Path) -> "StaffDirectory":
        if not path.exists():
            log.warning("Staff directory %s not found; staff login disabled", path)
            return cls([])
        with path.open(newline="", encoding="utf-8") as f:
            members = [
                StaffMember(
                    phone=normalise_phone(row["phone"]),
                    employee_id=row["employee_id"].strip().upper(),
                    name=row["name"].strip(),
                    role=CLINICIAN if row["role"].strip().lower() == CLINICIAN else STAFF,
                    department=row.get("department", "").strip(),
                    desk=_yes(row.get("desk")),
                    roster=_yes(row.get("roster")),
                )
                for row in csv.DictReader(f)
            ]
        return cls(members)

    def lookup(self, wa_id: str) -> StaffMember | None:
        return self._by_phone.get(normalise_phone(wa_id))

    def by_employee_id(self, employee_id: str) -> StaffMember | None:
        return next((m for m in self._by_phone.values() if m.employee_id == employee_id), None)


def _yes(value: str | None) -> bool:
    return (value or "").strip().lower() in ("yes", "y", "true", "1")


class Identity:
    def __init__(self, directory: StaffDirectory, store: Store, session_hours: int,
                 max_attempts: int, lockout_minutes: int):
        self.directory = directory
        self.store = store
        self.session_seconds = session_hours * 3600
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_minutes * 60

    async def role_of(self, wa_id: str) -> str:
        return (await self.store.get(f"auth:{wa_id}")) or PATIENT

    async def is_locked(self, wa_id: str) -> bool:
        return bool(await self.store.get(f"lock:{wa_id}"))

    async def check_employee_id(self, wa_id: str, typed: str) -> StaffMember | None:
        """Returns the member on success. On failure counts the attempt and may lock the number."""
        member = self.directory.lookup(wa_id)
        if member and hmac.compare_digest(member.employee_id, typed.strip().upper()):
            await self.store.delete(f"attempts:{wa_id}")
            await self.store.set(f"auth:{wa_id}", member.role, self.session_seconds)
            return member
        attempts = await self.store.incr(f"attempts:{wa_id}", self.lockout_seconds)
        if attempts >= self.max_attempts:
            await self.store.set(f"lock:{wa_id}", True, self.lockout_seconds)
            await self.store.delete(f"attempts:{wa_id}")
        return None

    async def logout(self, wa_id: str) -> None:
        await self.store.delete(f"auth:{wa_id}")
