"""Fake HIS data for development, demos and tests. Not real patients or doctors."""

from datetime import date, datetime, timedelta

from app.his.base import HIS, Appointment, Doctor, Patient, Report

_PATIENTS = {("LH0000001", date(1980, 1, 15)): Patient("LH0000001", "Test Patient")}
_DOCTORS = [
    Doctor("D1", "Dr. Test Cardiologist", "Cardiology", "Mon, Wed, Fri 9:00–13:00"),
    Doctor("D2", "Dr. Test Orthopaedician", "Orthopaedics", "Tue, Thu 10:00–14:00"),
    Doctor("D3", "Dr. Test Gastroenterologist", "Gastroenterology", "Mon–Sat 9:00–12:00"),
]


class MockHIS(HIS):
    async def verify_patient(self, uhid, dob):
        return _PATIENTS.get((uhid.strip().upper(), dob))

    async def upcoming_appointments(self, uhid):
        tomorrow = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0) + timedelta(days=1)
        return [Appointment(tomorrow, "Dr. Test Cardiologist", "Cardiology", "OP Block A, 2nd floor")]

    async def report_status(self, uhid):
        return [
            Report("Complete Blood Count", True, date.today() - timedelta(days=1)),
            Report("Lipid Profile", False, date.today()),
        ]

    async def departments(self):
        return sorted({d.department for d in _DOCTORS})

    async def doctors(self, department):
        return [d for d in _DOCTORS if d.department == department]
