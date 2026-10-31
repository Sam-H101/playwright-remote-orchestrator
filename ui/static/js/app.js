// Shared page context, filled in by main.js, so views don't import each other.
import { publicHub, parseTime } from "./format.js";

const HUB_KEY = "gridHub";

function local(key, value) {
  try {
    if (value === undefined) return window.localStorage.getItem(key);
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch (e) { /* storage blocked: not remembered */ }
  return null;
}

export const prefs = {
  get: (key, fallback) => { const v = local(key); return v == null ? fallback : v; },
  set: (key, value) => local(key, value == null ? null : String(value)),
};

const hubListeners = new Set();

export const app = {
  live: null,
  inspect: null,        // (type, id, opener) -> void
  closeInspector: null, // () -> void
  go: null,             // (hash) -> void

  data() { return this.live && this.live.data; },
  now() { return this.live ? this.live.serverNow() : Date.now(); },

  findSlot(id) {
    const d = this.data();
    id = Number(id);
    for (const node of (d && d.nodes) || []) {
      for (const slot of node.slots) if (slot.id === id) return { node, slot, session: this.findSession(id) };
    }
    return null;
  },
  findNode(host) {
    const d = this.data();
    return ((d && d.nodes) || []).find((n) => n.host === host) || null;
  },
  findSession(slotId) {
    const d = this.data();
    return ((d && d.sessions) || []).find((s) => s.slot_id === Number(slotId)) || null;
  },

  // The hub address shown in every copyable command: the user's override, else the server's
  // public address, else one derived from this page's host.
  hub() {
    const custom = (prefs.get(HUB_KEY, "") || "").trim();
    return custom || publicHub(this.data(), location.hostname);
  },
  hubIsCustom() { return !!(prefs.get(HUB_KEY, "") || "").trim(); },
  setHub(value) {
    const v = String(value || "").trim().replace(/\/+$/, "");
    prefs.set(HUB_KEY, v && v !== publicHub(this.data(), location.hostname) ? v : null);
    hubListeners.forEach((fn) => fn(this.hub()));
  },
  onHub(fn) { hubListeners.add(fn); return () => hubListeners.delete(fn); },

  cfg() {
    const d = this.data() || {};
    return { threshold: d.fail_threshold || 3, cooldown: d.fail_cooldown_s || 300, staleAfter: d.stale_after_s || 3600, now: this.now() };
  },
  ms: parseTime,
};
