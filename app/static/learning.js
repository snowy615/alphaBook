/* Placement quiz + the learning path.
 *
 * One file serves three surfaces, each guarded by the elements it needs:
 *   /welcome  the four-question quiz and its result
 *   /learn    the path, level switcher, and "next step"
 *   anywhere  AlphaLearn.state() for pages that adapt to the player's level
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

  async function api(url, init) {
    const r = await fetch(url, { credentials: "include", ...init });
    const text = await r.text();
    let body = {};
    try { body = text ? JSON.parse(text) : {}; } catch { body = {}; }
    if (!r.ok) {
      const err = new Error(body.detail || `Request failed (${r.status})`);
      err.status = r.status;
      throw err;
    }
    return body;
  }

  const postJSON = (url, payload) => api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });

  // Cached so several widgets on one page share a single request.
  let statePromise = null;
  function state(force) {
    if (force || !statePromise) statePromise = api("/learning/state").catch(() => null);
    return statePromise;
  }

  // ── /welcome: the quiz ────────────────────────────────────────────────
  function initQuiz() {
    const card = $("#wizCard");
    if (!card || !window.QUESTIONS) return;

    const questions = window.QUESTIONS;
    const answers = {};
    let index = 0;

    function render() {
      const q = questions[index];
      $("#wizPrompt").textContent = q.prompt;
      $("#wizHelp").textContent = q.help || "";
      $("#wizStep").textContent = `Question ${index + 1} of ${questions.length}`;

      $("#wizProgress").innerHTML = questions
        .map((_, i) => `<span class="${i < index ? "is-done" : i === index ? "is-on" : ""}"></span>`)
        .join("");

      $("#wizOptions").innerHTML = q.options.map((o) => `
        <button class="wiz-option ${answers[q.key] === o.value ? "is-on" : ""}"
          type="button" data-value="${esc(o.value)}">
          <span class="wiz-option-label">${esc(o.label)}</span>
          <span class="wiz-option-tick" aria-hidden="true">→</span>
        </button>`).join("");

      $("#wizOptions").querySelectorAll("[data-value]").forEach((b) => {
        b.addEventListener("click", () => pick(q.key, b.dataset.value));
      });

      const back = $("#wizBack");
      back.classList.toggle("tut-invisible", index === 0);
      back.disabled = index === 0;
    }

    function pick(key, value) {
      answers[key] = value;
      if (index < questions.length - 1) {
        index += 1;
        render();
      } else {
        submit();
      }
    }

    async function submit() {
      card.classList.add("is-busy");
      try {
        const res = await postJSON("/learning/placement", { answers });
        showResult(res);
      } catch (err) {
        if (err.status === 401) { window.location.href = "/login"; return; }
        $("#wizPrompt").textContent = "Could not save that";
        $("#wizHelp").textContent = err.message;
      } finally {
        card.classList.remove("is-busy");
      }
    }

    function showResult(res) {
      card.classList.add("hidden");
      const box = $("#wizResult");
      box.classList.remove("hidden");

      const p = res.progress || {};
      $("#resLevel").textContent = p.level_label || res.level;
      $("#resWhy").textContent = res.reasons && res.reasons.length
        ? `Because ${res.reasons.join(", ")}.`
        : "";
      $("#resBlurb").textContent = p.level_blurb || "";

      $("#resPath").innerHTML = (p.steps || []).map((s) => `
        <div class="wiz-path-step">
          <span class="wiz-path-num">${s.index}</span>
          <div>
            <strong>${esc(s.title)}</strong>
            <span class="wiz-path-why">${esc(s.why)}</span>
          </div>
          <span class="wiz-path-mins">${esc(s.minutes)}</span>
        </div>`).join("");

      const first = p.next || (p.steps || [])[0];
      if (first) {
        $("#resStart").href = first.href;
        $("#resStart").textContent = `Start: ${first.title}`;
      }
    }

    $("#wizBack").addEventListener("click", () => {
      if (index > 0) { index -= 1; render(); }
    });

    $("#resRetake")?.addEventListener("click", () => {
      index = 0;
      $("#wizResult").classList.add("hidden");
      card.classList.remove("hidden");
      render();
    });

    // Number keys pick an option; the quiz should be finishable without a mouse.
    document.addEventListener("keydown", (e) => {
      if (card.classList.contains("hidden")) return;
      const n = Number(e.key);
      const opts = questions[index].options;
      if (n >= 1 && n <= opts.length) {
        e.preventDefault();
        pick(questions[index].key, opts[n - 1].value);
      } else if (e.key === "Backspace" && index > 0) {
        e.preventDefault(); index -= 1; render();
      }
    });

    render();
  }

  // ── /learn: the path ──────────────────────────────────────────────────
  function stepRow(s) {
    return `
      <div class="learn-step ${s.done ? "is-done" : ""} ${s.is_next ? "is-next" : ""}">
        <span class="learn-step-num">${s.done ? "✓" : s.index}</span>
        <div class="learn-step-body">
          <div class="learn-step-top">
            <strong>${esc(s.title)}</strong>
            <span class="learn-step-mode">${esc(s.label)}</span>
            ${s.is_next ? '<span class="learn-step-flag">Next</span>' : ""}
          </div>
          <p class="learn-step-why">${esc(s.why)}</p>
          <p class="learn-step-teaches"><span>Teaches</span> ${esc(s.teaches)}</p>
        </div>
        <div class="learn-step-go">
          <span class="learn-step-mins">${esc(s.minutes)}</span>
          <a class="btn ${s.is_next ? "primary" : "ghost"}" href="${esc(s.href)}">
            ${s.done ? "Play again" : "Open"}</a>
        </div>
      </div>`;
  }

  async function initLearnPage() {
    const stepsBox = $("#learnSteps");
    if (!stepsBox) return;

    const p = await state(true);
    if (!p) { window.location.href = "/login"; return; }

    const nameEl = $("#userName");
    if (nameEl) {
      api("/me").then((me) => { nameEl.textContent = me.username || "user"; }).catch(() => {});
    }

    function paint(p) {
      $("#levelPick").innerHTML = `
        <span class="learn-level-badge">${esc(p.level_label)}</span>
        <span class="learn-level-prog">${p.done} of ${p.total} done</span>
        <div class="learn-bar"><div class="learn-bar-fill" style="width:${p.pct}%"></div></div>`;

      const next = $("#learnNext");
      if (p.complete) {
        next.classList.remove("hidden");
        next.innerHTML = `
          <div>
            <span class="learn-next-eyebrow">Path complete</span>
            <strong>You've finished every step at ${esc(p.level_label)}.</strong>
            <p>${p.suggest_label
              ? `Move up to ${esc(p.suggest_label)} for the harder version.`
              : "You're at the top level — keep competing."}</p>
          </div>
          ${p.suggest_level
            ? `<button class="btn primary learn-switch" data-level="${esc(p.suggest_level)}"
                 type="button">Move to ${esc(p.suggest_label)}</button>`
            : `<a class="btn primary" href="/competitions">Find a competition</a>`}`;
      } else if (p.next) {
        next.classList.remove("hidden");
        next.innerHTML = `
          <div>
            <span class="learn-next-eyebrow">Do this next</span>
            <strong>${esc(p.next.title)}</strong>
            <p>${esc(p.next.why)}</p>
          </div>
          <a class="btn primary" href="${esc(p.next.href)}">Open ${esc(p.next.label)}</a>`;
      } else {
        next.classList.add("hidden");
      }

      stepsBox.innerHTML = p.steps.map(stepRow).join("");

      document.querySelectorAll(".learn-tier").forEach((el) => {
        el.classList.toggle("is-on", el.dataset.level === p.level);
      });
      document.querySelectorAll(".learn-switch").forEach((b) => {
        b.disabled = b.dataset.level === p.level;
        b.onclick = async () => {
          b.disabled = true;
          try {
            const res = await postJSON("/learning/level", { level: b.dataset.level });
            statePromise = Promise.resolve(res.progress);
            paint(res.progress);
            window.scrollTo({ top: 0, behavior: "smooth" });
          } catch { b.disabled = false; }
        };
      });
    }

    paint(p);
  }

  // ── Anywhere: a compact "next step" strip ─────────────────────────────
  // Rendered into #nextStep on the landing page when a player is signed in.
  async function initNextStrip() {
    const host = $("#nextStep");
    if (!host) return;
    const p = await state();
    if (!p) return;

    if (!p.placed) {
      host.classList.remove("hidden");
      host.innerHTML = `
        <div class="next-strip">
          <div>
            <span class="next-eyebrow">Start here</span>
            <strong>Answer four questions and we'll pick your path</strong>
            <p>Nine modes is a lot. This narrows it to the three or four that suit you.</p>
          </div>
          <a class="btn primary" href="/welcome">Find my level</a>
        </div>`;
      return;
    }

    host.classList.remove("hidden");
    const body = p.complete
      ? `<strong>Path complete at ${esc(p.level_label)}</strong>
         <p>${p.suggest_label ? `Move up to ${esc(p.suggest_label)}.` : "Keep competing."}</p>`
      : `<strong>${esc(p.next.title)}</strong><p>${esc(p.next.why)}</p>`;

    host.innerHTML = `
      <div class="next-strip">
        <div>
          <span class="next-eyebrow">${esc(p.level_label)} · step ${p.done + 1} of ${p.total}</span>
          ${body}
        </div>
        <div class="next-actions">
          ${p.next ? `<a class="btn primary" href="${esc(p.next.href)}">Open ${esc(p.next.label)}</a>` : ""}
          <a class="btn ghost" href="/learn">Full path</a>
        </div>
        <div class="learn-bar next-bar"><div class="learn-bar-fill" style="width:${p.pct}%"></div></div>
      </div>`;
  }

  window.AlphaLearn = { state, api };

  function boot() {
    initQuiz();
    initLearnPage();
    initNextStrip();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
