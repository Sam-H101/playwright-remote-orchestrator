// The jump palette (/ or Ctrl K): one field that finds nodes, slots, sessions, views and tokens.
import { html, icon, setHTML } from "../html.js";
import { parseSlotQuery } from "../format.js";

// items(): [{ id, group, label, meta, icon, keywords, slot: { node, port }, run() }]
export function score(item, q) {
  if (!q) return item.group === "View" ? 1 : 0;
  const label = item.label.toLowerCase(), hay = (item.label + " " + (item.keywords || "")).toLowerCase();
  const slotQ = parseSlotQuery(q);
  if (slotQ && item.slot) {
    if (String(item.slot.port) === slotQ.slot && item.slot.node.toLowerCase().indexOf(slotQ.node) !== -1) return 100;
  }
  if (label === q) return 90;
  if (label.startsWith(q)) return 70;
  if (label.indexOf(q) !== -1) return 50;
  const words = q.split(/\s+/).filter(Boolean);
  if (words.length > 1 && words.every((w) => hay.indexOf(w) !== -1)) return 30;
  if (hay.indexOf(q) !== -1) return 20;
  return 0;
}

export function createPalette({ items }) {
  const dlg = document.getElementById("palette");
  setHTML(dlg, html`
    <div class="pal-box">
      <div class="pal-field">
        ${icon("i-search")}
        <input id="pal-input" type="text" role="combobox" aria-expanded="true" aria-controls="pal-list" aria-autocomplete="list"
          aria-label="Jump to a node, slot, session or view" placeholder="Jump to node, slot, session…" autocomplete="off" spellcheck="false">
        <button type="button" class="btn ghost small pal-close" data-dismiss>Esc</button>
      </div>
      <ul id="pal-list" role="listbox" aria-label="Results"></ul>
      <p class="pal-foot"><kbd>↑</kbd><kbd>↓</kbd> move <kbd>Enter</kbd> open <kbd>Esc</kbd> close <span class="dim">· try “b81e04/3”</span></p>
    </div>`);
  const input = dlg.querySelector("#pal-input"), list = dlg.querySelector("#pal-list");
  let results = [], active = 0, returnTo = null;

  function search() {
    const q = input.value.trim().toLowerCase();
    results = items().map((it) => ({ it, s: score(it, q) })).filter((r) => r.s > 0)
      .sort((a, b) => b.s - a.s).slice(0, 40).map((r) => r.it);
    active = 0;
    draw();
  }

  function draw() {
    if (!results.length) {
      setHTML(list, html`<li class="pal-empty" role="option" aria-disabled="true" aria-selected="false">Nothing matches. Try a node name, “b81e04 3”, or a view.</li>`);
      input.removeAttribute("aria-activedescendant");
      return;
    }
    let group = null;
    setHTML(list, html`${results.map((it, i) => {
      const head = it.group !== group ? html`<li class="pal-group" role="presentation">${it.group}</li>` : "";
      group = it.group;
      return html`${head}<li role="option" id="pal-opt-${i}" class="pal-opt" data-i="${i}" aria-selected="${i === active}">
        ${icon(it.icon || "i-arrow")}<span class="pal-label ${it.mono ? "mono" : ""}">${it.label}</span><span class="pal-meta">${it.meta || ""}</span></li>`;
    })}`);
    input.setAttribute("aria-activedescendant", "pal-opt-" + active);
    const opt = list.querySelector("#pal-opt-" + active);
    if (opt) opt.scrollIntoView({ block: "nearest" });
  }

  function choose(i) {
    const it = results[i];
    if (!it) return;
    returnTo = null; // the item decides where focus goes
    dlg.close();
    it.run();
  }

  input.addEventListener("input", search);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!results.length) return;
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
      draw();
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(active);
    }
  });
  list.addEventListener("click", (e) => {
    const opt = e.target.closest("[data-i]");
    if (opt) choose(Number(opt.dataset.i));
  });
  dlg.addEventListener("click", (e) => {
    if (e.target.closest("[data-dismiss]") || e.target === dlg) dlg.close();
  });
  dlg.addEventListener("close", () => {
    if (returnTo && returnTo.isConnected) returnTo.focus();
    returnTo = null;
  });

  return {
    open() {
      if (dlg.open) { input.select(); return; }
      returnTo = document.activeElement;
      input.value = "";
      search();
      dlg.showModal();
      input.focus();
    },
    close() { if (dlg.open) { dlg.close(); return true; } return false; },
    isOpen: () => dlg.open,
  };
}
