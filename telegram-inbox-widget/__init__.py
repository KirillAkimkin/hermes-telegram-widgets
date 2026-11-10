"""Hermes registration for the Telegram inbox widget."""

from __future__ import annotations

import asyncio

from gateway.session_context import get_session_env

try:
    from .inbox_widget import MAX_ITEMS, GatewayRuntime, InboxService, enable_widget_queries
except ImportError:  # loaded as a top-level module by some plugin hosts
    from inbox_widget import MAX_ITEMS, GatewayRuntime, InboxService, enable_widget_queries

TOOL_SCHEMA = {
    "name": "telegram_inbox_widget",
    "description": (
        "Show emails in this private Telegram chat as a widget the user can act on, or read them silently. "
        f"mode=list: up to {MAX_ITEMS} emails as cards; tapping one opens it, its Reply button asks you for a reply; "
        "mode=read: one email in full (from, to, date, subject, text) with Reply and Open in Gmail; mode=lookup: "
        "nothing is shown, the matching emails come back as data only (use it when you need context, e.g. to draft "
        "a reply or a follow-up). The plugin reads Gmail itself (read-only, via gog)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "accounts": {"type": "string", "description": "Gmail accounts to read, comma-separated (gog account emails)."},
            "query": {"type": "string", "description": "Gmail search query; default in:inbox. For one email use e.g. from:northwind."},
            "mode": {"type": "string", "enum": ["list", "read", "lookup"],
                     "description": "list (default), read (first match in full) or lookup (no card, data only)."},
            "limit": {"type": "integer", "description": f"list/lookup: 1-{MAX_ITEMS} newest matches across the accounts, default 3."},
        },
        "required": ["accounts"],
        "additionalProperties": False,
    },
}


def _guidance(session_info) -> str:
    if str(session_info.get("platform", "")).lower() != "telegram":
        return ""
    return (
        "When the user asks to see or check emails in this Telegram chat, call telegram_inbox_widget "
        "(mode list) instead of listing them in text; to show one email, use mode read with a query that "
        "finds it. When you only need emails as context (to write a reply, a follow-up or a summary the user "
        "did not ask to see), use mode lookup: it shows nothing. Do not retell what a widget shows."
    )


_service: InboxService | None = None


async def _on_update(update, context) -> None:
    """Taps go to the live service: a registry rebuild (/new) replaces ``_service`` after Telegram is wired."""
    if _service is not None:
        await _service.on_update(update, context)


def register(ctx) -> None:
    global _service
    runtime = _service.runtime if _service is not None else None
    _service = InboxService(ctx, lambda key, default="": get_session_env(key, default))
    _service.runtime = runtime

    ctx.register_tool(name="telegram_inbox_widget", toolset="telegram_inbox_widget", schema=TOOL_SCHEMA,
                      handler=_service.handle_tool, emoji="📥")
    ctx.register_system_prompt_section("telegram-inbox-widget.workflow", _guidance, position="after_memory",
                                       max_chars=500)

    ctx.register_hook("post_tool_call", _service.on_post_tool_call)
    ctx.register_hook("post_llm_call", _service.on_post_llm_call)

    def _wire(application, adapter) -> None:
        del adapter
        from telegram import Update
        from telegram.ext import TypeHandler

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        _service.runtime = GatewayRuntime(application.bot, loop)
        # Taps inside the widget arrive as widget_query updates; polling must ask Telegram for them.
        if enable_widget_queries(Update):
            application.add_handler(TypeHandler(Update, _on_update), group=-5)

    ctx.register_platform_handler("telegram", _wire)
