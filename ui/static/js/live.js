// The flight recorder. /api/status is a snapshot; diff() turns two snapshots into events, the
// Recorder keeps the last 15 minutes of them for this tab, and createLive() polls and feeds both
// through one subscribe() adapter. A server-side event source could replace diff() and the
// polling without any view noticing: views only see { data, events, recorder }.
import { slotName, parseTime, runLength, plural, stateWord } from "./format.js";

export const WINDOW_MS = 15 * 60 * 1000;
export const MAX_EVENTS = 500;
const PROBLEM = { failing: 1, unhealthy: 1, offline: 1 };

function index(status) {
  const nodes = new Map(), slots = new Map();
  for (const n of (status && status.nodes) || []) {
    nodes.set(n.host, n);
    for (const s of n.slots || []) slots.set(s.id, { s, n });
  }
  return { nodes, slots };
}

// Everything that changed between two snapshots, as events. Pure: same input, same output.
// mine: { slots: Map(slotId -> action), nodes: Map(host -> action) } for admin actions this tab
// just took, so their effects can be attributed to "you".
export function diff(prev, next, mine) {
  const events = [];
  if (!prev || !next) return events;
  mine = mine || { slots: new Map(), nodes: new Map() };
  const now = parseTime(next.generated);
  const P = index(prev), N = index(next);

  const flipped = new Set();
  for (const [host, n] of N.nodes) {
    const p = P.nodes.get(host);
    if (!p) {
      events.push({ type: "node-joined", kind: "node", t: now, node: host, subject: host, slots: n.total, nodeKind: n.kind });
    } else if (n.kind === "gateway" && !!p.online !== !!n.online) {
      flipped.add(host);
      events.push({ type: n.online ? "node-online" : "node-offline", kind: "node", t: now, node: host, subject: host, away: n.last_seen_s });
    }
  }
  for (const [host, p] of P.nodes) {
    if (N.nodes.has(host)) continue;
    const you = mine.nodes.get(host) === "forget";
    events.push({ type: "node-left", kind: you ? "admin" : "node", t: now, node: host, subject: host, slots: p.total, by: you ? "you" : undefined });
  }

  for (const [id, { s, n }] of N.slots) {
    const before = P.slots.get(id);
    if (!before) continue; // arrived with its node: "node joined" says it
    const p = before.s;
    const base = { slot: id, node: n.host, port: s.port, subject: slotName(n, s) };
    const run = parseTime(s.last_run), prevRun = parseTime(p.last_run);
    const failedMore = s.fails > p.fails;
    const released = mine.slots.get(id) === "release";

    if (!p.busy && s.busy) {
      events.push(Object.assign({ type: "claimed", kind: "claim", t: run != null ? run : now, start: run != null ? run : now }, base));
    } else if (p.busy && !s.busy) {
      const end = run != null ? run : now;
      events.push(Object.assign({
        type: "finished", kind: "finish", t: end, start: prevRun, end, dur: prevRun != null ? end - prevRun : null,
        failed: failedMore, by: released ? "you" : undefined,
      }, base));
    } else if (!p.busy && !s.busy && run != null && run !== prevRun) {
      // Claimed and released between two polls: both stamp LastRunDate, so only the end is known.
      events.push(Object.assign({ type: "finished", kind: "finish", approx: true, t: run, start: null, end: run, dur: null, failed: failedMore }, base));
    } else if (p.busy && s.busy && run != null && run !== prevRun) {
      // Released and claimed again between polls: one run ended (roughly when the next began).
      events.push(Object.assign({ type: "finished", kind: "finish", approx: true, t: run, start: prevRun, end: run, dur: null, failed: failedMore }, base));
      events.push(Object.assign({ type: "claimed", kind: "claim", t: run, start: run }, base));
    }

    if (failedMore) {
      events.push(Object.assign({ type: "failure", kind: "failure", t: now, fails: s.fails, nowFailing: s.state === "failing" && p.state !== "failing" }, base));
    } else if (s.fails === 0 && p.fails > 0) {
      const you = mine.slots.get(id) === "reset" || mine.nodes.get(n.host) === "reset";
      events.push(Object.assign({ type: "reset", kind: you ? "admin" : "reset", t: now, was: p.fails, by: you ? "you" : undefined }, base));
    }

    // Health changes worth a line, unless the node event already explains them.
    if (p.state !== s.state && (PROBLEM[p.state] || PROBLEM[s.state]) && !flipped.has(n.host)) {
      const explained = (failedMore && s.state === "failing") || (s.fails === 0 && p.fails > 0 && p.state === "failing");
      if (!explained && !(p.state === "failing" && s.state === "busy")) {
        events.push(Object.assign({ type: "state", kind: "failure", t: now, from: p.state, to: s.state }, base));
      }
    }
  }
  return events;
}

