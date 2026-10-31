// The one way markup is built: html`...` escapes every interpolation. Nested html`` results and
// arrays of them pass through unescaped because they were escaped when they were made. raw() is
// the only opt-out and is reserved for constant markup, never data.

class Safe {
  constructor(text) { this.text = text; }
  toString() { return this.text; }
}

const ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" };

export function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"'`]/g, (c) => ENTITIES[c]);
}

function part(value) {
  if (value == null || value === false || value === true) return "";
  if (value instanceof Safe) return value.text;
  if (Array.isArray(value)) return value.map(part).join("");
  return esc(value);
}

export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += part(values[i]) + strings[i + 1];
  return new Safe(out);
}

export function raw(text) {
  return new Safe(String(text));
}

export const isSafe = (v) => v instanceof Safe;

export function icon(id, cls = "") {
  return html`<svg class="i ${cls}" aria-hidden="true" focusable="false"><use href="#${id}"></use></svg>`;
}

// Set an element's content, skipping the write when it would not change anything. Leaving the
// DOM alone keeps text selections, focus and hover states across polls.
export function setHTML(el, markup) {
  if (!(markup instanceof Safe)) throw new TypeError("setHTML takes html`` markup");
  if (el.__html === markup.text) return false;
  el.__html = markup.text;
  el.innerHTML = markup.text;
  return true;
}

const scratch = document.createElement("template");
function toElement(text) {
  scratch.innerHTML = text;
  return scratch.content.firstElementChild;
}

export function setText(el, text) {
  text = String(text);
  if (el.textContent !== text) el.textContent = text;
}

// Patch a list of keyed children (each item's markup has a single root carrying data-key).
// Unchanged items are left untouched; changed ones are swapped; focus survives a swap when the
// focused element carries data-fk.
export function patchList(container, items, keyOf, render) {
  const active = document.activeElement;
  const fk = active && container.contains(active) && active.dataset ? active.dataset.fk : null;
  const existing = new Map();
  for (const child of Array.from(container.children)) {
    if (child.dataset.key != null) existing.set(child.dataset.key, child);
    else child.remove();
  }
  items.forEach((item, i) => {
    const key = String(keyOf(item));
    const markup = render(item);
    if (!(markup instanceof Safe)) throw new TypeError("patchList render must return html``");
    let el = existing.get(key);
    if (el && el.__html !== markup.text) {
      const fresh = toElement(markup.text);
      fresh.__html = markup.text;
      el.replaceWith(fresh);
      el = fresh;
    } else if (!el) {
      el = toElement(markup.text);
      el.__html = markup.text;
    }
    existing.delete(key);
    const at = container.children[i];
    if (at !== el) container.insertBefore(el, at || null);
  });
  existing.forEach((el) => el.remove());
  if (fk && !active.isConnected) {
    const again = container.querySelector('[data-fk="' + CSS.escape(fk) + '"]');
    if (again) again.focus({ preventScroll: true });
  }
}
