/* Level 1 · Easy — a guided, paced first trade on the real order book.
 *
 * tutorial.js already ships a one-shot "how to play" deck plus an anchored
 * screen tour, and neither is touched here — this is a different thing. That
 * system is a quick orientation you can click through in five seconds; this
 * one is a *level*: it forces a beat of thinking time on every step (the
 * Next button is disabled for a moment, not just click-to-skip), and for the
 * two steps that matter most — opening a ticket, submitting an order — it
 * will not advance until the player actually does the real thing on the
 * real button. That is the Cuphead-dojo / Zelda-tutorial-room idea: tell,
 * then immediately make them do it themselves, on the exact control they'll
 * use for real once this is over.
 *
 * It never touches the trading interface itself. Every element it points at
 * (#open-order, .ladder, #position-summary, ...) already exists on the page;
 * this file only reads them and draws on top. It boots itself — nothing else
 * needs to call into it — when the page sets window.TUTORIAL_MODE = true,
 * which only happens for /trade/{symbol}?tutorial=1.
 */
(function () {
  "use strict";

  if (!window.TUTORIAL_MODE) return;

  const SYMBOL = window.SYMBOL || "";
  const STORE_KEY = "ab-tut-level-easy-v1";

  // ── tiny DOM helpers ──────────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const authed = () => {
    const box = document.getElementById("userBox");
    return !!box && !box.classList.contains("hidden");
  };
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };

  function waitFor(check, { timeout = 8000, interval = 250 } = {}) {
    return new Promise((resolve) => {
      const start = Date.now();
      (function poll() {
        let ok = false;
        try { ok = !!check(); } catch { ok = false; }
        if (ok) return resolve(true);
        if (Date.now() - start >= timeout) return resolve(false);
        setTimeout(poll, interval);
      })();
    });
  }

  // ── injected styles (scoped under .tl-, reusing the site's own tokens) ──
  const STYLE = `
    .tl-badge {
      position: fixed; top: 14px; left: 50%; transform: translateX(-50%);
      z-index: 96; display: flex; align-items: center; gap: 8px;
      padding: 7px 14px; background: var(--panel);
      border: 1px solid var(--border-strong); color: var(--text);
      font-size: 12.5px; font-weight: 650;
      animation: tlSlideDown .32s cubic-bezier(.2,.8,.3,1.15) both;
    }
    .tl-badge .tl-dot {
      width: 7px; height: 7px; border-radius: 50%; background: var(--brand);
      box-shadow: 0 0 0 0 var(--brand-wash);
      animation: tlDotPulse 1.8s ease-out infinite;
    }
    .tl-badge b { color: var(--brand); }
    .tl-badge a { color: var(--muted); text-decoration: none; margin-left: 6px; }
    .tl-badge a:hover { color: var(--text); text-decoration: underline; }

    /* .tl-layer itself must never intercept a click: it's a fixed,
       full-viewport container, and a parent box claims hit-testing over its
       whole area regardless of what pointer-events its children declare.
       Only .tl-scrim and .tl-card opt back in — that is what actually lets
       an action step's spotlighted click reach the real button underneath
       once .tl-scrim's own pointer-events is switched off for that step. */
    .tl-layer { position: fixed; inset: 0; z-index: 94; pointer-events: none; }
    .tl-layer.hidden { display: none; }
    .tl-scrim {
      position: fixed; inset: 0; background: rgba(4, 8, 15, .72);
      pointer-events: auto;
      animation: tlFadeIn .22s ease both;
    }
    :root[data-theme="light"] .tl-scrim { background: rgba(16, 28, 51, .48); }

    .tl-spot {
      position: fixed; border: 1px solid var(--brand); pointer-events: none;
      box-shadow: 0 0 0 9999px rgba(4, 8, 15, .72);
      transition: top .2s ease, left .2s ease, width .2s ease, height .2s ease;
    }
    :root[data-theme="light"] .tl-spot { box-shadow: 0 0 0 9999px rgba(16, 28, 51, .48); }
    .tl-spot.tl-action { animation: tlPulseGlow 1.6s ease-in-out infinite; }

    .tl-card {
      position: fixed; width: min(360px, calc(100vw - 24px));
      padding: 18px 20px 16px; background: var(--panel);
      border: 1px solid var(--border-strong); outline: none;
      pointer-events: auto;
      animation: tlPopIn .3s cubic-bezier(.2,.85,.3,1.2) both;
      transition: top .2s ease, left .2s ease;
    }
    .tl-card.tl-centered { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); }
    .tl-eyebrow {
      font-size: 10.5px; font-weight: 700; text-transform: uppercase;
      letter-spacing: .09em; color: var(--brand); margin-bottom: 8px;
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
    }
    .tl-title { margin: 0 0 6px; font-size: 16.5px; font-weight: 650; line-height: 1.3; }
    .tl-body { margin: 0; font-size: 13px; line-height: 1.6; color: var(--muted); }
    .tl-body strong { color: var(--text); }
    .tl-metric {
      margin-top: 12px; padding: 8px 10px; background: var(--panel-2);
      border: 1px solid var(--border); display: flex; align-items: baseline;
      justify-content: space-between; gap: 10px; font-size: 12px;
    }
    .tl-metric b { font-size: 13.5px; color: var(--text); font-variant-numeric: tabular-nums; }
    .tl-waiting {
      margin-top: 12px; font-size: 11.5px; color: var(--brand);
      display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
    }
    .tl-waiting .tl-chevron { animation: tlNudge 1.1s ease-in-out infinite; }
    .tl-continue {
      margin-left: auto; background: none; border: none; cursor: pointer;
      color: var(--muted); font-size: 11.5px; text-decoration: underline;
      padding: 2px 0; animation: tlFadeIn .25s ease both;
    }
    .tl-continue:hover { color: var(--text); }
    .tl-foot {
      display: flex; align-items: center; justify-content: space-between;
      margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border);
      gap: 12px;
    }
    .tl-dots { display: flex; gap: 5px; margin-right: auto; }
    .tl-dots span { width: 5px; height: 5px; border-radius: 50%; background: var(--border-strong); }
    .tl-dots span.on { background: var(--brand); }
    .tl-next {
      position: relative; overflow: hidden;
    }
    .tl-next .tl-gate {
      position: absolute; inset: 0; background: var(--brand-deep);
      transform-origin: left; transform: scaleX(1);
      transition: transform linear;
    }
    .tl-skip { background: none; border: none; color: var(--muted); font-size: 12px; cursor: pointer; padding: 4px 0; }
    .tl-skip:hover { color: var(--text); }

    .tl-toast {
      position: fixed; bottom: 18px; right: 18px; z-index: 96;
      width: min(300px, calc(100vw - 32px)); padding: 12px 14px;
      background: var(--panel); border: 1px solid var(--border-strong);
      font-size: 12.5px; line-height: 1.5; color: var(--text);
      animation: tlSlideUp .28s cubic-bezier(.2,.85,.3,1.2) both;
    }
    .tl-toast.tl-out { animation: tlFadeOut .25s ease both; }
    .tl-toast b { color: var(--green); }

    @keyframes tlFadeIn { from { opacity: 0; } to { opacity: 1; } }
    @keyframes tlFadeOut { from { opacity: 1; } to { opacity: 0; } }
    @keyframes tlPopIn {
      from { opacity: 0; transform: scale(.93) translateY(6px); }
      to   { opacity: 1; transform: none; }
    }
    .tl-card.tl-centered { animation-name: tlPopInCentered; }
    @keyframes tlPopInCentered {
      from { opacity: 0; transform: translate(-50%, -50%) scale(.93); }
      to   { opacity: 1; transform: translate(-50%, -50%) scale(1); }
    }
    @keyframes tlSlideDown { from { opacity: 0; transform: translate(-50%, -14px); } to { opacity: 1; transform: translate(-50%, 0); } }
    @keyframes tlSlideUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    @keyframes tlDotPulse {
      0% { box-shadow: 0 0 0 0 var(--brand-wash); }
      70% { box-shadow: 0 0 0 6px transparent; }
      100% { box-shadow: 0 0 0 0 transparent; }
    }
    @keyframes tlPulseGlow {
      0%, 100% { box-shadow: 0 0 0 9999px rgba(4, 8, 15, .72), 0 0 0 0 var(--brand-wash); }
      50% { box-shadow: 0 0 0 9999px rgba(4, 8, 15, .72), 0 0 0 8px var(--brand-wash); }
    }
    @keyframes tlNudge { 0%, 100% { transform: translateX(0); } 50% { transform: translateX(4px); } }
    @media (max-width: 560px) {
      .tl-card { padding: 15px 16px 14px; }
      .tl-title { font-size: 15px; }
    }
    /* Every other animated surface built this pass (toasts, rank flash,
       clock pulse) already respects this; the tutorial was the one thing
       still looping decorative motion unconditionally. */
    @media (prefers-reduced-motion: reduce) {
      .tl-badge, .tl-scrim, .tl-card, .tl-continue, .tl-toast, .tl-toast.tl-out {
        animation: none;
      }
      .tl-badge .tl-dot, .tl-spot.tl-action, .tl-waiting .tl-chevron {
        animation: none;
      }
      .tl-spot, .tl-card {
        transition: none;
      }
    }
  `;

  function injectStyle() {
    const tag = document.createElement("style");
    tag.id = "tl-style";
    tag.textContent = STYLE;
    document.head.appendChild(tag);
  }

  // ── badge ─────────────────────────────────────────────────────────────
  function mountBadge() {
    const badge = document.createElement("div");
    badge.className = "tl-badge";
    badge.innerHTML = `
      <span class="tl-dot"></span>
      <span>Tutorial Level 1 · <b>Easy</b></span>
      <a href="/market">Exit</a>`;
    document.body.appendChild(badge);
  }

  // ── the paced, spotlighted layer ─────────────────────────────────────
  const layer = { root: null, spot: null, card: null };

  function mountLayer() {
    const root = document.createElement("div");
    root.className = "tl-layer hidden";
    root.innerHTML = `
      <div class="tl-scrim"></div>
      <div class="tl-spot" style="display:none;"></div>
      <div class="tl-card" tabindex="-1">
        <div class="tl-eyebrow"><span class="tl-count"></span><button class="tl-skip" type="button">Skip tutorial</button></div>
        <h3 class="tl-title"></h3>
        <p class="tl-body"></p>
        <div class="tl-metric" style="display:none;"></div>
        <div class="tl-waiting" style="display:none;">
          <span class="tl-waiting-text">Go ahead —</span><span class="tl-chevron">›</span>
          <button class="tl-continue" type="button" style="display:none;">Already done that — continue</button>
        </div>
        <div class="tl-foot">
          <div class="tl-dots"></div>
          <button class="btn primary tl-next" type="button"><span class="tl-gate"></span><span class="tl-label">Next</span></button>
        </div>
      </div>`;
    document.body.appendChild(root);
    layer.root = root;
    layer.spot = root.querySelector(".tl-spot");
    layer.card = root.querySelector(".tl-card");
    layer.scrim = root.querySelector(".tl-scrim");
    root.querySelector(".tl-skip").addEventListener("click", finish);
    root.querySelector(".tl-continue").addEventListener("click", (e) => {
      e.stopPropagation();
      advance();
    });
    root.querySelector(".tl-next").addEventListener("click", () => {
      if (state.gated) return;
      advance();
    });
    window.addEventListener("resize", placeCard);
    window.addEventListener("scroll", placeCard, true);
    return root;
  }

  // ── step sequence ─────────────────────────────────────────────────────
  const state = { steps: [], index: 0, gated: true, cleanupStep: null };

  function buildSteps() {
    const canAct = authed();
    const headSel = ".order-book-card .head";
    const ladderSel = `#ladder-${SYMBOL}`;
    const steps = [
      {
        title: "Welcome to the desk",
        body: "This is <strong>Level 1 · Easy</strong> — a paced first lap around the real order book. Balances are virtual, so nothing here is risking real money. Each step gives you a moment before you can move on, so take it slow.",
        dwell: 1600,
      },
      {
        target: headSel,
        title: "Last price",
        body: `This is the last traded price for <strong>${SYMBOL || "this name"}</strong> — the number that updates as trades print. It's the first thing to glance at before you do anything else.`,
        dwell: 1400,
        ready: () => visible($(headSel)),
      },
      {
        target: ladderSel,
        title: "The order book",
        body: "Asks (sellers) sit above the middle row, bids (buyers) below. Each row is real resting size at that price. The middle row shows the <strong>spread</strong> — the gap between the best ask and best bid — the tighter it is, the cheaper it is to trade right now.",
        dwell: 1600,
        ready: () => {
          const body = document.getElementById(`ladder-body-${SYMBOL}`);
          return !!body && body.children.length > 0;
        },
      },
      {
        target: "#position-summary",
        title: "Your position",
        body: "How many shares you're holding. Green means you're long (you own it), red means you're short (you'd profit if it falls). Right now it reads flat — that's about to change.",
        dwell: 1400,
      },
    ];

    if (canAct) {
      steps.push({
        target: "#open-order",
        title: "Open a ticket",
        body: "This is the one button you actually need: it opens the form for placing an order. Go ahead and click it.",
        action: { kind: "click", selector: "#open-order" },
      });
      steps.push({
        target: "#order-modal .panel-body",
        title: "Set your price and size",
        body: "Buy or sell, a price, and a quantity. The price field starts prefilled at the last traded price — leave it there and you'll likely trade immediately; move it away from the market and it rests on the book until someone trades against it, exactly like the rows you just saw.",
        dwell: 1800,
        ready: () => visible($("#order-modal")) && $("#order-modal").open,
        onSkip: () => { try { $("#order-modal").close(); } catch { /* noop */ } },
      });
      steps.push({
        target: "#order-form .btn.primary",
        title: "Submit it",
        body: "This sends a real (practice) order to the book. If your price crosses the spread you'll trade instantly; otherwise it waits its turn.",
        action: { kind: "submit", selector: "#order-form" },
        onSkip: () => { try { $("#order-modal").close(); } catch { /* noop */ } },
      });
    } else {
      steps.push({
        target: "#open-order",
        title: "Open a ticket",
        body: "This button opens the order form — price, quantity, buy or sell. You'll need a free account to actually submit one; sign up any time and come back to try it for real.",
        dwell: 1800,
        cta: { label: "Sign up free", href: "/signup" },
      });
    }

    steps.push({
      target: "#my-orders-card",
      title: "Working orders",
      body: "Anything you've placed that hasn't traded yet lives here, with a cancel button for each one. Check back here after placing an order that didn't fill immediately.",
      dwell: 1400,
    });
    steps.push({
      target: "#recent-trades-card",
      title: "The tape",
      body: "Every trade that just printed, newest first — including the market-maker bot's. Watching this is how you learn where the real action is.",
      dwell: 1400,
    });
    steps.push({
      title: "That's the whole board",
      body: "Price, book, position, orders, tape — five things, always visible, always worth a glance. You're ready for the live room and the harder levels from here.",
      dwell: 1200,
      final: true,
    });

    return steps;
  }

  // ── placement ─────────────────────────────────────────────────────────
  function placeCard() {
    const step = state.steps[state.index];
    if (!step) return;
    const card = layer.card;
    const spot = layer.spot;
    const scrim = layer.scrim;
    const el = step.target ? $(step.target) : null;

    if (!el || !visible(el)) {
      spot.style.display = "none";
      scrim.style.background = ""; // no spotlight hole to dim around — scrim covers the whole screen
      card.classList.add("tl-centered");
      card.style.top = ""; card.style.left = "";
      return;
    }

    // A native <dialog>.showModal() always paints in the browser's "top
    // layer" — above every other fixed-position element on the page, no
    // matter its z-index. A spotlight border behind the order ticket would
    // just be invisible, and the card would render half-hidden under it. The
    // dialog is already the obvious focal point on its own, so skip the
    // spotlight and place the card against the *dialog's* rect instead of
    // the specific field inside it — same below-else-above logic as normal,
    // just aimed at the box that's actually going to be on top.
    const dialogEl = el.closest("dialog[open]");
    card.classList.remove("tl-centered");
    const r = dialogEl ? dialogEl.getBoundingClientRect() : el.getBoundingClientRect();

    if (dialogEl) {
      spot.style.display = "none";
      scrim.style.background = ""; // nothing punching a hole here — let the scrim dim normally
    } else {
      const pad = 6;
      spot.style.display = "block";
      // .tl-spot's own box-shadow already dims the whole viewport outside its
      // rect — that's the actual spotlight hole. If .tl-scrim also keeps its
      // flat background on top of that, the "highlighted" area gets dimmed by
      // the scrim too, just with one less layer than the rest — it reads as
      // highlighted only by comparison, never as genuinely full-brightness.
      // Turning the scrim's own fill off here removes that double layer.
      scrim.style.background = "transparent";
      spot.classList.toggle("tl-action", !!step.action);
      spot.style.top = `${r.top - pad}px`;
      spot.style.left = `${r.left - pad}px`;
      spot.style.width = `${r.width + pad * 2}px`;
      spot.style.height = `${r.height + pad * 2}px`;
    }

    const cw = card.offsetWidth || 340;
    const ch = card.offsetHeight || 180;
    const gap = dialogEl ? 20 : 14;
    let top = r.bottom + gap;
    if (top + ch > window.innerHeight - 8) top = Math.max(8, r.top - ch - gap);
    let left = r.left + r.width / 2 - cw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - cw - 8));
    card.style.top = `${top}px`;
    card.style.left = `${left}px`;
  }

  function scrollToStep(step) {
    if (!step || !step.target) return;
    const el = $(step.target);
    if (!el) return;
    const r = el.getBoundingClientRect();
    if (r.top < 60 || r.bottom > window.innerHeight - 20) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  // ── the dwell gate: Next stays disabled for step.dwell ms ──────────────
  function armGate(ms) {
    const btn = layer.root.querySelector(".tl-next");
    const gateEl = btn.querySelector(".tl-gate");
    state.gated = ms > 0;
    btn.disabled = state.gated;
    gateEl.style.transition = "none";
    gateEl.style.transform = "scaleX(1)";
    if (!state.gated) return;
    requestAnimationFrame(() => {
      gateEl.style.transition = `transform ${ms}ms linear`;
      gateEl.style.transform = "scaleX(0)";
    });
    setTimeout(() => { state.gated = false; btn.disabled = false; }, ms);
  }

  // ── action-required steps: wait for the real event, then advance ──────
  // If the target control becomes unreachable mid-step — the order modal
  // this step wants clicked was already closed by an earlier submit, say —
  // there is otherwise no way forward: no Next button, and a listener that
  // never fires. A "continue anyway" fallback appears after a few seconds
  // so a step can never be a dead end.
  function armAction(action) {
    const target = document.querySelector(action.selector);
    if (!target) { advance(); return () => {}; }

    const continueBtn = layer.root.querySelector(".tl-continue");
    const revealTimer = setTimeout(() => {
      continueBtn.style.display = "inline-block";
    }, 4500);

    let done = false;
    const handler = () => {
      if (done) return;
      done = true;
      clearTimeout(revealTimer);
      window.removeEventListener(action.kind, onDoc, true);
      setTimeout(advance, action.kind === "submit" ? 750 : 260);
    };
    // Capture-phase document listener: purely observational, never calls
    // preventDefault/stopPropagation, so trading.js's own handlers on the
    // same elements run exactly as they always do.
    function onDoc(e) {
      if (e.target.closest && e.target.closest(action.selector)) handler();
    }
    window.addEventListener(action.kind, onDoc, true);
    return () => {
      done = true;
      clearTimeout(revealTimer);
      continueBtn.style.display = "none";
      window.removeEventListener(action.kind, onDoc, true);
    };
  }

  // ── render one step ───────────────────────────────────────────────────
  async function renderStep() {
    if (state.cleanupStep) { state.cleanupStep(); state.cleanupStep = null; }

    const step = state.steps[state.index];
    if (!step) return finish();

    if (step.ready) await waitFor(step.ready, { timeout: 6000 });
    scrollToStep(step);

    const root = layer.root;
    root.querySelector(".tl-count").textContent = `Step ${state.index + 1} of ${state.steps.length}`;
    root.querySelector(".tl-title").textContent = step.title || "";
    root.querySelector(".tl-body").innerHTML = step.body || "";
    root.querySelector(".tl-dots").innerHTML = state.steps
      .map((_, i) => `<span class="${i === state.index ? "on" : ""}"></span>`).join("");

    const metricEl = root.querySelector(".tl-metric");
    metricEl.style.display = "none";

    const waitingEl = root.querySelector(".tl-waiting");
    const nextBtn = root.querySelector(".tl-next");
    const nextLabel = nextBtn.querySelector(".tl-label");

    // Remove any previous CTA link we might have added.
    const oldCta = root.querySelector(".tl-cta");
    if (oldCta) oldCta.remove();
    if (step.cta) {
      const a = document.createElement("a");
      a.className = "btn ghost tl-cta";
      a.style.marginTop = "10px";
      a.style.display = "inline-block";
      a.href = step.cta.href;
      a.textContent = step.cta.label;
      root.querySelector(".tl-body").insertAdjacentElement("afterend", a);
    }

    // Re-run the pop-in animation on every step so it always reads as a
    // fresh, deliberate beat rather than a page that just changed text.
    layer.card.style.animation = "none";
    void layer.card.offsetWidth;
    layer.card.style.animation = "";

    // Placed three times on purpose: immediately so the card is never left
    // unpositioned, on the next frame for the layout the text/animation
    // reset just caused, and once more shortly after in case rAF gets
    // throttled (background tabs do that) before offsetHeight has settled.
    placeCard();
    requestAnimationFrame(placeCard);
    setTimeout(placeCard, 80);

    // .tl-scrim is a fixed, full-viewport div — the "hole" the spotlight
    // shows is only a box-shadow illusion on .tl-spot, so the scrim still
    // sits, at full size, in front of the real page and swallows every
    // click by default. That's exactly what an explain step wants (nothing
    // underneath is reachable while it's up), but an action step needs the
    // opposite: the real control has to be genuinely clickable.
    const scrimEl = root.querySelector(".tl-scrim");
    if (step.action) {
      scrimEl.style.pointerEvents = "none";
      waitingEl.style.display = "flex";
      nextBtn.style.display = "none";
      state.cleanupStep = armAction(step.action);
    } else {
      scrimEl.style.pointerEvents = "";
      waitingEl.style.display = "none";
      nextBtn.style.display = "inline-flex";
      nextLabel.textContent = step.final ? "Done" : "Next";
      armGate(step.dwell || 0);
      state.cleanupStep = null;
    }
  }

  // renderStep() can await a step's `ready` check before it re-arms the gate,
  // and a step's own advance (a real click or submit) can race a queued
  // double-fire of the same event. Re-gate synchronously, right here, so a
  // second advance() during that window is a no-op instead of skipping a
  // step the player never actually saw.
  let advancing = false;
  function advance() {
    if (advancing) return;
    advancing = true;
    state.gated = true;
    const btn = layer.root && layer.root.querySelector(".tl-next");
    if (btn) btn.disabled = true;

    const step = state.steps[state.index];
    if (step && step.final) { finish(); advancing = false; return; }
    state.index += 1;
    if (state.index >= state.steps.length) { finish(); advancing = false; return; }
    renderStep().then(() => { advancing = false; });
  }

  function finish() {
    if (state.cleanupStep) { state.cleanupStep(); state.cleanupStep = null; }
    const step = state.steps[state.index];
    if (step && step.onSkip) step.onSkip();
    if (layer.root) layer.root.classList.add("hidden");
    window.removeEventListener("resize", placeCard);
    window.removeEventListener("scroll", placeCard, true);
    try { localStorage.setItem(STORE_KEY, "done"); } catch { /* private mode */ }
    watchForFirstFill();
  }

  // trading.js resolves login state asynchronously (Firebase), and toggles
  // exactly one of #loginBox / #userBox out of "hidden" once it does. Both
  // start hidden, so "settled" means at least one of them no longer is.
  // Building the step list before that resolves risks reading a stale
  // "logged out" and handing a signed-in player the sign-up fallback.
  function authSettled() {
    const login = document.getElementById("loginBox");
    const user = document.getElementById("userBox");
    const loginShown = !!login && !login.classList.contains("hidden");
    const userShown = !!user && !user.classList.contains("hidden");
    return loginShown || userShown;
  }

  async function start() {
    injectStyle();
    mountBadge();
    mountLayer();
    await waitFor(authSettled, { timeout: 3000, interval: 120 });
    state.steps = buildSteps();
    state.index = 0;
    layer.root.classList.remove("hidden");
    renderStep();
    layer.card.focus();
  }

  // ── after the guided sequence: one non-blocking "you got a fill" toast ─
  function watchForFirstFill() {
    try { if (sessionStorage.getItem("ab-tut-fill-toast")) return; } catch { /* ignore */ }
    const list = document.getElementById("recent-trades-list");
    if (!list) return;
    const obs = new MutationObserver(() => {
      obs.disconnect();
      try { sessionStorage.setItem("ab-tut-fill-toast", "1"); } catch { /* ignore */ }
      toast("That's a fill. Watch <b>Recent Trades</b> — every print, yours and everyone else's, shows up here the instant it happens.");
    });
    obs.observe(list, { childList: true });
    // Stop watching once the tutorial page is left; nothing to clean up
    // otherwise since a disconnected observer is inert.
  }

  function toast(html) {
    const el = document.createElement("div");
    el.className = "tl-toast";
    el.innerHTML = html;
    document.body.appendChild(el);
    setTimeout(() => {
      el.classList.add("tl-out");
      setTimeout(() => el.remove(), 300);
    }, 5000);
  }

  function boot() {
    start();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
