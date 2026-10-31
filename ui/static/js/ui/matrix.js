// The slot matrix: every node as one dense row, every slot as a glyph-coded 28px cell labelled with
// its slot number (gateway) or port (static host), the number checks and reports use. The whole
// matrix is one role=grid with a roving tabindex, so 300 slots are still a single Tab stop.
import { html, icon, patchList, setHTML } from "../html.js";
import { browserMonograms, platformOf, plural, slotState, stateWord, parseTime } from "../format.js";
import { showTip, hideTip } from "./toast.js";

const RANK = { DOWN: 0, DEGRADED: 1, OFFLINE: 2 };
export const NODE_TONE = { ONLINE: "ok", UP: "ok", DEGRADED: "warn", DOWN: "bad", OFFLINE: "off" };
const NODE_WORD = { ONLINE: "Online", UP: "Up", DEGRADED: "Degraded", DOWN: "Down", OFFLINE: "Offline" };
const HALO_MS = 10000, FLASH_MS = 600;

export function sortNodes(nodes) {
  return nodes.slice().sort((a, b) => ((RANK[a.status] ?? 3) - (RANK[b.status] ?? 3)) || a.host.localeCompare(b.host));
}

export function cellLabel(node, slot) {
  const st = slotState(slot);
  return (node.kind === "gateway" ? "Slot " : "Port ") + slot.port + ": " + stateWord(st) +
    (slot.fails ? ", " + plural(slot.fails, "failure") : "");
}

export function slotCell(node, slot, extra = "") {
  const st = slotState(slot);
  const label = cellLabel(node, slot);
  return html`<div role="gridcell" class="cell s-${st}${slot.fails && st !== "failing" && st !== "cooling" ? " has-fails" : ""} ${extra}"
    data-slot="${slot.id}" data-fk="slot-${slot.id}" data-nav tabindex="-1" aria-label="${label}"
    ${slot.retry_at ? html`data-retry="${slot.retry_at}" data-label="${label}"` : ""}><span class="cell-n" aria-hidden="true">${slot.port}</span>${slot.fails
      ? html`<span class="badge" aria-hidden="true">${slot.fails}</span>` : ""}</div>`;
}

export function statusGlyph(node) {
  const tone = NODE_TONE[node.status] || "off";
  return html`<span class="glyph g-${tone}" role="img" aria-label="${NODE_WORD[node.status] || node.status}" title="${NODE_WORD[node.status] || node.status}"></span>`;
}

function row(node) {
  const p = platformOf(node);
  const gateway = node.kind === "gateway";
  const tags = [];
  if (node.ephemeral) tags.push(html`<span class="tag" title="A short-lived node, such as a docker runner. The hub removes it once it disconnects.">ephemeral</span>`);
  if (!gateway) tags.push(html`<span class="tag" title="A fixed host:port range from database.yaml">static</span>`);
  if (node.ephemeral && node.status === "OFFLINE") {
    tags.push(html`<span class="tag note" title="Ephemeral node: it and its slots are removed automatically.">removed automatically</span>`);
  }
  return html`
    <div role="row" class="mx-row t-${NODE_TONE[node.status] || "off"}" data-key="${node.host}" aria-label="${node.host}">
      <div role="gridcell" class="mx-status">${statusGlyph(node)}</div>
      <div role="rowheader" class="mx-name"><a href="#/node/${encodeURIComponent(node.host)}" class="mono node-link" data-inspect="node" data-id="${node.host}"
        data-nav data-fk="node-${node.host}" tabindex="-1" title="${node.host}${node.remote_address ? " (" + node.remote_address + ")" : ""}">${node.host}</a></div>
      <div role="gridcell" class="mx-plat" title="${p.label}">${icon(p.icon)}<span class="sr-only">${p.label}</span></div>
      <div role="gridcell" class="mx-browsers">${gateway ? browserMonograms(node.browsers).map((b) => html`<abbr class="mono" title="${b.name}">${b.mono}</abbr>`) : html`<span class="dim">–</span>`}</div>
      <div role="gridcell" class="mx-ver mono">${node.playwright_version || html`<span class="dim">–</span>`}</div>
      <div role="gridcell" class="mx-beat mono" title="${gateway ? "Last heartbeat" : "Last activity"}">${gateway
        ? html`<span data-seen="${node.host}" data-short></span>`
        : html`<span data-ago="${node.last_activity || ""}" data-short></span>`}</div>
      <div role="gridcell" class="mx-tags">${tags}</div>
      <div role="gridcell" class="mx-count mono" title="${node.busy} of ${node.total} slots in use"><b>${node.busy}</b>/${node.total}</div>
      <div class="mx-slots" role="presentation">${node.slots.length ? node.slots.map((s) => slotCell(node, s)) : html`<span class="dim">No slots</span>`}</div>
      <div role="gridcell" class="mx-more"><button type="button" class="icon-btn small" data-nav data-fk="menu-${node.host}" tabindex="-1"
        data-row-menu="${node.host}" aria-haspopup="menu" aria-expanded="false" aria-label="Actions for ${node.host}">${icon("i-more")}</button></div>
    </div>`;
}

