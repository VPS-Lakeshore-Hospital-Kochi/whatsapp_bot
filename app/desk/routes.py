"""Handoff dashboard (/desk): the queue of tickets the bot has passed to people."""

import asyncio
import hmac
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.desk.auth import COOKIE, DeskUser, end_session, network_allowed, redeem_login_token, session_user
from app.handoffs import IST_OFFSET, Ticket
from app.roster import RosterError

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

MAX_REPLY = 1000
MAX_ROSTER_BYTES = 256 * 1024


def _ist(dt: datetime | None, fmt: str = "%d %b, %I:%M %p") -> str:
    return dt.astimezone(timezone(IST_OFFSET)).strftime(fmt) if dt else ""


def _ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    minutes = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // 1440} days"


def _ago_phrase(dt: datetime | None) -> str:
    ago = _ago(dt)
    return ago if ago in ("", "just now") else f"{ago} ago"


def _masked(wa_id: str) -> str:
    return f"+{wa_id[:2]} ••••• {wa_id[-4:]}" if len(wa_id) > 6 else wa_id


templates.env.filters.update(ist=_ist, ago=_ago, ago_phrase=_ago_phrase, masked=_masked)


def build_desk_router(app_state) -> APIRouter:
    """app_state exposes .settings, .store, .handoffs, .sender, .audit (the running bot's objects)."""
    r = APIRouter(prefix="/desk")
    s = app_state.settings

    def page(request: Request, name: str, user: DeskUser | None, **ctx) -> HTMLResponse:
        resp = templates.TemplateResponse(request, name, {"user": user, "hospital": s.hospital_name, **ctx})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def guard_network(request: Request) -> None:
        if not network_allowed(request.client.host if request.client else None, s.desk_allowed_cidrs):
            raise HTTPException(status_code=403)

    async def current_user(request: Request, _=Depends(guard_network)) -> DeskUser:
        user = await session_user(app_state.store, request.cookies.get(COOKIE))
        if not user:
            raise HTTPException(status_code=401)
        return user

    def check_csrf(user: DeskUser, token: str) -> None:
        if not hmac.compare_digest(user.csrf, token):
            raise HTTPException(status_code=403)

    def load(ticket_id: int) -> Ticket:
        ticket = app_state.handoffs.get(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404)
        return ticket

    def back_to(ticket_id: int, flash: str = "") -> RedirectResponse:
        return RedirectResponse(f"/desk/t/{ticket_id}" + (f"?done={flash}" if flash else ""), status_code=303)

    # ------------------------------------------------------------ sign in / out
    @r.get("/login", dependencies=[Depends(guard_network)])
    async def login_page(request: Request, token: str = ""):
        return page(request, "login.html", None, token=token)

    @r.post("/login", dependencies=[Depends(guard_network)])
    async def login(request: Request, token: str = Form("")):
        sid = await redeem_login_token(app_state.store, token, s.desk_session_hours * 3600)
        if not sid:
            return page(request, "login.html", None, token="", expired=True)
        resp = RedirectResponse("/desk", status_code=303)
        resp.set_cookie(COOKIE, sid, max_age=s.desk_session_hours * 3600, httponly=True,
                        secure=s.desk_cookie_secure, samesite="strict", path="/desk")
        return resp

    @r.post("/logout")
    async def logout(request: Request, csrf: str = Form(""), user: DeskUser = Depends(current_user)):
        check_csrf(user, csrf)
        await end_session(app_state.store, request.cookies.get(COOKIE))
        resp = RedirectResponse("/desk/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/desk")
        return resp

    # ------------------------------------------------------------ queue
    @r.get("")
    async def queue(request: Request, view: str = "active", user: DeskUser = Depends(current_user)):
        view = view if view in ("active", "mine", "resolved") else "active"
        tickets, stats = await asyncio.gather(
            asyncio.to_thread(app_state.handoffs.queue, view, user.name),
            asyncio.to_thread(app_state.handoffs.stats),
        )
        return page(request, "queue.html", user, tickets=tickets, stats=stats, view=view, duty=_duty_summary(app_state))

    @r.get("/api/summary")
    async def summary(user: DeskUser = Depends(current_user)):
        stats = await asyncio.to_thread(app_state.handoffs.stats)
        return JSONResponse(stats, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ one ticket
    @r.get("/t/{ticket_id}")
    async def ticket(request: Request, ticket_id: int, done: str = "", user: DeskUser = Depends(current_user)):
        t = load(ticket_id)
        messages = await asyncio.to_thread(app_state.handoffs.messages, ticket_id)
        return page(request, "ticket.html", user, t=t, messages=messages, done=done,
                    can_reply=t.can_reply() and t.status != "resolved", max_reply=MAX_REPLY)

    @r.post("/t/{ticket_id}/claim")
    async def claim(ticket_id: int, csrf: str = Form(""), user: DeskUser = Depends(current_user)):
        check_csrf(user, csrf)
        t = load(ticket_id)
        if t.status == "open":
            await asyncio.to_thread(app_state.handoffs.claim, ticket_id, user.name)
            _audit(app_state, t, "claim", user)
        return back_to(ticket_id)

    @r.post("/t/{ticket_id}/reply")
    async def reply(ticket_id: int, body: str = Form(""), csrf: str = Form(""),
                    user: DeskUser = Depends(current_user)):
        check_csrf(user, csrf)
        t = load(ticket_id)
        body = body.strip()[:MAX_REPLY]
        if not body or t.status == "resolved" or not t.can_reply():
            return back_to(ticket_id)
        if not await app_state.sender.text(t.wa_id, body):
            return back_to(ticket_id, "failed")
        await asyncio.to_thread(app_state.handoffs.reply, ticket_id, user.name, body)
        _audit(app_state, t, "reply", user, body)
        return back_to(ticket_id, "sent")

    @r.post("/t/{ticket_id}/resolve")
    async def resolve(ticket_id: int, note: str = Form(""), csrf: str = Form(""),
                      user: DeskUser = Depends(current_user)):
        check_csrf(user, csrf)
        t = load(ticket_id)
        if t.status != "resolved":
            await asyncio.to_thread(app_state.handoffs.resolve, ticket_id, user.name, note.strip()[:500])
            _audit(app_state, t, "resolve", user)
        return RedirectResponse("/desk", status_code=303)

    @r.post("/t/{ticket_id}/reopen")
    async def reopen(ticket_id: int, csrf: str = Form(""), user: DeskUser = Depends(current_user)):
        check_csrf(user, csrf)
        t = load(ticket_id)
        if t.status == "resolved":
            await asyncio.to_thread(app_state.handoffs.reopen, ticket_id, user.name)
            _audit(app_state, t, "reopen", user)
        return back_to(ticket_id)

    # ------------------------------------------------------------ duty roster
    def roster_admin(user: DeskUser) -> bool:
        member = app_state.directory.by_employee_id(user.employee_id)
        return bool(member and member.roster)

    def roster_page(request: Request, user: DeskUser, **ctx) -> HTMLResponse:
        now = datetime.now(timezone.utc)
        roster = app_state.roster.current()
        return page(request, "roster.html", user, view="roster", now=now,
                    tiers=app_state.alerts.tiers_at(now),
                    gaps=roster.gaps(now, 24, tier=1) if roster else [],
                    upcoming=roster.upcoming(now, 24) if roster else [],
                    version=app_state.roster.latest(), has_roster=roster is not None,
                    can_upload=roster_admin(user), escalate=s.alert_escalate_minutes, **ctx)

    @r.get("/roster")
    async def roster(request: Request, done: str = "", user: DeskUser = Depends(current_user)):
        return roster_page(request, user, done=done)

    @r.get("/roster.csv")
    async def roster_csv(user: DeskUser = Depends(current_user)):
        version = app_state.roster.latest()
        if not version:
            raise HTTPException(status_code=404)
        return Response(version.content, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="duty-roster-v{version.id}.csv"',
            "Cache-Control": "no-store"})

    @r.post("/roster")
    async def upload_roster(request: Request, file: UploadFile = File(...), csrf: str = Form(""),
                            user: DeskUser = Depends(current_user)):
        check_csrf(user, csrf)
        if not roster_admin(user):
            raise HTTPException(status_code=403)
        raw = await file.read(MAX_ROSTER_BYTES + 1)
        if len(raw) > MAX_ROSTER_BYTES:
            return roster_page(request, user, problems=["The file is too big (limit 256 KB)."])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return roster_page(request, user, problems=["Save the file as CSV (UTF-8) and try again."])
        try:
            version = await asyncio.to_thread(app_state.roster.upload, text, f"{user.name} ({user.employee_id})")
        except RosterError as e:
            return roster_page(request, user, problems=e.problems[:30])
        app_state.audit.log("desk", "staff", "roster", f"upload:{user.employee_id}", "", "",
                            [f"roster#{version.id}"])
        return RedirectResponse("/desk/roster?done=uploaded", status_code=303)

    return r


def _duty_summary(app_state) -> dict:
    now = datetime.now(timezone.utc)
    tiers = app_state.alerts.tiers_at(now)
    roster = app_state.roster.current()
    gaps = roster.gaps(now, 24, tier=1) if roster else []
    return {"first": tiers[0] if tiers else [], "gap": gaps[0] if gaps else None, "has_roster": roster is not None}


def _audit(app_state, t: Ticket, action: str, user: DeskUser, body: str = "") -> None:
    app_state.audit.log(t.wa_id, t.role, "desk", f"{action}:{user.employee_id}", "", body, [f"handoff#{t.id}"])


async def unauthorised_to_login(request: Request, exc: HTTPException) -> Response:
    """Send browsers without a session to the sign-in page instead of a bare 401."""
    if exc.status_code == 401 and request.url.path.startswith("/desk") and "/api/" not in request.url.path:
        return RedirectResponse("/desk/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
