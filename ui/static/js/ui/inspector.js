// The inspector: a non-modal panel on the right that explains one slot, node or session and
// keeps updating while it's open. URL-addressable (#/slot/12, #/node/<name>, #/session/12).
import { app, prefs } from "../app.js";
import { html, icon, setHTML, patchList } from "../html.js";
import {
  age, clock, connectUrl, duration, fullDate, nodeSentence, parseTime, platformOf, plural, reproduce,
  runLength, slotName, slotNameLong, slotSentence, slotState, stateWord,
} from "../format.js";
import { describe } from "../live.js";
import { adminButton } from "../actions.js";
import { slotCell, statusGlyph, NODE_TONE } from "./matrix.js";
import { hubField, wireHubFields } from "./hub.js";

const MIN = 320, MAX = 640, WIDTH_KEY = "gridInspectorWidth", TAB_KEY = "gridReproTab";
const LANGS = [["js", "Playwright JS"], ["python", "Playwright Python"], ["powershell", "PowerShell"], ["bash", "bash"]];
const STATE_TONE = { free: "ok", busy: "accent", unhealthy: "warn", failing: "bad", cooling: "bad", offline: "off" };

const pill = (tone, text) => html`<span class="pill p-${tone}"><span class="glyph g-${tone}" aria-hidden="true"></span>${text}</span>`;

function eventRow(e) {
  return html`<li data-key="${e.id}" class="ev k-${e.kind}${e.failed ? " failed" : ""}">
    <span class="mono ev-time">${clock(e.t)}</span>
    <span class="ev-glyph" aria-hidden="true"></span>
    <span class="ev-text">${e.by === "you" && e.type === "admin" ? e.text : describe(e)}</span></li>`;
}

function copyable(text, label) {
  return html`<span class="copyable"><code class="mono">${text}</code><button type="button" class="icon-btn tiny" data-copy="${text}"
    data-copy-label="${label}" aria-label="Copy ${label}">${icon("i-copy")}</button></span>`;
}

