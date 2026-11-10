import asyncio
import importlib.util
import json
import sys
import types
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tpw_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)
from tpw_under_test import progress_widget as pw  # noqa: E402

CHAT, USER, START = 1000001, 1000001, 1794333300.0
SECRETS = ("https://bank.example/statement?id=991", "rm -rf /srv/data", "kirill@example.com",
           "Dear Anna, the contract", "flights to Lisbon", "TOOL RESULT 4242")


# Gateway pieces the Stop path imports at tap time (the real ones live in hermes-agent).
@dataclass
class MessageEvent:
    text: str
    message_type: str = "text"
    source: object = None
    user_id: str = None
    metadata: dict = field(default_factory=dict)


_platforms = types.ModuleType("gateway.platforms")
_event = types.ModuleType("gateway.platforms.event")
_event.MessageEvent = MessageEvent
_event.MessageType = SimpleNamespace(COMMAND="command", TEXT="text")
sys.modules.setdefault("gateway.platforms", _platforms)
sys.modules["gateway.platforms.event"] = _event


class State:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return json.loads(json.dumps(self.values.get(key, default)))

    def set(self, key, value):
        self.values[key] = json.loads(json.dumps(value))


class Ctx:
    def __init__(self, inject_ok=True):
        self.state, self.hooks, self.platform, self.injected, self.inject_ok = State(), {}, {}, [], inject_ok

    def register_hook(self, name, callback, **kw):
        self.hooks[name] = callback

    def register_platform_handler(self, platform, factory):
        self.platform[platform] = factory

    def inject_message(self, content, role="user", *, session_key=None):
        self.injected.append((content, session_key))
        return self.inject_ok


class Bot:
    def __init__(self):
        self.calls, self.next_id = [], 9000

    async def do_api_request(self, method, api_kwargs=None, return_type=None):
        self.calls.append((method, api_kwargs))
        if method == "sendWidget":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def methods(self):
        return [m for m, _ in self.calls]


class Timer:
    def __init__(self, at, fn, args):
        self.at, self.fn, self.args, self.cancelled, self.fired = at, fn, args, False, False

    def cancel(self):
        self.cancelled = True


class Runtime:
    """The gateway loop, run by hand: ``call`` executes at once, timers fire on ``advance``, spawned
    coroutines run on ``flush`` (so a test can end a turn while a send is still in flight)."""

    def __init__(self, bot):
        self.bot, self.now, self.timers, self.coros = bot, 0.0, [], []

    def call(self, fn, *args):
        fn(*args)

    def later(self, delay, fn, *args):
        timer = Timer(self.now + delay, fn, args)
        self.timers.append(timer)
        return timer

    def spawn(self, coroutine):
        self.coros.append(coroutine)

    def flush(self):
        while self.coros:
            asyncio.run(self.coros.pop(0))

    def advance(self, seconds):
        end = self.now + seconds
        while True:
            due = [t for t in self.timers if not t.cancelled and not t.fired and t.at <= end]
            if not due:
                break
            timer = min(due, key=lambda t: t.at)
            self.now, timer.fired = timer.at, True
            timer.fn(*timer.args)
            self.flush()
        self.now = end
        self.flush()


class Adapter:
    """The Telegram adapter's message path; like the gateway, a /stop fires agent_loop_stopped."""

    def __init__(self, service, fail=False):
        self.service, self.fail, self.events, self.sources = service, fail, [], []

    def build_source(self, **kw):
        self.sources.append(kw)
        return SimpleNamespace(**kw)

    async def handle_message(self, event):
        if self.fail:
            raise RuntimeError("adapter is gone")
        self.events.append(event)
        self.service.on_agent_loop_stopped(session_key=event.metadata["gateway_session_key"], reason="stop")


def session_env(**over):
    values = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_TYPE": "dm",
              "HERMES_SESSION_CHAT_ID": str(CHAT), "HERMES_SESSION_USER_ID": str(USER),
              "HERMES_SESSION_KEY": "agent:main:telegram:dm:1000001", "HERMES_SESSION_THREAD_ID": ""}
    values.update(over)
    return lambda key, default="": values.get(key, default)


