// Hash routes. Every view, filter and object has an address, and back/forward walk through them.
//   #/overview  #/activity?node=&slot=&kind=&range=  #/sessions?filter=  #/access
//   #/slot/<id>  #/node/<name>  #/session/<slotId>     (the inspector, over the last view)
export const VIEWS = ["overview", "activity", "sessions", "access"];
const INSPECT = { slot: 1, node: 1, session: 1 };
const LEGACY = { "#access": "#/access", "#sessions": "#/sessions", "#overview": "#/overview", "#activity": "#/activity" };

export function parse(hash) {
  hash = hash || "";
  if (LEGACY[hash]) hash = LEGACY[hash];
  const body = hash.replace(/^#\/?/, "");
  const q = body.indexOf("?");
  const path = (q === -1 ? body : body.slice(0, q)).split("/").map((p) => {
    try { return decodeURIComponent(p); } catch (e) { return p; }
  });
  const params = {};
  new URLSearchParams(q === -1 ? "" : body.slice(q + 1)).forEach((v, k) => { params[k] = v; });
  if (INSPECT[path[0]] && path.length > 1 && path[1] !== "") {
    return { view: null, params, inspect: { type: path[0], id: path.slice(1).join("/") } };
  }
  return { view: VIEWS.indexOf(path[0]) !== -1 ? path[0] : "overview", params, inspect: null };
}

export function build(view, params) {
  const qs = new URLSearchParams();
  Object.keys(params || {}).forEach((k) => { if (params[k] != null && params[k] !== "" && params[k] !== "all") qs.set(k, params[k]); });
  const s = qs.toString();
  return "#/" + view + (s ? "?" + s : "");
}

export function inspectHash(type, id) {
  return "#/" + type + "/" + encodeURIComponent(id);
}

const listeners = new Set();
let current = parse(location.hash);
// The view under the inspector. A deep link to an object opens the view it belongs to.
let baseView = current.view || (current.inspect && current.inspect.type === "session" ? "sessions" : "overview");
let baseParams = current.view ? current.params : {};

function dispatch() {
  current = parse(location.hash);
  if (current.view) { baseView = current.view; baseParams = current.params; }
  listeners.forEach((fn) => fn(state()));
}

export function state() {
  return { view: baseView, params: baseParams, inspect: current.inspect };
}

export function onRoute(fn) {
  listeners.add(fn);
}

export function go(hash, { replace = false } = {}) {
  if (hash === location.hash) return dispatch();
  if (replace) {
    history.replaceState(history.state, "", hash);
    dispatch();
  } else {
    location.hash = hash; // hashchange dispatches
  }
}

export function viewHash() {
  return build(baseView, baseParams);
}

export function start() {
  window.addEventListener("hashchange", dispatch);
  if (LEGACY[location.hash]) history.replaceState(null, "", LEGACY[location.hash]);
  dispatch();
}
