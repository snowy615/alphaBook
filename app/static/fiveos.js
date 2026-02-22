(function () {
  "use strict";

  const GAME_ID = window.GAME_ID;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => root.querySelectorAll(sel);

  const SUIT_SYMBOLS = { hearts: "♥", diamonds: "♦", clubs: "♣", spades: "♠" };
  const SUIT_COLORS = { hearts: "#ff6b6b", diamonds: "#ff6b6b", clubs: "#c8d6e5", spades: "#c8d6e5" };
  const RANK_NAMES = { 1: "A", 11: "J", 12: "Q", 13: "K" };

  let pollTimer = null;

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
  let lastStateHash = "";

  function stateHash(state) {
    // Build a lightweight fingerprint of the parts that affect the UI
    const players = (state.players || []).map(p => p.user_id + p.team).join(",");
    const subs = Object.keys(state.my_submissions || {}).join(",");
    return state.status + "|" + players + "|" + subs;
  }

  function render(state) {
    const area = $("#gameArea");
    if (!area) return;

    const hash = stateHash(state);
    if (hash === lastStateHash) return; // nothing changed
    lastStateHash = hash;

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
          <span>Q1: ${sub.est_q1}</span>
          <span>Q2: ${sub.est_q2}</span>
          <span>Q3: ${sub.est_q3}</span>
        </div>
        <p class="muted">Waiting for admin to advance to next round...</p>
      </div>`;
    } else if (!state.is_admin) {
      formHTML = `<div class="submit-form">
        <h3>Submit Your Estimates</h3>
        <p class="muted" style="margin-bottom:12px;">Your position (long/short) is determined by whether your estimate is above or below the group median.</p>
        <div class="form-grid">
          <div class="form-question">
            <label>Q1: Sum of ranks NOT in the 15 cards</label>
            <input type="number" id="est_q1" step="0.01" placeholder="Enter estimate">
          </div>
          <div class="form-question">
            <label>Q2: Odd-rank sum − Even-rank sum</label>
            <input type="number" id="est_q2" step="0.01" placeholder="Enter estimate">
          </div>
          <div class="form-question">
            <label>Q3: Sum of all 15 card ranks</label>
            <input type="number" id="est_q3" step="0.01" placeholder="Enter estimate">
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
    const teamRoundPnl = pnl.team_round_pnl || {};
    const winner = pnl.winner;
    const deck15 = state.deck_15 || [];
    const optimal = state.optimal || {};
    const mySubs = state.my_submissions || {};

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
          <h3>Team PnL Over Rounds</h3>
          <div class="chart-container"><canvas id="teamPnlChart"></canvas></div>
        </div>

        <div class="results-section">
          <h3>Your Estimates vs Optimal</h3>
          <div class="chart-tabs">
            <button class="chart-tab active" onclick="showQuestionChart('q1', this)">Q1</button>
            <button class="chart-tab" onclick="showQuestionChart('q2', this)">Q2</button>
            <button class="chart-tab" onclick="showQuestionChart('q3', this)">Q3</button>
          </div>
          <div class="chart-container"><canvas id="estimateChart"></canvas></div>
        </div>

        <div class="results-section">
          <h3>Player Leaderboard</h3>
          <div class="leaderboard">${leaderboardHTML}</div>
        </div>

        <a href="/" class="btn" style="margin-top: 24px; display: inline-block;">← Back to Home</a>
      </div>
    `;

    // Draw charts after DOM is ready
    setTimeout(() => {
      drawTeamPnlChart(teamRoundPnl, playerPnl);
      // Store data for tab switching
      window._chartData = { mySubs, optimal, actuals };
      drawEstimateChart("q1", mySubs, optimal, actuals);
    }, 100);
  }

  window.submitAnswers = async function () {
    const msg = $("#submitMsg");

    const body = {
      est_q1: parseFloat($("#est_q1").value) || 0,
      est_q2: parseFloat($("#est_q2").value) || 0,
      est_q3: parseFloat($("#est_q3").value) || 0,
    };

    try {
      await fetchJSON(`/5os/game/${GAME_ID}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      lastStateHash = ""; // force re-render to show submitted state
      poll();
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
  // ---- Charts ----
  const TEAM_COLORS = { A: "#6c5ce7", B: "#00cec9" };
  const PLAYER_COLORS = ["#fd79a8", "#00b894", "#6c5ce7", "#fdcb6e", "#00cec9", "#e17055", "#a29bfe", "#55efc4"];
  let estimateChartInstance = null;

  function drawTeamPnlChart(teamRoundPnl, playerPnl) {
    const canvas = document.getElementById("teamPnlChart");
    if (!canvas || typeof Chart === "undefined") return;

    const labels = ["R1", "R2", "R3", "R4", "R5"];
    let datasets;

    const hasTeams = Object.keys(teamRoundPnl).length > 0;

    if (hasTeams) {
      // Show team-level lines
      datasets = Object.entries(teamRoundPnl).map(([team, values]) => ({
        label: `Team ${team}`,
        data: values,
        borderColor: TEAM_COLORS[team] || "#888",
        backgroundColor: (TEAM_COLORS[team] || "#888") + "33",
        fill: true,
        tension: 0.3,
        pointRadius: 5,
        pointHoverRadius: 7,
        borderWidth: 2,
      }));
    } else {
      // Fallback: show per-player lines
      let i = 0;
      datasets = Object.values(playerPnl || {}).map(p => {
        const color = PLAYER_COLORS[i++ % PLAYER_COLORS.length];
        return {
          label: p.username,
          data: p.round_pnls || [0, 0, 0, 0, 0],
          borderColor: color,
          backgroundColor: color + "33",
          fill: false,
          tension: 0.3,
          pointRadius: 5,
          pointHoverRadius: 7,
          borderWidth: 2,
        };
      });
    }

    new Chart(canvas, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        plugins: {
          legend: { labels: { color: "#c8d6e5" } },
        },
        scales: {
          x: { ticks: { color: "#636e72" }, grid: { color: "#2d3436" } },
          y: {
            ticks: { color: "#636e72" }, grid: { color: "#2d3436" },
            title: { display: true, text: "Cumulative PnL", color: "#636e72" }
          },
        },
      },
    });
  }

  function drawEstimateChart(qKey, mySubs, optimal, actuals) {
    const canvas = document.getElementById("estimateChart");
    if (!canvas || typeof Chart === "undefined") return;

    if (estimateChartInstance) estimateChartInstance.destroy();

    const labels = ["R1", "R2", "R3", "R4", "R5"];
    const qLabels = { q1: "Q1: Ranks NOT in 15", q2: "Q2: Odd − Even", q3: "Q3: Sum of 15" };

    const myVals = labels.map((_, i) => {
      const sub = mySubs[String(i + 1)];
      return sub ? sub[`est_${qKey}`] : null;
    });

    const optVals = labels.map((_, i) => {
      const o = optimal[String(i + 1)];
      return o ? o[qKey] : null;
    });

    const actualVal = actuals[qKey];

    estimateChartInstance = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Your Estimate",
            data: myVals,
            borderColor: "#fd79a8",
            backgroundColor: "#fd79a833",
            pointRadius: 6,
            pointHoverRadius: 8,
            borderWidth: 2,
            tension: 0.2,
          },
          {
            label: "Optimal (Expected)",
            data: optVals,
            borderColor: "#00b894",
            backgroundColor: "#00b89433",
            pointRadius: 6,
            pointHoverRadius: 8,
            borderWidth: 2,
            borderDash: [6, 3],
            tension: 0.2,
          },
          {
            label: "Actual Value",
            data: labels.map(() => actualVal),
            borderColor: "#ffeaa7",
            borderWidth: 1,
            borderDash: [3, 3],
            pointRadius: 0,
            fill: false,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          title: { display: true, text: qLabels[qKey] || qKey, color: "#c8d6e5", font: { size: 14 } },
          legend: { labels: { color: "#c8d6e5" } },
        },
        scales: {
          x: { ticks: { color: "#636e72" }, grid: { color: "#2d3436" } },
          y: { ticks: { color: "#636e72" }, grid: { color: "#2d3436" } },
        },
      },
    });
  }

  window.showQuestionChart = function (qKey, btn) {
    document.querySelectorAll(".chart-tab").forEach(t => t.classList.remove("active"));
    if (btn) btn.classList.add("active");
    const d = window._chartData;
    if (d) drawEstimateChart(qKey, d.mySubs, d.optimal, d.actuals);
  };

  // ---- Init ----
  getMe();
  startPolling();
})();
