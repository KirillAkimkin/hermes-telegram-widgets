# Telegram agent computer

An "agent computer" in the Hermes Telegram bot. While Hermes uses its screen, a **Computer** card
shows up in the private chat. One tap on **Open screen** opens a Telegram Mini App with the bot's
desktop live (noVNC), where the user can **Take over** and **Hand back**.

## How it works

**Agent half** (`plugin.yaml`, `__init__.py`, `computer.py`, `viewer.py`)

- **Hooks.** `pre_tool_call` on `computer_use` and `browser_exec` / `browser_cdp` / `browser_dialog` /
  `browser_vault`, in a Telegram private chat:
  - The first call of a session sends the card (`sendWidget`).
  - Later calls change the same message (`editMessageWidgetData`).
  - Hooks only update stored state and schedule the Telegram call on the gateway loop (fire and
    forget, coalesced per card), so they never block or fail a tool call.
  - The one exception is the takeover fence: while a human holds the Bot Desktop lease,
    `pre_tool_call` refuses `terminal`, `process_manage`, `execute_code`, `delegate_task`,
    `computer_use` and the `browser_exec` / `browser_cdp` / `browser_dialog` / `browser_vault` tools in
    every session (`{"action": "block"}`; the model is told the user is in control, not to retry, and
    to wait for the hand-back). Every other tool, and every call while the lease cannot be read, passes.
  - `post_tool_call` adds the page title when a browser result reports `url` + `title`.
  - `post_llm_call` (the turn ended) sets the card to Idle.
  - An idle card stays reusable for 10 minutes. After that, the next screen call sends a new card.
  - A 10-minute timer is the fallback when no turn-end hook arrives.
- **The card** (Bot API 10.4 HTML widget, no caption; 185 px tall, 245 px with a takeover request, at
  380 px wide; about 1.3 KB zlib of the 4096 B limit). It shows:
  - title 🖥 Computer / 🖥 Компьютер;
  - pill: Hermes is working / You are in control / Idle;
  - the current page host and title;
  - the last action. For `browser_exec` this is the step label from the code's first-line comment,
    or "Opened github.com". Typed text, code and secrets never reach the card;
  - one primary in-card button **Open screen** / **Открыть экран**. The tap itself calls
    `TelegramWidget.openTelegramLink("https://t.me/<bot>/hermes_desktop?startapp=c_<card_id>")`, because one tap
    unlocks one privileged call. No bot round trip, no widget requests.
- **Live state** is `chat_data` (≤ 1024 bytes): `{"m": "w|h|i", "h": host, "t": title, "a": action,
  "q": request, "u": unix}`. Long fields shrink until it fits. The bot username comes from the live
  bot (`bot.username`, else `get_me`).
- **Lease.** `tools.bot_desktop.lease.on_change` moves the pill to "You are in control" while a
  human holds the lease, and back afterwards.
- **Tool `telegram_agent_computer(reason)`.** The model calls it when only a human can go on
  (sign-in, 2FA, CAPTCHA, payment):
  - The card shows "Hermes needs you: <reason>".
  - The result tells the model to end the turn with NO_REPLY or one short line.
  - When the human hands control back (in-process lease transition human → agent), the request
    clears and the owning session gets an `[Agent computer]` message to continue. This needs
    `allow_gateway_injection`; without it the user writes in the chat.
  - The tool description and a short Telegram system-prompt section tell the model to browse with
    `browser_*` (the headed browser on this screen), not camofox (hidden browser).

**Dashboard half** (`dashboard/`)

- **`manifest.json`**: name `telegram-agent-computer`, hidden tab, `api: api.py`. `dist/index.js`
  only registers a placeholder, because the dashboard SPA loads every plugin's entry.
- **`api.py`**: one WebSocket route, `/api/plugins/telegram-agent-computer/ws`.
  - It is imported as its own module, so it reaches the agent half through an in-process registry,
    `sys.modules["hermes_telegram_agent_computer_shared"].hub`, filled by `register()`.
  - Both halves run in the gateway process.
