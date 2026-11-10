import asyncio
import hashlib
import hmac
import importlib.util
import json
import sys
import time
import types
import zlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tac_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)
from tac_under_test import computer as cm  # noqa: E402
from tac_under_test import viewer as vw  # noqa: E402
from widget_dom import run_widget  # noqa: E402

CHAT, USER, TG_MSG, TOKEN, BOT = 1000001, 1000001, 9100, "123456:TEST-token", "hermes_test_bot"


# ---- fakes ---------------------------------------------------------------------------------------
class State:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return json.loads(json.dumps(self.values.get(key, default)))

    def set(self, key, value):
        self.values[key] = json.loads(json.dumps(value))


class Ctx:
    def __init__(self):
        self.state, self.tools, self.hooks, self.injected, self.handlers = State(), {}, {}, [], []

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw

    def register_hook(self, name, callback, **kw):
        self.hooks[name] = callback

    def register_platform_handler(self, platform, factory):
        self.handlers.append((platform, factory))

    def register_system_prompt_section(self, *args, **kw):
        pass

    def inject_message(self, content, role="user", *, session_key=None):
        self.injected.append((content, session_key))
        return True


class Bot:
    def __init__(self):
        self.calls, self.username, self.token = [], BOT, TOKEN

    async def do_api_request(self, method, api_kwargs=None, return_type=None):
        self.calls.append((method, api_kwargs))
        return {"message_id": TG_MSG + len(self.calls)} if method == "sendWidget" else True


class Runtime:
    """Fire-and-forget calls queue up like on the gateway loop; drain() runs them."""

    def __init__(self, bot):
        self.bot, self.pending = bot, []

    def submit(self, coroutine, timeout=15):
        result = asyncio.run(coroutine)
        self.drain()
        return result

    def fire(self, coroutine):
        self.pending.append(coroutine)
        return True

    def drain(self):
        while self.pending:
            asyncio.run(self.pending.pop(0))


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def session_env(**over):
    values = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_TYPE": "dm",
              "HERMES_SESSION_CHAT_ID": str(CHAT), "HERMES_SESSION_USER_ID": str(USER),
              "HERMES_SESSION_KEY": "sess-1", "HERMES_SESSION_THREAD_ID": ""}
    values.update(over)
    return lambda key, default="": values.get(key, default)


def make(lang=None, **env):
    ctx = Ctx()
    plugin.get_session_env = session_env(**env)
    plugin.register(ctx)
    service = plugin._service
    bot = Bot()
    service.runtime = Runtime(bot)
    service.clock = Clock()
    service.human_holds = lambda: False
    if lang:
        service.language = lambda: lang
    return ctx, service, bot


def browse(ctx, code="# Opening GitHub\nnew_tab('https://github.com/login')", tool="browser_exec", **args):
    result = ctx.hooks["pre_tool_call"](tool_name=tool, args={"code": code, **args} if tool == "browser_exec" else args,
                                        task_id="t", session_id="agent-session")
    plugin._service.runtime.drain()
    return result


def sent(bot, method=None):
    return [api for m, api in bot.calls if method is None or m == method]


def data(bot, index=-1):
    return json.loads([api["chat_data"] for m, api in bot.calls
                       if m in {"sendWidget", "editMessageWidgetData"}][index])


# ---- Telegram initData ---------------------------------------------------------------------------
def sign(fields, token=TOKEN):
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def init_data(user_id=USER, auth_date=None, token=TOKEN, **extra):
    user = json.dumps({"id": user_id, "first_name": "Alex", "language_code": "ru"}, separators=(",", ":"))
    fields = {"auth_date": str(int(auth_date if auth_date is not None else time.time())), "query_id": "AAH",
              "user": user, "signature": "sig", **extra}
    return sign(fields, token)


ALLOWED = {str(USER)}


def test_valid_init_data_returns_the_user_and_start_param():
    user = vw.verify_init_data(init_data(start_param="c_0a1b2c3d"), TOKEN, ALLOWED)
    assert user == {"id": USER, "language_code": "ru", "start_param": "c_0a1b2c3d"}


