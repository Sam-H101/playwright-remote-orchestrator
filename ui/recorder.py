"""What just happened on the grid: the UI server's flight recorder.

/api/status is a snapshot. The checks stamp portUsage.LastRunDate when they claim a slot and again
when they release it, so two snapshots in a row say what changed in between. diff() turns them into
events and Recorder keeps the last 15 minutes of them. The page polls /api/status?events=<cursor>
and gets the events it hasn't seen yet; a tab opened after a burst of 4-second checks gets the whole
buffer. A background thread keeps recording while nobody has the page open.

Event ids are built from database values (slot id and LastRunDate, node name and heartbeat), never
from the moment this process noticed. Two server processes (gunicorn's workers each run a recorder)
that see the same change therefore produce the same id, and the page merges them into one event.
static/js/live.js has the same diff() for a server that doesn't record; keep the two in step.
"""
import datetime
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

WINDOW_MS = 15 * 60 * 1000
MAX_EVENTS = 500
SAMPLE_EVERY_MS = 2000
PROBLEM = {"failing", "unhealthy", "offline"}


def ms(iso):
    """'2026-10-31T10:00:04Z' -> epoch milliseconds (None when empty or unparseable)."""
    if not iso:
        return None
    try:
        value = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return int(round(value.timestamp() * 1000))


def slot_name(node, slot):
    if node.get("kind") == "gateway":
        return "{} / {}".format(node["host"], slot["port"])
    return "{}:{}".format(node["host"], slot["port"])


def _index(status):
    nodes, slots = {}, {}
    for node in (status or {}).get("nodes") or []:
        nodes[node["host"]] = node
        for slot in node.get("slots") or []:
            slots[slot["id"]] = (slot, node)
    return nodes, slots


def _key(value):
    return "" if value is None else str(value)


def diff(prev, nxt):
    """Everything that changed between two snapshots, as events. Pure: same input, same output."""
    events = []
    if not prev or not nxt:
        return events
    now = ms(nxt.get("generated")) or int(time.time() * 1000)
    p_nodes, p_slots = _index(prev)
    n_nodes, n_slots = _index(nxt)

    flipped = set()
    for host, n in n_nodes.items():
        p = p_nodes.get(host)
        if p is None:
            events.append({"id": "nj:{}:{}".format(host, _key(n.get("registered_at"))), "type": "node-joined", "kind": "node",
                           "t": now, "node": host, "subject": host, "slots": n.get("total"), "nodeKind": n.get("kind")})
        elif n.get("kind") == "gateway" and bool(p.get("online")) != bool(n.get("online")):
            flipped.add(host)
            if n.get("online"):
                event_id, kind = "no:{}:{}".format(host, _key(n.get("connected_at"))), "node-online"
            else:
                event_id, kind = "nf:{}:{}".format(host, _key(n.get("last_seen"))), "node-offline"
            events.append({"id": event_id, "type": kind, "kind": "node", "t": now, "node": host, "subject": host,
                           "away": n.get("last_seen_s")})
    for host, p in p_nodes.items():
        if host not in n_nodes:
            events.append({"id": "nl:{}:{}".format(host, _key(p.get("last_seen"))), "type": "node-left", "kind": "node",
                           "t": now, "node": host, "subject": host, "slots": p.get("total")})

    for slot_id, (s, n) in n_slots.items():
        before = p_slots.get(slot_id)
        if before is None:
            continue  # it arrived with its node: "node joined" says so
        p = before[0]
        base = {"slot": slot_id, "node": n["host"], "port": s["port"], "subject": slot_name(n, s)}
        run, prev_run = ms(s.get("last_run")), ms(p.get("last_run"))
        fails, prev_fails = s.get("fails") or 0, p.get("fails") or 0
        failed_more = fails > prev_fails

        def add(event_id, **fields):
            event = dict(base, id=event_id)
            event.update(fields)
            events.append(event)

        if not p.get("busy") and s.get("busy"):
            start = run if run is not None else now
            add("c:{}:{}".format(slot_id, start), type="claimed", kind="claim", t=start, start=start)
        elif p.get("busy") and not s.get("busy"):
            end = run if run is not None else now
            add("f:{}:{}".format(slot_id, end), type="finished", kind="finish", t=end, start=prev_run, end=end,
                dur=end - prev_run if prev_run is not None else None, failed=failed_more)
        elif not p.get("busy") and not s.get("busy") and run is not None and run != prev_run:
            # Claimed and released between two snapshots: both stamp LastRunDate, so only the end is known.
            add("f:{}:{}".format(slot_id, run), type="finished", kind="finish", approx=True, t=run, start=None, end=run,
                dur=None, failed=failed_more)
        elif p.get("busy") and s.get("busy") and run is not None and run != prev_run:
            # Released and claimed again in between: one run ended, roughly when the next began.
            add("f:{}:{}".format(slot_id, run), type="finished", kind="finish", approx=True, t=run, start=prev_run, end=run,
                dur=None, failed=failed_more)
            add("c:{}:{}".format(slot_id, run), type="claimed", kind="claim", t=run, start=run)

        if failed_more:
            add("x:{}:{}:{}".format(slot_id, fails, _key(run)), type="failure", kind="failure", t=now, fails=fails,
                nowFailing=s.get("state") == "failing" and p.get("state") != "failing")
        elif fails == 0 and prev_fails > 0:
            add("r:{}:{}:{}".format(slot_id, prev_fails, _key(run)), type="reset", kind="reset", t=now, was=prev_fails)

        # Health changes worth a line, unless the node event or the failure line already explains them.
        if p.get("state") != s.get("state") and (p.get("state") in PROBLEM or s.get("state") in PROBLEM) and n["host"] not in flipped:
            explained = (failed_more and s.get("state") == "failing") or (fails == 0 and prev_fails > 0 and p.get("state") == "failing")
            if not explained and not (p.get("state") == "failing" and s.get("state") == "busy"):
                add("s:{}:{}>{}:{}".format(slot_id, p.get("state"), s.get("state"), _key(run)), type="state", kind="failure",
                    t=now, **{"from": p.get("state"), "to": s.get("state")})
    return events


