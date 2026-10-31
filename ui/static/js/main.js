// Boot: wires the shell (top bar, rail, inspector, palette, keys) to the live recorder and views.
import { app, prefs } from "./app.js";
import * as api from "./api.js";
import * as auth from "./auth.js";
import * as router from "./router.js";
import * as keys from "./keys.js";
import { createLive } from "./live.js";
import { localStore } from "./storage.js";
import { html, icon, setHTML, setText } from "./html.js";
import { age, ago, clock, duration, inTime, parseTime, plural, slotName, slotState, stateWord } from "./format.js";
import { bindActions, forgetNode, resetNode } from "./actions.js";
import { createInspector } from "./ui/inspector.js";
import { createPalette } from "./ui/palette.js";
import { dismissible } from "./ui/dialog.js";
import { copy, hideTip } from "./ui/toast.js";
import { createOverview, attentionItems, verdict } from "./views/overview.js";
import { createSessions } from "./views/sessions.js";
import { createActivity } from "./views/activity.js";
import { createAccess } from "./views/access.js";

const $ = (id) => document.getElementById(id);
const root = document.documentElement;

// ---- live data ----------------------------------------------------------------
const live = createLive({ load: api.status, storage: localStore });
app.live = live;

// ---- views ------------------------------------------------------------------------
const views = {
  overview: createOverview($("view-overview")),
  activity: createActivity($("view-activity")),
  sessions: createSessions($("view-sessions")),
  access: createAccess($("view-access")),
};
const inspector = createInspector({ onClose: () => closeInspector() });
let route = router.state();
let lastMsg = { data: null, events: [], recorder: live.recorder, live };

function closeInspector() {
  if (!inspector.isOpen()) return false;
  router.go(router.viewHash(), { replace: true });
  return true;
}

app.inspect = (type, id, opener) => {
  if (opener) inspector.open(type, id, opener); // remember who opened it before the route changes
  router.go(router.inspectHash(type, id), { replace: inspector.isOpen() && !!route.inspect });
};
app.closeInspector = closeInspector;
app.go = (hash) => router.go(hash);