def make(lang=None, adapter=True, inject_ok=True, **env):
    ctx = Ctx(inject_ok)
    plugin.get_session_env = session_env(**env)
    plugin._service = None
    plugin.register(ctx)
    service = plugin._service
    bot = Bot()
    rt = Runtime(bot)
    service.runtime = rt
    service.clock = lambda: START + rt.now
    if adapter:
        service.adapter = Adapter(service)
    if lang:
        service.language = lambda: lang
    return ctx, service, bot, rt


KEY = "agent:main:telegram:dm:1000001"


def start(ctx, message="Plan the Lisbon trip", turn="t1", **kw):
    ctx.hooks["pre_llm_call"](session_id="s1", task_id="", turn_id=turn, user_message=message,
                              conversation_history=[], is_first_turn=False, model="m", platform="telegram",
                              parent_session_id=kw.pop("parent_session_id", ""), sender_id=str(USER), **kw)


def tool(ctx, name, turn="t1", finish=True, args=None):
    args = args if args is not None else {"query": SECRETS[4], "url": SECRETS[0], "command": SECRETS[1]}
    ctx.hooks["pre_tool_call"](tool_name=name, args=args, task_id="", session_id="s1", tool_call_id="c",
                               turn_id=turn, api_request_id="", conversation_id="")
    if finish:
        done(ctx, name, turn, args)


def done(ctx, name, turn="t1", args=None):
    ctx.hooks["post_tool_call"](tool_name=name, args=args or {"to": SECRETS[2]}, result=json.dumps(
        {"text": SECRETS[3], "id": SECRETS[5]}), task_id="", session_id="s1", tool_call_id="c", turn_id=turn,
        duration_ms=10, status="ok", error_type=None, error_message=None)


def end(ctx, turn="t1", hook="post_llm_call"):
    extra = {"assistant_response": "Done"} if hook == "post_llm_call" else {"completed": True, "interrupted": False}
    ctx.hooks[hook](session_id="s1", task_id="", turn_id=turn, user_message="x", platform="telegram", **extra)


def sends(bot):
    return [api for m, api in bot.calls if m == "sendWidget"]


def data(bot, index=-1):
    raw = [api["chat_data"] for m, api in bot.calls if m in {"sendWidget", "editMessageWidgetData"}]
    return json.loads(raw[index])


def tap(service, data_="stop", *, user=USER, message=None, chat=CHAT):
    message = message if message is not None else service._turns[KEY].message_id
    query = {"id": "8123456789", "from": {"id": user}, "chat": {"id": chat, "type": "private"},
             "message_id": int(message), "chat_instance": "1", "type": {"type": "data"}, "data": data_}
    asyncio.run(service.on_update(SimpleNamespace(api_kwargs={"widget_query": query}), None))


def answers(bot):
    return [json.loads(api["data"]) for m, api in bot.calls if m == "answerWidgetQuery"]


# ---- when the card appears ---------------------------------------------------------------------------

def test_card_appears_once_after_eight_seconds_with_the_start_time():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(7.9)
    assert bot.calls == []
    rt.advance(0.2)
    (api,) = sends(bot)
    assert api["chat_id"] == CHAT and api["allow_queries"] is True
    assert "caption" not in api and "reply_markup" not in api  # Stop lives inside the card
    assert data(bot) == {"t": int(START), "s": []}
    rt.advance(60)
    assert len(sends(bot)) == 1  # the timer ticks in the widget: no more Telegram calls


def test_second_tool_call_shows_the_card_at_once():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(1)
    tool(ctx, "web_search")
    assert bot.calls == []
    tool(ctx, "read_file", finish=False)
    rt.flush()
    assert len(sends(bot)) == 1
    assert data(bot)["s"] == [["web", 1, 0], ["files", 1, 1]]
    rt.advance(10)
    assert len(sends(bot)) == 1  # the 8 s timer was cancelled


