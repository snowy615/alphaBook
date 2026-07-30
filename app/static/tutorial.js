/* Short click-through tutorial, shared by every game page.
 *
 * Nobody reads a rules page before a competition starts, so each game ships a
 * handful of cards covering only what you cannot play without. It shows once
 * per game per browser, and the topbar button reopens it mid-round without
 * navigating away from a live game.
 *
 *   AlphaTutorial.init({
 *     key: "risks",            // localStorage key + version stamp
 *     version: 1,              // bump to re-show after rewriting the content
 *     title: "Risks",
 *     label: "How to play",    // topbar button text
 *     fullRulesHref: "/risks", // optional link in the footer
 *     steps: [{ heading, body, facts: [[label, value], ...] }, ...],
 *   });
 */
(function () {
  "use strict";

  const state = {
    key: null, version: 1, title: "", steps: [], fullRulesHref: null,
    index: 0, root: null, open: false, lastFocus: null,
  };

  const storeKey = () => `ab-tut-${state.key}-v${state.version}`;

  function seen() {
    try { return localStorage.getItem(storeKey()) === "done"; }
    catch { return false; }
  }

  function markSeen() {
    try { localStorage.setItem(storeKey(), "done"); } catch { /* private mode */ }
  }

  function build() {
    if (state.root) return state.root;

    const root = document.createElement("div");
    root.className = "tut-overlay hidden";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", state.title + " — how to play");
    root.innerHTML = `
      <div class="tut-card" tabindex="-1">
        <div class="tut-top">
          <span class="tut-eyebrow"></span>
          <button class="tut-skip" type="button">Skip</button>
        </div>
        <h2 class="tut-heading"></h2>
        <div class="tut-body"></div>
        <div class="tut-facts"></div>
        <div class="tut-foot">
          <div class="tut-dots"></div>
          <div class="tut-nav">
            <button class="btn ghost tut-back" type="button">Back</button>
            <button class="btn primary tut-next" type="button">Next</button>
          </div>
        </div>
        <div class="tut-hint">Click anywhere, or press <kbd>Enter</kbd>, to continue</div>
      </div>`;
    document.body.appendChild(root);
    state.root = root;

    // A click anywhere on the card or the backdrop advances, which is the
    // whole interaction. The controls stop propagation so they still work.
    root.addEventListener("click", (e) => {
      if (e.target.closest(".tut-skip, .tut-back, .tut-next, .tut-full")) return;
      next();
    });
    root.querySelector(".tut-skip").addEventListener("click", (e) => {
      e.stopPropagation(); finish();
    });
    root.querySelector(".tut-back").addEventListener("click", (e) => {
      e.stopPropagation(); prev();
    });
    root.querySelector(".tut-next").addEventListener("click", (e) => {
      e.stopPropagation(); next();
    });

    return root;
  }

  function onKey(e) {
    if (!state.open) return;
    if (e.key === "Enter" || e.key === " " || e.key === "ArrowRight") {
      e.preventDefault(); next();
    } else if (e.key === "ArrowLeft" || e.key === "Backspace") {
      e.preventDefault(); prev();
    } else if (e.key === "Escape") {
      e.preventDefault(); finish();
    }
  }

  function render() {
    const root = state.root;
    const step = state.steps[state.index];
    if (!step) return;
    const last = state.index === state.steps.length - 1;

    root.querySelector(".tut-eyebrow").textContent =
      `${state.title} · ${state.index + 1} of ${state.steps.length}`;
    root.querySelector(".tut-heading").textContent = step.heading || "";
    root.querySelector(".tut-body").innerHTML = step.body || "";

    const facts = root.querySelector(".tut-facts");
    if (step.facts && step.facts.length) {
      facts.classList.remove("hidden");
      facts.innerHTML = step.facts.map(([label, value]) =>
        `<div><span>${label}</span><strong>${value}</strong></div>`).join("");
    } else {
      facts.classList.add("hidden");
      facts.innerHTML = "";
    }

    root.querySelector(".tut-dots").innerHTML = state.steps
      .map((_, i) => `<span class="${i === state.index ? "is-on" : ""}"></span>`).join("");

    const back = root.querySelector(".tut-back");
    back.disabled = state.index === 0;
    back.classList.toggle("tut-invisible", state.index === 0);

    const nextBtn = root.querySelector(".tut-next");
    nextBtn.textContent = last ? "Got it" : "Next";

    const foot = root.querySelector(".tut-foot");
    let full = foot.querySelector(".tut-full");
    if (last && state.fullRulesHref && !full) {
      full = document.createElement("a");
      full.className = "tut-full";
      full.href = state.fullRulesHref;
      full.textContent = "Full rules";
      foot.insertBefore(full, foot.querySelector(".tut-nav"));
    } else if (full) {
      full.classList.toggle("hidden", !last);
    }

    root.querySelector(".tut-hint").innerHTML = last
      ? "Press <kbd>Enter</kbd> to close"
      : "Click anywhere, or press <kbd>Enter</kbd>, to continue";
  }

  function open(index) {
    if (!state.steps.length) return;
    build();
    state.index = Math.max(0, Math.min(index || 0, state.steps.length - 1));
    state.open = true;
    state.lastFocus = document.activeElement;
    state.root.classList.remove("hidden");
    document.body.classList.add("tut-locked");
    render();
    state.root.querySelector(".tut-card").focus();
  }

  function close() {
    if (!state.root) return;
    state.open = false;
    state.root.classList.add("hidden");
    document.body.classList.remove("tut-locked");
    if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
  }

  function finish() {
    markSeen();
    close();
  }

  function next() {
    if (state.index >= state.steps.length - 1) { finish(); return; }
    state.index += 1;
    render();
  }

  function prev() {
    if (state.index === 0) return;
    state.index -= 1;
    render();
  }

  function mountButton(label) {
    if (document.getElementById("tutBtn")) return;
    const btn = document.createElement("button");
    btn.id = "tutBtn";
    btn.type = "button";
    btn.className = "btn ghost tut-btn";
    btn.textContent = label || "How to play";
    btn.title = "How to play";
    btn.addEventListener("click", () => open(0));

    const actions = [...document.querySelectorAll(".topbar .actions")]
      .find((el) => !el.classList.contains("hidden"))
      || document.querySelector(".topbar .actions");
    if (actions) {
      actions.insertBefore(btn, actions.firstChild);
    } else {
      btn.classList.add("tut-btn-float");
      document.body.appendChild(btn);
    }
  }

  function init(config) {
    state.key = config.key;
    state.version = config.version || 1;
    state.title = config.title || "How to play";
    state.steps = config.steps || [];
    state.fullRulesHref = config.fullRulesHref || null;

    document.addEventListener("keydown", onKey);

    const start = () => {
      mountButton(config.label);
      if (config.autoShow !== false && !seen()) open(0);
    };

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  }

  window.AlphaTutorial = { init, open, close, seen };
})();
