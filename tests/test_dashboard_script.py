"""The dashboard's page script, run under node against the real "In the machine" markup.

There is no browser here, so a small DOM stands in for one (as in `test_money_chart`): just enough
HTML parsing, selectors and events for `dashboard.SCRIPT`. Like a browser, it drops focus when the
focused element leaves the page, which is what the 30-second refresh does to everything in <main>.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from vending import dashboard, visits

NOW = datetime(2026, 12, 9, 15, tzinfo=timezone.utc)
OURS = ("13", "15", "22", "27", "31", "46", "58")      # made up and scattered, for the markup only

_DOM = r'''
const VOID = new Set(["area","base","br","col","embed","hr","img","input","link","meta","source","track","wbr"]);
const RAW = new Set(["script","style"]);
class Node {
  constructor(tag, attrs) { this.tagName = tag.toUpperCase(); this.attrs = attrs || {}; this.children = [];
    this.parentNode = null; this.text = ""; this.scrollTop = 0; }
  get id() { return this.attrs.id || ""; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  get classList() { const c = (this.attrs["class"] || "").split(/\s+/).filter(Boolean);
    return {contains: x => c.includes(x)}; }
  get hidden() { return "hidden" in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ""; else delete this.attrs.hidden; }
  get open() { return "open" in this.attrs; }
  set open(v) { if (v) this.attrs.open = ""; else delete this.attrs.open; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(""); }
  set textContent(v) { this.children.forEach(c => c.parentNode = null); this.children = []; this.text = v; }
  appendChild(n) { n.parentNode = this; this.children.push(n); return n; }
  cloneNode() { const n = new Node(this.tagName, {...this.attrs}); n.text = this.text;
    this.children.forEach(c => n.appendChild(c.cloneNode())); return n; }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; }
  closest(sel) { for (let n = this; n && n.tagName; n = n.parentNode) if (matches(n, sel)) return n; return null; }
  matches(sel) { return matches(this, sel); }
  querySelectorAll(sel) { const out = []; const walk = n => { for (const c of n.children) {
    if (matches(c, sel, this)) out.push(c); walk(c); } }; walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  set innerHTML(html) {
    if (document.activeElement && document.activeElement !== this && this.contains(document.activeElement))
      document.activeElement = document.body;           // a browser drops focus from a removed element
    this.children.forEach(c => c.parentNode = null); this.children = [];
    parse(html, this);
  }
  get innerHTML() { return serialize(this); }
  focus() { if (document.contains(this)) document.activeElement = this; }
  blur() { document.activeElement = document.body; }
  scrollIntoView() { scrolled.push(this.id || this.attrs["class"]); }
  addEventListener() {}
}
function serialize(n) { return n.children.map(c => { const a = Object.entries(c.attrs).map(([k, v]) =>
  ` ${k}="${String(v).replace(/&/g, "&amp;").replace(/"/g, "&quot;")}"`).join("");
  const tag = c.tagName.toLowerCase(); return VOID.has(tag) ? `<${tag}${a}>` : `<${tag}${a}>${c.text}${serialize(c)}</${tag}>`; }).join(""); }
function unescape(s) { return s.replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/&amp;/g, "&"); }
function parse(html, root) {
  const re = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][\w:-]*)((?:\s+[^\s=>\/]+(?:="[^"]*"|='[^']*'|=[^\s>]*)?)*)\s*(\/?)>|([^<]+)/g;
  let cur = root, m;
  while ((m = re.exec(html))) {
    if (m[5] !== undefined) { cur.text += unescape(m[5]); continue; }
    if (!m[2]) continue;
    const tag = m[2].toLowerCase();
    if (m[1]) { for (let n = cur; n && n !== root.parentNode; n = n.parentNode)
      if (n.tagName === tag.toUpperCase()) { cur = n.parentNode; break; } continue; }
    const attrs = {}; const ar = /([^\s=>\/]+)(?:="([^"]*)"|='([^']*)'|=([^\s>]*))?/g; let a;
    while ((a = ar.exec(m[3]))) attrs[a[1]] = unescape(a[2] ?? a[3] ?? a[4] ?? "");
    const node = cur.appendChild(new Node(tag, attrs));
    if (RAW.has(tag)) { const end = html.indexOf(`</${tag}>`, re.lastIndex); node.text = html.slice(re.lastIndex, end);
      re.lastIndex = end + tag.length + 3; continue; }
    if (!VOID.has(tag) && !m[4]) cur = node;
  }
}
// Selectors: compounds of tag, #id, .class, [a], [a="v"], [a^="v"] and :not(compound), joined by spaces.
function compound(n, s) {
  let rest = s, m;
  const nots = []; rest = rest.replace(/:not\(([^)]*)\)/g, (_, x) => { nots.push(x); return ""; });
  if (nots.some(x => compound(n, x))) return false;
  if ((m = /^[a-zA-Z][\w-]*/.exec(rest))) { if (n.tagName !== m[0].toUpperCase()) return false; rest = rest.slice(m[0].length); }
  const parts = rest.match(/#[\w-]+|\.[\w-]+|\[[^\]]+\]/g) || [];
  return parts.every(p => {
    if (p[0] === "#") return n.id === p.slice(1);
    if (p[0] === ".") return n.classList.contains(p.slice(1));
    const am = /^\[([\w-]+)(?:(\^?=)"([^"]*)")?\]$/.exec(p);
    if (!am[2]) return n.hasAttribute(am[1]);
    const v = n.getAttribute(am[1]); return am[2] === "=" ? v === am[3] : v !== null && v.startsWith(am[3]);
  });
}
function matches(n, sel, scope) {
  return sel.split(",").some(one => {
    const steps = one.trim().split(/\s+/);
    if (!compound(n, steps[steps.length - 1])) return false;
    let i = steps.length - 2;
    for (let p = n.parentNode; i >= 0 && p && p !== scope; p = p.parentNode) if (p.tagName && compound(p, steps[i])) i--;
    return i < 0;
  });
}
const scrolled = [];
const handlers = {document: {}, window: {}};
const on = where => (type, fn) => { (handlers[where][type] ||= []).push(fn); };
const document = new Node("#document");
document.body = document.appendChild(new Node("body"));
document.activeElement = document.body;
document.hidden = false; document.title = "";
document.addEventListener = on("document");
document.getElementById = id => document.querySelector("#" + id);
const window = {addEventListener: on("window"), matchMedia: q => ({matches: q.includes("max-width") ? NARROW : false})};
const matchMedia = window.matchMedia;
const localStorage = {getItem: () => null, setItem() {}};
const location = {hash: "", pathname: "/", reload() {}};
const CSS = {escape: s => String(s)};
const intervals = [];
const setInterval = (fn, ms) => intervals.push({fn, ms});
class DOMParser { parseFromString(html) { const d = new Node("#doc"); parse(html, d); d.title = ""; return d; } }
const fetch = () => Promise.resolve({ok: true, redirected: false, text: () => Promise.resolve(PAGE)});
const fire = (type, target, extra) => { let stopped = false;
  const e = Object.assign({type, target, preventDefault() { stopped = true; }}, extra || {});
  (handlers.document[type] || []).forEach(fn => fn(e)); return stopped; };
const refresh = async () => { intervals.find(i => i.ms === 30000).fn(); await new Promise(r => setTimeout(r, 20)); };
const assert = (ok, message) => { if (!ok) throw Error(message); };
document.body.innerHTML = PAGE;
'''


def _main_html() -> str:
    """The real "In the machine" section, from a snapshot with nothing but our assignment in it."""
    snap = {"as_of": NOW, "ours": {"ok": True, "data": list(OURS)}, "plan": {"ok": True, "data": None},
            "sales": {"ok": True, "data": {"as_of": None, "stale": False, "window_start": None, "slots": {}}},
            "stock": {"ok": True, "data": {"slots": {}, "closet": [], "warnings": []}},
            "counts": {"ok": True, "data": {}}, "products": {"ok": False, "why": "made up"}}
    return f"<main>{dashboard._cabinet_html(snap, [])}</main>"


def run_script(tmp_path, checks: str, narrow: bool = False) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = dashboard.SCRIPT % (30, 0, dashboard.LIVE_STORAGE, visits.REFRESH_HEADER)    # the live page, not the demo
    prelude = f"const PAGE = {json.dumps(_main_html())};\nconst NARROW = {'true' if narrow else 'false'};\n"
    path = tmp_path / "page.js"
    path.write_text(prelude + _DOM + script + "\n(async () => {\n" + checks + "\n})().catch(e => "
                    "{ console.error(e.stack || e); process.exit(1); });\n", encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("narrow", [False, True], ids=["wide", "phone"])
def test_a_keyboard_pick_keeps_focus_through_the_refresh_and_escape_returns_it(tmp_path, narrow):
    run_script(tmp_path, r'''
      const vm = () => document.querySelector(".vm");
      const cell = document.querySelector('.fc[data-sel="15"]');
      cell.focus();                                       // Tab to the cell...
      fire("click", cell);                                // ...and press Enter: a link's Enter is a click
      assert(vm().getAttribute("data-sel") === "15", "the pick took");
      const before = document.activeElement.id;
      assert(before, "the focused element has an id: " + document.activeElement.tagName);
      await refresh();
      assert(vm().getAttribute("data-sel") === "15", "the pick survives the refresh");
      assert(document.activeElement.id === before, "focus after the refresh: " + document.activeElement.id);
      assert(document.activeElement !== cell, "and it is on the new element, not the removed one");
      fire("keydown", document.activeElement, {key: "Escape"});
      assert(!vm().hasAttribute("data-sel"), "Escape closes the close-up");
      assert(document.activeElement === document.querySelector('.fc[data-sel="15"]'),
             "focus is back on the opener: " + (document.activeElement.id || document.activeElement.tagName));
    ''', narrow=narrow)
