// This browser's storage, wrapped once: it can be blocked (privacy modes) or full, and must never
// throw. Preferences and the recorder's cache live here; admin sign-in keeps its own (auth.js).
export const localStore = {
  get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
  set(key, value) { try { window.localStorage.setItem(key, value); return true; } catch (e) { return false; } },
  remove(key) { try { window.localStorage.removeItem(key); } catch (e) { /* nothing stored */ } },
};