def test_tampered_init_data_is_refused():
    raw = init_data()
    assert vw.verify_init_data(raw.replace("Alex", "Mallory"), TOKEN, ALLOWED) is None
    assert vw.verify_init_data(init_data(token="999:other"), TOKEN, ALLOWED) is None
    assert vw.verify_init_data(raw + "&user=%7B%22id%22%3A1%7D", TOKEN, ALLOWED) is None  # repeated key
    assert vw.verify_init_data(raw.split("&hash=")[0], TOKEN, ALLOWED) is None  # no hash
    assert vw.verify_init_data(raw, "", ALLOWED) is None  # bot not wired yet: fail closed


def test_stale_or_future_init_data_is_refused():
    now = time.time()
    assert vw.MAX_AUTH_AGE == 300
    assert vw.verify_init_data(init_data(auth_date=now - 301), TOKEN, ALLOWED, now=now) is None
    assert vw.verify_init_data(init_data(auth_date=now + 600), TOKEN, ALLOWED, now=now) is None
    assert vw.verify_init_data(init_data(auth_date=now - 240), TOKEN, ALLOWED, now=now) is not None


def test_foreign_user_is_refused_and_empty_allow_list_refuses_everyone():
    assert vw.verify_init_data(init_data(user_id=42), TOKEN, ALLOWED) is None
    assert vw.verify_init_data(init_data(), TOKEN, set()) is None
    assert vw.allowed_users({"TELEGRAM_ALLOWED_USERS": " 1000001, 42 ,x,"}) == {"1000001", "42"}


# ---- the Computer card ---------------------------------------------------------------------------
def test_first_screen_call_sends_one_card_and_later_calls_update_it_in_place():
    ctx, service, bot = make()
    assert browse(ctx) is None  # never a directive: the tool call is untouched
    assert [m for m, _ in bot.calls] == ["sendWidget"]
    api = sent(bot, "sendWidget")[0]
    card_id = next(iter(ctx.state.get(cm.STATE_KEY)))
    assert api["chat_id"] == CHAT and "reply_markup" not in api and "caption" not in api
    assert ">Open screen</button>" in api["html"] and "🖥 Computer" in api["html"]
    assert data(bot) == {"m": "w", "h": "github.com", "a": "Opening GitHub", "u": int(service.clock())}
    run = run_widget(api["html"], api["chat_data"], look=["#q", "#pg", "#ac"], steps=[{"click": "#os"}])
    shown = run["frames"][0]
    assert not shown["#q"][0]["shown"] and shown["#pg"][0]["text"] == "github.com"
    assert shown["#ac"][0]["text"] == "Opening GitHub"
    # Open screen opens the Mini App inside the tap itself, never through a bot request.
    assert run["tg_links"] == [f"https://t.me/{BOT}/hermes_desktop?startapp=c_{card_id}"] and run["requests"] == []

    service.clock.now += 30
    browse(ctx, tool="computer_use", action="left_click", element=4)
    browse(ctx, tool="computer_use", action="type", text="hunter2")
    assert [m for m, _ in bot.calls] == ["sendWidget", "editMessageWidgetData", "editMessageWidgetData"]
    assert all(api["message_id"] == TG_MSG + 1 for api in sent(bot, "editMessageWidgetData"))
    assert data(bot)["a"] == "Typed text" and data(bot)["h"] == "github.com"
    assert "hunter2" not in json.dumps(bot.calls)  # typed text never reaches the card


def test_other_tools_and_other_chats_send_nothing():
    ctx, service, bot = make()
    ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "ls"})
    ctx.hooks["pre_tool_call"](tool_name="camofox_navigate", args={"url": "https://x.com"})
    assert bot.calls == []
    for env in ({"HERMES_SESSION_PLATFORM": "discord"}, {"HERMES_SESSION_CHAT_TYPE": "group"}):
        ctx, service, bot = make(**env)
        browse(ctx)
        assert bot.calls == []


def test_hook_never_fails_the_tool_call():
    ctx, service, bot = make()

    def broken(coroutine):
        coroutine.close()
        raise RuntimeError("loop gone")
    service.runtime.fire = broken
    assert browse(ctx) is None


