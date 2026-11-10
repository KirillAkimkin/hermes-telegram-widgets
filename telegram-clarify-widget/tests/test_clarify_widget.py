import asyncio
import importlib.util
import json
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tqw_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)
from tqw_under_test import clarify_widget as cq  # noqa: E402
from widget_dom import run_widget  # noqa: E402

CHAT, TG_MSG, CID, SESSION = 1000001, 7100, "a1b2c3d4e5", "agent:main:telegram:dm:1000001"
QUESTION = "В каком календаре создать событие?\nОтветьте «skip», чтобы пропустить этот вопрос."
CHOICES = ["Личный: owner@example.com (Recommended)", "Рабочий: work@example.org"]
SKIPPED = "\x00skipped"


class Result:
    def __init__(self, success, message_id=None, error=None):
        self.success, self.message_id, self.error = success, message_id, error


class Registry:
    def __init__(self):
        self.entries, self.resolved, self.awaiting = {}, [], []
        self.skipped = SKIPPED

    def get_pending(self, clarify_id):
        return self.entries.get(clarify_id)

    def resolve(self, clarify_id, response):
        if clarify_id not in self.entries:
            return False
        self.resolved.append((clarify_id, response))
        del self.entries[clarify_id]
        return True

    def mark_awaiting_text(self, clarify_id):
        self.awaiting.append(clarify_id)
        return True


class Bot:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def do_api_request(self, method, api_kwargs=None, return_type=None):
        if self.fail and method == "sendWidget":
            raise RuntimeError("Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE")
        self.calls.append((method, api_kwargs))
        return {"message_id": TG_MSG} if method == "sendWidget" else True


class Adapter:
    def __init__(self, bot):
        self._bot = bot
        self.stock = []

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.stock.append((chat_id, question, choices))
        return Result(True, "stock")

    def _metadata_thread_id(self, metadata):
        return (metadata or {}).get("thread_id")


def make(fail=False, lang="en"):
    registry = Registry()
    widgets = cq.ClarifyWidgets(registry, language=lambda: lang)
    adapter = Adapter(Bot(fail))
    assert widgets.install(adapter, Result, None)
    return widgets, adapter, registry


def ask(adapter, registry, chat_id=CHAT, choices=CHOICES, clarify_id=CID, metadata=None):
    registry.entries[clarify_id] = SimpleNamespace(session_key=SESSION, choices=list(choices) if choices else None)
    return asyncio.run(adapter.send_clarify(chat_id, QUESTION, choices, clarify_id, SESSION, metadata))


def tap(widgets, data, *, user=CHAT, chat=CHAT, message=TG_MSG):
    """A widget_query update as python-telegram-bot 22.8 delivers it: the field sits in api_kwargs."""
    query = {"id": "8123456789", "from": {"id": user}, "chat": {"id": chat, "type": "private"},
             "message_id": message, "chat_instance": "1", "type": {"type": "data"}, "data": data}
    asyncio.run(widgets.on_update(SimpleNamespace(api_kwargs={"widget_query": query}), None))


def answers(adapter):
    return [json.loads(api["data"]) if "data" in api else None
            for m, api in adapter._bot.calls if m == "answerWidgetQuery"]


def last(adapter, method):
    return [api for m, api in adapter._bot.calls if m == method][-1]


def test_private_choice_question_becomes_widget_with_actions_inside_the_card():
    widgets, adapter, registry = make()
    result = ask(adapter, registry, metadata={"thread_id": "12"})
    assert result.success and result.message_id == str(TG_MSG) and adapter.stock == []
    method, api = adapter._bot.calls[0]
    assert method == "sendWidget" and api["message_thread_id"] == 12
    assert api["allow_queries"] is True and "reply_markup" not in api and "caption" not in api
    assert "«skip»" not in api["html"] and ">recommended<" in api["html"] and "(Recommended)" not in api["html"]
    assert api["html"].count('class="oi"') == 2 and ">Other<" in api["html"] and ">Skip<" in api["html"]


def test_russian_card_when_hermes_speaks_russian():
    widgets, adapter, registry = make(lang="ru")
    ask(adapter, registry)
    api = adapter._bot.calls[0][1]
    assert "❓ Вопрос" in api["html"] and ">рекомендую<" in api["html"] and "Жду ответ" in api["html"]
    assert ">Свой ответ<" in api["html"] and ">Пропустить<" in api["html"]
    tap(widgets, "o:0")
    tap(widgets, "o:1")
    assert answers(adapter)[-1] == {"m": "Этот вопрос уже закрыт"}


