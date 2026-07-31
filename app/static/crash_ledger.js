/* Crash Ledger — "Crash Call" game + the ticker tape + the chart modal.
 * The chart modal (the preserved graph-analysis view) is opened via event
 * delegation, so it works for both the static reference cards and the stock
 * cards the game injects at runtime. The embedded chart follows the site theme.
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
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  const pct = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(1) + "%");

  // ---- topbar user ----
  (async function () {
    try {
      const me = await api("/me");
      const el = $("#userName");
      if (el) el.textContent = me.username || me.name || "user";
    } catch (e) { /* not signed in */ }
  })();

  // ---- scrolling ticker tape ----
  (function () {
    const tapeData = [
      { t: "CVNA", name: "Carvana Co.", v: -52.6 }, { t: "UAL", name: "United Airlines Holdings, Inc.", v: -55.9 },
      { t: "LVS", name: "Las Vegas Sands Corp.", v: -33.5 }, { t: "BLDR", name: "Builders FirstSource, Inc.", v: -43.1 },
      { t: "NCLH", name: "Norwegian Cruise Line Holdings Ltd.", v: -57.3 }, { t: "APO", name: "Apollo Global Management, Inc.", v: -10.2 },
      { t: "COIN", name: "Coinbase Global, Inc.", v: -85.9 }, { t: "AKAM", name: "Akamai Technologies, Inc.", v: -42.6 },
      { t: "EQIX", name: "Equinix, Inc.", v: -36.2 }, { t: "SBAC", name: "SBA Communications Corporation", v: -67.1 },
    ];
    const track = $("#tapeTrack");
    if (!track) return;
    const renderSet = () => tapeData.map((d) => (
      `<span class="tape-item"><span class="t-ticker">${esc(d.t)}</span>${esc(d.name)}` +
      `<span class="${d.v >= 0 ? "t-pos" : "t-neg"}">${d.v >= 0 ? "+" : ""}${d.v.toFixed(1)}%</span></span>`
    )).join("");
    track.innerHTML = renderSet() + renderSet();
  })();

  // ---- chart modal (graph analysis) — delegated so dynamic buttons work ----
  (function () {
    const backdrop = $("#chartBackdrop");
    const chartHost = $("#chartHost");
    const mhTicker = $("#mhTicker");
    const mhName = $("#mhName");
    const mhYahoo = $("#mhYahoo");
    const mhClose = $("#mhClose");
    if (!backdrop) return;

    function chartTheme() {
      return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }
    function openChart(ticker, name, exchange) {
      mhTicker.textContent = ticker;
      mhName.textContent = name || "";
      mhYahoo.href = `https://finance.yahoo.com/quote/${encodeURIComponent(ticker)}/`;
      backdrop.classList.add("open");
      document.body.style.overflow = "hidden";
      const symbol = encodeURIComponent(`${exchange || "NASDAQ"}:${ticker}`);
      const theme = chartTheme();
      const bg = theme === "light" ? "ffffff" : "111522";
      const src = `https://s.tradingview.com/widgetembed/?details=0&hide_side_toolbar=1&interval=D` +
        `&save_image=0&studies=%5B%5D&style=1&symbol=${symbol}&theme=${theme}&toolbar_bg=${bg}` +
        `&timezone=exchange&withdateranges=1&hideideas=1&symboledit=0&locale=en`;
      chartHost.innerHTML = `<iframe title="TradingView chart for ${esc(ticker)}" src="${src}"
        style="width:100%;height:100%;border:0;display:block;position:absolute;inset:0;"
        loading="eager" referrerpolicy="strict-origin-when-cross-origin" allow="fullscreen"></iframe>`;
    }
    function closeChart() {
      backdrop.classList.remove("open");
      document.body.style.overflow = "";
      chartHost.innerHTML = "";
    }

    // Any element carrying data-ticker (reference cards use .ticker; game uses
    // .cl-chart) opens the chart. Delegation covers runtime-injected buttons.
    document.addEventListener("click", (e) => {
      const el = e.target.closest("[data-ticker]");
      if (el && backdrop.contains(el) === false) {
        openChart(el.dataset.ticker, el.dataset.name, el.dataset.exchange);
      }
    });
    mhClose.addEventListener("click", closeChart);
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeChart(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeChart(); });
  })();

  // ---- Crash Call game ----
  (function () {
    const game = $("#clGame");
    if (!game) return;

    const views = { start: $("#clStart"), round: $("#clRound"), final: $("#clFinal") };
    const msg = $("#clMsg");
    let gameId = null;
    let total = 10;
    let answering = false;

    function show(view) {
      Object.entries(views).forEach(([k, el]) => el.classList.toggle("hidden", k !== view));
    }
    function showMsg(text) {
      if (!msg) return;
      msg.textContent = text;
      msg.classList.remove("hidden");
    }

    async function loadBoard() {
      const table = $("#clBoard");
      if (!table) return;
      try {
        const { leaderboard } = await api("/crash-ledger/leaderboard");
        if (!leaderboard.length) {
          table.innerHTML = `<tbody><tr><td class="msp-muted">No scores yet — be the first.</td></tr></tbody>`;
          return;
        }
        table.innerHTML = `
          <thead><tr><th>#</th><th>Player</th><th class="cl-num">Score</th><th class="cl-num">Best streak</th></tr></thead>
          <tbody>${leaderboard.map((r) => `
            <tr class="${r.is_me ? "cl-me-row" : ""}">
              <td>${r.rank}</td>
              <td>${esc(r.username)}${r.is_me ? " (you)" : ""}</td>
              <td class="cl-num">${r.score.toLocaleString()}</td>
              <td class="cl-num">${r.best_streak}</td>
            </tr>`).join("")}</tbody>`;
      } catch (err) {
        table.innerHTML = `<tbody><tr><td class="msp-muted">${esc(err.message)}</td></tr></tbody>`;
      }
    }

    function stockCard(side, s) {
      return `
        <div class="cl-stock" data-side="${side}">
          <div class="cl-stock-top">
            <span class="cl-stock-ticker">${esc(s.ticker)}</span>
            <span class="cl-stock-name">${esc(s.name)}</span>
          </div>
          <div class="cl-stock-actions">
            <button type="button" class="btn ghost cl-chart" data-ticker="${esc(s.ticker)}"
              data-name="${esc(s.name)}" data-exchange="${esc(s.exchange)}">View chart ↗</button>
            <button type="button" class="btn cl-pick" data-side="${side}">Call it</button>
          </div>
          <div class="cl-stock-value" data-value></div>
        </div>`;
    }

    function renderRound(round, score, streak) {
      answering = false;
      $("#clRoundNum").textContent = `Round ${round.index + 1} / ${round.total}`;
      $("#clScore").textContent = `${(score || 0).toLocaleString()} pts`;
      $("#clStreak").textContent = streak ? `🔥 ${streak}` : "";
      $("#clQuestion").textContent = round.question;
      $("#clDuel").innerHTML = stockCard("a", round.a) + stockCard("b", round.b);
      $("#clReveal").classList.add("hidden");
      $("#clReveal").innerHTML = "";
      $("#clNextBtn").classList.add("hidden");
      $("#clDuel").querySelectorAll(".cl-pick").forEach((btn) =>
        btn.addEventListener("click", () => submitPick(btn.dataset.side, round.label)));
    }

    async function submitPick(pick, label) {
      if (answering) return;
      answering = true;
      $("#clDuel").querySelectorAll(".cl-pick").forEach((b) => { b.disabled = true; });
      let res;
      try {
        res = await postJSON(`/crash-ledger/game/${gameId}/answer`, { pick });
      } catch (err) {
        showMsg(err.message);
        answering = false;
        return;
      }

      // reveal the metric value on each side and mark right/wrong
      const duel = $("#clDuel");
      const map = { a: res.a_value, b: res.b_value };
      ["a", "b"].forEach((side) => {
        const card = duel.querySelector(`.cl-stock[data-side="${side}"]`);
        const val = card.querySelector("[data-value]");
        val.textContent = `${label}: ${pct(map[side])}`;
        val.classList.add("shown");
        if (side === res.answer) card.classList.add("cl-correct");
        else if (side === pick) card.classList.add("cl-wrong");
      });

      const reveal = $("#clReveal");
      reveal.className = "cl-reveal " + (res.correct ? "cl-reveal-ok" : "cl-reveal-bad");
      reveal.innerHTML = res.correct
        ? `<strong>Correct.</strong> +${res.points} pts${res.streak > 1 ? ` &middot; 🔥 ${res.streak} streak` : ""}`
        : `<strong>Missed it.</strong> The answer was ${res.answer.toUpperCase()}.`;
      $("#clScore").textContent = `${res.score.toLocaleString()} pts`;
      $("#clStreak").textContent = res.streak ? `🔥 ${res.streak}` : "";

      if (res.done) {
        setTimeout(() => finish(res.final), 900);
      } else {
        const next = $("#clNextBtn");
        next.classList.remove("hidden");
        next.onclick = () => renderRound(res.round, res.score, res.streak);
      }
    }

    function finish(final) {
      $("#clFinalScore").textContent = final.score.toLocaleString();
      $("#clFinalDetail").innerHTML =
        `${final.correct} of ${final.total} correct &middot; best streak 🔥 ${final.best_streak}`;
      show("final");
      loadBoard();
    }

    async function startGame() {
      $("#clPlayBtn").disabled = true;
      $("#clPlayAgain").disabled = true;
      msg.classList.add("hidden");
      try {
        const res = await postJSON("/crash-ledger/game/start", {});
        gameId = res.game_id;
        total = res.total;
        show("round");
        renderRound(res.round, 0, 0);
      } catch (err) {
        showMsg(err.status === 401 ? "Please log in to play." : err.message);
        show("start");
      } finally {
        $("#clPlayBtn").disabled = false;
        $("#clPlayAgain").disabled = false;
      }
    }

    $("#clPlayBtn").addEventListener("click", startGame);
    $("#clPlayAgain").addEventListener("click", () => { show("start"); startGame(); });
    loadBoard();
  })();
})();
