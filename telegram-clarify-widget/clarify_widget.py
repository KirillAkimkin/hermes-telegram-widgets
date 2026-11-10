"""Hermes ``clarify`` questions as Telegram HTML widgets (Bot API 10.4 ``sendWidget``).

Hermes asks through ``adapter.send_clarify``; the stock Telegram card is plain text with numeric
buttons. In a private chat this plugin draws the question and its options as a widget whose options
are tappable, with Other and Skip buttons inside the card. Taps arrive as widget requests
(``widget_query``) and resolve through Hermes's own clarify registry (``tools.clarify_gateway``), so
typed answers, «skip», timeouts and /new keep working unchanged. Anything the widget cannot show
(group chat, oversize, send error) falls back to the stock card.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import threading
import time
import zlib
from typing import Any, Callable

logger = logging.getLogger(__name__)

_DATA_RE = re.compile(r"^(?:o:(\d{1,2})|other|skip)$")  # widget request data: o:k = option k
MAX_HTML_BYTES = 32768
MAX_COMPRESSED_BYTES = 4096
MAX_HEIGHT = 600  # px: Telegram's widget bubble shows no more
MAX_CARDS = 200
# Hermes appends a typed-"skip" hint to the question; the widget offers a Skip button instead.
_SKIP_HINT = re.compile(r"\n[^\n]*(?:«skip»|\"skip\"|'skip')[^\n]*\s*$")
_RECOMMENDED = re.compile(r"\s*\((?:recommended|рекомендуется)\)\s*$", re.IGNORECASE)

_CSS = (
    "body{margin:0}"
    ".c{padding:12px 14px 10px}"
    ".h{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}"
    ".t{font-weight:600;font-size:1.05em}"
    ".p{font-size:.78em;font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap;"
    "background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-accent-text-color)}"
    ".p.ok{color:#fff;background:#2fa84f}"
    ".p.off{color:var(--tg-theme-hint-color)}"
    ".q{font-weight:600;white-space:pre-wrap;overflow-wrap:anywhere;margin-bottom:10px;"
    "user-select:text;-webkit-user-select:text}"
    ".o{display:grid;gap:6px}"
    ".oi{display:flex;gap:10px;align-items:baseline;width:100%;box-sizing:border-box;margin:0;border:0;"
    "padding:8px 10px;border-radius:10px;text-align:left;font:inherit;font-size:.93em;cursor:pointer;"
    "color:var(--tg-theme-text-color);background:var(--tg-theme-secondary-bg-color);transition:color .2s,transform .12s}"
    ".oi[disabled]{cursor:default}"
    ".oi:active,.bt:active{transform:scale(.98)}"
    ".x{flex:0 0 auto;font-weight:700;color:var(--tg-theme-accent-text-color);"
    "font-variant-numeric:tabular-nums}"
    ".l{min-width:0;overflow-wrap:anywhere}"
    ".r{flex:0 0 auto;margin-left:auto;font-size:.75em;font-weight:600;color:var(--tg-theme-accent-text-color)}"
    ".oi.on .x{color:#2fa84f}"
    ".oi.dim{color:var(--tg-theme-hint-color)}"
    ".oi.dim .x,.oi.dim .r{color:var(--tg-theme-hint-color)}"
    ".z{display:flex;gap:8px;margin-top:10px}"
    ".bt{flex:1;border:0;border-radius:10px;padding:10px 12px;font:inherit;font-size:.93em;font-weight:600;"
    "background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color);transition:transform .12s}"
    ".bt[disabled],.oi.q{opacity:.6}"
    ".f{margin-top:8px;font-size:.8em;color:var(--tg-theme-hint-color)}"
    "#ms{display:none}"
    "code{font-family:ui-monospace,monospace;font-size:.9em}"
    "@media (prefers-reduced-motion:reduce){.oi,.bt{transition:none}}"
)

# UI text per language. English is the default; the card keeps the language it was created in.
TEXT: dict[str, dict[str, Any]] = {
    "en": {
        "kind": "❓ Question", "recommended": "recommended", "other": "Other", "skip": "Skip",
        "hint_choices": "Tap an option or type your own answer", "hint_open": "Type your answer in the chat",
        "denied": "Not allowed", "closed": "This question is already closed", "no_option": "No such option",
        "err": "Hermes did not answer ({code}), try again", "at": "{s} at {t}",
        "status": {
            "pending": ["Waiting", "", ""],
            "typing": ["Typing", "", "Type your answer as a normal message"],
            "answered": ["Answered", "ok", "Hermes continues", 1],
            "skipped": ["Skipped", "off", "Hermes continues", 1],
            "expired": ["Closed", "off", "Time to answer ran out", 1],
            "cancelled": ["Cancelled", "off", "The task was stopped", 1],
        },
    },
    "ru": {
        "kind": "❓ Вопрос", "recommended": "рекомендую", "other": "Свой ответ", "skip": "Пропустить",
        "hint_choices": "Выберите вариант или напишите свой ответ", "hint_open": "Напишите ответ в чат",
        "denied": "Недоступно", "closed": "Этот вопрос уже закрыт", "no_option": "Такого варианта нет",
        "err": "Hermes не ответил ({code}), попробуйте ещё раз", "at": "{s} в {t}",
        "status": {
            "pending": ["Жду ответ", "", ""],
            "typing": ["Пишете ответ", "", "Напишите ответ обычным сообщением"],
            "answered": ["Ответ получен", "ok", "Hermes продолжает работу", 1],
            "skipped": ["Пропущено", "off", "Hermes продолжает работу", 1],
            "expired": ["Закрыт", "off", "Время на ответ вышло", 1],
            "cancelled": ["Отменён", "off", "Задача остановлена", 1],
        },
    },
}


def current_language() -> str:
    """Hermes's UI language (``HERMES_LANGUAGE`` > ``display.language`` > en), reduced to en/ru."""
    try:
        from agent.i18n import get_language
        return "ru" if str(get_language()).lower().startswith("ru") else "en"
    except Exception:
        return "en"


