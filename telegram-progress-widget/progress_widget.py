"""A "Hermes is working" card for long turns in a private Telegram chat (Bot API 10.4 ``sendWidget``).

A turn that is still running after ``SHOW_DELAY`` seconds, or whose second tool call starts first, gets
ONE card: a header, an elapsed timer that ticks inside the widget from the start time in ``chat_data``,
the latest steps (done ✓, the current one pulsing with animated dots, older ones as "+N earlier") and a
neutral Stop button. Steps are fixed human labels per tool, never tool arguments or results. When the
turn ends the card is deleted, so the answer is the only message left.

Threading: hooks run on agent (or hook-worker) threads. They only read the session context there and
hand plain values to the gateway loop (``call_soon_threadsafe``); every turn state change, timer and
Telegram call happens on that loop, so no hook ever blocks or fails and the state needs no lock.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

SHOW_DELAY = 8.0  # seconds a turn runs before the card appears
SHOW_AT_TOOL = 2  # ... or the tool call number that shows it at once
PUSH_DELAY = 1.5  # coalesce step updates into one editMessageWidgetData per this many seconds
STOP_GRACE = 20.0  # after Stop, a turn still running this long gets the "type /stop" hint
MAX_ROWS = 5  # latest steps on the card; older ones collapse into "+N earlier"
MAX_COUNT = 999  # "×N" cap per merged step
MAX_CHAT_DATA = 1024
MAX_HTML_BYTES = 32768
MAX_COMPRESSED_BYTES = 4096
STATE_KEY = "progress_cards"  # {session_key: [chat_id, message_id]} of cards on screen (restart sweep)
SUPPRESSING_TOOLS = {"telegram_task_activity"}  # that card already shows progress: ours goes away
_DATA_RE = re.compile(r"^stop$")  # the only widget request on the card

# Tool name -> label code. The card only ever carries these codes, so no argument can reach it.
_EXACT = {
    "web_search": "web", "telegram_inbox_widget": "mail", "telegram_email_widget": "email",
    "telegram_calendar_widget": "event", "computer_use": "screen", "terminal": "cmd", "execute_code": "code",
    "read_file": "files", "search_files": "files", "delegate_task": "helper", "skill_view": "skill",
    "memory": "memory", "session_search": "memory",
}
_PREFIX = (("browser_", "browser"), ("web_", "web"))

TEXT: dict[str, dict[str, Any]] = {
    "en": {
        "title": "⏳ Working on it", "stop": "Stop", "stopping": "Stopping…", "earlier": "+{n} earlier",
        "think": "Thinking", "hint": "Type /stop in the chat", "err": "Could not send: {code}",
        "denied": "Not allowed", "stop_ack": "Stopping…", "stop_asked": "Asked Hermes to stop",
        # Done steps read in the past tense; the current (pulsing) step in the present, with animated dots.
        "labels": {
            "web": "Searched the web", "mail": "Checked mail", "email": "Drafted an email",
            "event": "Drafted an event", "browser": "Used the browser", "screen": "Used the screen",
            "cmd": "Ran a command", "code": "Ran code", "files": "Read files", "helper": "Asked a helper",
            "skill": "Looked up a skill", "memory": "Checked memory", "work": "Worked on it",
        },
        "now": {
            "web": "Searching the web", "mail": "Checking mail", "email": "Drafting an email",
            "event": "Drafting an event", "browser": "Using the browser", "screen": "Using the screen",
            "cmd": "Running a command", "code": "Running code", "files": "Reading files",
            "helper": "Asking a helper", "skill": "Looking up a skill", "memory": "Checking memory",
            "work": "Working",
        },
    },
    "ru": {
        "title": "⏳ Работаю", "stop": "Остановить", "stopping": "Останавливаю…", "earlier": "+{n} ранее",
        "think": "Думаю", "hint": "Напишите /stop в чат", "err": "Не удалось отправить: {code}",
        "denied": "Недоступно", "stop_ack": "Останавливаю…", "stop_asked": "Попросил Hermes остановиться",
        "labels": {
            "web": "Искал в интернете", "mail": "Проверил почту", "email": "Подготовил письмо",
            "event": "Подготовил событие", "browser": "Работал в браузере", "screen": "Работал с экраном",
            "cmd": "Выполнил команду", "code": "Запустил код", "files": "Прочитал файлы",
            "helper": "Попросил помощника", "skill": "Открыл навык", "memory": "Проверил память",
            "work": "Поработал",
        },
        "now": {
            "web": "Ищу в интернете", "mail": "Проверяю почту", "email": "Готовлю письмо",
            "event": "Готовлю событие", "browser": "Работаю в браузере", "screen": "Работаю с экраном",
            "cmd": "Выполняю команду", "code": "Запускаю код", "files": "Читаю файлы",
            "helper": "Прошу помощника", "skill": "Открываю навык", "memory": "Проверяю память",
            "work": "Работаю",
        },
    },
}

_CSS = (
    "body{margin:0}"
    ".c{padding:12px 14px 10px}"
    ".h{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}"
    ".t{font-weight:600;font-size:1.05em}"
    ".p{font-size:.78em;font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap;"
    "font-variant-numeric:tabular-nums;"
    "background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-accent-text-color)}"
    ".p.off{color:var(--tg-theme-hint-color)}"
    "ol{list-style:none;margin:0;padding:0;display:grid;gap:4px}"
    "li{display:flex;gap:8px;font-size:.92em;overflow-wrap:anywhere}"
    "li i{flex:0 0 1.1em;font-style:normal;text-align:center;color:var(--tg-theme-hint-color)}"
    "li.ok i{color:#2fa84f}"
    "li.e{font-size:.8em;color:var(--tg-theme-hint-color)}"
    "li.cur{font-weight:600}"
    "li.cur i{color:var(--tg-theme-button-color)}"
    ".dt{display:inline-block;overflow:hidden;vertical-align:bottom;white-space:nowrap;width:0;"
    "animation:dt 1.2s steps(4,jump-none) infinite}"
    "@keyframes dt{to{width:1.1em}}"
    ".busy{animation:pl 1.6s ease-in-out infinite}@keyframes pl{50%{opacity:.4}}"  # Hermes is working
    ".z{display:flex;gap:8px;margin-top:10px}"
    ".bt{flex:1;border:0;border-radius:10px;padding:10px 12px;font:inherit;font-size:.93em;font-weight:600;"
    "background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color);transition:transform .12s}"
    ".bt:active{transform:scale(.98)}"
    ".bt[disabled]{opacity:.6}"
    ".f{margin-top:8px;font-size:.8em;color:var(--tg-theme-hint-color)}"
    "#ms{display:none}"
    "@media (prefers-reduced-motion:reduce){.bt{transition:none}.dt,.busy{animation:none}.dt{width:auto}}"
)


def current_language() -> str:
    """Hermes's UI language (``HERMES_LANGUAGE`` > ``display.language`` > en), reduced to en/ru."""
    try:
        from agent.i18n import get_language
        return "ru" if str(get_language()).lower().startswith("ru") else "en"
    except Exception:
        return "en"


