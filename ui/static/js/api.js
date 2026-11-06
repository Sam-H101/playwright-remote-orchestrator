// Every request to the grid goes through here, with credentials from auth.js.
import * as auth from "./auth.js";

// kind says which hop failed, so the page can say so: "network" (no answer from the Grid UI
// server at all), "server" (it answered, but not with JSON: a proxy, a crash), "database" (the UI
// is up and its database isn't), "refused" (the status poll, where reading the grid needs a sign-in
// or a role this page doesn't have), or "http" (a refusal the body explains).
export class ApiError extends Error {
  constructor(message, status, body, kind = "http") {
    super(message);
    this.status = status;
    this.body = body || {};
    this.kind = kind;
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
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    throw new ApiError("No answer from the Grid UI server at " + location.host + ".", 0, {}, "network");
  }
  auth.answered(r, opts.headers); // it may say the admin token sent with it no longer works
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (data === null && !r.ok) {
    throw new ApiError("The Grid UI server at " + location.host + " answered HTTP " + r.status + " without data.", r.status, {}, "server");
  }
  return { status: r.status, ok: r.ok, body: data || {} };
}

// A refusal for lack of permission: the page's idea of the person's roles is out of date (a role was
// taken while it was open), so it asks again what they may do.
function refused(r) {
  if (r.status === 403 && r.body.forbidden) auth.refresh();
}

// A read. Never prompts: a refused read marks us signed out and the caller shows that state.
export async function get(url) {
  const r = await send("GET", url);
  if (r.status === 403 && r.body.token_required) auth.markSignedOut(r.body.reason);
  refused(r);
  if (!r.ok) throw new ApiError(r.body.error || "The request failed with HTTP " + r.status + ".", r.status, r.body);
  return r.body;
}

// An action. If the grid says our sign-in is missing or wrong, sign in once and retry once.
// Resolves with the body, or null when sign-in was dismissed; throws ApiError otherwise. Any other
// 403 (a proxy, the origin guard, a role that doesn't allow it: forbidden) is an error: retrying it
// without asking would loop.
export async function post(url, body) {
  let r = await send("POST", url, body);
  if (r.status === 403 && r.body.token_required) {
    auth.markSignedOut(r.body.reason);
    if (!(await auth.signIn("rejected"))) return null;
    r = await send("POST", url, body);
    if (r.status === 403 && r.body.token_required) auth.markSignedOut(r.body.reason);
  }
  refused(r);
  if (!r.ok) throw new ApiError(r.body.error || "The action failed with HTTP " + r.status + ".", r.status, r.body);
  return r.body;
}

// The page's poll. ?soft=1: a database outage comes back as data rather than as an HTTP error the
// browser would log every time, and so does a refusal where reading the grid needs sign-in
// ({refused: "signedout" | "forbidden", error}). ?events=<cursor>: what the server's recorder saw since then.
export async function status(cursor) {
  const body = await get("api/status?soft=1&events=" + encodeURIComponent(cursor == null ? 0 : cursor));
  if (body.unavailable) throw new ApiError(body.error || "The database is unavailable.", 503, body, "database");
  if (body.refused) throw new ApiError(body.error || "Reading the grid needs sign-in.", 403, body, "refused");
  return body;
}
