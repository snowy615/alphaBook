(function () {
  "use strict";

  const GAME_ID = window.GAME_ID;
  const $ = (sel, root = document) => root.querySelector(sel);
  let pollTimer = null;
  let lastStateHash = "";
  let priceChart = null;

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

  // Auth
  async function getMe() {
    try {
      const me = await fetchJSON("/me");
      $("#userName").textContent = me.username || "user";
      return me;
    } catch { return null; }
  }

  // Polling
  async function poll() {
    try {
      const state = await fetchJSON(`/headline/game/${GAME_ID}/state`);
      render(state);
    } catch (e) {
      console.error("Poll error:", e);
    }
  }

  function startPolling() {
    poll();
    pollTimer = setInterval(poll, 1000);
  }

  // State hash for smart re-render
  function stateHash(state) {
    return state.status + "|" + state.tick + "|" + (state.news || []).length + "|" +
      (state.players || []).map(p => p.user_id).join(",");
  }

  // Main render
  function render(state) {
    const area = $("#gameArea");
    if (!area) return;

    if (state.status === "lobby") {
      const hash = stateHash(state);
      if (hash === lastStateHash) return;
      lastStateHash = hash;
      renderLobby(area, state);
    } else if (state.status === "active") {
      renderActive(area, state);
    } else if (state.status === "finished") {
      const hash = stateHash(state);
      if (hash === lastStateHash) return;
      lastStateHash = hash;
      renderFinished(area, state);
    }
  }

  // ---- LOBBY ----
  function renderLobby(area, state) {
    const players = state.players || [];
    const playerRows = players.map(p => `
      <div class="lobby-player">
        <span class="lobby-name">${p.username}</span>
      </div>
    `).join("");

    const adminPanel = state.is_admin ? `
      <div class="admin-controls">
        <div class="join-code-display">
          <span class="label">Join Code</span>
          <span class="code">${state.join_code}</span>
        </div>
        <button onclick="startGame()" class="btn" id="startBtn" ${players.length < 1 ? 'disabled' : ''}>
          Start Game →
        </button>
      </div>
    ` : '';

    area.innerHTML = `
      <div class="fiveos-lobby">
        <h2>📰 Headline — Lobby</h2>
        <p class="muted" style="margin-bottom:16px;">Scenario: <strong style="color:var(--text);">${state.template_name}</strong></p>
        ${adminPanel}
        <h3>Players (${players.length})</h3>
        <div class="lobby-players">${playerRows || '<p class="muted">Waiting for players to join...</p>'}</div>
        ${!state.is_admin ? '<p class="muted" style="margin-top:16px;">Waiting for admin to start the game...</p>' : ''}
      </div>
    `;
  }

  // ---- ACTIVE GAME ----
  function renderActive(area, state) {
    const tick = state.tick || 0;
    const duration = state.duration || 300;
    const timeLeft = Math.max(0, duration - tick);
    const minutes = Math.floor(timeLeft / 60);
    const seconds = timeLeft % 60;
    const price = state.current_price || 100;
    const startPrice = state.start_price || 100;
    const priceChange = ((price - startPrice) / startPrice * 100).toFixed(2);
    const priceColor = price >= startPrice ? '#00b894' : '#ff6b6b';
    const myPos = state.my_position || 0;
    const myPnl = state.my_pnl || 0;
    const news = state.news || [];
    const leaderboard = state.leaderboard || [];

    // Only rebuild DOM if this is the first render
    if (!$("#hl-price-display")) {
      area.innerHTML = `
        <div class="hl-game">
          <div class="hl-header">
            <h2>📰 ${state.template_name}</h2>
            <div class="hl-timer" id="hl-timer">${minutes}:${String(seconds).padStart(2, '0')}</div>
          </div>

          <div class="hl-grid">
            <div class="hl-left">
              <div class="hl-price-section">
                <div class="hl-price-label">Current Price</div>
                <div class="hl-price-display" id="hl-price-display">$${price.toFixed(2)}</div>
                <div class="hl-price-change" id="hl-price-change" style="color:${priceColor};">${priceChange >= 0 ? '+' : ''}${priceChange}%</div>
              </div>

              <div class="chart-container" style="height:220px;">
                <canvas id="priceChart"></canvas>
              </div>

              <div class="hl-trade-section">
                <div class="hl-pnl-row">
                  <span class="hl-pnl-label">Your PnL</span>
                  <span class="hl-pnl-value" id="hl-pnl"
                    style="color:${myPnl >= 0 ? '#00b894' : '#ff6b6b'};">
                    ${myPnl >= 0 ? '+' : ''}${myPnl.toFixed(2)}
                  </span>
                </div>
                <div class="hl-position-row">
                  <span class="hl-pos-label">Position</span>
                  <span class="hl-pos-value" id="hl-pos"
                    style="color:${myPos > 0 ? '#00b894' : myPos < 0 ? '#ff6b6b' : 'var(--muted)'};">
                    ${myPos > 0 ? '+' : ''}${myPos}
                  </span>
                </div>
                <div class="hl-slider-row">
                  <input type="range" id="posSlider" min="-1000" max="1000" step="50" value="${myPos}"
                    oninput="updateSliderLabel(this.value)">
                  <div class="hl-slider-labels">
                    <span>-1000</span>
                    <span id="sliderVal" style="font-weight:700;color:var(--brand);">${myPos}</span>
                    <span>+1000</span>
                  </div>
                </div>
                <div class="hl-trade-btns">
                  <button class="btn hl-btn-short" onclick="submitTrade(-1000)">Max Short</button>
                  <button class="btn hl-btn-sell" onclick="submitTrade(Math.max(currentPos()-100, -1000))">Sell 100</button>
                  <button class="btn hl-btn-flat" onclick="submitTrade(0)">Flatten</button>
                  <button class="btn hl-btn-buy" onclick="submitTrade(Math.min(currentPos()+100, 1000))">Buy 100</button>
                  <button class="btn hl-btn-long" onclick="submitTrade(1000)">Max Long</button>
                </div>
                <button class="btn" onclick="submitTrade(parseInt(document.getElementById('posSlider').value))" style="width:100%;margin-top:8px;">
                  Set Position →
                </button>
                <p id="tradeMsg" class="small" style="margin-top:4px;"></p>
              </div>
            </div>

            <div class="hl-right">
              <div class="hl-news-section">
                <h3>📰 News Feed</h3>
                <div class="hl-news-feed" id="hl-news-feed"></div>
              </div>

              <div class="hl-lb-section">
                <h3>🏆 Leaderboard</h3>
                <div class="hl-leaderboard" id="hl-leaderboard"></div>
              </div>
            </div>
          </div>
        </div>
      `;

      // Init chart
      initPriceChart(state.price_history || []);
    }

    // Update values
    updateLiveData(state);
  }

  function updateLiveData(state) {
    const tick = state.tick || 0;
    const duration = state.duration || 300;
    const timeLeft = Math.max(0, duration - tick);
    const minutes = Math.floor(timeLeft / 60);
    const seconds = timeLeft % 60;
    const price = state.current_price || 100;
    const startPrice = state.start_price || 100;
    const priceChange = ((price - startPrice) / startPrice * 100).toFixed(2);
    const priceColor = price >= startPrice ? '#00b894' : '#ff6b6b';
    const myPos = state.my_position || 0;
    const myPnl = state.my_pnl || 0;

    const timerEl = $("#hl-timer");
    if (timerEl) timerEl.textContent = `${minutes}:${String(seconds).padStart(2, '0')}`;

    const priceEl = $("#hl-price-display");
    if (priceEl) priceEl.textContent = `$${price.toFixed(2)}`;

    const changeEl = $("#hl-price-change");
    if (changeEl) {
      changeEl.textContent = `${priceChange >= 0 ? '+' : ''}${priceChange}%`;
      changeEl.style.color = priceColor;
    }

    const pnlEl = $("#hl-pnl");
    if (pnlEl) {
      pnlEl.textContent = `${myPnl >= 0 ? '+' : ''}${myPnl.toFixed(2)}`;
      pnlEl.style.color = myPnl >= 0 ? '#00b894' : '#ff6b6b';
    }

    const posEl = $("#hl-pos");
    if (posEl) {
      posEl.textContent = `${myPos > 0 ? '+' : ''}${myPos}`;
      posEl.style.color = myPos > 0 ? '#00b894' : myPos < 0 ? '#ff6b6b' : 'var(--muted)';
    }

    // Update chart
    updatePriceChart(state.price_history || []);

    // Update news feed — show strength badge during active game
    const newsEl = $("#hl-news-feed");
    if (newsEl) {
      const news = state.news || [];
      newsEl.innerHTML = news.length === 0
        ? '<p class="muted">No news yet... stay alert!</p>'
        : news.slice().reverse().map(n => `
          <div class="hl-news-item">
            <div class="hl-news-time">${formatTime(n.time)}</div>
            <div class="hl-news-caption">📰 ${n.caption}</div>
            <div class="hl-news-detail">${n.detail}</div>
          </div>
        `).join("");
    }

    // Update leaderboard
    const lbEl = $("#hl-leaderboard");
    if (lbEl) {
      const lb = state.leaderboard || [];
      lbEl.innerHTML = lb.map((p, i) => `
        <div class="hl-lb-row ${i === 0 ? 'hl-lb-first' : ''}">
          <span class="lb-rank">#${i + 1}</span>
          <span class="lb-name">${p.username}</span>
          <span class="hl-lb-pos" style="color:${p.position > 0 ? '#00b894' : p.position < 0 ? '#ff6b6b' : 'var(--muted)'};">
            ${p.position > 0 ? '+' : ''}${p.position}
          </span>
          <span class="lb-pnl ${p.pnl >= 0 ? 'positive' : 'negative'}">
            ${p.pnl >= 0 ? '+' : ''}${p.pnl.toFixed(2)}
          </span>
        </div>
      `).join("");
    }
  }

  function formatTime(tick) {
    const m = Math.floor(tick / 60);
    const s = tick % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  // ---- PRICE CHART ----
  function initPriceChart(priceHistory) {
    const canvas = document.getElementById("priceChart");
    if (!canvas || typeof Chart === "undefined") return;

    const labels = priceHistory.map((_, i) => i);
    priceChart = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          data: priceHistory,
          borderColor: "#6c5ce7",
          backgroundColor: "#6c5ce733",
          fill: true,
          tension: 0.1,
          pointRadius: 0,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        plugins: { legend: { display: false } },
        scales: {
          x: {
            display: false,
          },
          y: {
            ticks: { color: "#636e72", callback: v => '$' + v.toFixed(0) },
            grid: { color: "#2d3436" },
          },
        },
      },
    });
  }

  function updatePriceChart(priceHistory) {
    if (!priceChart) {
      initPriceChart(priceHistory);
      return;
    }

    const lastPrice = priceHistory[priceHistory.length - 1] || 100;
    const startPrice = priceHistory[0] || 100;
    const color = lastPrice >= startPrice ? "#00b894" : "#ff6b6b";

    priceChart.data.labels = priceHistory.map((_, i) => i);
    priceChart.data.datasets[0].data = priceHistory;
    priceChart.data.datasets[0].borderColor = color;
    priceChart.data.datasets[0].backgroundColor = color + "33";
    priceChart.update();
  }

  // ---- FINISHED ----
  function renderFinished(area, state) {
    const price = state.current_price || 100;
    const startPrice = state.start_price || 100;
    const priceChange = ((price - startPrice) / startPrice * 100).toFixed(2);
    const leaderboard = state.leaderboard || [];

    const lbHTML = leaderboard.map((p, i) => `
      <div class="lb-row ${i === 0 ? 'lb-first' : ''}">
        <span class="lb-rank">#${i + 1}</span>
        <span class="lb-name">${p.username}</span>
        <span class="lb-pnl ${p.pnl >= 0 ? 'positive' : 'negative'}">
          ${p.pnl >= 0 ? '+' : ''}${p.pnl.toFixed(2)}
        </span>
      </div>
    `).join("");

    // Build analysis section
    const allNews = state.all_news || state.news || [];
    const strengthLabel = { strong: "Strong", moderate: "Moderate", weak: "Weak" };
    const strengthColor = { strong: "#e84393", moderate: "#fdcb6e", weak: "#636e72" };

    const analysisHTML = allNews.map(n => {
      const dir = n.impact > 0 ? 'Bullish' : 'Bearish';
      const dirIcon = n.impact > 0 ? '🟢' : '🔴';
      const dirColor = n.impact > 0 ? '#00b894' : '#ff6b6b';
      const str = strengthLabel[n.strength] || 'Moderate';
      const strColor = strengthColor[n.strength] || '#fdcb6e';
      const probPct = n.prob_up ? Math.round(n.prob_up * 100) : '?';
      const probLabel = n.impact > 0
        ? `${probPct}% chance of going up`
        : `${100 - probPct}% chance of going down`;

      return `
        <div class="hl-analysis-card ${n.impact > 0 ? 'bullish' : 'bearish'}">
          <div class="hl-analysis-header">
            <span class="hl-analysis-time">${formatTime(n.time)}</span>
            <span class="hl-analysis-dir" style="color:${dirColor};">${dirIcon} ${dir}</span>
            <span class="hl-analysis-strength" style="background:${strColor};">${str}</span>
          </div>
          <div class="hl-analysis-caption">${n.caption}</div>
          <div class="hl-analysis-detail">${n.detail}</div>
          <div class="hl-analysis-prob">Effect: <strong>${probLabel}</strong> for up to 45s</div>
          <div class="hl-analysis-text">${n.analysis || ''}</div>
        </div>
      `;
    }).join("");

    area.innerHTML = `
      <div class="fiveos-finished">
        <h2>🏆 Game Over!</h2>

        <div class="results-section">
          <h3>Final Price</h3>
          <div class="actuals-grid">
            <div class="actual-item">
              <span class="actual-label">Close</span>
              <span class="actual-val">$${price.toFixed(2)}</span>
            </div>
            <div class="actual-item">
              <span class="actual-label">Change</span>
              <span class="actual-val" style="color:${price >= startPrice ? '#00b894' : '#ff6b6b'};">
                ${priceChange >= 0 ? '+' : ''}${priceChange}%
              </span>
            </div>
          </div>
        </div>

        <div class="results-section">
          <h3>Price Chart</h3>
          <div class="chart-container"><canvas id="finalPriceChart"></canvas></div>
        </div>

        <div class="results-section">
          <h3>🏆 Final Leaderboard</h3>
          <div class="leaderboard">${lbHTML}</div>
        </div>

        <div class="results-section">
          <h3>📊 Event Analysis</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:12px;">
            Each headline shows its direction, strength, and the probability bias it set on the price.
          </p>
          <div class="hl-analysis-list">${analysisHTML}</div>
        </div>

        <a href="/headline" class="btn" style="margin-top: 24px; display: inline-block;">← New Game</a>
      </div>
    `;

    // Draw final chart
    setTimeout(() => {
      const canvas = document.getElementById("finalPriceChart");
      if (!canvas || typeof Chart === "undefined") return;
      const ph = state.price_history || [];
      new Chart(canvas, {
        type: "line",
        data: {
          labels: ph.map((_, i) => i),
          datasets: [{
            data: ph,
            borderColor: ph[ph.length - 1] >= ph[0] ? "#00b894" : "#ff6b6b",
            backgroundColor: (ph[ph.length - 1] >= ph[0] ? "#00b894" : "#ff6b6b") + "33",
            fill: true, tension: 0.1, pointRadius: 0, borderWidth: 2,
          }],
        },
        options: {
          responsive: true,
          animation: { duration: 0 },
          plugins: { legend: { display: false } },
          scales: {
            x: { display: false },
            y: { ticks: { color: "#636e72", callback: v => '$' + v.toFixed(0) }, grid: { color: "#2d3436" } },
          },
        },
      });
    }, 100);
  }

  // ---- Actions ----
  window._lastPos = 0;
  window.currentPos = function () { return window._lastPos; };

  window.updateSliderLabel = function (val) {
    const el = $("#sliderVal");
    if (el) el.textContent = val;
  };

  window.submitTrade = async function (targetPos) {
    targetPos = Math.max(-1000, Math.min(1000, parseInt(targetPos) || 0));
    const msg = $("#tradeMsg");
    try {
      const data = await fetchJSON(`/headline/game/${GAME_ID}/trade`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ position: targetPos }),
      });
      window._lastPos = targetPos;
      if (msg) { msg.textContent = `Position set to ${targetPos}`; msg.style.color = "#00b894"; }
      // Update slider
      const slider = $("#posSlider");
      if (slider) slider.value = targetPos;
      updateSliderLabel(targetPos);
    } catch (e) {
      if (msg) { msg.textContent = String(e.message); msg.style.color = "#ff6b6b"; }
    }
  };

  window.startGame = async function () {
    const btn = document.getElementById("startBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Starting..."; }
    try {
      await fetchJSON(`/headline/game/${GAME_ID}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      lastStateHash = "";
      poll();
    } catch (e) {
      console.error("Start error:", e);
      if (btn) { btn.textContent = "Start Game →"; btn.disabled = false; }
    }
  };

  // ---- Init ----
  getMe().then(me => {
    if (me) {
      window._lastPos = 0;
    }
  });
  startPolling();
})();
