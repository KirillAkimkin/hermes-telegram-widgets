"""Hard stop for destructive Gmail commands the agent runs itself (owner decision, 2026-11-10).

The widgets never delete mail, but the agent's shell can call ``gog gmail …``. This guard vetoes, through
Hermes's ``pre_tool_call`` hook, any shell or code tool call that would trash, archive, batch-change,
relabel, filter or delete Gmail messages. One narrow case passes: moving at most MAX_IDS explicitly named
message ids to the trash right after the user's own message asked to delete. Bulk changes (batch, a search
query, filters, label deletion) never pass. Best effort by design: it reads the command text, so code that
calls the Gmail API without ``gog`` is not caught; the prompt rules and the user remain the other layers.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

GUARDED_TOOLS = {"terminal", "execute_code", "process_manage"}
MAX_IDS = 5
_GOG = re.compile(r"\bgog(?:cli)?\b", re.I)
# The Gmail command word itself, not part of an address or a domain (owner@example.com, mail.google.com).
_MAIL = re.compile(r"(?<![\w@.-])(?:gmail|mail|email)(?![\w@.-])", re.I)
# Command tokens: shell words and the items of a Python/JSON list (['gog', 'gmail', 'trash', …]).
_TOKEN = re.compile(r"[^\s'\",\[\]()]+")
_DESTRUCTIVE = {"trash", "batch", "archive", "delete", "modify", "filter", "filters", "untrash"}
_READ_ONLY = {"search", "list", "get", "show", "read", "view", "url", "history", "count"}
_LOOKAHEAD = 3  # subcommand tokens checked after the Gmail word ("labels delete", "thread modify", "-a x trash")
_BULK = re.compile(r"\bbatch\b|\bfilters?\b|\blabels?\W+delete\b|--query\b|--q\b|(?<![\w-])-q\b|\bsearch\b", re.I)
_MESSAGE_ID = re.compile(r"\b[0-9a-f]{16}\b")
_ASKED_TO_DELETE = re.compile(r"(?i)(удал|стер|сотри|в\s+корзин|delete|remove|trash|erase)")

BLOCKED = (
    "Blocked by the owner's mail safety rule: the agent may not delete, trash, archive, relabel, filter or "
    "batch-change Gmail messages. Only moving at most {n} specific messages (by message id) to the trash is "
    "allowed, right after the user explicitly asked to delete them. Tell the user what you wanted to do and "
    "let them do it in Gmail; do not retry another way."
)


def _command_text(args: Any) -> str:
    if isinstance(args, dict):
        parts = [args.get(k) for k in ("command", "code", "cmd", "input", "script")]
        text = " ".join(p if isinstance(p, str) else json.dumps(p) for p in parts if p)
        return text or json.dumps(args, ensure_ascii=False)
    return str(args or "")


def gmail_verbs(command: str) -> set[str]:
    """Destructive verbs used as a Gmail subcommand: among the first command words after each ``gmail``,
    flags skipped, up to the first read-only verb or the end of that command (``|``, ``;``, ``&``). Search
    queries (``in:trash``, ``label:archive``) and other gog services (calendar, tasks, drive) never count."""
    if not _GOG.search(command):
        return set()
    verbs: set[str] = set()
    for mail in _MAIL.finditer(command):
        seen = 0
        for token in _TOKEN.finditer(command, mail.end()):
            word = token.group().lower()
            if any(stop in word for stop in "|;&") or seen == _LOOKAHEAD:
                break
            if word.startswith("-"):
                continue
            if word in _DESTRUCTIVE:
                verbs.add(word)
            elif word in _READ_ONLY:
                break
            seen += 1
    return verbs


def verdict(command: str, last_user_message: str) -> str | None:
    """None when the command may run; otherwise the block message."""
    verbs = gmail_verbs(command)
    if not verbs:
        return None
    narrow_trash = (verbs == {"trash"} and not _BULK.search(command)
                    and 1 <= len(set(_MESSAGE_ID.findall(command))) <= MAX_IDS)
    if narrow_trash and _ASKED_TO_DELETE.search(last_user_message):
        return None
    return BLOCKED.format(n=MAX_IDS)


class MailGuard:
    """Remembers each session's latest message from the user and vetoes destructive mail commands."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}
        self._lock = threading.Lock()

    def on_pre_llm_call(self, session_id: Any = "", user_message: Any = "", **_: Any) -> None:
        text = str(user_message or "")
        # Plugin-injected prompts ("[Inbox widget] …") are not the user, and they carry untrusted email text:
        # once one arrives, the user's earlier request to delete no longer counts.
        remembered = "" if text.lstrip().startswith("[") else text[:2000]
        with self._lock:
            self._last[str(session_id)] = remembered
            while len(self._last) > 200:
                self._last.pop(next(iter(self._last)))

    def on_pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: Any = "", **_: Any) -> dict | None:
        try:
            if tool_name not in GUARDED_TOOLS:
                return None
            with self._lock:
                last = self._last.get(str(session_id), "")
            message = verdict(_command_text(args), last)
            return {"action": "block", "message": message} if message else None
        except Exception:  # never break an unrelated tool call
            return None
