"""Hermes registration: clarify questions as Telegram widgets in private chats."""

from __future__ import annotations

import asyncio
import logging

try:
    from .clarify_widget import ClarifyWidgets, enable_widget_queries
except ImportError:  # loaded as a top-level module by some plugin hosts
    from clarify_widget import ClarifyWidgets, enable_widget_queries

logger = logging.getLogger(__name__)
_widgets: ClarifyWidgets | None = None


def _guidance(session_info) -> str:
    if str(session_info.get("platform", "")).lower() != "telegram":
        return ""
    return (
        "When the user must pick between known options, call clarify with those options in `choices` "
        "(best first), never written into the question text: Telegram shows each choice as its own "
        "tappable option. Ask one decision per question. A choice is one tap, so never offer an "
        "irreversible or bulk action the user did not name themselves (delete all emails or files, "
        "send to many, pay): resolve vague requests toward the narrowest safe reading."
    )


def register(ctx) -> None:
    global _widgets
    # Session resets rebuild the registry after Telegram is wired; keep the installed wrapper's state.
    _widgets = _widgets or ClarifyWidgets()
    widgets = _widgets
    ctx.register_hook("on_human_input_resolved", widgets.on_human_input_resolved)
    ctx.register_system_prompt_section(
        "telegram-clarify-widget.choices", _guidance, position="after_memory", max_chars=600,
    )

    def _wire(application, adapter) -> None:
        from telegram import Update
        from telegram.ext import TypeHandler
        from gateway.platforms.base import SendResult

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        # Taps inside the widget arrive as widget_query updates; polling must ask Telegram for them.
        # Without that the card's controls would be dead, so the stock card stays.
        if enable_widget_queries(Update) and widgets.install(adapter, SendResult, loop):
            # PTB runs one handler per group, so each widget plugin's TypeHandler has its own group.
            application.add_handler(TypeHandler(Update, widgets.on_update), group=-8)

    ctx.register_platform_handler("telegram", _wire)
