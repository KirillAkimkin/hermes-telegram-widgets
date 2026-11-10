# Telegram inbox widget

`telegram_inbox_widget` shows emails in the private Telegram chat as Bot API 10.4 HTML widgets, or reads
them silently for context. All actions are inside the card — no button rows under it.

- `mode=list`: up to 5 newest matches across the given accounts as stacked cards (number, unread dot,
  sender, mailbox domain and the email's own time, subject, first text line, a Reply pill). Tapping an
  email opens it as a reading card; opened emails dim in the list.
- `mode=read`: one email in full — From, To, Date ("Today, 17:00"), subject, text trimmed to the
  600 px bubble ("The rest is in Gmail") — with in-card **Reply** and **Open in Gmail**. The text gets 17
  lines of ~40 characters; every extra line the subject (~36 characters a line) or the From/To values wrap
  to costs one text line, so the button row always stays inside the bubble (measured in Chromium at a
  380 px bubble: 17 lines put the card at 580 px, a 105-character subject 581 px, with a long sender too
  561 px).
- `mode=lookup`: nothing is shown; the matching emails (id, mailbox, sender, date, subject, the first 2000
  characters of text) come back to the model only, e.g. as context for a reply or a follow-up.
- While Gmail is searched, a small "Checking mail…" card with pulsing rows stands in; it is deleted once
  the list arrives (or nothing matched). Widget HTML cannot be edited, so the list is its own message.
- Taps are widget requests: the widget calls `TelegramWidget.request("r:k" | "p:k")`, Telegram delivers
  a `widget_query` update, the plugin answers with `answerWidgetQuery` (≤ 15 s, first) and then acts.
  The answer text or the error code shows under the card. Open in Gmail is `TelegramWidget.openLink`,
  no bot round trip.
- Reply injects a request into the owning session (`allow_gateway_injection: true`) with the message id as a
  plain identifier; mailbox, sender address and subject are JSON-quoted and labelled untrusted data, not
  instructions, and so is the email text. The card says "Hermes is drafting a reply, it will appear below"
  and its Reply turns into a pulsing **Drafting…**; a second tap within a minute asks nothing new. The
  draft then arrives as a `telegram_email_widget` card with Send.
- Reply outcome through agent hooks: `post_tool_call` sees a successful `telegram_email_widget` call with
  that email's `reply_to_message_id` and the button reads **Draft ready**; `post_llm_call` turns it back
  into **Reply** when a later turn ends without a draft (the user said never mind). The hooks save under
  the plugin's lock and only schedule the Telegram push on the gateway loop, so the agent never waits.
- Card state goes into the list and every reading card of that list as `chat_data`
  `{"o": [opened…], "a": [asked…], "d": [drafted…]}` (`editMessageWidgetData`). Taps on the same chat run
  concurrently (PTB 22.8 gives `widget_query` no effective chat, so Hermes does not queue them): every
  change is applied to the stored card under the lock, never to a copy taken before an `await`.
- **Gateway patch:** python-telegram-bot 22.8 predates Bot API 10.4, and Hermes polls with
  `allowed_updates=Update.ALL_TYPES`, which lacks `widget_query`. The plugin appends `widget_query` to
  that list in place (`enable_widget_queries`) and handles it with a `TypeHandler` in group -5; PTB keeps
  the field in `update.api_kwargs`. Taps on widgets of other plugins are ignored here. The handler looks
  up the live service on every tap, so a registry rebuild (`/new`) does not leave it on an old one.
- The plugin reads Gmail itself: `gog gmail messages search <query> --account=… --max=N --include-body
  --json --readonly`. It never marks, archives or sends. Lines that are only a link are dropped. A body is
  cut to 200 000 characters before parsing, and the HTML-to-text pass is one linear scan, so a crafted
  multi-megabyte HTML email cannot stall the gateway.
- The sender address is kept only if it is a plain `local@domain.tld` address of at most 254 characters;
  otherwise it is blank, and Reply answers "Tell me what to answer in the chat" without injecting anything.
  The display name is only shown on the card (escaped), never put into a prompt.
- English by default, Russian when Hermes runs in Russian. No caption. The last 50 cards are kept.

Tests (from this directory; the stub provides `gateway.session_context`; the card-script tests run the
widget JS in Node and are skipped without `node`):

```bash
PYTHONPATH=../stubs uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests
```
