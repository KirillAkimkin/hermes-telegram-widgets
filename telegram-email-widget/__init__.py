"""Hermes registration for Telegram email widgets."""

from __future__ import annotations

import asyncio

from gateway.session_context import get_session_env

try:
    from .mail_guard import MailGuard
    from .widget import LINE_CHARS, MAX_BODY_LINES, EmailWidgetService, GatewayRuntime, enable_widget_queries
except ImportError:  # loaded as a top-level module by some plugin hosts
    from mail_guard import MailGuard
    from widget import LINE_CHARS, MAX_BODY_LINES, EmailWidgetService, GatewayRuntime, enable_widget_queries

TOOL_SCHEMA = {
    "name": "telegram_email_widget",
    "description": (
        "Show an email draft (new email or a reply) to the user as a Telegram widget with in-card "
        "Send / Edit / Cancel buttons. Gmail sends it only when the user taps Send; "
        "this tool itself sends nothing. Private Telegram chat only. "
        f"Body: at most {MAX_BODY_LINES} lines of ~{LINE_CHARS} characters, fewer when a long subject or "
        "recipient list wraps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account": {"type": "string", "description": "Sender Gmail account (gog account email)."},
            "to": {"type": "string", "description": "Recipient addresses, comma-separated."},
            "cc": {"type": "string", "description": "CC addresses, comma-separated; empty for none."},
            "subject": {"type": "string", "description": "Exact subject line."},
            "body": {"type": "string", "description": "Exact plain-text body that will be sent."},
            "reply_to_message_id": {
                "type": "string",
                "description": "For a reply: the Gmail message ID being answered. Omit for a new email.",
            },
        },
        "required": ["account", "to", "subject", "body"],
        "additionalProperties": False,
    },
}


def _guidance(session_info) -> str:
    if str(session_info.get("platform", "")).lower() != "telegram":
        return ""
    return (
        "To send or reply to an email in this Telegram chat, call telegram_email_widget with the exact "
        "draft. The user sees it as a widget and sends it with its Send button. Do not ask for a "
        "text confirmation and do not run `gog gmail send` yourself for such drafts. For a reply, pass "
        "the source Gmail message ID as reply_to_message_id. After Edit, show a corrected draft "
        "with the same tool. The card shows the whole draft: do not restate it in chat."
    )


_service: EmailWidgetService | None = None


async def _on_update(update, context) -> None:
    """Taps go to the live service: a registry rebuild (/new) replaces ``_service`` after Telegram is wired."""
    if _service is not None:
        await _service.on_update(update, context)


def register(ctx) -> None:
    global _service
    # Session resets rebuild the plugin registry after Telegram is already wired; keep the bot binding.
    runtime = _service.runtime if _service is not None else None
    _service = EmailWidgetService(ctx, lambda key, default="": get_session_env(key, default))
    _service.runtime = runtime

    ctx.register_tool(
        name="telegram_email_widget",
        toolset="telegram_email_widget",
        schema=TOOL_SCHEMA,
        handler=_service.handle_tool,
        emoji="✉️",
    )
    ctx.register_system_prompt_section(
        "telegram-email-widget.workflow", _guidance, position="after_memory", max_chars=600,
    )
    # Hard stop for the agent's own destructive Gmail commands (trash/archive/batch/filters via gog).
    guard = MailGuard()
    ctx.register_hook("pre_llm_call", guard.on_pre_llm_call)
    ctx.register_hook("pre_tool_call", guard.on_pre_tool_call)

    def _wire(application, adapter) -> None:
        del adapter
        from telegram import Update
        from telegram.ext import TypeHandler

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        _service.runtime = GatewayRuntime(application.bot, loop)
        _service.recover_interrupted()  # a restart mid-send leaves "Sending…" cards: their outcome is unknown
        # Taps inside the widget arrive as widget_query updates; polling must ask Telegram for them.
        # Own group -6: PTB runs one handler per group and TypeHandler(Update) matches every update.
        if enable_widget_queries(Update):
            application.add_handler(TypeHandler(Update, _on_update), group=-6)

    ctx.register_platform_handler("telegram", _wire)
