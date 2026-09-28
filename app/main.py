"""FastAPI entry point: Meta webhook, and the handoff dashboard under /desk."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles

from app.alerts import Alerts
from app.answer import LLM, Answerer
from app.audit import Audit
from app.config import get_settings
from app.desk.routes import build_desk_router, unauthorised_to_login
from app.handoffs import Handoffs
from app.his import make_his
from app.identity import Identity, StaffDirectory
from app.knowledge.loader import load_knowledge
from app.knowledge.retriever import Retriever
from app.roster import RosterStore
from app.router import Router
from app.store import make_store
from app.whatsapp.client import CloudApiSender
from app.whatsapp.parse import parse_webhook
from app.whatsapp.security import verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lakeshore-bot")

DESK_HEADERS = {
    # Patient data is on these pages: no framing, no caching, no referrer (sign-in links carry a token).
    "Content-Security-Policy": "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
                               "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def build_app(settings) -> FastAPI:
    store = make_store(settings.redis_url)
    audit = Audit(settings.database_url, settings.audit_hash_secret)
    handoffs = Handoffs(audit.engine)
    sender = CloudApiSender(settings.wa_access_token, settings.wa_phone_number_id, settings.wa_graph_version)
    roster = RosterStore(audit.engine, seed_csv=settings.duty_roster_csv)
    alerts = Alerts(settings, store, sender, handoffs, roster=roster)
    directory = StaffDirectory.from_csv(settings.staff_directory_csv)
    router = Router(
        settings=settings,
        store=store,
        sender=sender,
        identity=Identity(
            directory, store, settings.staff_session_hours,
            settings.max_login_attempts, settings.lockout_minutes,
        ),
        answerer=Answerer(Retriever(load_knowledge(settings.knowledge_dir)), LLM(settings), settings),
        his=make_his(settings),
        audit=audit,
        handoffs=handoffs,
        alerts=alerts,
    )

    @asynccontextmanager
    async def lifespan(_app):
        # Escalation runs in the background: re-alerts the next tier while an emergency sits unclaimed.
        task = asyncio.create_task(alerts.run_forever())
        yield
        task.cancel()

    app = FastAPI(title="Lakeshore WhatsApp Bot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.bot = SimpleNamespace(settings=settings, store=store, sender=sender, audit=audit,
                                    handoffs=handoffs, router=router, alerts=alerts, roster=roster,
                                    directory=directory)
    app.include_router(build_desk_router(app.state.bot))
    app.mount("/desk/static", StaticFiles(directory=Path(__file__).parent / "desk" / "static"), name="desk-static")
    app.add_exception_handler(HTTPException, unauthorised_to_login)

    @app.middleware("http")
    async def desk_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/desk"):
            response.headers.update(DESK_HEADERS)
        return response

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/webhook")
    async def verify(
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ):
        if mode == "subscribe" and settings.wa_verify_token and token == settings.wa_verify_token:
            return Response(content=challenge, media_type="text/plain")
        raise HTTPException(status_code=403)

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks):
        raw = await request.body()
        if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), settings.wa_app_secret):
            raise HTTPException(status_code=401)
        for msg in parse_webhook(await request.json()):
            background.add_task(_safe_handle, router, settings, msg)
        # Acknowledge immediately; Meta retries if we are slow, and the router de-duplicates.
        return {"ok": True}

    return app


async def _safe_handle(router: Router, settings, msg):
    try:
        await router.handle(msg)
    except Exception:
        log.exception("Failed handling message %s", msg.message_id)
        try:
            await router.send.text(
                msg.wa_id,
                f"Sorry, something went wrong on our side. For urgent help call {settings.emergency_phone}.",
            )
        except Exception:
            log.exception("Failed sending error notice")


app = build_app(get_settings())