def test_turn_end_idles_the_card_and_an_old_idle_card_is_replaced():
    ctx, service, bot = make()
    browse(ctx)
    ctx.hooks["post_llm_call"](session_id="agent-session")
    service.runtime.drain()
    assert data(bot)["m"] == "i"
    service.clock.now += cm.REUSE_SECONDS - 60
    browse(ctx, code="# Checking the PR\ngoto_url('https://github.com/o/r/pull/1')")
    assert [m for m, _ in bot.calls] == ["sendWidget", "editMessageWidgetData", "editMessageWidgetData"]
    assert data(bot)["m"] == "w" and data(bot)["a"] == "Checking the PR"
    ctx.hooks["post_llm_call"](session_id="agent-session")
    service.runtime.drain()
    service.clock.now += cm.REUSE_SECONDS + 1
    browse(ctx)
    assert [m for m, _ in bot.calls][-1] == "sendWidget" and len(sent(bot, "sendWidget")) == 2


def test_page_title_comes_from_browser_results_and_resets_on_another_site():
    ctx, service, bot = make()
    browse(ctx)
    ctx.hooks["post_tool_call"](tool_name="browser_exec", args={},
                                result=json.dumps({"output": "{'url': 'https://github.com/login', 'title': 'Sign in to GitHub · GitHub'}"}))
    service.runtime.drain()
    assert data(bot)["t"] == "Sign in to GitHub · GitHub"
    browse(ctx, code="new_tab('https://www.example.org/')")
    assert data(bot)["h"] == "example.org" and "t" not in data(bot) and data(bot)["a"] == "Opened example.org"


def test_chat_data_and_html_stay_within_telegram_limits():
    ctx, service, bot = make(lang="ru")
    state = {"m": "h", "h": "очень-длинный-домен." * 3, "t": "Заголовок " * 60, "a": "Действие " * 30,
             "q": "Войдите в аккаунт " * 20, "u": 1_800_000_000}
    text = cm.chat_data(state)
    assert len(text.encode("utf-8")) <= cm.MAX_CHAT_DATA and json.loads(text)["m"] == "h"
    browse(ctx)
    markup = sent(bot, "sendWidget")[0]["html"]
    assert len(zlib.compress(markup.encode(), 9)) <= cm.MAX_COMPRESSED_BYTES
    assert ">Открыть экран</button>" in markup and "🖥 Компьютер" in markup and "Управляете вы" in markup


def test_takeover_request_posts_the_reason_and_tells_the_model_to_end_the_turn():
    ctx, service, bot = make()
    reply = json.loads(ctx.tools["telegram_agent_computer"]["handler"]({"reason": "Sign in to GitHub"}))
    assert reply["ok"] and reply["card"]["request"] == "Sign in to GitHub" and "NO_REPLY" in reply["note"]
    assert data(bot)["q"] == "Sign in to GitHub" and [m for m, _ in bot.calls] == ["sendWidget"]
    assert json.loads(ctx.tools["telegram_agent_computer"]["handler"]({"reason": ""}))["error"] == "invalid_reason"
    assert json.loads(ctx.tools["telegram_agent_computer"]["handler"]({"reason": "x" * 200}))["error"] == "invalid_reason"
    assert "camofox" in ctx.tools["telegram_agent_computer"]["schema"]["description"]


def test_lease_changes_drive_the_pill_and_a_hand_back_resumes_the_session():
    ctx, service, bot = make()
    ctx.tools["telegram_agent_computer"]["handler"]({"reason": "Enter the SMS code"})
    ctx.hooks["post_llm_call"](session_id="agent-session")
    service.runtime.drain()
    service.on_lease(SimpleNamespace(holder="human", viewer_id="v", epoch=2))
    service.runtime.drain()
    assert data(bot)["m"] == "h" and data(bot)["q"] == "Enter the SMS code" and ctx.injected == []
    service.on_lease(SimpleNamespace(holder="agent", viewer_id=None, epoch=3))
    service.runtime.drain()
    assert data(bot)["m"] == "i" and "q" not in data(bot)
    assert len(ctx.injected) == 1 and ctx.injected[0][1] == "sess-1" and "Enter the SMS code" in ctx.injected[0][0]


