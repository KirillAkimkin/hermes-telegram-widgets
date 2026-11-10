# Telegram clarify widget

Hermes asks questions through its `clarify` tool. The stock Telegram card is text with numeric
buttons ("1", "2", "Other") and a typed-"skip" hint. In a private chat this plugin renders the same
question as a Bot API 10.4 HTML widget: the question, every option in full with a "recommended" mark
on the recommended one, and "Other" and "Skip" buttons, all inside the card. The options themselves
are tappable. No caption, no inline keyboard. English by default, Russian when Hermes runs in Russian
(`display.language` / `HERMES_LANGUAGE` = `ru`); all strings are in `TEXT` in `clarify_widget.py`.

- It wraps `TelegramAdapter.send_clarify` on the live adapter instance and sends the widget with
  `allow_queries`. Group chats, widgets over the size budget (4096 bytes after zlib, an estimated
  600 px height) and send errors fall back to the stock card.
- A tap sends a widget request (`widget_query`): `o:k` for option k, `other`, `skip`. The plugin adds
  `widget_query` to python-telegram-bot's `Update.ALL_TYPES` (Hermes passes it as `allowed_updates`)
  and handles the updates with a `TypeHandler` in group -8 (each widget plugin has its own group).
  Only taps on this plugin's cards, from the chat's own user, are handled; each is answered with
  `answerWidgetQuery`, and the card then redraws itself from `chat_data` (`editMessageWidgetData`).
- Taps resolve through Hermes's registry (`tools.clarify_gateway`): an option resolves with the exact
  offered choice text, Skip with `SKIPPED`, Other flips the entry to text capture, so the next chat
  message answers it, as with the stock "Other".
- An open question (no choices) shows Skip only; the next chat message answers it. `ForceReply` is not
  used: Telegram Desktop keeps its "Reply to …" bar on screen until the user dismisses it. Widgets have
  no text input of their own. A prompt section asks the model to put options into `choices`, not into
  the question text.
- Typed answers, timeouts and /stop close the card through the `on_human_input_resolved` hook. A closed
  card shows the chosen option with ✓, turns the options off and hides Other/Skip; the pill carries the
  action time ("Answered at 17:26"), never a bare time.
- Card state is in memory: clarify entries never outlive the gateway process.

Relies on Hermes internals: `TelegramAdapter.send_clarify` signature, `_bot`, `_metadata_thread_id`,
`tools.clarify_gateway.get_pending/resolve_gateway_clarify/mark_awaiting_text/SKIPPED`, and PTB's
`Update.ALL_TYPES` list. If the adapter shape changes or widget requests cannot be enabled, install is
refused and the stock card stays.

Tests (the card-script tests run the widget JS in Node and are skipped without `node`):
`PYTHONPATH=../stubs uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests`
