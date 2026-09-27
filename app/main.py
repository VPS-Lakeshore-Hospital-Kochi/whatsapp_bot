"""FastAPI entry point: Meta webhook verification + inbound message handling."""

import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response

from app.answer import LLM, Answerer
from app.audit import Audit
from app.config import get_settings
from app.his import make_his
from app.identity import Identity, StaffDirectory
from app.knowledge.loader import load_knowledge
from app.knowledge.retriever import Retriever
from app.router import Router
from app.store import make_store
from app.whatsapp.client import CloudApiSender
from app.whatsapp.parse import parse_webhook
from app.whatsapp.security import verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lakeshore-bot")


def build_router(settings) -> Router:
    store = make_store(settings.redis_url)
    return Router(
        settings=settings,
        store=store,
        sender=CloudApiSender(settings.wa_access_token, settings.wa_phone_number_id, settings.wa_graph_version),
        identity=Identity(
            StaffDirectory.from_csv(settings.staff_directory_csv), store, settings.staff_session_hours,
            settings.max_login_attempts, settings.lockout_minutes,
        ),
        answerer=Answerer(Retriever(load_knowledge(settings.knowledge_dir)), LLM(settings), settings),
        his=make_his(settings),
        audit=Audit(settings.database_url, settings.audit_hash_secret),
    )


settings = get_settings()
app = FastAPI(title="Lakeshore WhatsApp Bot", docs_url=None, redoc_url=None)
router = build_router(settings)


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
        background.add_task(_safe_handle, msg)
    # Acknowledge immediately; Meta retries if we are slow, and the router de-duplicates.
    return {"ok": True}


async def _safe_handle(msg):
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