def tr(lang: str, key: str) -> Any:
    return TEXT.get(lang, TEXT["en"])[key]


def label_for(tool_name: Any) -> str:
    """The label code of a tool; unknown tools are "work" ("Working")."""
    name = str(tool_name or "")
    if name in _EXACT:
        return _EXACT[name]
    return next((code for prefix, code in _PREFIX if name.startswith(prefix)), "work")


def enable_widget_queries(update_cls: Any) -> bool:
    """python-telegram-bot 22.8 predates Bot API 10.4: ``Update.ALL_TYPES`` (which Hermes passes as
    ``allowed_updates`` on every start of polling) lacks ``widget_query``, so Telegram never sends taps
    from widgets. Add it to that list in place; PTB keeps the unknown field in ``update.api_kwargs``."""
    types = getattr(update_cls, "ALL_TYPES", None)
    if not isinstance(types, list):
        return False
    if "widget_query" not in types:
        types.append("widget_query")
    return True


def _script(lang: str) -> str:
    """Draws the timer, the steps and Stop from chat_data {"t": start unix, "s": [[code, count, running], …],
    "e": earlier steps, "x": 1 stopping / 2 asked to stop, "f": 1 stop hint}. The timer ticks here (setInterval),
    never via Telegram edits. Stop is a widget request ("stop"); the bot's {"m": text} answer or the error shows
    in #ms."""
    words = {k: tr(lang, k) for k in ("labels", "now", "earlier", "think", "stopping", "stop_asked", "hint", "err")}
    return (
        "(function(){var W=window.TelegramWidget,X=" + json.dumps(words, ensure_ascii=False, separators=(",", ":"))
        + ",$=function(i){return document.getElementById(i)},d={};"
        "function z(n){return(n<10?\"0\":\"\")+n}"
        "function tk(){var s=d.t?Math.max(0,Math.floor(Date.now()/1e3-d.t)):0,h=Math.floor(s/3600),m=Math.floor(s/60)%60;"
        "$(\"tm\").textContent=(h?h+\":\"+z(m):m)+\":\"+z(s%60)}"
        "function say(t){var m=$(\"ms\");m.textContent=t||\"\";m.style.display=t?\"block\":\"none\"}"
        "function row(c,i,t,dots){var li=document.createElement(\"li\"),a=document.createElement(\"i\"),"
        "b=document.createElement(\"span\");li.className=c;a.textContent=i;b.textContent=t;"
        "if(dots){var o=document.createElement(\"span\");o.className=\"dt\";o.textContent=\"...\";b.appendChild(o)}"
        "li.appendChild(a);li.appendChild(b);return li}"
        "function r(){try{d=JSON.parse(W&&W.chatData)||{}}catch(e){d={}}"
        "var g=$(\"lg\"),s=d.s||[],on=0;g.innerHTML=\"\";"
        "if(d.e)g.appendChild(row(\"e\",\"\",X.earlier.replace(\"{n}\",d.e)));"
        "s.forEach(function(e){var L=e[2]?X.now:X.labels,t=(L[e[0]]||L.work)+(e[1]>1?\" ×\"+e[1]:\"\");"
        "if(e[2]){on=1;g.appendChild(row(\"cur busy\",\"•\",t,1))}else g.appendChild(row(\"ok\",\"✓\",t))});"
        "if(!on&&!d.x)g.appendChild(row(\"cur busy\",\"•\",X.think,1));"
        "$(\"tm\").className=\"p\"+(d.x?\" off\":\"\");$(\"z\").style.display=d.x?\"none\":\"\";"
        "if(d.x)say(d.f?X.hint:d.x==2?X.stop_asked:X.stopping);tk()}"
        "$(\"sp\").addEventListener(\"click\",function(){var b=$(\"sp\");if(!W||!W.request||b.disabled)return;"
        "b.disabled=true;say(\"\");W.request(\"stop\").then(function(a){var q=null;try{q=JSON.parse(a.result)}catch(e){}"
        "say(q&&q.m);if(!(q&&q.d))b.disabled=false},function(e){b.disabled=false;"
        "say(X.err.replace(\"{code}\",(e&&e.code)||\"FAILED\"))})});"
        "if(W&&W.onEvent)W.onEvent(\"chatDataChanged\",r);r();setInterval(tk,1000)})();"
    )


