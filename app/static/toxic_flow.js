/* Toxic Flow — table client.
 *
 * Two pages share this file: the rules page (create/join) and the table
 * itself. The table polls once a second, which is also what drives the game
 * forward — the server resolves an expired audit window on read, so the clock
 * is honest even if a browser goes away mid-claim.
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

  const RANKS = [
    [1, "A"], [2, "2"], [3, "3"], [4, "4"], [5, "5"], [6, "6"], [7, "7"],
    [8, "8"], [9, "9"], [10, "10"], [11, "J"], [12, "Q"], [13, "K"],
  ];
  const rankValue = (r) => (r === 1 ? 11 : r >= 11 ? 10 : r);
  const SUIT_CLASS = { hearts: "tfl-red", diamonds: "tfl-red", clubs: "", spades: "" };

  // ── Rules page ────────────────────────────────────────────────────────
  function initRulesPage() {
    $("#createBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const res = await api("/toxic-flow/create", {});
        window.location.href = `/toxic-flow/game/${res.game_id}`;
      } catch (err) {
        flash(err.status === 401 ? "Please log in first." : err.message, true);
        e.target.disabled = false;
      }
    });

    const join = async () => {
      const code = ($("#joinCode")?.value || "").trim().toUpperCase();
      if (code.length !== 6) { flash("Join codes are six characters.", true); return; }
      try {
        const res = await api("/toxic-flow/join", { join_code: code });
        window.location.href = `/toxic-flow/game/${res.game_id}`;
      } catch (err) {
        flash(err.status === 401 ? "Please log in first." : err.message, true);
      }
    };
    $("#joinBtn")?.addEventListener("click", join);
    $("#joinCode")?.addEventListener("keydown", (e) => { if (e.key === "Enter") join(); });
  }

  // ── Table ─────────────────────────────────────────────────────────────
  function initTable(gameId) {
    let state = null;
    const picked = new Set();      // indexes into my hand
    let claimRank = null;          // rank I'm about to declare

    async function poll() {
      try {
        state = await api(`/toxic-flow/game/${gameId}/state`);
        render();
      } catch (err) {
        if (err.status === 401) window.location.href = "/login";
      }
    }

    // ---- pieces ----
    const chip = (n) => `<span class="tfl-chip">$${n}</span>`;

    function seat(p) {
      const flags = [
        p.is_turn ? '<span class="tfl-badge tfl-badge-turn">to act</span>' : "",
        p.bankrupt ? '<span class="tfl-badge tfl-badge-out">margin called</span>' : "",
        p.is_me ? '<span class="tfl-badge tfl-badge-you">you</span>' : "",
      ].join("");
      return `
        <div class="tfl-seat${p.is_turn ? " is-turn" : ""}${p.bankrupt ? " is-out" : ""}">
          <div class="tfl-seat-top">
            <span class="tfl-seat-name">${esc(p.username)}</span>${flags}
          </div>
          <div class="tfl-seat-stats">
            ${chip(p.chips)}
            <span class="tfl-cards">🂠 ${p.cards}</span>
          </div>
        </div>`;
    }

    function handCards() {
      const hand = state.hand || [];
      if (!hand.length) return '<p class="msp-muted">Your hand is empty.</p>';
      return hand.map((c, i) => `
        <button type="button" class="tfl-card ${SUIT_CLASS[c.suit] || ""}${picked.has(i) ? " is-picked" : ""}"
                data-i="${i}" ${state.my_turn ? "" : "disabled"}>${esc(c.label)}</button>`).join("");
    }

    function claimPanel() {
      const c = state.claim;
      if (!c) return "";
      const pct = Math.max(0, Math.min(100, (c.seconds_left / state.audit_seconds) * 100));
      const canAudit = !c.is_mine && !state.bankrupt && state.joined;
      return `
        <div class="tfl-claim">
          <div class="tfl-claim-head">
            <div>
              <div class="tfl-claim-title">
                <strong>${esc(c.username)}</strong> claims
                <span class="tfl-claim-rank">${c.count}× ${esc(c.rank)}</span>
              </div>
              <div class="msp-muted">margin ${chip(c.margin)} · staked ${chip(c.pool)}
                ${c.needed ? `· ${chip(c.needed)} more to flip` : "· covered"}</div>
            </div>
            <div class="tfl-timer"><span class="tfl-timer-n">${c.seconds_left}</span>s</div>
          </div>
          <div class="tfl-timerbar"><span style="width:${pct}%"></span></div>
          ${canAudit ? `
            <div class="tfl-audit">
              <input id="auditAmt" class="msp-input" type="number" min="1"
                     value="${c.needed || c.margin}" step="1">
              <button class="btn primary" id="auditBtn">Pay to look</button>
              <span class="msp-muted">a single auditor must cover ${chip(c.margin)}; several can pool</span>
            </div>` : c.is_mine
              ? '<p class="msp-muted">Your claim is on the table. Sit tight.</p>'
              : ""}
        </div>`;
    }

    function choicePanel() {
      const ch = state.choice;
      if (!ch) return "";
      if (!ch.is_mine) {
        return `<div class="tfl-claim"><p class="msp-muted">Nobody looked.
          <strong>${esc(state.players.find(p => p.user_id === ch.player_id)?.username || "")}</strong>
          is deciding what to do.</p></div>`;
      }
      const others = state.players.filter(p => !p.is_me && !p.bankrupt);
      return `
        <div class="tfl-claim tfl-choice">
          <div class="tfl-claim-title">You got away with it. Now what?</div>
          <div class="tfl-choice-row">
            <button class="btn primary" id="concealBtn">
              Conceal · margin back +$${ch.conceal_bonus}
            </button>
            <div class="tfl-flash">
              <select id="flashTarget" class="msp-input">
                ${others.map(p => `<option value="${esc(p.user_id)}">${esc(p.username)}</option>`).join("")}
              </select>
              <button class="btn" id="flashBtn">Flash · they eat ${ch.count} cards</button>
            </div>
          </div>
          <p class="msp-muted">Concealing keeps the lie secret. Flashing shows the table what you
            did — and hands the cards to whoever you pick.</p>
        </div>`;
    }

    function revealPanel() {
      const r = state.reveal;
      if (!r) return "";
      const name = (id) => esc(state.players.find(p => p.user_id === id)?.username || "");
      const cards = (r.cards || []).map(c => `<span class="tfl-mini">${esc(c)}</span>`).join("");
      let text = "";
      if (r.kind === "caught") text = `<strong>Caught.</strong> ${name(r.claimant)} was lying ${cards} — ${name(r.auditor)} takes the margin and a $${r.fine} fine.`;
      else if (r.kind === "squeeze") text = `<strong>Squeeze.</strong> ${name(r.claimant)} was telling the truth ${cards} — ${name(r.auditor)} pays up and picks up the pile.`;
      else if (r.kind === "flash") text = `<strong>Flashed.</strong> ${name(r.claimant)} was lying ${cards} — ${name(r.target)} eats them.`;
      else if (r.kind === "conceal") text = `<strong>Concealed.</strong> ${name(r.claimant)} skimmed the bank and said nothing.`;
      else text = `<strong>No audit.</strong> ${name(r.claimant)} takes the margin back.`;
      return `<div class="tfl-reveal tfl-reveal-${esc(r.kind)}">${text}</div>`;
    }

    function playPanel() {
      if (!state.my_turn) return "";
      const forced = state.next_rank;
      const opts = RANKS.map(([r, label]) =>
        `<option value="${r}"${claimRank === r ? " selected" : ""}>${label}</option>`).join("");
      const n = picked.size;
      const rank = claimRank || (forced ? RANKS.find(([, l]) => l === forced)?.[0] : null);
      const margin = n && rank ? n * rankValue(rank) : 0;
      const afford = margin <= (state.chips || 0);
      return `
        <div class="tfl-play">
          <div class="tfl-play-row">
            <span>Declare</span>
            ${forced
              ? `<span class="tfl-forced">${esc(forced)}</span>
                 <span class="msp-muted">the sequence decides it</span>`
              : `<select id="rankSel" class="msp-input">${opts}</select>
                 <span class="msp-muted">any rank starts a new sequence</span>`}
          </div>
          <div class="tfl-play-row">
            <span>${n} card${n === 1 ? "" : "s"} selected · margin
              <strong class="${afford ? "" : "tfl-short"}">$${margin}</strong></span>
            <button class="btn primary" id="playBtn" ${n && afford ? "" : "disabled"}>
              Post claim
            </button>
          </div>
          ${!afford ? '<p class="tfl-short">You can\'t cover that margin.</p>' : ""}
        </div>`;
    }

    function lobby() {
      const enough = state.players.length >= 3;
      return `
        <div class="card msp-panel">
          <div class="head">
            <h3>Table ${esc(state.join_code)}</h3>
            <span class="msp-muted">${state.players.length} seated</span>
          </div>
          <div class="msp-panel-body">
            <p class="msp-muted" style="margin-top:0;">Share the code. Everyone starts on
              $${state.starting_capital}. Three players minimum, six maximum.</p>
            <div class="tfl-seats">${state.players.map(seat).join("")}</div>
            ${state.is_host
              ? `<button class="btn primary" id="startBtn" ${enough ? "" : "disabled"}>
                   ${enough ? "Deal" : "Waiting for a third player"}</button>`
              : '<p class="msp-muted">Waiting for the host to deal.</p>'}
          </div>
        </div>`;
    }

    function finished() {
      const rows = (state.results || []).map((r, i) => `
        <tr class="${r.user_id === (state.players.find(p => p.is_me) || {}).user_id ? "lbx-me" : ""}">
          <td class="lbx-rank">${i === 0 ? "🥇" : i + 1}</td>
          <td class="lbx-name">${esc(r.username)}</td>
          <td class="lbx-mid">$${r.chips}</td>
          <td class="lbx-mid">${r.bankrupt ? "margin called" : `${r.cards} left`}</td>
        </tr>`).join("");
      return `
        <div class="card msp-panel">
          <div class="head"><h3>Final capital</h3><span class="msp-muted">bonuses paid</span></div>
          <div class="msp-panel-body msp-flush">
            <table class="msp-table">
              <thead><tr><th>#</th><th>Player</th><th>Capital</th><th>Hand</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
          <div class="msp-panel-body">
            <div id="fbkBox" class="hidden"></div>
            <a class="btn primary" href="/toxic-flow">New table</a>
          </div>
        </div>`;
    }

    function render() {
      const area = $("#gameArea");
      if (!state) return;

      if (state.status === "lobby") { area.innerHTML = lobby(); bind(); return; }
      if (state.status === "finished") {
        area.innerHTML = finished();
        if (window.AB && AB.feedback) AB.feedback.render("#fbkBox", state.feedback);
        return;
      }

      area.innerHTML = `
        <div class="tfl-board">
          <div class="tfl-main">
            <div class="tfl-pile-card">
              <div class="tfl-pile">
                <div class="tfl-pile-n">${state.pile}</div>
                <div class="msp-muted">cards in the middle</div>
                ${state.declared_rank
                  ? `<div class="tfl-seq">sequence on
                      <strong>${esc(RANKS.find(([r]) => r === state.declared_rank)?.[1] || "")}</strong>
                      · next is <strong>${esc(state.next_rank || "")}</strong></div>`
                  : '<div class="tfl-seq">fresh pile — any rank opens it</div>'}
              </div>
              ${revealPanel()}
              ${claimPanel()}
              ${choicePanel()}
            </div>

            <div class="card msp-panel">
              <div class="head">
                <h3>Your hand</h3>
                <span class="msp-muted">${chip(state.chips || 0)} in front of you</span>
              </div>
              <div class="msp-panel-body">
                <div class="tfl-hand">${handCards()}</div>
                ${playPanel()}
              </div>
            </div>
          </div>

          <div class="tfl-side">
            <div class="card msp-panel">
              <div class="head"><h3>Table</h3><span class="msp-muted">${esc(state.join_code)}</span></div>
              <div class="msp-panel-body"><div class="tfl-seats">${state.players.map(seat).join("")}</div></div>
            </div>
            <div class="card msp-panel">
              <div class="head"><h3>Tape</h3></div>
              <div class="msp-panel-body msp-scroll tfl-log">
                ${(state.log || []).slice().reverse().map(l =>
                  `<div class="tfl-log-row">${esc(l.text)}</div>`).join("")
                  || '<span class="msp-muted">Nothing yet.</span>'}
              </div>
            </div>
          </div>
        </div>`;
      bind();
    }

    // ---- wiring ----
    function bind() {
      $("#startBtn")?.addEventListener("click", async (e) => {
        e.target.disabled = true;
        try { await api(`/toxic-flow/game/${gameId}/start`, {}); await poll(); }
        catch (err) { flash(err.message, true); e.target.disabled = false; }
      });

      document.querySelectorAll(".tfl-card").forEach((b) => {
        b.addEventListener("click", () => {
          const i = Number(b.dataset.i);
          if (picked.has(i)) picked.delete(i);
          else if (picked.size < state.max_cards) picked.add(i);
          else flash(`You can play at most ${state.max_cards} cards.`, true);
          render();
        });
      });

      $("#rankSel")?.addEventListener("change", (e) => {
        claimRank = Number(e.target.value);
        render();
      });

      $("#playBtn")?.addEventListener("click", async (e) => {
        const forced = state.next_rank;
        const rank = forced
          ? RANKS.find(([, l]) => l === forced)[0]
          : (claimRank || Number($("#rankSel")?.value));
        e.target.disabled = true;
        try {
          await api(`/toxic-flow/game/${gameId}/play`,
                    { rank, cards: [...picked] });
          picked.clear();
          claimRank = null;
          await poll();
        } catch (err) { flash(err.message, true); e.target.disabled = false; }
      });

      $("#auditBtn")?.addEventListener("click", async (e) => {
        const amount = Number($("#auditAmt")?.value || 0);
        e.target.disabled = true;
        try {
          const res = await api(`/toxic-flow/game/${gameId}/audit`, { amount });
          if (res.resolved) flash("Cards flipped.", false);
          await poll();
        } catch (err) { flash(err.message, true); e.target.disabled = false; }
      });

      $("#concealBtn")?.addEventListener("click", async (e) => {
        e.target.disabled = true;
        try { await api(`/toxic-flow/game/${gameId}/choose`, { action: "conceal" }); await poll(); }
        catch (err) { flash(err.message, true); e.target.disabled = false; }
      });

      $("#flashBtn")?.addEventListener("click", async (e) => {
        e.target.disabled = true;
        try {
          await api(`/toxic-flow/game/${gameId}/choose`,
                    { action: "flash", target_id: $("#flashTarget")?.value });
          await poll();
        } catch (err) { flash(err.message, true); e.target.disabled = false; }
      });
    }

    poll();
    setInterval(poll, 1000);
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
