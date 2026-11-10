"""The Mini App's control socket: Telegram ``initData`` auth, display tickets and Take over / Hand back.

The dashboard half (``dashboard/api.py``) mounts ``/api/plugins/telegram-agent-computer/ws``, refuses
any Origin but the ``webapp.public_url`` origin, and hands every connection to :meth:`ViewerHub.serve`.
Plain HTTP plugin routes need a dashboard cookie the Mini App does not have, so this route
authenticates itself:

1. The first message must be ``{"init": "<Telegram.WebApp.initData>"}``. It is verified with the bot
   token (HMAC, <= 5 min old) and the user must be the home-channel owner
   (``platforms.telegram.home_channel.user_id``) and in ``TELEGRAM_ALLOWED_USERS``; anything else
   closes 4401.
2. Every connection gets its own viewer id, server-side only (viewer ids are lease capabilities), so one
   Mini App closing its screen never releases a lease another one holds. The hub starts the screen if
   needed and answers ``{"type": "ready", "ticket", "path", "lease", "card"}``. The page opens
   ``/api/display/ws?display_ticket=`` with that single-use 30 s ticket.
3. ``{"op": "take"}`` / ``{"op": "back"}`` acquire / release the lease for that viewer id (a Hand back
   from a connection that does not hold it changes nothing); ``{"op": "ticket"}`` mints a fresh ticket
   for a reconnect. Lease changes are pushed as ``{"type": "lease"}``. Every other message is refused.
4. A connection lives at most ``SESSION_LIFETIME``; then it closes 4401 "session expired" and the page
   asks the user to reopen the screen from the Computer card (fresh initData).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

SHARED_MODULE = "hermes_telegram_agent_computer_shared"  # sys.modules registry the dashboard half reads
MAX_AUTH_AGE = 300
SESSION_LIFETIME = 1800  # a control socket outlives its initData check by at most this long
MAX_MESSAGE = 8192
DISPLAY_PATH = "/api/display/ws"
INIT_TIMEOUT = 20
LEASE_POLL = 3.0  # lease changes made by another process are observed by reading
TICKETS_PER_MINUTE = 30
CLOSE_BAD_AUTH = 4401  # also "session expired"
EXPIRED = "session expired"
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
_START_RE = re.compile(r"^c_([0-9a-f]{8})$")
_OPS = frozenset({"take", "back", "ticket"})


def allowed_users(env: dict[str, str] | None = None) -> set[str]:
    raw = (env if env is not None else os.environ).get("TELEGRAM_ALLOWED_USERS", "")
    return {part.strip() for part in raw.split(",") if part.strip().isdigit()}


def home_channel_owner() -> str:
    """``platforms.telegram.home_channel.user_id`` of the gateway's profile, or "" when unset."""
    from hermes_cli.config import load_config_readonly
    node: Any = load_config_readonly()
    for key in ("platforms", "telegram", "home_channel", "user_id"):
        node = node.get(key) if isinstance(node, dict) else None
    owner = str(node).strip() if isinstance(node, (str, int)) and not isinstance(node, bool) else ""
    return owner if owner.isdigit() else ""


def desktop_users(env: dict[str, str], owner: str) -> set[str]:
    """Who may open the desktop: the home-channel owner, and only while they are an allowed user."""
    return {owner} & allowed_users(env) if owner else set()


def verify_init_data(init_data: Any, bot_token: str, allowed: set[str], *, now: float | None = None,
                     max_age: int = MAX_AUTH_AGE) -> dict[str, Any] | None:
    """The Telegram user of a valid Mini App ``initData``, or None.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app: the data-check
    string is every field but ``hash``, sorted, ``key=value`` joined by newlines; the secret key is
    HMAC-SHA256(key="WebAppData", msg=bot_token)."""
    if not isinstance(init_data, str) or not init_data or len(init_data) > MAX_MESSAGE or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        return None  # a repeated key: refuse rather than guess which one Telegram signed
    received = fields.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", received):
        return None
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    now = time.time() if now is None else now
    try:
        auth_date = int(fields.get("auth_date", ""))
        user = json.loads(fields.get("user", ""))
    except ValueError:
        return None
    if not isinstance(user, dict) or not (now - max_age <= auth_date <= now + 60):
        return None
    user_id = user.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or str(user_id) not in allowed:
        return None
    return {"id": user_id, "language_code": str(user.get("language_code", "")),
            "start_param": fields.get("start_param", "")}


