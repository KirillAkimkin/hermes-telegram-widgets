# Telegram task activity widget

`telegram_task_activity` keeps one live card per long task in the private Telegram chat
(Bot API 10.4 `sendWidget`): title, status pill, progress segments, the current operation,
"Updated at 14:32", and a Work log that opens on tap. Inside the card, while the task is running,
waiting or blocked: a neutral **Stop** button. No buttons under the message.

- `create` sends the card and returns `task_id`; `update` / `close` change the same message through
  `editMessageWidgetData` — no new messages. The title is fixed (widget HTML cannot change).
- Live state is `chat_data` (≤ 1024 bytes): `{"s","n","N","c","u","t","l":[[unix, text], …]}`. The log
  keeps the last 8 rows and drops older rows until the state fits the limit.
- Time rule: a running task says "Updated at 14:32"; a finished one carries its time in the pill
  ("Done at 17:40"); log rows have a time column. Never a bare time after a status.
- Statuses: running, waiting, blocked, stopping (after the Stop tap), done, failed, cancelled.
- The widget draws Stop from `chat_data` (`s` running/waiting/blocked) and hides it from stopping on.
  A tap is a widget request (`sendWidget` with `allow_queries`, data `stop`, answered with
  `answerWidgetQuery`; the answer shows under the card). The plugin adds `widget_query` to PTB's
  `Update.ALL_TYPES` so polling receives these taps.
- Stop is accepted only from the card owner on that card's message, once: the switch to stopping is
  saved first, then a stop request is injected into the owning session
  (`allow_gateway_injection: true`) and the agent closes the card as cancelled. The request carries the
  task id as a plain identifier and the title JSON-quoted, labelled untrusted data, not instructions.
  If nothing could be injected (no session), the card keeps its status and its Stop button and the tap
  answers "Write «stop» in the chat".
- Cards untouched (no create or update) for 7 days are pruned from the plugin state. The tap handler looks
  up the live service on every tap, so a registry rebuild (`/new`) does not leave it on an old one.
- English by default, Russian when Hermes runs in Russian. No caption.

Replaces the text-based `telegram-task-cards` plugin for the same purpose.

Tests (from this directory; the stub provides `gateway.session_context`; the card-script tests run the
widget JS in Node and are skipped without `node`):

```bash
PYTHONPATH=../stubs uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests
```

On the host, `PYTHONPATH=/home/hermes/.hermes/hermes-agent` replaces the stub.
