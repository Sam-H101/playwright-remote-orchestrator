// Transient messages, a shared tooltip, and copying to the clipboard.
import { html, setHTML } from "../html.js";

let timer;
export function toast(text, bad = false) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 3000);
}

export async function copy(text, what) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) { ok = false; }
  if (!ok) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.className = "offscreen";
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
  }
  if (ok) toast("Copied " + (what || "to the clipboard"));
  else toast("Copy isn't available here. Select the text and copy it instead.", true);
  return ok;
}

// One tooltip for the whole page, placed next to the element it describes. Content is html``.
let tipFor = null;
export function showTip(target, markup) {
  const tip = document.getElementById("tip");
  setHTML(tip, markup);
  tip.hidden = false;
  tipFor = target;
  const r = target.getBoundingClientRect(), t = tip.getBoundingClientRect();
  let x = r.left + r.width / 2 - t.width / 2, y = r.top - t.height - 8;
  if (y < 52) y = r.bottom + 8;
  x = Math.max(8, Math.min(window.innerWidth - t.width - 8, x));
  tip.style.setProperty("--tip-x", Math.round(x) + "px");
  tip.style.setProperty("--tip-y", Math.round(y) + "px");
}

export function hideTip(target) {
  if (target && tipFor !== target) return;
  const tip = document.getElementById("tip");
  tip.hidden = true;
  tipFor = null;
}

export function tipLines(title, rows) {
  return html`<strong class="tip-title">${title}</strong>${rows.length ? html`<dl class="tip-rows">${rows.map(
    ([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>` : ""}`;
}