@pytest.mark.parametrize("hook", ["post_llm_call", "on_session_end"])
def test_short_turns_never_get_a_card(hook):
    ctx, service, bot, rt = make()
    start(ctx)
    tool(ctx, "web_search")
    rt.advance(5)
    end(ctx, hook=hook)
    rt.advance(30)
    assert bot.calls == [] and service._turns == {}


@pytest.mark.parametrize("env", [{"HERMES_SESSION_CHAT_TYPE": "group"}, {"HERMES_SESSION_CHAT_TYPE": "channel"},
                                 {"HERMES_SESSION_PLATFORM": "discord"}, {"HERMES_SESSION_CHAT_ID": ""},
                                 {"HERMES_SESSION_KEY": ""}])
def test_no_card_outside_private_telegram_chats(env):
    ctx, service, bot, rt = make(**env)
    ctx.hooks["pre_llm_call"](session_id="s1", turn_id="t1", user_message="hi",
                              platform=env.get("HERMES_SESSION_PLATFORM", "telegram"))
    tool(ctx, "web_search")
    tool(ctx, "web_search")
    rt.advance(20)
    assert bot.calls == []


@pytest.mark.parametrize("message", [
    "[Inbox widget] The user tapped Reply on email 18f…", "  [Task activity] The user tapped Stop",
    [{"type": "text", "text": "[Calendar widget] create"}, {"type": "image_url", "image_url": {"url": "x"}}],
])
def test_plugin_injected_turns_get_no_card(message):
    ctx, service, bot, rt = make()
    start(ctx, message=message)
    tool(ctx, "terminal")
    tool(ctx, "terminal")
    rt.advance(20)
    assert bot.calls == []


def test_multimodal_user_message_still_gets_a_card():
    ctx, service, bot, rt = make()
    start(ctx, message=[{"type": "text", "text": "What is on this photo?"}, {"type": "image_url"}])
    rt.advance(9)
    assert len(sends(bot)) == 1


def test_helper_turns_neither_start_cards_nor_touch_the_parent_card():
    ctx, service, bot, rt = make()
    start(ctx, turn="sub", parent_session_id="s0")  # delegate_task's own turn
    rt.advance(20)
    assert bot.calls == []
    start(ctx)
    tool(ctx, "delegate_task", finish=False)
    tool(ctx, "web_search", turn="sub")  # the helper's tool calls carry the helper's turn id
    tool(ctx, "web_search", turn="sub")
    end(ctx, turn="sub", hook="on_session_end")  # the helper's turn ends; the reply goes on
    rt.advance(9)
    assert data(bot)["s"] == [["helper", 1, 1]]
    assert "deleteMessage" not in bot.methods()


def test_activity_card_turns_get_no_card():
    ctx, service, bot, rt = make()
    start(ctx)
    tool(ctx, "telegram_task_activity")
    tool(ctx, "terminal")
    rt.advance(30)
    assert bot.calls == []


def test_activity_card_appearing_mid_turn_deletes_ours():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    message_id = service._turns[KEY].message_id
    tool(ctx, "telegram_task_activity", finish=False)
    rt.flush()
    assert bot.calls[-1] == ("deleteMessage", {"chat_id": CHAT, "message_id": int(message_id)})
    tool(ctx, "terminal")
    tool(ctx, "terminal")
    end(ctx)
    rt.advance(30)
    assert bot.methods() == ["sendWidget", "deleteMessage"]


# ---- steps --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,code", [
    ("web_search", "web"), ("web_extract", "web"), ("telegram_inbox_widget", "mail"),
    ("telegram_email_widget", "email"), ("telegram_calendar_widget", "event"), ("browser_navigate", "browser"),
    ("browser_click", "browser"), ("computer_use", "screen"), ("terminal", "cmd"), ("execute_code", "code"),
    ("read_file", "files"), ("search_files", "files"), ("delegate_task", "helper"), ("skill_view", "skill"),
    ("memory", "memory"), ("session_search", "memory"), ("write_file", "work"), ("mcp_github_issue", "work"),
    ("", "work"),
])
def test_tools_map_to_fixed_labels(name, code):
    assert pw.label_for(name) == code
    for lang in ("en", "ru"):
        assert code in pw.TEXT[lang]["labels"] and code in pw.TEXT[lang]["now"]


