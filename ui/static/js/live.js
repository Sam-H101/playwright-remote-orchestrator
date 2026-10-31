// What just happened. The Grid UI server records it (ui/recorder.py): every /api/status poll comes
// back with the events since the page's cursor, and a tab opened after a burst of checks gets the
// last 15 minutes in its first poll. The Recorder below is this page's copy of that buffer, merged
// by event id. Against a server that doesn't record, the same diff() runs here instead, and the
// page labels the history as its own. Views only see { data, events, recorder } through subscribe().
import { slotName, parseTime, runLength, plural, stateWord } from "./format.js";

export const WINDOW_MS = 15 * 60 * 1000;
export const MAX_EVENTS = 500;
const PROBLEM = { failing: 1, unhealthy: 1, offline: 1 };
const key = (v) => (v == null ? "" : String(v));

function index(status) {
  const nodes = new Map(), slots = new Map();
  for (const n of (status && status.nodes) || []) {
    nodes.set(n.host, n);
    for (const s of n.slots || []) slots.set(s.id, { s, n });
  }
  return { nodes, slots };
}

// Everything that changed between two snapshots, as events. Pure: same input, same output. Ids
// match the server's (database values, not the moment of noticing). mine: { slots: Map(slotId ->
// action), nodes: Map(host -> action) }: admin actions this tab just took, attributed to "you".
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
      events.push({ id: "nj:" + host + ":" + key(n.registered_at), type: "node-joined", kind: "node", t: now, node: host, subject: host, slots: n.total, nodeKind: n.kind });
    } else if (n.kind === "gateway" && !!p.online !== !!n.online) {
      flipped.add(host);
      events.push({
        id: n.online ? "no:" + host + ":" + key(n.connected_at) : "nf:" + host + ":" + key(n.last_seen),
        type: n.online ? "node-online" : "node-offline", kind: "node", t: now, node: host, subject: host, away: n.last_seen_s,
      });
    }
  }
  for (const [host, p] of P.nodes) {
    if (N.nodes.has(host)) continue;
    const you = mine.nodes.get(host) === "forget";
    events.push({ id: "nl:" + host + ":" + key(p.last_seen), type: "node-left", kind: you ? "admin" : "node", t: now, node: host, subject: host, slots: p.total, by: you ? "you" : undefined, you: you || undefined });
  }

  for (const [id, { s, n }] of N.slots) {
    const before = P.slots.get(id);
    if (!before) continue; // arrived with its node: "node joined" says it
    const p = before.s;
    const base = { slot: id, node: n.host, port: s.port, subject: slotName(n, s) };
    const run = parseTime(s.last_run), prevRun = parseTime(p.last_run);
    const fails = s.fails || 0, prevFails = p.fails || 0;
    const failedMore = fails > prevFails;
    const released = mine.slots.get(id) === "release";

    if (!p.busy && s.busy) {
      const start = run != null ? run : now;
      events.push(Object.assign({ id: "c:" + id + ":" + start, type: "claimed", kind: "claim", t: start, start }, base));
    } else if (p.busy && !s.busy) {
      const end = run != null ? run : now;
      events.push(Object.assign({
        id: "f:" + id + ":" + end, type: "finished", kind: released ? "admin" : "finish", t: end, start: prevRun, end,
        dur: prevRun != null ? end - prevRun : null, failed: failedMore, by: released ? "you" : undefined, you: released || undefined,
      }, base));
    } else if (!p.busy && !s.busy && run != null && run !== prevRun) {
      // Claimed and released between two polls: both stamp LastRunDate, so only the end is known.
      events.push(Object.assign({ id: "f:" + id + ":" + run, type: "finished", kind: "finish", approx: true, t: run, start: null, end: run, dur: null, failed: failedMore }, base));
    } else if (p.busy && s.busy && run != null && run !== prevRun) {
      // Released and claimed again between polls: one run ended (roughly when the next began).
      events.push(Object.assign({ id: "f:" + id + ":" + run, type: "finished", kind: "finish", approx: true, t: run, start: prevRun, end: run, dur: null, failed: failedMore }, base));
      events.push(Object.assign({ id: "c:" + id + ":" + run, type: "claimed", kind: "claim", t: run, start: run }, base));
    }

    if (failedMore) {
      events.push(Object.assign({ id: "x:" + id + ":" + fails + ":" + key(run), type: "failure", kind: "failure", t: now, fails, nowFailing: s.state === "failing" && p.state !== "failing" }, base));
    } else if (fails === 0 && prevFails > 0) {
      const you = mine.slots.get(id) === "reset" || mine.nodes.get(n.host) === "reset";
      events.push(Object.assign({ id: "r:" + id + ":" + prevFails + ":" + key(run), type: "reset", kind: you ? "admin" : "reset", t: now, was: prevFails, by: you ? "you" : undefined, you: you || undefined }, base));
    }

    // Health changes worth a line, unless the node event already explains them.
    if (p.state !== s.state && (PROBLEM[p.state] || PROBLEM[s.state]) && !flipped.has(n.host)) {
      const explained = (failedMore && s.state === "failing") || (fails === 0 && prevFails > 0 && p.state === "failing");
      if (!explained && !(p.state === "failing" && s.state === "busy")) {
        events.push(Object.assign({ id: "s:" + id + ":" + p.state + ">" + s.state + ":" + key(run), type: "state", kind: "failure", t: now, from: p.state, to: s.state }, base));
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

// Who did an admin action: "you" when this tab did it, else the signed-in name the server recorded,
// or nobody in particular when the grid has no admin sign-in.
export function actor(e) {
  if (e.you || e.by === "you") return "you";
  return e.by || "";
}

// One line of text per event, for the activity log, tooltips and screen readers.
export function describe(e) {
  const who = e.kind === "admin" ? actor(e) : "";
  switch (e.type) {
    case "claimed": return "claimed";
    case "finished":
      if (e.kind === "admin") {
        return (who ? "released by " + who : "released from the Grid UI") + (e.dur != null ? " after " + runLength(e.dur) : "");
      }
      return (e.approx ? "finished between updates (approx.)" : "finished in " + runLength(e.dur)) +
        (e.failed ? " · failures went up" : "");
    case "failure": return "connect failure #" + e.fails + (e.nowFailing ? " · now failing" : "");
    case "reset":
      if (e.kind === "admin") return (who ? who + " reset failures" : "failures reset from the Grid UI") + " (was " + e.was + ")";
      return "failures reset (was " + e.was + ")";
    case "state": return stateWord(e.from).toLowerCase() + " → " + stateWord(e.to).toLowerCase();
    case "node-joined": return "joined" + (e.slots != null ? " with " + plural(e.slots, "slot") : "");
    case "node-left":
      if (e.kind === "admin") return who === "you" ? "you forgot it" : who ? "forgotten by " + who : "forgotten from the Grid UI";
      return "left the grid";
    case "node-online": return "came online";
    case "node-offline": return "went offline";
    default: return e.type;
  }
}

// This page's copy of the recording: the last 15 minutes (at most 500 events) plus capacity
// samples, cached in this browser so a reload draws at once. Events merge by id, so the same run
// reported by two server processes, or by the server and this page, is one event.
export class Recorder {
  constructor(storage, key = "gridRecorder") {
    this.storage = storage;
    this.key = key;
    this.events = [];
    this.samples = [];
    this.dropped = 0;
    this.since = Date.now();
    this.source = "page"; // "server" once the server's recorder answers
    this.floor = 0; // "Clear" hides everything before this (server clock)
    this.seq = 0;
    this.yours = new Set();
    this.byId = new Map();
    this.saveTimer = null;
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
        this.source = saved.source || this.source;
        this.floor = saved.floor || 0;
        this.seq = saved.seq || 0;
        this.yours = new Set(Array.isArray(saved.yours) ? saved.yours : []);
      }
    } catch (e) { /* unreadable: start a new recording */ }
    this.events.forEach((e) => this.byId.set(e.id, e));
  }

  // Writing 500 events on every 1.5 s poll would be wasteful: once every few seconds is plenty.
  save() {
    if (!this.storage || this.saveTimer) return;
    this.saveTimer = setTimeout(() => this.flush(), 3000);
  }

  flush() {
    clearTimeout(this.saveTimer);
    this.saveTimer = null;
    if (!this.storage) return;
    try {
      this.storage.set(this.key, JSON.stringify({
        events: this.events, samples: this.samples, dropped: this.dropped, since: this.since, source: this.source,
        floor: this.floor, seq: this.seq, yours: Array.from(this.yours).slice(-100),
      }));
    } catch (e) { /* storage full or blocked: the recording lives in memory only */ }
  }

  // Merge events in; returns the ones that are new. now: server-clock ms. seen(e): this browser's
  // clock when the change happened, for afterglows (a backfill passes the event's own time).
  merge(events, now, seen = () => Date.now()) {
    const fresh = [];
    for (const e of events) {
      if (e.t < this.floor) continue;
      e.id = e.id || "e" + (++this.seq);
      if (this.yours.has(e.id)) e.you = true;
      const known = this.byId.get(e.id);
      if (!known) {
        e.seen = seen(e);
        this.events.push(e);
        this.byId.set(e.id, e);
        fresh.push(e);
        continue;
      }
      // The same change, told again: keep the better facts.
      for (const k of Object.keys(e)) if (known[k] == null && e[k] != null) known[k] = e[k];
      if (known.approx && e.approx === false) Object.assign(known, { approx: false, start: e.start, dur: e.dur });
      if (e.kind === "admin" && known.kind !== "admin") Object.assign(known, { kind: "admin", by: e.by });
    }
    if (fresh.length) this.events.sort((a, b) => a.t - b.t);
    this.prune(now);
    return fresh;
  }

  add(events, now, seen = Date.now()) {
    return this.merge(events, now, () => seen);
  }

  // Ids of events this tab caused (from the action's response): shown as "you".
  mark(ids) {
    for (const id of ids || []) {
      this.yours.add(id);
      const e = this.byId.get(id);
      if (e) e.you = true;
    }
    this.save();
  }

  sample(status, now) {
    const t = status.totals || {};
    this.samples.push({ t: now, busy: t.busy || 0, total: t.slots || 0, avail: t.available || 0 });
    this.prune(now);
  }

  // The server's recorder: where its recording starts, what it dropped, its capacity samples.
  adopt(meta, now) {
    if (!meta) return;
    this.source = "server";
    if (meta.since != null) this.since = meta.since;
    if (meta.dropped != null) this.dropped = meta.dropped;
    const known = new Set(this.samples.map((s) => s.t));
    let added = false;
    for (const s of meta.samples || []) if (!known.has(s.t)) { this.samples.push(s); added = true; }
    if (added) this.samples.sort((a, b) => a.t - b.t);
    this.prune(now);
  }

  prune(now) {
    const cutoff = now - WINDOW_MS;
    if (this.events.length && this.events[0].t < cutoff) this.events = this.events.filter((e) => e.t >= cutoff);
    if (this.events.length > MAX_EVENTS) {
      if (this.source !== "server") this.dropped += this.events.length - MAX_EVENTS;
      this.events = this.events.slice(-MAX_EVENTS);
    }
    if (this.byId.size !== this.events.length) this.byId = new Map(this.events.map((e) => [e.id, e]));
    if (this.samples.length && this.samples[0].t < cutoff) this.samples = this.samples.filter((s) => s.t >= cutoff);
    this.save();
  }

  clear(now = Date.now()) {
    this.floor = now;
    this.events = [];
    this.byId.clear();
    this.samples = [];
    this.dropped = 0;
    this.since = Date.now();
    this.flush();
  }

  within(ms, now) {
    return this.events.filter((e) => e.t >= now - ms);
  }
}

// Polls /api/status every 1.5 s while visible (or the server's poll_ms), every 12 s while hidden,
// immediately when the tab comes back or after an action. Never a held connection: the production
// server has two sync workers and a stream would pin one.
export function createLive({ load, storage, visibleMs = 1500, hiddenMs = 12000 }) {
  const recorder = new Recorder(storage);
  const subs = new Set();
  const mine = { slots: new Map(), nodes: new Map() };
  const s = { data: null, error: null, errorKind: null, lastOk: 0, skew: 0, paused: false, failures: 0, nextAt: 0, loading: false, cursor: 0 };
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
    // Back off while the grid is unreachable, so the retry countdown is readable.
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
        const next = await load(s.cursor);
        const generated = parseTime(next.generated);
        if (generated != null) s.skew = generated - Date.now();
        if (next.poll_ms) visibleMs = Math.max(1000, Number(next.poll_ms) || visibleMs);
        if (Array.isArray(next.events)) {
          // The server recorded it. The first answer is the backfill: old news, so no afterglow
          // beyond what its own timestamps allow, and no announcement.
          const backfill = s.cursor === 0;
          const fresh = recorder.merge(next.events, serverNow(), backfill ? (e) => e.t - s.skew : () => Date.now());
          recorder.adopt(next.recorder, serverNow());
          const at = (next.recorder && next.recorder.now) || generated;
          // A little overlap: another server process may notice a change a moment later.
          if (at) s.cursor = Math.max(1, at - 10000);
          events = backfill ? [] : fresh;
        } else {
          events = recorder.add(diff(s.data, next, actions()), serverNow());
          recorder.sample(next, serverNow());
        }
        s.data = next;
        s.error = s.errorKind = null;
        s.failures = 0;
        s.lastOk = Date.now();
      } catch (err) {
        s.error = err.message || String(err);
        s.errorKind = err.kind || "network";
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
    // "network" (no answer from the Grid UI server), "server" (a bad answer) or "database".
    get errorKind() { return s.errorKind; },
    get lastOk() { return s.lastOk; },
    get paused() { return s.paused; },
    get nextAt() { return s.nextAt; },
    serverNow,
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
    start() {
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden && !s.paused) poll(); else schedule();
      });
      window.addEventListener("pagehide", () => recorder.flush());
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
    // An admin action this tab is about to take: without a server recorder, its effects in the
    // next diffs are "you".
    expect(action, target) {
      const map = target.slot != null ? mine.slots : mine.nodes;
      map.set(target.slot != null ? target.slot : target.node, { action, at: Date.now() });
    },
    // ... and with one, the server names the events the action caused.
    mine(ids) { recorder.mark(ids); emit([]); },
    clear() { recorder.clear(serverNow()); emit([]); },
  };
  return api;
}