// Groups of at least `min` finished runs whose ends fall within `gap` ms of the previous one.
export function bursts(events, gap = 2000, min = 3) {
  const ends = events.filter((e) => e.type === "finished").sort((a, b) => a.t - b.t);
  const out = [];
  let group = [];
  const flush = () => {
    if (group.length >= min) {
      const known = group.filter((e) => e.dur != null);
      out.push({
        t: group[group.length - 1].t, first: group[0].t, count: group.length,
        avg: known.length ? known.reduce((a, e) => a + e.dur, 0) / known.length : null,
        failed: group.filter((e) => e.failed).length, approx: group.length - known.length,
        id: "burst-" + group[0].t + "-" + group.length,
      });
    }
    group = [];
  };
  for (const e of ends) {
    if (group.length && e.t - group[group.length - 1].t > gap) flush();
    group.push(e);
  }
  flush();
  return out;
}

// One line of text per event, for the activity log, tooltips and screen readers.
export function describe(e) {
  switch (e.type) {
    case "claimed": return "claimed";
    case "finished":
      if (e.by === "you") return "released by you" + (e.dur != null ? " after " + runLength(e.dur) : "");
      return (e.approx ? "finished between updates (approx.)" : "finished in " + runLength(e.dur)) +
        (e.failed ? " · failures went up" : "");
    case "failure": return "connect failure #" + e.fails + (e.nowFailing ? " · now failing" : "");
    case "reset": return (e.by === "you" ? "you reset failures" : "failures reset") + " (was " + e.was + ")";
    case "state": return stateWord(e.from).toLowerCase() + " → " + stateWord(e.to).toLowerCase();
    case "node-joined": return "joined" + (e.slots != null ? " with " + plural(e.slots, "slot") : "");
    case "node-left": return e.by === "you" ? "you forgot it" : "left the grid";
    case "node-online": return "came online";
    case "node-offline": return "went offline";
    case "admin": return e.text || "admin action";
    default: return e.type;
  }
}

// The ring buffer: the last 15 minutes (at most 500 events) seen by this tab, plus capacity
// samples for the sparkline. Mirrored to tab storage so a reload keeps the history.
export class Recorder {
  constructor(storage, key = "gridRecorder") {
    this.storage = storage;
    this.key = key;
    this.events = [];
    this.samples = [];
    this.dropped = 0;
    this.since = Date.now();
    this.seq = 0;
    this.restore();
  }

  restore() {
    if (!this.storage) return;
    try {
      const saved = JSON.parse(this.storage.get(this.key) || "null");
      if (saved && Array.isArray(saved.events)) {
        this.events = saved.events;
        this.samples = Array.isArray(saved.samples) ? saved.samples : [];
        this.dropped = saved.dropped || 0;
        this.since = saved.since || this.since;
        this.seq = saved.seq || 0;
      }
    } catch (e) { /* unreadable: start a new recording */ }
  }

  save() {
    if (!this.storage) return;
    try {
      this.storage.set(this.key, JSON.stringify({
        events: this.events, samples: this.samples, dropped: this.dropped, since: this.since, seq: this.seq,
      }));
    } catch (e) { /* storage full or blocked: the recording lives in memory only */ }
  }

  // now: server-clock ms; seen: this browser's clock, for afterglows.
  add(events, now, seen = Date.now()) {
    for (const e of events) {
      e.id = e.id || "e" + (++this.seq);
      e.seen = seen;
      this.events.push(e);
    }
    if (events.length) this.events.sort((a, b) => a.t - b.t);
    this.prune(now);
  }

  sample(status, now) {
    const t = status.totals || {};
    this.samples.push({ t: now, busy: t.busy || 0, total: t.slots || 0, avail: t.available || 0 });
    this.prune(now);
  }