def tr(card: dict[str, Any], key: str) -> Any:
    return TEXT.get(card.get("lang"), TEXT["en"])[key]


def _script(card: dict[str, Any]) -> str:
    """Options, Other and Skip send one widget request per tap (``o:k`` / ``other`` / ``skip``); the bot's
    ``{"m": text}`` answer or the error code shows in #ms, ``"d": 1`` keeps the tapped control off until the
    closing chat_data arrives. A closed card disables every control and hides Other/Skip. The action time
    goes into the pill ("Answered at 17:26"), never as a bare time; an empty note hides the footer."""
    return (
        "(function(){var W=window.TelegramWidget,S=" + json.dumps(tr(card, "status"), ensure_ascii=False)
        + ",A=" + json.dumps(tr(card, "at"), ensure_ascii=False) + ",E=" + json.dumps(tr(card, "err"), ensure_ascii=False)
        + ",$=function(i){return document.getElementById(i)},o=document.querySelectorAll(\".oi\");"
        "function say(t){var m=$(\"ms\");m.textContent=t||\"\";m.style.display=t?\"block\":\"none\"}"
        "function done(el){el.disabled=true;el.dataset.done=\"1\";el.classList.add(\"q\")}"
        "function ask(d,el){if(!W||!W.request||el.disabled)return;el.disabled=true;el.classList.add(\"q\");say(\"\");"
        "W.request(d).then(function(a){var r=null;try{r=JSON.parse(a.result)}catch(e){}say(r&&r.m);"
        "if(r&&r.d)done(el)},function(e){say(E.replace(\"{code}\",(e&&e.code)||\"FAILED\"))})"
        ".then(function(){if(!el.dataset.done){el.disabled=false;el.classList.remove(\"q\")}})}"
        "function r(){var d=null;try{d=JSON.parse(W&&W.chatData)}catch(e){}d=d||{s:\"pending\"};"
        "var v=S[d.s]||S.pending,p=$(\"st\"),t=$(\"ft\"),open=d.s===\"pending\"||d.s===\"typing\","
        "l=v[0],f=v[2]||(d.s===\"pending\"?t.dataset.p:\"\"),k;"
        "for(k=0;k<o.length;k++){var on=d.i===k;if(!open){o[k].className=\"oi \"+(on?\"on\":\"dim\");"
        "o[k].disabled=true;o[k].dataset.done=\"1\"}o[k].querySelector(\".x\").textContent=on?\"✓\":String(k+1)}"
        "$(\"z\").style.display=open?\"\":\"none\";"
        "if(d.t&&v[3]){try{l=A.replace(\"{s}\",l).replace(\"{t}\",new Date(d.t*1e3).toLocaleTimeString(navigator.language,"
        "{hour:\"2-digit\",minute:\"2-digit\",hourCycle:\"h23\"}))}catch(e){}}"
        "p.textContent=l;p.className=\"p \"+v[1];t.textContent=f;t.style.display=f?\"\":\"none\"}"
        "o.forEach(function(el,k){el.addEventListener(\"click\",function(){ask(\"o:\"+k,el)})});"
        "[\"other\",\"skip\"].forEach(function(d){var b=$(d);if(b)b.addEventListener(\"click\",function(){ask(d,b)})});"
        "if(W)W.onEvent(\"chatDataChanged\",r);r()})();"
    )


