// "Recent runs": one swimlane per node, one track per slot, a bar for every run the recorder saw,
// over a window of 1, 5 or 15 minutes. Below it, a full-width strip of the last 15 minutes: runs
// finished per 30 s and busy slots, red where none was free. Hand-built DOM: positions are CSS
// variables set with style.setProperty, never style attributes in markup (the CSP forbids them).
import { app, prefs } from "../app.js";
import { html, setHTML, setText, patchList } from "../html.js";
import { clock, runLength, runText, parseTime, plural, slotName } from "../format.js";
import { bursts, actor } from "../live.js";
import { showTip, hideTip, tipLines } from "./toast.js";

export const SPAN = 5 * 60 * 1000;
const LONG = 15 * 60 * 1000;
const MIN_SPAN = 60 * 1000;
const ZOOMS = [["1m", "1 min", 60 * 1000], ["5m", "5 min", SPAN], ["15m", "15 min", LONG]];
const ZOOM_KEY = "gridRunsZoom";
const AGGREGATE_OVER = 8;
const BUCKETS = 60;
const STRIP_BUCKETS = 30;
const SVG = "http://www.w3.org/2000/svg";

const pct = (v) => (Math.round(v * 10000) / 100).toFixed(2);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function svg(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  Object.keys(attrs || {}).forEach((k) => el.setAttribute(k, attrs[k]));
  return el;
}

// Runs to draw, from the recorder (finished) and the snapshot (still running).
export function runsFrom(data, events, now, span = SPAN) {
  const from = now - span;
  const at = new Map();
  for (const n of (data && data.nodes) || []) n.slots.forEach((s, i) => at.set(s.id, { n, s, row: i }));
  const stale = new Set(((data && data.sessions) || []).filter((s) => s.stale).map((s) => s.slot_id));
  const runs = new Map();
  for (const e of events) {
    if (e.type !== "finished" || e.end == null || e.end < from) continue;
    const a = at.get(e.slot);
    if (!a) continue;
    const id = e.start != null ? "run-" + e.slot + "-" + e.start : e.id;
    runs.set(id, {
      id, slot: e.slot, host: a.n.host, row: a.row, subject: e.subject || slotName(a.n, a.s), start: e.start, end: e.end,
      approx: !!e.approx, failed: !!e.failed, admin: e.kind === "admin", who: e.kind === "admin" ? actor(e) : "", running: false,
    });
  }
  for (const [slotId, a] of at) {
    if (!a.s.busy) continue;
    const start = parseTime(a.s.last_run);
    const id = "run-" + slotId + "-" + start;
    if (!runs.has(id)) runs.set(id, { id, slot: slotId, host: a.n.host, row: a.row, subject: slotName(a.n, a.s), start, end: null, running: true, stale: stale.has(slotId) });
  }
  return Array.from(runs.values());
}

// The numbers above the chart, for one window. Stale claims are held slots, not runs: they're
// counted apart, so "running" means checks that are plausibly running now.
export function summarize(runs, events, samples, data, now, span) {
  const from = now - span;
  const finished = events.filter((e) => e.type === "finished" && e.end != null && e.end >= from);
  const running = runs.filter((r) => r.running && !r.stale).length;
  const stale = runs.filter((r) => r.running && r.stale).length;
  const recent = samples.filter((s) => s.t >= from);
  const total = (data && data.totals && data.totals.slots) || 0;
  const peak = recent.reduce((m, s) => Math.max(m, s.busy), data && data.totals ? data.totals.busy : 0);
  return { running, stale, finished: finished.length, failed: finished.filter((e) => e.failed).length, peak, total };
}