def test_done_steps_read_in_the_past_and_the_current_step_in_the_present():
    en, ru = pw.TEXT["en"], pw.TEXT["ru"]
    assert en["labels"]["web"] == "Searched the web" and en["now"]["web"] == "Searching the web"
    assert en["labels"]["cmd"] == "Ran a command" and en["now"]["cmd"] == "Running a command"
    assert en["labels"]["mail"] == "Checked mail" and en["now"]["mail"] == "Checking mail"
    assert en["labels"]["email"] == "Drafted an email" and en["now"]["email"] == "Drafting an email"
    assert en["labels"]["work"] == "Worked on it" and en["now"]["work"] == "Working"
    assert ru["labels"]["cmd"] == "Выполнил команду" and ru["now"]["cmd"] == "Выполняю команду"
    assert ru["labels"]["work"] == "Поработал" and ru["now"]["work"] == "Работаю"
    for lang in ("en", "ru"):
        assert set(pw.TEXT[lang]["labels"]) == set(pw.TEXT[lang]["now"]) == set(en["labels"])
        assert all(pw.TEXT[lang]["labels"][k] != pw.TEXT[lang]["now"][k] for k in en["labels"])
    # chat_data keeps the codes; the widget picks the form by the row's running flag
    assert "L=e[2]?X.now:X.labels" in pw.render_html("en")


def test_repeats_merge_and_the_current_step_is_marked_running():
    ctx, service, bot, rt = make()
    start(ctx)
    for _ in range(3):
        tool(ctx, "web_search")
    tool(ctx, "read_file", finish=False)
    rt.advance(2)
    assert data(bot)["s"] == [["web", 3, 0], ["files", 1, 1]]
    done(ctx, "read_file")
    tool(ctx, "web_search")  # not adjacent to the first web searches: a new row
    rt.advance(2)
    assert data(bot)["s"] == [["web", 3, 0], ["files", 1, 0], ["web", 1, 0]]


def test_parallel_calls_of_one_tool_stay_running_until_each_finishes():
    ctx, service, bot, rt = make()
    start(ctx)
    tool(ctx, "web_search", finish=False)
    tool(ctx, "web_search", finish=False)
    rt.flush()
    assert data(bot)["s"] == [["web", 2, 1]]
    done(ctx, "web_search")
    rt.advance(2)
    assert data(bot)["s"] == [["web", 2, 1]]
    done(ctx, "web_search")
    rt.advance(2)
    assert data(bot)["s"] == [["web", 2, 0]]


def test_step_updates_are_coalesced_into_one_edit():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    for name in ("terminal", "read_file", "web_search", "execute_code"):
        tool(ctx, name)
    assert bot.methods() == ["sendWidget"]
    rt.advance(pw.PUSH_DELAY)
    assert bot.methods() == ["sendWidget", "editMessageWidgetData"]
    assert [row[0] for row in data(bot)["s"]] == ["cmd", "files", "web", "code"]
    rt.advance(10)
    assert bot.methods().count("editMessageWidgetData") == 1  # nothing changed since


def test_older_steps_collapse_into_earlier_and_counts_are_capped():
    ctx, service, bot, rt = make()
    start(ctx)
    names = ["terminal", "read_file", "web_search", "execute_code", "skill_view", "memory", "browser_click"]
    for name in names:
        tool(ctx, name)
    for _ in range(pw.MAX_COUNT + 50):
        tool(ctx, "terminal")
    rt.advance(10)
    state = data(bot)
    assert state["e"] == 3 and len(state["s"]) == pw.MAX_ROWS
    assert state["s"][-1] == ["cmd", pw.MAX_COUNT, 0]