def test_request_answered_in_chat_clears_on_the_next_screen_turn():
    ctx, service, bot = make()
    ctx.tools["telegram_agent_computer"]["handler"]({"reason": "Solve the CAPTCHA"})
    browse(ctx)  # same turn: the request stays
    assert data(bot)["q"] == "Solve the CAPTCHA"
    ctx.hooks["post_llm_call"](session_id="agent-session")
    service.runtime.drain()
    browse(ctx)  # a later turn works on the screen again
    assert "q" not in data(bot)


def test_mini_app_sees_only_its_owners_card():
    ctx, service, bot = make()
    ctx.tools["telegram_agent_computer"]["handler"]({"reason": "Sign in to X"})
    card_id = next(iter(ctx.state.get(cm.STATE_KEY)))
    assert service.request_for(card_id, str(USER)) == {"request": "Sign in to X", "page": ""}
    assert service.request_for(card_id, "42") is None and service.request_for("deadbeef", str(USER)) is None


# ---- the Mini App control socket ------------------------------------------------------------------
class Desktop:
    def __init__(self, running=True, start_error=None):
        self.is_running, self.start_error = running, start_error
        self.calls, self.current = [], SimpleNamespace(holder="agent", viewer_id=None, reason="", epoch=1)

    def lease(self):
        return self.current

    def acquire(self, viewer_id, reason):
        self.calls.append(("acquire", viewer_id, reason))
        self.current = SimpleNamespace(holder="human", viewer_id=viewer_id, reason=reason, epoch=self.current.epoch + 1)
        return self.current

    def release(self, viewer_id):
        """Like ``lease.release(viewer_id)``: only the holder releases; otherwise nothing changes."""
        self.calls.append(("release", viewer_id))
        if self.current.holder == "human" and self.current.viewer_id == viewer_id:
            self.current = SimpleNamespace(holder="agent", viewer_id=None, reason="", epoch=self.current.epoch + 1)
        return self.current

    def viewer_may_send_input(self, viewer_id):
        return self.current.holder == "human" and self.current.viewer_id == viewer_id

    def close_display(self, viewer_id):
        """What the display bridge does when a viewer's window closes cleanly (display.py)."""
        if self.viewer_may_send_input(viewer_id):
            self.release(viewer_id)

    def running(self):
        return self.is_running

    def start(self):
        self.calls.append(("start",))
        if self.start_error:
            raise RuntimeError(self.start_error)
        self.is_running = True

    def mint(self, viewer_id):
        self.calls.append(("mint", viewer_id))
        return f"ticket-{len(self.calls)}"


class Socket:
    DISCONNECT = object()

    def __init__(self, *incoming):
        self.incoming, self.sent, self.closed = asyncio.Queue(), [], None
        for item in incoming:
            self.incoming.put_nowait(item)

    async def receive_text(self):
        item = await self.incoming.get()
        if item is Socket.DISCONNECT:
            raise ConnectionError("disconnect")
        return item if isinstance(item, str) else json.dumps(item)

    async def send_json(self, value):
        self.sent.append(json.loads(json.dumps(value)))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def hub_for(desktop, cards=None, allowed=str(USER), owner=str(USER), **kw):
    return vw.ViewerHub(desktop, lambda: TOKEN, cards, env=lambda: {"TELEGRAM_ALLOWED_USERS": allowed},
                        owner=owner if callable(owner) else (lambda: owner), **kw)


def minted(desktop):
    return [c[1] for c in desktop.calls if c[0] == "mint"]


def serve(hub, socket):
    asyncio.run(asyncio.wait_for(hub.serve(socket), timeout=5))
    return socket


def test_socket_requires_init_first_and_closes_4401_without_touching_the_screen():
    for first in ({"op": "take"}, {"init": init_data(user_id=42)}, {"init": init_data(), "viewer_id": "x"}, "nonsense"):
        desktop = Desktop()
        socket = serve(hub_for(desktop), Socket(first, {"op": "take"}, Socket.DISCONNECT))
        assert socket.closed[0] == 4401 and socket.sent == [] and desktop.calls == []