export function createTimeline(root, { onOpen }) {
  let zoom = prefs.get(ZOOM_KEY, "5m");
  if (!ZOOMS.some((z) => z[0] === zoom)) zoom = "5m";
  setHTML(root, html`
    <div class="tl-head">
      <h2 class="section-label" id="runs-title">Recent runs</h2>
      <p class="tl-summary" id="runs-summary" aria-live="off"></p>
      <div class="segmented tl-zoom" role="group" aria-label="Time window" id="tl-zoom">
        ${ZOOMS.map(([id, label]) => html`<button type="button" data-zoom="${id}" aria-pressed="${id === zoom}">${label}</button>`)}
      </div>
    </div>
    <div class="tl-body">
      <div class="tl-ruler" aria-hidden="true">
        <div class="tl-gutter tl-since" id="tl-since"></div>
        <div class="tl-scale"><div class="tl-ticks" id="tl-ticks"></div><span class="tl-now">now</span></div>
      </div>
      <div class="tl-flagrow"><div class="tl-gutter"></div><div class="tl-flags" id="tl-bursts"><span class="tl-quiet" id="tl-quiet" hidden></span></div></div>
      <div class="tl-lanes" id="tl-lanes" role="group" aria-label="Runs per node. Arrow keys move between runs, Enter opens the slot."></div>
      <p class="tl-empty" id="tl-empty" hidden>No nodes to show yet.</p>
      <div class="tl-strip">
        <div class="tl-gutter tl-strip-label">
          <span class="tl-counter" id="runs-counter"></span>
          <span class="tl-strip-sub">last 15 min</span>
        </div>
        <div class="tl-strip-plot" id="tl-spark"></div>
      </div>
      <ul class="tl-key" aria-hidden="true">
        <li><i class="k-bar done"></i>Finished</li><li><i class="k-bar running"></i>Running</li><li><i class="k-bar failed"></i>Failures went up</li>
        <li><i class="k-diamond"></i>Between updates (approx.)</li><li><i class="k-hist"></i>Runs per 30 s</li><li><i class="k-line"></i>Busy slots</li><li><i class="k-zero"></i>No free slot</li>
      </ul>
    </div>
    <div class="tl-phone">
      <div class="tl-phone-spark"><span class="tl-cap-label"><span id="runs-counter-phone"></span> · last 15 min</span><div id="tl-runbars" class="tl-runbars"></div></div>
      <ol class="tl-recent" id="tl-recent" aria-label="Last 5 runs"></ol>
    </div>`);

  const lanesEl = root.querySelector("#tl-lanes"), ticksEl = root.querySelector("#tl-ticks");
  const burstsEl = root.querySelector("#tl-bursts"), sparkEl = root.querySelector("#tl-spark");
  const lanes = new Map();
  const runById = new Map();
  let runs = [], focusId = null, data = null, events = [], samples = [], lastNow = Date.now(), since = null, source = "page", rec = null;

  // The 15-minute strip: runs finished per bucket as bars, busy slots as a line, capacity dashed,
  // red bands where nothing was free.
  const W = 1000, H = 40;
  const spark = svg("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", class: "spark", role: "img" });
  const bands = svg("g", { class: "spark-zero" });
  const hist = svg("g", { class: "spark-hist" });
  const cap = svg("path", { class: "spark-total" });
  const busyLine = svg("path", { class: "spark-line" });
  spark.append(bands, hist, cap, busyLine);
  sparkEl.appendChild(spark);

  const runbars = svg("svg", { viewBox: "0 0 300 32", preserveAspectRatio: "none", class: "spark", role: "img" });
  root.querySelector("#tl-runbars").appendChild(runbars);

  root.querySelector("#tl-zoom").addEventListener("click", (e) => {
    const b = e.target.closest("[data-zoom]");
    if (!b || b.dataset.zoom === zoom) return;
    zoom = b.dataset.zoom;
    prefs.set(ZOOM_KEY, zoom);
    root.querySelectorAll("#tl-zoom [data-zoom]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.zoom === zoom)));
    lanes.forEach((l) => { l.el.remove(); });
    lanes.clear();
    if (data && rec) update(data, rec, app.now(), true);
  });

  const spanOf = () => ZOOMS.find((z) => z[0] === zoom)[2];

  // The axis: the chosen window, but never back past the start of the recording (there is nothing
  // to show there), and never shorter than a minute.
  function axis(now) {
    const span = spanOf();
    const from = Math.min(now - MIN_SPAN, Math.max(now - span, since == null ? now - span : since));
    return { from, span: now - from, fitted: from > now - span + 1000 };
  }

  function lane(node) {
    let l = lanes.get(node.host);
    const agg = node.slots.length > AGGREGATE_OVER;
    if (l && l.agg === agg && l.rows === node.slots.length) return l;
    if (l) l.el.remove();
    const el = document.createElement("div");
    el.className = "tl-lane" + (agg ? " agg" : "");
    el.dataset.key = node.host;
    setHTML(el, html`<div class="tl-label mono" title="${node.host}">${node.host}</div><div class="tl-area" role="presentation"></div>`);
    const areaEl = el.querySelector(".tl-area");
    el.style.setProperty("--rows", agg ? 1 : Math.max(1, node.slots.length));
    const buckets = [];
    if (agg) {
      for (let i = 0; i < BUCKETS; i++) {
        const b = document.createElement("i");
        b.className = "tl-bucket";
        areaEl.appendChild(b);
        buckets.push(b);
      }
    }
    l = { el, area: areaEl, agg, rows: node.slots.length, bars: new Map(), buckets, node };
    lanes.set(node.host, l);
    return l;
  }

  function result(r) {
    if (r.running) return r.stale ? "held, stale" : "running";
    if (r.admin) return r.who ? "released by " + r.who : "released from the Grid UI";
    return r.failed ? "failures went up" : r.approx ? "finished, approx." : "finished";
  }

  function barLabel(r) {
    const dur = r.running ? "" : r.start != null && r.end != null && !r.approx ? ", " + runLength(r.end - r.start) : "";
    return r.subject + ": " + result(r) + dur + (r.start != null ? ", started " + clock(r.start) : "") + (r.end != null ? ", ended " + clock(r.end) : "");
  }

  function makeBar(r) {
    const b = document.createElement("div");
    b.className = "bar";
    b.tabIndex = -1;
    b.setAttribute("role", "button");
    b.dataset.run = r.id;
    b.dataset.slot = r.slot;
    return b;
  }

  function styleBar(b, r) {
    const cls = "bar" + (r.running ? " running" : " done") + (r.stale ? " stale" : "") + (r.failed ? " failed" : "") +
      (r.approx && r.start == null ? " approx" : "") + (r.approx && r.start != null ? " approx-end" : "") + (r.admin ? " admin" : "");
    if (b.className !== cls) b.className = cls;
    const label = barLabel(r);
    if (b.getAttribute("aria-label") !== label) b.setAttribute("aria-label", label);
    b.style.setProperty("--row", r.row);
  }

  // Data changed: lanes, bars, numbers, list.
  function update(nextData, recorder, now, rebuilt) {
    data = nextData;
    rec = recorder;
    events = recorder.events;
    samples = recorder.samples;
    source = recorder.source;
    // The recording's start on the server's clock (this page's own recording uses this browser's).
    since = recorder.source === "server" ? recorder.since : recorder.since + (now - Date.now());
    const nodes = ((data && data.nodes) || []).slice().sort((a, b) =>
      (a.kind === b.kind ? 0 : a.kind === "gateway" ? -1 : 1) || a.host.localeCompare(b.host));
    root.querySelector("#tl-empty").hidden = nodes.length > 0;
    const keep = new Set(nodes.map((n) => n.host));
    lanes.forEach((l, host) => { if (!keep.has(host)) { l.el.remove(); lanes.delete(host); } });
    nodes.forEach((n, i) => {
      const l = lane(n);
      l.node = n;
      if (lanesEl.children[i] !== l.el) lanesEl.insertBefore(l.el, lanesEl.children[i] || null);
    });

    runs = runsFrom(data, events, now, axis(now).span);
    runById.clear();
    runs.forEach((r) => runById.set(r.id, r));
    const seen = new Set();
    for (const r of runs) {
      const l = lanes.get(r.host);
      if (!l) continue;
      seen.add(r.id);
      if (l.agg) continue;
      let b = l.bars.get(r.id);
      if (!b) { b = makeBar(r); l.bars.set(r.id, b); l.area.appendChild(b); }
      styleBar(b, r);
    }
    lanes.forEach((l) => l.bars.forEach((b, id) => {
      if (seen.has(id)) return;
      if (document.activeElement === b) focusId = null;
      b.remove();
      l.bars.delete(id);
    }));
    roving();
    renderSummary(now);
    renderRecent();
    tick(now, rebuilt);
  }

  function renderSummary(now) {
    const ax = axis(now), label = ZOOMS.find((z) => z[0] === zoom)[1];
    const s = summarize(runs, events, samples, data, now, ax.span);
    const n = (v) => html`<span class="num">${v}</span>`;
    setHTML(root.querySelector("#runs-summary"), html`<span class="tl-window" title="${ax.fitted ? (source === "server" ? "The Grid UI server" : "This page") + " started recording at " + clock(ax.from) : ""}">${ax.fitted ? "Since " + clock(ax.from, false) : "Last " + label}</span>
      ${n(s.running)} running · ${n(s.finished)} finished · ${s.failed ? html`<span class="bad-text">${n(s.failed)} failed</span>` : html`${n(0)} failed`}
      · peak ${n(s.peak)}/${s.total} busy${s.stale ? html` <span class="tl-stale" title="Claims older than the stale threshold aren't counted as running">· ${plural(s.stale, "stale claim")} not counted</span>` : ""}`);
    const fifteen = events.filter((e) => e.type === "finished" && e.t >= now - LONG).length;
    const words = html`<span class="num">${fifteen}</span> ${fifteen === 1 ? "run" : "runs"} finished`;
    setHTML(root.querySelector("#runs-counter"), html`${words}<span class="sr-only"> · last 15 min</span>`);
    setHTML(root.querySelector("#runs-counter-phone"), words);
  }

  function renderRecent() {
    const last = events.filter((e) => e.type === "finished").slice(-5).reverse();
    const list = root.querySelector("#tl-recent");
    if (!last.length) {
      setHTML(list, html`<li class="tl-recent-empty">${source === "server" ? "No runs finished in the last 15 minutes." : "No runs seen yet. Runs appear here as checks finish."}</li>`);
      return;
    }
    list.__html = null;
    patchList(list, last, (e) => e.id, (e) => html`
      <li data-key="${e.id}"><a href="#/slot/${e.slot}" class="tl-recent-row">
        <span class="dot ${e.failed ? "bad" : "accent"}" aria-hidden="true"></span>
        <span class="mono subject">${e.subject}</span>
        <span class="mono meta">${runText(e)}</span>
        <span class="mono meta">${clock(e.end)}</span></a></li>`);
  }

  // Every second: slide everything left.
  function tick(now, instant) {
    const ax = axis(now), from = ax.from, span = ax.span;
    const x = (t) => clamp((t - from) / span, 0, 1);
    root.classList.toggle("tl-instant", !!instant);
    // The axis starts where the recording does: say so at that edge.
    setText(root.querySelector("#tl-since"), ax.fitted ? (source === "server" ? "recording started" : "page opened") : "");
    lanes.forEach((l) => {
      l.el.classList.toggle("off", l.node.kind === "gateway" && !l.node.online);
      if (l.agg) {
        const total = Math.max(1, l.node.slots.length), size = span / BUCKETS;
        const mine = runs.filter((r) => r.host === l.node.host);
        l.buckets.forEach((b, i) => {
          const t0 = from + i * size, t1 = t0 + size;
          const busy = mine.filter((r) => (r.start == null ? r.end - 1000 : r.start) < t1 && (r.end == null ? now : r.end) > t0).length;
          b.style.setProperty("--l", pct(i / BUCKETS));
          b.style.setProperty("--w", pct(1 / BUCKETS));
          b.style.setProperty("--o", Math.min(1, busy / total).toFixed(3));
        });
        return;
      }
      l.bars.forEach((b, id) => {
        const r = runById.get(id);
        if (!r) return;
        const end = r.end == null ? now : Math.min(r.end, now);
        if (r.approx && r.start == null) {
          b.style.setProperty("--l", pct(x(end)));
          b.style.setProperty("--w", "0");
        } else {
          const start = r.start == null ? end : r.start;
          b.style.setProperty("--l", pct(x(start)));
          b.style.setProperty("--w", pct(x(end) - x(start)));
        }
        b.classList.toggle("clipped", r.start != null && r.start < from);
      });
    });

    // Ticks slide with the window; "now" stays pinned to the right edge.
    const step = span <= 90e3 ? 15e3 : span <= 6 * 60e3 ? 60e3 : 180e3;
    const marks = [];
    for (let m = Math.ceil(from / step) * step; m <= now - step / 4; m += step) marks.push(m);
    patchList(ticksEl, marks, (m) => m, (m) => html`<span class="tl-tick" data-key="${m}">${clock(m, step < 60e3)}</span>`);
    Array.from(ticksEl.children).forEach((el) => el.style.setProperty("--l", pct(x(Number(el.dataset.key)))));

    // Burst flags sit in their own row over the runs they summarise: right edge at the last end,
    // pushed right of the gutter when there isn't room. Where two would overlap, the newer wins.
    const scale = burstsEl.clientWidth || 1000;
    const flags = [];
    let leftEdge = Infinity;
    bursts(events.filter((e) => e.end != null && e.end >= from)).filter((b) => b.t >= from).reverse().forEach((b) => {
      const width = 16 + 7 * (String(b.count).length + (b.avg != null ? 15 : 14));
      const right = Math.max(width, x(b.t) * scale);
      if (right <= leftEdge - 6) { flags.push(Object.assign({ right }, b)); leftEdge = right - width; }
    });
    flags.reverse();
    const quiet = root.querySelector("#tl-quiet");
    const any = runs.some((r) => !r.running || !r.stale);
    quiet.hidden = any || flags.length > 0;
    if (!quiet.hidden) setText(quiet, "No runs " + (ax.fitted ? "since " + clock(from, false) : "in the last " + ZOOMS.find((z) => z[0] === zoom)[1]) + ".");
    const flagEls = Array.from(burstsEl.querySelectorAll(".tl-flag"));
    const wanted = new Map(flags.map((f) => [f.id, f]));
    flagEls.forEach((el) => { if (!wanted.has(el.dataset.key)) el.remove(); });
    flags.forEach((f) => {
      let el = burstsEl.querySelector('.tl-flag[data-key="' + CSS.escape(f.id) + '"]');
      const text = f.count + " runs" + (f.avg != null ? " · " + runLength(f.avg) + " avg" : " · approx.");
      if (!el) {
        el = document.createElement("span");
        el.className = "tl-flag";
        el.dataset.key = f.id;
        burstsEl.appendChild(el);
      }
      el.classList.toggle("bad", !!f.failed);
      if (el.textContent !== text) el.textContent = text;
      el.style.setProperty("--x", Math.round(f.right) + "px");
    });

    drawStrip(now);
    drawRunBars(now);
  }

  function drawStrip(now) {
    const from = now - LONG;
    const pts = samples.filter((s) => s.t >= from);
    const X = (t) => clamp(((t - from) / LONG) * W, 0, W);
    const size = LONG / STRIP_BUCKETS;
    const counts = new Array(STRIP_BUCKETS).fill(0);
    events.forEach((e) => {
      if (e.type !== "finished" || e.t < from) return;
      counts[clamp(Math.floor((e.t - from) / size), 0, STRIP_BUCKETS - 1)]++;
    });
    const most = Math.max(1, ...counts);
    hist.replaceChildren(...counts.map((c, i) => c ? svg("rect", {
      x: (X(from + i * size) + 1).toFixed(1), width: (W / STRIP_BUCKETS - 2).toFixed(1),
      y: (H - Math.max(3, (c / most) * (H - 4))).toFixed(1), height: Math.max(3, (c / most) * (H - 4)).toFixed(1),
    }) : null).filter(Boolean));
    const max = Math.max(1, ...pts.map((s) => s.total));
    const Y = (v) => H - 1 - (v / max) * (H - 4);
    const finished = counts.reduce((a, b) => a + b, 0);
    if (!pts.length) {
      busyLine.setAttribute("d", "");
      cap.setAttribute("d", "");
      bands.replaceChildren();
      spark.setAttribute("aria-label", "Last 15 minutes: " + plural(finished, "run") + " finished; no capacity samples yet");
      return;
    }
    let line = "", top = "";
    pts.forEach((s, i) => {
      line += (i ? "L" : "M") + X(s.t).toFixed(1) + " " + Y(s.busy).toFixed(1);
      top += (i ? "L" : "M") + X(s.t).toFixed(1) + " " + Y(s.total).toFixed(1);
    });
    const lastX = X(now).toFixed(1), last = pts[pts.length - 1];
    line += "L" + lastX + " " + Y(last.busy).toFixed(1);
    top += "L" + lastX + " " + Y(last.total).toFixed(1);
    busyLine.setAttribute("d", line);
    cap.setAttribute("d", top);
    const zero = [];
    pts.forEach((s, i) => {
      if (s.total && s.avail === 0) zero.push([X(s.t), i + 1 < pts.length ? X(pts[i + 1].t) : X(now)]);
    });
    bands.replaceChildren(...zero.map(([a, b]) => svg("rect", { x: a.toFixed(1), y: 0, width: Math.max(2, b - a).toFixed(1), height: H })));
    const peak = pts.reduce((m, s) => Math.max(m, s.busy), 0);
    spark.setAttribute("aria-label", "Last 15 minutes: " + plural(finished, "run") + " finished; now " + last.busy + " of " + last.total +
      " slots busy, peak " + peak + (zero.length ? "; there were times when no slot was free" : ""));
  }

  function drawRunBars(now) {
    const from = now - LONG, n = 30, size = LONG / n;
    const counts = new Array(n).fill(0);
    events.forEach((e) => {
      if (e.type !== "finished" || e.t < from) return;
      counts[clamp(Math.floor((e.t - from) / size), 0, n - 1)]++;
    });
    const max = Math.max(1, ...counts);
    runbars.replaceChildren(...counts.map((c, i) => svg("rect", {
      x: (i * 10 + 1).toString(), width: "8", y: (32 - Math.max(c ? 3 : 1, (c / max) * 30)).toFixed(1),
      height: Math.max(c ? 3 : 1, (c / max) * 30).toFixed(1), class: c ? "on" : "off",
    })));
    const total = counts.reduce((a, b) => a + b, 0);
    runbars.setAttribute("aria-label", plural(total, "run") + " finished in the last 15 minutes");
  }

  // One tab stop for the whole timeline; arrows move between bars and lanes.
  function allBars() {
    return Array.from(lanes.values()).filter((l) => !l.agg).map((l) =>
      Array.from(l.bars.values()).sort((a, b) => num(a, "--l") - num(b, "--l")));
  }
  const num = (el, v) => parseFloat(el.style.getPropertyValue(v)) || 0;

  function roving() {
    const flat = allBars().flat();
    if (!flat.length) return;
    let cur = flat.find((b) => b.dataset.run === focusId);
    if (!cur) { cur = flat[flat.length - 1]; focusId = cur.dataset.run; }
    flat.forEach((b) => { b.tabIndex = b === cur ? 0 : -1; });
  }

  function focusBar(b) {
    focusId = b.dataset.run;
    roving();
    b.focus();
  }

  lanesEl.addEventListener("keydown", (e) => {
    const bar = e.target.closest(".bar");
    if (!bar) return;
    const grid = allBars().filter((row) => row.length);
    const li = grid.findIndex((row) => row.includes(bar));
    if (li < 0) return;
    const row = grid[li], i = row.indexOf(bar);
    let next = null;
    if (e.key === "ArrowRight") next = row[i + 1];
    else if (e.key === "ArrowLeft") next = row[i - 1];
    else if (e.key === "Home") next = row[0];
    else if (e.key === "End") next = row[row.length - 1];
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const other = grid[li + (e.key === "ArrowDown" ? 1 : -1)];
      if (other) {
        const at = num(bar, "--l");
        next = other.reduce((best, b) => (Math.abs(num(b, "--l") - at) < Math.abs(num(best, "--l") - at) ? b : best), other[0]);
      }
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onOpen(Number(bar.dataset.slot), bar);
      return;
    } else return;
    e.preventDefault();
    if (next) focusBar(next);
  });

  lanesEl.addEventListener("click", (e) => {
    const bar = e.target.closest(".bar");
    if (bar) { focusId = bar.dataset.run; roving(); onOpen(Number(bar.dataset.slot), bar); }
  });

  const tip = (bar) => {
    const r = runById.get(bar.dataset.run);
    if (!r) return;
    showTip(bar, tipLines(r.subject, [
      ["Start", r.start != null ? clock(r.start) : "unknown (between updates)"],
      ["End", r.running ? "still held" : clock(r.end) + (r.approx ? " (approx.)" : "")],
      ["Duration", r.running ? (r.start != null ? "so far " + runLength(lastNow - r.start) : "running")
        : r.start == null ? "approx." : r.approx ? "about " + runLength(r.end - r.start) : runLength(r.end - r.start)],
      ["Result", result(r)],
    ]));
  };
  lanesEl.addEventListener("pointerover", (e) => { const b = e.target.closest(".bar"); if (b) tip(b); });
  lanesEl.addEventListener("pointerout", (e) => { const b = e.target.closest(".bar"); if (b) hideTip(b); });
  lanesEl.addEventListener("focusin", (e) => { const b = e.target.closest(".bar"); if (b) { focusId = b.dataset.run; tip(b); } });
  lanesEl.addEventListener("focusout", (e) => { const b = e.target.closest(".bar"); if (b) hideTip(b); });

  return {
    update,
    tick(now) { lastNow = now; tick(now); },
    count: () => runs.length,
  };
}
