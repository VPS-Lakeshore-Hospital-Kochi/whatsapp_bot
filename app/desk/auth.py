"""Sign-in for the handoff dashboard, without passwords.

A staff member who is logged in to the bot and marked `desk=yes` in the staff directory
messages the bot "desk". The bot replies (on their own WhatsApp) with a one-time link valid
for 5 minutes. Opening it and pressing "Continue" starts a 12-hour browser session.

Why a button rather than signing in on page load: link scanners and previews sometimes fetch
URLs, which would use up a single-use token.
"""

import ipaddress
import secrets
from dataclasses import dataclass

from app.store import Store

LOGIN_TOKEN_SECONDS = 300
COOKIE = "lk_desk"


@dataclass
class DeskUser:
    employee_id: str
    name: str
    csrf: str


async def issue_login_token(store: Store, employee_id: str, name: str) -> str:
    token = secrets.token_urlsafe(32)
    await store.set(f"desk_token:{token}", {"employee_id": employee_id, "name": name}, LOGIN_TOKEN_SECONDS)
    return token


async def redeem_login_token(store: Store, token: str, session_seconds: int) -> str | None:
    """Single use: returns a new session id, or None if the token is unknown/expired/used."""
    key = f"desk_token:{token}"
    data = await store.get(key)
    if not data:
        return None
    await store.delete(key)
    sid = secrets.token_urlsafe(32)
    await store.set(f"desk_sess:{sid}", {**data, "csrf": secrets.token_urlsafe(24)}, session_seconds)
    return sid


async def session_user(store: Store, sid: str | None) -> DeskUser | None:
    if not sid:
        return None
    data = await store.get(f"desk_sess:{sid}")
    return DeskUser(**data) if data else None


async def end_session(store: Store, sid: str | None) -> None:
    if sid:
        await store.delete(f"desk_sess:{sid}")


def network_allowed(client_ip: str | None, allowed_cidrs: str) -> bool:
    if not allowed_cidrs.strip():
        return True
    if not client_ip:
        return False
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(c.strip(), strict=False) for c in allowed_cidrs.split(",") if c.strip())
