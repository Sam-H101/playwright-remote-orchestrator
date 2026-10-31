'use strict';

// Who may use the hub: the env bootstrap tokens (GATEWAY_AGENT_TOKEN / GATEWAY_CLIENT_TOKEN) and the
// managed tokens in the accessTokens table, which the Grid UI creates and revokes. Lookups are cached
// so a burst of connections doesn't hammer the database.

const P = require('./protocol');

const NEGATIVE_CACHE_MS = 2000;
const TOUCH_INTERVAL_MS = 60000;
const MAX_CACHE_ENTRIES = 5000;

// Why a managed token row doesn't let `kind` in, or null when it does.
function tokenProblem(row, kind, now = Date.now()) {
  if (!row) return 'unknown';
  if (row.kind !== kind) return kind === 'agent' ? 'not an agent token' : 'not a client token';
  if (row.revokedAt) return 'revoked';
  if (row.expiresAt && Date.parse(row.expiresAt) <= now) return 'expired';
  return null;
}

// Verdicts: { ok: true, via: 'env' | 'managed' | 'open', tokenId, nodeName, label }
//           { ok: false, unavailable: true, message }   the database couldn't answer: fail closed
//           { ok: false, message }                       missing, unknown, revoked, expired or wrong-kind token
// `allowOpen` (GATEWAY_ALLOW_OPEN_AGENTS / _CLIENTS) opts a kind back into the old rule: once every managed
// token of it is revoked, it is let in without a token again.
function createTokenAuth({ registry, log, envTokens = {}, allowOpen = {}, cacheMs = 10000, negativeCacheMs, touchMs = TOUCH_INTERVAL_MS }) {
  const negativeMs = negativeCacheMs ?? Math.min(NEGATIVE_CACHE_MS, cacheMs);
  const lookups = new Map(); // hash -> { value: token row or null, expires }
  const switches = new Map(); // kind -> { value: enforcement on, expires }
  const lastKnown = new Map(); // kind -> last enforcement answer the database gave, used while it is down
  const inflight = new Map(); // key -> promise, so concurrent misses share one query
  const touched = new Map(); // token id -> when lastUsedAt was last written
  let generation = 0; // bumped by clear(); answers to queries started before it aren't cached

  function cached(map, key, ttl, query) {
    const hit = map.get(key);
    if (hit && hit.expires > Date.now()) return Promise.resolve(hit.value);
    const flightKey = `${map === lookups ? 'l' : 's'}:${key}`;
    if (inflight.has(flightKey)) return inflight.get(flightKey);
    const gen = generation;
    const promise = Promise.resolve().then(query).then((value) => {
      const ms = ttl(value);
      if (gen === generation && ms > 0) {
        if (map.size >= MAX_CACHE_ENTRIES) prune(map);
        map.set(key, { value, expires: Date.now() + ms });
      }
      return value;
    }).finally(() => {
      if (inflight.get(flightKey) === promise) inflight.delete(flightKey);
    });
    inflight.set(flightKey, promise);
    return promise;
  }

  function prune(map) {
    const now = Date.now();
    for (const [key, entry] of map) if (entry.expires <= now) map.delete(key);
    if (map.size >= MAX_CACHE_ENTRIES) map.clear(); // a flood of made-up tokens
  }

  function lookup(hash) {
    return cached(lookups, hash, (row) => (row ? cacheMs : negativeMs), () => registry.lookupToken(hash));
  }

  // Enforcement is on for a kind once a managed token of that kind exists, and it stays on: revoking a
  // leaked token (or deleting the revoked rows) must not let in whoever simply leaves the token out.
  // Only allowOpen, a hub restart after every row of the kind is deleted, or no token ever, means off.
  async function enforced(kind) {
    if (lastKnown.get(kind) === true && !allowOpen[kind]) return true;
    try {
      const on = await cached(switches, kind, () => cacheMs, () => registry.anyTokens(kind, !!allowOpen[kind]));
      if (lastKnown.has(kind) && lastKnown.get(kind) !== on) {
        if (on) log.info(`a managed ${kind} token exists now: ${kind}s must present a valid token`);
        else log.warn(`!!! every managed ${kind} token is revoked and GATEWAY_ALLOW_OPEN_${kind.toUpperCase()}S is set: `
          + `ANY ${kind} that can reach this port is let in again, with or without a token !!!`);
      }
      lastKnown.set(kind, on);
      return on;
    } catch (err) {
      if (!lastKnown.has(kind)) throw err;
      // Off stays off while the database is away (there was no token to enforce); on means the managed
      // token lookup that follows has to reach the database, and fails closed if it can't.
      return lastKnown.get(kind);
    }
  }

  // Rate-limited and fire-and-forget: a failed write is logged and tried again on the next use.
  function touch(id) {
    const now = Date.now();
    if (now - (touched.get(id) || 0) < touchMs) return;
    touched.set(id, now);
    Promise.resolve().then(() => registry.touchToken(id)).catch((err) => {
      touched.delete(id);
      log.warn(`could not record the use of access token #${id}: ${err.message}`);
    });
  }

  // `token` is the presented string (or null). The env token is compared first and never needs the database.
  // Only the shapes the Grid UI makes can be in accessTokens, so anything else is unknown without a query:
  // made-up tokens can't queue up work on the database, and a mistyped env token is refused as wrong
  // (4401/401) rather than "database unavailable".
  function check(kind, token) {
    const env = envTokens[kind] || null;
    if (env && P.tokenMatches(env, token)) return Promise.resolve({ ok: true, via: 'env', tokenId: null, nodeName: null, label: 'the bootstrap token' });
    if (token && !P.isManagedToken(token)) return unmatched(kind, 'unknown token');
    return checkHash(kind, token ? P.hashToken(token) : null, P.tokenLabel(token));
  }

  // The same check from a stored hash; the revocation re-check uses it, so the hub never keeps a token.
  // A managed token that is revoked, expired or of the other kind is refused even while enforcement is
  // off (only possible with allowOpen).
  async function checkHash(kind, hash, label = 'token') {
    const unavailable = (err) => ({ ok: false, unavailable: true, message: `could not check the ${kind} token: ${err.message}` });
    let row = null;
    if (hash) {
      try {
        row = await lookup(hash);
      } catch (err) {
        return unavailable(err); // fail closed: it may be a revoked token
      }
    }
    if (row) {
      const problem = tokenProblem(row, kind);
      if (problem) return { ok: false, problem, message: `${row.kind} token ${row.prefix}... (#${row.id}) is ${problem}` };
      touch(row.id);
      return { ok: true, via: 'managed', tokenId: row.id, nodeName: row.nodeName || null, label: `token ${row.prefix}... (#${row.id} "${row.name}")` };
    }
    return unmatched(kind, hash ? `unknown ${label}` : `missing ${kind} token`);
  }

  // No managed token matched: refused with `message` while enforcement is on, let in while it is off.
  async function unmatched(kind, message) {
    let on = !!envTokens[kind];
    if (!on) {
      try {
        on = await enforced(kind);
      } catch (err) {
        return { ok: false, unavailable: true, message: `could not check the ${kind} token: ${err.message}` };
      }
    }
    if (!on) return { ok: true, via: 'open', tokenId: null, nodeName: null, label: 'no token (none required)' };
    return { ok: false, message };
  }

  // Drops every cached answer so the next checks go to the database.
  function clear() {
    generation++;
    lookups.clear();
    switches.clear();
    inflight.clear();
    const now = Date.now();
    for (const [id, at] of touched) if (now - at >= touchMs) touched.delete(id);
  }

  return { check, checkHash, enforced, clear, touch };
}

module.exports = { createTokenAuth, tokenProblem };
