// Confirms and small modal sheets, on native <dialog>. Cancel is focused first, so Enter alone
// never destroys anything.
import { html, setHTML } from "../html.js";

// opts: { title, text, ok, danger (default true), items: [{ text, tone, label }], note, facts: [[k, v]] }
export function confirm(opts) {
  return new Promise((resolve) => {
    const dlg = document.getElementById("confirm-dialog");
    const danger = opts.danger !== false;
    dlg.__html = null;
    setHTML(dlg, html`
      <form method="dialog" class="confirm">
        <h2 id="confirm-title">${opts.title}</h2>
        <p id="confirm-text">${opts.text || ""}</p>
        ${opts.facts && opts.facts.length ? html`<dl class="confirm-facts">${opts.facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>` : ""}
        <ul class="confirm-list" id="confirm-list" ${opts.items && opts.items.length ? "" : "hidden"}>
          ${(opts.items || []).map((it) => html`<li title="${it.label || ""}"><span class="dot ${it.tone || "off"}" aria-hidden="true"></span><span class="confirm-name">${it.text}</span><span class="sr-only">${it.label ? " (" + it.label + ")" : ""}</span></li>`)}
        </ul>
        <p class="confirm-note" id="confirm-note" ${opts.note ? "" : "hidden"}>${opts.note || ""}</p>
        <div class="dialog-actions">
          <button type="button" class="btn ghost" data-dismiss>Cancel</button>
          <button class="btn ${danger ? "danger-fill" : "primary"}" value="ok" id="confirm-ok">${opts.ok || "Confirm"}</button>
        </div>
      </form>`);
    dlg.returnValue = "";
    dlg.querySelector("[data-dismiss]").addEventListener("click", () => dlg.close("cancel"));
    dlg.addEventListener("close", function onClose() {
      dlg.removeEventListener("close", onClose);
      resolve(dlg.returnValue === "ok");
    });
    dlg.showModal();
    dlg.querySelector("[data-dismiss]").focus();
  });
}

// Wire a static dialog's [data-dismiss] buttons once.
export function dismissible(dlg) {
  dlg.addEventListener("click", (e) => {
    if (e.target.closest("[data-dismiss]")) dlg.close("cancel");
    // A press on the backdrop (outside the dialog box) closes it; drags that start inside don't.
    else if (e.target === dlg && dlg.__downOutside) {
      const r = dlg.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dlg.close("cancel");
    }
  });
  dlg.addEventListener("pointerdown", (e) => {
    const r = dlg.getBoundingClientRect();
    dlg.__downOutside = e.target === dlg && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom);
  });
}
