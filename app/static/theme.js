/* Global light/dark theme toggle, shared by every AlphaBook page.
 *
 * Loaded from each template's <head> so the saved theme is applied before the
 * body paints (no flash). It also injects a toggle button into the topbar's
 * actions area, falling back to a floating button on pages without one.
 */
(function () {
  "use strict";
  var KEY = "ab-theme";

  function apply(theme) {
    if (theme === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function saved() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }

  function store(theme) {
    try { localStorage.setItem(KEY, theme); } catch (e) { /* private mode */ }
  }

  function current() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  var icon = function (t) { return t === "light" ? "🌙" : "☀️"; };

  // Apply immediately — this runs in <head>, before the body renders.
  apply(saved() || "dark");

  function mount() {
    if (document.getElementById("themeToggle")) return;
    var btn = document.createElement("button");
    btn.id = "themeToggle";
    btn.type = "button";
    btn.className = "theme-toggle";
    btn.title = "Toggle light / dark";
    btn.setAttribute("aria-label", "Toggle light or dark mode");
    btn.textContent = icon(current());
    btn.addEventListener("click", function () {
      var next = current() === "light" ? "dark" : "light";
      apply(next);
      store(next);
      btn.textContent = icon(next);
    });

    var actions = document.querySelector(".topbar .actions");
    if (actions) {
      actions.insertBefore(btn, actions.firstChild);
    } else {
      btn.classList.add("theme-toggle-float");
      document.body.appendChild(btn);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