OPEN = {"pending", "typing"}


class ClarifyRegistry:
    """Thin view of Hermes's ``tools.clarify_gateway`` (imported lazily: it lives in the gateway)."""

    @staticmethod
    def _mod():
        from tools import clarify_gateway
        return clarify_gateway

    def get_pending(self, clarify_id: str):
        return self._mod().get_pending(clarify_id)

    def resolve(self, clarify_id: str, response: str) -> bool:
        return self._mod().resolve_gateway_clarify(clarify_id, response)

    def mark_awaiting_text(self, clarify_id: str) -> bool:
        return self._mod().mark_awaiting_text(clarify_id)

    @property
    def skipped(self) -> str:
        return self._mod().SKIPPED


def clean_question(question: str) -> str:
    return _SKIP_HINT.sub("", str(question or "")).strip()


def split_choice(choice: str) -> tuple[str, bool]:
    """(label without the recommendation marker, recommended?)"""
    text = str(choice).strip()
    label = _RECOMMENDED.sub("", text)
    return label, label != text


def _rich(text: str) -> str:
    """Escape, then turn the model's `inline code` into <code> (emails, paths, ids)."""
    return re.sub(r"`([^`\n]{1,200})`", r"<code>\1</code>", html.escape(str(text), quote=True))


def render_html(card: dict[str, Any]) -> str:
    mark = f'<span class="r">{tr(card, "recommended")}</span>'
    items = "".join(
        f'<button class="oi"><span class="x">{i + 1}</span><span class="l">{_rich(label)}</span>'
        + (mark if recommended else "") + "</button>"
        for i, (label, recommended) in enumerate(map(split_choice, card["choices"]))
    )
    options = f'<div class="o">{items}</div>' if items else ""
    # An open question is answered by the next chat message (widgets have no text input): Skip only.
    other = f'<button class="bt" id="other">{tr(card, "other")}</button>' if items else ""
    hint = html.escape(tr(card, "hint_choices") if items else tr(card, "hint_open"), quote=True)
    return (
        f"<style>{_CSS}</style><div class=\"c\"><div class=\"h\"><span class=\"t\">{tr(card, 'kind')}</span>"
        f"<span id=\"st\" class=\"p\">{tr(card, 'status')['pending'][0]}</span></div>"
        f"<div class=\"q\">{_rich(card['question'])}</div>{options}"
        f"<div id=\"z\" class=\"z\">{other}<button class=\"bt\" id=\"skip\">{tr(card, 'skip')}</button></div>"
        f"<div id=\"ft\" class=\"f\" data-p=\"{hint}\">{hint}</div><div id=\"ms\" class=\"f\"></div>"
        f"</div><script>{_script(card)}</script>"
    )


def widget_fits(markup: str) -> bool:
    raw = markup.encode("utf-8")
    return len(raw) <= MAX_HTML_BYTES and len(zlib.compress(raw, 9)) <= MAX_COMPRESSED_BYTES