def test_init_starts_the_screen_and_returns_a_ticket_and_the_lease():
    desktop = Desktop(running=False)
    hub = hub_for(desktop)
    socket = serve(hub, Socket({"init": init_data()}, Socket.DISCONNECT))
    vid = minted(desktop)[0]
    assert desktop.calls == [("start",), ("mint", vid)]
    ready = socket.sent[0]
    assert ready["type"] == "ready" and ready["ticket"] == "ticket-2" and ready["path"] == "/api/display/ws"
    assert ready["lease"] == {"holder": "agent", "mine": False, "reason": "", "epoch": 1}
    assert vid not in json.dumps(socket.sent)  # the viewer id never leaves the server


def test_take_and_back_use_the_server_side_viewer_id_and_the_cards_request():
    ctx, service, bot = make()
    ctx.tools["telegram_agent_computer"]["handler"]({"reason": "Sign in to GitHub"})
    card_id = next(iter(ctx.state.get(cm.STATE_KEY)))
    desktop = Desktop()
    hub = hub_for(desktop, service)
    socket = serve(hub, Socket({"init": init_data(start_param=f"c_{card_id}")}, {"op": "take"}, {"op": "back"},
                               {"op": "ticket"}, Socket.DISCONNECT))
    vid = minted(desktop)[0]
    assert socket.sent[0]["card"] == {"request": "Sign in to GitHub", "page": ""}
    assert [c for c in desktop.calls if c[0] != "mint"] == [("acquire", vid, "Sign in to GitHub"), ("release", vid)]
    assert socket.sent[1] == {"type": "lease", "re": "take",
                              "lease": {"holder": "human", "mine": True, "reason": "Sign in to GitHub", "epoch": 2}}
    assert socket.sent[2]["lease"]["mine"] is False and socket.sent[2]["re"] == "back"
    assert socket.sent[3]["type"] == "ticket" and socket.sent[3]["ticket"].startswith("ticket-")
    assert vid not in json.dumps(socket.sent)


def test_foreign_messages_are_refused_and_never_reach_the_lease():
    desktop = Desktop()
    socket = serve(hub_for(desktop), Socket(
        {"init": init_data()}, {"op": "take", "viewer_id": "stolen"}, {"op": "release"}, {"init": init_data()},
        "[1,2]", "not json", Socket.DISCONNECT))
    assert [m for m in socket.sent[1:]] == [{"type": "error", "code": "unsupported"}] * 5
    assert [c[0] for c in desktop.calls] == ["mint"]
    big = serve(hub_for(Desktop()), Socket({"init": init_data()}, "x" * (vw.MAX_MESSAGE + 1), {"op": "take"}))
    assert big.closed[0] == 1009


def test_another_cards_start_param_shows_nothing():
    ctx, service, bot = make()
    ctx.tools["telegram_agent_computer"]["handler"]({"reason": "Sign in"})
    socket = serve(hub_for(Desktop(), service), Socket({"init": init_data(start_param="c_deadbeef")}, Socket.DISCONNECT))
    assert socket.sent[0]["card"] is None


def test_screen_start_failure_is_reported_and_a_later_ticket_retries():
    desktop = Desktop(running=False, start_error="Bot Desktop needs Xvnc on the gateway host")
    socket = serve(hub_for(desktop), Socket({"init": init_data()}, Socket.DISCONNECT))
    assert socket.sent[0]["error"] == "screen_unavailable" and "Xvnc" in socket.sent[0]["detail"]
    assert "ticket" not in socket.sent[0] and socket.closed is None


