// Every request to the grid goes through here, with credentials from auth.js.
import * as auth from "./auth.js";

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body || {};
  }
}

async function send(method, url, body) {
  const opts = {
    method, cache: "no-store", credentials: "same-origin",
    headers: Object.assign({ Accept: "application/json" }, auth.headers()),
  };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const r = await fetch(url, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { data = {}; }
  return { status: r.status, ok: r.ok, body: data || {} };
}

// A read. Never prompts: a refused read marks us signed out and the caller shows that state.
export async function get(url) {
  const r = await send("GET", url);
  if (r.status === 403 && r.body.token_required) auth.markSignedOut();
  if (!r.ok) throw new ApiError(r.body.error || "The request failed with HTTP " + r.status + ".", r.status, r.body);
  return r.body;
}

// An action. If the grid says our sign-in is missing or wrong, sign in once and retry once.
// Resolves with the body, or null when sign-in was dismissed; throws ApiError otherwise. Any other
// 403 (a proxy, the origin guard) is an error: retrying it without asking would loop.
export async function post(url, body) {
  let r = await send("POST", url, body);
  if (r.status === 403 && r.body.token_required) {
    auth.markSignedOut();
    if (!(await auth.signIn("rejected"))) return null;
    r = await send("POST", url, body);
    if (r.status === 403 && r.body.token_required) auth.markSignedOut();
  }
  if (!r.ok) throw new ApiError(r.body.error || "The action failed with HTTP " + r.status + ".", r.status, r.body);
  return r.body;
}

// The page polls with ?soft=1, so a database outage comes back as data rather than as an HTTP
// error the browser would log on every poll.
export async function status() {
  const body = await get("api/status?soft=1");
  if (body.unavailable) throw new ApiError(body.error || "The database is unavailable.", 503, body);
  return body;
}
