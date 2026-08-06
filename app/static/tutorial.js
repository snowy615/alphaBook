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

    // On the last card, offer the screen tour explicitly for anyone replaying.
    let tourBtn = foot.querySelector(".tut-tourbtn");
    if (last && state.tour && !tourBtn) {
      tourBtn = document.createElement("button");
      tourBtn.type = "button";
      tourBtn.className = "btn ghost tut-tourbtn";
      tourBtn.textContent = "Show me the screen";
      tourBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        markSeen();
        markTourSeen();
        close();
        setTimeout(() => startTour(state.tour), 180);
      });
      foot.insertBefore(tourBtn, foot.querySelector(".tut-nav"));
    } else if (tourBtn) {
      tourBtn.classList.toggle("hidden", !last);
    }

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

  const tourKey = () => `ab-tour-${state.key}-v${state.version}`;

  function tourSeen() {
    try { return localStorage.getItem(tourKey()) === "done"; }
    catch { return false; }
  }

  function markTourSeen() {
    try { localStorage.setItem(tourKey(), "done"); } catch { /* private mode */ }
  }

  function finish() {
    markSeen();
    close();
    // The cards explain the game; the tour explains the screen. Run it once,
    // straight after, so a first-timer lands on the board already oriented.
    if (state.tour && !tourSeen()) {
      markTourSeen();
      setTimeout(() => startTour(state.tour), 220);
    }
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

  // A step may be scoped to certain levels: a beginner gets the long version,
  // an advanced player is not made to sit through it.
  function forLevel(steps, level) {
    return (steps || []).filter((s) => !s.levels || s.levels.includes(level));
  }

  function init(config) {
    state.key = config.key;
    state.version = config.version || 1;
    state.title = config.title || "How to play";
    state.steps = config.steps || [];
    state.fullRulesHref = config.fullRulesHref || null;
    state.tour = config.tour || null;

    document.addEventListener("keydown", onKey);

    const start = async () => {
      mountButton(config.label);

      // Trim the deck to the player's level before anything is shown.
      let level = "beginner";
      if (window.AlphaLearn) {
        try {
          const p = await window.AlphaLearn.state();
          if (p && p.level) level = p.level;
        } catch { /* signed out: treat as beginner */ }
      }
      state.level = level;
      const trimmed = forLevel(config.steps, level);
      if (trimmed.length) state.steps = trimmed;

      if (config.autoShow !== false && !seen()) open(0);
    };

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  }

  // ── Anchored tour ─────────────────────────────────────────────────────
  // A spotlight on one real control at a time with a sentence on what it does.
  // This is the part that answers "what does this button do" — the card deck
  // explains the game, the tour explains the screen.
  const tour = {
    steps: [], index: 0, root: null, open: false, onEnd: null,
  };

  // Present is not the same as visible: several of these controls exist in the
  // DOM but render 0x0 until you are signed in or a game is running. Pointing a
  // spotlight at a zero-size box is worse than skipping the step.
  function visible(selector) {
    const el = document.querySelector(selector);
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  }

  function buildTour() {
    if (tour.root) return tour.root;
    const root = document.createElement("div");
    root.className = "tut-tour hidden";
    root.innerHTML = `
      <div class="tut-spot" aria-hidden="true"></div>
      <div class="tut-call" role="dialog" aria-modal="true" tabindex="-1">
        <div class="tut-call-top">
          <span class="tut-call-count"></span>
          <button class="tut-skip tut-call-skip" type="button">Done</button>
        </div>
        <h3 class="tut-call-title"></h3>
        <p class="tut-call-body"></p>
        <div class="tut-call-foot">
          <div class="tut-dots"></div>
          <div class="tut-nav">
            <button class="btn ghost tut-call-back" type="button">Back</button>
            <button class="btn primary tut-call-next" type="button">Next</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(root);
    tour.root = root;

    root.querySelector(".tut-call-skip").addEventListener("click", endTour);
    root.querySelector(".tut-call-back").addEventListener("click", () => stepTour(-1));
    root.querySelector(".tut-call-next").addEventListener("click", () => stepTour(1));
    // Clicking the dimmed area advances, same as the card deck.
    root.addEventListener("click", (e) => {
      if (e.target.closest(".tut-call")) return;
      stepTour(1);
    });
    return root;
  }

  // Bring the target into view. Kept separate from placeTour: the scroll
  // listener calls placeTour, so scrolling from inside it would re-scroll on
  // every frame and the spotlight would chase a rect that never settles.
  function scrollToStep() {
    const step = tour.steps[tour.index];
    if (!step) return;
    const el = document.querySelector(step.target);
    if (!el) return;
    const r = el.getBoundingClientRect();
    const fullyVisible = r.top >= 60 && r.bottom <= window.innerHeight - 20;
    if (!fullyVisible) {
      el.scrollIntoView({ block: "center", inline: "nearest", behavior: "auto" });
    }
  }

  function placeTour() {
    const step = tour.steps[tour.index];
    if (!step) return;
    const el = document.querySelector(step.target);
    const spot = tour.root.querySelector(".tut-spot");
    const call = tour.root.querySelector(".tut-call");

    if (!el) { spot.style.display = "none"; centreCall(call); return; }

    const r = el.getBoundingClientRect();
    const pad = 6;
    spot.style.display = "block";
    spot.style.top = `${r.top - pad}px`;
    spot.style.left = `${r.left - pad}px`;
    spot.style.width = `${r.width + pad * 2}px`;
    spot.style.height = `${r.height + pad * 2}px`;

    // Prefer below the target, flip above when there is no room.
    const cw = call.offsetWidth || 320;
    const ch = call.offsetHeight || 160;
    const gap = 12;
    let top = r.bottom + gap;
    if (top + ch > window.innerHeight - 8) top = Math.max(8, r.top - ch - gap);
    let left = r.left + r.width / 2 - cw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - cw - 8));
    call.style.top = `${top}px`;
    call.style.left = `${left}px`;
  }

  function centreCall(call) {
    call.style.top = `${Math.max(8, window.innerHeight / 2 - call.offsetHeight / 2)}px`;
    call.style.left = `${Math.max(8, window.innerWidth / 2 - call.offsetWidth / 2)}px`;
  }

  function renderTour() {
    const step = tour.steps[tour.index];
    if (!step) return;
    const root = tour.root;
    const last = tour.index === tour.steps.length - 1;

    root.querySelector(".tut-call-count").textContent =
      `${tour.index + 1} of ${tour.steps.length}`;
    root.querySelector(".tut-call-title").textContent = step.title || "";
    root.querySelector(".tut-call-body").textContent = step.body || "";
    root.querySelector(".tut-call-next").textContent = last ? "Done" : "Next";
    const back = root.querySelector(".tut-call-back");
    back.classList.toggle("tut-invisible", tour.index === 0);
    root.querySelector(".tut-dots").innerHTML = tour.steps
      .map((_, i) => `<span class="${i === tour.index ? "is-on" : ""}"></span>`).join("");

    // Scroll first, then place. Placed three times on purpose: immediately so
    // the spotlight is never unpositioned, on the next frame for the layout
    // the scroll caused, and once more shortly after in case the browser
    // throttles rAF (background tabs do) or the scroll was smooth.
    scrollToStep();
    placeTour();
    requestAnimationFrame(placeTour);
    setTimeout(placeTour, 80);
  }

  function stepTour(dir) {
    const n = tour.index + dir;
    if (n < 0) return;
    if (n >= tour.steps.length) { endTour(); return; }
    tour.index = n;
    renderTour();
  }

  function endTour() {
    if (!tour.root) return;
    tour.open = false;
    tour.root.classList.add("hidden");
    window.removeEventListener("resize", placeTour);
    window.removeEventListener("scroll", placeTour, true);
    if (tour.onEnd) { const f = tour.onEnd; tour.onEnd = null; f(); }
  }

  function startTour(steps, opts) {
    const level = state.level || "beginner";
    // Drop steps whose control is not on this screen, and those above the
    // player's level. A tour that points at nothing is worse than none.
    const usable = (steps || [])
      .filter((s) => !s.levels || s.levels.includes(level))
      .filter((s) => visible(s.target));
    if (!usable.length) return false;

    buildTour();
    tour.steps = usable;
    tour.index = 0;
    tour.open = true;
    tour.onEnd = (opts && opts.onEnd) || null;
    tour.root.classList.remove("hidden");
    // Deliberately no body scroll lock here. The card deck locks scrolling, but
    // a tour has to be able to scroll the page to reach a control further down;
    // the scroll listener repositions the spotlight as it goes.
    renderTour();
    tour.root.querySelector(".tut-call").focus();
    window.addEventListener("resize", placeTour);
    window.addEventListener("scroll", placeTour, true);
    return true;
  }

  // Tour keys, kept separate from the card deck's handler.
  document.addEventListener("keydown", (e) => {
    if (!tour.open) return;
    if (e.key === "Enter" || e.key === " " || e.key === "ArrowRight") {
      e.preventDefault(); stepTour(1);
    } else if (e.key === "ArrowLeft" || e.key === "Backspace") {
      e.preventDefault(); stepTour(-1);
    } else if (e.key === "Escape") {
      e.preventDefault(); endTour();
    }
  });

  window.AlphaTutorial = {
    init, open, close, seen,
    tour: startTour,
    endTour,
    level: () => state.level || "beginner",
  };
})();