def test_lease_changes_are_pushed_once_per_epoch():
    desktop = Desktop()
    hub = hub_for(desktop)
    socket = Socket({"init": init_data()})

    async def run():
        task = asyncio.ensure_future(hub.serve(socket))
        while not socket.sent:
            await asyncio.sleep(0.01)
        other = SimpleNamespace(holder="human", viewer_id="someone-else", reason="Desktop", epoch=7)
        hub.on_lease(other)
        hub.on_lease(other)
        await asyncio.sleep(0.05)
        socket.incoming.put_nowait(Socket.DISCONNECT)
        await asyncio.wait_for(task, timeout=5)
    asyncio.run(run())
    pushes = [m for m in socket.sent if m["type"] == "lease"]
    assert pushes == [{"type": "lease", "lease": {"holder": "human", "mine": False, "reason": "Desktop", "epoch": 7}}]


def test_registry_exposes_the_hub_to_the_dashboard_half():
    ctx, service, bot = make()
    shared = sys.modules[vw.SHARED_MODULE]
    assert shared.hub is plugin._hub and shared.hub.bot_token() == TOKEN


def _public_url(monkeypatch, url, error=None):
    """``gateway.webapp_listener.webapp_settings(home).public_url`` as the dashboard half reads it."""
    listener = types.ModuleType("gateway.webapp_listener")

    def webapp_settings(home):
        if error:
            raise error
        return SimpleNamespace(public_url=url)
    listener.webapp_settings = webapp_settings
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: "/home/hermes/.hermes"
    monkeypatch.setitem(sys.modules, "gateway.webapp_listener", listener)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)


def _dashboard_app():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api_spec = importlib.util.spec_from_file_location("tac_dashboard_api", ROOT / "dashboard" / "api.py")
    api = importlib.util.module_from_spec(api_spec)
    api_spec.loader.exec_module(api)
    assert api.SHARED_MODULE == vw.SHARED_MODULE
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/telegram-agent-computer")
    return TestClient(app)


PUBLIC = "https://hermes.example.ts.net:9443"
WS_PATH = "/api/plugins/telegram-agent-computer/ws"


def test_dashboard_route_hands_the_socket_to_the_hub(monkeypatch):
    client = _dashboard_app()
    _public_url(monkeypatch, PUBLIC)
    shared = sys.modules[vw.SHARED_MODULE]
    shared.hub = hub_for(Desktop())
    with client.websocket_connect(WS_PATH, headers={"Origin": PUBLIC}) as ws:
        ws.send_text(json.dumps({"init": init_data()}))
        assert ws.receive_json()["type"] == "ready"
        ws.send_text(json.dumps({"op": "take"}))
        assert ws.receive_json()["lease"]["mine"] is True
    del shared.hub
    with client.websocket_connect(WS_PATH, headers={"Origin": PUBLIC}) as ws:
        with pytest.raises(Exception) as closed:
            ws.receive_json()
    assert getattr(closed.value, "code", None) == 1011


@pytest.mark.parametrize("origin", [
    None,  # no Origin header
    "https://hermes.example.ts.net",  # the same host on :443
    "https://hermes.example.ts.net:8443",  # a sibling port
    "http://hermes.example.ts.net:9443",  # another scheme
    "https://evil.example",
    "https://hermes.example.ts.net:9443.evil.example",
    "https://user@hermes.example.ts.net:9443",
    "https://hermes.example.ts.net:9443/path",
    "null",
])
def test_dashboard_route_refuses_any_origin_but_the_public_url_before_accepting(monkeypatch, origin):
    from starlette.websockets import WebSocketDisconnect
    client = _dashboard_app()
    _public_url(monkeypatch, PUBLIC)
    desktop = Desktop()
    sys.modules[vw.SHARED_MODULE].hub = hub_for(desktop)
    headers = {"Origin": origin} if origin is not None else {}
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(WS_PATH, headers=headers):
            pass
    assert closed.value.code == 4403 and desktop.calls == []


@pytest.mark.parametrize("url,error", [("", None), ("not a url", None), (PUBLIC, OSError("config.yaml unreadable"))])
def test_dashboard_route_fails_closed_without_a_usable_public_url(monkeypatch, url, error):
    from starlette.websockets import WebSocketDisconnect
    client = _dashboard_app()
    _public_url(monkeypatch, url, error)
    sys.modules[vw.SHARED_MODULE].hub = hub_for(Desktop())
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(WS_PATH, headers={"Origin": PUBLIC}):
            pass
    assert closed.value.code == 4403


