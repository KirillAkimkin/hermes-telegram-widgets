"""Hermes registration for the Telegram progress widget ("Hermes is working" card on long replies)."""

from __future__ import annotations

import asyncio

from gateway.session_context import get_session_env

try:
    from .progress_widget import GatewayRuntime, ProgressService, enable_widget_queries
except ImportError:  # loaded as a top-level module by some plugin hosts
    from progress_widget import GatewayRuntime, ProgressService, enable_widget_queries

WIDGET_QUERY_GROUP = -11  # PTB runs one handler per group; each widget plugin owns its own (-5..-10 taken)

_service: ProgressService | None = None


def register(ctx) -> None:
    global _service
    previous = _service
    _service = ProgressService(ctx, lambda key, default="": get_session_env(key, default))
    if previous is not None:  # a plugin reload keeps the live gateway wiring
        _service.runtime, _service.adapter = previous.runtime, previous.adapter

    ctx.register_hook("pre_llm_call", _service.on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _service.on_pre_tool_call)
    ctx.register_hook("post_tool_call", _service.on_post_tool_call)
    ctx.register_hook("post_llm_call", _service.on_turn_end)
    ctx.register_hook("on_session_end", _service.on_turn_end)  # every turn end, also failed / interrupted
    ctx.register_hook("agent_loop_stopped", _service.on_agent_loop_stopped)

    def _wire(application, adapter) -> None:
        from telegram import Update
        from telegram.ext import TypeHandler

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        _service.runtime = GatewayRuntime(application.bot, loop)
        _service.adapter = adapter  # its message path carries the in-card Stop as a real /stop
        if loop is not None:
            loop.call_soon(_service.sweep)  # cards a previous gateway process left on screen
        # Stop taps inside the widget arrive as widget_query updates; polling must ask Telegram for them.
        if enable_widget_queries(Update):
            application.add_handler(TypeHandler(Update, _service.on_update), group=WIDGET_QUERY_GROUP)

    ctx.register_platform_handler("telegram", _wire)