def render_html(lang: str) -> str:
    # Stop is neutral (.bt, not red): it ends the reply early and loses nothing.
    return (
        f"<style>{_CSS}</style><div class=\"c\"><div class=\"h\"><span class=\"t\">{html.escape(tr(lang, 'title'))}"
        f"</span><span id=\"tm\" class=\"p\">0:00</span></div><ol id=\"lg\"></ol>"
        f"<div id=\"z\" class=\"z\"><button id=\"sp\" class=\"bt\" type=\"button\">{html.escape(tr(lang, 'stop'))}"
        f"</button></div><div id=\"ms\" class=\"f\"></div></div><script>{_script(lang)}</script>"
    )


def widget_fits(markup: str) -> bool:
    raw = markup.encode("utf-8")
    return len(raw) <= MAX_HTML_BYTES and len(zlib.compress(raw, 9)) <= MAX_COMPRESSED_BYTES


@dataclass
class Turn:
    """One running agent turn in a private chat, owned by the gateway loop."""
    key: str  # gateway session key (HERMES_SESSION_KEY)
    turn_id: str
    session_id: str
    chat_id: str
    owner: str  # the chat owner's Telegram user id: the only one who may Stop
    thread_id: str
    lang: str
    started: float
    steps: list[list[Any]] = field(default_factory=list)  # [code, count, running]
    tools: int = 0
    phase: str = "pending"  # pending -> sending -> shown; ended / suppressed are final
    message_id: str = ""
    stopping: int = 0  # 1: a real /stop was sent, 2: the session was asked to stop
    hint: bool = False
    timer: Any = None
    push_timer: Any = None
    sent: str = ""  # the chat_data last sent to Telegram

    @property
    def over(self) -> bool:
        return self.phase in {"ended", "suppressed"}