def test_dashboard_route_accepts_the_public_origin_with_its_default_port_spelled_out(monkeypatch):
    client = _dashboard_app()
    _public_url(monkeypatch, "https://hermes.example")
    sys.modules[vw.SHARED_MODULE].hub = hub_for(Desktop())
    for origin in ("https://hermes.example", "https://hermes.example:443"):
        with client.websocket_connect(WS_PATH, headers={"Origin": origin}) as ws:
            ws.send_text(json.dumps({"init": init_data()}))
            assert ws.receive_json()["type"] == "ready"


# ---- audit fixes: takeover fence, session limits, owner gate, per-connection viewer ids -------------
FENCED = ("terminal", "process_manage", "execute_code", "delegate_task", "computer_use", "browser_exec",
          "browser_cdp", "browser_dialog", "browser_vault")


def test_tools_that_could_reach_the_screen_are_blocked_while_the_user_holds_control():
    ctx, service, bot = make()
    service.human_holds = lambda: True
    for tool in FENCED:
        result = ctx.hooks["pre_tool_call"](tool_name=tool, args={"command": "xdotool type x"}, session_id="s")
        assert result["action"] == "block" and "in control of the screen" in result["message"]
        assert "not retry" in result["message"]
    for tool in ("read_file", "web_search", "telegram_agent_computer", "camofox_navigate", "", None):
        assert ctx.hooks["pre_tool_call"](tool_name=tool, args={}, session_id="s") is None
    service.runtime.drain()
    assert bot.calls == []  # a refused call is not reported on the card


def test_tools_pass_through_when_the_agent_holds_or_the_lease_cannot_be_read():
    ctx, service, bot = make()
    for tool in FENCED:
        assert ctx.hooks["pre_tool_call"](tool_name=tool, args={}, session_id="s") is None

    def broken():
        raise OSError("lease unreadable")
    service.human_holds = broken
    for tool in FENCED:
        assert ctx.hooks["pre_tool_call"](tool_name=tool, args={}, session_id="s") is None
    assert browse(ctx) is None and [m for m, _ in bot.calls][0] == "sendWidget"


def test_the_socket_refuses_init_data_older_than_five_minutes():
    desktop = Desktop()
    socket = serve(hub_for(desktop), Socket({"init": init_data(auth_date=time.time() - 301)}, Socket.DISCONNECT))
    assert socket.closed[0] == 4401 and socket.sent == [] and desktop.calls == []
    fresh = serve(hub_for(Desktop()), Socket({"init": init_data(auth_date=time.time() - 240)}, Socket.DISCONNECT))
    assert fresh.sent[0]["type"] == "ready"


def test_a_session_closes_4401_when_its_lifetime_ends():
    assert vw.SESSION_LIFETIME == 1800
    desktop = Desktop()
    socket = serve(hub_for(desktop, session_lifetime=0.2), Socket({"init": init_data()}))  # never disconnects
    assert socket.sent[0]["type"] == "ready" and socket.closed == (4401, "session expired")


def test_an_expired_session_serves_nothing_more():
    desktop = Desktop()
    socket = Socket({"init": init_data()})
    hub = hub_for(desktop, session_lifetime=0.2)

    async def run():
        task = asyncio.ensure_future(hub.serve(socket))
        while not socket.sent:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        socket.incoming.put_nowait({"op": "take"})
        await asyncio.wait_for(task, timeout=5)
    asyncio.run(run())
    assert socket.closed == (4401, "session expired") and [c[0] for c in desktop.calls] == ["mint"]


