"""Hermes registration for the Telegram task activity widget."""

from __future__ import annotations

import asyncio

from gateway.session_context import get_session_env

try:
    from .activity_widget import (MAX_LINE, MAX_STEPS, STATUSES, ActivityService, GatewayRuntime,
                                  enable_widget_queries)
except ImportError:  # loaded as a top-level module by some plugin hosts
    from activity_widget import MAX_LINE, MAX_STEPS, STATUSES, ActivityService, GatewayRuntime, enable_widget_queries

TOOL_SCHEMA = {
    "name": "telegram_task_activity",
    "description": (
        "Show and update one live task card in this private Telegram chat: title, status, progress "
        "segments, the current operation, the last update time and a work log the user opens on tap. "
        "create returns task_id; update and close change that same card in place (no new message). "
        "The user can tap Stop on the card."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "close"]},
            "task_id": {"type": "string", "description": "Required for update and close."},
            "title": {"type": "string", "description": "Short task title, create only (fixed afterwards)."},
            "status": {"type": "string", "enum": [s for s in STATUSES if s != "stopping"],
                       "description": "running by default; close defaults to done."},
            "current": {"type": "string", "description": f"What happens now, one line, at most {MAX_LINE} characters."},
            "step": {"type": "integer", "description": f"Finished steps, 0-{MAX_STEPS}."},
            "steps_total": {"type": "integer", "description": f"Planned steps, 0-{MAX_STEPS}; 0 hides the bar."},
            "log": {"type": "string", "description": "One finished-step line to append to the work log."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _guidance(session_info) -> str:
    if str(session_info.get("platform", "")).lower() != "telegram":
        return ""
    return (
        "For work in this Telegram chat likely to take more than two minutes (build, deploy, test, "
        "investigation), create one telegram_task_activity card at the start and update that same card "
        "at meaningful steps: current operation, step/steps_total, and a log line for each finished step. "
        "Close it with done, failed or cancelled at the end. Do not create cards for short answers, and "
        "do not repeat the card's content in chat."
    )


_service: ActivityService | None = None


async def _on_update(update, context) -> None:
    """Taps go to the live service: a registry rebuild (/new) replaces ``_service`` after Telegram is wired."""
    if _service is not None:
        await _service.on_update(update, context)


def register(ctx) -> None:
    global _service
    runtime = _service.runtime if _service is not None else None
    _service = ActivityService(ctx, lambda key, default="": get_session_env(key, default))
    _service.runtime = runtime

    ctx.register_tool(name="telegram_task_activity", toolset="telegram_task_activity", schema=TOOL_SCHEMA,
                      handler=_service.handle_tool, emoji="⚡")
    ctx.register_system_prompt_section("telegram-activity-widget.workflow", _guidance, position="after_memory",
                                       max_chars=600)

    def _wire(application, adapter) -> None:
        del adapter
        from telegram import Update
        from telegram.ext import TypeHandler

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        _service.runtime = GatewayRuntime(application.bot, loop)
        # Stop taps inside the widget arrive as widget_query updates; polling must ask Telegram for them.
        if enable_widget_queries(Update):
            # PTB runs one handler per group, so each widget plugin owns its group (activity: -9).
            application.add_handler(TypeHandler(Update, _on_update), group=-9)

    ctx.register_platform_handler("telegram", _wire)
