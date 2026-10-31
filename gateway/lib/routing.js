'use strict';

// Which slot a /connect client gets. Pure functions over the slot rows (registry.slotStates) and the
// agents connected to this hub; the hub does the claiming, queueing and piping.

const { isValidName, canonicalName } = require('./protocol');

// Playwright's own names: chromium, firefox, webkit, android, _bidiChromium, ...
const BROWSER_RE = /^[a-z0-9_][a-z0-9._-]{0,63}$/;
// A node may list Chromium channels or builds (chrome, msedge-beta, chromium-headless-shell, ...)
// instead of, or next to, chromium; any of them serves a client that asks for chromium.
const CHROMIUM_FAMILY = /^(chromium|chrome|msedge)(-|$)/;

// What the Playwright client asks for, read the way run-server reads it: the browser from ?browser= or
// else the x-playwright-browser header (browserType.connect sends it, in JS and Python), the client
// version from the User-Agent ("Playwright/1.64.1 (x64; windows 10.0) node/20.17"), and ?node= to
// stay on one node. Resolves { want: { browser, version, node } } or { error } for a 400.
function clientWants(searchParams, headers) {
  let browser = searchParams.get('browser');
  if (!browser) {
    const header = headers['x-playwright-browser'];
    browser = Array.isArray(header) ? header[0] : header;
  }
  browser = browser ? String(browser).trim().toLowerCase() : null;
  if (browser && !BROWSER_RE.test(browser)) return { error: 'bad browser name (x-playwright-browser / ?browser=)' };
  let node = searchParams.get('node');
  if (node !== null) {
    if (!isValidName(node)) return { error: 'node must be 1-128 characters of A-Z a-z 0-9 . _ -' };
    node = canonicalName(node);
  }
  return { want: { browser, version: clientVersion(headers['user-agent']), node } };
}

// The same match run-server uses for its version check (major.minor must agree).
function clientVersion(userAgent) {
  const match = /^Playwright\/(\d+)\.(\d+)\.\d+/.exec(typeof userAgent === 'string' ? userAgent : '');
  return match ? `${match[1]}.${match[2]}` : null;
}

// major.minor of a node's playwright-core version, or null when it isn't a version ("unknown").
function majorMinor(version) {
  const match = /^(\d+)\.(\d+)(?:\.|$)/.exec(typeof version === 'string' ? version : '');
  return match ? `${Number(match[1])}.${Number(match[2])}` : null;
}

// A server that lists no browsers (a static host, or an agent that reports none) takes any.
function offers(browsers, browser) {
  if (!browser || !browsers.length) return true;
  return browsers.some((b) => {
    const name = String(b).toLowerCase();
    return name === browser || (browser === 'chromium' && CHROMIUM_FAMILY.test(name));
  });
}

// Every server that has slots or is connected: { key, name, gateway, connected, agent, browsers, version, slots }.
// A connected agent's own report beats its (possibly older) nodes row.
function servers(rows, agents) {
  const out = new Map();
  for (const agent of agents.values()) {
    out.set(agent.name, {
      key: agent.name, name: agent.name, gateway: true, connected: true, agent,
      browsers: agent.info.browsers, version: majorMinor(agent.info.playwrightVersion), slots: [],
    });
  }
  for (const row of rows) {
    const key = canonicalName(row.server);
    let server = out.get(key);
    if (!server) {
      server = row.node
        ? { key, name: key, gateway: true, connected: false, agent: null, browsers: row.node.browsers, version: majorMinor(row.node.playwrightVersion), slots: [] }
        : { key, name: row.server, gateway: false, connected: false, agent: null, browsers: [], version: null, slots: [] };
      out.set(key, server);
    }
    server.slots.push(row);
  }
  return [...out.values()];
}

// Why `server` can't serve `want`, or null if it can.
function mismatch(server, want) {
  if (want.node && server.key !== want.node) return 'node';
  if (!offers(server.browsers, want.browser)) return 'browser';
  if (want.version && server.version && server.version !== want.version) return 'version';
  return null;
}

// The state of one slot row for this request: free, busy, failing, unhealthy, offline or tried (it
// already failed this request). `taken` holds slot ids claimed since the rows were read.
function slotState(server, row, failed, taken) {
  if (failed.has(row.id)) return 'tried';
  if (server.gateway) {
    if (!server.connected) return 'offline';
    const live = server.agent.slots[row.slot - 1];
    if (!live) return 'offline'; // a slot beyond the node's current count, kept while a check holds it
    if (row.inUse || taken.has(row.id)) return 'busy';
    if (!live.healthy) return 'unhealthy';
  } else if (row.inUse || taken.has(row.id)) {
    return 'busy';
  }
  return row.failing ? 'failing' : 'free';
}