def test_group_chats_keep_the_stock_card():
    widgets, adapter, registry = make()
    assert ask(adapter, registry, chat_id=-1001234).message_id == "stock"
    assert adapter._bot.calls == []


def test_open_question_has_skip_only_and_closes_when_answered_in_chat():
    widgets, adapter, registry = make()
    assert ask(adapter, registry, choices=None).message_id == str(TG_MSG)
    api = adapter._bot.calls[0][1]
    assert 'class="oi"' not in api["html"] and "Type your answer in the chat" in api["html"]
    # No ForceReply or other reply_markup: the next chat message answers; the card offers Skip only.
    assert "reply_markup" not in api and 'id="other"' not in api["html"] and 'id="skip"' in api["html"]
    widgets.on_human_input_resolved(kind="clarify", session_key=SESSION, outcome="submitted")
    card = widgets.cards[CID]
    assert card["status"] == "answered"
    asyncio.run(widgets._push(card))
    assert json.loads(last(adapter, "editMessageWidgetData")["chat_data"])["s"] == "answered"
    assert not [m for m, _ in adapter._bot.calls if m == "editMessageReplyMarkup"]


def test_model_inline_code_renders_as_code_but_stays_escaped():
    widgets, adapter, registry = make()
    registry.entries[CID] = SimpleNamespace(session_key=SESSION, choices=None)
    asyncio.run(adapter.send_clarify(CHAT, "Где: `work@example.org` или `<x>`?", None, CID, SESSION))
    markup = adapter._bot.calls[0][1]["html"]
    assert "<code>work@example.org</code>" in markup and "<code>&lt;x&gt;</code>" in markup


def test_widget_send_failure_falls_back_to_stock_card():
    widgets, adapter, registry = make(fail=True)
    assert ask(adapter, registry).message_id == "stock"


def test_option_tap_resolves_clarify_with_the_exact_offered_choice():
    widgets, adapter, registry = make()
    ask(adapter, registry)
    tap(widgets, "o:0")
    assert registry.resolved == [(CID, CHOICES[0])]
    assert answers(adapter) == [{"d": 1}]
    assert json.loads(last(adapter, "editMessageWidgetData")["chat_data"])["i"] == 0
    assert not [m for m, _ in adapter._bot.calls if m == "editMessageReplyMarkup"]
    tap(widgets, "o:1")
    assert answers(adapter)[-1] == {"m": "This question is already closed"}
    assert len(registry.resolved) == 1


def test_skip_and_other_use_hermes_semantics():
    widgets, adapter, registry = make()
    ask(adapter, registry)
    tap(widgets, "other")
    assert registry.awaiting == [CID] and registry.resolved == []
    assert json.loads(last(adapter, "editMessageWidgetData")["chat_data"])["s"] == "typing"
    tap(widgets, "skip")
    assert registry.resolved == [(CID, SKIPPED)]
    assert json.loads(last(adapter, "editMessageWidgetData")["chat_data"])["s"] == "skipped"
    assert answers(adapter) == [None, {"d": 1}]


@pytest.mark.parametrize("user,data", [(1, "o:0"), (CHAT, "tqw:a1b2c3d4e5:0"), (CHAT, "o:123"), (CHAT, "delete")])
def test_foreign_or_malformed_tap_resolves_nothing(user, data):
    widgets, adapter, registry = make()
    ask(adapter, registry)
    tap(widgets, data, user=user)
    assert answers(adapter) == [{"m": "Not allowed"}]
    assert registry.resolved == [] and registry.awaiting == [] and widgets.cards[CID]["status"] == "pending"


def test_option_outside_the_offered_choices_is_refused():
    widgets, adapter, registry = make()
    ask(adapter, registry)
    tap(widgets, "o:7")
    assert answers(adapter) == [{"m": "No such option"}] and registry.resolved == []


@pytest.mark.parametrize("where", [{"chat": 2}, {"message": 3}])
def test_taps_on_other_widgets_are_left_alone(where):
    widgets, adapter, registry = make()
    ask(adapter, registry)
    tap(widgets, "o:0", **where)  # e.g. an inbox widget in the same chat
    asyncio.run(widgets.on_update(SimpleNamespace(api_kwargs={}, callback_query=None), None))
    assert len(adapter._bot.calls) == 1 and registry.resolved == []


