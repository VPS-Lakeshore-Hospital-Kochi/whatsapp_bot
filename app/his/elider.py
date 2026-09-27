"""Adapter for the Elider / Datamate HIS.

Elider's integration interface is not public, so this is a stub. Ask Datamate (or the in-house
Elider team) for ONE of the following, in order of preference:

  1. A REST/JSON API with a dedicated read-only service account for the bot, covering:
     patient lookup by UHID + DOB, upcoming appointments by UHID, lab/radiology report status
     by UHID, department list, doctor list with OP schedule.
  2. HL7 v2 / FHIR feeds (SIU for appointments, ORU for results) that we consume into a small
     read-only cache database.
  3. Read-only SQL views on a reporting replica (never the live transactional DB), exposing
     exactly the fields above and nothing else.

Whichever route: the bot's credentials must be read-only, IP-restricted to the bot server,
and every call is written to the audit log. Once the vendor shares the spec, implement the
methods below and switch HIS_ADAPTER=elider.
"""

from app.his.base import HIS


class EliderHIS(HIS):
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key

    async def verify_patient(self, uhid, dob):
        raise NotImplementedError("Implement once Datamate shares the Elider integration spec")

    async def upcoming_appointments(self, uhid):
        raise NotImplementedError("Implement once Datamate shares the Elider integration spec")

    async def report_status(self, uhid):
        raise NotImplementedError("Implement once Datamate shares the Elider integration spec")

    async def departments(self):
        raise NotImplementedError("Implement once Datamate shares the Elider integration spec")

    async def doctors(self, department):
        raise NotImplementedError("Implement once Datamate shares the Elider integration spec")
