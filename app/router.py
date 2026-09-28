"""Decides what happens to each inbound WhatsApp message.

Order of checks (earliest wins):
  1. duplicate / rate limit          -> ignore
     "I'm on it" on an emergency alert (duty staff) -> they take the ticket
  2. emergency words (patients)      -> fixed emergency reply, no AI, plus a handoff ticket
  3. privacy consent (first contact) -> must agree before anything else
  4. a live handoff (staff have replied) -> message goes to that ticket, not the bot
  5. commands (menu, staff login, logout, desk sign-in link, talk to a person)
  6. a pending step (typing employee ID, UHID, date of birth)
  7. menu taps                        -> fixed flows backed by the HIS
  8. free text                        -> patients: clinical questions go to a person;
                                         everything else: cited answer from approved documents
"""

import asyncio
import logging
import re
from datetime import date, datetime

from app.alerts import ACK_PREFIX, Alerts
from app.answer import Answerer
from app.audit import Audit
from app.config import Settings
from app.desk.auth import issue_login_token
from app.handoffs import Handoffs
from app.his.base import HIS
from app.identity import CLINICIAN, PATIENT, STAFF, Identity
from app.safety import is_clinical_question, is_emergency
from app.store import Store
from app.whatsapp.client import Button, ListRow, Sender
from app.whatsapp.parse import Inbound

log = logging.getLogger(__name__)

_GREETING = re.compile(r"^\s*(hi|hello|hey|menu|start|namaskaram|help)\W*$", re.IGNORECASE)
_LOGIN = re.compile(r"^\s*(staff login|login|staff)\s*$", re.IGNORECASE)
_LOGOUT = re.compile(r"^\s*logout\s*$", re.IGNORECASE)
_DESK = re.compile(r"^\s*desk\s*$", re.IGNORECASE)
_HUMAN = re.compile(r"\b(agent|human|real person|talk to (a |some)?(one|person|body)|call me|call back)\b", re.IGNORECASE)

PATIENT_VERIFY_SECONDS = 30 * 60
STATE_SECONDS = 15 * 60


