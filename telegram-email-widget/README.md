# Telegram email widget

`telegram_email_widget` shows an email draft (new email or a reply) in the private Telegram chat as a
Bot API 10.4 HTML widget (`sendWidget`). The actions are inside the card: **Send** (green, the same
colour as the "Sent" pill), **Edit** and **Cancel** (neutral). There is no inline keyboard under the message.

- Language: English by default, Russian when Hermes runs in Russian (`HERMES_LANGUAGE` or
  `display.language` = `ru`, read through `agent.i18n.get_language()`). The card keeps the language it
  was created in; all strings are in `TEXT` in `widget.py`.
- No caption: the card is the whole message, a caption would repeat the subject.
- The tool sends nothing. Gmail is called only by the Send tap: `gog gmail send --account=… --to=…
  --subject=… --body-file=- --json --no-input` (+ `--cc=`, `--reply-to-message-id=`). Values use the
  `--flag=value` form; the body goes through stdin.
- Taps are widget requests: the card calls `TelegramWidget.request("send" | "edit" | "cancel")`, Telegram
  delivers a `widget_query` update, the plugin answers it with `answerWidgetQuery` (data `{"m": text, "d": 1}`:
  `m` is shown under the card, `d` keeps the buttons locked). python-telegram-bot 22.8 does not list
  `widget_query` in `Update.ALL_TYPES`; `enable_widget_queries` adds it (Hermes passes that list as
  `allowed_updates` when polling starts) and a `TypeHandler` in group -6 reads the update from
  `update.api_kwargs` (each widget plugin has its own group: PTB runs one handler per group). The handler
  looks up the live service on every tap, so a registry rebuild (`/new`) does not leave it on an old one.
- A tap is accepted only on this card's message in this chat, from the card owner (`from.id` is
  authenticated by Telegram), with data exactly `send`/`edit`/`cancel`, and only while the card is
  `pending`. Taps on other widgets are left alone. The transition is committed before anything else runs,
  so a second tap or a gateway restart never sends twice. The request is answered before Gmail starts.
- A pending draft older than 24 hours never sends: any tap answers "This draft is out of date, ask again"
  and the card closes as Expired (nothing was sent). Stored cards older than 7 days are pruned.
- Status goes into the widget through `editMessageWidgetData` (`chat_data` = `{"s","d","t","u"}`); the
  widget script draws it (Draft → Sending → Sent/Failed/Unclear, Cancelled, Editing). The Send/Edit/Cancel
  row shows only while the draft is pending; after a send it becomes one in-card **Open in Gmail** button
  (`TelegramWidget.openLink` with the link from `chat_data.u`, no bot round trip). Finished actions show
  their time in the pill ("Sent at 15:56").
- `failed` means gog returned non-zero, so nothing was sent. `unknown` (timeout/crash) means check Sent
  before any retry. Neither is retried automatically. A card still `sending` when the gateway starts (it
  restarted mid-send) is closed as `unknown` and the card is told.
- Edit closes the card and injects a prompt into the owning session (needs
  `plugins.entries.telegram-email-widget.allow_gateway_injection: true`); the draft id is a plain identifier,
  recipient and subject are JSON-quoted and labelled untrusted data, not instructions. The agent then shows
  a new card.
- The tool result tells the model to reply with exactly `NO_REPLY` when it has nothing to add, so Hermes
  sends no chat message after the card.
- **Mail guard** (`mail_guard.py`, owner decision): a `pre_tool_call` hook vetoes `terminal`, `execute_code`
  and `process_manage` calls that run `gog` with a Gmail subcommand trash, archive, batch, modify, delete,
  filters or untrash: the verb must be among the first command words after `gmail` (flags skipped, shell
  words or Python list items), so an account address like `x@gmail.com`, a read-only search such as
  `gog gmail search "in:trash"`, and other gog services (`gog calendar/tasks/drive delete`) pass. The only
  destructive pass: `gog gmail trash` of at most 5 explicit message ids, no search or query, right after the
  user's own message asked to delete. A plugin-injected prompt (`[Inbox widget] …`) clears that permission:
  such turns carry untrusted email text. It reads the command text, so code that calls the Gmail API
  directly is not caught.

Limits: private chats only (Telegram production rule). The draft must fit the 600 px bubble together
with the button row: the body gets at most 18 lines of ~40 characters, less one line for every extra line
the subject (~36 characters a line) or a From/To/Cc value (~34 characters a line) wraps to. Measured in
Chromium at a 380 px bubble (15px/1.35 system-ui): an 18-line body with Cc is 574 px; five recipients in
To leave 14 lines (573 px; 18 lines would be 652 px); a 200-character subject leaves 13 (537 px). Longer
bodies are refused with `body_too_long` and the `max_body_lines` left for that draft. Drafts sent before
this version have no in-card buttons and their old inline buttons no longer respond.

Tests (from this directory; the stub provides `gateway.session_context`; the card-script tests run the
widget JS in Node and are skipped without `node`):

```bash
PYTHONPATH=../stubs uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests
```