def chat_data(turn: Turn) -> str:
    """The card state: the start time, the latest MAX_ROWS steps and how many older ones there are.
    Label codes and counters only, so it stays far below Telegram's 1024 bytes."""
    rows = turn.steps[-MAX_ROWS:]
    state: dict[str, Any] = {"t": int(turn.started), "s": [[c, min(n, MAX_COUNT), 1 if r else 0] for c, n, r in rows]}
    if len(turn.steps) > len(rows):
        state["e"] = len(turn.steps) - len(rows)
    if turn.stopping:
        state["x"] = turn.stopping
    if turn.hint:
        state["f"] = 1
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def _first_text(message: Any) -> str:
    """The user message's text: a plain string, or the first text part of a multimodal message."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        for part in message:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
            if isinstance(part, str):
                return part
    return ""


_STOP_PROMPT = (
    "[Progress widget] The user tapped Stop on the progress card of the reply you are working on. Stop this work "
    "as soon as it is safe, do not start another destructive or external action, and say in one line what state "
    "things are in."
)


class GatewayRuntime:
    """The live gateway's bot and event loop. ``call`` is the only method used off the loop."""

    def __init__(self, bot: Any, loop: asyncio.AbstractEventLoop | None):
        self.bot = bot
        self.loop = loop
        self._tasks: set[asyncio.Task] = set()

    def call(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run ``fn(*args)`` on the gateway loop soon; never blocks, never raises."""
        loop = self.loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # the loop closed in between
            pass

    def later(self, delay: float, fn: Callable[..., Any], *args: Any) -> Any:
        return self.loop.call_later(delay, fn, *args)

    def spawn(self, coroutine) -> None:
        task = self.loop.create_task(coroutine)
        self._tasks.add(task)  # keep a reference until done (fire-and-forget)
        task.add_done_callback(self._tasks.discard)


class ProgressService:
    def __init__(self, ctx: Any, session_env: Callable[..., str], language: Callable[[], str] = current_language,
                 clock: Callable[[], float] = time.time):
        self.ctx = ctx
        self.session_env = session_env
        self.language = language
        self.clock = clock
        self.runtime: GatewayRuntime | None = None
        self.adapter: Any = None  # the Telegram adapter: its message path is how a real /stop is sent
        self._turns: dict[str, Turn] = {}  # session key -> turn; touched only on the gateway loop

    # ---- hooks (agent threads): read the session context, hand plain values to the loop --------------

    def _env(self, key: str) -> str:
        try:
            return str(self.session_env(key, "") or "").strip()
        except Exception:
            return ""

    def _post(self, fn: Callable[..., Any], *args: Any) -> None:
        runtime = self.runtime
        if runtime is not None:
            runtime.call(fn, *args)

    def on_pre_llm_call(self, session_id: Any = "", turn_id: Any = "", user_message: Any = "", platform: Any = "",
                        parent_session_id: Any = "", **_: Any) -> None:
        try:
            if self.runtime is None or parent_session_id:  # a helper's own turn is not a reply in the chat
                return
            if str(platform or self._env("HERMES_SESSION_PLATFORM")).lower() != "telegram":
                return
            if self._env("HERMES_SESSION_CHAT_TYPE").lower() not in {"dm", "private"}:
                return
            if _first_text(user_message).lstrip().startswith("["):  # plugin-injected turn
                return
            key, chat_id, owner = (self._env(k) for k in ("HERMES_SESSION_KEY", "HERMES_SESSION_CHAT_ID",
                                                          "HERMES_SESSION_USER_ID"))
            if not key or not chat_id.lstrip("-").isdigit() or not owner:
                return
            turn = Turn(key=key, turn_id=str(turn_id or ""), session_id=str(session_id or ""), chat_id=chat_id,
                        owner=owner, thread_id=self._env("HERMES_SESSION_THREAD_ID"), lang=self.language(),
                        started=self.clock())
            self._post(self._begin, turn)
        except Exception:
            logger.debug("progress widget pre_llm_call failed", exc_info=True)

    def on_pre_tool_call(self, tool_name: Any = "", turn_id: Any = "", session_id: Any = "", **_: Any) -> None:
        # Only the tool NAME crosses to the loop; args never leave this frame. Returns None: never blocks.
        try:
            self._post(self._tool, self._env("HERMES_SESSION_KEY"), str(turn_id or ""), str(session_id or ""),
                       str(tool_name or ""), True)
        except Exception:
            logger.debug("progress widget pre_tool_call failed", exc_info=True)

    def on_post_tool_call(self, tool_name: Any = "", turn_id: Any = "", session_id: Any = "", **_: Any) -> None:
        try:
            self._post(self._tool, self._env("HERMES_SESSION_KEY"), str(turn_id or ""), str(session_id or ""),
                       str(tool_name or ""), False)
        except Exception:
            logger.debug("progress widget post_tool_call failed", exc_info=True)

    def on_turn_end(self, turn_id: Any = "", session_id: Any = "", **_: Any) -> None:
        """post_llm_call and on_session_end (which also fires for failed and interrupted turns)."""
        try:
            self._post(self._finish, self._env("HERMES_SESSION_KEY"), str(turn_id or ""), str(session_id or ""))
        except Exception:
            logger.debug("progress widget turn end failed", exc_info=True)

    def on_agent_loop_stopped(self, session_key: Any = "", **_: Any) -> None:
        """/stop (typed, or ours) interrupted the session's turn: the card goes at once."""
        try:
            self._post(self._finish, str(session_key or ""), "", "", True)
        except Exception:
            logger.debug("progress widget agent_loop_stopped failed", exc_info=True)

    # ---- gateway loop -------------------------------------------------------------------------------

    def _match(self, key: str, turn_id: str, session_id: str) -> Turn | None:
        """The live turn these hook ids belong to. Helpers (delegate_task) run their own turns with their
        own ids under the same session key, so they never touch the parent's card."""
        turn = self._turns.get(key)
        if turn is None or turn.over:
            return None
        if turn_id and turn.turn_id:
            return turn if turn_id == turn.turn_id else None
        if session_id and turn.session_id:
            return turn if session_id == turn.session_id else None
        return turn

    def _begin(self, turn: Turn) -> None:
        old = self._turns.get(turn.key)
        if old is not None and not old.over:
            if old.turn_id and old.turn_id == turn.turn_id:
                return  # the same turn's pre_llm_call again
            self._end(old, "ended")  # its end was missed: never leave a stale card behind
        self._turns[turn.key] = turn
        turn.timer = self.runtime.later(SHOW_DELAY, self._due, turn)

    def _due(self, turn: Turn) -> None:
        turn.timer = None
        if turn.phase == "pending" and self._turns.get(turn.key) is turn:
            turn.phase = "sending"
            self.runtime.spawn(self._send(turn))

    def _tool(self, key: str, turn_id: str, session_id: str, tool_name: str, starting: bool) -> None:
        turn = self._match(key, turn_id, session_id)
        if turn is None:
            return
        if tool_name in SUPPRESSING_TOOLS:
            if starting:
                self._end(turn, "suppressed")
            return
        code = label_for(tool_name)
        if starting:
            turn.tools += 1
            last = turn.steps[-1] if turn.steps else None
            if last is not None and last[0] == code:
                last[1] += 1
                last[2] += 1
            else:
                turn.steps.append([code, 1, 1])
            if turn.phase == "pending" and turn.tools >= SHOW_AT_TOOL:
                self._cancel(turn, "timer")
                self._due(turn)
                return
        else:
            step = next((s for s in reversed(turn.steps) if s[0] == code and s[2] > 0), None)
            if step is None:
                return
            step[2] -= 1
        self._dirty(turn)

    def _finish(self, key: str, turn_id: str, session_id: str, any_turn: bool = False) -> None:
        turn = self._turns.get(key) if any_turn else self._match(key, turn_id, session_id)
        if turn is not None and not turn.over:
            self._end(turn, "ended")

    def _cancel(self, turn: Turn, which: str) -> None:
        handle = getattr(turn, which)
        if handle is not None:
            handle.cancel()
            setattr(turn, which, None)

    def _end(self, turn: Turn, phase: str) -> None:
        shown = turn.phase == "shown"
        turn.phase = phase
        self._cancel(turn, "timer")
        self._cancel(turn, "push_timer")
        if self._turns.get(turn.key) is turn:
            del self._turns[turn.key]
        if shown:
            self.runtime.spawn(self._delete(turn.chat_id, turn.message_id, turn.key))
        # phase "sending": _send sees the turn is over when Telegram answers and deletes the card then.

    def _dirty(self, turn: Turn) -> None:
        if turn.phase == "shown" and turn.push_timer is None:
            turn.push_timer = self.runtime.later(PUSH_DELAY, self._flush, turn)

    def _flush(self, turn: Turn) -> None:
        turn.push_timer = None
        if turn.phase == "shown":
            self.runtime.spawn(self._push(turn))

    # ---- Telegram ----------------------------------------------------------------------------------

    async def _send(self, turn: Turn) -> None:
        api: dict[str, Any] = {"chat_id": int(turn.chat_id), "html": render_html(turn.lang),
                               "chat_data": chat_data(turn), "allow_queries": True}
        if turn.thread_id.isdigit():
            api["message_thread_id"] = int(turn.thread_id)
        try:
            message = await self.runtime.bot.do_api_request("sendWidget", api_kwargs=api)
        except Exception as exc:
            logger.warning("progress card not sent: %s", exc)
            if not turn.over:
                self._end(turn, "ended")  # no card: nothing to update or stop
            return
        turn.message_id = str(message["message_id"] if isinstance(message, dict) else message.message_id)
        turn.sent = api["chat_data"]
        if turn.over:  # the turn ended (or the activity card took over) while Telegram was sending
            await self._delete(turn.chat_id, turn.message_id, "")
            return
        turn.phase = "shown"
        self._remember(turn.key, turn.chat_id, turn.message_id)
        if chat_data(turn) != turn.sent:
            self._dirty(turn)

    async def _push(self, turn: Turn) -> None:
        data = chat_data(turn)
        if turn.phase != "shown" or data == turn.sent:
            return
        turn.sent = data
        try:
            await self.runtime.bot.do_api_request("editMessageWidgetData", api_kwargs={
                "chat_id": int(turn.chat_id), "message_id": int(turn.message_id), "chat_data": data})
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                logger.warning("progress card update failed: %s", exc)

    async def _delete(self, chat_id: str, message_id: str, key: str) -> None:
        try:
            await self.runtime.bot.do_api_request("deleteMessage", api_kwargs={
                "chat_id": int(chat_id), "message_id": int(message_id)})
        except Exception as exc:
            logger.warning("progress card not deleted: %s", exc)
        if key:
            self._forget(key, message_id)

    # Cards on screen are also kept in plugin state, so a gateway restart mid-turn cannot leave a ticking
    # "Working on it" behind: the next wiring deletes every recorded card that no live turn owns.
    def _cards(self) -> dict[str, Any]:
        try:
            cards = self.ctx.state.get(STATE_KEY, default={})
        except Exception:
            return {}
        return cards if isinstance(cards, dict) else {}

    def _remember(self, key: str, chat_id: str, message_id: str) -> None:
        try:
            self.ctx.state.set(STATE_KEY, {**self._cards(), key: [chat_id, message_id]})
        except Exception:
            logger.debug("progress card state not saved", exc_info=True)

    def _forget(self, key: str, message_id: str | None = None) -> None:
        """Drop the record of ``key`` (only while it still names ``message_id``, when one is given)."""
        cards = self._cards()
        entry = cards.get(key)
        if key not in cards or (message_id is not None and not (
                isinstance(entry, list) and len(entry) == 2 and str(entry[1]) == str(message_id))):
            return
        cards.pop(key)
        try:
            self.ctx.state.set(STATE_KEY, cards)
        except Exception:
            logger.debug("progress card state not saved", exc_info=True)

    def sweep(self) -> None:
        """Delete cards left by a previous gateway process (loop thread, at wiring)."""
        live = {t.message_id for t in self._turns.values() if t.message_id}
        for key, entry in list(self._cards().items()):
            if not (isinstance(entry, list) and len(entry) == 2 and str(entry[0]).lstrip("-").isdigit()
                    and str(entry[1]).isdigit()):
                self._forget(key)
            elif str(entry[1]) not in live:
                self.runtime.spawn(self._delete(str(entry[0]), str(entry[1]), key))

    # ---- Stop (widget requests, on the gateway loop) -----------------------------------------------

    async def _answer(self, query_id: str, text: str, *, done: bool = False) -> None:
        """{"m": text} shows under the card; "d": 1 keeps the tapped Stop disabled."""
        payload = {"m": text, **({"d": 1} if done else {})}
        try:
            await self.runtime.bot.do_api_request("answerWidgetQuery", api_kwargs={
                "widget_query_id": query_id, "data": json.dumps(payload, ensure_ascii=False)})
        except Exception as exc:
            logger.warning("answerWidgetQuery failed: %s", exc)

    def _can_interrupt(self) -> bool:
        adapter = self.adapter
        return callable(getattr(adapter, "handle_message", None)) and callable(getattr(adapter, "build_source", None))

    async def _interrupt(self, turn: Turn) -> None:
        """A real /stop from the owner through the adapter's own message path — the same MessageEvent the
        Telegram adapter builds for a typed /stop, routed to this turn's session key, so the gateway runs its
        /stop handling (access check, interrupt, queue clear, background helpers stopped, "Stopped" reply)."""
        from gateway.platforms.event import MessageEvent, MessageType

        source = self.adapter.build_source(chat_id=turn.chat_id, chat_type="dm", user_id=turn.owner,
                                           thread_id=turn.thread_id or None)
        await self.adapter.handle_message(MessageEvent(
            text="/stop", message_type=MessageType.COMMAND, source=source, user_id=turn.owner,
            metadata={"gateway_session_key": turn.key}))

    async def on_update(self, update: Any, context: Any) -> None:
        """Stop taps. Telegram authenticates ``from``; the chat and message pin the card."""
        del context
        query = (getattr(update, "api_kwargs", None) or {}).get("widget_query")
        if not isinstance(query, dict) or not query.get("id"):
            return
        chat_id = str((query.get("chat") or {}).get("id", ""))
        user_id = str((query.get("from") or {}).get("id", ""))
        message_id = str(query.get("message_id", ""))
        turn = next((t for t in self._turns.values() if t.chat_id == chat_id and t.message_id == message_id), None)
        if turn is None:
            return  # not a live card of ours: another plugin's widget may answer it
        logger.info("progress widget query %s on session %s", query.get("data"), turn.key)
        if turn.owner != user_id or not _DATA_RE.fullmatch(str(query.get("data", ""))):
            await self._answer(query["id"], tr(turn.lang, "denied"))
            return
        if turn.stopping:
            await self._answer(query["id"], tr(turn.lang, "stop_ack" if turn.stopping == 1 else "stop_asked"),
                               done=True)
            return
        real = self._can_interrupt()
        turn.stopping = 1 if real else 2  # one Stop per turn, committed before any side effect
        # Answer first (Telegram allows 15 s), and only claim what is about to happen.
        await self._answer(query["id"], tr(turn.lang, "stop_ack") if real else tr(turn.lang, "stop_asked"), done=True)
        await self._push(turn)
        stopped = False
        if real:
            try:
                await self._interrupt(turn)
                stopped = True
            except Exception as exc:
                logger.warning("progress widget /stop failed, asking the session instead: %s", exc)
                turn.stopping = 2
        if not stopped:
            try:
                stopped = bool(self.ctx.inject_message(_STOP_PROMPT, role="user", session_key=turn.key))
            except Exception as exc:
                logger.warning("progress widget stop request not injected: %s", exc)
            if stopped and real and not turn.over:
                await self._push(turn)  # the card now says "Asked Hermes to stop"
        if not stopped:
            self._stop_hint(turn)
        elif not turn.over:
            self._cancel(turn, "timer")
            turn.timer = self.runtime.later(STOP_GRACE, self._stop_hint, turn)

    def _stop_hint(self, turn: Turn) -> None:
        """Stop did not end the turn: the card says to type /stop."""
        turn.timer = None
        if not turn.over and not turn.hint:
            turn.hint = True
            self.runtime.spawn(self._push(turn))
