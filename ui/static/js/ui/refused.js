// What the overview, sessions and activity show in place of the grid where reading it needs sign-in
// (GRID_UI_READS) and this page may not: live.refused says why, "signedout" or "forbidden".
import { html, icon } from "../html.js";

export function refusedPanel(why) {
  if (why === "forbidden") {
    return html`<div class="empty calm">${icon("i-lock", "empty-mark")}
      <h2>You don't have access to the grid view</h2>
      <p>Your roles don't include “See the grid”. Ask an admin for a role that does.</p>
      <button type="button" class="btn" data-your-access>Your access</button></div>`;
  }
  return html`<div class="empty calm">${icon("i-lock", "empty-mark")}
    <h2>Sign in to see the grid</h2>
    <p>This grid needs sign-in to view it.</p>
    <button type="button" class="btn primary" data-signin>Sign in</button></div>`;
}
