// Pure formatting: no DOM, no clock of its own (callers pass "now"), so it can be unit-tested.

const PLATFORMS = {
  win32: { label: "Windows", icon: "i-windows" },
  windows: { label: "Windows", icon: "i-windows" },
  linux: { label: "Linux", icon: "i-linux" },
  darwin: { label: "macOS", icon: "i-mac" },
  macos: { label: "macOS", icon: "i-mac" },
};
const BROWSERS = { chromium: "Cr", chrome: "Cr", firefox: "Ff", webkit: "Wk" };
const STATE_WORDS = {
  // One state, one word: a failing slot waiting for its retry is still "Failing".
  free: "Free", busy: "In use", unhealthy: "Unhealthy", failing: "Failing", offline: "Offline", cooling: "Failing · cooling down",
};

export const pad = (n) => String(n).padStart(2, "0");

export function plural(n, word, many) {
  return n + " " + (n === 1 ? word : many || word + "s");
}

// "2h 4m", "4m 12s", "38s"; null for unknown.
export function duration(s) {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return d + "d " + h + "h";
  if (h) return h + "h" + (m ? " " + m + "m" : "");
  if (m) return m + "m" + (sec ? " " + sec + "s" : "");
  return sec + "s";
}

// Run lengths: "4.1 s" under a minute, where tenths matter; duration() above.
export function runLength(ms) {
  if (ms == null || !isFinite(ms)) return "–";
  const s = Math.max(0, ms) / 1000;
  return s < 60 ? s.toFixed(1) + " s" : duration(s);
}

// Minute-grained, for sentences that shouldn't change (and re-render) every second.
export function coarse(s) {
  if (s == null || !isFinite(s)) return "an unknown time";
  if (s < 60) return "under a minute";
  return duration(Math.floor(s / 60) * 60);
}

