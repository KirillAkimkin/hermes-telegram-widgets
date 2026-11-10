"""Dashboard half of telegram-agent-computer: the Mini App's control socket.

Mounted by the dashboard at ``/api/plugins/telegram-agent-computer``. Only a WebSocket route: plain HTTP
plugin routes require a dashboard sign-in cookie the Telegram Mini App does not have, while a plugin
WebSocket passes the server's Host/Origin and plugin-consent gate (``PluginApiWebSocketGate``) and then
authenticates itself here with Telegram ``initData``.

That gate compares only the Origin's hostname, so a page on another port or scheme of the same host
would pass it. Before accepting, this route therefore requires the Origin to be exactly the origin of
``webapp.public_url`` (scheme, host and port), the address the Mini App is served from; a missing
Origin, or a config it cannot read, is refused (4403).

This file is imported as its own module (``hermes_dashboard_plugin_telegram-agent-computer``), so it
reaches the agent half (bot token, cards, lease) through an in-process registry in ``sys.modules``
that the agent half's ``register()`` fills. Both run in the gateway process.
"""

from __future__ import annotations

import logging
import sys
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket

logger = logging.getLogger(__name__)

SHARED_MODULE = "hermes_telegram_agent_computer_shared"
CLOSE_FORBIDDEN = 4403
_DEFAULT_PORTS = {"http": 80, "https": 443}

router = APIRouter()


def origin_of(url: str, *, bare: bool = False) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of an http(s) URL, the port made explicit; None when it is not one.
    ``bare``: *url* must be a serialized Origin (no user info, path, query or fragment)."""
    if not isinstance(url, str) or not url or not url.isascii() or url != url.strip():
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host or parts.username is not None or parts.password is not None:
        return None
    if bare and (parts.path or parts.query or parts.fragment or url.endswith(("?", "#"))):
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def public_origin() -> tuple[str, str, int] | None:
    """The origin of the gateway profile's ``webapp.public_url``; None when unset or unusable."""
    from gateway.webapp_listener import webapp_settings
    from hermes_constants import get_hermes_home
    return origin_of(webapp_settings(get_hermes_home()).public_url or "")


def origin_allowed(header: str | None) -> bool:
    """True only for an Origin equal to the ``webapp.public_url`` origin. Fails closed."""
    if not header:
        return False
    try:
        expected = public_origin()
    except Exception:
        logger.warning("agent computer: webapp.public_url unreadable; refusing the control socket", exc_info=True)
        return False
    return expected is not None and origin_of(header, bare=True) == expected


@router.websocket("/ws")
async def agent_computer_ws(ws: WebSocket) -> None:
    if not origin_allowed(ws.headers.get("origin")):
        logger.info("agent computer: refused a control socket from a foreign or missing Origin")
        await ws.close(code=CLOSE_FORBIDDEN, reason="origin not allowed")
        return
    await ws.accept()
    hub = getattr(sys.modules.get(SHARED_MODULE), "hub", None)
    if hub is None:
        await ws.close(code=1011, reason="telegram-agent-computer is not loaded in this process")
        return
    await hub.serve(ws)
