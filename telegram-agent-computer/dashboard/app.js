// Telegram Mini App: the bot's live screen (noVNC over /api/display/ws) with Take over / Hand back.
// The control socket (/api/plugins/telegram-agent-computer/ws) authenticates with Telegram initData and
// hands out single-use display tickets; the lease decides who may drive the screen (server-side gate).
import RFB from "./novnc/core/rfb.js";
import keysyms from "./novnc/core/input/keysymdef.js";

const TEXT = {
  en: {
    title: "Computer", connecting: "Connecting…", need: "Hermes needs you", take: "Take over", back: "Hand back",
    type: "Type", enter: "Enter", placeholder: "Text to type on the screen", show: "Show", hide: "Hide",
    watching: "You are watching. Take over to use the screen.",
    controlling: "You are in control. Hermes waits until you hand back.",
    other: "Another window is in control. Take over to use the screen here.",
    working: "Hermes is working", mine: "You are in control", elsewhere: "In control elsewhere",
    starting: "Starting the screen…", off: "Screen is off", reconnecting: "Reconnecting…",
    denied: "Not allowed", ended: "Session ended",
    expired: "This session has expired. Reopen the screen from the Computer card.",
    notTelegram: "Open this from the Computer card in Telegram.",
    unauthorized: "This Telegram account may not open the screen.",
    refused: "The server refused this page. Open it from the Computer card in Telegram.",
    unavailable: "The screen is not available: {detail}",
  },
  ru: {
    title: "Компьютер", connecting: "Подключение…", need: "Нужна ваша помощь", take: "Взять управление",
    back: "Вернуть управление", type: "Ввести", enter: "Enter", placeholder: "Текст для ввода на экране",
    show: "Показать", hide: "Скрыть",
    watching: "Вы смотрите. Возьмите управление, чтобы работать с экраном.",
    controlling: "Управляете вы. Hermes ждёт, пока вы вернёте управление.",
    other: "Управление в другом окне. Возьмите управление, чтобы работать здесь.",
    working: "Hermes работает", mine: "Управляете вы", elsewhere: "Управляют в другом окне",
    starting: "Запуск экрана…", off: "Экран выключен", reconnecting: "Переподключение…",
    denied: "Нет доступа", ended: "Сеанс завершён",
    expired: "Сеанс истёк. Откройте экран заново из карточки «Компьютер».",
    notTelegram: "Откройте это из карточки «Компьютер» в Telegram.",
    unauthorized: "Этому аккаунту Telegram экран недоступен.",
    refused: "Сервер отклонил страницу. Откройте её из карточки «Компьютер» в Telegram.",
    unavailable: "Экран недоступен: {detail}",
  },
};
const KEYS = { enter: 0xff0d, backspace: 0xff08, tab: 0xff09 };

const TG = window.Telegram && window.Telegram.WebApp;
const user = TG && TG.initDataUnsafe && TG.initDataUnsafe.user;
const lang = String((user && user.language_code) || "").toLowerCase().startsWith("ru") ? "ru" : "en";
const T = TEXT[lang];
const $ = (id) => document.getElementById(id);
const WS_BASE = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
const CONTROL_URL = WS_BASE + "/api/plugins/telegram-agent-computer/ws";

let control = null;
let controlRetry = 0;
let rfb = null;
let screenSocket = null;
let screenRetry = 0;
let ticketTimer = 0;
let lease = null;
let fatal = false;
let authed = false; // this page's initData was accepted once: a later 4401 means it went stale

document.documentElement.lang = lang;
document.title = T.title;
document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = T[el.dataset.t]; });
$("text").placeholder = T.placeholder;

// The typing field is masked (it often carries a password) and never outlives control of the screen.
function mask(hidden) {
  $("text").type = hidden ? "password" : "text";
  $("reveal").textContent = hidden ? T.show : T.hide;
  $("reveal").setAttribute("aria-pressed", String(!hidden));
}

function clearText() {
  $("text").value = "";
  mask(true);
}
mask(true);

function chip(key, tone) {
  $("chip").textContent = T[key];
  $("chip").className = "chip" + (tone ? " " + tone : "");
}

function overlay(text, busy) {
  const ov = $("ov");
  if (text === null) { ov.hidden = true; return; }
  ov.hidden = false;
  ov.querySelector(".sp").style.display = busy ? "" : "none";
  $("ovt").textContent = text;
}

function fail(key, tone, chipKey) {
  fatal = true;
  clearTimeout(ticketTimer);
  chip(chipKey || "denied", tone || "bad");
  overlay(T[key], false);
  $("take").disabled = true;
  $("take").hidden = false;
  $("back").hidden = true;
  $("kb").hidden = true;
  clearText();
  const screen = rfb;
  rfb = null;
  screenSocket = null;
  if (screen) {
    try { screen.disconnect(); } catch (e) { /* already gone */ }
  }
}

function backoff(attempt) {
  return Math.min(10000, 500 * 2 ** Math.min(attempt, 5));
}

function send(message) {
  if (control && control.readyState === WebSocket.OPEN) {
    control.send(JSON.stringify(message));
    return true;
  }
  return false;
}

function showCard(card) {
  const request = card && card.request;
  $("req").hidden = !request;
  $("reqt").textContent = request || "";
}