- **Protocol** (`viewer.py`, `ViewerHub`):
  1. The first message must be `{"init": Telegram.WebApp.initData}`. A missing, invalid, stale or
     foreign initData closes 4401. Before that, `api.py` closes 4403 unless the `Origin` header is
     exactly the `webapp.public_url` origin.
  2. The server starts the screen if needed (`runtime.start` in a thread) and mints a display
     ticket: `mint_ticket(user_id="display:<vid>", provider="bot-desktop", extra={"hermes_home",
     "viewer_id"})`.
  3. It answers `{"type": "ready", "ticket", "path": "/api/display/ws", "lease", "card"}`. `card`
     carries the takeover request, and only for the owner's own `startapp` card.
  4. Messages after that:
     - `{"op": "take"}` → `touch_activity()` + `lease.acquire(vid, reason=<request>)`
     - `{"op": "back"}` → `lease.release(vid)`. From a connection that does not hold the lease this
       changes nothing and the reply shows the real holder ("Another window is in control").
     - `{"op": "ticket"}` → a fresh ticket for a reconnect (≤ 30 a minute)
  5. Lease changes are pushed as `{"type": "lease", "lease": {"holder", "mine", "reason", "epoch"}}`:
     in-process ones at once, other processes' ones within 3 s by polling.
  6. Anything else gets `{"type": "error", "code": "unsupported"}`. A message over 8 KB closes 1009.
  7. After 30 minutes the socket closes 4401 "session expired".
- **Page** (`index.html`, `app.js`):
  - Loads `telegram-web-app.js`, calls `ready()` and `expand()`, disables vertical swipes and uses the
    Telegram theme colours.
  - Runs noVNC `RFB` on its own `WebSocket` to `/api/display/ws?display_ticket=…`, so the page sees
    close codes. Settings: `scaleViewport`, and `viewOnly` unless this viewer holds the lease.
  - Take over (primary) and Hand back (neutral) buttons, a status chip and the takeover request.
  - A text field with **Type**, which sends each character as a keysym via `rfb.sendKey`, plus
    Enter / ⌫ / Tab keys. Mobile keyboards cannot drive the canvas. The field is masked
    (`type=password`, no autofill) with a Show / Hide toggle, and is cleared after Type, on Hand back,
    on a disconnect and when this window loses control.
  - Reconnects with a fresh ticket on 4000 (control taken), 1006, 4401 (spent ticket) and 4001
    (screen off).
  - A 4401 on the control socket after the page was let in (session expired, or initData older than
    5 minutes on a reconnect) ends the page: it closes the screen and asks the user to reopen it from
    the Computer card. No reconnect loop.
  - English by default, Russian when `initDataUnsafe.user.language_code` starts with `ru`.
- **noVNC 1.7.0** is vendored from `npm pack @novnc/novnc@1.7.0`: `dashboard/novnc/core/` and
  `dashboard/novnc/vendor/pako/`, with `LICENSE.txt` (MPL-2.0), `AUTHORS`, `docs/LICENSE.*` and pako's
  `LICENSE` (MIT).

## Set up

1. Copy the directory to the host as `~/.hermes/plugins/telegram-agent-computer`.
2. Enable the plugin: `hermes plugins enable telegram-agent-computer`.
   - This adds the name to `plugins.enabled`. The dashboard half needs that too: the manifest
     `name` is the same.
   - To resume the session after a hand-back, set
     `plugins.entries.telegram-agent-computer.allow_gateway_injection: true`.
3. Restart the gateway. The plugin WebSocket route is mounted at startup. The hot mount for a plugin
   enabled later only runs on an HTTP request, and in gated mode that request is answered 401 without
   a dashboard cookie.
4. BotFather: **/newapp** for the bot, short name `hermes_desktop`, URL:

   ```
   https://hermes.example.ts.net:9443/dashboard-plugins/telegram-agent-computer/index.html
   ```

   A direct-link Mini App, not the Main Mini App (that one adds an "Open" button to the chat list; keep it
   empty with `/empty`). The card opens it as `https://t.me/<bot_username>/hermes_desktop?startapp=c_<card_id>`.