class BotDesktop:
    """The in-process Bot Desktop API (lease, runtime, display tickets) of the gateway's profile."""

    def home(self) -> str:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())

    def lease(self) -> Any:
        from tools.bot_desktop import lease
        return lease.get(self.home())

    def human_holds(self) -> bool:
        return self.lease().holder == "human"

    def acquire(self, viewer_id: str, reason: str) -> Any:
        from tools.bot_desktop import lease, runtime
        runtime.touch_activity()
        return lease.acquire(viewer_id, profile_key=self.home(), reason=reason)

    def release(self, viewer_id: str) -> Any:
        from tools.bot_desktop import lease
        return lease.release(viewer_id, profile_key=self.home())

    def on_change(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        from tools.bot_desktop import lease
        return lease.on_change(lambda _key, value: listener(value))

    def running(self) -> bool:
        from tools.bot_desktop import runtime
        return runtime.is_running()

    def start(self) -> None:
        from tools.bot_desktop import runtime
        runtime.start(wait_seconds=15.0)

    def mint(self, viewer_id: str) -> str:
        from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
        return mint_ticket(user_id=f"display:{viewer_id}", provider="bot-desktop",
                           extra={"hermes_home": self.home(), "viewer_id": viewer_id})


def lease_view(lease: Any, viewer_id: str) -> dict[str, Any]:
    """The lease as the page may see it: never a viewer id, only whether it is this viewer's."""
    holder = str(getattr(lease, "holder", "agent"))
    return {"holder": holder, "mine": holder == "human" and getattr(lease, "viewer_id", None) == viewer_id,
            "reason": str(getattr(lease, "reason", "") or "")[:200], "epoch": int(getattr(lease, "epoch", 0) or 0)}


class ViewerHub:
    """Serves Mini App control sockets. ``bot_token`` and ``cards`` come from the agent half."""

    def __init__(self, desktop: Any, bot_token: Callable[[], str], cards: Any = None,
                 env: Callable[[], dict[str, str]] = lambda: dict(os.environ),
                 owner: Callable[[], str] = home_channel_owner, session_lifetime: float = SESSION_LIFETIME):
        self.desktop = desktop
        self.bot_token = bot_token
        self.cards = cards
        self.env = env
        self.owner = owner
        self.session_lifetime = session_lifetime
        self._sessions: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    # ---- lease fan-out (any thread) ------------------------------------------------------------
    def on_lease(self, lease: Any) -> None:
        for loop, queue in list(self._sessions):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, lease)
            except RuntimeError:
                self._sessions.discard((loop, queue))

    def _allowed(self) -> set[str]:
        owner = ""
        try:
            owner = str(self.owner() or "")
        except Exception:
            logger.warning("agent computer viewer: home channel owner unavailable", exc_info=True)
        return desktop_users(self.env(), owner)

    async def _ensure_screen(self) -> None:
        if not await asyncio.to_thread(self.desktop.running):
            await asyncio.to_thread(self.desktop.start)

    # ---- one connection ------------------------------------------------------------------------
    async def serve(self, ws: Any) -> None:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=INIT_TIMEOUT)
        except Exception:
            await self._close(ws, CLOSE_BAD_AUTH, "init required")
            return
        message = _parse(raw)
        token = ""
        try:
            token = self.bot_token() or ""
        except Exception:
            logger.warning("agent computer viewer: bot token unavailable")
        user = None
        if isinstance(message, dict) and set(message) == {"init"}:
            user = verify_init_data(message["init"], token, self._allowed())
        if user is None:
            logger.info("agent computer viewer: refused a connection (bad or missing initData)")
            await self._close(ws, CLOSE_BAD_AUTH, "unauthorized")
            return
        deadline = time.monotonic() + self.session_lifetime
        viewer_id = secrets.token_urlsafe(16)  # per connection: one Mini App closing never frees another's lease
        logger.info("agent computer viewer: Telegram user %s connected", user["id"])
        card = None
        match = _START_RE.fullmatch(user["start_param"])
        if match and self.cards is not None:
            card = self.cards.request_for(match.group(1), str(user["id"]))
        loop, queue = asyncio.get_running_loop(), asyncio.Queue()
        session = (loop, queue)
        self._sessions.add(session)
        state = {"epoch": None, "tickets": []}
        try:
            ready = {"type": "ready", "card": card}
            ready.update(await self._fresh_ticket(viewer_id, state))
            ready["lease"] = await self._lease(viewer_id, state)
            await ws.send_json(ready)
            await self._loop(ws, viewer_id, card, queue, state, deadline)
        except _Closed:
            pass
        except Exception:
            logger.warning("agent computer viewer: connection failed", exc_info=True)
            await self._close(ws, 1011, "server error")
        finally:
            self._sessions.discard(session)

    async def _loop(self, ws: Any, viewer_id: str, card: dict | None, queue: asyncio.Queue, state: dict,
                    deadline: float) -> None:
        receive = asyncio.ensure_future(ws.receive_text())
        pushed = asyncio.ensure_future(queue.get())
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.info("agent computer viewer: session expired")
                    await self._close(ws, CLOSE_BAD_AUTH, EXPIRED)
                    raise _Closed()
                done, _ = await asyncio.wait({receive, pushed}, timeout=min(LEASE_POLL, remaining),
                                             return_when=asyncio.FIRST_COMPLETED)
                if time.monotonic() >= deadline:
                    continue  # nothing more is served: the next round closes
                if pushed in done:
                    lease = pushed.result()
                    pushed = asyncio.ensure_future(queue.get())
                    await self._push(ws, lease_view(lease, viewer_id), state)
                if receive in done:
                    try:
                        raw = receive.result()
                    except Exception:
                        raise _Closed() from None
                    receive = asyncio.ensure_future(ws.receive_text())
                    await self._handle(ws, raw, viewer_id, card, state)
                if not done:  # another process may have changed the lease
                    await self._push(ws, await self._lease(viewer_id, state, record=False), state)
        finally:
            receive.cancel()
            pushed.cancel()

    async def _handle(self, ws: Any, raw: Any, viewer_id: str, card: dict | None, state: dict) -> None:
        if not isinstance(raw, str) or len(raw) > MAX_MESSAGE:
            await self._close(ws, CLOSE_TOO_BIG, "message too large")
            raise _Closed()
        message = _parse(raw)
        op = message.get("op") if isinstance(message, dict) and set(message) == {"op"} else None
        if op not in _OPS:
            await ws.send_json({"type": "error", "code": "unsupported"})
            return
        if op == "ticket":
            reply = {"type": "ticket"}
            reply.update(await self._fresh_ticket(viewer_id, state))
            await ws.send_json(reply)
            return
        if op == "take":
            reason = (card or {}).get("request") or "Taken over from Telegram"
            lease = await asyncio.to_thread(self.desktop.acquire, viewer_id, reason)
        else:
            lease = await asyncio.to_thread(self.desktop.release, viewer_id)
        view = lease_view(lease, viewer_id)
        state["epoch"] = view["epoch"]
        await ws.send_json({"type": "lease", "re": op, "lease": view})

    async def _fresh_ticket(self, viewer_id: str, state: dict) -> dict[str, Any]:
        now = time.monotonic()
        state["tickets"] = [t for t in state["tickets"] if now - t < 60]
        if len(state["tickets"]) >= TICKETS_PER_MINUTE:
            return {"error": "rate_limited"}
        state["tickets"].append(now)
        try:
            await self._ensure_screen()
        except Exception as exc:  # runtime.start names the blocker (packages, memory)
            return {"error": "screen_unavailable", "detail": str(exc)[:200]}
        return {"ticket": await asyncio.to_thread(self.desktop.mint, viewer_id), "path": DISPLAY_PATH}

    async def _lease(self, viewer_id: str, state: dict, *, record: bool = True) -> dict[str, Any]:
        view = lease_view(await asyncio.to_thread(self.desktop.lease), viewer_id)
        if record:
            state["epoch"] = view["epoch"]
        return view

    @staticmethod
    async def _push(ws: Any, view: dict[str, Any], state: dict) -> None:
        if view["epoch"] == state["epoch"]:
            return
        state["epoch"] = view["epoch"]
        await ws.send_json({"type": "lease", "lease": view})

    @staticmethod
    async def _close(ws: Any, code: int, reason: str) -> None:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass


class _Closed(Exception):
    pass


def _parse(raw: Any) -> Any:
    if not isinstance(raw, str) or len(raw) > MAX_MESSAGE:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
