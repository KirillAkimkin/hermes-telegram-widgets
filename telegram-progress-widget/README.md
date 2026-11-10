# Telegram progress widget

A "Hermes is working" card for long replies in a private Telegram chat (Bot API 10.4 `sendWidget`).
No tool: the plugin watches the turn through hooks.

- **When:** a turn (`pre_llm_call`) that is still running after 8 s, or whose 2nd tool call starts first,
  gets ONE card. Short turns, groups/channels, non-Telegram sessions, plugin-injected turns (the user
  message starts with `[`) and helper turns (`delegate_task`, `parent_session_id` set) get none.
- **Card:** "⏳ Working on it" / «⏳ Работаю», an elapsed timer (`0:42`) that ticks inside the widget
  from the start time in `chat_data` (no Telegram edits for the clock), up to 5 latest steps (done ✓, the
  current one pulsing with animated dots, older ones as "+N earlier"; with no tool running the last row
  is "Thinking…") and an in-card neutral **Stop**. No caption, no buttons under the message.
- **Steps:** `pre_tool_call` / `post_tool_call` map the tool NAME to a fixed label code. A done step reads
  in the past tense, the current (pulsing) one in the present: web_search → Searched / Searching the web,
  telegram_inbox_widget → Checked / Checking mail, telegram_email_widget → Drafted / Drafting an email,
  telegram_calendar_widget → Drafted / Drafting an event, browser_* → Used / Using the browser,
  computer_use → Used / Using the screen, terminal → Ran / Running a command, execute_code → Ran / Running
  code, read_file/search_files → Read / Reading files, delegate_task → Asked / Asking a helper,
  skill_view → Looked / Looking up a skill, memory/session_search → Checked / Checking memory, anything
  else → Worked on it / Working (ru: «Выполнил команду» / «Выполняю команду», …, «Поработал» / «Работаю»).
  The widget picks the form from the row's running flag. Arguments and results never leave the hook.
  Back-to-back repeats merge ("Searched the web ×3"). `chat_data` is `{"t": start, "s": [[code, count, running], …],
  "e": earlier, "x": stop state, "f": stop hint}` — label codes and counters only, far below 1024 bytes.
  Step changes are coalesced into one `editMessageWidgetData` per 1.5 s.
- **End:** `post_llm_call`, `on_session_end` (fires for failed and interrupted turns too) or
  `agent_loop_stopped` deletes the card (`deleteMessage`), so the answer is the only message left. A
  turn that ends while the card is still being sent deletes it as soon as Telegram returns it.
  `telegram_task_activity` in the turn means no card (or deletes ours): that card already shows progress.
- **Stop:** taps arrive as `widget_query` updates (the plugin adds `widget_query` to PTB's
  `Update.ALL_TYPES` and handles it with a `TypeHandler` in group -11). Only the chat owner (`from.id`)
  on that card may stop, once. The plugin answers the request first (`answerWidgetQuery`, "Stopping…"),
  then sends a real `/stop` through the Telegram adapter's own message path — a `MessageEvent`
  (`/stop`, `COMMAND`, the owner's source from `adapter.build_source`, pinned to the turn's session key
  via `metadata.gateway_session_key`) into `adapter.handle_message`, the same call the adapter makes for a
  typed `/stop`. The gateway runs its normal `/stop`: access check, interrupt of the running agent and
  its helpers, queue clear, its "Stopped" reply; `agent_loop_stopped` then deletes the card. Without the
  adapter (or if that call fails) the plugin injects a stop request into the session
  (`allow_gateway_injection: true`) and the card says "Asked Hermes to stop"; if neither works, or the
  turn still runs 20 s later, the card says "Type /stop in the chat".
- **Never blocks:** hooks only read the session context and hand plain values to the gateway loop
  (`call_soon_threadsafe`); timers and Telegram calls run there, fire-and-forget.
- **Restart:** cards on screen are recorded in plugin state; the next wiring deletes any card a previous
  gateway process left behind.
- English by default, Russian when Hermes runs in Russian (`agent.i18n.get_language()`).

Tests: `PYTHONPATH=../stubs uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests`
