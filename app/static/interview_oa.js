/* Interview OA — one candidate, one attempt, one question at a time.
 *
 * The countdown shown here is cosmetic: the server stamps when a question
 * was served and is the only clock that actually matters. This file polls
 * `/interview-oa/state` every second to stay in sync (and to pick up a
 * server-side timeout the candidate's own client might have missed — a
 * closed tab, a stalled network call), and also runs a local 250ms ticker
 * so the number on screen counts down smoothly between polls. When the
 * local ticker hits zero it fires one auto-submit of whatever's currently
 * typed, exactly like pressing Submit — the server is the one that decides
 * whether that arrived in time.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  async function api(url, body) {
    const opts = { credentials: "include" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(url, opts);
    const txt = await r.text();
    let data = {};
    try { data = txt ? JSON.parse(txt) : {}; } catch { /* non-JSON */ }
    if (!r.ok) {
      const e = new Error(data.detail || `HTTP ${r.status}`);
      e.status = r.status;
      throw e;
    }
    return data;
  }

  function flash(text, bad) {
    const box = $("#msg");
    if (!box) return;
    box.textContent = text;
    box.className = "msp-msg msp-msg-" + (bad ? "error" : "ok");
    box.classList.remove("hidden");
    clearTimeout(flash.t);
    flash.t = setTimeout(() => box.classList.add("hidden"), 4200);
  }

  let pollTimer = null;
  let tickTimer = null;
  let currentIndex = -1;
  let localSecondsLeft = 0;
  let autoSubmittedIndex = -1;
  let submitting = false;

  async function poll() {
    try {
      const state = await api("/interview-oa/state");
      render(state);
    } catch (err) {
      if (err.status === 401) { window.location.href = "/login"; return; }
      flash(err.message, true);
    }
  }

  function renderLocked() {
    $("#app").innerHTML = `
      <div class="card msp-panel">
        <div class="head"><h3>Enter the access code</h3></div>
        <div class="msp-panel-body">
          <p class="msp-muted" style="margin-top:0;">
            ${window.QUESTIONS_PER_SESSION} questions, ${window.TIME_PER_QUESTION} seconds each,
            one attempt. Once you start, don't leave the page.
          </p>
          <label class="msp-label" for="pwInput">Access code</label>
          <input id="pwInput" class="msp-input" type="password" autocomplete="off">
          <button class="btn primary" id="unlockBtn" style="margin-top:10px;">Unlock</button>
        </div>
      </div>`;
    $("#unlockBtn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        await api("/interview-oa/unlock", { password: $("#pwInput").value });
        await poll();
      } catch (err) {
        flash(err.status === 401 ? "Please log in first." : err.message, true);
        e.target.disabled = false;
      }
    });
    $("#pwInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#unlockBtn").click(); });
  }

  function renderReady() {
    $("#app").innerHTML = `
      <div class="card msp-panel">
        <div class="head"><h3>Ready when you are</h3></div>
        <div class="msp-panel-body">
          <p class="msp-muted" style="margin-top:0;">
            You'll get ${window.QUESTIONS_PER_SESSION} probability and expectation questions,
            one at a time, ${window.TIME_PER_QUESTION} seconds on the clock for each. The timer
            keeps running whether you answer or not, and this is a one-shot attempt.
          </p>
          <button class="btn primary" id="startBtn">Start</button>
        </div>
      </div>`;
    $("#startBtn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { await api("/interview-oa/start", {}); await poll(); }
      catch (err) { flash(err.message, true); e.target.disabled = false; }
    });
  }

  function renderFinished(state) {
    stopTicking();
    $("#app").innerHTML = `
      <div class="card msp-panel">
        <div class="head"><h3>Submitted</h3></div>
        <div class="msp-panel-body">
          <p>${esc(state.message || "Thanks — your responses have been submitted.")}</p>
        </div>
      </div>`;
  }

  function renderActive(q) {
    if (q.index !== currentIndex) {
      currentIndex = q.index;
      autoSubmittedIndex = -1;
      localSecondsLeft = q.seconds_left;
      $("#app").innerHTML = `
        <div class="card msp-panel">
          <div class="head">
            <h3>Question ${q.index + 1} of ${q.total}</h3>
            <span class="msp-muted">${esc(q.kind)}</span>
          </div>
          <div class="msp-panel-body">
            <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px;">
              <span class="msp-muted">Time left</span>
              <strong id="clockN" style="font-size:22px; font-variant-numeric:tabular-nums;">${Math.ceil(localSecondsLeft)}s</strong>
            </div>
            <p style="font-size:16px;">${esc(q.prompt)}</p>
            <input id="ansInput" class="msp-input" type="text" inputmode="decimal"
                   placeholder="e.g. 0.375 or 3/8" autocomplete="off">
            <button class="btn primary" id="submitBtn" style="margin-top:10px;">Submit</button>
          </div>
        </div>`;
      $("#submitBtn").addEventListener("click", () => submitAnswer(false));
      $("#ansInput").addEventListener("keydown", (e) => { if (e.key === "Enter") submitAnswer(false); });
      $("#ansInput").focus();
      startTicking();
    } else {
      // Resync the visible clock from the server without disturbing the input.
      localSecondsLeft = Math.max(localSecondsLeft, q.seconds_left);
    }
  }

  function startTicking() {
    stopTicking();
    tickTimer = setInterval(() => {
      localSecondsLeft = Math.max(0, localSecondsLeft - 0.25);
      const n = $("#clockN");
      if (n) n.textContent = Math.ceil(localSecondsLeft) + "s";
      if (localSecondsLeft <= 0 && autoSubmittedIndex !== currentIndex) {
        autoSubmittedIndex = currentIndex;
        submitAnswer(true);
      }
    }, 250);
  }

  function stopTicking() {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  }

  async function submitAnswer(auto) {
    if (submitting) return;
    submitting = true;
    const btn = $("#submitBtn");
    if (btn) btn.disabled = true;
    const value = ($("#ansInput") && $("#ansInput").value) || "";
    try {
      await api("/interview-oa/answer", { index: currentIndex, value });
    } catch { /* the next poll reconciles either way */ }
    submitting = false;
    await poll();
  }

  function render(state) {
    if (state.status === "locked") { renderLocked(); return; }
    if (state.status === "ready") { renderReady(); return; }
    if (state.status === "finished") { renderFinished(state); return; }
    if (state.status === "active" && state.question) { renderActive(state.question); return; }
  }

  (async () => {
    try {
      const me = await api("/me");
      const el = $("#userName");
      if (el) el.textContent = me.username || "user";
    } catch (err) {
      if (err.status === 401) { window.location.href = "/login"; return; }
    }
    await poll();
    pollTimer = setInterval(poll, 1000);
  })();
})();
