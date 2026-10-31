// Keyboard shortcuts. Ignored while typing and while a modal dialog is open (dialogs own their keys).
export const SHORTCUTS = [
  ["/", "Jump to a node, slot, session or view"],
  ["Ctrl K", "Jump to anything"],
  ["g o", "Overview"],
  ["g a", "Activity"],
  ["g s", "Sessions"],
  ["g t", "Access tokens"],
  ["j / k", "Next / previous row"],
  ["Enter", "Open the inspector for the row"],
  ["Esc", "Close the inspector, then the palette"],
  [".", "Pause or resume live updates"],
  ["?", "This list"],
];

function typing(target) {
  if (!target || !target.closest) return false;
  return !!target.closest("input, textarea, select, [contenteditable=''], [contenteditable=true]");
}

// handlers: { palette, view(name), move(step), open(), escape() -> bool, pause(), help() }
export function bind(handlers) {
  let chord = 0;
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented) return;
    const modal = document.querySelector("dialog[open]:modal");
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
      if (modal && modal.id !== "palette") return;
      e.preventDefault();
      handlers.palette();
      return;
    }
    if (modal) return;
    if (e.key === "Escape") {
      if (handlers.escape()) e.preventDefault();
      return;
    }
    if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (chord && Date.now() - chord < 1200) {
      chord = 0;
      const view = { o: "overview", a: "activity", s: "sessions", t: "access" }[e.key];
      if (view) { e.preventDefault(); handlers.view(view); }
      return;
    }
    chord = 0;
    switch (e.key) {
      case "/": e.preventDefault(); handlers.palette(); break;
      case "g": chord = Date.now(); break;
      case "j": e.preventDefault(); handlers.move(1); break;
      case "k": e.preventDefault(); handlers.move(-1); break;
      case ".": e.preventDefault(); handlers.pause(); break;
      case "?": e.preventDefault(); handlers.help(); break;
      default: break;
    }
  });
}