class Router:
    def __init__(self, settings: Settings, store: Store, sender: Sender, identity: Identity,
                 answerer: Answerer, his: HIS, audit: Audit, handoffs: Handoffs, alerts: Alerts | None = None):
        self.s = settings
        self.store = store
        self.send = sender
        self.identity = identity
        self.answerer = answerer
        self.his = his
        self.audit = audit
        self.handoffs = handoffs
        self.alerts = alerts

    # ------------------------------------------------------------------ entry point
    async def handle(self, msg: Inbound) -> None:
        if not msg.message_id or not await self.store.set_if_absent(f"seen:{msg.message_id}", 86400):
            return  # Meta retries webhooks; process each message once
        if await self.store.incr(f"rl:{msg.wa_id}", 60) > self.s.rate_limit_per_minute:
            log.warning("Rate limit hit for %s", self.audit.user_hash(msg.wa_id)[:12])
            return

        wa, text = msg.wa_id, msg.text
        role = await self.identity.role_of(wa)

        if msg.kind == "reply" and msg.reply_id.startswith(ACK_PREFIX):
            return await self._ack_alert(wa, role, msg.reply_id[len(ACK_PREFIX):])

        if role == PATIENT and msg.kind == "text" and is_emergency(text):
            return await self._emergency(wa, text)

        if role == PATIENT and not await self.store.get(f"consent:{wa}"):
            # Staff are covered by their employment terms, so login skips the patient consent screen.
            if msg.kind == "text" and _LOGIN.match(text):
                return await self._start_login(wa, role)
            if msg.kind == "text" and _DESK.match(text):
                return await self._desk_link(wa, role)
            state = await self.store.get(f"state:{wa}")
            if msg.kind == "text" and state and state.get("step") == "employee_id":
                return await self._continue_step(wa, role, state, text)
            return await self._consent(wa, msg)

        if msg.kind == "text":
            live = await asyncio.to_thread(self.handoffs.live_ticket_for, wa)
            if live:
                return await self._to_live_ticket(wa, role, live.id, text)

        if msg.kind == "unsupported":
            return await self._reply(wa, role, "unsupported", "unsupported", "",
                                     "Sorry, I can only read text messages for now. Type *menu* to see what I can do.")

        if msg.kind == "text":
            if _GREETING.match(text):
                return await self._menu(wa, role)
            if _LOGIN.match(text):
                return await self._start_login(wa, role)
            if _DESK.match(text):
                return await self._desk_link(wa, role)
            if _LOGOUT.match(text):
                await self.identity.logout(wa)
                return await self._reply(wa, role, "logout", "ok", text, "You're logged out.")
            if _HUMAN.search(text):
                return await self._handoff(wa, role, "requested", text)
            state = await self.store.get(f"state:{wa}")
            if state:
                return await self._continue_step(wa, role, state, text)
            return await self._free_text(wa, role, text)

        return await self._on_tap(wa, role, msg.reply_id)

    # ------------------------------------------------------------------ safety & consent
    async def _emergency(self, wa: str, text: str) -> None:
        body = (
            f"🚨 If this is an emergency, call *{self.s.emergency_phone}* now or come straight to the "
            f"{self.s.hospital_name} Emergency Department. Don't wait for a reply here.\n\n"
            "I've also alerted our team to contact you."
        )
        await self.send.text(wa, body)
        ticket_id = await asyncio.to_thread(self.handoffs.open, wa, PATIENT, "emergency", text)
        await asyncio.to_thread(self.audit.log, wa, PATIENT, "emergency", "sent", text, body)
        await self._alert(ticket_id)

    async def _consent(self, wa: str, msg: Inbound) -> None:
        if msg.kind == "reply" and msg.reply_id == "consent_yes":
            await self.store.set(f"consent:{wa}", datetime.now().isoformat())
            await asyncio.to_thread(self.audit.log, wa, PATIENT, "consent", "given")
            return await self._menu(wa, PATIENT)
        if msg.kind == "reply" and msg.reply_id == "consent_no":
            return await self._reply(wa, PATIENT, "consent", "declined", "",
                                     f"No problem. You can call us on {self.s.front_desk_phone}. "
                                     "Message *hi* any time if you change your mind.")
        await self.send.buttons(
            wa,
            f"Welcome to {self.s.hospital_name} on WhatsApp.\n\n"
            "I'm an automated assistant. I can't give medical advice. To help you I'll process the "
            "messages you send and, if you ask, look up your appointments or report status.\n\n"
            f"Privacy notice: {self.s.privacy_notice_url}\n\nDo you agree?",
            [Button("consent_yes", "I agree"), Button("consent_no", "No thanks")],
        )

    # ------------------------------------------------------------------ menus
    async def _menu(self, wa: str, role: str) -> None:
        await self.store.delete(f"state:{wa}")
        if role in (STAFF, CLINICIAN):
            label = "Clinician" if role == CLINICIAN else "Staff"
            await self.send.buttons(
                wa,
                f"{label} assistant. Type your question about a policy, SOP"
                f"{', protocol or formulary' if role == CLINICIAN else ''} and I'll answer from the "
                "approved documents, with the source.",
                [Button("human", "Talk to a person"), Button("logout", "Log out")],
            )
            return
        await self.send.list(
            wa, f"How can {self.s.hospital_name} help you today? You can also just type your question.",
            "Choose", [
                ListRow("appts", "My appointments", "See upcoming appointments"),
                ListRow("reports", "Report status", "Check if lab reports are ready"),
                ListRow("doctors", "Find a doctor", "Departments and OP days"),
                ListRow("book", "Book appointment", "Request a booking"),
                ListRow("ask", "Ask a question", "Timings, directions, facilities"),
                ListRow("human", "Talk to a person", "Our team will contact you"),
            ],
        )

    async def _on_tap(self, wa: str, role: str, reply_id: str) -> None:
        if reply_id == "menu":
            return await self._menu(wa, role)
        if reply_id == "human":
            return await self._handoff(wa, role, "requested", "")
        if reply_id == "logout":
            await self.identity.logout(wa)
            return await self._reply(wa, role, "logout", "ok", "", "You're logged out.")
        if reply_id == "ask":
            return await self._reply(wa, role, "menu", "ask", "", "Sure, type your question.")
        if reply_id in ("appts", "reports"):
            return await self._with_verified_patient(wa, role, reply_id)
        if reply_id in ("doctors", "book"):
            return await self._departments(wa, role, reply_id)
        if reply_id.startswith("dept:"):
            _, purpose, dept = reply_id.split(":", 2)
            return await self._show_department(wa, role, purpose, dept)
        if reply_id.startswith("bookreq:"):
            return await self._handoff(wa, role, "booking", f"Booking request: {reply_id[8:]}")
        return await self._menu(wa, role)

    # ------------------------------------------------------------------ duty-staff alerts
    async def _alert(self, ticket_id: int) -> None:
        if not self.alerts:
            return
        ticket = await asyncio.to_thread(self.handoffs.get, ticket_id)
        try:
            await self.alerts.check_ticket(ticket)
        except Exception:  # never let an alert failure stop the person getting their reply
            log.exception("Alerting for ticket #%s failed; the escalation loop will retry later tiers", ticket_id)

    async def _ack_alert(self, wa: str, role: str, raw_id: str) -> None:
        if not self.alerts or not raw_id.isdigit() or not await self.alerts.can_ack(wa, int(raw_id)):
            return await self._menu(wa, role)
        ticket = await asyncio.to_thread(self.handoffs.get, int(raw_id))
        if not ticket:
            return await self._menu(wa, role)
        member = self.identity.directory.lookup(wa)
        name = member.name if member else f"+{wa}"
        link = f"{self.s.public_base_url.rstrip('/')}/desk/t/{ticket.id}"
        if ticket.status == "open":
            await asyncio.to_thread(self.handoffs.claim, ticket.id, name)
            await asyncio.to_thread(self.handoffs.note, ticket.id, name, f"{name} took this from the WhatsApp alert")
            body = (f"Ticket #{ticket.id} is yours. Call them now: +{ticket.wa_id}\n"
                    f"Details and reply: {link}")
            outcome = "claimed"
        elif ticket.status == "claimed":
            body = f"Ticket #{ticket.id} was already taken by {ticket.assigned_to}. No action needed unless they ask."
            outcome = "already_claimed"
        else:
            body = f"Ticket #{ticket.id} is already resolved."
            outcome = "already_resolved"
        await self.send.text(wa, body)
        await asyncio.to_thread(self.audit.log, wa, role, "alert_ack", outcome, "", body, [f"handoff#{ticket.id}"])

    # ------------------------------------------------------------------ live handoff
    async def _to_live_ticket(self, wa: str, role: str, ticket_id: int, text: str) -> None:
        if _GREETING.match(text):  # "menu" hands the person back to the bot
            await asyncio.to_thread(self.handoffs.end_live, ticket_id)
            return await self._menu(wa, role)
        await asyncio.to_thread(self.handoffs.add_inbound, ticket_id, text)
        await asyncio.to_thread(self.audit.log, wa, role, "handoff_live", "forwarded", text)
        # Acknowledge at most every 30 minutes so a back-and-forth doesn't fill up with bot notes.
        if await self.store.set_if_absent(f"ack:{ticket_id}", 1800):
            await self.send.text(wa, f"Passed to our team (ref #{ticket_id}). Type *menu* to go back to the assistant.")

    # ------------------------------------------------------------------ desk sign-in
    async def _desk_link(self, wa: str, role: str) -> None:
        member = self.identity.directory.lookup(wa)
        if role not in (STAFF, CLINICIAN) or not member or not member.desk:
            return await self._reply(wa, role, "desk", "denied", "",
                                     "Desk access is for authorised staff. Type *login* first, or ask IT "
                                     "to enable desk access for your number.")
        token = await issue_login_token(self.store, member.employee_id, member.name)
        link = f"{self.s.public_base_url.rstrip('/')}/desk/login?token={token}"
        await self.send.text(wa, f"Your handoff desk sign-in link (valid 5 minutes, one use):\n{link}")
        await asyncio.to_thread(self.audit.log, wa, role, "desk", "link_sent", "", "[desk link]")

    # ------------------------------------------------------------------ staff login
    async def _start_login(self, wa: str, role: str) -> None:
        if role in (STAFF, CLINICIAN):
            return await self._menu(wa, role)
        if await self.identity.is_locked(wa):
            return await self._reply(wa, role, "login", "locked", "",
                                     "Too many attempts. Please try again later or contact IT.")
        if not self.identity.directory.lookup(wa):
            return await self._reply(wa, role, "login", "not_in_directory", "",
                                     "This number isn't in the staff directory. Please ask HR to update "
                                     "your registered mobile number.")
        await self.store.set(f"state:{wa}", {"step": "employee_id"}, STATE_SECONDS)
        await self._reply(wa, role, "login", "prompted", "", "Please type your employee ID.")

    # ------------------------------------------------------------------ patient verification
    async def _with_verified_patient(self, wa: str, role: str, action: str) -> None:
        uhid = await self.store.get(f"patient:{wa}")
        if uhid:
            return await self._patient_action(wa, role, uhid, action)
        if await self.store.get(f"plock:{wa}"):
            return await self._reply(wa, role, "verify", "locked", "",
                                     f"Too many attempts. Please try later or call {self.s.front_desk_phone}.")
        await self.store.set(f"state:{wa}", {"step": "uhid", "next": action}, STATE_SECONDS)
        await self._reply(wa, role, "verify", "prompted", "",
                          "To protect your privacy, please type the patient's UHID "
                          "(it's printed on the hospital card or bill).")

    async def _continue_step(self, wa: str, role: str, state: dict, text: str) -> None:
        step = state.get("step")
        if step == "employee_id":
            await self.store.delete(f"state:{wa}")
            member = await self.identity.check_employee_id(wa, text)
            if member:
                await self._reply(wa, member.role, "login", "success", "[employee id]",
                                  f"Welcome, {member.name}. You're logged in for {self.s.staff_session_hours} hours.")
                return await self._menu(wa, member.role)
            return await self._reply(wa, role, "login", "failed", "[employee id]",
                                     "That didn't match our records. Type *login* to try again.")
        if step == "uhid":
            state.update(step="dob", uhid=text.strip().upper())
            await self.store.set(f"state:{wa}", state, STATE_SECONDS)
            return await self._reply(wa, role, "verify", "uhid", "[uhid]",
                                     "Thanks. Now the patient's date of birth as DD-MM-YYYY.")
        if step == "dob":
            await self.store.delete(f"state:{wa}")
            dob = _parse_dob(text)
            patient = await self.his.verify_patient(state["uhid"], dob) if dob else None
            if not patient:
                if await self.store.incr(f"pattempts:{wa}", 1800) >= 3:
                    await self.store.set(f"plock:{wa}", True, 1800)
                return await self._reply(wa, role, "verify", "failed", "[dob]",
                                         "Those details didn't match. Please check the UHID and date of birth "
                                         "and choose the option again from the *menu*.")
            await self.store.delete(f"pattempts:{wa}")
            await self.store.set(f"patient:{wa}", patient.uhid, PATIENT_VERIFY_SECONDS)
            return await self._patient_action(wa, role, patient.uhid, state["next"])
        await self.store.delete(f"state:{wa}")
        return await self._menu(wa, role)

    async def _patient_action(self, wa: str, role: str, uhid: str, action: str) -> None:
        if action == "appts":
            appts = await self.his.upcoming_appointments(uhid)
            body = "No upcoming appointments found." if not appts else "Upcoming appointments:\n" + "\n".join(
                f"• {a.when:%a %d %b, %I:%M %p} — {a.doctor} ({a.department}), {a.location}" for a in appts
            )
        else:
            reports = await self.his.report_status(uhid)
            body = "No recent reports found." if not reports else "Report status:\n" + "\n".join(
                f"• {r.name} ({r.ordered_on:%d %b}): {'✅ Ready' if r.ready else '⏳ In progress'}" for r in reports
            ) + f"\n\n{self.s.report_pickup_note}\nFor questions about results, please speak to your doctor."
        await self.send.buttons(wa, body, [Button("menu", "Menu"), Button("human", "Talk to a person")])
        await asyncio.to_thread(self.audit.log, wa, role, f"his_{action}", "shown", "", body)

    # ------------------------------------------------------------------ doctors & booking
    async def _departments(self, wa: str, role: str, purpose: str) -> None:
        depts = await self.his.departments()
        await self.send.list(wa, "Which department?", "Departments",
                             [ListRow(f"dept:{purpose}:{d}", d) for d in depts[:10]])

    async def _show_department(self, wa: str, role: str, purpose: str, dept: str) -> None:
        doctors = await self.his.doctors(dept)
        body = f"*{dept}*\n" + "\n".join(f"• {d.name}: {d.op_days}" for d in doctors) if doctors \
            else f"No doctors listed for {dept}."
        buttons = [Button(f"bookreq:{dept}", "Request booking"), Button("menu", "Menu")]
        await self.send.buttons(wa, body, buttons)
        await asyncio.to_thread(self.audit.log, wa, role, "his_doctors", "shown", dept, body)

    # ------------------------------------------------------------------ free text
    async def _free_text(self, wa: str, role: str, text: str) -> None:
        if role == PATIENT and is_clinical_question(text):
            return await self._handoff(wa, role, "clinical", text,
                                       lead="I can't give medical advice here, but I've asked our team "
                                            "to get back to you.")
        result = await self.answerer.answer(text, role)
        if result.ok:
            await self.send.buttons(wa, result.text, [Button("menu", "Menu"), Button("human", "Talk to a person")])
            return await asyncio.to_thread(self.audit.log, wa, role, "kb_answer", "answered", text,
                                           result.text, result.chunk_ids)
        await asyncio.to_thread(self.audit.log, wa, role, "kb_answer", result.reason, text, "", result.chunk_ids)
        if result.reason == "needs_human":
            return await self._handoff(wa, role, "needs_human", text)
        await self.send.buttons(
            wa,
            "I don't have an approved answer to that, so I won't guess. Would you like someone from "
            "our team to help?",
            [Button("human", "Talk to a person"), Button("menu", "Menu")],
        )

    # ------------------------------------------------------------------ helpers
    async def _handoff(self, wa: str, role: str, reason: str, text: str, lead: str = "") -> None:
        ticket = await asyncio.to_thread(self.handoffs.open, wa, role, reason, text)
        await self._alert(ticket)
        body = (f"{lead + ' ' if lead else ''}I've passed this to our team (ref #{ticket}). "
                "Someone will contact you on this number.")
        if role == PATIENT:
            body += f"\n\nIf it's urgent, call {self.s.emergency_phone}."
        await self._reply(wa, role, "handoff", reason, text, body)

    async def _reply(self, wa: str, role: str, route: str, outcome: str, inbound: str, body: str) -> None:
        await self.send.text(wa, body)
        await asyncio.to_thread(self.audit.log, wa, role, route, outcome, inbound, body)


def _parse_dob(text: str) -> date | None:
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %m %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None