5. Host requirements:
   - Bot Desktop packages installed and `bot_desktop.auto_start: true`;
   - `browser.headed: true`;
   - `webapp.enabled: true` with `public_url` set to the address above;
   - `TELEGRAM_ALLOWED_USERS` in `~/.hermes/.env`;
   - `platforms.telegram.home_channel.user_id` (the only user who may open the screen).

## Security model

- **Network.** The page and both sockets are served only by the existing tailnet-only listener
  (Tailscale Serve `:9443` → gateway `127.0.0.1:9120`). No new port, no public URL. The phone needs
  Tailscale on.
- **Page.** Static files under `/dashboard-plugins/<name>/` are public by design; they contain no
  secrets. Python sources are never served (suffix allowlist).
- **Origin.** The server's WebSocket gate compares only the Origin's hostname, so pages on other ports
  of the same ts.net host (`:443`, `:8443`) would pass it. `api.py` therefore closes 4403 before
  `accept()` unless `Origin` equals the `webapp.public_url` origin exactly (scheme, host and port).
  A missing Origin, or a `public_url` that cannot be read, is refused.
- **Control socket.** It then authenticates itself:
  - `initData` HMAC with the live bot's token (`HMAC_SHA256("WebAppData", bot_token)`);
  - `auth_date` at most 5 minutes old (the page reconnects with the same initData, so a reconnect
    after that asks the user to reopen the screen);
  - `user.id` equal to `platforms.telegram.home_channel.user_id` and in `TELEGRAM_ALLOWED_USERS`. Other
    allowed chat users do not get the desktop. A missing owner or an empty list refuses everyone, and
    until the Telegram bot is wired there is no token, so the socket fails closed.
  - A socket lives at most 30 minutes, then closes 4401 "session expired".
  - The token is never logged or sent.
- **Viewer ids.** Every control connection gets its own viewer id, created and kept server-side, so
  closing one Mini App window never releases a lease another window holds. Viewer ids are lease
  capabilities: they never leave the server. Messages carry no ids, and messages with extra fields
  are refused.
- **Lease** (the real gate, in the display bridge): input reaches Xvnc only while
  `lease.holder == human` and `viewer_id` matches.
  - Watching never grants input. noVNC `viewOnly` is only UX.
  - While a human holds the lease, `computer_use` and the browser tools refuse to act (Hermes core),
    and this plugin's `pre_tool_call` also refuses the shell and code tools (see Hooks).
  - A clean close by the holder releases the lease. A dropped link keeps it. A reopened or reconnected
    Mini App is a new connection with a new viewer id, so it shows "Another window is in control":
    Take over there, then Hand back.
  - **Remaining gap.** The fence is tool-level, not an OS boundary (`terminal.backend: local` runs the
    agent's shell as uid `hermes`, the screen's uid). A command, script or subagent already running
    when the user takes over keeps running to its end, and anything else running as uid `hermes` can
    still reach `DISPLAY`/`XAUTHORITY`, `rfb.sock` and Chromium's DevTools port. Only a sandboxed
    terminal backend (or a separate uid for the screen) closes this.
  - The 30-minute limit ends the control socket; the page then closes its screen socket itself, which
    the display bridge treats as a dropped link (a held lease stays held until a new window takes over
    and hands back). A client that ignores the close keeps an already open display connection until
    Hermes evicts it (another Take over) or the screen stops.
- **Display tickets** are single use and live 30 s, without `admitted_with`. They are never stored.
- **Thumbnails are not used**, so no screenshot of a human-held screen leaves the host.

## Tests

```
PYTHONPATH=../stubs uv run --python 3.11 --with pytest --with fastapi --with httpx python -m pytest -q tests
```

The card-script test runs the widget JS in Node and is skipped without `node`.