def test_expired_question_tap_closes_card_without_resolving():
    widgets, adapter, registry = make()
    ask(adapter, registry)
    registry.entries.clear()  # Hermes timed out or /new cleared it
    tap(widgets, "o:0")
    assert answers(adapter) == [{"m": "This question is already closed"}]
    assert json.loads(last(adapter, "editMessageWidgetData")["chat_data"])["s"] == "expired"


def test_enable_widget_queries_extends_the_polling_list_once():
    class Update:
        ALL_TYPES = ["message", "callback_query"]

    assert cq.enable_widget_queries(Update) and cq.enable_widget_queries(Update)
    assert Update.ALL_TYPES == ["message", "callback_query", "widget_query"]
    assert not cq.enable_widget_queries(type("Old", (), {"ALL_TYPES": ("message",)}))


def test_card_controls_come_only_from_chat_data():
    """Pending draws the tappable options with Other/Skip; a closed state turns them off via chat_data only."""
    widgets, adapter, registry = make()
    ask(adapter, registry)
    api = adapter._bot.calls[0][1]
    tap(widgets, "o:1")
    pushed = [a for m, a in adapter._bot.calls[1:] if m not in {"answerWidgetQuery", "editMessageWidgetData"}]
    assert pushed == []  # no new markup: the same HTML redraws itself from chat_data
    closing = last(adapter, "editMessageWidgetData")["chat_data"]
    data = json.loads(closing)
    assert data["s"] == "answered" and data["i"] == 1 and isinstance(data["t"], int)
    run = run_widget(api["html"], api.get("chat_data"), look=[".oi", "#z", "#st"], answers=[{"d": 1}],
                     steps=[{"click": ".oi", "index": 1}, {"chat": closing}, {"click": ".oi", "index": 0},
                            {"click": "#skip"}])
    assert run["requests"] == ["o:1"]  # once closed, no option and no Skip sends anything
    pending, tapped, answered, _, _ = run["frames"]
    assert pending["#z"][0]["shown"] and not any(o["disabled"] for o in pending[".oi"])
    assert tapped[".oi"][1]["disabled"]
    assert not answered["#z"][0]["shown"] and all(o["disabled"] for o in answered[".oi"])
    assert [o["cls"] for o in answered[".oi"]] == ["oi dim", "oi on"] and answered[".oi"][1]["text"].startswith("✓")
    assert answered["#st"][0]["text"].startswith("Answered at ")


@pytest.mark.parametrize("outcome,status", [("submitted", "answered"), ("timed_out", "expired"), ("cancelled", "cancelled")])
def test_typed_answer_or_timeout_closes_waiting_cards_of_that_session(outcome, status):
    widgets, adapter, registry = make()
    ask(adapter, registry)
    widgets.on_human_input_resolved(kind="clarify", session_key=SESSION, outcome=outcome)
    widgets.on_human_input_resolved(kind="clarify", session_key="other-session", outcome="submitted")
    assert widgets.cards[CID]["status"] == status


def test_reinstall_on_registry_rebuild_does_not_stack_wrappers():
    widgets, adapter, registry = make()
    assert widgets.install(adapter, Result, None)
    ask(adapter, registry)
    assert len([m for m, _ in adapter._bot.calls if m == "sendWidget"]) == 1


def test_largest_clarify_question_fits_or_falls_back():
    widgets, adapter, registry = make()
    choices = [f"Вариант {i}: " + "Ж" * 40 for i in range(4)]  # 429 px in Chromium at 360 px wide
    ask(adapter, registry, choices=choices)
    raw = adapter._bot.calls[0][1]["html"].encode()
    assert len(zlib.compress(raw, 9)) <= 4096 and adapter.stock == []


def test_card_taller_than_the_bubble_falls_back_to_the_stock_card():
    widgets, adapter, registry = make()
    choices = [f"Вариант {i}: " + "Ж" * 120 for i in range(4)]  # 685 px in Chromium at 360 px wide
    assert ask(adapter, registry, choices=choices).message_id == "stock"
    assert adapter._bot.calls == [] and CID not in widgets.cards


def test_question_text_is_escaped():
    widgets, adapter, registry = make()
    registry.entries[CID] = SimpleNamespace(session_key=SESSION, choices=["<b>x</b>", "y"])
    asyncio.run(adapter.send_clarify(CHAT, "<script>alert(1)</script>", ["<b>x</b>", "y"], CID, SESSION))
    markup = adapter._bot.calls[0][1]["html"]
    assert "&lt;script&gt;" in markup and "&lt;b&gt;x&lt;/b&gt;" in markup