function setLease(next) {
  lease = next;
  const mine = Boolean(next && next.mine);
  const human = next && next.holder === "human";
  $("take").hidden = mine;
  $("take").disabled = false;
  $("back").hidden = !mine;
  $("back").disabled = false;
  $("kb").hidden = !mine;
  if (!mine) clearText();
  $("hint").textContent = mine ? T.controlling : human ? T.other : T.watching;
  if (rfb) rfb.viewOnly = !mine;
  if (!screenSocket && !rfb) return;
  if (mine) chip("mine", "wait");
  else if (human) chip("elsewhere", "wait");
  else chip("working");
}

// ---- control socket --------------------------------------------------------------------------
function connectControl() {
  if (fatal) return;
  const ws = new WebSocket(CONTROL_URL);
  control = ws;
  ws.onopen = () => ws.send(JSON.stringify({ init: TG.initData }));
  ws.onmessage = (event) => {
    let message = null;
    try { message = JSON.parse(event.data); } catch (e) { return; }
    onControl(message);
  };
  ws.onclose = (event) => {
    if (control === ws) control = null;
    clearText();
    if (fatal) return;
    // 4401 after this page was let in: the session hit its time limit or the initData went stale.
    if (event.code === 4401 && (authed || event.reason === "session expired")) { fail("expired", "off", "ended"); return; }
    if (event.code === 4401) { fail("unauthorized"); return; }
    if (event.code === 4403 || event.code === 1008) { fail("refused"); return; }
    if (!rfb) { chip("reconnecting", "off"); overlay(T.reconnecting, true); }
    setTimeout(connectControl, backoff(controlRetry++));
  };
}

function onControl(message) {
  if (message.type === "ready") {
    controlRetry = 0;
    authed = true;
    showCard(message.card);
    setLease(message.lease);
    if (!screenSocket) onTicket(message);
  } else if (message.type === "ticket") {
    if (!screenSocket) onTicket(message);
  } else if (message.type === "lease") {
    setLease(message.lease);
  }
}

function onTicket(message) {
  if (message.ticket) { connectScreen(message.ticket, message.path || "/api/display/ws"); return; }
  if (message.error === "screen_unavailable") {
    chip("off", "off");
    overlay(T.unavailable.replace("{detail}", message.detail || ""), false);
  }
  askTicket(backoff(screenRetry++) * 2);
}

function askTicket(delay) {
  clearTimeout(ticketTimer);
  ticketTimer = setTimeout(() => {
    if (!send({ op: "ticket" })) askTicket(1000);
  }, delay);
}

// ---- screen ----------------------------------------------------------------------------------
function connectScreen(ticket, path) {
  chip("connecting", "off");
  overlay(T.starting, true);
  const ws = new WebSocket(WS_BASE + path + "?display_ticket=" + encodeURIComponent(ticket));
  screenSocket = ws;
  ws.addEventListener("close", (event) => onScreenClosed(ws, event));
  try {
    rfb = new RFB($("screen"), ws, { shared: true });
  } catch (e) {
    console.warn("agent computer: viewer failed to start", e);
    screenSocket = null;
    rfb = null;
    askTicket(backoff(screenRetry++));
    return;
  }
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.clipViewport = false;
  rfb.showDotCursor = true;
  rfb.focusOnClick = false;
  rfb.qualityLevel = 6;
  rfb.compressionLevel = 2;
  rfb.viewOnly = !(lease && lease.mine);
  rfb.addEventListener("connect", () => {
    screenRetry = 0;
    overlay(null);
    setLease(lease);
  });
}

function onScreenClosed(ws, event) {
  if (screenSocket !== ws) return;
  screenSocket = null;
  rfb = null;
  clearText();
  if (fatal) return;
  if (event.code === 4403) { fail("refused"); return; }
  if (event.code === 4001) { chip("off", "off"); overlay(T.off, false); }
  else { chip("reconnecting", "off"); overlay(T.reconnecting, true); }
  // 4000 control-taken, 1006 dropped link, 4401 spent or expired ticket, 4001 screen gone: fresh ticket.
  askTicket(event.code === 4000 ? 200 : backoff(screenRetry++));
}

// ---- controls --------------------------------------------------------------------------------
function haptic() {
  try { TG.HapticFeedback.impactOccurred("light"); } catch (e) { /* optional */ }
}

$("take").addEventListener("click", () => {
  if (send({ op: "take" })) { $("take").disabled = true; haptic(); }
});
$("back").addEventListener("click", () => {
  clearText();
  if (send({ op: "back" })) { $("back").disabled = true; haptic(); }
});
$("reveal").addEventListener("click", () => mask($("text").type !== "password"));

function sendKeysym(keysym) {
  if (rfb && lease && lease.mine) rfb.sendKey(keysym, null);
}

$("kb").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("text").value;
  if (!text || !rfb || !(lease && lease.mine)) return;
  for (const ch of text) {
    sendKeysym(ch === "\n" ? KEYS.enter : keysyms.lookup(ch.codePointAt(0)));
  }
  clearText();
});
document.querySelectorAll("[data-key]").forEach((button) => {
  button.addEventListener("click", () => sendKeysym(KEYS[button.dataset.key]));
});

// ---- start -----------------------------------------------------------------------------------
if (!TG || !TG.initData) {
  fail("notTelegram", "off");
} else {
  TG.ready();
  TG.expand();
  if (TG.isVersionAtLeast && TG.isVersionAtLeast("7.7") && TG.disableVerticalSwipes) TG.disableVerticalSwipes();
  if (TG.isVersionAtLeast && TG.isVersionAtLeast("6.1")) {
    TG.setHeaderColor("bg_color");
    TG.setBackgroundColor("bg_color");
  }
  connectControl();
}