def _lines(text: str, per_line: int) -> int:
    return sum(max(1, -(-len(part) // per_line)) for part in str(text).split("\n"))


def estimated_height(card: dict[str, Any]) -> int:
    """Rough px height of the card in a phone-wide bubble, erring tall (wide Cyrillic letters, narrow
    phones): padding, header, question, option rows, the Other/Skip row and the footer hint."""
    labels = [split_choice(c)[0] for c in card["choices"]]
    hint = tr(card, "hint_choices") if labels else tr(card, "hint_open")
    return (22 + 32 + 19 * _lines(card["question"], 32) + 10 + 48 + 8 + 16 * _lines(hint, 44)
            + sum(16 + 17 * _lines(label, 24) for label in labels) + 6 * max(len(labels) - 1, 0))


def chat_data(card: dict[str, Any]) -> str:
    data: dict[str, Any] = {"s": card["status"]}
    if card.get("choice") is not None:
        data["i"] = card["choice"]
    if card["status"] not in OPEN:
        data["t"] = int(card.get("closed_at") or time.time())
    return json.dumps(data)


def _is_private(chat_id: Any) -> bool:
    try:
        return int(str(chat_id)) > 0  # Telegram user ids are positive; groups/channels are negative
    except ValueError:
        return False


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


_OUTCOME_STATUS = {"submitted": "answered", "cancelled": "cancelled"}


class ClarifyWidgets:
    def __init__(self, registry: Any = None, result_type: Callable[..., Any] | None = None,
                 language: Callable[[], str] = current_language):
        self.language = language
        self.registry = registry or ClarifyRegistry()
        self.result_type = result_type
        self.adapter: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.original: Callable[..., Any] | None = None
        self.cards: dict[str, dict[str, Any]] = {}  # clarify_id → card; clarify entries never outlive the process
        self._lock = threading.RLock()

    # --- wiring ------------------------------------------------------------------------------
    def install(self, adapter: Any, result_type: Callable[..., Any], loop: asyncio.AbstractEventLoop | None) -> bool:
        """Wrap ``adapter.send_clarify``. Refuses (stock card stays) when the adapter shape is unknown."""
        original = getattr(adapter, "send_clarify", None)
        if not callable(original) or not hasattr(adapter, "_bot"):
            logger.warning("telegram-clarify-widget: adapter has no send_clarify/_bot; stock clarify cards kept")
            return False
        if getattr(original, "__tqw__", False):  # registry rebuild re-wires the same adapter
            original = original.__tqw_original__
        self.adapter, self.result_type, self.loop, self.original = adapter, result_type, loop, original

        async def send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=None):
            return await self.send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)

        send_clarify.__tqw__ = True
        send_clarify.__tqw_original__ = original
        adapter.send_clarify = send_clarify
        return True

    async def _stock(self, *args) -> Any:
        return await self.original(*args)

    # --- send --------------------------------------------------------------------------------
    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        args = (chat_id, question, choices, clarify_id, session_key, metadata)
        self.loop = asyncio.get_running_loop()  # the hook fires from the agent thread and needs this loop
        if not _is_private(chat_id) or getattr(self.adapter, "_bot", None) is None:
            return await self._stock(*args)
        card = {"id": str(clarify_id), "chat_id": str(chat_id), "session_key": session_key or "",
                "question": clean_question(question), "choices": [str(c) for c in choices or []],
                "status": "pending", "choice": None, "tg_message_id": "", "lang": self.language()}
        markup = render_html(card)
        if not widget_fits(markup) or estimated_height(card) > MAX_HEIGHT:
            return await self._stock(*args)
        # No caption: it would repeat the question inside the bubble (owner decision 2026-11-10).
        # No reply_markup: the card draws its own controls and taps come back as widget requests.
        api: dict[str, Any] = {"chat_id": int(card["chat_id"]), "html": markup, "chat_data": chat_data(card),
                               "allow_queries": True}
        thread_of = getattr(self.adapter, "_metadata_thread_id", None)
        thread_id = thread_of(metadata) if callable(thread_of) else None
        if thread_id and str(thread_id).isdigit():
            api["message_thread_id"] = int(thread_id)
        try:
            message = await self.adapter._bot.do_api_request("sendWidget", api_kwargs=api)
        except Exception as exc:
            logger.warning("clarify widget send failed, using stock card: %s", exc)
            return await self._stock(*args)
        card["tg_message_id"] = str(message["message_id"] if isinstance(message, dict) else message.message_id)
        with self._lock:
            self.cards[card["id"]] = card
            while len(self.cards) > MAX_CARDS:
                self.cards.pop(next(iter(self.cards)))
        return self.result_type(success=True, message_id=card["tg_message_id"])

    # --- taps --------------------------------------------------------------------------------
    async def _answer(self, query_id: str, text: str = "", *, done: bool = False) -> None:
        """{"m": text} shows under the card; "d": 1 keeps the tapped control off until chat_data closes it."""
        payload = {**({"m": text} if text else {}), **({"d": 1} if done else {})}
        api = {"widget_query_id": query_id, **({"data": json.dumps(payload, ensure_ascii=False)} if payload else {})}
        try:
            await self.adapter._bot.do_api_request("answerWidgetQuery", api_kwargs=api)
        except Exception as exc:
            logger.warning("answerWidgetQuery failed: %s", exc)

    async def on_update(self, update: Any, context: Any) -> None:
        """Taps from clarify widgets. Telegram authenticates ``from``; the chat and message pin the card.
        The registry calls are in-memory, so the answer still goes out well inside the 15 s limit."""
        del context
        query = (getattr(update, "api_kwargs", None) or {}).get("widget_query")
        if not isinstance(query, dict) or not query.get("id"):
            return
        chat_id = str((query.get("chat") or {}).get("id", ""))
        user_id = str((query.get("from") or {}).get("id", ""))
        message_id = str(query.get("message_id", ""))
        with self._lock:
            card = next((c for c in self.cards.values()
                         if c["chat_id"] == chat_id and c["tg_message_id"] == message_id), None)
        if card is None:
            return  # not one of ours: another plugin's widget may answer it
        match = _DATA_RE.fullmatch(str(query.get("data", "")))
        if user_id != card["chat_id"] or not match:  # private chat: the chat id is the owner's user id
            await self._answer(query["id"], tr(card, "denied"))
            return
        clarify_id = card["id"]
        entry = self.registry.get_pending(clarify_id)
        if entry is None or getattr(entry, "session_key", None) != card["session_key"] or card["status"] not in OPEN:
            await self._answer(query["id"], tr(card, "closed"))
            if card["status"] in OPEN:
                await self._push(self._close(card, "expired"))
            return
        if match.group(0) == "other":
            self.registry.mark_awaiting_text(clarify_id)
            card = self._update(card, status="typing")
            await self._answer(query["id"])
            await self._push(card)
            return
        if match.group(0) == "skip":
            response, status, index = self.registry.skipped, "skipped", None
        else:
            index = int(match.group(1))
            offered = list(getattr(entry, "choices", None) or [])
            if index >= len(offered):
                await self._answer(query["id"], tr(card, "no_option"))
                return
            response, status = offered[index], "answered"
        if not self.registry.resolve(clarify_id, response):
            card = self._close(card, "expired")
            await self._answer(query["id"], tr(card, "closed"))
            await self._push(card)
            return
        card = self._close(card, status, choice=index)
        await self._answer(query["id"], done=True)
        await self._push(card)

    # --- typed answers, timeouts, /stop ------------------------------------------------------
    def on_human_input_resolved(self, **payload: Any) -> None:
        """Hermes fires this once per clarify batch; close this session's cards still waiting."""
        if payload.get("kind") != "clarify" or not payload.get("session_key"):
            return
        status = _OUTCOME_STATUS.get(str(payload.get("outcome") or ""), "expired")
        with self._lock:
            waiting = [c for c in self.cards.values()
                       if c["session_key"] == payload["session_key"] and c["status"] in OPEN]
        for card in waiting:
            closed = self._close(card, status)
            if self.loop is not None and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._push(closed), self.loop)

    # --- state -------------------------------------------------------------------------------
    def _update(self, card: dict[str, Any], **changes: Any) -> dict[str, Any]:
        with self._lock:
            card = {**self.cards.get(card["id"], card), **changes}
            self.cards[card["id"]] = card
        return card

    def _close(self, card: dict[str, Any], status: str, choice: int | None = None) -> dict[str, Any]:
        return self._update(card, status=status, choice=choice, closed_at=int(time.time()))

    async def _push(self, card: dict[str, Any]) -> None:
        api = {"chat_id": int(card["chat_id"]), "message_id": int(card["tg_message_id"]), "chat_data": chat_data(card)}
        try:
            await self.adapter._bot.do_api_request("editMessageWidgetData", api_kwargs=api)
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                logger.warning("editMessageWidgetData failed for clarify card %s: %s", card["id"], exc)
