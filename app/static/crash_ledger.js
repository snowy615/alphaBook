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
      const symbol = encodeURIComponent(exchange ? `${exchange}:${ticker}` : ticker);
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
    if (!$("#clGame")) return;

    const views = { start: $("#clStart"), round: $("#clRound"), final: $("#clFinal") };
    const msg = $("#clMsg");
    let gameId = null;
    let answering = false;
    let profile = null;

    const TIER_ICON = { bronze: "🥉", silver: "🥈", gold: "🥇", platinum: "💠", diamond: "💎" };

    function show(view) {
      Object.entries(views).forEach(([k, el]) => el.classList.toggle("hidden", k !== view));
    }
    function showMsg(text) {
      if (!msg) return;
      msg.textContent = text;
      msg.classList.remove("hidden");
    }

    // ── Start-screen player card (level bar, tier, streak) ──────────────
    function renderPlayer(p) {
      const el = $("#clPlayer");
      if (!el) return;
      const tier = p.tier || { key: "bronze", label: "Bronze", color: "#b08d57" };
      const pctInto = Math.round((p.level_pct || 0) * 100);
      const streakLine = p.day_streak > 0
        ? `<span class="cl-streak-flame">🔥 ${p.day_streak}-day streak</span>`
        : `<span class="msp-muted">Play daily to start a streak</span>`;
      const dailyLine = p.daily_available
        ? `<span class="cl-daily-badge">Daily bonus ready →</span>` : "";
      el.innerHTML = `
        <div class="cl-player-main">
          <span class="cl-tier-pill" style="--tier:${tier.color}">${TIER_ICON[tier.key] || "🏅"} ${esc(tier.label)}</span>
          <div class="cl-level-block">
            <div class="cl-level-row"><strong>Level ${p.level}</strong>
              <span class="msp-muted">${p.level_into} / ${p.level_span} XP</span></div>
            <div class="cl-xpbar"><div class="cl-xpfill" style="width:${pctInto}%"></div></div>
          </div>
        </div>
        <div class="cl-player-meta">
          ${streakLine} ${dailyLine}
          <span class="msp-muted">Best ${(p.best_score || 0).toLocaleString()} &middot; ${p.games_played || 0} games</span>
        </div>`;
    }

    function renderDaily(goal) {
      const el = $("#clDaily");
      if (!el || !goal) return;
      const pct = Math.min(100, Math.round((goal.current / goal.goal) * 100));
      el.innerHTML = `
        <div class="cl-daily-head">
          <span>🤝 Today's desk goal</span>
          <span class="msp-muted">${goal.current.toLocaleString()} / ${goal.goal.toLocaleString()} correct calls${goal.reached ? " · reached! 🎉" : ""}</span>
        </div>
        <div class="cl-goalbar"><div class="cl-goalfill${goal.reached ? " cl-goalfill-done" : ""}" style="width:${pct}%"></div></div>`;
    }

    async function loadProfile() {
      try {
        profile = await api("/crash-ledger/profile");
        renderPlayer(profile);
        renderDaily(profile.daily_goal);
      } catch (err) {
        const el = $("#clPlayer");
        if (el) el.innerHTML = `<span class="msp-muted">${err.status === 401 ? "Log in to track XP, streaks and leagues." : esc(err.message)}</span>`;
      }
    }

    // ── Tiered leaderboard ──────────────────────────────────────────────
    async function loadBoard() {
      const table = $("#clBoard");
      const badge = $("#clTierBadge");
      if (!table) return;
      try {
        const data = await api("/crash-ledger/leaderboard");
        if (badge && data.tier) {
          badge.textContent = `${TIER_ICON[data.tier.key] || "🏅"} ${data.tier.label} league`;
          badge.style.setProperty("--tier", data.tier.color);
        }
        const rows = data.rows || [];
        if (!rows.length) {
          table.innerHTML = `<tbody><tr><td class="msp-muted">No one in this league yet — be the first.</td></tr></tbody>`;
          return;
        }
        table.innerHTML = `
          <thead><tr><th>#</th><th>Player</th><th class="cl-num">XP</th><th class="cl-num">Lvl</th><th class="cl-num">Best</th></tr></thead>
          <tbody>${rows.map((r) => `
            <tr class="${r.is_me ? "cl-me-row" : ""}">
              <td>${r.rank}</td>
              <td>${esc(r.username)}${r.is_me ? " (you)" : ""}</td>
              <td class="cl-num">${r.xp.toLocaleString()}</td>
              <td class="cl-num">${r.level}</td>
              <td class="cl-num">${r.best_score.toLocaleString()}</td>
            </tr>`).join("")}</tbody>`;
      } catch (err) {
        table.innerHTML = `<tbody><tr><td class="msp-muted">${esc(err.message)}</td></tr></tbody>`;
      }
    }

    // ── Round rendering ─────────────────────────────────────────────────
    function renderProgress(index, total) {
      const el = $("#clProgress");
      if (!el) return;
      // Endowed head start: the first segment reads as already underway.
      el.innerHTML = Array.from({ length: total }, (_, i) => {
        let cls = "cl-seg";
        if (i < index) cls += " cl-seg-done";
        else if (i === index) cls += " cl-seg-now";
        return `<span class="${cls}"></span>`;
      }).join("");
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

    function setDiff(tag) {
      const el = $("#clDiff");
      if (el) el.textContent = tag ? tag : "";
    }

    function renderRound(round, score, streak, diffTag) {
      answering = false;
      renderProgress(round.index, round.total);
      $("#clRoundNum").textContent = `Round ${round.index + 1} / ${round.total}`;
      $("#clScore").textContent = `${(score || 0).toLocaleString()} pts`;
      $("#clStreak").textContent = streak ? `🔥 ${streak}` : "";
      setDiff(diffTag);
      $("#clQuestion").textContent = round.question;
      $("#clDuel").innerHTML = stockCard("a", round.a) + stockCard("b", round.b);
      $("#clReveal").classList.add("hidden");
      $("#clReveal").innerHTML = "";
      $("#clNextBtn").classList.add("hidden");
      $("#clDuel").querySelectorAll(".cl-pick").forEach((btn) =>
        btn.addEventListener("click", () => submitPick(btn.dataset.side, round.label)));
    }

    function pointPop(card, points) {
      const pop = document.createElement("div");
      pop.className = "cl-pop";
      pop.textContent = `+${points}`;
      card.appendChild(pop);
      setTimeout(() => pop.remove(), 1100);
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
      if (res.correct) pointPop(duel.querySelector(`.cl-stock[data-side="${pick}"]`), res.points);

      const reveal = $("#clReveal");
      reveal.className = "cl-reveal " + (res.correct ? "cl-reveal-ok" : "cl-reveal-bad");
      let line = res.correct
        ? `<strong>Correct.</strong> +${res.points} pts${res.streak > 1 ? ` &middot; 🔥 ${res.streak} in a row` : ""}`
        : `<strong>Missed it.</strong> The answer was ${res.answer.toUpperCase()}.`;
      if (res.milestone) line += ` <span class="cl-milestone">🔥 ${res.milestone}-streak!</span>`;
      reveal.innerHTML = line;
      reveal.classList.remove("hidden");
      $("#clScore").textContent = `${res.score.toLocaleString()} pts`;
      $("#clStreak").textContent = res.streak ? `🔥 ${res.streak}` : "";
      setDiff(res.difficulty_tag);

      if (res.done) {
        setTimeout(() => finish(res.final), 950);
      } else {
        const next = $("#clNextBtn");
        next.classList.remove("hidden");
        next.onclick = () => renderRound(res.round, res.score, res.streak, res.difficulty_tag);
      }
    }

    // ── Final screen ────────────────────────────────────────────────────
    function finish(f) {
      const bits = [];
      if (f.leveled_up) bits.push(`<div class="cl-final-flag cl-flag-level">⬆️ Level up! You're Level ${f.level}</div>`);
      if (f.new_best) bits.push(`<div class="cl-final-flag cl-flag-best">🏆 New personal best</div>`);
      if (f.daily_bonus) bits.push(`<div class="cl-final-flag cl-flag-daily">🔥 Day ${f.day_streak} · +${f.daily_bonus} daily bonus</div>`);

      const pctInto = Math.round((f.level_pct || 0) * 100);
      const levelBar = f.level != null ? `
        <div class="cl-final-level">
          <div class="cl-level-row"><strong>Level ${f.level}</strong>
            <span class="msp-muted">${f.level_into} / ${f.level_span} XP</span></div>
          <div class="cl-xpbar"><div class="cl-xpfill" style="width:${pctInto}%"></div></div>
        </div>` : "";

      $("#clFinalBody").innerHTML = `
        <div class="cl-final-score" id="clFinalScore">${(f.score || 0).toLocaleString()}</div>
        <div class="cl-final-label">points</div>
        <div class="cl-final-detail">${f.correct} of ${f.total} correct &middot; best streak 🔥 ${f.best_streak}
          ${f.xp_earned ? ` &middot; <strong>+${f.xp_earned} XP</strong>` : ""}</div>
        <div class="cl-final-flags">${bits.join("")}</div>
        ${levelBar}
        <button class="btn primary" id="clPlayAgain">Play again</button>`;
      $("#clPlayAgain").addEventListener("click", () => { startGame(); });
      show("final");
      loadProfile();
      loadBoard();
    }

    async function startGame() {
      const btn = $("#clPlayBtn");
      if (btn) btn.disabled = true;
      msg.classList.add("hidden");
      try {
        const res = await postJSON("/crash-ledger/game/start", {});
        gameId = res.game_id;
        show("round");
        renderRound(res.round, 0, 0, res.difficulty_tag);
      } catch (err) {
        showMsg(err.status === 401 ? "Please log in to play." : err.message);
        show("start");
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    $("#clPlayBtn").addEventListener("click", startGame);
    loadProfile();
    loadBoard();
  })();

  // ---- Full universe browser ----
  // Renders the remaining constituents with search and a cohort filter. Every
  // name is a [data-ticker] button, so the delegated handler above opens the
  // same chart the curated cards use — no second modal.
  (function () {
    const DATA = window.UNIVERSE || { cohorts: [], stocks: [] };
    const STOCKS = DATA.stocks || [];
    const COHORTS = DATA.cohorts || [];
    const root = $("#uniRoot");
    if (!root || !STOCKS.length) return;

    const chips = $("#uniChips");
    const empty = $("#uniEmpty");
    const countEl = $("#uniCount");
    const search = $("#uniSearch");
    const state = { query: "", cohort: "all" };

    const counts = COHORTS.reduce((acc, c) => {
      acc[c] = STOCKS.filter((s) => s.c === c).length;
      return acc;
    }, {});

    const label = (c) => c.replace(/([a-z])([A-Z])/g, "$1 $2");

    function matches() {
      const q = state.query.trim().toUpperCase();
      return STOCKS.filter((s) => {
        if (state.cohort !== "all" && s.c !== state.cohort) return false;
        if (!q) return true;
        return s.t.toUpperCase().includes(q) || s.n.toUpperCase().includes(q);
      });
    }

    function renderChips() {
      chips.innerHTML =
        `<button class="uni-chip ${state.cohort === "all" ? "is-on" : ""}"
          data-cohort="all" type="button">All <span>${STOCKS.length}</span></button>` +
        COHORTS.map((c) => `
          <button class="uni-chip ${state.cohort === c ? "is-on" : ""}"
            data-cohort="${esc(c)}" type="button">${esc(label(c))} <span>${counts[c]}</span></button>`
        ).join("");
      chips.querySelectorAll("[data-cohort]").forEach((b) => {
        b.addEventListener("click", () => { state.cohort = b.dataset.cohort; render(); });
      });
    }

    function render() {
      const rows = matches();
      countEl.textContent = rows.length === STOCKS.length
        ? `${STOCKS.length} names`
        : `${rows.length} of ${STOCKS.length}`;
      empty.classList.toggle("hidden", rows.length > 0);

      const grouped = new Map();
      rows.forEach((s) => {
        if (!grouped.has(s.c)) grouped.set(s.c, []);
        grouped.get(s.c).push(s);
      });

      root.innerHTML = COHORTS.filter((c) => grouped.has(c)).map((c) => {
        const names = grouped.get(c);
        return `<div class="uni-group">
          <div class="uni-group-head">
            <h3>${esc(label(c))}</h3>
            <span>${names.length} name${names.length === 1 ? "" : "s"}</span>
          </div>
          <div class="uni-grid">${names.map((s) => `
            <button class="uni-name" type="button"
              data-ticker="${esc(s.t)}" data-name="${esc(s.n)}">
              <span class="uni-ticker">${esc(s.t)}</span>
              <span class="uni-company">${esc(s.n)}</span>
            </button>`).join("")}</div>
        </div>`;
      }).join("");

      renderChips();
    }

    let debounce = null;
    search.addEventListener("input", () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => { state.query = search.value; render(); }, 90);
    });

    $("#uniClear").addEventListener("click", () => {
      state.query = "";
      state.cohort = "all";
      search.value = "";
      render();
      search.focus();
    });

    render();
  })();

})();
