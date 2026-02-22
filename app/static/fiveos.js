(function () {
    "use strict";

    const GAME_ID = window.GAME_ID;
    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => root.querySelectorAll(sel);

    const SUIT_SYMBOLS = { hearts: "♥", diamonds: "♦", clubs: "♣", spades: "♠" };
    const SUIT_COLORS = { hearts: "#ff6b6b", diamonds: "#ff6b6b", clubs: "#c8d6e5", spades: "#c8d6e5" };
    const RANK_NAMES = { 1: "A", 11: "J", 12: "Q", 13: "K" };

    let pollTimer = null;
    let lastStatus = "";

    const fetchJSON = async (url, init) => {
        const r = await fetch(url, { credentials: "include", ...init });
        if (!r.ok) {
            const txt = await r.text();
            let detail = r.status;
            try { detail = JSON.parse(txt).detail || detail; } catch { }
            throw new Error(detail);
        }
        return r.json();
    };

    function cardLabel(card) {
        const rank = RANK_NAMES[card.rank] || card.rank;
        const suit = SUIT_SYMBOLS[card.suit] || card.suit;
        return `${rank}${suit}`;
    }

    function renderCard(card, size = "normal") {
        const rank = RANK_NAMES[card.rank] || card.rank;
        const suit = SUIT_SYMBOLS[card.suit] || card.suit;
        const color = SUIT_COLORS[card.suit] || "#c8d6e5";
        const cls = size === "large" ? "playing-card large" : "playing-card";
        return `<div class="${cls}" style="--card-color: ${color};">
      <span class="card-rank">${rank}</span>
      <span class="card-suit">${suit}</span>
    </div>`;
    }

    // ---- Auth ----
    async function getMe() {
        try {
            const me = await fetchJSON("/me");
            $("#userName").textContent = me.username || "user";
            return me;
        } catch { return null; }
    }

    // ---- Poll game state ----
    async function poll() {
        try {
            const state = await fetchJSON(`/5os/game/${GAME_ID}/state`);
            render(state);
            lastStatus = state.status;
        } catch (e) {
            console.error("Poll error:", e);
        }
    }

    function startPolling() {
        poll();
        pollTimer = setInterval(poll, 3000);
    }

    // ---- Main render ----
    function render(state) {
        const area = $("#gameArea");
        if (!area) return;

        if (state.status === "lobby") {
            renderLobby(area, state);
        } else if (state.status.startsWith("round_")) {
            renderRound(area, state);
        } else if (state.status === "finished") {
            renderFinished(area, state);
        }
    }

    // ---- LOBBY ----
    function renderLobby(area, state) {
        const players = state.players || [];
        const playerRows = players.map(p => {
            const teamBadge = p.team ? `<span class="team-badge team-${p.team.toLowerCase()}">${p.team}</span>` : '<span class="team-badge unassigned">—</span>';
            const adminControls = state.is_admin ? `
        <button onclick="assignTeam('${p.user_id}','A')" class="btn small ${p.team === 'A' ? 'active' : 'ghost'}">A</button>
        <button onclick="assignTeam('${p.user_id}','B')" class="btn small ${p.team === 'B' ? 'active' : 'ghost'}">B</button>
      ` : '';
            return `<div class="lobby-player">
        <span class="lobby-name">${p.username}</span>
        ${teamBadge}
        ${adminControls}
      </div>`;
        }).join("");

        const adminPanel = state.is_admin ? `
      <div class="admin-controls">
        <div class="join-code-display">
          <span class="label">Join Code</span>
          <span class="code">${state.join_code}</span>
        </div>
        <button onclick="advanceRound()" class="btn" id="startBtn" ${players.length < 1 ? 'disabled' : ''}>
          Start Game →
        </button>
      </div>
    ` : '';

        area.innerHTML = `
      <div class="fiveos-lobby">
        <h2>🎲 5Os — Lobby</h2>
        ${adminPanel}
        <h3>Players (${players.length})</h3>
        <div class="lobby-players">${playerRows || '<p class="muted">Waiting for players to join...</p>'}</div>
        ${!state.is_admin ? `<p class="muted" style="margin-top:16px;">Waiting for admin to start the game...</p>` : ''}
      </div>
    `;
    }

    // ---- ROUND ----
    function renderRound(area, state) {
        const roundNum = parseInt(state.status.split("_")[1]);
        const isOddRound = roundNum % 2 === 1;
        const hasSubmitted = !!state.my_submissions[String(roundNum)];

        // Build card display
        let cardsHTML = '<div class="round-cards">';

        // Show all cards accumulated so far
        for (let r = 1; r <= roundNum; r++) {
            const rKey = String(r);
            const myCard = state.my_cards[rKey];
            const commonCard = state.common_cards[rKey];

            cardsHTML += `<div class="round-card-group">
        <div class="round-label">Round ${r}</div>
        <div class="round-card-row">`;

            if (myCard) {
                cardsHTML += `<div class="card-slot">
          <div class="card-label">Your Card</div>
          ${renderCard(myCard, "large")}
        </div>`;
            }

            if (commonCard) {
                cardsHTML += `<div class="card-slot">
          <div class="card-label">Common</div>
          ${renderCard(commonCard, "large")}
        </div>`;
            }

            cardsHTML += `</div></div>`;
        }
        cardsHTML += '</div>';

        // Submission form or submitted state
        let formHTML = '';
        if (hasSubmitted) {
            const sub = state.my_submissions[String(roundNum)];
            formHTML = `<div class="submitted-banner">
        <span>✅ Submitted for Round ${roundNum}</span>
        <div class="submitted-details">
          <span>Q1: ${sub.est_q1} (${sub.pos_q1})</span>
          <span>Q2: ${sub.est_q2} (${sub.pos_q2})</span>
          <span>Q3: ${sub.est_q3} (${sub.pos_q3})</span>
        </div>
        <p class="muted">Waiting for admin to advance to next round...</p>
      </div>`;
        } else if (!state.is_admin) {
            formHTML = `<div class="submit-form">
        <h3>Submit Your Estimates</h3>
        <div class="form-grid">
          <div class="form-question">
            <label>Q1: Sum of ranks NOT in the 15 cards</label>
            <input type="number" id="est_q1" step="0.01" placeholder="0">
            <div class="pos-toggle">
              <button class="pos-btn active" data-q="q1" data-pos="long" onclick="togglePos(this)">Long</button>
              <button class="pos-btn" data-q="q1" data-pos="short" onclick="togglePos(this)">Short</button>
            </div>
          </div>
          <div class="form-question">
            <label>Q2: Odd-rank sum − Even-rank sum</label>
            <input type="number" id="est_q2" step="0.01" placeholder="0">
            <div class="pos-toggle">
              <button class="pos-btn active" data-q="q2" data-pos="long" onclick="togglePos(this)">Long</button>
              <button class="pos-btn" data-q="q2" data-pos="short" onclick="togglePos(this)">Short</button>
            </div>
          </div>
          <div class="form-question">
            <label>Q3: Sum of all 15 card ranks</label>
            <input type="number" id="est_q3" step="0.01" placeholder="0">
            <div class="pos-toggle">
              <button class="pos-btn active" data-q="q3" data-pos="long" onclick="togglePos(this)">Long</button>
              <button class="pos-btn" data-q="q3" data-pos="short" onclick="togglePos(this)">Short</button>
            </div>
          </div>
        </div>
        <button onclick="submitAnswers()" class="btn" id="submitBtn">Submit Round ${roundNum}</button>
        <p id="submitMsg" class="small" style="margin-top:8px;"></p>
      </div>`;
        }

        // Show previous round medians
        let mediansHTML = '';
        const medianKeys = Object.keys(state.round_medians || {}).sort();
        if (medianKeys.length > 0) {
            mediansHTML = '<div class="past-medians"><h3>Previous Round Medians</h3><table><tr><th>Round</th><th>Q1</th><th>Q2</th><th>Q3</th></tr>';
            for (const rk of medianKeys) {
                const m = state.round_medians[rk];
                mediansHTML += `<tr><td>${rk}</td><td>${m.q1?.toFixed(2)}</td><td>${m.q2?.toFixed(2)}</td><td>${m.q3?.toFixed(2)}</td></tr>`;
            }
            mediansHTML += '</table></div>';
        }

        // Admin controls
        const adminPanel = state.is_admin ? `
      <div class="admin-controls" style="margin-bottom:24px;">
        <button onclick="advanceRound()" class="btn">
          ${roundNum >= 5 ? 'Finish Game' : `End Round ${roundNum} → Start Round ${roundNum + 1}`}
        </button>
      </div>
    ` : '';

        area.innerHTML = `
      <div class="fiveos-round">
        <h2>🎲 Round ${roundNum} of 5</h2>
        ${adminPanel}
        ${cardsHTML}
        ${formHTML}
        ${mediansHTML}
      </div>
    `;
    }

    // ---- FINISHED ----
    function renderFinished(area, state) {
        const actuals = state.actuals || {};
        const pnl = state.pnl || {};
        const playerPnl = pnl.players || {};
        const teamPnl = pnl.teams || {};
        const winner = pnl.winner;
        const deck15 = state.deck_15 || [];

        // All cards
        const allCardsHTML = deck15.map(c => renderCard(c)).join("");

        // Player leaderboard
        const sortedPlayers = Object.values(playerPnl).sort((a, b) => b.pnl - a.pnl);
        const leaderboardHTML = sortedPlayers.map((p, i) => `
      <div class="lb-row ${i === 0 ? 'lb-first' : ''}">
        <span class="lb-rank">#${i + 1}</span>
        <span class="lb-name">${p.username}</span>
        <span class="team-badge team-${(p.team || '').toLowerCase()}">${p.team || '—'}</span>
        <span class="lb-pnl ${p.pnl >= 0 ? 'positive' : 'negative'}">${p.pnl >= 0 ? '+' : ''}${p.pnl.toFixed(2)}</span>
      </div>
    `).join("");

        // Team results
        const teamHTML = Object.entries(teamPnl).sort((a, b) => b[1] - a[1]).map(([team, val]) => `
      <div class="team-result ${team === winner ? 'team-winner' : ''}">
        <span class="team-badge team-${team.toLowerCase()}">${team}</span>
        <span class="team-pnl ${val >= 0 ? 'positive' : 'negative'}">${val >= 0 ? '+' : ''}${val.toFixed(2)}</span>
        ${team === winner ? '<span class="winner-label">🏆 WINNER</span>' : ''}
      </div>
    `).join("");

        area.innerHTML = `
      <div class="fiveos-finished">
        <h2>🏆 Game Over!</h2>

        <div class="results-section">
          <h3>Actual Values</h3>
          <div class="actuals-grid">
            <div class="actual-item"><span class="actual-label">Q1: Ranks NOT in 15</span><span class="actual-val">${actuals.q1}</span></div>
            <div class="actual-item"><span class="actual-label">Q2: Odd − Even</span><span class="actual-val">${actuals.q2}</span></div>
            <div class="actual-item"><span class="actual-label">Q3: Sum of 15</span><span class="actual-val">${actuals.q3}</span></div>
          </div>
        </div>

        <div class="results-section">
          <h3>The 15 Cards</h3>
          <div class="final-deck">${allCardsHTML}</div>
        </div>

        <div class="results-section">
          <h3>Team Results</h3>
          <div class="team-results">${teamHTML}</div>
        </div>

        <div class="results-section">
          <h3>Player Leaderboard</h3>
          <div class="leaderboard">${leaderboardHTML}</div>
        </div>

        <a href="/" class="btn" style="margin-top: 24px; display: inline-block;">← Back to Home</a>
      </div>
    `;
    }

    // ---- Actions ----
    window.togglePos = function (btn) {
        const q = btn.dataset.q;
        const parent = btn.parentElement;
        parent.querySelectorAll(".pos-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
    };

    window.submitAnswers = async function () {
        const msg = $("#submitMsg");
        const positions = {};
        $$(".pos-toggle").forEach(toggle => {
            const activeBtn = toggle.querySelector(".pos-btn.active");
            if (activeBtn) positions[activeBtn.dataset.q] = activeBtn.dataset.pos;
        });

        const body = {
            est_q1: parseFloat($("#est_q1").value) || 0,
            est_q2: parseFloat($("#est_q2").value) || 0,
            est_q3: parseFloat($("#est_q3").value) || 0,
            pos_q1: positions.q1 || "long",
            pos_q2: positions.q2 || "long",
            pos_q3: positions.q3 || "long",
        };

        try {
            await fetchJSON(`/5os/game/${GAME_ID}/submit`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            poll(); // Refresh immediately
        } catch (e) {
            if (msg) { msg.textContent = String(e.message); msg.style.color = "#ff6b6b"; }
        }
    };

    window.advanceRound = async function () {
        try {
            await fetchJSON(`/5os/game/${GAME_ID}/advance`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
            });
            poll();
        } catch (e) {
            alert("Error: " + e.message);
        }
    };

    window.assignTeam = async function (userId, team) {
        try {
            await fetchJSON(`/5os/game/${GAME_ID}/team`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ user_id: userId, team: team }),
            });
            poll();
        } catch (e) {
            alert("Error: " + e.message);
        }
    };

    // ---- Init ----
    getMe();
    startPolling();
})();
