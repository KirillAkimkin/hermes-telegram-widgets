"""The "Computer" card: a live Telegram HTML widget (Bot API 10.4 ``sendWidget``) for the bot's screen.

When Hermes starts using its screen (``computer_use`` or the headed ``browser_*`` tools) in a private
Telegram chat, one card per session appears: status pill, the current page, the last action and an
in-card **Open screen** button that opens the Mini App (``openTelegramLink`` to ``t.me/<bot>/hermes_desktop?startapp=``).
The HTML is fixed at send time; everything that changes lives in ``chat_data`` (<= 1024 bytes) and the
widget redraws it on ``chatDataChanged``. Updates go through ``editMessageWidgetData`` in place.

Hooks run on the agent thread and must never block or fail a tool call: they only update the stored
state and schedule the Telegram call on the gateway loop (fire and forget, coalesced per card). The one
veto: while a human holds the Bot Desktop lease, ``pre_tool_call`` refuses the screen tools and the
tools that run code as the screen's uid (``FENCED_TOOLS``); a lease read error never refuses anything.
UI text is English by default and Russian when Hermes runs in Russian.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import secrets
import threading
import time
import zlib
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

STATE_KEY = "agent_computer_cards"
SCREEN_TOOLS = frozenset({"computer_use", "browser_exec", "browser_cdp", "browser_dialog", "browser_vault"})
# Refused while a human holds the Bot Desktop lease: the screen tools, and the tools that run code as the
# screen's uid (they could read the display, the RFB socket or Chromium's DevTools port).
FENCED_TOOLS = SCREEN_TOOLS | {"terminal", "process_manage", "execute_code", "delegate_task"}
FENCE_MESSAGE = (
    "The user is in control of the screen right now (they took over the Bot Desktop and may be entering a "
    "password or code). This tool is refused until they hand control back. Do not retry it and do not try "
    "another way around this: end your turn. When they hand back, an [Agent computer] message or the user's "
    "reply tells you to continue."
)
MODES = ("w", "h", "i")  # Hermes is working / you are in control / idle
IDLE_SECONDS = 600  # no screen tool call for this long: the card goes idle
REUSE_SECONDS = 600  # an idle card this recent is reused in place; older: the next use sends a new card
MAX_CARDS = 20
MAX_HOST = 80
MAX_TITLE = 100
MAX_ACTION = 100
MAX_REASON = 160
MAX_CHAT_DATA = 1024
MAX_HTML_BYTES = 32768
MAX_COMPRESSED_BYTES = 4096
_BOT_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_URL_RE = re.compile(r"""(?:new_tab|goto_url|goto|navigate)\(\s*(?:url\s*=\s*)?["'](https?://[^"'\s]{1,2000})["']""")
_RESULT_URL_RE = re.compile(r"""["']url["']\s*:\s*["'](https?://[^"'\s]{1,2000})["']""")
_RESULT_TITLE_RE = re.compile(r"""["']title["']\s*:\s*["']([^"'\n\\]{1,300})["']""")

TEXT: dict[str, dict[str, Any]] = {
    "en": {
        "kind": "🖥 Computer", "open": "Open screen", "need": "Hermes needs you", "updated": "Updated at {t}",
        "mode": {"w": ["Hermes is working", "run"], "h": ["You are in control", "wait"], "i": ["Idle", "off"],
                 "n": ["Waiting for you", "wait"]},
        "act": {
            "opened": "Opened {host}", "browser": "Working in the browser", "dialog": "Answered a page dialog",
            "vault": {"list": "Checked saved logins", "fill": "Filled a saved login", "save_login": "Saving a login",
                      "enter_code": "Entered a one-time code", "unlock": "Unlocking the password manager"},
            "capture": "Looked at the screen", "click": "Clicked", "type": "Typed text", "key": "Pressed a key",
            "scroll": "Scrolled", "drag": "Dragged", "set_value": "Filled a field", "screen": "Used the screen",
        },
    },
    "ru": {
        "kind": "🖥 Компьютер", "open": "Открыть экран", "need": "Нужна ваша помощь", "updated": "Обновлено в {t}",
        "mode": {"w": ["Hermes работает", "run"], "h": ["Управляете вы", "wait"], "i": ["Не используется", "off"],
                 "n": ["Ждёт вас", "wait"]},
        "act": {
            "opened": "Открыл {host}", "browser": "Работает в браузере", "dialog": "Ответил на диалог страницы",
            "vault": {"list": "Проверил сохранённые входы", "fill": "Подставил сохранённый вход",
                      "save_login": "Сохраняет вход", "enter_code": "Ввёл одноразовый код",
                      "unlock": "Открывает менеджер паролей"},
            "capture": "Посмотрел на экран", "click": "Нажал", "type": "Ввёл текст", "key": "Нажал клавишу",
            "scroll": "Прокрутил", "drag": "Перетащил", "set_value": "Заполнил поле", "screen": "Работает с экраном",
        },
    },
}

_CSS = (
    "body{margin:0}"
    ".c{padding:12px 14px 10px}"
    ".h{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}"
    ".t{font-weight:600;font-size:1.05em}"
    ".p{font-size:.78em;font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap;"
    "background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-accent-text-color)}"
    ".p.wait{color:#fff;background:#c8961e}"
    ".p.off{color:var(--tg-theme-hint-color)}"
    ".q{margin:2px 0 8px;padding:8px 10px;border-radius:10px;border-left:3px solid #c8961e;"
    "background:var(--tg-theme-secondary-bg-color)}"
    ".ql{font-size:.78em;font-weight:600;color:#c8961e}"
    ".qt{font-weight:600;overflow-wrap:anywhere;user-select:text;-webkit-user-select:text}"
    ".s{font-weight:600;overflow-wrap:anywhere}"
    ".g{font-size:.9em;color:var(--tg-theme-hint-color);overflow-wrap:anywhere;margin-top:1px}"
    ".u{font-size:.92em;margin-top:6px;overflow-wrap:anywhere}"
    ".dt{display:inline-block;overflow:hidden;vertical-align:bottom;white-space:nowrap;width:0;"
    "animation:dt 1.2s steps(4,jump-none) infinite}"
    "@keyframes dt{to{width:1.1em}}"
    ".p.run{animation:pl 1.6s ease-in-out infinite}@keyframes pl{50%{opacity:.4}}"  # Hermes is acting
    ".m{font-size:.8em;color:var(--tg-theme-hint-color);margin-top:2px}"
    ".z{display:flex;gap:8px;margin-top:10px}"
    ".bt{flex:1;border:0;border-radius:10px;padding:10px 12px;font:inherit;font-size:.93em;font-weight:600;"
    "background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color);transition:transform .12s}"
    ".bt.pr{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}"
    ".bt:active{transform:scale(.98)}"
    "@media (prefers-reduced-motion:reduce){.bt{transition:none}.dt,.p.run{animation:none}.dt{width:auto}}"
)


class GatewayRuntime:
    """Submit PTB calls from a tool or hook thread to the live gateway's event loop."""

    def __init__(self, bot: Any, loop: asyncio.AbstractEventLoop | None):
        self.bot = bot
        self.loop = loop

    def _usable(self) -> bool:
        return self.loop is not None and not self.loop.is_closed() and self.loop.is_running()

    def submit(self, coroutine, timeout: float = 15):
        if not self._usable():
            coroutine.close()
            raise RuntimeError("telegram gateway loop is unavailable")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            raise RuntimeError("telegram operation timed out") from None

    def fire(self, coroutine) -> bool:
        """Schedule without waiting: hooks must return at once."""
        if not self._usable():
            coroutine.close()
            return False
        asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return True


def current_language() -> str:
    """Hermes's UI language (``HERMES_LANGUAGE`` > ``display.language`` > en), reduced to en/ru."""
    try:
        from agent.i18n import get_language
        return "ru" if str(get_language()).lower().startswith("ru") else "en"
    except Exception:
        return "en"


def tr(card: dict[str, Any], key: str) -> Any:
    return TEXT.get(card.get("lang"), TEXT["en"])[key]


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    host = host[4:] if host.startswith("www.") else host
    return host if re.fullmatch(r"[a-z0-9.-]{1,253}", host) else ""


MINI_APP = "hermes_desktop"  # BotFather /newapp short name: a direct-link app, no "Open" button in the chat list


def startapp_link(bot: str, card_id: str) -> str:
    return f"https://t.me/{bot}/{MINI_APP}?startapp=c_{card_id}"


def describe(tool_name: str, args: Any, lang: str) -> dict[str, str]:
    """What a screen tool call is about to do, as card fields: ``a`` (last action) and ``h`` (page host).
    Never echoes typed text, code or secrets: only the agent's own step label and URL hosts."""
    words = TEXT.get(lang, TEXT["en"])["act"]
    args = args if isinstance(args, dict) else {}
    out: dict[str, str] = {}
    if tool_name == "browser_exec":
        code = args.get("code") if isinstance(args.get("code"), str) else ""
        first = code.lstrip().split("\n", 1)[0].strip()
        label = _one_line(first[1:], MAX_ACTION) if first.startswith("#") else ""
        match = _URL_RE.search(code)
        host = host_of(match.group(1)) if match else ""
        if host:
            out["h"] = host
        out["a"] = label or (words["opened"].format(host=host) if host else words["browser"])
    elif tool_name == "browser_cdp":
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        host = host_of(params.get("url", "")) if args.get("method") == "Page.navigate" and isinstance(params.get("url"), str) else ""
        if host:
            out["h"] = host
        out["a"] = words["opened"].format(host=host) if host else words["browser"]
    elif tool_name == "browser_dialog":
        out["a"] = words["dialog"]
    elif tool_name == "browser_vault":
        out["a"] = words["vault"].get(str(args.get("action", "")), words["browser"])
    else:  # computer_use
        action = str(args.get("action", ""))
        key = "click" if action.endswith("click") else action
        out["a"] = words.get(key) if isinstance(words.get(key), str) else words["screen"]
    return out


def _strings(value: Any, out: list[str]) -> list[str]:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _strings(item, out)
    return out


def page_from_result(result: Any) -> dict[str, str]:
    """``{"h", "t"}`` when a browser result reports the page (``page_info()``, ``Target.getTargets``).
    JSON results are decoded first so escaped quotes and non-ASCII titles match as printed."""
    if isinstance(result, str):
        try:
            result = json.loads(result[:200000])
        except ValueError:
            pass
    try:
        text = "\n".join([json.dumps(result, ensure_ascii=False)] + _strings(result, []))[:40000]
    except (TypeError, ValueError):
        return {}
    url, title = _RESULT_URL_RE.search(text), _RESULT_TITLE_RE.search(text)
    host = host_of(url.group(1)) if url else ""
    if not host or not title:
        return {}
    return {"h": host, "t": _one_line(title.group(1), MAX_TITLE)}


def _script(card: dict[str, Any], link: str) -> str:
    """Draws the pill, the takeover request, the page and the last action from chat_data
    {"m","h","t","a","q","u"}. Open screen calls openTelegramLink in the tap itself (one tap unlocks
    one privileged call), so it never waits for a request."""
    words = {"mode": tr(card, "mode"), "updated": tr(card, "updated")}
    return (
        "(function(){var W=window.TelegramWidget,X=" + json.dumps(words, ensure_ascii=False) + ","
        "L=" + json.dumps(link) + ",$=function(i){return document.getElementById(i)};"
        "function h(s){try{return new Date(s*1e3).toLocaleTimeString(navigator.language,"
        "{hour:\"2-digit\",minute:\"2-digit\",hourCycle:\"h23\"})}catch(e){return \"\"}}"
        "function v(i,t){$(i).textContent=t||\"\";$(i).style.display=t?\"\":\"none\"}"
        "function r(){var d=null;try{d=JSON.parse(W&&W.chatData)}catch(e){}d=d||{m:\"w\"};"
        "var p=X.mode[d.q&&d.m==\"i\"?\"n\":d.m]||X.mode.w;$(\"st\").textContent=p[0];$(\"st\").className=\"p \"+p[1];"
        "v(\"qt\",d.q);$(\"q\").style.display=d.q?\"\":\"none\";v(\"pg\",d.h);v(\"pt\",d.t);v(\"ac\",d.a);"
        "$(\"aw\").style.display=d.a?\"\":\"none\";$(\"dt\").style.display=d.m==\"w\"?\"\":\"none\";"
        "v(\"up\",d.u?X.updated.replace(\"{t}\",h(d.u)):\"\")}"
        "$(\"os\").addEventListener(\"click\",function(){if(W&&W.openTelegramLink)W.openTelegramLink(L)});"
        "if(W)W.onEvent(\"chatDataChanged\",r);r()})();"
    )


def render_html(card: dict[str, Any], bot: str) -> str:
    link = startapp_link(bot, card["id"])
    return (
        f"<style>{_CSS}</style><div class=\"c\"><div class=\"h\"><span class=\"t\">{tr(card, 'kind')}</span>"
        f"<span id=\"st\" class=\"p\">{html.escape(tr(card, 'mode')['w'][0])}</span></div>"
        f"<div id=\"q\" class=\"q\"><div class=\"ql\">{html.escape(tr(card, 'need'))}</div><div id=\"qt\" class=\"qt\"></div></div>"
        f"<div id=\"pg\" class=\"s\"></div><div id=\"pt\" class=\"g\"></div>"
        f"<div id=\"aw\" class=\"u\"><span id=\"ac\"></span><span id=\"dt\" class=\"dt\">...</span></div>"
        f"<div id=\"up\" class=\"m\"></div>"
        f"<div class=\"z\"><button id=\"os\" class=\"bt pr\" type=\"button\">{html.escape(tr(card, 'open'))}</button></div>"
        f"</div><script>{_script(card, link)}</script>"
    )


def widget_fits(markup: str) -> bool:
    raw = markup.encode("utf-8")
    return len(raw) <= MAX_HTML_BYTES and len(zlib.compress(raw, 9)) <= MAX_COMPRESSED_BYTES


def chat_data(state: dict[str, Any]) -> str:
    """The live state within Telegram's 1024-byte chat_data limit: long fields shrink, title first."""
    data = {k: state[k] for k in ("m", "h", "t", "a", "q", "u") if state.get(k) not in (None, "")}
    for field in ("t", "a", "q", "h"):
        while True:
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if len(text.encode("utf-8")) <= MAX_CHAT_DATA:
                return text
            value = data.get(field, "")
            if len(value) <= 12:
                data.pop(field, None)
                break
            data[field] = value[: len(value) * 3 // 4].rstrip() + "…"  # strictly shorter each round
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _public(card: dict[str, Any]) -> dict[str, Any]:
    s = card["state"]
    return {"card_id": card["id"], "status": {"w": "working", "h": "user_in_control", "i": "idle"}[s["m"]],
            "page": s.get("h", ""), "request": s.get("q", "")}


_RESUME_PROMPT = (
    "[Agent computer] The user returned control of the screen after your request \"{reason}\" "
    "(Hand back, or closed the viewer). Check the screen and continue the task."
)


class ComputerService:
    """Cards per Telegram session, kept in plugin state; Telegram calls run on the gateway loop."""

    def __init__(self, ctx: Any, session_env: Callable[..., str], language: Callable[[], str] = current_language,
                 clock: Callable[[], float] = time.time):
        self.ctx = ctx
        self.session_env = session_env
        self.language = language
        self.clock = clock
        self.runtime: GatewayRuntime | None = None
        self.human_holds: Callable[[], bool] = lambda: False
        self._lock = threading.RLock()
        self._queued: set[str] = set()
        self._alock: tuple[Any, asyncio.Lock] | None = None
        self._bot_username = ""
        self._last_holder = ""

    # ---- storage -------------------------------------------------------------------------------
    def _cards(self) -> dict[str, dict[str, Any]]:
        cards = self.ctx.state.get(STATE_KEY, default={})
        return cards if isinstance(cards, dict) else {}

    def _save(self, card: dict[str, Any]) -> None:
        with self._lock:
            cards = self._cards()
            cards[card["id"]] = card
            if len(cards) > MAX_CARDS:
                for old in sorted(cards.values(), key=lambda c: c.get("last", 0))[: len(cards) - MAX_CARDS]:
                    cards.pop(old["id"], None)
            self.ctx.state.set(STATE_KEY, cards)

    def card(self, card_id: str) -> dict[str, Any] | None:
        card = self._cards().get(card_id)
        return card if isinstance(card, dict) else None

    def _origin(self) -> dict[str, str] | str:
        env = lambda key: str(self.session_env(key, "") or "").strip()  # noqa: E731
        if env("HERMES_SESSION_PLATFORM").lower() != "telegram":
            return "telegram_session_required"
        if env("HERMES_SESSION_CHAT_TYPE").lower() not in {"dm", "private"}:
            return "private_chat_required"
        origin = {"chat_id": env("HERMES_SESSION_CHAT_ID"), "owner_user_id": env("HERMES_SESSION_USER_ID"),
                  "thread_id": env("HERMES_SESSION_THREAD_ID"), "session_key": env("HERMES_SESSION_KEY")}
        if not origin["chat_id"].lstrip("-").isdigit() or not origin["owner_user_id"]:
            return "telegram_session_required"
        return origin

    def _session(self, origin: dict[str, str], session_id: str = "") -> str:
        return origin["session_key"] or session_id or f"chat:{origin['chat_id']}"

    def _current(self, origin: dict[str, str], session: str, now: float) -> dict[str, Any] | None:
        """This session's card while it is live (working, held, or idle for less than REUSE_SECONDS)."""
        mine = [c for c in self._cards().values() if isinstance(c, dict) and c.get("session") == session
                and c.get("chat_id") == origin["chat_id"] and c.get("mini_app") == MINI_APP]  # link is baked into HTML
        if not mine:
            return None
        card = max(mine, key=lambda c: c.get("created_at", 0))
        if card.get("active") or card["state"].get("m") == "h" or now - card.get("last", 0) < REUSE_SECONDS:
            return card
        return None

    def _mode(self, active: bool) -> str:
        try:
            if self.human_holds():
                return "h"
        except Exception:
            logger.debug("lease read failed", exc_info=True)
        return "w" if active else "i"

    def _touch(self, origin: dict[str, str], session: str, changes: dict[str, Any], *, active: bool) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            card = self._current(origin, session, now)
            if card is None:
                card = {"id": secrets.token_hex(4), "session": session, **origin, "lang": self.language(),
                        "state": {}, "tg_message_id": "", "sent": "", "created_at": int(now), "mini_app": MINI_APP}
            state = dict(card["state"])
            if "h" in changes and changes["h"] != state.get("h"):
                state.pop("t", None)  # another site: the old title is wrong
            state.update({k: v for k, v in changes.items() if v is not None})
            state = {k: v for k, v in state.items() if v != ""}
            card = {**card, "state": state, "active": active or card.get("active", False), "last": int(now)}
            if "q" in changes:
                card["q_turn_ended"] = False
            card["state"]["m"] = self._mode(card["active"])
            card["state"]["u"] = int(now)
            self._save(card)
        return card

    # ---- hooks (agent thread: never block, never raise) -----------------------------------------
    def on_pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> Any:
        if not isinstance(tool_name, str) or tool_name not in FENCED_TOOLS:
            return None
        try:
            if self.human_holds():
                return {"action": "block", "message": FENCE_MESSAGE}
        except Exception:  # the lease cannot be read: Hermes's own screen-tool fence still applies
            logger.debug("agent computer: lease read failed in pre_tool_call", exc_info=True)
        if tool_name not in SCREEN_TOOLS:
            return None
        try:
            origin = self._origin()
            if isinstance(origin, str) or self.runtime is None:
                return None
            session = self._session(origin, session_id)
            current = self._current(origin, session, self.clock()) or {}
            changes = describe(tool_name, args, current.get("lang") or self.language())
            if current.get("q_turn_ended"):
                changes["q"] = ""  # a later turn uses the screen again: the old request was answered in chat
            card = self._touch(origin, session, changes, active=True)
            self._kick(card["id"])
        except Exception:
            logger.warning("agent computer: pre_tool_call update failed", exc_info=True)
        return None

    def on_post_tool_call(self, tool_name: str = "", result: Any = None, session_id: str = "", **_: Any) -> None:
        if not tool_name.startswith("browser_"):
            return None
        try:
            origin = self._origin()
            if isinstance(origin, str) or self.runtime is None:
                return None
            page = page_from_result(result)
            session = self._session(origin, session_id)
            if page and self._current(origin, session, self.clock()) is not None:
                self._kick(self._touch(origin, session, page, active=True)["id"])
        except Exception:
            logger.debug("agent computer: post_tool_call update failed", exc_info=True)
        return None

    def on_post_llm_call(self, session_id: str = "", **_: Any) -> None:
        """The turn ended: Hermes no longer works on the screen (the card stays reusable for a while)."""
        try:
            origin = self._origin()
            if isinstance(origin, str) or self.runtime is None:
                return None
            self._idle(self._session(origin, session_id), origin["chat_id"], turn_end=True)
        except Exception:
            logger.debug("agent computer: post_llm_call update failed", exc_info=True)
        return None

    def _idle(self, session: str, chat_id: str, *, older_than: float = 0, turn_end: bool = False) -> None:
        now = self.clock()
        with self._lock:
            for card in self._cards().values():
                if not isinstance(card, dict) or card.get("session") != session or card.get("chat_id") != chat_id:
                    continue
                if not card.get("active") or now - card.get("last", 0) < older_than:
                    continue
                card = {**card, "active": False, "state": {**card["state"], "m": self._mode(False)}}
                if turn_end and card["state"].get("q"):
                    card["q_turn_ended"] = True
                self._save(card)
                self._kick(card["id"])

    # ---- the model's takeover request ----------------------------------------------------------
    def handle_tool(self, args: dict, **kwargs) -> str:
        session_id = str(kwargs.get("session_id", "") or "")
        try:
            args = args or {}
            origin = self._origin()
            if isinstance(origin, str):
                return self._error(origin)
            if self.runtime is None:
                return self._error("telegram_gateway_unavailable")
            reason = _one_line(args.get("reason", ""), MAX_REASON + 1)
            if not reason or len(reason) > MAX_REASON:
                return self._error("invalid_reason", limit=MAX_REASON)
            card = self._touch(origin, self._session(origin, session_id), {"q": reason}, active=True)
            self.runtime.submit(self._sync(card["id"]))
            card = self.card(card["id"]) or card
            if not card.get("tg_message_id"):
                return self._error("card_not_sent")
            return json.dumps({"ok": True, "card": _public(card), "note": (
                "The Computer card in the chat now asks the user to take over the screen for this. End your "
                "turn now: reply NO_REPLY or one short line, and do not repeat the request. When the user "
                "hands control back you get an [Agent computer] message (or they write in the chat); then "
                "check the screen and continue. Until then screen, shell and code tools are refused while the "
                "user holds control.")})
        except Exception as exc:
            logger.exception("telegram_agent_computer failed")
            return self._error("agent_computer_failed", detail=type(exc).__name__)

    @staticmethod
    def _error(code: str, **extra: Any) -> str:
        return json.dumps({"ok": False, "error": code, **extra})

    # ---- lease transitions (any thread) ---------------------------------------------------------
    def on_lease(self, lease: Any) -> None:
        """Pill follows the lease; a hand-back after a takeover request resumes the owning session."""
        holder = str(getattr(lease, "holder", "") or "")
        with self._lock:
            returned = self._last_holder == "human" and holder == "agent"
            self._last_holder = holder
            resume: list[dict[str, Any]] = []
            now = self.clock()
            for card in self._cards().values():
                if not isinstance(card, dict) or not card.get("tg_message_id") or now - card.get("last", 0) > 86400:
                    continue
                state = dict(card["state"])
                state["m"] = "h" if holder == "human" else ("w" if card.get("active") else "i")
                if returned and state.get("q"):
                    resume.append(card)
                    state.pop("q")
                if state != card["state"]:
                    state["u"] = int(now)
                    self._save({**card, "state": state})
                    self._kick(card["id"])
        for card in resume:
            if card.get("session_key"):
                try:
                    self.ctx.inject_message(_RESUME_PROMPT.format(reason=card["state"]["q"]), role="user",
                                            session_key=card["session_key"])
                except Exception:
                    logger.warning("agent computer: resume injection failed", exc_info=True)

    def request_for(self, card_id: str, user_id: str) -> dict[str, str] | None:
        """What the Mini App may show for ``startapp=c_<card_id>``: only the card owner's own card."""
        card = self.card(card_id)
        if card is None or card.get("owner_user_id") != str(user_id):
            return None
        return {"request": card["state"].get("q", ""), "page": card["state"].get("h", "")}

    # ---- Telegram (gateway loop) ---------------------------------------------------------------
    def _kick(self, card_id: str) -> None:
        with self._lock:
            if card_id in self._queued or self.runtime is None:
                return
            self._queued.add(card_id)
        scheduled = False
        try:
            scheduled = self.runtime.fire(self._sync(card_id))
        finally:
            if not scheduled:
                with self._lock:
                    self._queued.discard(card_id)

    def _loop_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._alock is None or self._alock[0] is not loop:
            self._alock = (loop, asyncio.Lock())
        return self._alock[1]

    async def _username(self) -> str:
        if self._bot_username:
            return self._bot_username
        bot = self.runtime.bot
        try:
            name = bot.username
        except Exception:
            name = ""
        if not name:
            name = (await bot.get_me()).username
        self._bot_username = str(name or "")
        return self._bot_username

    async def _sync(self, card_id: str) -> None:
        """Send the card once, then push only changed chat_data. Reads the newest state, so queued
        updates coalesce; serialised so a send finishes before the first edit."""
        with self._lock:
            self._queued.discard(card_id)
        async with self._loop_lock():
            card = self.card(card_id)
            if card is None:
                return
            data = chat_data(card["state"])
            try:
                if not card.get("tg_message_id"):
                    bot = await self._username()
                    if not _BOT_RE.fullmatch(bot):
                        logger.warning("agent computer: bot username unavailable, card not sent")
                        return
                    markup = render_html(card, bot)
                    if not widget_fits(markup):
                        logger.warning("agent computer: card HTML over the widget limit")
                        return
                    api: dict[str, Any] = {"chat_id": int(card["chat_id"]), "html": markup, "chat_data": data}
                    if card["thread_id"].isdigit():
                        api["message_thread_id"] = int(card["thread_id"])
                    message = await self.runtime.bot.do_api_request("sendWidget", api_kwargs=api)
                    message_id = message["message_id"] if isinstance(message, dict) else message.message_id
                    with self._lock:
                        fresh = self.card(card_id) or card
                        self._save({**fresh, "tg_message_id": str(message_id), "sent": data})
                        if chat_data(fresh["state"]) != data:
                            self._kick(card_id)
                    self._schedule_idle(card)
                    return
                if data == card.get("sent"):
                    return
                api = {"chat_id": int(card["chat_id"]), "message_id": int(card["tg_message_id"]), "chat_data": data}
                await self.runtime.bot.do_api_request("editMessageWidgetData", api_kwargs=api)
                with self._lock:
                    self._save({**(self.card(card_id) or card), "sent": data})
                self._schedule_idle(card)
            except Exception as exc:
                text = str(exc).lower()
                if "not found" in text or "message to edit" in text:
                    # The user deleted the card (or it is too old to edit): forget it, the next use sends a new one.
                    with self._lock:
                        cards = self._cards()
                        cards.pop(card_id, None)
                        self.ctx.state.set(STATE_KEY, cards)
                    logger.info("agent computer: card %s is gone from the chat, forgotten", card_id)
                elif "not modified" not in text:
                    logger.warning("agent computer: Telegram update failed for card %s: %s", card_id, exc)

    def _schedule_idle(self, card: dict[str, Any]) -> None:
        """Fallback when no turn-end hook arrives: idle after IDLE_SECONDS without screen calls."""
        if not card.get("active"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        session, chat = card["session"], card["chat_id"]
        loop.call_later(IDLE_SECONDS + 1, lambda: self._idle(session, chat, older_than=IDLE_SECONDS))
