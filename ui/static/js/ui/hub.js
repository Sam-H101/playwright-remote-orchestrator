// The editable "Hub address" shown wherever a command is offered. Every snippet on the page
// follows it live; the value is remembered in this browser.
import { app } from "../app.js";
import { html } from "../html.js";
import { publicHub } from "../format.js";

let seq = 0;
export function hubField(hint = "") {
  const id = "hub-" + (++seq);
  return html`<div class="field hub-field">
    <label for="${id}">Hub address</label>
    <div class="hub-input">
      <input id="${id}" class="mono" data-hub-input type="text" spellcheck="false" autocomplete="off" autocapitalize="off"
        aria-describedby="${id}-hint">
      <button type="button" class="btn ghost small" data-hub-reset hidden>Reset</button>
    </div>
    <p class="hint" id="${id}-hint">${hint || "Where your hosts and checks reach the hub. Edit it if they use another name; every command follows."}</p>
  </div>`;
}

function sync(input) {
  if (document.activeElement !== input) input.value = app.hub();
  input.placeholder = publicHub(app.data(), location.hostname);
  const reset = input.parentElement.querySelector("[data-hub-reset]");
  if (reset) reset.hidden = !app.hubIsCustom();
}

export function wireHubFields(root) {
  root.querySelectorAll("[data-hub-input]").forEach((input) => {
    if (input.__wired) return sync(input);
    input.__wired = true;
    sync(input);
    let t;
    input.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => app.setHub(input.value), 150);
    });
    input.addEventListener("change", () => app.setHub(input.value));
    input.parentElement.querySelector("[data-hub-reset]").addEventListener("click", () => {
      app.setHub("");
      input.value = app.hub();
      input.focus();
    });
  });
}

app.onHub(() => document.querySelectorAll("[data-hub-input]").forEach(sync));
