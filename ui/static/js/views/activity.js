// Activity: the flight recorder's full log, filterable, copyable, honest about its reach.
import { app } from "../app.js";
import { html, patchList, setHTML, setText } from "../html.js";
import { clock, runLength } from "../format.js";
import { describe } from "../live.js";
import * as router from "../router.js";
import { copy, toast } from "../ui/toast.js";

const KINDS = [["claim", "Claims"], ["finish", "Finished runs"], ["failure", "Failures and health"], ["reset", "Resets"], ["node", "Nodes"], ["admin", "Your actions"]];
const RANGES = [["5m", "5 min", 5 * 60e3], ["15m", "15 min", 15 * 60e3], ["all", "All", Infinity]];

export function filterEvents(events, params, now) {
  const range = RANGES.find((r) => r[0] === params.range) || RANGES[2];
  return events.filter((e) =>
    (!params.node || e.node === params.node) &&
    (!params.slot || String(e.slot) === String(params.slot)) &&
    (!params.kind || e.kind === params.kind) &&
    (range[2] === Infinity || e.t >= now - range[2]));
}

function row(e) {
  const dur = e.type === "finished" ? (e.approx ? "approx." : runLength(e.dur)) : "";
  const inspect = e.slot != null ? ["slot", e.slot] : e.node ? ["node", e.node] : null;
  return html`<tr data-key="${e.id}" class="k-${e.kind}${e.failed ? " failed" : ""}${e.by === "you" ? " yours" : ""}" data-jk tabindex="-1"
      ${inspect ? html`data-inspect="${inspect[0]}" data-id="${inspect[1]}"` : ""}>
    <td class="cell-time mono">${clock(e.t)}</td>
    <td class="cell-kind"><span class="ev-glyph" aria-hidden="true"></span><span class="sr-only">${e.kind}</span></td>
    <td class="cell-subject">${inspect ? html`<a class="mono" href="#/${inspect[0]}/${encodeURIComponent(inspect[1])}" data-inspect="${inspect[0]}" data-id="${inspect[1]}" tabindex="-1">${e.subject || e.node}</a>`
      : html`<span class="mono">${e.subject || ""}</span>`}</td>
    <td class="cell-detail">${e.by === "you" && e.type === "admin" ? e.text : describe(e)}</td>
    <td class="cell-dur mono">${dur}</td>
  </tr>`;
}

export function createActivity(section) {
  setHTML(section, html`
    <header class="view-head">
      <h1 id="activity-h1" tabindex="-1">Activity</h1>
      <p class="view-sub" id="act-sub"></p>
    </header>
    <div class="toolbar act-toolbar">
      <label class="select-field"><span>Node</span><select id="act-node"></select></label>
      <label class="select-field"><span>Slot</span><select id="act-slot"></select></label>
      <label class="select-field"><span>Kind</span><select id="act-kind">
        <option value="">All kinds</option>${KINDS.map(([v, l]) => html`<option value="${v}">${l}</option>`)}</select></label>
      <div class="segmented" role="group" aria-label="Time range" id="act-range">
        ${RANGES.map(([v, l]) => html`<button type="button" data-range="${v}" aria-pressed="false">${l}</button>`)}
      </div>
      <span class="toolbar-spacer"></span>
      <button type="button" class="btn small" id="act-copy">Copy as JSON</button>
      <button type="button" class="btn ghost small destructive" id="act-clear">Clear</button>
    </div>
    <div class="panel table-panel">
      <table class="table activity">
        <caption class="sr-only">Events seen by this tab, newest first</caption>
        <thead><tr><th scope="col">Time</th><th scope="col"><span class="sr-only">Kind</span></th><th scope="col">Subject</th><th scope="col">What happened</th><th scope="col">Duration</th></tr></thead>
        <tbody id="act-rows"></tbody>
      </table>
      <div class="empty compact" id="act-empty" hidden></div>
    </div>
    <p class="hint act-reach">This page sees what changes between polls of the grid's database, about every 1.5 s while it's visible:
      claims, finished runs (runs shorter than a poll show as “approx.”), connect failures, resets, health and nodes.
      It can't see which check ran, or why a connection failed: no error text is stored.</p>`);

  const rows = section.querySelector("#act-rows");
  let params = {};

  const set = (patch) => router.go(router.build("activity", Object.assign({}, params, patch)), { replace: true });
  section.querySelector("#act-node").addEventListener("change", (e) => set({ node: e.target.value, slot: "" }));
  section.querySelector("#act-slot").addEventListener("change", (e) => set({ slot: e.target.value }));
  section.querySelector("#act-kind").addEventListener("change", (e) => set({ kind: e.target.value }));
  section.querySelector("#act-range").addEventListener("click", (e) => {
    const b = e.target.closest("[data-range]");
    if (b) set({ range: b.dataset.range });
  });
  section.querySelector("#act-copy").addEventListener("click", () => {
    const evs = filterEvents(app.live.recorder.events, params, app.now()).map((e) => {
      const o = Object.assign({}, e);
      delete o.seen;
      o.time = new Date(e.t).toISOString();
      return o;
    });
    copy(JSON.stringify(evs, null, 2), evs.length === 1 ? "1 event as JSON" : evs.length + " events as JSON");
  });
  section.querySelector("#act-clear").addEventListener("click", () => {
    app.live.clear();
    toast("Recording cleared. It starts again now.");
  });

  function options(select, list, value, all) {
    const markup = html`<option value="">${all}</option>${list.map(([v, l]) => html`<option value="${v}">${l}</option>`)}`;
    setHTML(select, markup);
    if (select.value !== (value || "")) select.value = value || "";
  }

  function update(data, p, recorder) {
    params = p || {};
    const now = app.now();
    const nodes = ((data && data.nodes) || []).map((n) => n.host);
    const known = new Set(nodes);
    recorder.events.forEach((e) => { if (e.node && !known.has(e.node)) { known.add(e.node); nodes.push(e.node); } });
    options(section.querySelector("#act-node"), nodes.sort().map((n) => [n, n]), params.node, "All nodes");
    const node = data && params.node ? data.nodes.find((n) => n.host === params.node) : null;
    options(section.querySelector("#act-slot"), node ? node.slots.map((s) => [String(s.id), (node.kind === "gateway" ? "slot " : "port ") + s.port]) : [],
      params.slot, node ? "All slots" : "Pick a node first");
    section.querySelector("#act-slot").disabled = !node;
    const kind = section.querySelector("#act-kind");
    if (kind.value !== (params.kind || "")) kind.value = params.kind || "";
    const range = params.range || "all";
    section.querySelectorAll("#act-range [data-range]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === range)));

    const evs = filterEvents(recorder.events, params, now).slice().reverse();
    setText(section.querySelector("#act-sub"), "Recorded by this tab since " + clock(recorder.since) + " · " +
      recorder.events.length + " events in the last 15 min · " + recorder.dropped + " dropped");
    patchList(rows, evs, (e) => e.id, row);
    const empty = section.querySelector("#act-empty");
    empty.hidden = evs.length > 0;
    setHTML(empty, recorder.events.length
      ? html`<h2>No events match</h2><p>Nothing in these filters yet.</p><a class="btn small" href="#/activity">Clear filters</a>`
      : html`<h2>Nothing recorded yet</h2><p>Events appear here as this page sees checks claim and release slots, failures, and nodes come and go.</p>`);
  }

  return { update };
}