// note: how checks reach the group. Every check connects to the hub's one address; no slot has
// an address of its own.
function groupHead(key, title, count, note) {
  return html`<div role="row" class="mx-group" data-key="group-${key}">
    <div role="columnheader" aria-colspan="10" class="mx-group-cell"><span class="mx-group-title">${title}</span>
      <span class="mx-group-count mono">${count}</span><span class="mx-pattern">${note}</span></div></div>`;
}

export function createMatrix(root, { onOpen, onMenu, hub }) {
  setHTML(root, html`
    <div class="mx" role="grid" aria-label="Slot matrix: nodes and their slots. Arrow keys move, Enter opens the inspector." aria-describedby="mx-legend" id="matrix">
      <div role="rowgroup" class="mx-head-group">
        <div role="row" class="mx-row mx-colheads">
          <div role="columnheader" class="mx-status"><span class="sr-only">Status</span></div>
          <div role="columnheader" class="mx-name">Node</div>
          <div role="columnheader" class="mx-plat"><span class="sr-only">Platform</span>OS</div>
          <div role="columnheader" class="mx-browsers">Browsers</div>
          <div role="columnheader" class="mx-ver">Playwright</div>
          <div role="columnheader" class="mx-beat">Seen</div>
          <div role="columnheader" class="mx-tags"><span class="sr-only">Tags</span></div>
          <div role="columnheader" class="mx-count">Busy</div>
          <div role="columnheader" class="mx-slots-h">Slots</div>
          <div role="columnheader" class="mx-more"><span class="sr-only">Actions</span></div>
        </div>
      </div>
      <div role="rowgroup" class="mx-body" id="mx-body"></div>
    </div>`);
  const grid = root.querySelector(".mx"), body = root.querySelector("#mx-body");
  let current = null;

  function render(data) {
    const nodes = sortNodes((data && data.nodes) || []);
    const gw = nodes.filter((n) => n.kind === "gateway"), st = nodes.filter((n) => n.kind !== "gateway");
    const h = hub();
    const items = [];
    if (gw.length) items.push({ key: "group-gw", head: groupHead("gw", "Gateway nodes", gw.length, html`checks connect to <code>${h}/connect</code>`) });
    gw.forEach((n) => items.push({ key: n.host, node: n }));
    if (st.length) items.push({ key: "group-st", head: groupHead("st", "Static hosts", st.length, html`through the hub too · any browser, after the gateway nodes`) });
    st.forEach((n) => items.push({ key: n.host, node: n }));
    patchList(body, items, (it) => it.key, (it) => it.head || row(it.node));
    seen(data);
    roving();
  }

  // Heartbeat ages change every few seconds; they go in place, outside the row markup, so a
  // heartbeat never rebuilds a row (or drops a selection).
  function seen(data) {
    const map = new Map(((data && data.nodes) || []).map((n) => [n.host, n.last_seen || ""]));
    body.querySelectorAll("[data-seen]").forEach((el) => {
      const iso = map.get(el.dataset.seen);
      if (iso != null && el.dataset.ago !== iso) el.dataset.ago = iso;
    });
  }

  const navs = () => Array.from(body.querySelectorAll("[data-nav]"));
  function roving() {
    const all = navs();
    if (!all.length) return;
    let cur = current && all.find((el) => el.dataset.fk === current);
    if (!cur) { cur = all[0]; current = cur.dataset.fk; }
    all.forEach((el) => { const t = el === cur ? 0 : -1; if (el.tabIndex !== t) el.tabIndex = t; });
  }
  function move(el) {
    if (!el) return;
    current = el.dataset.fk;
    roving();
    el.focus();
  }

  grid.addEventListener("focusin", (e) => {
    const el = e.target.closest("[data-nav]");
    if (el && el.dataset.fk !== current) { current = el.dataset.fk; roving(); }
  });

  grid.addEventListener("keydown", (e) => {
    const el = e.target.closest("[data-nav]");
    if (!el) return;
    const rowEl = el.closest(".mx-row");
    const rows = Array.from(body.querySelectorAll(".mx-row"));
    const items = Array.from(rowEl.querySelectorAll("[data-nav]"));
    const i = items.indexOf(el), r = rows.indexOf(rowEl);
    let next = null;
    switch (e.key) {
      case "ArrowRight": next = items[i + 1]; break;
      case "ArrowLeft": next = items[i - 1]; break;
      case "Home": next = e.ctrlKey ? rows[0].querySelector("[data-nav]") : items[0]; break;
      case "End": next = e.ctrlKey ? Array.from(rows[rows.length - 1].querySelectorAll("[data-nav]")).pop() : items[items.length - 1]; break;
      case "ArrowDown":
      case "ArrowUp": {
        const other = rows[r + (e.key === "ArrowDown" ? 1 : -1)];
        if (other) {
          const o = Array.from(other.querySelectorAll("[data-nav]"));
          next = o[Math.min(i, o.length - 1)];
        }
        break;
      }
      case "Enter":
      case " ":
        if (el.dataset.slot) { e.preventDefault(); onOpen("slot", Number(el.dataset.slot), el); }
        return;
      default: return;
    }
    e.preventDefault();
    move(next);
  });

  grid.addEventListener("click", (e) => {
    const cell = e.target.closest(".cell[data-slot]");
    if (cell) { current = cell.dataset.fk; roving(); onOpen("slot", Number(cell.dataset.slot), cell); return; }
    const menu = e.target.closest("[data-row-menu]");
    if (menu) onMenu(menu.dataset.rowMenu, menu);
  });

  grid.addEventListener("pointerover", (e) => {
    const cell = e.target.closest(".cell[data-slot]");
    if (cell) showTip(cell, html`<strong class="tip-title">${cell.closest(".mx-row").dataset.key}</strong><span class="tip-text">${cell.getAttribute("aria-label")}</span>`);
  });
  grid.addEventListener("pointerout", (e) => {
    const cell = e.target.closest(".cell[data-slot]");
    if (cell) hideTip(cell);
  });

  // Afterglow: a 600 ms ring on the change, then a 10 s halo. Its colour is not a state colour (so a
  // just-freed slot doesn't read as "healthy" or "selected"): neutral when a run finished, accent
  // when one was claimed, red when failures went up. Applied after rendering: a new row starts clean.
  function glow(recorder, fresh) {
    const now = Date.now();
    const latest = new Map();
    for (const e of recorder.events) {
      if (e.slot == null || !e.seen || now - e.seen > HALO_MS) continue;
      if (["claimed", "finished", "failure", "reset"].indexOf(e.type) === -1) continue;
      latest.set(e.slot, e);
    }
    const freshIds = new Set((fresh || []).filter((e) => e.slot != null).map((e) => e.slot));
    body.querySelectorAll(".cell[data-slot]").forEach((cell) => {
      const e = latest.get(Number(cell.dataset.slot));
      const tone = !e ? "" : e.type === "failure" || e.failed ? "bad" : e.type === "finished" || e.type === "reset" ? "ok" : "neutral";
      ["changed-ok", "changed-bad", "changed-neutral"].forEach((c) => cell.classList.toggle(c, c === "changed-" + tone));
      if (freshIds.has(Number(cell.dataset.slot))) {
        cell.classList.remove("flash");
        void cell.offsetWidth; // restart the animation
        cell.classList.add("flash");
        setTimeout(() => cell.classList.remove("flash"), FLASH_MS);
      }
    });
  }

  // The retry sweep: a failing slot's cooldown drawn as a filling conic.
  function sweep(now, cooldownS) {
    body.querySelectorAll(".cell[data-retry]").forEach((cell) => {
      const left = (parseTime(cell.dataset.retry) - now) / 1000;
      const p = Math.max(0, Math.min(1, 1 - left / cooldownS));
      cell.style.setProperty("--sweep", (p * 360).toFixed(1) + "deg");
    });
  }

  return {
    render, glow, sweep,
    focusFirst() { const all = navs(); const cur = all.find((el) => el.tabIndex === 0) || all[0]; if (cur) cur.focus(); },
  };
}