def test_no_tool_argument_or_result_reaches_telegram():
    ctx, service, bot, rt = make()
    start(ctx, message="Pay " + SECRETS[0])
    for name in ("web_search", "terminal", "telegram_email_widget", "telegram_inbox_widget", "browser_navigate"):
        tool(ctx, name)
    tool(ctx, "execute_code", finish=False, args={"code": SECRETS[1], "to": SECRETS[2]})
    rt.advance(12)
    done(ctx, "execute_code")
    rt.advance(5)
    end(ctx)
    rt.flush()
    payload = json.dumps(bot.calls, ensure_ascii=False)
    assert "editMessageWidgetData" in bot.methods()
    for secret in SECRETS:
        assert secret not in payload
    codes = {row[0] for m, api in bot.calls if "chat_data" in api for row in json.loads(api["chat_data"])["s"]}
    assert codes <= set(pw.TEXT["en"]["labels"])


# ---- turn end ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("hook", ["post_llm_call", "on_session_end"])
def test_card_is_deleted_when_the_turn_ends(hook):
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    message_id = service._turns[KEY].message_id
    assert ctx.state.get(pw.STATE_KEY) == {KEY: [str(CHAT), message_id]}
    end(ctx, hook=hook)
    rt.flush()
    assert bot.calls[-1] == ("deleteMessage", {"chat_id": CHAT, "message_id": int(message_id)})
    assert ctx.state.get(pw.STATE_KEY) == {} and service._turns == {}
    end(ctx, hook="on_session_end")  # the second end hook of the same turn does nothing
    rt.advance(30)
    assert bot.methods() == ["sendWidget", "deleteMessage"]


def test_turn_ending_while_the_card_is_being_sent_still_deletes_it():
    ctx, service, bot, rt = make()
    start(ctx)
    tool(ctx, "web_search")
    tool(ctx, "terminal", finish=False)  # spawns the send; it has not run yet
    end(ctx)
    rt.flush()
    assert bot.methods() == ["sendWidget", "deleteMessage"]
    assert bot.calls[-1][1]["message_id"] == bot.next_id
    assert ctx.state.get(pw.STATE_KEY, default={}) == {}


def test_interrupt_from_a_typed_stop_removes_the_card():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    ctx.hooks["agent_loop_stopped"](session_key=KEY, platform="telegram", reason="stop",
                                    invalidation_reason="stop_command")
    rt.flush()
    assert bot.methods() == ["sendWidget", "deleteMessage"]


def test_a_missed_end_never_leaves_a_stale_card_behind():
    ctx, service, bot, rt = make()
    start(ctx, turn="t1")
    rt.advance(9)
    start(ctx, turn="t2")
    rt.flush()
    assert bot.methods() == ["sendWidget", "deleteMessage"]
    start(ctx, turn="t2")  # the same turn's pre_llm_call again keeps its timer
    rt.advance(9)
    assert bot.methods() == ["sendWidget", "deleteMessage", "sendWidget"]


def test_failed_send_is_logged_and_forgotten():
    ctx, service, bot, rt = make()

    async def broken(method, api_kwargs=None, return_type=None):
        raise RuntimeError("Bad Request: widgets are not supported")

    bot.do_api_request = broken
    start(ctx)
    rt.advance(9)
    assert service._turns == {}
    end(ctx)
    rt.flush()


def test_restart_sweep_deletes_cards_left_by_the_previous_process():
    ctx, service, bot, rt = make()
    ctx.state.set(pw.STATE_KEY, {"old": [str(CHAT), "7001"], "junk": ["x", None]})
    start(ctx)
    rt.advance(9)
    service.sweep()
    rt.flush()
    assert ("deleteMessage", {"chat_id": CHAT, "message_id": 7001}) in bot.calls
    assert bot.methods().count("deleteMessage") == 1  # the live card stays
    assert set(ctx.state.get(pw.STATE_KEY)) == {KEY}


