// Runs before first paint (a classic script in <head>), so the saved theme, row density and rail
// width apply without a flash. Without a saved theme the page follows the OS.
(function () {
  var root = document.documentElement;
  try {
    var theme = localStorage.getItem("gridTheme");
    if (theme === "light" || theme === "dark") root.dataset.theme = theme;
    if (localStorage.getItem("gridDensity") === "compact") root.dataset.density = "compact";
    if (localStorage.getItem("gridRail") === "collapsed") root.dataset.rail = "collapsed";
    var width = Number(localStorage.getItem("gridInspectorWidth"));
    if (width >= 320 && width <= 640) root.style.setProperty("--insp-w", width + "px");
  } catch (e) { /* storage blocked: defaults */ }
})();
