// "Recent runs": one swimlane per node over the last 5 minutes, one 4px track per slot, bars for
// every run the recorder saw. Hand-built DOM: positions are CSS variables set with
// style.setProperty, never style attributes in markup (the CSP forbids them).
import { html, setHTML, setText, patchList } from "../html.js";
import { clock, runLength, parseTime, plural, slotName } from "../format.js";
import { bursts } from "../live.js";
import { showTip, hideTip, tipLines } from "./toast.js";

export const SPAN = 5 * 60 * 1000;
const LONG = 15 * 60 * 1000;
const AGGREGATE_OVER = 8;
const BUCKET = 5000;
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
  const runs = new Map();
  for (const e of events) {
    if (e.type !== "finished" || e.end == null || e.end < from) continue;
    const a = at.get(e.slot);
    if (!a) continue;
    const id = e.start != null ? "run-" + e.slot + "-" + e.start : e.id;
    runs.set(id, {
      id, slot: e.slot, host: a.n.host, row: a.row, subject: e.subject || slotName(a.n, a.s), start: e.start, end: e.end,
      approx: !!e.approx, failed: !!e.failed, by: e.by, running: false,
    });
  }
  for (const [slotId, a] of at) {
    if (!a.s.busy) continue;
    const start = parseTime(a.s.last_run);
    const id = "run-" + slotId + "-" + start;
    if (!runs.has(id)) runs.set(id, { id, slot: slotId, host: a.n.host, row: a.row, subject: slotName(a.n, a.s), start, end: null, running: true });
  }
  return Array.from(runs.values());
}