def test_hooks_never_raise_and_never_block():
    ctx, service, bot, rt = make()

    def boom(*a, **k):
        raise RuntimeError("session context is gone")

    service.session_env = boom
    start(ctx)
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={}) is None
    assert ctx.hooks["post_tool_call"](tool_name="terminal") is None
    assert ctx.hooks["post_llm_call"]() is None and ctx.hooks["on_session_end"]() is None
    service.runtime = None  # before the gateway is wired
    assert ctx.hooks["pre_llm_call"](user_message="hi", platform="telegram") is None


# ---- Stop ----------------------------------------------------------------------------------------------

def test_stop_from_the_owner_sends_a_real_stop_and_answers_first():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    adapter = service.adapter
    message_id = service._turns[KEY].message_id
    tap(service)
    rt.flush()
    assert bot.methods()[1] == "answerWidgetQuery"  # answered before anything else happens
    assert answers(bot) == [{"m": "Stopping…", "d": 1}]
    assert data(bot)["x"] == 1  # the card hides Stop and says "Stopping…"
    (event,) = adapter.events
    assert event.text == "/stop" and event.message_type == "command" and event.user_id == str(USER)
    assert event.metadata == {"gateway_session_key": KEY}  # pinned: never another session
    assert adapter.sources == [{"chat_id": str(CHAT), "chat_type": "dm", "user_id": str(USER), "thread_id": None}]
    assert bot.calls[-1] == ("deleteMessage", {"chat_id": CHAT, "message_id": int(message_id)})
    assert ctx.injected == []


def test_stop_only_from_the_owner_and_only_the_stop_request():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    tap(service, user=1)
    tap(service, "delete")
    assert [a["m"] for a in answers(bot)] == ["Not allowed", "Not allowed"]
    assert service.adapter.events == [] and ctx.injected == [] and KEY in service._turns


def test_second_stop_tap_does_not_stop_twice():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    service.adapter.handle_message = _record_only(service.adapter)  # the turn takes a while to stop
    tap(service)
    tap(service)
    assert len(service.adapter.events) == 1
    assert answers(bot)[-1] == {"m": "Stopping…", "d": 1}


def _record_only(adapter):
    async def handle(event):
        adapter.events.append(event)
    return handle


def test_stop_hint_appears_when_the_turn_keeps_running():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    service.adapter.handle_message = _record_only(service.adapter)
    tap(service)
    rt.advance(pw.STOP_GRACE + 1)
    assert data(bot)["f"] == 1  # "Type /stop in the chat"
    end(ctx)
    rt.flush()
    assert bot.methods()[-1] == "deleteMessage"


def test_without_the_adapter_stop_asks_the_session_and_says_so():
    ctx, service, bot, rt = make(adapter=False)
    start(ctx)
    rt.advance(9)
    tap(service)
    assert answers(bot) == [{"m": "Asked Hermes to stop", "d": 1}]
    assert data(bot)["x"] == 2
    ((prompt, key),) = ctx.injected
    assert key == KEY and prompt.startswith("[Progress widget]")  # its own turn gets no card


def test_failed_stop_falls_back_to_asking_then_to_the_hint():
    ctx, service, bot, rt = make()
    service.adapter = Adapter(service, fail=True)
    start(ctx)
    rt.advance(9)
    tap(service)
    assert data(bot)["x"] == 2 and len(ctx.injected) == 1
    ctx2, service2, bot2, rt2 = make(adapter=False, inject_ok=False)
    start(ctx2)
    rt2.advance(9)
    tap(service2)
    rt2.flush()
    assert data(bot2)["f"] == 1


def test_taps_on_other_widgets_are_left_alone():
    ctx, service, bot, rt = make()
    start(ctx)
    rt.advance(9)
    tap(service, message=4242)
    tap(service, chat=1)
    asyncio.run(service.on_update(SimpleNamespace(api_kwargs={}, callback_query=None), None))
    assert bot.methods() == ["sendWidget"]


# ---- the card itself -------------------------------------------------------------------------------------

