// Sessions: every claim in force, stale first, then oldest. Release lives in the inspector.
import { app } from "../app.js";
import { html, icon, patchList, setHTML, setText } from "../html.js";
import { clock, connectUrl, duration, fullDate, parseTime, plural } from "../format.js";
import * as router from "../router.js";

const FILTERS = [["all", "All"], ["stale", "Stale"], ["offline", "On offline nodes"]];
const match = {
  all: () => true,
  stale: (s) => s.stale,
  offline: (s) => s.kind === "gateway" && !s.node_online,
};

function subject(s) {
  if (!s.host) return "(blank host):" + s.port;
  return s.kind === "gateway" ? s.host + " / " + s.port : s.host + ":" + s.port;
}

function endpointOf(s) {
  if (!s.endpoint) return null;
  if (s.kind !== "gateway") return s.endpoint;
  return connectUrl(app.hub(), { host: s.host }, { port: s.port });
}

function rowMarkup(s, staleAfter) {
  const started = parseTime(s.started), ep = endpointOf(s);
  const state = s.kind === "gateway" && !s.node_online ? ["off", "Node offline"] : s.stale ? ["warn", "Stale"] : ["accent", "Running"];
  return html`<tr data-key="${s.slot_id}" data-fk="session-${s.slot_id}" data-jk tabindex="-1" data-inspect="session" data-id="${s.slot_id}" class="${s.stale ? "is-stale" : ""}">
    <td class="cell-subject"><a class="mono" href="#/session/${s.slot_id}" data-inspect="session" data-id="${s.slot_id}" tabindex="-1">${subject(s)}</a>
      <small>${s.kind === "gateway" ? "Gateway node" : s.kind === "legacy" ? "Old config row" : "Static host"}</small></td>
    <td class="cell-time" data-label="Started"><span class="mono" title="${fullDate(started)}">${started != null ? clock(started) : "–"}</span></td>
    <td class="cell-duration" data-label="Running">
      <div class="dur-bar" data-dur-bar="${s.started || ""}" data-stale="${staleAfter}" aria-hidden="true"><span class="dur-fill"></span><span class="dur-mark"></span></div>
      <span class="mono dur-text" data-dur="${s.started || ""}"></span></td>
    <td class="cell-endpoint" data-label="Endpoint">${ep ? html`<span class="copyable"><code class="mono" title="${ep}">${ep}</code>
      <button type="button" class="icon-btn tiny" data-copy="${ep}" data-copy-label="the endpoint" aria-label="Copy endpoint">${icon("i-copy")}</button></span>`
      : html`<span class="dim" title="Never handed out to checks">–</span>`}</td>
    <td class="cell-state"><span class="pill p-${state[0]}"><span class="glyph g-${state[0]}" aria-hidden="true"></span>${state[1]}</span></td>
  </tr>`;
}

export function createSessions(section) {
  setHTML(section, html`
    <header class="view-head">
      <h1 id="sessions-h1" tabindex="-1">Sessions</h1>
      <p class="view-sub" id="sessions-sub"></p>
    </header>
    <div class="toolbar">
      <div class="chips" role="group" aria-label="Show sessions" id="session-filters">
        ${FILTERS.map(([id, label]) => html`<button type="button" class="chip" data-filter="${id}" aria-pressed="false">${label}<span class="n mono"></span></button>`)}
      </div>
      <p class="toolbar-note" id="session-note" role="status"></p>
    </div>
    <div class="panel table-panel" id="sessions-panel">
      <table class="table sessions">
        <caption class="sr-only">Running sessions. Select a row to inspect it and release it.</caption>
        <thead><tr>
          <th scope="col">Node / slot</th><th scope="col">Started</th><th scope="col">Running <span class="dim">· stale at 1h</span></th>
          <th scope="col">Endpoint</th><th scope="col">State</th>
        </tr></thead>
        <tbody id="session-rows"></tbody>
      </table>
      <div class="empty compact" id="sessions-empty" hidden></div>
    </div>`);
  const rows = section.querySelector("#session-rows");
  let lastNote = "";

  section.querySelector("#session-filters").addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]");
    if (b) router.go(router.build("sessions", { filter: b.dataset.filter }), { replace: true });
  });

  function update(data, params) {
    const filter = match[params.filter] ? params.filter : "all";
    const all = (data && data.sessions) || [];
    const staleAfter = (data && data.stale_after_s) || 3600;
    setText(section.querySelector("thead th:nth-child(3) .dim"), "· stale at " + duration(staleAfter));
    // An action that empties the current filter (releasing the last stale session) goes back to All.
    if (filter !== "all" && !all.some(match[filter]) && app.lastActionAt && Date.now() - app.lastActionAt < 8000) {
      lastNote = "No " + (filter === "stale" ? "stale sessions" : "sessions on offline nodes") + " left: showing all.";
      app.lastActionAt = 0;
      router.go(router.build("sessions", {}), { replace: true });
      return;
    }
    section.querySelectorAll("#session-filters [data-filter]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.filter === filter));
      const n = all.filter(match[b.dataset.filter]).length;
      setText(b.querySelector(".n"), String(n));
    });
    const stale = all.filter((s) => s.stale).length;
    setText(section.querySelector("#sessions-sub"), all.length
      ? plural(all.length, "session") + " running" + (stale ? " · " + stale + " stale" : "") + ". Select one to inspect or release it."
      : "");
    setText(section.querySelector("#session-note"), lastNote);
    const shown = all.filter(match[filter]).sort((a, b) =>
      (b.stale - a.stale) || ((a.started || "") < (b.started || "") ? -1 : (a.started || "") > (b.started || "") ? 1 : 0) || a.slot_id - b.slot_id);
    patchList(rows, shown, (s) => s.slot_id, (s) => rowMarkup(s, staleAfter));
    const empty = section.querySelector("#sessions-empty");
    empty.hidden = shown.length > 0;
    setHTML(empty, all.length
      ? html`<h2>No sessions match</h2><p>Nothing in this filter right now.</p><button type="button" class="btn small" data-filter-all>Show all sessions</button>`
      : html`<h2>No sessions running</h2><p>A check claims a slot when it starts and frees it when it finishes. Running checks show up here, and finished ones in Activity.</p>`);
  }

  section.addEventListener("click", (e) => {
    if (e.target.closest("[data-filter-all]")) router.go(router.build("sessions", {}), { replace: true });
    if (e.target.closest("[data-copy], a, button")) return;
    const tr = e.target.closest("tr[data-inspect]");
    if (tr && !String(window.getSelection()).length) app.inspect("session", Number(tr.dataset.id), tr);
  });

  return { update, clearNote() { lastNote = ""; } };
}