function showView(view) {
  router.VIEWS.forEach((v) => { $("view-" + v).hidden = v !== view; });
  document.querySelectorAll("[data-nav-view]").forEach((a) => {
    if (a.dataset.navView === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (view === "access") views.access.show(); else views.access.hide();
  document.title = { overview: "Playwright Grid", activity: "Activity · Playwright Grid", sessions: "Sessions · Playwright Grid", access: "Access · Playwright Grid" }[view];
}

router.onRoute((r) => {
  const viewChanged = r.view !== route.view;
  route = r;
  showView(r.view);
  if (r.inspect) {
    const type = r.inspect.type, id = type === "node" ? r.inspect.id : Number(r.inspect.id);
    inspector.open(type, id, null);
  } else {
    inspector.close({ restore: !viewChanged });
  }
  renderViews(lastMsg);
  if (viewChanged && !r.inspect) {
    const h1 = document.querySelector("#view-" + r.view + " h1");
    if (h1 && document.activeElement && !document.activeElement.closest(".rail, .tabbar")) window.scrollTo(0, 0);
  }
});

function renderViews(msg) {
  const v = views.overview.update(msg);
  if (route.view === "sessions" || !$("view-sessions").hidden) views.sessions.update(msg.data, route.params);
  if (route.view === "activity") views.activity.update(msg.data, route.params, msg.recorder);
  views.access.update(msg.data);
  inspector.render();
  renderTop(msg, v);
  tick(); // fill ages and countdowns in anything just rendered, not a second later
}

// ---- top bar -----------------------------------------------------------------------
function renderTop(msg, v) {
  const d = msg.data;
  const chip = $("verdict-chip");
  chip.className = "verdict-chip t-" + v.tone;
  setText($("verdict-chip-text"), v.chip);
  chip.setAttribute("aria-label", "Grid status: " + v.h1 + ". Show what needs you.");
  setText($("capacity"), d && d.totals.slots ? d.totals.available + " free / " + d.totals.slots + " · " + d.totals.busy + " running" : d && !live.error ? "No slots yet" : "");
  setText($("grid-name"), (d && d.grid_name) || location.host);
  const n = d ? d.sessions.length : 0;
  document.querySelectorAll("[data-session-count]").forEach((el) => { setText(el, String(n)); el.hidden = !n; });
  if (d) {
    const names = $("node-names");
    setHTML(names, html`${d.nodes.filter((x) => x.kind === "gateway").map((x) => html`<option value="${x.host.toLowerCase()}"></option>`)}`);
  }
  freshness();
}

// Which hop is down, in the freshness chip's few words.
const why = () => (live.errorKind === "database" ? "database unreachable" : "Grid UI server unreachable");

function freshness() {
  const el = $("freshness"), text = $("fresh-text");
  const age = live.lastOk ? Math.max(0, Math.round((Date.now() - live.lastOk) / 1000)) : null;
  let tone = "ok", label;
  if (live.paused) {
    tone = "paused";
    label = "Paused" + (age != null ? " · " + duration(age) + " old" : "");
  } else if (age == null) {
    tone = live.error ? "bad" : "wait";
    label = live.error ? "No data · " + why() : "Connecting…";
  } else {
    label = "Live · " + (age < 1 ? "now" : age + "s");
    if (age >= 30) { tone = "bad"; label = "Stale · " + duration(age) + " · " + (live.error ? why() : "no response"); }
    else if (age >= 10) { tone = "warn"; label = "Delayed · " + age + "s" + (live.error ? " · " + why() : ""); }
  }
  el.className = "freshness f-" + tone;
  setText(text, label);
  el.title = live.error ? "Last error: " + live.error : live.lastOk ? "Updated " + clock(live.lastOk) : "";
}

$("pause-btn").addEventListener("click", togglePause);
function togglePause() {
  live.setPaused(!live.paused);
  const btn = $("pause-btn");
  btn.setAttribute("aria-pressed", String(live.paused));
  btn.setAttribute("aria-label", live.paused ? "Resume live updates" : "Pause live updates");
  btn.title = (live.paused ? "Resume" : "Pause") + " live updates (.)";
  btn.querySelector("use").setAttribute("href", live.paused ? "#i-play" : "#i-pause");
  freshness();
}

$("verdict-chip").addEventListener("click", () => {
  const focusRail = () => {
    const att = $("attention");
    att.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    att.focus({ preventScroll: true });
  };
  if (route.view !== "overview" || route.inspect) { router.go("#/overview"); setTimeout(focusRail, 0); } else focusRail();
});

// ---- account ---------------------------------------------------------------------
const accountBtn = $("account-btn"), accountMenu = $("account-menu");
function renderAccount() {
  const s = auth.state();
  accountBtn.classList.toggle("signed-in", s.signedIn);
  accountBtn.hidden = !s.known;
  if (!s.adminConfigured) {
    setHTML(accountBtn, html`${icon("i-user")}<span class="acct-label">Open access</span>${icon("i-caret", "caret")}`);
    accountBtn.setAttribute("aria-haspopup", "menu");
  } else if (s.signedIn) {
    setHTML(accountBtn, html`<span class="acct-dot" aria-hidden="true"></span><span class="acct-label">${s.name || "Admin"}</span>${icon("i-caret", "caret")}`);
    accountBtn.setAttribute("aria-haspopup", "menu");
  } else {
    setHTML(accountBtn, html`${icon("i-user")}<span class="acct-label">Sign in</span>`);
    accountBtn.removeAttribute("aria-haspopup");
  }
  $("unprotected-btn").hidden = !(s.known && !s.adminConfigured);
  document.body.classList.toggle("signed-in", s.signedIn);
}
function openAccountMenu() {
  const s = auth.state();
  const compact = root.dataset.density === "compact";
  setHTML(accountMenu, html`
    ${s.signedIn ? html`<p class="menu-note" role="none"><span class="acct-dot" aria-hidden="true"></span>${s.summary}</p>`
      : html`<p class="menu-note" role="none">Admin sign-in isn't configured: anyone who can open this page can manage the grid.</p>`}
    <button type="button" role="menuitemcheckbox" aria-checked="${compact}" data-menu="density">Compact rows</button>
    ${s.signedIn ? html`<button type="button" role="menuitem" data-menu="signout">${icon("i-logout")}Sign out</button>` : ""}`);
  openMenu(accountMenu, accountBtn);
}
accountBtn.addEventListener("click", () => {
  const s = auth.state();
  if (s.adminConfigured && !s.signedIn) { auth.signIn(); return; }
  if (!accountMenu.hidden) closeMenus(); else openAccountMenu();
});
accountMenu.addEventListener("click", (e) => {
  const item = e.target.closest("[data-menu]");
  if (!item) return;
  if (item.dataset.menu === "signout") { auth.signOut(); closeMenus(); accountBtn.focus(); }
  if (item.dataset.menu === "density") { toggleDensity(); item.setAttribute("aria-checked", String(root.dataset.density === "compact")); }
});
auth.onChange(() => { renderAccount(); renderViews(lastMsg); });

$("unprotected-btn").addEventListener("click", () => {
  const pop = $("unprotected-pop"), btn = $("unprotected-btn");
  const open = pop.hidden;
  pop.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
});

function toggleDensity() {
  const next = root.dataset.density === "compact" ? "comfortable" : "compact";
  root.dataset.density = next;
  prefs.set("gridDensity", next);
  document.querySelectorAll("[data-density-toggle]").forEach((b) => b.setAttribute("aria-pressed", String(next === "compact")));
}
document.querySelectorAll("[data-density-toggle]").forEach((b) => {
  b.setAttribute("aria-pressed", String(root.dataset.density === "compact"));
  b.addEventListener("click", toggleDensity);
});

// ---- menus (account, row actions) ---------------------------------------------------
let menuOwner = null;
function openMenu(menu, owner) {
  closeMenus();
  menu.hidden = false;
  menuOwner = owner;
  owner.setAttribute("aria-expanded", "true");
  const r = owner.getBoundingClientRect();
  menu.style.setProperty("--menu-top", Math.round(r.bottom + 4) + "px");
  menu.style.setProperty("--menu-right", Math.max(8, Math.round(window.innerWidth - r.right)) + "px");
  const first = menu.querySelector("[role^=menuitem]");
  if (first) first.focus();
}
function closeMenus(restore) {
  document.querySelectorAll(".menu").forEach((m) => { m.hidden = true; });
  if (menuOwner) {
    menuOwner.setAttribute("aria-expanded", "false");
    if (restore) menuOwner.focus();
  }
  menuOwner = null;
}
document.addEventListener("click", (e) => {
  if (menuOwner && !e.target.closest(".menu") && !menuOwner.contains(e.target)) closeMenus();
  const pop = $("unprotected-pop");
  if (!pop.hidden && !e.target.closest("#unprotected-pop, #unprotected-btn")) { pop.hidden = true; $("unprotected-btn").setAttribute("aria-expanded", "false"); }
});
document.addEventListener("keydown", (e) => {
  if (!menuOwner) return;
  const menu = document.querySelector(".menu:not([hidden])");
  const items = Array.from(menu.querySelectorAll("[role^=menuitem]"));
  const i = items.indexOf(document.activeElement);
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenus(true); }
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const next = items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
    if (next) next.focus();
  } else if (e.key === "Tab") closeMenus();
}, true);

const rowMenu = $("row-menu");
app.rowMenu = (host, btn) => {
  if (menuOwner === btn) { closeMenus(); return; }
  const n = app.findNode(host);
  if (!n) return;
  const hasFails = n.slots.some((s) => s.fails);
  const offline = n.kind === "gateway" && !n.online;
  const locked = auth.state().adminConfigured && !auth.state().signedIn;
  const lock = locked ? icon("i-lock") : "";
  setHTML(rowMenu, html`
    <button type="button" role="menuitem" data-row-act="inspect">${icon("i-panel")}Inspect ${host}</button>
    <button type="button" role="menuitem" data-row-act="activity">${icon("i-activity")}Activity for this node</button>
    <button type="button" role="menuitem" data-row-act="copy">${icon("i-copy")}Copy endpoint prefix</button>
    ${hasFails ? html`<button type="button" role="menuitem" data-row-act="reset"${locked ? html` aria-label="Sign in to reset node failures"` : ""}>${lock || icon("i-undo")}Reset node failures</button>` : ""}
    ${offline && !n.busy ? html`<button type="button" role="menuitem" class="destructive" data-row-act="forget"${locked ? html` aria-label="Sign in to forget ${host}"` : ""}>${lock || icon("i-trash")}Forget…</button>` : ""}`);
  rowMenu.dataset.host = host;
  openMenu(rowMenu, btn);
};
rowMenu.addEventListener("click", async (e) => {
  const item = e.target.closest("[data-row-act]");
  if (!item) return;
  const host = rowMenu.dataset.host, owner = menuOwner, n = app.findNode(host);
  closeMenus();
  switch (item.dataset.rowAct) {
    case "inspect": app.inspect("node", host, owner); break;
    case "activity": router.go(router.build("activity", { node: host })); break;
    case "copy":
      copy(n && n.kind === "gateway" ? app.hub() + "/connect/" + encodeURIComponent(host) + "/" : "ws://" + host + ":", "the endpoint prefix");
      if (owner) owner.focus();
      break;
    case "reset": await resetNode(host); if (owner && owner.isConnected) owner.focus(); break;
    case "forget": if (await forgetNode(host)) { app.lastActionAt = Date.now(); $("attention").focus(); } else if (owner && owner.isConnected) owner.focus(); break;
  }
});

// ---- rail --------------------------------------------------------------------------
$("rail-toggle").addEventListener("click", () => {
  const collapsed = root.dataset.rail !== "collapsed";
  root.dataset.rail = collapsed ? "collapsed" : "open";
  prefs.set("gridRail", root.dataset.rail);
  $("rail-toggle").setAttribute("aria-expanded", String(!collapsed));
});
$("rail-toggle").setAttribute("aria-expanded", String(root.dataset.rail !== "collapsed"));

// ---- theme ---------------------------------------------------------------------------
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const currentTheme = () => root.dataset.theme || (darkQuery.matches ? "dark" : "light");
function syncTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  const btn = $("theme-btn");
  btn.setAttribute("aria-label", "Switch to " + next + " theme");
  btn.title = "Switch to " + next + " theme";
  btn.querySelector("use").setAttribute("href", next === "dark" ? "#i-moon" : "#i-sun");
}
$("theme-btn").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  prefs.set("gridTheme", next);
  syncTheme();
});
darkQuery.addEventListener("change", syncTheme);
syncTheme();

// ---- palette ---------------------------------------------------------------------------
const palette = createPalette({
  items() {
    const d = app.data(), out = [];
    const view = (id, label, hash, ic) => out.push({ id: "view-" + id, group: "View", label, icon: ic, run: () => router.go(hash) });
    view("overview", "Overview", "#/overview", "i-grid");
    view("activity", "Activity", "#/activity", "i-activity");
    view("sessions", "Sessions", "#/sessions", "i-sessions");
    view("stale", "Stale sessions", "#/sessions?filter=stale", "i-sessions");
    view("access", "Access tokens", "#/access", "i-key");
    out.push({ id: "help", group: "View", label: "Keyboard shortcuts", icon: "i-keyboard", run: () => $("shortcuts-dialog").showModal() });
    for (const n of (d && d.nodes) || []) {
      out.push({ id: "node-" + n.host, group: "Nodes", label: n.host, mono: true, icon: "i-node", keywords: [n.platform, n.remote_address, n.status].join(" "),
        meta: n.busy + "/" + n.total + " busy · " + n.status.toLowerCase(), run: () => app.inspect("node", n.host, null) });
    }
    for (const n of (d && d.nodes) || []) {
      for (const s of n.slots) {
        out.push({ id: "slot-" + s.id, group: "Slots", label: slotName(n, s), mono: true, icon: "i-slot", slot: { node: n.host, port: s.port },
          keywords: n.host + " " + s.port + " " + slotState(s), meta: stateWord(slotState(s)), run: () => app.inspect("slot", s.id, null) });
      }
    }
    for (const s of (d && d.sessions) || []) {
      const n = d.nodes.find((x) => x.host === s.host);
      out.push({ id: "session-" + s.slot_id, group: "Sessions", label: "Session " + (n ? slotName(n, { port: s.port }) : s.host + ":" + s.port), mono: true,
        icon: "i-sessions", keywords: "session " + s.host + " " + s.port + (s.stale ? " stale" : ""), meta: duration(s.duration_s) + (s.stale ? " · stale" : ""),
        run: () => app.inspect("session", s.slot_id, null) });
    }
    for (const t of views.access.tokens()) {
      out.push({ id: "token-" + t.id, group: "Tokens", label: t.name, icon: "i-key", keywords: t.prefix + " " + (t.nodeName || "") + " " + t.kind,
        meta: t.kind + " · " + t.status, run: () => router.go("#/access") });
    }
    return out;
  },
});
$("jump-btn").addEventListener("click", () => palette.open());

const shortcuts = $("shortcuts-dialog");
dismissible(shortcuts);
setHTML($("shortcuts-list"), html`${keys.SHORTCUTS.map(([k, what]) => html`<dt>${k.split(" ").map((p) => p === "/" && k.length > 1 ? html` / ` : html`<kbd>${p}</kbd>`)}</dt><dd>${what}</dd>`)}`);
document.querySelectorAll("[data-shortcuts]").forEach((b) => b.addEventListener("click", () => shortcuts.showModal()));

// ---- keyboard ----------------------------------------------------------------------------
keys.bind({
  palette: () => palette.open(),
  view: (v) => router.go("#/" + v),
  pause: togglePause,
  help: () => shortcuts.showModal(),
  escape: () => closeInspector() || palette.close(),
  move(step) {
    const view = $("view-" + route.view);
    const rows = Array.from(view.querySelectorAll("[data-jk]")).filter((el) => el.offsetParent);
    if (!rows.length) return;
    const i = rows.indexOf(document.activeElement.closest("[data-jk]"));
    const next = rows[i === -1 ? (step > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, i + step))];
    rows.forEach((r) => { r.tabIndex = r === next ? 0 : -1; });
    next.focus();
    next.scrollIntoView({ block: "nearest" });
  },
});

// Rows with data-inspect open their inspector on Enter; links with data-inspect remember their opener.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.defaultPrevented) return;
  const rowEl = e.target.closest && e.target.closest("[data-jk][data-inspect]");
  if (rowEl && rowEl === e.target) { e.preventDefault(); app.inspect(rowEl.dataset.inspect, rowEl.dataset.id, rowEl); }
});
document.addEventListener("click", (e) => {
  const copyBtn = e.target.closest("[data-copy]");
  if (copyBtn) { copy(copyBtn.dataset.copy, copyBtn.dataset.copyLabel, copyBtn); return; }
  const link = e.target.closest("a[data-inspect]");
  if (link && !e.ctrlKey && !e.metaKey && !e.shiftKey && e.button === 0) {
    e.preventDefault();
    const opener = link.closest("[data-jk]") || link;
    app.inspect(link.dataset.inspect, link.dataset.inspect === "node" ? link.dataset.id : Number(link.dataset.id), opener);
    return;
  }
  // A whole row is the target (its chevron and hover say so), except its own buttons and links,
  // and except when the click ends a text selection.
  const row = e.target.closest("[data-jk][data-inspect]");
  if (!row || e.target.closest("a, button, input, select, textarea, label, [data-act]") || e.button !== 0) return;
  if (row.closest("#view-sessions")) return; // the sessions table opens its rows itself
  if (String(window.getSelection && window.getSelection()).length) return;
  app.inspect(row.dataset.inspect, row.dataset.inspect === "node" ? row.dataset.id : Number(row.dataset.id), row);
});

// Admin controls anywhere on the page. After an action, focus lands somewhere sensible.
bindActions(document.body, (el, done, act) => {
  if (!done) { if (el.isConnected) el.focus(); return; }
  app.lastActionAt = Date.now();
  const row = el.closest(".att-row");
  if (row) {
    const rows = Array.from(document.querySelectorAll("#att-list .att-row"));
    const next = rows[rows.indexOf(row) + 1] || rows[rows.indexOf(row) - 1];
    (next && next.isConnected ? next : $("attention")).focus();
    return;
  }
  if (el.isConnected) { el.focus(); return; }
  const h = document.getElementById("insp-title");
  if (h && h.offsetParent) h.focus(); else $("attention").focus();
});

window.addEventListener("beforeunload", (e) => {
  // A freshly made token is only in this page: warn before it's gone.
  if (app.unsaved) { e.preventDefault(); e.returnValue = ""; }
});

// ---- live updates, announcements ---------------------------------------------------------
let pending = { runs: 0, failures: 0, offline: 0, online: 0 }, lastSaid = 0;
function announce(events) {
  for (const e of events) {
    if (e.type === "finished") pending.runs++;
    if (e.type === "failure") pending.failures++;
    if (e.type === "node-offline") pending.offline++;
    if (e.type === "node-online" || e.type === "node-joined") pending.online++;
  }
  if (Date.now() - lastSaid < 10000) return;
  const parts = [];
  if (pending.runs) parts.push(plural(pending.runs, "run") + " finished");
  if (pending.failures) parts.push(plural(pending.failures, "new failure"));
  if (pending.offline) parts.push(plural(pending.offline, "node") + " went offline");
  if (pending.online) parts.push(plural(pending.online, "node") + " came online");
  if (!parts.length) return;
  $("live-summary").textContent = parts.join(", ") + ".";
  pending = { runs: 0, failures: 0, offline: 0, online: 0 };
  lastSaid = Date.now();
}

live.subscribe((msg) => {
  lastMsg = msg;
  renderViews(msg);
  if (msg.events.length) announce(msg.events);
  tick();
});

// ---- the one-second clock -------------------------------------------------------------------
// Heartbeat ages live outside any markup string (so a heartbeat never rebuilds a row): fill them in.
function seen() {
  const d = app.data();
  if (!d) return;
  const map = new Map(d.nodes.map((n) => [n.host, n.last_seen || ""]));
  document.querySelectorAll("[data-seen]").forEach((el) => {
    const iso = map.get(el.dataset.seen);
    if (iso != null && el.dataset.ago !== iso) el.dataset.ago = iso;
  });
}

function tick() {
  const now = live.serverNow();
  seen();
  const since = (iso) => { const t = parseTime(iso); return t == null ? null : (now - t) / 1000; };
  document.querySelectorAll("[data-ago]").forEach((el) => {
    if (!el.dataset.ago) { setText(el, el.hasAttribute("data-short") ? "–" : "never"); return; }
    const s = since(el.dataset.ago);
    setText(el, el.hasAttribute("data-short") ? age(s) : ago(s));
  });
  document.querySelectorAll("[data-dur]").forEach((el) => { if (el.dataset.dur) setText(el, duration(since(el.dataset.dur))); });
  document.querySelectorAll("[data-retry]").forEach((el) => {
    const left = (parseTime(el.dataset.retry) - now) / 1000;
    const text = left > 0 ? (el.hasAttribute("data-short") ? inTime(left) : "retry " + inTime(left)) : (el.hasAttribute("data-short") ? "due now" : "retry due");
    if (el.dataset.label) {
      const label = el.dataset.label + ", " + (left > 0 ? "retry " + inTime(left) : "retry due");
      if (el.getAttribute("aria-label") !== label) el.setAttribute("aria-label", label);
    } else setText(el, text);
  });
  document.querySelectorAll("[data-dur-bar]").forEach((el) => {
    const s = since(el.dataset.durBar), stale = Number(el.dataset.stale) || 3600;
    if (s == null) return;
    // The bar spans twice the stale threshold, so the threshold mark sits in the middle.
    el.style.setProperty("--p", Math.min(1, s / (2 * stale)).toFixed(4));
    el.classList.toggle("over", s > stale);
  });
  document.querySelectorAll("[data-age-ms]").forEach((el) => setText(el, ago((Date.now() - Number(el.dataset.ageMs)) / 1000)));
  document.querySelectorAll("[data-next-poll]").forEach((el) => {
    const s = Math.max(0, Math.ceil((live.nextAt - Date.now()) / 1000));
    setText(el, live.paused ? "when you resume" : s ? "in " + s + "s" : "now");
  });
  document.querySelectorAll("[data-meter]").forEach((m) => m.style.setProperty("--p", m.dataset.meter));
  views.overview.tick(now);
  views.overview.glow(live.recorder);
  freshness();
}
setInterval(tick, 1000);
app.tick = tick; // views that render on their own schedule (Access) fill their ages at once

// ---- start -----------------------------------------------------------------------------------
if (prefs.get("gridDensity", "") === "compact") root.dataset.density = "compact";
renderAccount();
router.start();
auth.init().then(() => { renderAccount(); renderViews(lastMsg); });
live.start();
document.addEventListener("scroll", () => hideTip(), { passive: true });