export function createTimeline(root, { onOpen }) {
  setHTML(root, html`
    <div class="tl-head">
      <h2 class="section-label" id="runs-title">Recent runs</h2>
      <p class="tl-summary mono" id="runs-summary"></p>
      <p class="tl-counter" id="runs-counter"></p>
    </div>
    <div class="tl-body">
      <div class="tl-ruler" aria-hidden="true">
        <div class="tl-gutter"></div>
        <div class="tl-scale">
          <div class="tl-bursts" id="tl-bursts"></div>
          <div class="tl-ticks" id="tl-ticks"></div>
          <span class="tl-now">now</span>
        </div>
      </div>
      <div class="tl-lanes" id="tl-lanes" role="group" aria-label="Runs per node, last 5 minutes. Arrow keys move between runs, Enter opens the slot."></div>
      <p class="tl-empty" id="tl-empty" hidden>No nodes to show yet.</p>
      <div class="tl-capacity">
        <div class="tl-gutter"><span class="tl-cap-label">Capacity</span><span class="tl-cap-sub" id="tl-cap-sub">15 min</span></div>
        <div class="tl-spark" id="tl-spark"></div>
      </div>
    </div>
    <div class="tl-phone">
      <div class="tl-phone-spark"><span class="tl-cap-label">Runs · 15 min</span><div id="tl-runbars" class="tl-runbars"></div></div>
      <ol class="tl-recent" id="tl-recent" aria-label="Last 5 runs"></ol>
    </div>`);

  const lanesEl = root.querySelector("#tl-lanes"), ticksEl = root.querySelector("#tl-ticks");
  const burstsEl = root.querySelector("#tl-bursts"), sparkEl = root.querySelector("#tl-spark");
  const lanes = new Map();
  const runById = new Map();
  let runs = [], focusId = null, data = null, events = [], samples = [], lastNow = Date.now();

  // Capacity sparkline: busy as an area, capacity as a line, red where nothing was free.
  const W = 1000, H = 28;
  const spark = svg("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", class: "spark", role: "img" });
  const bands = svg("g", { class: "spark-zero" });
  const area = svg("path", { class: "spark-busy" });
  const cap = svg("path", { class: "spark-total" });
  const busyLine = svg("path", { class: "spark-line" });
  spark.append(bands, area, busyLine, cap);
  sparkEl.appendChild(spark);

  const runbars = svg("svg", { viewBox: "0 0 300 32", preserveAspectRatio: "none", class: "spark", role: "img" });
  root.querySelector("#tl-runbars").appendChild(runbars);

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
      for (let i = 0; i <= SPAN / BUCKET; i++) {
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

  function barLabel(r) {
    const what = r.running ? "running" : r.by === "you" ? "released by you" : r.failed ? "failures went up" : r.approx ? "finished, approx." : "finished";
    const dur = r.running ? "" : r.start != null && r.end != null && !r.approx ? ", " + runLength(r.end - r.start) : "";
    return r.subject + ": " + what + dur + (r.start != null ? ", started " + clock(r.start) : "") + (r.end != null ? ", ended " + clock(r.end) : "");
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
    const cls = "bar" + (r.running ? " running" : " done") + (r.failed ? " failed" : "") + (r.approx && r.start == null ? " approx" : "") +
      (r.approx && r.start != null ? " approx-end" : "") + (r.by === "you" ? " yours" : "");
    if (b.className !== cls) b.className = cls;
    const label = barLabel(r);
    if (b.getAttribute("aria-label") !== label) b.setAttribute("aria-label", label);
    b.style.setProperty("--row", r.row);
  }

  // Data changed: lanes, bars, bursts, list.
  function update(nextData, recorder, now) {
    data = nextData;
    events = recorder.events;
    samples = recorder.samples;
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

    runs = runsFrom(data, events, now);
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
    tick(now);
  }

  function renderSummary(now) {
    const inWindow = runs.filter((r) => r.running || (r.end != null && r.end >= now - SPAN));
    const failed = inWindow.filter((r) => r.failed).length;
    const recent = samples.filter((s) => s.t >= now - SPAN);
    const peak = recent.reduce((m, s) => (s.busy > m.busy ? s : m), { busy: 0, total: data && data.totals ? data.totals.slots : 0 });
    const total = (data && data.totals && data.totals.slots) || 0;
    setText(root.querySelector("#runs-summary"), "Last 5 min: " + plural(inWindow.length, "run") +
      " · " + failed + " failed · peak " + peak.busy + "/" + total + " busy");
    const fifteen = events.filter((e) => e.type === "finished" && e.t >= now - LONG).length;
    setHTML(root.querySelector("#runs-counter"), html`<span class="num">${fifteen}</span> ${fifteen === 1 ? "run" : "runs"} finished · 15 min`);
  }

  function renderRecent() {
    const last = events.filter((e) => e.type === "finished").slice(-5).reverse();
    const list = root.querySelector("#tl-recent");
    if (!last.length) {
      setHTML(list, html`<li class="tl-recent-empty">No runs seen yet. Runs appear here as checks finish.</li>`);
      return;
    }
    list.__html = null;
    patchList(list, last, (e) => e.id, (e) => html`
      <li data-key="${e.id}"><a href="#/slot/${e.slot}" class="tl-recent-row">
        <span class="dot ${e.failed ? "bad" : "accent"}" aria-hidden="true"></span>
        <span class="mono subject">${e.subject}</span>
        <span class="mono meta">${e.approx ? "approx." : runLength(e.dur)}</span>
        <span class="mono meta">${clock(e.end)}</span></a></li>`);
  }

  // Every second: slide everything left.
  function tick(now) {
    const from = now - SPAN;
    const x = (t) => clamp((t - from) / SPAN, 0, 1);
    lanes.forEach((l) => {
      if (l.agg) {
        const total = Math.max(1, l.node.slots.length);
        const first = Math.floor(from / BUCKET) * BUCKET;
        const mine = runs.filter((r) => r.host === l.node.host);
        l.buckets.forEach((b, i) => {
          const t0 = first + i * BUCKET, t1 = t0 + BUCKET;
          const busy = mine.filter((r) => (r.start == null ? r.end - 1000 : r.start) < t1 && (r.end == null ? now : r.end) > t0).length;
          b.style.setProperty("--l", pct(x(t0)));
          b.style.setProperty("--w", pct(x(t1) - x(t0)));
          b.style.setProperty("--o", (busy / total).toFixed(3));
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

    // Minute ticks slide with the window; "now" stays pinned to the right edge.
    const minutes = [];
    for (let m = Math.ceil(from / 60000) * 60000; m <= now - 20000; m += 60000) minutes.push(m);
    patchList(ticksEl, minutes, (m) => m, (m) => html`<span class="tl-tick" data-key="${m}">${clock(m, false)}</span>`);
    Array.from(ticksEl.children).forEach((el) => el.style.setProperty("--l", pct(x(Number(el.dataset.key)))));
    lanesEl.style.setProperty("--grid-offset", pct(x(Math.ceil(from / 60000) * 60000)));

    // Flags hang left of their end time. Where two would overlap, the newer one wins.
    const scale = burstsEl.clientWidth || 1000;
    const flags = [];
    let leftEdge = Infinity;
    bursts(events.filter((e) => e.end != null && e.end >= from)).filter((b) => b.t >= from).reverse().forEach((b) => {
      const right = x(b.t) * scale, width = 14 + 7 * (String(b.count).length + (b.avg != null ? 15 : 14));
      if (right <= leftEdge - 4) { flags.push(b); leftEdge = right - width; }
    });
    flags.reverse();
    patchList(burstsEl, flags, (b) => b.id, (b) => html`
      <span class="tl-flag ${b.failed ? "bad" : ""}" data-key="${b.id}">${b.count} runs${b.avg != null ? " · " + runLength(b.avg) + " avg" : " · approx."}</span>`);
    Array.from(burstsEl.children).forEach((el) => {
      const b = flags.find((f) => f.id === el.dataset.key);
      if (b) el.style.setProperty("--l", pct(x(b.t)));
    });

    drawSpark(now);
    drawRunBars(now);
  }

  function drawSpark(now) {
    const from = now - LONG;
    const pts = samples.filter((s) => s.t >= from);
    const sub = root.querySelector("#tl-cap-sub");
    const first = pts.length ? pts[0].t : now;
    setText(sub, first > from + 30000 ? "since " + clock(first, false) : "15 min");
    const max = Math.max(1, ...pts.map((s) => s.total));
    const X = (t) => ((t - from) / LONG) * W;
    const Y = (v) => H - 1 - (v / max) * (H - 2);
    if (!pts.length) {
      area.setAttribute("d", "");
      busyLine.setAttribute("d", "");
      cap.setAttribute("d", "");
      bands.replaceChildren();
      spark.setAttribute("aria-label", "Capacity over 15 minutes: no samples yet");
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
    area.setAttribute("d", line + "L" + lastX + " " + H + "L" + X(pts[0].t).toFixed(1) + " " + H + "Z");
    cap.setAttribute("d", top);
    const zero = [];
    pts.forEach((s, i) => {
      if (s.total && s.avail === 0) zero.push([X(s.t), i + 1 < pts.length ? X(pts[i + 1].t) : X(now)]);
    });
    bands.replaceChildren(...zero.map(([a, b]) => svg("rect", { x: a.toFixed(1), y: 0, width: Math.max(2, b - a).toFixed(1), height: H })));
    const peak = pts.reduce((m, s) => Math.max(m, s.busy), 0);
    spark.setAttribute("aria-label", "Capacity over 15 minutes: now " + last.busy + " of " + last.total +
      " busy, peak " + peak + (zero.length ? ", with times when no slot was free" : ""));
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
    const result = r.running ? "running" : r.by === "you" ? "released by you" : r.failed ? "failures went up" : "finished";
    showTip(bar, tipLines(r.subject, [
      ["Start", r.start != null ? clock(r.start) : "unknown (between updates)"],
      ["End", r.running ? "running" : clock(r.end) + (r.approx ? " (approx.)" : "")],
      ["Duration", r.running ? (r.start != null ? "so far " + runLength(lastNow - r.start) : "running")
        : r.approx || r.start == null ? "approx." : runLength(r.end - r.start)],
      ["Result", result],
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
