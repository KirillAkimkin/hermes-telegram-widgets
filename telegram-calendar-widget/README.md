# Telegram calendar widget

`telegram_calendar_widget` shows a Google Calendar event draft in the private Telegram chat as a
Bot API 10.4 HTML widget. The buttons are inside the card: **Create** (or **Create and invite** when
there are guests; green, the main action), **Edit**, **Cancel**. English by default, Russian when Hermes
runs in Russian (`display.language` / `HERMES_LANGUAGE` = `ru`); all strings, dates and units are in
`TEXT` in `calendar_widget.py`. No caption, no inline keyboard.

- Taps are widget requests: the card is sent with `allow_queries`, each button calls
  `TelegramWidget.request("create" | "edit" | "cancel")`, and Telegram delivers a `widget_query`
  update. `enable_widget_queries` adds `widget_query` to PTB's `Update.ALL_TYPES` (Hermes's
  `allowed_updates`); a `TypeHandler` in group -7 picks the queries for this plugin's cards (matched by
  chat and message), checks the tapping user is the card's owner, answers with `answerWidgetQuery`
  first, then does the work and pushes the new status with `editMessageWidgetData`. The handler looks up
  the live service on every tap, so a registry rebuild (`/new`) does not leave it on an old one.
- The tool creates nothing. Only the Create tap runs `gog calendar create <calendar> --account=…
  --summary=… --from=… --to=… --timezone=… --send-updates=all|none --json --no-input` (+ location,
  description, attendees, `--with-meet`, `--all-day`). Guests get invitations only on that tap.
- Times must carry the zone's real offset on that date (`offset_mismatch` otherwise: the CET/CEST
  trap). All-day `end` is the last day, inclusive; the plugin passes Google the exclusive next day.
- After creation the event is read back (`gog calendar event … --readonly`) and compared with the
  draft: title, time/dates, timezone, place, guests, Meet link, status. A mismatch or a failed
  readback ends as `unverified` with the differing fields, not as "Created".
- One-time transition (committed before any side effect) and the Edit injection into the session
  work as in `telegram-email-widget` (title, start and timezone JSON-quoted and labelled untrusted data).
  A pending draft older than 24 hours never creates: the tap answers "This draft is out of date, ask again"
  and the card closes as Expired; stored cards older than 7 days are pruned. Statuses in `chat_data`:
  pending, creating, created, unverified, failed, unknown, cancelled, editing, expired. After
  created/unverified, `chat_data` also carries the event link (`u`) and the Meet link (`g`); the card then
  shows **Open in Calendar** and **Google Meet**, which open via `TelegramWidget.openLink` without a bot
  round trip. A card still `creating` when the gateway starts (it restarted mid-create) is closed as
  `unknown` ("Check the calendar before you retry") and the card is told.

Limits: private chats only; description ≤ 8 lines of ~40 characters; ≤ 15 guests; the card must fit
600 px (`estimated_height`, calibrated in Chromium; `draft_too_tall` otherwise) so the buttons stay visible.

Tests (the card-script tests run the widget JS in Node and are skipped without `node`):
`PYTHONPATH=../stubs uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests`
