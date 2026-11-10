"""Hermes registration for the Telegram agent computer: the Computer card and the Mini App backend."""

from __future__ import annotations

import asyncio
import logging
import sys
import types

from gateway.session_context import get_session_env

try:
    from .computer import MAX_REASON, ComputerService, GatewayRuntime
    from .viewer import SHARED_MODULE, BotDesktop, ViewerHub
except ImportError:  # loaded as a top-level module by some plugin hosts
    from computer import MAX_REASON, ComputerService, GatewayRuntime
    from viewer import SHARED_MODULE, BotDesktop, ViewerHub

logger = logging.getLogger(__name__)

TOOL_SCHEMA = {
    "name": "telegram_agent_computer",
    "description": (
        "Ask the user in this private Telegram chat to take over your screen when only a human can go on: "
        "sign-in, 2FA, CAPTCHA, payment confirmation. The Computer card shows the request; the user taps "
        "Open screen, Take over, does it on the live screen and hands control back. Call it once, then end "
        "the turn. For anything the user may need to watch or take over, browse with the browser_* tools "
        "(the headed browser on this screen), not the camofox tools, whose browser is hidden."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": (
                f"What the user should do, one line, at most {MAX_REASON} characters, e.g. "
                "\"Sign in to GitHub\" or \"Enter the SMS code from your bank\".")},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}


def _guidance(session_info) -> str:
    if str(session_info.get("platform", "")).lower() != "telegram":
        return ""
    return (
        "Your browser_* tools and computer_use drive your own screen, which the user can watch live from the "
        "Computer card in this chat. Browse with browser_* (not camofox) whenever the user may want to watch "
        "or take over. When you need the user on the screen (sign-in, 2FA, CAPTCHA, payment), call "
        "telegram_agent_computer with a one-line reason and end the turn; never ask for passwords in chat."
    )


_service: ComputerService | None = None
_hub: ViewerHub | None = None


def _shared() -> types.ModuleType:
    """In-process registry the dashboard half (a separately imported module) reads."""
    module = sys.modules.get(SHARED_MODULE)
    if module is None:
        module = types.ModuleType(SHARED_MODULE)
        sys.modules[SHARED_MODULE] = module
    return module


def register(ctx) -> None:
    global _service, _hub
    runtime = _service.runtime if _service is not None else None
    desktop = BotDesktop()
    _service = ComputerService(ctx, lambda key, default="": get_session_env(key, default))
    _service.runtime = runtime
    _service.human_holds = desktop.human_holds
    _hub = ViewerHub(desktop, lambda: getattr(getattr(_service.runtime, "bot", None), "token", "") or "", _service)
    shared = _shared()
    shared.hub = _hub

    ctx.register_tool(name="telegram_agent_computer", toolset="telegram_agent_computer", schema=TOOL_SCHEMA,
                      handler=_service.handle_tool, emoji="🖥")
    ctx.register_hook("pre_tool_call", _service.on_pre_tool_call)
    ctx.register_hook("post_tool_call", _service.on_post_tool_call)
    ctx.register_hook("post_llm_call", _service.on_post_llm_call)
    ctx.register_system_prompt_section("telegram-agent-computer.workflow", _guidance, position="after_memory",
                                       max_chars=600)

    def _on_lease(lease) -> None:
        _service.on_lease(lease)
        _hub.on_lease(lease)

    def _wire(application, adapter) -> None:
        del adapter
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        _service.runtime = GatewayRuntime(application.bot, loop)
        previous = getattr(shared, "unsubscribe", None)
        if callable(previous):
            previous()
        try:
            _service.on_lease(desktop.lease())  # the current holder, so the first hand-back is recognised
            shared.unsubscribe = desktop.on_change(_on_lease)
        except Exception:
            logger.warning("agent computer: Bot Desktop lease unavailable; the card will not follow takeovers",
                           exc_info=True)

    ctx.register_platform_handler("telegram", _wire)