export function createInspector({ onClose }) {
  const el = document.getElementById("inspector");
  const body = el.querySelector("#insp-body");
  const handle = el.querySelector("#insp-handle");
  let cur = null, opener = null, openerKey = null, parts = null, built = null;

  // ---- width ---------------------------------------------------------------
  const setWidth = (w, save) => {
    w = Math.max(MIN, Math.min(MAX, Math.round(w)));
    document.documentElement.style.setProperty("--insp-w", w + "px");
    handle.setAttribute("aria-valuenow", String(w));
    handle.setAttribute("aria-valuetext", w + " pixels wide");
    if (save) prefs.set(WIDTH_KEY, w);
    return w;
  };
  let width = setWidth(Number(prefs.get(WIDTH_KEY, 420)) || 420, false);
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    document.documentElement.classList.add("resizing");
    const moveTo = (ev) => { width = setWidth(window.innerWidth - ev.clientX, false); };
    const up = () => {
      handle.removeEventListener("pointermove", moveTo);
      handle.removeEventListener("pointerup", up);
      document.documentElement.classList.remove("resizing");
      setWidth(width, true);
    };
    handle.addEventListener("pointermove", moveTo);
    handle.addEventListener("pointerup", up);
  });
  handle.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 64 : 16;
    const next = { ArrowLeft: width + step, ArrowRight: width - step, Home: MIN, End: MAX }[e.key];
    if (next == null) return;
    e.preventDefault();
    width = setWidth(next, true);
  });

  // ---- open / close ----------------------------------------------------------
  function open(type, id, openerEl) {
    const same = cur && cur.type === type && String(cur.id) === String(id);
    if (openerEl && !el.contains(openerEl)) {
      opener = openerEl;
      openerKey = openerEl.dataset && openerEl.dataset.fk;
    }
    cur = { type, id };
    if (!same) built = null;
    const wasHidden = el.hidden;
    el.hidden = false;
    document.documentElement.classList.add("insp-open");
    render();
    if (!same || wasHidden) {
      const h = el.querySelector("#insp-title");
      if (h) h.focus({ preventScroll: true });
    }
  }

  function close({ restore = true } = {}) {
    if (el.hidden) return false;
    el.hidden = true;
    document.documentElement.classList.remove("insp-open");
    cur = null;
    built = null;
    if (restore) {
      let target = opener && opener.isConnected ? opener : null;
      if (!target && openerKey) target = document.querySelector('[data-fk="' + CSS.escape(openerKey) + '"]');
      if (!target) target = document.querySelector(".view:not([hidden]) h1");
      if (target) {
        if (target.tabIndex < 0 && !target.matches("a, button, input, [role=gridcell], [role=button]")) target.tabIndex = -1;
        target.focus({ preventScroll: true });
      }
    }
    opener = null;
    return true;
  }

  el.addEventListener("click", (e) => {
    if (e.target.closest("[data-insp-close]")) { onClose(); return; }
    const tab = e.target.closest("[role=tab][data-lang]");
    if (tab) selectTab(tab.dataset.lang, true);
    const cell = e.target.closest(".cell[data-slot]");
    if (cell) app.inspect("slot", Number(cell.dataset.slot), cell);
  });
  el.addEventListener("keydown", (e) => {
    const tab = e.target.closest("[role=tab][data-lang]");
    if (tab && (e.key === "ArrowRight" || e.key === "ArrowLeft" || e.key === "Home" || e.key === "End")) {
      e.preventDefault();
      const i = LANGS.findIndex((l) => l[0] === tab.dataset.lang);
      const j = e.key === "Home" ? 0 : e.key === "End" ? LANGS.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + LANGS.length) % LANGS.length;
      selectTab(LANGS[j][0], true);
    }
    const cell = e.target.closest(".insp-mini .cell[data-slot]");
    if (cell && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); app.inspect("slot", Number(cell.dataset.slot), cell); }
    if (cell && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
      const cells = Array.from(el.querySelectorAll(".insp-mini .cell"));
      const next = cells[cells.indexOf(cell) + (e.key === "ArrowRight" ? 1 : -1)];
      if (next) { e.preventDefault(); cells.forEach((c) => { c.tabIndex = c === next ? 0 : -1; }); next.focus(); }
    }
  });

  // A press outside the panel (on nothing interactive) closes it. A drag that starts inside, such
  // as selecting the endpoint text past its end, doesn't.
  let downInside = false;
  document.addEventListener("pointerdown", (e) => { downInside = el.contains(e.target); }, true);
  document.addEventListener("click", (e) => {
    if (el.hidden || downInside || el.contains(e.target)) return;
    if (e.target.closest("a, button, input, select, textarea, label, [role=button], [role=gridcell], [role=tab], [tabindex], dialog, .menu, .tip")) return;
    if (String(window.getSelection && window.getSelection()).length) return;
    onClose();
  });

  // ---- reproduce -------------------------------------------------------------
  let lang = prefs.get(TAB_KEY, "js");
  if (!LANGS.some((l) => l[0] === lang)) lang = "js";
  function selectTab(next, focus) {
    lang = next;
    prefs.set(TAB_KEY, lang);
    el.querySelectorAll("[role=tab][data-lang]").forEach((t) => {
      const on = t.dataset.lang === lang;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      if (on && focus) t.focus();
    });
    const panel = el.querySelector("#repro-panel");
    if (panel) panel.setAttribute("aria-labelledby", "repro-tab-" + lang);
    updateSnippet();
  }
  function updateSnippet() {
    const code = el.querySelector("#repro-code");
    if (!code || !cur) return;
    const f = cur.type === "node" ? null : app.findSlot(cur.id);
    if (!f) return;
    const text = reproduce(app.hub(), f.node, f.slot)[lang];
    if (code.textContent !== text) code.textContent = text;
    const btn = el.querySelector("#repro-copy");
    if (btn) btn.dataset.copy = text;
  }
  app.onHub(() => { updateSnippet(); render(); });

  function reproBlock(gateway) {
    return html`<section class="insp-section repro" aria-labelledby="repro-title">
      <h3 class="section-label" id="repro-title">Reproduce</h3>
      ${gateway ? hubField("Your checks' hub address. The client token is read from GATEWAY_CLIENT_TOKEN, never copied.") : html`<p class="hint">Static hosts take a plain run-server address; no token.</p>`}
      <div class="tabs-row">
        <div class="tabs" role="tablist" aria-label="Snippet language">
          ${LANGS.map(([id, label]) => html`<button type="button" role="tab" id="repro-tab-${id}" data-lang="${id}" aria-controls="repro-panel"
            aria-selected="${id === lang}" tabindex="${id === lang ? 0 : -1}">${label}</button>`)}
        </div>
      </div>
      <div class="code-panel" role="tabpanel" id="repro-panel" aria-labelledby="repro-tab-${lang}">
        <pre class="code" tabindex="0"><code id="repro-code" class="mono"></code></pre>
        <button type="button" class="icon-btn tiny copy-btn" id="repro-copy" data-copy="" data-copy-label="the snippet" aria-label="Copy the snippet" title="Copy">${icon("i-copy")}</button>
      </div>
    </section>`;
  }

  // ---- skeletons ---------------------------------------------------------------
  function skeleton(type, gateway) {
    const sections = {
      slot: html`
        <header class="insp-head" data-part="head"></header>
        <p class="insp-sentence" data-part="sentence"></p>
        <dl class="facts" data-part="facts"></dl>
        <section class="insp-section" aria-labelledby="ev-title"><h3 class="section-label" id="ev-title">Recent events <span class="dim">· this tab</span></h3>
          <ol class="ev-list" data-part="events"></ol></section>
        ${reproBlock(gateway)}
        <div class="insp-actions" data-part="actions"></div>`,
      session: html`
        <header class="insp-head" data-part="head"></header>
        <p class="insp-sentence" data-part="sentence"></p>
        <div class="insp-duration" data-part="bar"></div>
        <dl class="facts" data-part="facts"></dl>
        <section class="insp-section" aria-labelledby="ev-title"><h3 class="section-label" id="ev-title">Recent events <span class="dim">· this tab</span></h3>
          <ol class="ev-list" data-part="events"></ol></section>
        ${reproBlock(gateway)}
        <div class="insp-actions" data-part="actions"></div>`,
      node: html`
        <header class="insp-head" data-part="head"></header>
        <p class="insp-sentence" data-part="sentence"></p>
        <dl class="facts" data-part="facts"></dl>
        <section class="insp-section" aria-labelledby="mini-title"><h3 class="section-label" id="mini-title">Slots</h3>
          <div class="insp-mini" data-part="mini" role="group" aria-label="Slots on this node"></div></section>
        <section class="insp-section" aria-labelledby="ev-title"><h3 class="section-label" id="ev-title">Runs and failures <span class="dim">· this tab</span></h3>
          <p class="insp-stats" data-part="stats"></p>
          <ol class="ev-list" data-part="events"></ol></section>
        <div class="insp-actions" data-part="actions"></div>`,
      gone: html`<header class="insp-head" data-part="head"></header><p class="insp-sentence" data-part="sentence"></p>`,
    };
    setHTML(body, sections[type]);
    parts = {};
    body.querySelectorAll("[data-part]").forEach((p) => { parts[p.dataset.part] = p; });
    wireHubFields(body);
  }

  function part(name, markup) {
    if (parts[name]) setHTML(parts[name], markup);
  }

  // ---- rendering ---------------------------------------------------------------
  function render() {
    if (!cur || el.hidden) return;
    const d = app.data();
    const kind = el.querySelector("#insp-kind");
    kind.textContent = { slot: "Slot", node: "Node", session: "Session" }[cur.type];
    if (!d) {
      if (built !== "wait") { setHTML(body, html`<p class="insp-sentence">Loading…</p>`); built = "wait"; }
      return;
    }
    if (cur.type === "node") return renderNode(d);
    const f = app.findSlot(cur.id);
    if (!f || (cur.type === "session" && !f.session)) return renderGone(f);
    const gateway = f.node.kind === "gateway";
    const key = cur.type + ":" + cur.id + ":" + gateway;
    if (built !== key) { skeleton(cur.type, gateway); built = key; selectTab(lang, false); }
    if (cur.type === "slot") renderSlot(f); else renderSession(f);
    updateSnippet();
  }

  function renderGone(f) {
    if (built !== "gone:" + cur.type + cur.id) { skeleton("gone"); built = "gone:" + cur.type + cur.id; }
    const ended = cur.type === "session" && f;
    part("head", html`<h2 id="insp-title" class="mono" tabindex="-1">${ended ? slotNameLong(f.node, f.slot) : cur.type + " " + cur.id}</h2>`);
    part("sentence", ended
      ? html`This session has ended: the slot is ${stateWord(slotState(f.slot)).toLowerCase()} now. <a href="#/slot/${f.slot.id}" data-inspect="slot" data-id="${f.slot.id}">Inspect the slot</a>.`
      : html`This ${cur.type} isn't on the grid any more. It may have been forgotten, or its node left.`);
  }

  function slotFacts(f) {
    const { node, slot } = f, cfg = app.cfg(), gateway = node.kind === "gateway";
    const endpoint = gateway ? connectUrl(app.hub(), node, slot) : slot.endpoint;
    const retry = parseTime(slot.retry_at), run = parseTime(slot.last_run);
    const meter = Math.min(1, slot.fails / Math.max(1, cfg.threshold));
    return [
      html`<dt>Endpoint</dt><dd>${copyable(endpoint, "the endpoint")}</dd>`,
      html`<dt>Run-server</dt><dd>${gateway ? (slot.healthy ? html`<span class="ok-text">Passes health checks</span>` : html`<span class="warn-text">Not answering health checks</span>`) : html`<span class="dim">Not checked (static host)</span>`}</dd>`,
      html`<dt>Failures</dt><dd><span class="meter-wrap"><span class="meter${slot.fails >= cfg.threshold ? " over" : ""}" role="img"
        aria-label="${slot.fails} of ${cfg.threshold} consecutive failures"><span class="meter-fill" data-meter="${meter}"></span></span>
        <span class="mono">${slot.fails} of ${cfg.threshold}</span></span></dd>`,
      html`<dt>Next retry</dt><dd>${retry ? html`<span class="mono">${clock(retry)}</span> · <span data-retry="${slot.retry_at}" data-short></span>` : html`<span class="dim">${slot.fails >= cfg.threshold ? "Due now" : "Not cooling down"}</span>`}</dd>`,
      html`<dt>${slot.busy ? "Current run" : "Last run"}</dt><dd>${run ? html`<span class="mono">${clock(run)}</span> · ${slot.busy
        ? html`running <span data-dur="${slot.last_run}"></span>` : html`<span data-ago="${slot.last_run}"></span>`}` : html`<span class="dim">Never</span>`}</dd>`,
      gateway ? html`<dt>Node heartbeat</dt><dd><span data-seen="${node.host}"></span>${node.online ? "" : html` · <span class="warn-text">offline</span>`}</dd>`
        : html`<dt>Host</dt><dd class="mono">${node.host}</dd>`,
    ];
  }

  function eventsFor(filter, limit = 8) {
    const evs = app.live.recorder.events.filter(filter).slice(-limit).reverse();
    return evs;
  }

  function renderEvents(list) {
    if (!list.length) {
      setHTML(parts.events, html`<li class="ev-empty">Nothing seen yet. This list fills as this page watches.</li>`);
      return;
    }
    parts.events.__html = null;
    patchList(parts.events, list, (e) => e.id, eventRow);
  }

  function slotHead(f, extra) {
    const st = slotState(f.slot);
    return html`<h2 id="insp-title" class="mono" tabindex="-1">${slotNameLong(f.node, f.slot)}</h2>
      ${extra || pill(STATE_TONE[st] || "off", stateWord(st))}`;
  }

  function renderSlot(f) {
    const cfg = app.cfg();
    part("head", slotHead(f));
    part("sentence", html`${slotSentence(f.node, f.slot, Object.assign({ session: f.session }, cfg))}`);
    part("facts", html`${slotFacts(f)}`);
    renderEvents(eventsFor((e) => e.slot === f.slot.id));
    const name = slotName(f.node, f.slot);
    part("actions", html`
      ${f.slot.fails ? adminButton({ label: "Reset failures", verb: "reset failures", attrs: html`data-act="reset" data-target="${f.slot.id}"`, cls: "btn" }) : ""}
      ${f.slot.busy ? adminButton({ label: "Release…", verb: "release " + name, attrs: html`data-act="release" data-target="${f.slot.id}"`, cls: "btn", danger: true }) : ""}
      ${!f.slot.fails && !f.slot.busy ? html`<p class="dim">No actions needed: nothing to reset or release.</p>` : ""}`);
    meters();
  }

  function renderSession(f) {
    const cfg = app.cfg(), s = f.session, started = parseTime(s.started);
    part("head", slotHead(f, s.stale ? pill("warn", "Stale") : pill("accent", "Running")));
    part("sentence", html`${slotSentence(f.node, f.slot, Object.assign({ session: s }, cfg))}`);
    part("bar", html`<div class="dur-bar big" data-dur-bar="${s.started || ""}" data-stale="${cfg.staleAfter}" role="img"
        aria-label="Running for ${duration(s.duration_s)}; stale after ${duration(cfg.staleAfter)}"><span class="dur-fill"></span><span class="dur-mark" title="${duration(cfg.staleAfter)}"></span></div>
      <div class="dur-legend mono"><span>Started ${clock(started)}</span><span data-dur="${s.started || ""}"></span><span>stale at ${duration(cfg.staleAfter)}</span></div>`);
    part("facts", html`
      <dt>Started</dt><dd><span class="mono">${clock(started)}</span> <span class="dim">${fullDate(started)}</span></dd>
      ${slotFacts(f)}
      <dt>Node</dt><dd>${s.kind === "gateway" ? (s.node_online ? "Online" : html`<span class="warn-text">Offline</span>`) : "Static host"}</dd>`);
    renderEvents(eventsFor((e) => e.slot === f.slot.id));
    part("actions", adminButton({ label: "Release…", verb: "release " + slotName(f.node, f.slot), attrs: html`data-act="release" data-target="${f.slot.id}"`, cls: "btn", danger: true }));
    meters();
  }

  function renderNode(d) {
    const node = app.findNode(cur.id);
    if (!node) return renderGone(null);
    if (built !== "node:" + node.host) { skeleton("node"); built = "node:" + node.host; }
    const p = platformOf(node), gateway = node.kind === "gateway";
    part("head", html`<h2 id="insp-title" class="mono" tabindex="-1">${node.host}</h2>${pill(NODE_TONE[node.status] || "off", node.status.charAt(0) + node.status.slice(1).toLowerCase())}`);
    part("sentence", html`${nodeSentence(node, app.cfg())}`);
    const facts = [
      html`<dt>Kind</dt><dd>${gateway ? "Gateway node" : "Static host"}${node.ephemeral ? html` · <span class="tag">ephemeral</span>` : ""}</dd>`,
      html`<dt>Platform</dt><dd>${icon(p.icon)} ${p.label}</dd>`,
    ];
    if (gateway) {
      facts.push(
        html`<dt>Address</dt><dd class="mono">${node.remote_address || "–"}</dd>`,
        html`<dt>Playwright</dt><dd class="mono">${node.playwright_version || "–"}${node.agent_version ? html` <span class="dim">· agent ${node.agent_version}</span>` : ""}</dd>`,
        html`<dt>Browsers</dt><dd>${(node.browsers || []).join(", ") || "–"}</dd>`,
        html`<dt>Heartbeat</dt><dd><span data-seen="${node.host}"></span></dd>`,
        html`<dt>Connected</dt><dd class="mono" title="${fullDate(parseTime(node.connected_at))}">${node.connected_at ? clock(parseTime(node.connected_at), false) + " · " + age((app.now() - parseTime(node.connected_at)) / 1000) + " ago" : "–"}</dd>`,
        html`<dt>Endpoints</dt><dd>${copyable(app.hub() + "/connect/" + encodeURIComponent(node.host) + "/", "the endpoint prefix")}</dd>`);
      if (node.ephemeral && !node.online) facts.push(html`<dt>Note</dt><dd>Ephemeral node: it and its slots are removed automatically.</dd>`);
    } else {
      facts.push(html`<dt>Ports</dt><dd class="mono">${node.slots.length ? node.slots[0].port + "–" + node.slots[node.slots.length - 1].port : "–"}</dd>`,
        html`<dt>Source</dt><dd>hosts: in database.yaml</dd>`);
    }
    part("facts", html`${facts}`);
    part("mini", html`${node.slots.map((s, i) => slotCell(node, s, i === 0 ? "first" : ""))}`);
    parts.mini.querySelectorAll(".cell").forEach((c, i) => { c.tabIndex = i === 0 ? 0 : -1; });
    const evs = app.live.recorder.events.filter((e) => e.node === node.host);
    const runs = evs.filter((e) => e.type === "finished"), fails = evs.filter((e) => e.type === "failure");
    const known = runs.filter((e) => e.dur != null);
    part("stats", html`<span class="mono">${runs.length}</span> ${runs.length === 1 ? "run" : "runs"}${known.length ? html` · avg <span class="mono">${runLength(known.reduce((a, e) => a + e.dur, 0) / known.length)}</span>` : ""} · <span class="mono">${fails.length}</span> connect ${fails.length === 1 ? "failure" : "failures"} seen`);
    renderEvents(evs.filter((e) => e.type !== "claimed").slice(-10).reverse());
    const busy = node.slots.some((s) => s.busy), hasFails = node.slots.some((s) => s.fails);
    part("actions", html`
      ${hasFails ? adminButton({ label: "Reset node failures", verb: "reset failures on " + node.host, attrs: html`data-act="reset-node" data-target="${node.host}"`, cls: "btn" }) : ""}
      ${gateway && !node.online ? (busy ? html`<p class="dim">Release its ${plural(node.busy, "slot")} in use before forgetting it.</p>`
        : adminButton({ label: "Forget…", verb: "forget " + node.host, attrs: html`data-act="forget" data-target="${node.host}"`, cls: "btn", danger: true })) : ""}
      ${!hasFails && !(gateway && !node.online) ? html`<p class="dim">No actions needed.</p>` : ""}`);
  }

  function meters() {
    el.querySelectorAll("[data-meter]").forEach((m) => m.style.setProperty("--p", m.dataset.meter));
  }

  return {
    open, close, render,
    get current() { return cur; },
    isOpen: () => !el.hidden,
  };
}
