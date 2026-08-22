/* Dark Pool — desk client.
 *
 * Two pages share this file: the rules page (create/join) and the desk
 * itself. The desk polls once a second. The one thing that deliberately does
 * *not* redraw on every poll is the calibration checkpoint form: once it's
 * showing, later polls that report the same still-unanswered decision are
 * ignored, so a slider mid-drag never gets yanked out from under the player.
 * The server is still the clock — chip movement, street reveals, and
 * showdowns all come from what it reports.
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

  const chip = (n) => `<span class="tfl-chip">$${n}</span>`;

  // ── Rules page ────────────────────────────────────────────────────────
  function initRulesPage() {
    $("#createBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const res = await api("/dark-pool/create", {});
        window.location.href = `/dark-pool/game/${res.game_id}`;
      } catch (err) {
        flash(err.status === 401 ? "Please log in first." : err.message, true);
        e.target.disabled = false;
      }
    });

    const join = async () => {
      const code = ($("#joinCode")?.value || "").trim().toUpperCase();
      if (code.length !== 6) { flash("Join codes are six characters.", true); return; }
      try {
        const res = await api("/dark-pool/join", { join_code: code });
        window.location.href = `/dark-pool/game/${res.game_id}`;
      } catch (err) {
        flash(err.status === 401 ? "Please log in first." : err.message, true);
      }
    };
    $("#joinBtn")?.addEventListener("click", join);
    $("#joinCode")?.addEventListener("keydown", (e) => { if (e.key === "Enter") join(); });
  }

  // ── Desk ──────────────────────────────────────────────────────────────
  function initTable(gameId) {
    let state = null;
    let raiseAmt = 0;

    // Local, in-progress checkpoint form values — reset only when a new
    // decision shows up, never on an ordinary poll tick.
    let activeCpDecisionNo = null;
    let cpProb = 50, cpLow = 25, cpHigh = 75, cpEv = 0;

    async function poll() {
      try {
        state = await api(`/dark-pool/game/${gameId}/state`);
        render();
      } catch (err) {
        if (err.status === 401) window.location.href = "/login";
      }
    }

    const myUserId = () => (state.desks.find((d) => d.is_me) || {}).user_id;

    // ---- pieces ----
    function seat(d) {
      const flags = [
        d.is_turn ? '<span class="dpk-badge dpk-badge-turn">to act</span>' : "",
        d.folded ? '<span class="dpk-badge dpk-badge-out">folded</span>' : "",
        d.all_in ? '<span class="dpk-badge dpk-badge-allin">all-in</span>' : "",
        d.bankrupt ? '<span class="dpk-badge dpk-badge-out">bankrupt</span>' : "",
        d.is_me ? '<span class="dpk-badge dpk-badge-you">you</span>' : "",
      ].join("");
      const committed = d.committed ? ` <span class="msp-muted">+$${d.committed} this street</span>` : "";
      return `
        <div class="dpk-seat${d.is_turn ? " is-turn" : ""}${(d.folded || d.bankrupt) ? " is-out" : ""}">
          <span class="dpk-seat-name">${esc(d.username)}${flags}</span>
          <span class="dpk-seat-stats">${chip(d.chips)}${committed}</span>
        </div>`;
    }

    function lobby() {
      const enough = state.desks.length >= state.min_desks;
      return `
        <div class="card msp-panel">
          <div class="head">
            <h3>Desk ${esc(state.join_code)}</h3>
            <span class="msp-muted">${state.desks.length} seated</span>
          </div>
          <div class="msp-panel-body">
            <p class="msp-muted" style="margin-top:0;">Share the code. Everyone starts on
              $${state.starting_capital}. ${state.min_desks}–${state.max_desks} desks.</p>
            <div class="dpk-seats">${state.desks.map(seat).join("")}</div>
            ${state.is_host
              ? `<button class="btn primary" id="startBtn" ${enough ? "" : "disabled"}>
                   ${enough ? "Deal" : `Waiting for a ${state.min_desks}rd desk`}</button>`
              : '<p class="msp-muted">Waiting for the host to deal.</p>'}
          </div>
        </div>`;
    }

    function boardPanel() {
      const h = state.hand;
      const tiles = h.public.map((t) => `<span class="dpk-tile">${esc(t)}</span>`).join("");
      const empties = Array.from({ length: Math.max(0, 5 - h.public.length) })
        .map(() => `<span class="dpk-tile dpk-tile-empty">?</span>`).join("");
      return `
        <div class="card msp-panel dpk-pot-card">
          <div class="dpk-stage">${esc(h.stage_label)} · print ${state.print_no}/${state.total_prints}</div>
          <div class="dpk-pot-n">$${h.pot}</div>
          <div class="msp-muted">pot</div>
          <div class="dpk-public">${tiles}${empties}</div>
        </div>`;
    }

    function dualRange(id, lo, hi) {
      return `
        <div class="dpk-range2">
          <div class="dpk-range2-track"></div>
          <div class="dpk-range2-fill" id="${id}Fill" style="left:${lo}%;right:${100 - hi}%"></div>
          <input type="range" id="${id}Lo" min="0" max="100" value="${lo}">
          <input type="range" id="${id}Hi" min="0" max="100" value="${hi}">
        </div>`;
    }

    function checkpointForm(cp) {
      const span = Math.max(1, cp.pot_before + cp.to_call);
      return `
        <div class="dpk-checkpoint">
          <div>
            <div class="dpk-cp-head">Price the spot before you act</div>
            <div class="dpk-cp-sub">${esc(state.hand.stage_label)} · pot $${cp.pot_before} ·
              call $${cp.to_call} · breakeven ${cp.breakeven_pct}%</div>
          </div>

          <div class="dpk-cp-row">
            <div class="dpk-cp-label"><span>Chance your book is ahead right now</span>
              <strong id="cpProbN">${cpProb}%</strong></div>
            <input type="range" id="cpProb" min="0" max="100" value="${cpProb}">
            <div class="dpk-cp-scale"><span>0%</span><span>100%</span></div>
          </div>

          <div class="dpk-cp-row">
            <div class="dpk-cp-label"><span>90% confidence range for that same number</span>
              <strong id="cpRangeN">${cpLow}–${cpHigh}%</strong></div>
            ${dualRange("cpRange", cpLow, cpHigh)}
            <div class="dpk-cp-scale"><span>0%</span><span>100%</span></div>
          </div>

          <div class="dpk-cp-row">
            <div class="dpk-cp-label"><span>EV of calling — chips, averaged over 100 tries</span>
              <strong id="cpEvN">${cpEv >= 0 ? "+" : ""}${cpEv}</strong></div>
            <input type="range" id="cpEv" min="${-span}" max="${span}" value="${cpEv}">
            <div class="dpk-cp-scale"><span>-${span}</span><span>+${span}</span></div>
          </div>

          <button class="btn primary dpk-cp-submit" id="cpSubmitBtn">Submit read</button>
          <div class="dpk-cp-note">Your fold / call / raise buttons unlock once you submit.</div>
        </div>`;
    }

    function checkpointResult(cp) {
      const r = cp.result;
      const probCls = r.prob_error <= 8 ? "dpk-cp-hit" : "dpk-cp-miss";
      const ciCls = r.ci_hit ? "dpk-cp-hit" : "dpk-cp-miss";
      return `
        <div class="dpk-cp-result">
          <div class="dpk-cp-result-row"><span>Your read</span>
            <strong>${cpProb}% (range ${cpLow}–${cpHigh}%)</strong></div>
          <div class="dpk-cp-result-row"><span>True probability</span><strong>${r.true_prob_pct}%</strong></div>
          <div class="dpk-cp-result-row"><span>Off by</span>
            <strong class="${probCls}">${r.prob_error}pp</strong></div>
          <div class="dpk-cp-result-row"><span>Your range captured it?</span>
            <strong class="${ciCls}">${r.ci_hit ? "Yes" : "No"}</strong></div>
          <div class="dpk-cp-result-row"><span>Your EV vs. true EV of calling</span>
            <strong>${cpEv >= 0 ? "+" : ""}${cpEv} vs ${r.true_ev_call >= 0 ? "+" : ""}${r.true_ev_call}</strong></div>
        </div>`;
    }

    function actionBar() {
      const h = state.hand;
      const toCall = h.current_bet - h.my_committed;
      const canCheck = toCall <= 0;
      const minRaiseTo = Math.min(h.current_bet + Math.max(h.min_raise, state.ante), h.desk_limit);
      const maxRaiseTo = h.desk_limit;
      const canRaise = maxRaiseTo > h.current_bet && state.chips > 0;
      if (raiseAmt < minRaiseTo || raiseAmt > maxRaiseTo) raiseAmt = minRaiseTo;

      return `
        <div class="dpk-actions">
          <button class="btn" id="foldBtn">Fold</button>
          ${canCheck
            ? `<button class="btn primary" id="checkBtn">Check</button>`
            : `<button class="btn primary" id="callBtn">Call $${toCall}</button>`}
          ${canRaise ? `
            <div class="dpk-raise-row">
              <input type="range" id="raiseSlider" min="${minRaiseTo}" max="${maxRaiseTo}" value="${raiseAmt}">
              <span class="dpk-raise-n">$${raiseAmt}</span>
              <button class="btn primary" id="raiseBtn">Raise to</button>
            </div>` : ""}
        </div>`;
    }

    function revealPanel() {
      const h = state.hand;
      const r = h.reveal;
      if (!r) return "";
      let body;
      if (r.kind === "fold") {
        const winnerId = r.winners[0];
        const name = (state.desks.find((d) => d.user_id === winnerId) || {}).username || "?";
        body = `<div class="dpk-reveal-row dpk-reveal-win">
          <span>${esc(name)} takes the pot uncontested</span><span>+$${r.awarded[winnerId]}</span></div>`;
      } else {
        body = Object.entries(r.reads).map(([uid, read]) => {
          const name = (state.desks.find((d) => d.user_id === uid) || {}).username || "?";
          const isWinner = r.winners.includes(uid);
          const award = r.awarded[uid] || 0;
          return `<div class="dpk-reveal-row${isWinner ? " dpk-reveal-win" : ""}">
            <span>${esc(name)} — ${esc(read.tier_name)} (${read.tiles.join(" ")})</span>
            <span>${award ? "+$" + award : ""}</span></div>`;
        }).join("");
      }
      return `<div class="dpk-reveal">${body}</div>
        ${state.is_host
          ? `<button class="btn primary" id="nextPrintBtn" style="margin-top:10px;">Deal next print</button>`
          : `<p class="msp-muted" style="margin-top:8px;">Waiting for the host to deal the next print.</p>`}`;
    }

    function decisionArea() {
      const h = state.hand;
      if (h.stage === "showdown") return revealPanel();
      if (h.my_folded) return `<p class="msp-muted">You folded this print — waiting it out.</p>`;
      if (!h.my_turn) {
        const name = (state.desks.find((d) => d.user_id === h.turn_id) || {}).username || "—";
        return `<p class="dpk-waiting">Waiting on ${esc(name)}…</p>`;
      }
      const cp = state.checkpoint;
      if (cp && cp.needs_submit) return checkpointForm(cp);
      if (cp && cp.result) return checkpointResult(cp) + actionBar();
      return actionBar();
    }

    function myTilesPanel() {
      const h = state.hand;
      if (!h.my_tiles) {
        return `<div class="card msp-panel"><div class="msp-panel-body">
          <p class="msp-muted">You sat this print out.</p></div></div>`;
      }
      return `
        <div class="card msp-panel">
          <div class="head"><h3>Your book</h3><span class="msp-muted">${chip(state.chips)}</span></div>
          <div class="msp-panel-body">
            <div class="dpk-hand-tiles">${h.my_tiles.map((t) => `<span class="dpk-tile">${esc(t)}</span>`).join("")}</div>
            ${decisionArea()}
          </div>
        </div>`;
    }

    function finishedView() {
      const me = myUserId();
      const rows = (state.results || []).map((r, i) => `
        <tr class="${r.user_id === me ? "lbx-me" : ""}">
          <td class="lbx-rank">${i === 0 ? "🥇" : i + 1}</td>
          <td class="lbx-name">${esc(r.username)}</td>
          <td class="lbx-mid">$${r.chips}</td>
          <td class="lbx-mid">${r.bankrupt ? "bankrupt" : ""}</td>
        </tr>`).join("");

      const a = state.assessment;
      const gauges = a ? `
        <div class="dpk-gauges">
          <div class="dpk-gauge"><div class="dpk-gauge-n">${a.calibration_score}</div>
            <div class="dpk-gauge-l">Calibration score</div></div>
          <div class="dpk-gauge"><div class="dpk-gauge-n">${a.avg_prob_error}pp</div>
            <div class="dpk-gauge-l">Avg. read error</div></div>
          <div class="dpk-gauge"><div class="dpk-gauge-n">${a.rationality_index}</div>
            <div class="dpk-gauge-l">Rationality index</div></div>
          <div class="dpk-gauge"><div class="dpk-gauge-n">${a.decisions}</div>
            <div class="dpk-gauge-l">Decisions graded</div></div>
        </div>` : "";

      const followups = a && a.followups && a.followups.length ? `
        <div class="dpk-followups">
          <h4 style="margin:0 0 2px;">Worth walking through</h4>
          ${a.followups.map((f) => `<div class="dpk-followup">${esc(f)}</div>`).join("")}
        </div>` : "";

      return `
        <div class="card msp-panel">
          <div class="head"><h3>Session result</h3><span class="msp-muted">${state.total_prints} prints</span></div>
          <div class="msp-panel-body msp-flush">
            <table class="msp-table">
              <thead><tr><th>#</th><th>Desk</th><th>Capital</th><th></th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
          <div class="msp-panel-body dpk-report">
            ${gauges}
            ${followups}
            <div id="fbkBox"></div>
            <a class="btn primary" href="/dark-pool">New desk</a>
          </div>
        </div>`;
    }

    function render() {
      const area = $("#gameArea");
      if (!state) return;

      if (state.status === "lobby") { area.innerHTML = lobby(); bind(); return; }
      if (state.status === "finished") {
        area.innerHTML = finishedView();
        if (window.AB && AB.feedback) AB.feedback.render("#fbkBox", state.feedback);
        return;
      }

      const cp = state.checkpoint;
      if (cp && cp.needs_submit) {
        if (activeCpDecisionNo === cp.decision_no) return;   // mid-read — don't disturb the sliders
        activeCpDecisionNo = cp.decision_no;
        cpProb = 50; cpLow = 25; cpHigh = 75; cpEv = 0;
      } else {
        activeCpDecisionNo = null;
      }

      area.innerHTML = `
        <div class="dpk-board">
          <div class="dpk-main">
            ${boardPanel()}
            ${myTilesPanel()}
          </div>
          <div class="dpk-side">
            <div class="card msp-panel">
              <div class="head"><h3>Desks</h3><span class="msp-muted">${esc(state.join_code)}</span></div>
              <div class="msp-panel-body"><div class="dpk-seats">${state.desks.map(seat).join("")}</div></div>
            </div>
            <div class="card msp-panel">
              <div class="head"><h3>Tape</h3></div>
              <div class="msp-panel-body msp-scroll tfl-log">
                ${(state.log || []).slice().reverse().map((l) =>
                  `<div class="tfl-log-row">${esc(l.text)}</div>`).join("")
                  || '<span class="msp-muted">Nothing yet.</span>'}
              </div>
            </div>
          </div>
        </div>`;
      bind();
    }

    // ---- wiring ----
    async function doAct(body) {
      try { await api(`/dark-pool/game/${gameId}/act`, body); await poll(); }
      catch (err) { flash(err.message, true); }
    }

    function bind() {
      $("#startBtn")?.addEventListener("click", async (e) => {
        e.target.disabled = true;
        try { await api(`/dark-pool/game/${gameId}/start`, {}); await poll(); }
        catch (err) { flash(err.message, true); e.target.disabled = false; }
      });

      $("#nextPrintBtn")?.addEventListener("click", async (e) => {
        e.target.disabled = true;
        try { await api(`/dark-pool/game/${gameId}/next-print`, {}); await poll(); }
        catch (err) { flash(err.message, true); e.target.disabled = false; }
      });

      // Checkpoint sliders
      const probEl = $("#cpProb");
      probEl?.addEventListener("input", () => {
        cpProb = Number(probEl.value);
        const n = $("#cpProbN"); if (n) n.textContent = cpProb + "%";
      });

      const loEl = $("#cpRangeLo"), hiEl = $("#cpRangeHi");
      function syncRange() {
        const n = $("#cpRangeN"); if (n) n.textContent = `${cpLow}–${cpHigh}%`;
        const fill = $("#cpRangeFill");
        if (fill) { fill.style.left = cpLow + "%"; fill.style.right = (100 - cpHigh) + "%"; }
      }
      loEl?.addEventListener("input", () => {
        let v = Number(loEl.value);
        if (v > cpHigh) { v = cpHigh; loEl.value = v; }
        cpLow = v; syncRange();
      });
      hiEl?.addEventListener("input", () => {
        let v = Number(hiEl.value);
        if (v < cpLow) { v = cpLow; hiEl.value = v; }
        cpHigh = v; syncRange();
      });

      const evEl = $("#cpEv");
      evEl?.addEventListener("input", () => {
        cpEv = Number(evEl.value);
        const n = $("#cpEvN"); if (n) n.textContent = (cpEv >= 0 ? "+" : "") + cpEv;
      });

      $("#cpSubmitBtn")?.addEventListener("click", async (e) => {
        e.target.disabled = true;
        try {
          await api(`/dark-pool/game/${gameId}/checkpoint`,
                    { est_prob: cpProb, ci_low: cpLow, ci_high: cpHigh, est_ev: cpEv });
          await poll();
        } catch (err) { flash(err.message, true); e.target.disabled = false; }
      });

      // Action bar
      $("#foldBtn")?.addEventListener("click", () => doAct({ action: "fold" }));
      $("#checkBtn")?.addEventListener("click", () => doAct({ action: "check" }));
      $("#callBtn")?.addEventListener("click", () => doAct({ action: "call" }));
      $("#raiseSlider")?.addEventListener("input", (e) => {
        raiseAmt = Number(e.target.value);
        const n = $(".dpk-raise-n"); if (n) n.textContent = "$" + raiseAmt;
      });
      $("#raiseBtn")?.addEventListener("click", () => doAct({ action: "raise", amount: raiseAmt }));
    }

    poll();
    setInterval(poll, 1200);
  }

  // ── Boot ──────────────────────────────────────────────────────────────
  (async () => {
    try {
      const me = await api("/me");
      const el = $("#userName");
      if (el) el.textContent = me.username || "user";
    } catch { /* the rules page still reads fine signed out */ }

    if (window.GAME_ID) initTable(window.GAME_ID);
    else initRulesPage();
  })();
})();