@pytest.mark.parametrize("allowed,owner,user_id", [
    (f"{USER},42", str(USER), 42),  # an allowed chat user who is not the home-channel owner
    (str(USER), "", USER),  # no home-channel owner configured
    ("42", str(USER), USER),  # the owner is no longer an allowed user
    (str(USER), "unreadable", USER),  # the config cannot be read
])
def test_only_the_home_channel_owner_who_is_an_allowed_user_gets_the_desktop(allowed, owner, user_id):
    def broken():
        raise OSError("config.yaml unreadable")
    desktop = Desktop()
    hub = hub_for(desktop, allowed=allowed, owner=broken if owner == "unreadable" else owner)
    socket = serve(hub, Socket({"init": init_data(user_id=user_id)}, {"op": "take"}, Socket.DISCONNECT))
    assert socket.closed[0] == 4401 and socket.sent == [] and desktop.calls == []
    ok = serve(hub_for(Desktop(), allowed=f"{USER},42"), Socket({"init": init_data()}, Socket.DISCONNECT))
    assert ok.sent[0]["type"] == "ready"


def test_home_channel_owner_reads_the_telegram_home_channel(monkeypatch):
    config = {"platforms": {"telegram": {"home_channel": {"chat_id": "1", "user_id": "1000001"}}}}
    package, module = types.ModuleType("hermes_cli"), types.ModuleType("hermes_cli.config")
    module.load_config_readonly = lambda: config
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    assert vw.home_channel_owner() == "1000001"
    for home in ({}, {"user_id": None}, {"user_id": "abc"}, {"user_id": True}, "x"):
        config["platforms"]["telegram"]["home_channel"] = home
        assert vw.home_channel_owner() == ""


def test_two_connections_have_their_own_viewer_ids_and_one_closing_keeps_the_others_lease():
    desktop = Desktop()
    hub = hub_for(desktop)
    first, second = Socket({"init": init_data()}), Socket({"init": init_data()})

    async def until(socket, count):
        while len(socket.sent) < count:
            await asyncio.sleep(0.01)

    async def run():
        a = asyncio.ensure_future(hub.serve(first))
        await until(first, 1)
        first.incoming.put_nowait({"op": "take"})
        await until(first, 2)
        b = asyncio.ensure_future(hub.serve(second))
        await until(second, 1)
        second.incoming.put_nowait({"op": "back"})  # Hand back from the window that does not hold control
        await until(second, 2)
        second.incoming.put_nowait(Socket.DISCONNECT)
        await asyncio.wait_for(b, timeout=5)
        desktop.close_display(minted(desktop)[1])  # the second window's screen closes cleanly
        first.incoming.put_nowait(Socket.DISCONNECT)
        await asyncio.wait_for(a, timeout=5)
    asyncio.run(run())
    vid_a, vid_b = minted(desktop)
    assert vid_a != vid_b
    assert desktop.current.holder == "human" and desktop.current.viewer_id == vid_a  # still the first window's
    assert second.sent[0]["lease"]["mine"] is False and second.sent[0]["lease"]["holder"] == "human"
    assert second.sent[1] == {"type": "lease", "re": "back",
                              "lease": {"holder": "human", "mine": False, "reason": "Taken over from Telegram",
                                        "epoch": 2}}
    assert first.sent[1]["lease"]["mine"] is True
    assert vid_a not in json.dumps(first.sent + second.sent) and vid_b not in json.dumps(first.sent + second.sent)


def test_a_card_with_an_old_mini_app_link_is_not_reused():
    ctx, service, bot = make()
    browse(ctx)
    cards = ctx.state.get(cm.STATE_KEY)
    ctx.state.set(cm.STATE_KEY, {k: {**c, "mini_app": "screen"} for k, c in cards.items()})  # sent by an older version
    browse(ctx)
    assert len(sent(bot, "sendWidget")) == 2 and cm.MINI_APP in sent(bot, "sendWidget")[1]["html"]


def test_a_card_deleted_from_the_chat_is_forgotten_and_replaced():
    ctx, service, bot = make()
    browse(ctx)
    real = bot.do_api_request

    async def gone(method, api_kwargs=None, return_type=None):
        if method == "editMessageWidgetData":
            raise RuntimeError("Bad Request: message with widget to edit not found")
        return await real(method, api_kwargs, return_type)
    bot.do_api_request = gone
    browse(ctx, code="goto_url('https://example.org/')")
    service.runtime.drain()
    assert ctx.state.get(cm.STATE_KEY) == {}
    browse(ctx)
    service.runtime.drain()
    assert len(sent(bot, "sendWidget")) == 2