export function ago(s) {
  if (s == null || !isFinite(s)) return "never";
  s = Math.max(0, Math.round(s));
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

// Short age for dense columns: "3s", "47m", "2h", "9d".
export function age(s) {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

// Seconds until something: "in 3m", "in 42s", "now".
export function inTime(s) {
  if (s == null || !isFinite(s)) return "";
  s = Math.round(s);
  if (s <= 0) return "now";
  if (s < 60) return "in " + s + "s";
  if (s < 3600) return "in " + Math.ceil(s / 60) + "m";
  if (s < 86400) return "in " + Math.round(s / 3600) + "h";
  return "in " + Math.round(s / 86400) + "d";
}

// Local wall-clock time of an epoch-ms value.
export function clock(ms, seconds = true) {
  if (ms == null || !isFinite(ms)) return "–";
  const d = new Date(ms);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + (seconds ? ":" + pad(d.getSeconds()) : "");
}

export function fullDate(ms) {
  return ms == null || !isFinite(ms) ? "" : new Date(ms).toLocaleString();
}

export function shortDate(ms, nowMs) {
  const d = new Date(ms), sameYear = d.getFullYear() === new Date(nowMs == null ? ms : nowMs).getFullYear();
  return d.toLocaleDateString(undefined, sameYear ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
}

export function parseTime(iso) {
  const t = iso ? Date.parse(iso) : NaN;
  return isFinite(t) ? t : null;
}

export function platform(p) {
  const known = PLATFORMS[String(p || "").toLowerCase()];
  return known || { label: p || "Unknown platform", icon: "i-server" };
}

export function platformOf(node) {
  if (node.kind !== "gateway") return { label: "Static host", icon: "i-server" };
  if (node.ephemeral) return { label: "Docker runner (" + platform(node.platform).label + ")", icon: "i-docker" };
  return platform(node.platform);
}

export function browserMonograms(list) {
  return (list || []).map((b) => ({ name: b, mono: BROWSERS[String(b).toLowerCase()] || String(b).slice(0, 2) }));
}

// A slot whose failures put it in the cooldown is "cooling" while it waits for its retry.
export function slotState(slot) {
  return slot.state === "failing" && slot.retry_at ? "cooling" : slot.state;
}

export function stateWord(state) {
  return STATE_WORDS[state] || String(state);
}

export function slotName(node, slot) {
  return node.kind === "gateway" ? node.host + " / " + slot.port : node.host + ":" + slot.port;
}

export function slotNameLong(node, slot) {
  return node.kind === "gateway" ? node.host + " / slot " + slot.port : node.host + ":" + slot.port;
}

// The hub address checks and runner hosts outside the compose network can reach. GATEWAY_URL is
// usually "ws://hub:4500", a name only the containers resolve, so keep its scheme and port and
// put in the host this page was opened through.
export function publicHub(status, pageHost) {
  if (status && status.public_gateway_url) return String(status.public_gateway_url).replace(/\/+$/, "");
  const raw = (status && status.gateway_url) || "ws://hub:4500";
  const m = /^(wss?):\/\/(\[[^\]]+\]|[^/:]+)(?::(\d+))?/i.exec(raw);
  if (!m) return raw.replace(/\/+$/, "");
  const host = pageHost && pageHost.indexOf(":") !== -1 && pageHost[0] !== "[" ? "[" + pageHost + "]" : pageHost || m[2];
  return m[1].toLowerCase() + "://" + host + (m[3] ? ":" + m[3] : "");
}

// An address split where a line may break: after "://" and each "/", never inside a node name.
export function endpointParts(url) {
  const m = /^([a-z]+:\/\/)?(.*)$/i.exec(String(url || ""));
  const parts = m[2].split("/").map((p, i, all) => (i < all.length - 1 ? p + "/" : p)).filter(Boolean);
  if (m[1] && parts.length) parts[0] = m[1] + parts[0];
  return parts;
}

// The one address every check connects to: the hub picks a free slot for the browser the check
// launches. node (optional) keeps the check on that node or static host. There is no way to name a
// slot, and no per-slot address to show.
export function connectUrl(hub, node) {
  return hub.replace(/\/+$/, "") + "/connect" + (node ? "?node=" + encodeURIComponent(node) : "");
}

const psq = (s) => "'" + String(s).replace(/'/g, "''") + "'";

// Copy-paste snippets that connect a check to the grid. They read the client token from the
// environment and never embed it; PW_WS_ENDPOINT is what full/example/example.py reads.
export function connectSnippets(hub, node) {
  const base = hub.replace(/\/+$/, "");
  const path = "/connect?" + (node ? "node=" + encodeURIComponent(node) + "&" : "") + "gateway_token=";
  return {
    js: "const { chromium } = require('playwright');\n\nconst HUB = " + JSON.stringify(base).replace(/"/g, "'") + ";\n" +
      "const browser = await chromium.connect(`${HUB}" + path + "${process.env.GATEWAY_CLIENT_TOKEN}`);",
    python: "import os\nfrom playwright.sync_api import sync_playwright\n\nHUB = " + JSON.stringify(base) + "\n" +
      "with sync_playwright() as p:\n    browser = p.chromium.connect(f\"{HUB}" + path + "{os.environ['GATEWAY_CLIENT_TOKEN']}\")",
    powershell: "$env:PW_WS_ENDPOINT = " + psq(base + path) + " + $env:GATEWAY_CLIENT_TOKEN",
    bash: "export PW_WS_ENDPOINT=\"" + base + path + "$GATEWAY_CLIENT_TOKEN\"",
  };
}

// Who holds a busy slot, from portUsage.claimedBy: the hub, for a check that connected to /connect,
// or a claimer that went around the hub. s: a session, or a slot (via_hub only).
// -> { word, long, title, dim }
export function claimWord(s) {
  if (!s || s.via_hub == null) {
    return { word: "–", long: "Unknown", title: "This database doesn't record who claims slots (no portUsage.claimedBy yet)", dim: true };
  }
  if (s.via_hub) {
    return { word: "Hub", long: "The hub, for a check on /connect", title: s.claimed_by ? "claimedBy " + s.claimed_by : "", dim: false };
  }
  return { word: "Another client", long: "Another client, not through the hub", title: "claimedBy is empty: something other than the hub claimed it", dim: true };
}

// What the hub can route right now, per browser: the online gateway nodes that declare it (a
// Chromium channel such as chrome or msedge counts as chromium, as the hub counts it) and their free
// slots; a gateway node that lists none takes any browser (name "*"). Static hosts declare nothing
// and take any browser, after the gateway nodes.
// -> { browsers: [{ name, nodes, slots, free }], anyBrowser: { nodes, slots, free }, versions: [{ version, nodes }] }
const CHROMIUM_FAMILY = /^(chromium|chrome|msedge)(-|$)/; // the hub's rule (gateway/lib/routing.js)
export function routing(data) {
  const browsers = new Map(), versions = new Map(), any = { nodes: 0, slots: 0, free: 0 };
  for (const n of (data && data.nodes) || []) {
    if (n.kind === "gateway" && !n.online) continue;
    const free = n.slots.filter((s) => s.selectable).length;
    if (n.kind !== "gateway") {
      any.nodes++; any.slots += n.slots.length; any.free += free;
    } else {
      const offered = n.browsers || [];
      const names = new Set(offered.length ? offered.map((b) => { b = String(b).toLowerCase(); return CHROMIUM_FAMILY.test(b) ? "chromium" : b; }) : ["*"]);
      for (const name of names) {
        const b = browsers.get(name) || { name, nodes: 0, slots: 0, free: 0 };
        b.nodes++; b.slots += n.slots.length; b.free += free;
        browsers.set(name, b);
      }
    }
    const m = /^(\d+)\.(\d+)/.exec(n.playwright_version || "");
    if (n.kind === "gateway" && m) versions.set(m[1] + "." + m[2], (versions.get(m[1] + "." + m[2]) || 0) + 1);
  }
  const order = { chromium: 0, firefox: 1, webkit: 2, "*": 10 };
  return {
    browsers: Array.from(browsers.values()).sort((a, b) => ((order[a.name] ?? 9) - (order[b.name] ?? 9)) || a.name.localeCompare(b.name)),
    anyBrowser: any,
    versions: Array.from(versions, ([version, nodes]) => ({ version, nodes })).sort((a, b) => b.nodes - a.nodes || a.version.localeCompare(b.version)),
  };
}

// One runnable command per platform to install an agent with a freshly made token.
export function agentCommand(platformId, hub, token, nodeName) {
  const name = nodeName || "";
  switch (platformId) {
    case "windows":
      return ".\\install-backend.ps1 -HubUrl " + psq(hub) + " -AgentToken " + psq(token) + (name ? " -NodeName " + psq(name) : "");
    case "docker":
      return "docker run -d --name " + (name ? "pw-" + name : "pw-runner") + " --restart unless-stopped --init --shm-size 1g " +
        "-e GATEWAY_AGENT_TOKEN=" + token + " -e HUB_URL=" + hub + (name ? " -e NODE_NAME=" + name : "") +
        " -e EPHEMERAL=0 -e SLOTS=4 playwright_orchestrator_runner";
    default: // linux, macos: the agent from a checkout of this repository
      return "GATEWAY_AGENT_TOKEN=" + token + " node gateway/agent.js --hub " + hub + (name ? " --name " + name : "");
  }
}

// "b81e04/3", "b81e04 3", "runner-b81e04 / 3", "pw-static:3001" -> { node, slot } (lower-cased node part).
export function parseSlotQuery(q) {
  const m = /^\s*(.+?)\s*(?:\/|\s|:)\s*(\d+)\s*$/.exec(String(q || ""));
  return m ? { node: m[1].toLowerCase(), slot: m[2] } : null;
}

// Why a slot is in its state, in words, from data the grid really has. ctx: { now (ms),
// threshold, cooldown (s), staleAfter (s), session }.
export function slotSentence(node, slot, ctx) {
  const now = ctx.now, gateway = node.kind === "gateway";
  const lastRun = parseTime(slot.last_run);
  const retry = parseTime(slot.retry_at);
  if (slot.state === "offline") {
    const away = node.last_seen_s != null ? " for " + coarse(node.last_seen_s) : "";
    return "Its node " + node.host + " hasn't sent a heartbeat" + away + ", so checks can't use this slot. " +
      (node.ephemeral ? "It's an ephemeral node: the hub removes it and its slots once it has gone."
        : "It comes back by itself when the agent reconnects.");
  }
  if (slot.state === "busy") {
    const started = lastRun != null ? "since " + clock(lastRun, false) + " (" + coarse((now - lastRun) / 1000) + ")" : "since an unknown time";
    const stale = ctx.session && ctx.session.stale;
    return "A check is using it " + started + "." + (stale ? " That's longer than " + duration(ctx.staleAfter) +
      ", so the check may have died without releasing it." : "") +
      (slot.fails ? " " + plural(slot.fails, "connect failure") + " before this run." : "");
  }
  if (slot.state === "unhealthy") {
    return "Its run-server isn't answering health checks, so checks skip it until the agent reports it healthy again.";
  }
  if (slot.state === "failing") {
    let text = "Checks failed to connect " + plural(slot.fails, "time") + " in a row (limit " + ctx.threshold + ").";
    if (lastRun != null) text += " Last attempt " + clock(lastRun, false) + ".";
    if (retry != null && retry > now) {
      text += " Skipped until " + clock(retry, false) + " (" + inTime((retry - now) / 1000) + "), then retried automatically.";
    } else if (lastRun == null) {
      text += " It hasn't run yet, so the next check may try it.";
    } else {
      text += " Its cooldown is over: the next check may try it again.";
    }
    text += gateway
      ? (slot.healthy ? " Its run-server passes health checks, so suspect the gateway path or the browser launch." : "")
      : " Static hosts aren't health-checked: make sure a run-server listens on " + node.host + ":" + slot.port + ".";
    return text;
  }
  if (slot.fails) {
    return "Free. " + plural(slot.fails, "connect failure") + " in a row so far (limit " + ctx.threshold + "): it's still handed out.";
  }
  return "Free and ready: the hub can hand it to the next check that fits.";
}

export function nodeSentence(node, ctx) {
  if (node.kind === "gateway" && !node.online) {
    return "Offline: no heartbeat for " + coarse(node.last_seen_s) + "." +
      (node.ephemeral ? " The hub removes ephemeral nodes once they're gone." : " It comes back by itself when its agent reconnects.");
  }
  const parts = [];
  if (node.failing) parts.push(plural(node.failing, "slot") + " failing");
  if (node.unhealthy) parts.push(plural(node.unhealthy, "slot") + " unhealthy");
  const busy = node.busy + " of " + node.total + " slots in use";
  return (parts.length ? parts.join(", ") + ". " : "All slots healthy. ") + busy + ".";
}
