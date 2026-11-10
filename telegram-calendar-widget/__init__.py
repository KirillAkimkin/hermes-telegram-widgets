"""Hermes registration for Telegram calendar widgets."""

from __future__ import annotations

import asyncio

from gateway.session_context import get_session_env

try:
    from .calendar_widget import (LINE_CHARS, MAX_ATTENDEES, MAX_DESCRIPTION_LINES, CalendarWidgetService,
                                  GatewayRuntime, enable_widget_queries)
except ImportError:  # loaded as a top-level module by some plugin hosts
    from calendar_widget import (LINE_CHARS, MAX_ATTENDEES, MAX_DESCRIPTION_LINES, CalendarWidgetService,
                                 GatewayRuntime, enable_widget_queries)

TOOL_SCHEMA = {
    "name": "telegram_calendar_widget",
    "description": (
        "Show a Google Calendar event draft to the user as a Telegram widget with "
        "Create / Edit / Cancel buttons inside the card. The event (and guest invitations) is created only "
        "when the user taps Create; this tool itself creates nothing. Private Telegram chat only. "
        f"Description: at most {MAX_DESCRIPTION_LINES} lines of ~{LINE_CHARS} characters; "
        f"at most {MAX_ATTENDEES} guests. The card must fit 600 px (error draft_too_tall): "
        "keep the title, place and description short, especially with many guests."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account": {"type": "string", "description": "Google account that owns the calendar (gog account email)."},
            "calendar_id": {"type": "string", "description": "Calendar ID; omit for the account's primary calendar."},
            "title": {"type": "string", "description": "Exact event title."},
            "start": {"type": "string", "description": (
                "Timed event: RFC3339 with the zone's real offset on that date, e.g. 2026-11-12T10:00:00+01:00. "
                "All-day event: YYYY-MM-DD.")},
            "end": {"type": "string", "description": (
                "Timed event: RFC3339 end. All-day event: last day YYYY-MM-DD, inclusive (omit for one day).")},
            "timezone": {"type": "string", "description": "IANA timezone of the event, e.g. Europe/Berlin."},
            "all_day": {"type": "boolean", "description": "True for an all-day event."},
            "location": {"type": "string", "description": "Place; empty for none."},
            "description": {"type": "string", "description": "Event description; empty for none."},
            "attendees": {"type": "string", "description": (
                "Guest emails, comma-separated; empty for none. Every guest gets an invitation email on Create.")},
            "with_meet": {"type": "boolean", "description": "True to add a Google Meet link."},
        },
        "required": ["account", "title", "start", "timezone"],
        "additionalProperties": False,
    },
}


def _guidance(session_info) -> str:
    if str(session_info.get("platform", "")).lower() != "telegram":
        return ""
    return (
        "To create a calendar event in this Telegram chat, call telegram_calendar_widget with the exact "
        "event. The user creates it with its Create button. Do not ask for a text confirmation and do "
        "not run `gog calendar create` yourself for such events. Resolve the IANA timezone and its real "
        "offset on that date; list only the guests the user approved. After Edit, show a corrected "
        "draft with the same tool. The card shows the whole event: do not restate it in chat."
    )


_service: CalendarWidgetService | None = None


async def _on_update(update, context) -> None:
    """Taps go to the live service: a registry rebuild (/new) replaces ``_service`` after Telegram is wired."""
    if _service is not None:
        await _service.on_update(update, context)


def register(ctx) -> None:
    global _service
    runtime = _service.runtime if _service is not None else None
    _service = CalendarWidgetService(ctx, lambda key, default="": get_session_env(key, default))
    _service.runtime = runtime

    ctx.register_tool(
        name="telegram_calendar_widget",
        toolset="telegram_calendar_widget",
        schema=TOOL_SCHEMA,
        handler=_service.handle_tool,
        emoji="📅",
    )
    ctx.register_system_prompt_section(
        "telegram-calendar-widget.workflow", _guidance, position="after_memory", max_chars=600,
    )

    def _wire(application, adapter) -> None:
        del adapter
        from telegram import Update
        from telegram.ext import TypeHandler

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        _service.runtime = GatewayRuntime(application.bot, loop)
        _service.recover_interrupted()  # a restart mid-create leaves "Creating…" cards: their outcome is unknown
        # Taps inside the widget arrive as widget_query updates; polling must ask Telegram for them.
        if enable_widget_queries(Update):
            # PTB runs only one handler per group, so every widget plugin has its own group (calendar: -7).
            application.add_handler(TypeHandler(Update, _on_update), group=-7)

    ctx.register_platform_handler("telegram", _wire)