  prune(now) {
    const cutoff = now - WINDOW_MS;
    // The window keeps a little room for the oldest bar, which may have started before it.
    this.events = this.events.filter((e) => e.t >= cutoff);
    if (this.events.length > MAX_EVENTS) {
      this.dropped += this.events.length - MAX_EVENTS;
      this.events = this.events.slice(-MAX_EVENTS);
    }
    this.samples = this.samples.filter((s) => s.t >= cutoff);
    this.save();
  }

  clear() {
    this.events = [];
    this.samples = [];
    this.dropped = 0;
    this.since = Date.now();
    this.save();
  }

  within(ms, now) {
    return this.events.filter((e) => e.t >= now - ms);
  }
}

// Polls /api/status: every 1.5 s while visible, every 12 s while hidden, immediately when the tab
// comes back or after an action. Never a held connection: the production server has two sync
// workers and a stream would pin one.
export function createLive({ load, storage, visibleMs = 1500, hiddenMs = 12000 }) {
  const recorder = new Recorder(storage);
  const subs = new Set();
  const mine = { slots: new Map(), nodes: new Map() };
  const s = { data: null, error: null, lastOk: 0, skew: 0, paused: false, failures: 0, nextAt: 0, loading: false };
  let timer = null, inflight = null;

  const serverNow = () => Date.now() + s.skew;

  function emit(events) {
    const msg = { data: s.data, events, recorder, live: api };
    subs.forEach((fn) => { try { fn(msg); } catch (e) { console.error(e); } });
  }

  function expireMine() {
    const old = Date.now() - 30000;
    for (const map of [mine.slots, mine.nodes]) map.forEach((v, k) => { if (v.at < old) map.delete(k); });
  }

  function actions() {
    expireMine();
    const flat = (map) => new Map(Array.from(map, ([k, v]) => [k, v.action]));
    return { slots: flat(mine.slots), nodes: flat(mine.nodes) };
  }

  function schedule() {
    clearTimeout(timer);
    if (s.paused) return;
    const base = document.hidden ? hiddenMs : visibleMs;
    // Back off while the database is unreachable, so the retry countdown is readable.
    const delay = s.error ? Math.min(15000, Math.max(base, 1500 * Math.pow(2, Math.min(s.failures, 4)))) : base;
    s.nextAt = Date.now() + delay;
    timer = setTimeout(poll, delay);
  }

  function poll() {
    if (inflight) return inflight;
    s.loading = true;
    inflight = (async () => {
      let events = [];
      try {
        const next = await load();
        const generated = parseTime(next.generated);
        if (generated != null) s.skew = generated - Date.now();
        events = diff(s.data, next, actions());
        recorder.add(events, serverNow());
        recorder.sample(next, serverNow());
        s.data = next;
        s.error = null;
        s.failures = 0;
        s.lastOk = Date.now();
      } catch (err) {
        s.error = err.message || String(err);
        s.failures += 1;
      } finally {
        s.loading = false;
        inflight = null;
        schedule();
      }
      emit(events);
    })();
    return inflight;
  }

  const api = {
    recorder,
    get data() { return s.data; },
    get error() { return s.error; },
    get lastOk() { return s.lastOk; },
    get paused() { return s.paused; },
    get nextAt() { return s.nextAt; },
    serverNow,
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
    start() {
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden && !s.paused) poll(); else schedule();
      });
      return poll();
    },
    refresh() { return s.paused ? Promise.resolve() : poll(); },
    // A refresh the user asked for: runs even while paused.
    refreshNow() { return poll(); },
    setPaused(paused) {
      s.paused = !!paused;
      if (s.paused) clearTimeout(timer); else poll();
      emit([]);
    },
    // An admin action this tab is about to take: its effects in the next diffs are "you".
    expect(action, target) {
      const map = target.slot != null ? mine.slots : mine.nodes;
      map.set(target.slot != null ? target.slot : target.node, { action, at: Date.now() });
    },
    // ... and once it's done, it gets a line of its own.
    noteAction(action, target, text) {
      const ev = { type: "admin", kind: "admin", by: "you", t: serverNow(), text, action, slot: target.slot, node: target.node, subject: target.subject };
      recorder.add([ev], serverNow());
      emit([ev]);
    },
    clear() { recorder.clear(); emit([]); },
  };
  return api;
}