const list = (items) => [...new Set(items)].sort().join(', ');

// Sizes up the grid for one request. Resolves:
//   { fatal: { status, message } }  nothing connected or known could ever serve it
//   { free: [{ server, row }], summary }  free slots it may have (possibly none), and what the
//                                   matching slots are doing, for a timeout message
function evaluate(want, rows, agents, { failed = new Set(), taken = new Set(), lastError = null } = {}) {
  const all = servers(rows, agents);
  const matching = all.filter((s) => !mismatch(s, want));
  const browser = want.browser || 'browser';
  if (!matching.length) {
    if (want.node && !all.some((s) => s.key === want.node)) {
      return { fatal: { status: 404, message: `no node or static host named ${want.node} is known to the hub` } };
    }
    const pinned = want.node ? all.filter((s) => s.key === want.node) : all;
    const withBrowser = pinned.filter((s) => offers(s.browsers, want.browser));
    if (!withBrowser.length) {
      const offered = list(pinned.flatMap((s) => s.browsers));
      const where = want.node ? `node ${want.node} does not offer ${browser}` : `no node offers ${browser}`;
      return { fatal: { status: 503, message: `${where}${offered ? ` (offered: ${offered})` : ''}` } };
    }
    const versions = list(withBrowser.map((s) => s.version).filter(Boolean));
    const where = want.node ? `node ${want.node} runs` : `the nodes offering ${browser} run`;
    return { fatal: { status: 428, message: `Playwright version mismatch: the client is ${want.version}, ${where} ${versions}. Keep the client on the grid's Playwright version` } };
  }

  const counts = {};
  let total = 0;
  let retryInMs = Infinity;
  const free = [];
  const count = (state) => {
    total++;
    counts[state] = (counts[state] || 0) + 1;
  };
  for (const server of matching) {
    // A connected node's slots that aren't in the database yet (its register write is on the way).
    const known = new Set(server.slots.map((r) => r.slot));
    if (server.connected) for (let i = 1; i <= server.agent.slots.length; i++) if (!known.has(i)) count('starting');
    for (const row of server.slots) {
      const state = slotState(server, row, failed, taken);
      if (state === 'offline' && server.connected) continue; // removed from a connected node
      count(state);
      if (state === 'free') free.push({ server, row });
      if (state === 'failing') retryInMs = Math.min(retryInMs, row.retryInMs);
    }
  }
  if (total && counts.tried === total) {
    return { fatal: lastError || { status: 502, message: `every matching slot failed to open a session` } };
  }
  if (!total) {
    // Matching servers exist but have no slots at all (a static host whose rows were all removed).
    return { fatal: { status: 503, message: `no ${browser} slot is known to the hub` } };
  }
  return { free, summary: summarize(counts, total, retryInMs) };
}

function summarize(counts, total, retryInMs) {
  const noun = `${total} matching slot${total === 1 ? '' : 's'}`;
  const parts = Object.entries(counts).filter(([state]) => state !== 'free');
  if (parts.length === 1) {
    const [state] = parts[0];
    return `${noun}, ${total === 1 ? '' : 'all '}${describeState(state, retryInMs)}`;
  }
  return `${noun}: ${parts.map(([state, n]) => `${n} ${describeState(state, retryInMs)}`).join(', ')}`;
}

function describeState(state, retryInMs) {
  if (state === 'failing') return `failing${Number.isFinite(retryInMs) ? ` (next retry in ${Math.ceil(retryInMs / 1000)}s)` : ''}`;
  if (state === 'tried') return 'already tried';
  return state;
}

// Gateway nodes before static hosts, then the server with the largest share of its slots free (random
// among equals), then a random free slot on it.
function choose(free, random = Math.random) {
  if (!free.length) return null;
  const byServer = new Map();
  for (const entry of free) {
    if (!byServer.has(entry.server)) byServer.set(entry.server, []);
    byServer.get(entry.server).push(entry);
  }
  const pool = [...byServer.keys()].some((s) => s.gateway) ? [...byServer.keys()].filter((s) => s.gateway) : [...byServer.keys()];
  const size = (s) => (s.connected ? Math.max(s.agent.slots.length, 1) : Math.max(s.slots.length, 1));
  const share = (s) => byServer.get(s).length / size(s);
  const best = Math.max(...pool.map(share));
  const top = pool.filter((s) => share(s) === best);
  const server = top[Math.floor(random() * top.length)];
  const entries = byServer.get(server);
  return entries[Math.floor(random() * entries.length)];
}

module.exports = { clientWants, clientVersion, majorMinor, offers, evaluate, choose, summarize };