class Recorder:
    """The last 15 minutes (at most 500 events) of what the snapshots showed, plus capacity samples.

    snapshot() reads the database; identity() names it, so a different database (a test, a config
    change) starts a new recording instead of reporting every node as having left."""

    def __init__(self, snapshot, identity, interval_s=2.0):
        self._snapshot = snapshot
        self._identity = identity
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._thread = None
        self._reset(None)

    def _reset(self, identity):
        self._ident = identity
        self._prev = None
        self._events = {}
        self._samples = []
        self.since = None
        self.dropped = 0
        self._fed_at = 0.0

    def observe(self, attribute=None):
        """Take a snapshot and record what changed since the last one. Returns (snapshot, ids).

        attribute: (match, by) marks the new events match() accepts as an admin action by `by`
        (the signed-in name, or "" when nobody had to sign in); their ids are returned."""
        with self._lock:
            identity = self._identity()
            if identity != self._ident:
                self._reset(identity)
            data = self._snapshot()
            fresh = self._feed(data)
            ids = []
            if attribute:
                match, by = attribute
                for event in fresh:
                    if match(event):
                        event["by"] = by
                        event["kind"] = "admin"
                        ids.append(event["id"])
            return data, ids

    def _feed(self, data):
        now = ms(data.get("generated")) or int(time.time() * 1000)
        if self.since is None:
            self.since = now
        fresh = []
        if self._prev is not None:
            for event in diff(self._prev, data):
                fresh.append(self._add(event, now))
        self._prev = data
        totals = data.get("totals") or {}
        sample = {"t": now, "busy": totals.get("busy", 0), "total": totals.get("slots", 0), "avail": totals.get("available", 0)}
        last = self._samples[-1] if self._samples else None
        if last is None or now - last["t"] >= SAMPLE_EVERY_MS or any(last[k] != sample[k] for k in ("busy", "total", "avail")):
            self._samples.append(sample)
        self._prune(now)
        self._fed_at = time.monotonic()
        return fresh

    def _add(self, event, now):
        event["observed"] = now
        known = self._events.get(event["id"])
        if known is None:
            self._events[event["id"]] = event
            return event
        # Seen before (another snapshot pair said the same thing): keep the better facts.
        for k, v in event.items():
            if known.get(k) is None and v is not None:
                known[k] = v
        if known.get("approx") and not event.get("approx"):
            known.update(approx=False, start=event.get("start"), dur=event.get("dur"))
        known["observed"] = now
        return known

    def _prune(self, now):
        cutoff = now - WINDOW_MS
        for event_id in [k for k, e in self._events.items() if e["t"] < cutoff]:
            del self._events[event_id]
        if len(self._events) > MAX_EVENTS:
            oldest = sorted(self._events.values(), key=lambda e: e["t"])[:len(self._events) - MAX_EVENTS]
            for event in oldest:
                del self._events[event["id"]]
            self.dropped += len(oldest)
        self._samples = [s for s in self._samples if s["t"] >= cutoff]

    def export(self, cursor):
        """The events and samples recorded at or after cursor (epoch ms), for one page poll."""
        try:
            cursor = int(float(cursor))
        except (TypeError, ValueError):
            cursor = 0
        with self._lock:
            events = sorted((dict(e) for e in self._events.values() if e["observed"] >= cursor), key=lambda e: (e["t"], e["id"]))
            samples = [dict(s) for s in self._samples if s["t"] >= cursor]
            return {
                "events": events,
                "recorder": {"since": self.since, "dropped": self.dropped, "now": self._prev and ms(self._prev.get("generated")),
                             "samples": samples, "window_ms": WINDOW_MS},
            }

    # ---- recording while nobody watches ------------------------------------------------------
    def ensure_running(self):
        """Start the background recorder, once per process (after a fork: it's started by a request)."""
        if self._thread is not None or self.interval_s <= 0:
            return
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="grid-recorder", daemon=True)
                self._thread.start()

    def tick(self):
        """One background step: snapshot unless a page poll just did."""
        if time.monotonic() - self._fed_at < self.interval_s:
            return False
        try:
            self.observe()
        except Exception as e:  # the database is down, or a test removed it: the next tick tries again
            log.debug("recorder: snapshot failed: %s", e)
            return False
        return True

    def _run(self):
        # Wake when the last snapshot (a page poll's or ours) is interval_s old, so snapshots are
        # never further apart than that; after a failure, wait a full interval before trying again.
        while True:
            wait = self.interval_s - (time.monotonic() - self._fed_at)
            if wait > 0.01:
                time.sleep(wait)
            elif not self.tick():
                time.sleep(self.interval_s)


def interval_from_env():
    try:
        return float(os.environ.get("GRID_RECORDER_INTERVAL", "2"))
    except ValueError:
        return 2.0
