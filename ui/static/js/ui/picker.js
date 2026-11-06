// A field that finds someone, or a role, as you type: a combobox over a listbox of grouped options, with
// the palette's keys (arrows move, Enter chooses, Esc closes the list). The share dialog adds people and
// roles with it, and the dialogs that give something to someone pick them with it.
import * as api from "../api.js";
import { html, setHTML } from "../html.js";

const DEBOUNCE_MS = 200;

// Whom things can be shared with or given to (GET /api/directory): every role, and the enabled people
// whose name or email holds q, once it has 2 characters. Roles come back whatever q is: they're matched here.
export async function directory(q) {
  const body = await api.get("api/directory?q=" + encodeURIComponent(q));
  const text = q.trim().toLowerCase();
  return {
    roles: (body.roles || []).filter((r) => !text || r.name.toLowerCase().indexOf(text) !== -1),
    users: body.users || [],
  };
}

// host: the element the field goes in; the field's id is <opts.id>-input, for a <label for> to name it.
// opts: { id, placeholder, describedBy,
//   options(q): what matches q, [{ label, options: [{ key, label, meta, note }] }] (or a promise of it); a
//     group without a label has no heading,
//   empty(q): what the list says when nothing matches,
//   choose(option): what choosing it does (it may return a promise); resolving false keeps what was typed }.
export function createPicker(host, opts) {
  const id = opts.id;
  setHTML(host, html`<div class="picker">
    <input type="text" id="${id}-input" role="combobox" aria-expanded="false" aria-controls="${id}-list" aria-autocomplete="list"
      autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${opts.placeholder || ""}"${opts.describedBy ? html` aria-describedby="${opts.describedBy}"` : ""}>
    <div class="picker-list" id="${id}-list" role="listbox" aria-label="Matches" hidden></div>
  </div>`);
  const input = host.querySelector("input"), list = host.querySelector("[role=listbox]");
  // groups: the last answer; flat: its options in order; active: the one the arrows are on (-1: none).
  let groups = [], flat = [], active = -1, failed = "", open = false, busy = false, seq = 0, timer = null;

  function draw() {
    let i = 0;
    const shown = groups.filter((g) => g.options.length);
    if (!shown.length) {
      setHTML(list, html`<div class="picker-empty" role="option" aria-disabled="true" aria-selected="false">${failed ||
        (opts.empty ? opts.empty(input.value.trim()) : "Nothing matches.")}</div>`);
      input.removeAttribute("aria-activedescendant");
      return;
    }
    setHTML(list, html`${shown.map((g, n) => html`<div role="group"${g.label ? html` aria-labelledby="${id}-g${n}"` : ""}>
      ${g.label ? html`<div class="picker-group" id="${id}-g${n}" role="presentation">${g.label}</div>` : ""}
      ${g.options.map((o) => {
        const at = i++;
        return html`<div role="option" class="picker-opt" id="${id}-o${at}" data-i="${at}" aria-selected="${String(at === active)}">
          <span class="picker-name">${o.label}</span>${o.meta ? html`<span class="picker-meta">${o.meta}</span>` : ""}${o.note
            ? html`<small class="picker-note">${o.note}</small>` : ""}</div>`;
      })}</div>`)}`);
    mark();
  }

  // The option the arrows are on, for assistive technology and the eye.
  function mark() {
    list.querySelectorAll("[data-i]").forEach((el) => el.setAttribute("aria-selected", String(Number(el.dataset.i) === active)));
    const el = active >= 0 && list.querySelector("#" + id + "-o" + active);
    if (el) {
      input.setAttribute("aria-activedescendant", el.id);
      el.scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  // Asks what matches what's typed now. An answer that a later question overtook is dropped; the option
  // the arrows were on stays on, if it's still there.
  async function look() {
    clearTimeout(timer);
    const ticket = ++seq, q = input.value.trim();
    let found = [], error = "";
    try {
      found = await opts.options(q);
    } catch (err) {
      error = "Can't look them up: " + err.message;
    }
    if (ticket !== seq || !input.isConnected || document.activeElement !== input) return;
    const was = active >= 0 && flat[active] ? flat[active].key : null;
    groups = found || [];
    failed = error;
    flat = groups.reduce((all, g) => all.concat(g.options), []);
    active = was == null ? -1 : flat.findIndex((o) => o.key === was);
    draw();
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    open = true;
  }

  function close() {
    seq++; // an answer on its way has nowhere to go
    clearTimeout(timer);
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
    open = false;
  }

  function move(step) {
    if (!flat.length) return;
    active = active < 0 ? (step > 0 ? 0 : flat.length - 1) : (active + step + flat.length) % flat.length;
    mark();
  }

  async function pick(option) {
    if (busy || !option) return;
    busy = true;
    close();
    input.setAttribute("aria-busy", "true");
    let kept = false;
    try {
      kept = (await opts.choose(option)) === false;
    } finally {
      busy = false;
      input.removeAttribute("aria-busy");
    }
    if (!kept) input.value = "";
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(look, DEBOUNCE_MS);
  });
  input.addEventListener("click", () => { if (!open) look(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (open) move(e.key === "ArrowDown" ? 1 : -1);
      else look();
    } else if (e.key === "Enter") {
      e.preventDefault(); // never a form's submit
      if (open && active >= 0) pick(flat[active]);
    } else if (e.key === "Escape" && (open || input.value)) {
      // The list closes first, then what was typed goes; only then does Esc close the dialog around it.
      e.preventDefault();
      e.stopPropagation();
      if (open) close(); else input.value = "";
    }
  });
  // Pressing an option mustn't take focus from the field: the list would close before the click.
  list.addEventListener("mousedown", (e) => e.preventDefault());
  list.addEventListener("click", (e) => {
    const opt = e.target.closest("[data-i]");
    if (opt) pick(flat[Number(opt.dataset.i)]);
  });
  host.addEventListener("focusout", (e) => { if (!host.contains(e.relatedTarget)) close(); });

  return {
    input,
    focus() { input.focus(); },
    close,
  };
}
