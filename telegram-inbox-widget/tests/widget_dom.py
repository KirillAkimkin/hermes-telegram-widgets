"""Run a card's own script in Node against a small DOM: what the user sees and what a tap sends.

``run_widget(markup, chat_data, steps, look)`` parses the card HTML, runs its ``<script>`` with a fake
``TelegramWidget`` (chatData, onEvent, request, openLink, openTelegramLink) and returns, for the first
render and after every step, the state of the elements matched by each selector in ``look``:
``{"text", "disabled", "shown", "cls"}``. ``shown`` honours inline ``display`` and ``display:none`` rules
of the card's own stylesheet; hidden elements and disabled buttons ignore clicks, as for a user's tap.
Steps: ``{"click": selector, "index": k}`` or ``{"chat": chat_data}`` (sets chatData and fires
chatDataChanged). ``answers`` are the bot's replies to successive requests: a dict (sent back as JSON)
or ``{"error": code}`` (the request fails). Skips when Node is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

_VOID = {"br", "img", "input", "meta", "hr", "link", "wbr"}

_HARNESS = r"""
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
function parse(c) {
  const m = c.match(/^([a-zA-Z]*)((?:[#.][\w-]+)*)$/);
  if (!m || !c) throw new Error("unsupported selector: " + c);
  const parts = m[2].match(/[#.][\w-]+/g) || [];
  return {tag: m[1].toUpperCase(), ids: parts.filter(p => p[0] === "#").map(p => p.slice(1)),
          cls: parts.filter(p => p[0] === ".").map(p => p.slice(1))};
}
const chains = sel => sel.split(",").map(s => s.trim().split(/\s+/).map(parse));
function hit(el, c) {
  return (!c.tag || el.tagName === c.tag) && c.ids.every(i => el.id === i) && c.cls.every(k => el.classList.contains(k));
}
function matchChain(el, chain) {
  if (!hit(el, chain[chain.length - 1])) return false;
  let i = chain.length - 2;
  for (let a = el.parentNode; a && i >= 0; a = a.parentNode) if (hit(a, chain[i])) i--;
  return i < 0;
}
class El {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.parentNode = null; this.childNodes = [];
    this.listeners = {}; this.style = {}; this.dataset = {}; this.disabled = "disabled" in attrs;
    for (const decl of (attrs.style || "").split(";")) {
      const at = decl.indexOf(":");
      if (at > 0) this.style[decl.slice(0, at).trim()] = decl.slice(at + 1).trim();
    }
    for (const [k, v] of Object.entries(attrs))
      if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
    const el = this;
    this.classList = {
      list() { return (el.attrs.class || "").split(/\s+/).filter(Boolean); },
      contains(c) { return this.list().includes(c); },
      add(...cs) { const l = this.list(); for (const c of cs) if (!l.includes(c)) l.push(c); el.attrs.class = l.join(" "); },
      remove(...cs) { el.attrs.class = this.list().filter(x => !cs.includes(x)).join(" "); },
      toggle(c, force) { const on = force === undefined ? !this.contains(c) : !!force; on ? this.add(c) : this.remove(c); return on; },
    };
  }
  get id() { return this.attrs.id || ""; }
  get className() { return this.attrs.class || ""; }
  set className(v) { this.attrs.class = String(v); }
  get children() { return this.childNodes.filter(n => typeof n !== "string"); }
  get textContent() { return this.childNodes.map(n => typeof n === "string" ? n : n.textContent).join(""); }
  set textContent(v) { this.childNodes = [String(v)]; }
  set innerHTML(v) { if (v !== "") throw new Error("this harness only clears innerHTML"); this.childNodes = []; }
  appendChild(c) { if (typeof c !== "string") c.parentNode = this; this.childNodes.push(c); return c; }
  addEventListener(type, f) { (this.listeners[type] = this.listeners[type] || []).push(f); }
  click() {
    if ((this.tagName === "BUTTON" && this.disabled) || !shown(this)) return;
    let stopped = false;
    const ev = {target: this, stopPropagation() { stopped = true; }, preventDefault() {}};
    for (let n = this; n && !stopped; n = n.parentNode) for (const f of n.listeners.click || []) f.call(n, ev);
  }
  querySelectorAll(sel) {
    const cs = chains(sel), out = [];
    const walk = n => { for (const k of n.children) { if (cs.some(ch => matchChain(k, ch))) out.push(k); walk(k); } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function build(node) {
  const el = new El(node.tag, node.attrs);
  for (const k of node.kids) el.appendChild(typeof k === "string" ? k : build(k));
  return el;
}
const body = build(input.tree);
const hiddenByCss = [];
for (const css of input.styles)
  for (const m of css.matchAll(/([^{}]+)\{([^}]*)\}/g))
    if (/(^|;)\s*display\s*:\s*none/.test(m[2]))
      for (const s of m[1].split(",")) { try { hiddenByCss.push(chains(s.trim())[0]); } catch (e) {} }
function shown(el) {
  for (let n = el; n; n = n.parentNode) {
    const d = n.style.display;
    if (d === "none" || (!d && hiddenByCss.some(ch => matchChain(n, ch)))) return false;
  }
  return true;
}
const log = {requests: [], links: [], tg_links: []}, handlers = {}, answers = input.answers.slice();
globalThis.window = globalThis;
globalThis.document = {body, getElementById: id => body.querySelector("#" + id), createElement: t => new El(t),
                       querySelectorAll: s => body.querySelectorAll(s), querySelector: s => body.querySelector(s)};
globalThis.TelegramWidget = {
  chatData: input.chatData,
  onEvent(name, f) { (handlers[name] = handlers[name] || []).push(f); },
  request(d) {
    log.requests.push(d);
    const a = answers.shift() || {};
    return a.error ? Promise.reject({code: a.error}) : Promise.resolve({result: JSON.stringify(a)});
  },
  openLink(u) { log.links.push(u); },
  openTelegramLink(u) { log.tg_links.push(u); },
};
const snap = () => Object.fromEntries(input.look.map(s => [s, body.querySelectorAll(s).map(el => ({
  text: el.textContent, disabled: !!el.disabled, shown: shown(el), cls: el.className}))]));
(async () => {
  for (const code of input.scripts) (0, eval)(code);
  const frames = [snap()];
  for (const step of input.steps) {
    if ("chat" in step) {
      TelegramWidget.chatData = step.chat;
      for (const f of handlers.chatDataChanged || []) f();
    } else {
      body.querySelectorAll(step.click)[step.index || 0].click();
    }
    await new Promise(r => setTimeout(r, 0));
    frames.push(snap());
  }
  process.stdout.write(JSON.stringify({...log, frames}));
})().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: dict = {"tag": "body", "attrs": {}, "kids": []}
        self.stack = [self.root]
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self._raw: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._raw = self.scripts if tag == "script" else self.styles
            self._raw.append("")
            return
        node = {"tag": tag, "attrs": {k: "" if v is None else v for k, v in attrs}, "kids": []}
        self.stack[-1]["kids"].append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._raw = None
            return
        while len(self.stack) > 1 and tag not in _VOID:
            if self.stack.pop()["tag"] == tag:
                break

    def handle_data(self, data):
        if self._raw is not None:
            self._raw[-1] += data
        elif data:
            self.stack[-1]["kids"].append(data)


def run_widget(markup: str, chat_data: str | None = None, steps=(), look=(), answers=()) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is needed to run the widget script")
    tree = _Tree()
    tree.feed(markup)
    tree.close()
    payload = {"tree": tree.root, "scripts": tree.scripts, "styles": tree.styles, "chatData": chat_data,
               "steps": list(steps), "look": list(look), "answers": list(answers)}
    done = subprocess.run([node, "-e", _HARNESS], input=json.dumps(payload), capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)