@pytest.mark.parametrize("lang", ["en", "ru"])
def test_card_fits_telegram_limits(lang):
    markup = pw.render_html(lang)
    raw = markup.encode()
    assert len(raw) <= 32768 and len(zlib.compress(raw, 9)) <= 4096 and pw.widget_fits(markup)
    turn = pw.Turn(key=KEY, turn_id="t", session_id="s", chat_id=str(CHAT), owner=str(USER), thread_id="",
                   lang=lang, started=START, stopping=2, hint=True)
    turn.steps = [[code, 10 ** 6, 1] for code in pw.TEXT["en"]["labels"]] * 40
    assert len(pw.chat_data(turn).encode()) <= 1024


def test_card_copy_english_default_and_russian():
    ctx, service, bot, rt = make(lang="ru")
    start(ctx)
    rt.advance(9)
    markup = sends(bot)[0]["html"]
    assert "⏳ Работаю" in markup and ">Остановить</button>" in markup
    assert "Искал в интернете" in markup and "Ищу в интернете" in markup
    assert "Searched the web" not in markup and "Searching the web" not in markup
    en = pw.render_html("en")
    assert "⏳ Working on it" in en and '<button id="sp" class="bt" type="button">Stop</button>' in en
    assert "+{n} earlier" in en and "Could not send: {code}" in en
    assert "setInterval(tk,1000)" in en  # the clock ticks in the widget
    assert ".busy{animation:pl" in en and "@keyframes dt" in en  # the current step pulses, dots animate


def test_thread_id_is_kept_for_private_topics():
    ctx, service, bot, rt = make(HERMES_SESSION_THREAD_ID="77")
    start(ctx)
    rt.advance(9)
    assert sends(bot)[0]["message_thread_id"] == 77


def test_wiring_uses_group_minus_11_and_enables_widget_queries(monkeypatch):
    added = []

    class Update:
        ALL_TYPES = ["message", "callback_query"]

    class TypeHandler:
        def __init__(self, kind, callback):
            self.kind, self.callback = kind, callback

    telegram = types.ModuleType("telegram")
    telegram.Update = Update
    ext = types.ModuleType("telegram.ext")
    ext.TypeHandler = TypeHandler
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    ctx = Ctx()
    plugin._service = None
    plugin.register(ctx)
    assert set(ctx.hooks) == {"pre_llm_call", "pre_tool_call", "post_tool_call", "post_llm_call",
                              "on_session_end", "agent_loop_stopped"}
    manifest = (ROOT / "plugin.yaml").read_text()
    assert all(f"  - {hook}\n" in manifest for hook in ctx.hooks)
    app = SimpleNamespace(bot=Bot(), add_handler=lambda handler, group=0: added.append((handler, group)))
    adapter = object()

    async def wire():
        ctx.platform["telegram"](app, adapter)

    asyncio.run(wire())
    ((handler, group),) = added
    assert group == -11 and handler.callback == plugin._service.on_update
    assert Update.ALL_TYPES[-1] == "widget_query" and plugin._service.adapter is adapter
    assert isinstance(plugin._service.runtime, pw.GatewayRuntime)


def test_enable_widget_queries_extends_the_polling_list_once():
    class Update:
        ALL_TYPES = ["message"]

    assert pw.enable_widget_queries(Update) and pw.enable_widget_queries(Update)
    assert Update.ALL_TYPES == ["message", "widget_query"]
    assert not pw.enable_widget_queries(type("Old", (), {"ALL_TYPES": ("message",)}))


def test_gateway_runtime_runs_hook_work_on_the_loop_from_another_thread():
    import threading

    async def main():
        loop = asyncio.get_running_loop()
        runtime = pw.GatewayRuntime(Bot(), loop)
        seen, ready = [], asyncio.Event()

        def on_loop(value):
            seen.append((value, threading.current_thread() is threading.main_thread()))
            ready.set()

        threading.Thread(target=runtime.call, args=(on_loop, 1)).start()
        await asyncio.wait_for(ready.wait(), 2)
        return seen

    assert asyncio.run(main()) == [(1, True)]
    pw.GatewayRuntime(Bot(), None).call(lambda: 1 / 0)  # no loop: silently nothing
