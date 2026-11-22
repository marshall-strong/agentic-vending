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
  get className() { return this.attrs["class"] || ""; }
  set className(v) { this.attrs["class"] = String(v); }
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
  get outerHTML() { const tag = this.tagName.toLowerCase();
    const a = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${String(v).replace(/"/g, "&quot;")}"`).join("");
    return `<${tag}${a}>${this.text}${serialize(this)}</${tag}>`; }
  get firstChild() { return this.children[0] || null; }
  insertBefore(n, ref) { const i = ref ? this.children.indexOf(ref) : this.children.length;
    this.children.splice(i < 0 ? this.children.length : i, 0, n); n.parentNode = this; return n; }
  focus() { if (document.contains(this)) document.activeElement = this; }
  blur() { document.activeElement = document.body; }
  remove() { const p = this.parentNode, i = p ? p.children.indexOf(this) : -1;
    if (i >= 0) p.children.splice(i, 1); this.parentNode = null; }
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
document.createElement = tag => new Node(tag);
document.getElementById = id => document.querySelector("#" + id);
// A media query against the viewport's WIDTH: every (min-width) and (max-width) in it must hold.
const media = q => [...q.matchAll(/\((min|max)-width:\s*(\d+)px\)/g)].every(([, k, n]) =>
  k === "min" ? WIDTH >= +n : WIDTH <= +n) && !/prefers-reduced-motion/.test(q);
const window = {addEventListener: on("window"), matchMedia: q => ({matches: media(q)})};
const matchMedia = window.matchMedia;
const localStorage = {getItem: () => null, setItem() {}};
const location = {hash: HASH, pathname: "/", reload() {}};
const CSS = {escape: s => String(s)};
const intervals = [];
const setInterval = (fn, ms) => intervals.push({fn, ms});
class DOMParser { parseFromString(html) { const d = new Node("#doc"); parse(html, d); d.title = ""; return d; } }
// A test sets RESP to feed the refresh handler a fetch answer of its own ({ok, status, redirected,
// text}); with it null, the refresh reads the page itself, like the stub's default below.
let RESP = null;
const fetch = () => (RESP ? RESP() : Promise.resolve({ok: true, redirected: false, text: () => Promise.resolve(PAGE)}));
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


def run_script(tmp_path, checks: str, width: int = 1280, hash: str = "") -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = dashboard.SCRIPT % (30, 0, dashboard.LIVE_STORAGE, visits.REFRESH_HEADER)    # the live page, not the demo
    prelude = (f"const PAGE = {json.dumps(_main_html())};\nconst WIDTH = {int(width)};\n"
               f"const HASH = {json.dumps(hash)};\n")
    path = tmp_path / "page.js"
    path.write_text(prelude + _DOM + script + "\n(async () => {\n" + checks + "\n})().catch(e => "
                    "{ console.error(e.stack || e); process.exit(1); });\n", encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("width", [1280, 900, 390], ids=["wide", "tablet", "phone"])
def test_a_keyboard_pick_keeps_focus_through_the_refresh_and_escape_returns_it(tmp_path, width):
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
    ''', width=width)


@pytest.mark.parametrize("width, shown", [(1280, False), (979, True), (900, True), (820, True), (390, False)])
def test_between_the_phone_and_desktop_layouts_a_pick_scrolls_the_close_up_into_view(tmp_path, width, shown):
    # Wide, the close-up sits beside the front; on a phone it is a sheet. In between it sits below the
    # front in one column, so a pick must bring it into view.
    run_script(tmp_path, f'''
      fire("click", document.querySelector('.fc[data-sel="46"]'));
      assert(document.querySelector(".vm").getAttribute("data-sel") === "46", "the pick took");
      const panel = scrolled.includes("vm-panel");
      assert(panel === {'true' if shown else 'false'}, "close-up scrolled into view: " + JSON.stringify(scrolled));
    ''', width=width)


@pytest.mark.parametrize("width", [1280, 390], ids=["wide", "phone"])
def test_escape_falls_back_to_the_front_cell_then_the_heading_when_the_opener_is_gone(tmp_path, width):
    run_script(tmp_path, r'''
      // A plan-change row opens 15. The next refresh no longer has that row, but 15 is still ours.
      const row = new Node("a", {"class": "chg", id: "chg-15", href: "?sel=15#sel-15", "data-sel": "15"});
      document.querySelector(".ops-home").appendChild(row);
      row.focus();
      fire("click", row);
      assert(document.querySelector(".vm").getAttribute("data-sel") === "15", "the pick took");
      await refresh();
      assert(!document.getElementById("chg-15"), "the row is gone after the refresh");
      fire("keydown", document.activeElement, {key: "Escape"});
      assert(!document.querySelector(".vm").hasAttribute("data-sel"), "Escape closed it");
      assert(document.activeElement.id === "fc-15", "focus falls back to 15 on the front: " + document.activeElement.id);

      // Opened again from the row; this time the front cell is gone too: focus goes to the heading.
      document.querySelector(".ops-home").appendChild(row);
      fire("click", row);
      const cell = document.getElementById("fc-15");
      cell.parentNode.children.splice(cell.parentNode.children.indexOf(cell), 1); cell.parentNode = null;
      row.parentNode.children.splice(row.parentNode.children.indexOf(row), 1); row.parentNode = null;
      fire("keydown", document.body, {key: "Escape"});
      assert(document.activeElement.id === "vm-title", "focus falls back to the heading: " + document.activeElement.id);
    ''', width=width)


@pytest.mark.parametrize("width, lands", [(900, "vm-panel"), (820, "vm-panel"), (1280, "vm")])
def test_a_direct_pick_link_lands_on_the_close_up_at_tablet_width(tmp_path, width, lands):
    # Opened as /dashboard?sel=46#sel-46: in the one-column tablet layout the page must end on the
    # close-up, not be scrolled back to the machine's top by the start-up timer. Wide, the close-up is
    # beside the front, so the top of the machine is the right place to land.
    run_script(tmp_path, f'''
      assert(document.querySelector(".vm").getAttribute("data-sel") === "46", "the link picked 46");
      await new Promise(r => setTimeout(r, 150));      // past the start-up timer
      assert(scrolled[scrolled.length - 1] === "{lands}", "landed on: " + JSON.stringify(scrolled));
    ''', width=width, hash="#sel-46")


def test_an_outage_swaps_only_the_sync_line_and_an_unmarked_error_keeps_the_rest(tmp_path):
    """The refresh handler's outage behaviour, run under the same DOM stand-in: an outage swaps
    only the sync line in - the one line the worker can name without the database - and leaves
    the run and tracking lines it cannot check, until a refresh that read the database replaces
    the page. A marked outage with a recovered sync removes only the sync line; an error page
    without the marker (a proxy's 502, a 500 of ours) changes nothing."""
    at = NOW
    seed = dashboard._alerts_html([
        {"kind": "sync_failed", "at": at, "text": "`sync_history` exit 3"},
        {"kind": "run_failed", "at": at, "text": "Run #9 failed. See Runs below."},
        {"kind": "tracking_outside", "at": at,
         "text": "The bridge's tracking check said cash outside"}])
    failing = dashboard.outage_page(
        "Can't read the database: the server closed the connection",
        dashboard.outage_alerts({"rounds": 1, "last_round_at": at.isoformat(),
                                 "results": [{"job": "sync_history", "exit_code": 9,
                                              "outcome": "finished"}]}), outage=True)
    recovered = dashboard.outage_page(
        "Can't read the database: the server closed the connection",
        dashboard.outage_alerts({"rounds": 1, "last_round_at": at.isoformat(),
                                 "results": [{"job": "sync_history", "exit_code": 0,
                                              "outcome": "finished"}]}), outage=True)
    # The page's own generic 500 (a bug drawing it), as the pages answer it, even with a failing round.
    from vending import pages
    broken = pages._problem(pages.DASHBOARD, 500, pages.BROKEN, syncs={
        "rounds": 1, "last_round_at": at.isoformat(),
        "results": [{"job": "sync_history", "exit_code": 9, "outcome": "finished"}]}).body
    run_script(tmp_path, f'''
      const SEED = {json.dumps(seed)};
      const FAILING = {json.dumps(failing)};
      const RECOVERED = {json.dumps(recovered)};
      const BROKEN = {json.dumps(broken)};
      var main = document.querySelector("main");
      const clean = main.innerHTML;          // the page without any banner
      const serve = (ok, status, html) => {{ RESP = () => Promise.resolve(
        {{ok, status, redirected: false, text: () => Promise.resolve(html)}}); }};
      const kinds = () => Array.prototype.map.call(
        document.querySelectorAll("main .failbanner [data-kind]"),
        n => n.getAttribute("data-kind")).join(",");
      const reseed = () => {{ main = document.querySelector("main"); main.innerHTML = SEED + clean; }};

      // The page as last drawn carries all three lines: sync, run and tracking.
      reseed();
      assert(kinds() === "sync,run,tracking", "the page starts with all three: " + kinds());

      // An outage whose sync still failed: only the sync line is swapped in, the rest stays.
      serve(false, 503, FAILING);
      await refresh();
      var box = document.querySelector("main .failbanner");
      assert(box.textContent.indexOf("exit 9") >= 0, "the outage sync line was swapped in");
      assert(kinds() === "sync,run,tracking", "one line each, still three: " + kinds());
      assert(document.querySelector("main .vm"), "the rest of the page survived the swap");

      // Recovered sync, still no database: only the sync line goes; run and tracking stay.
      serve(false, 503, RECOVERED);
      await refresh();
      box = document.querySelector("main .failbanner");
      assert(kinds() === "run,tracking", "only the sync line was cleared: " + kinds());
      assert(box.textContent.indexOf("Run #9") >= 0 && box.textContent.indexOf("cash outside") >= 0,
             "the lines the outage cannot check stayed: " + box.textContent);

      // A database-backed refresh replaces the page: the run and tracking lines go with it.
      RESP = null;
      await refresh();
      assert(!document.querySelector("main .failbanner"), "the real refresh cleared them");

      // Another error page without the marker (a proxy's): what is shown stays.
      reseed();
      serve(false, 502, "<html><body><h1>502 Bad Gateway</h1></body></html>");
      await refresh();
      assert(kinds() === "sync,run,tracking", "an unmarked error page keeps what is shown: " + kinds());

      // A generic 500 of ours is not the outage either: no marker, nothing changes.
      serve(false, 500, BROKEN);
      await refresh();
      assert(kinds() === "sync,run,tracking", "a 500 without the marker changes nothing: " + kinds());
      box = document.querySelector("main .failbanner");
      assert(box.textContent.indexOf("exit 3") >= 0 && box.textContent.indexOf("exit 9") < 0,
             "the 500's own sync line was not swapped in: " + box.textContent);

      // The outage page never takes the page down: a fetch that throws is the old failed-refresh path.
      RESP = () => Promise.reject(new Error("gone"));
      await refresh();
      assert(document.querySelector("main .failbanner"), "a failed fetch keeps the last page");
      RESP = null;
    ''')


def test_an_outage_puts_the_banner_on_a_page_without_one(tmp_path):
    """The outage test above starts from a page that already carries a banner, so the handler's
    first-banner insertion was never exercised. From a healthy page with no banner, the real
    failed-round 503 body (`outage_page` over the worker's own failed round) must make the alert
    appear - and leave the rest of the page alone."""
    from vending import worker
    state = worker.State()
    state.results = worker.run_round([("sync_history", lambda: 3)], log=lambda _: None)
    state.rounds, state.last_round_at = 1, NOW.isoformat()
    body = dashboard.outage_page("Can't read the database: connection refused",
                                 dashboard.outage_alerts(state.public()), outage=True)
    assert 'data-sync-ok="1"' in body   # the handler only reads a marked outage reply
    run_script(tmp_path, f'''
      const OUTAGE = {json.dumps(body)};
      const serve = (ok, status, html) => {{ RESP = () => Promise.resolve(
        {{ok, status, redirected: false, text: () => Promise.resolve(html)}}); }};
      assert(!document.querySelector("main .failbanner"), "the healthy page starts with no banner");
      serve(false, 503, OUTAGE);
      await refresh();
      const box = document.querySelector("main .failbanner");
      assert(box, "the outage put the failure banner on the page");
      assert(box.textContent.indexOf("sync_history") >= 0 && box.textContent.indexOf("exit 3") >= 0,
             "the alert names the failed job: " + box.textContent);
      assert(document.querySelector("main .vm"), "the rest of the page survived");
      // The sync line goes back into the first position, where the page draws it.
      assert(document.querySelector('main .failbanner [data-kind="sync"]') === box.children[0],
             "the sync line is the banner's first line");
      RESP = null;
    ''')
