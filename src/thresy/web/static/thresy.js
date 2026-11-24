// Comfort only: every page works without this script.
//  * the theme switch (system -> light -> dark), remembered in this browser;
//  * tooltips for chart marks (their <title> text, shown faster and on touch);
//  * a crosshair across the three charts of a run, with the values under it.
(function () {
  "use strict";
  var root = document.documentElement;

  // -- theme ---------------------------------------------------------------------------------
  var order = ["system", "light", "dark"];
  function current() { return root.getAttribute("data-theme") || "system"; }
  function apply(next) {
    if (next === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", next);
    try {
      if (next === "system") localStorage.removeItem("thresy-theme");
      else localStorage.setItem("thresy-theme", next);
    } catch (e) {}
    var btn = document.querySelector("[data-theme-toggle]");
    if (btn) btn.setAttribute("aria-label", btn.getAttribute("data-label-" + next) || next);
  }
  var toggle = document.querySelector("[data-theme-toggle]");
  if (toggle) {
    toggle.hidden = false;
    toggle.setAttribute("aria-label", toggle.getAttribute("data-label-" + current()) || "");
    toggle.addEventListener("click", function () {
      apply(order[(order.indexOf(current()) + 1) % order.length]);
    });
  }

  // -- tooltips ------------------------------------------------------------------------------
  var tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;
  tip.setAttribute("role", "status");
  document.body.appendChild(tip);
  function place(x, y) {
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(Math.max(8, x - w / 2), window.innerWidth - w - 8);
    var top = y - h - 14;
    if (top < 8) top = y + 18;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function show(el, x, y) {
    var text = el.getAttribute("data-tip");
    if (!text) return;
    tip.textContent = text;
    tip.hidden = false;
    place(x, y);
  }
  // the native title would show as well: move it to data-tip only
  Array.prototype.forEach.call(document.querySelectorAll(".chart .hit title"), function (t) {
    t.parentNode.removeChild(t);
  });
  document.addEventListener("pointermove", function (ev) {
    var el = ev.target.closest && ev.target.closest(".chart .hit");
    if (el) show(el, ev.clientX, ev.clientY);
    else if (!tip.hidden && !(ev.target.closest && ev.target.closest(".hit-area"))) tip.hidden = true;
  });
  document.addEventListener("pointerdown", function (ev) {
    var el = ev.target.closest && ev.target.closest(".chart .hit");
    if (el) show(el, ev.clientX, ev.clientY);
    else tip.hidden = true;
  });
  window.addEventListener("scroll", function () { tip.hidden = true; }, { passive: true });

  // -- crosshair over the charts of one run --------------------------------------------------
  var dataEl = document.getElementById("profile-data");
  if (!dataEl) return;
  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }
  var readout = document.querySelector("[data-readout]");
  var SVGNS = "http://www.w3.org/2000/svg";
  function nearest(km) {
    var xs = data.x, lo = 0, hi = xs.length - 1;
    while (hi - lo > 1) {
      var mid = (lo + hi) >> 1;
      if (xs[mid] < km) lo = mid; else hi = mid;
    }
    return Math.abs(xs[lo] - km) <= Math.abs(xs[hi] - km) ? lo : hi;
  }
  function draw(i) {
    var km = data.x[i], max = data.x[data.x.length - 1] || 1;
    Array.prototype.forEach.call(document.querySelectorAll("g.xhair"), function (g) {
      var left = +g.getAttribute("data-left"), width = +g.getAttribute("data-width");
      var top = +g.getAttribute("data-top"), height = +g.getAttribute("data-height");
      var x = left + (km / max) * width;
      while (g.firstChild) g.removeChild(g.firstChild);
      var line = document.createElementNS(SVGNS, "line");
      line.setAttribute("x1", x); line.setAttribute("x2", x);
      line.setAttribute("y1", top); line.setAttribute("y2", top + height);
      g.appendChild(line);
    });
    if (readout) {
      while (readout.firstChild) readout.removeChild(readout.firstChild);
      var parts = [["km", (Math.round(km * 10) / 10).toString(), ""]];
      for (var k = 0; k < 3; k++) {
        var key = ["pace", "hr", "alt"][k];
        parts.push([data.labels[k], data[key][i], data.units[k]]);
      }
      parts.forEach(function (p) {
        var span = document.createElement("span");
        span.appendChild(document.createTextNode(p[0] + " "));
        var b = document.createElement("b");
        b.textContent = p[1];
        span.appendChild(b);
        if (p[2]) span.appendChild(document.createTextNode(" " + p[2]));
        readout.appendChild(span);
      });
    }
  }
  function hint() {
    if (!readout || !data.hint) return;
    while (readout.firstChild) readout.removeChild(readout.firstChild);
    var span = document.createElement("span");
    span.className = "hint";
    span.textContent = data.hint;
    readout.appendChild(span);
  }
  function clear() {
    Array.prototype.forEach.call(document.querySelectorAll("g.xhair"), function (g) {
      while (g.firstChild) g.removeChild(g.firstChild);
    });
    hint();
  }
  hint();
  function onMove(ev) {
    var rect = ev.currentTarget;
    var svg = rect.ownerSVGElement;
    var box = svg.getBoundingClientRect();
    var scale = svg.viewBox.baseVal.width / box.width;
    var x = (ev.clientX - box.left) * scale;
    var left = +rect.getAttribute("x"), width = +rect.getAttribute("width");
    var frac = Math.min(Math.max((x - left) / width, 0), 1);
    draw(nearest(frac * data.x[data.x.length - 1]));
  }
  Array.prototype.forEach.call(document.querySelectorAll(".hit-area[data-profile]"), function (r) {
    r.addEventListener("pointermove", onMove);
    r.addEventListener("pointerdown", onMove);
    r.addEventListener("pointerleave", clear);
  });
})();
