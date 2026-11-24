// Applies the theme chosen with the switch before the page paints (no flash). Without a
// stored choice the page follows the system (prefers-color-scheme).
(function () {
  try {
    var t = localStorage.getItem("thresy-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) {}
})();
