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

    const views = {
      start: $("#clStart"), round: $("#clRound"), final: $("#clFinal"),
      roomLobby: $("#clRoomLobby"),
    };
    const msg = $("#clMsg");
    let gameId = null;
    let answering = false;
    let profile = null;
    // Head-to-head state. `room` is null for solo play, which is what every
    // shared function keys off.
    let room = null;
    let roomPoll = null;

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

    function setDiff(tag) {
      const el = $("#clDiff");
      if (el) el.textContent = tag ? tag : "";
    }

    // ── Making a market ─────────────────────────────────────────────────
    // The player quotes a bid and an ask on the round's statistic. Both sides
    // are draggable on one scale; the payout for the current width is shown
    // live, so the risk/reward of tightening is visible before committing.

    let current = null;   // the round being quoted

    const fmtV = (v, unit) => `${Number(v).toFixed(Math.abs(v) < 10 ? 1 : 0)}${unit || ""}`;
    const posPct = (v) => ((v - current.lo) / (current.hi - current.lo)) * 100;

    function heldPayout(width) {
      const s = current.scoring;
      const w = width / current.spread;
      if (w > s.max_width_units) return 0;
      return Math.round(s.base / (1 + Math.max(0, w)));
    }

    function readQuote() {
      const bid = parseFloat($("#clBid").value);
      const ask = parseFloat($("#clAsk").value);
      return { bid, ask };
    }

    function paintQuote() {
      let { bid, ask } = readQuote();
      if (!isFinite(bid) || !isFinite(ask)) return;
      const band = $("#clBand");
      band.style.left = Math.max(0, Math.min(100, posPct(bid))) + "%";
      band.style.width = Math.max(0, Math.min(100, posPct(ask) - posPct(bid))) + "%";

      const width = Math.max(0, ask - bid);
      const w = width / current.spread;
      const pay = heldPayout(width);
      const tooWide = w > current.scoring.max_width_units;
      $("#clSpreadOut").innerHTML = tooWide
        ? `spread <strong>${fmtV(width, current.unit)}</strong> —
           <span class="cl-warn">too wide to trade, 0 pts</span>`
        : `spread <strong>${fmtV(width, current.unit)}</strong>
           <span class="cl-muted">(${w.toFixed(2)}× these names' spread)</span> ·
           holds for <strong>${pay}</strong> pts`;
    }

    function clampSides(moved) {
      const bidEl = $("#clBid"), askEl = $("#clAsk");
      let bid = parseFloat(bidEl.value), ask = parseFloat(askEl.value);
      if (bid > ask) {
        // Push the other side rather than snapping back, so dragging feels sane.
        if (moved === "bid") askEl.value = bid; else bidEl.value = ask;
      }
      $("#clBidNum").value = bidEl.value;
      $("#clAskNum").value = askEl.value;
      paintQuote();
    }

    function renderRound(round, score, streak, diffTag) {
      answering = false;
      current = round;
      renderProgress(round.index, round.total);
      $("#clRoundNum").textContent = `Round ${round.index + 1} / ${round.total}`;
      $("#clScore").textContent = `${(score || 0).toLocaleString()} pts`;
      $("#clStreak").textContent = streak ? `🔥 ${streak}` : "";
      setDiff(diffTag);
      $("#clQuestion").textContent = round.question;

      const s = round.stock;
      // Open centred on the cohort average with a one-spread-wide market: a
      // reasonable default that shows what a sane starting quote looks like.
      const start = round.cohort_avg;
      const half = round.spread / 2;
      const bid0 = Math.max(round.lo, +(start - half).toFixed(2));
      const ask0 = Math.min(round.hi, +(start + half).toFixed(2));

      $("#clMarket").innerHTML = `
        <div class="cl-mkt-head">
          <div class="cl-mkt-name">
            <span class="cl-stock-ticker">${esc(s.ticker)}</span>
            <span class="cl-stock-name">${esc(s.name)}</span>
            ${s.category ? `<span class="cl-cat">${esc(s.category)}</span>` : ""}
          </div>
          <button type="button" class="btn ghost cl-chart" data-ticker="${esc(s.ticker)}"
            data-name="${esc(s.name)}" data-exchange="${esc(s.exchange)}">View chart ↗</button>
        </div>

        <div class="cl-scale">
          <div class="cl-scale-track">
            <div class="cl-anchor" style="left:${posPct(round.cohort_avg)}%">
              <span class="cl-anchor-tick"></span>
              <span class="cl-anchor-label">all names avg ${fmtV(round.cohort_avg, round.unit)}</span>
            </div>
            <div class="cl-band" id="clBand"></div>
            <div class="cl-truth hidden" id="clTruth"><span class="cl-truth-tick"></span>
              <span class="cl-truth-label" id="clTruthLabel"></span></div>
          </div>
          <div class="cl-scale-ends">
            <span>${fmtV(round.lo, round.unit)}</span>
            <span class="cl-muted">${esc(round.label)}</span>
            <span>${fmtV(round.hi, round.unit)}</span>
          </div>
        </div>

        <div class="cl-quote">
          <label class="cl-side cl-side-bid">
            <span>Your bid</span>
            <input type="number" id="clBidNum" step="${round.step}" min="${round.lo}" max="${round.hi}" value="${bid0}">
          </label>
          <label class="cl-side cl-side-ask">
            <span>Your ask</span>
            <input type="number" id="clAskNum" step="${round.step}" min="${round.lo}" max="${round.hi}" value="${ask0}">
          </label>
        </div>
        <input type="range" class="cl-range cl-range-bid" id="clBid"
          min="${round.lo}" max="${round.hi}" step="${round.step}" value="${bid0}">
        <input type="range" class="cl-range cl-range-ask" id="clAsk"
          min="${round.lo}" max="${round.hi}" step="${round.step}" value="${ask0}">

        <div class="cl-spread-out" id="clSpreadOut"></div>
        <button type="button" class="btn primary cl-quote-btn" id="clQuoteBtn">Quote this market</button>`;

      $("#clBid").addEventListener("input", () => clampSides("bid"));
      $("#clAsk").addEventListener("input", () => clampSides("ask"));
      ["clBidNum", "clAskNum"].forEach((id) => {
        $("#" + id).addEventListener("input", () => {
          const side = id === "clBidNum" ? "clBid" : "clAsk";
          const v = parseFloat($("#" + id).value);
          if (isFinite(v)) $("#" + side).value = Math.max(round.lo, Math.min(round.hi, v));
          clampSides(id === "clBidNum" ? "bid" : "ask");
        });
      });
      $("#clQuoteBtn").addEventListener("click", submitQuote);

      $("#clTruth").classList.add("hidden");
      $("#clReveal").classList.add("hidden");
      $("#clReveal").innerHTML = "";
      $("#clNextBtn").classList.add("hidden");
      paintQuote();
    }

    async function submitQuote() {
      if (answering) return;
      const { bid, ask } = readQuote();
      if (!isFinite(bid) || !isFinite(ask)) { showMsg("Set both sides of your market."); return; }
      answering = true;
      $("#clQuoteBtn").disabled = true;
      ["clBid", "clAsk", "clBidNum", "clAskNum"].forEach((id) => { $("#" + id).disabled = true; });

      let res;
      try {
        res = await postJSON(
          room ? `/crash-ledger/room/${room.room_id}/answer`
               : `/crash-ledger/game/${gameId}/answer`,
          { bid, ask });
      } catch (err) {
        showMsg(err.message);
        answering = false;
        $("#clQuoteBtn").disabled = false;
        ["clBid", "clAsk", "clBidNum", "clAskNum"].forEach((id) => { $("#" + id).disabled = false; });
        return;
      }

      // Drop the true value onto the same scale the player just quoted on.
      const truth = $("#clTruth");
      truth.style.left = Math.max(0, Math.min(100, posPct(res.truth))) + "%";
      $("#clTruthLabel").textContent = fmtV(res.truth, res.unit);
      truth.classList.remove("hidden");
      $("#clBand").classList.add(res.held && res.tradeable ? "cl-band-ok" : "cl-band-bad");

      const reveal = $("#clReveal");
      const good = res.points > 0;
      reveal.className = "cl-reveal " + (good ? "cl-reveal-ok" : "cl-reveal-bad");
      let line;
      if (res.side === "held" && res.tradeable) {
        line = `<strong>Held.</strong> True ${esc(res.label)} was
          <strong>${fmtV(res.truth, res.unit)}</strong>, inside your
          ${fmtV(res.bid, res.unit)} / ${fmtV(res.ask, res.unit)} market. +${res.points} pts`;
      } else if (res.side === "held") {
        line = `<strong>No trade.</strong> True ${esc(res.label)} was
          <strong>${fmtV(res.truth, res.unit)}</strong> — inside your market, but a
          ${fmtV(res.width, res.unit)} spread is too wide for anyone to trade. 0 pts`;
      } else {
        const verb = res.side === "lifted" ? "lifted your offer" : "hit your bid";
        line = `<strong>Picked off.</strong> True ${esc(res.label)} was
          <strong>${fmtV(res.truth, res.unit)}</strong>, so the house ${verb} —
          off by ${fmtV(res.miss, res.unit)}. ${res.points} pts`;
      }
      if (res.milestone) line += ` <span class="cl-milestone">🔥 ${res.milestone} in a row!</span>`;
      reveal.innerHTML = line;
      reveal.classList.remove("hidden");

      $("#clScore").textContent = `${res.score.toLocaleString()} pts`;
      $("#clStreak").textContent = res.streak ? `🔥 ${res.streak}` : "";
      setDiff(res.difficulty_tag);

      if (room && res.standings) renderLive(markMe(res.standings));

      if (res.done) {
        if (room) {
          setTimeout(() => waitForRoom(), 1100);
        } else {
          setTimeout(() => finish(res.final), 1100);
        }
      } else {
        const next = $("#clNextBtn");
        next.classList.remove("hidden");
        next.onclick = () => renderRound(res.round, res.score, res.streak, res.difficulty_tag);
      }
    }

    // ── Head-to-head rooms ──────────────────────────────────────────────
    function stopRoomPoll() {
      if (roomPoll) { clearInterval(roomPoll); roomPoll = null; }
    }

    function renderLive(standings) {
      const el = $("#clLive");
      if (!el) return;
      if (!room || !standings || !standings.length) { el.classList.add("hidden"); return; }
      el.classList.remove("hidden");
      el.innerHTML = standings.map((r) => `
        <span class="cl-live-chip${r.is_me ? " cl-live-me" : ""}">
          <span class="cl-live-rank">${r.rank}</span>
          <span class="cl-live-name">${esc(r.username)}</span>
          <span class="cl-live-score">${r.score.toLocaleString()}</span>
          <span class="cl-live-prog">${r.done ? "done" : `${r.progress}/${r.total}`}</span>
        </span>`).join("");
    }

    function markMe(standings) {
      const myId = room && room.my_id;
      return (standings || []).map((r) => ({ ...r, is_me: r.user_id === myId }));
    }

    function renderRoomPlayers(state) {
      $("#clRoomCodeOut").textContent = state.code;
      const rows = markMe(state.standings);
      $("#clRoomPlayers").innerHTML = `
        <tbody>${rows.map((r) => `
          <tr class="${r.is_me ? "cl-row-me" : ""}">
            <td class="cl-num">${r.rank}</td>
            <td>${esc(r.username)}${r.is_host ? ' <span class="cl-host-tag">host</span>' : ""}</td>
            <td class="cl-num">${state.status === "lobby" ? "ready" : `${r.progress}/${r.total}`}</td>
            <td class="cl-num">${r.score.toLocaleString()}</td>
          </tr>`).join("")}</tbody>`;

      const startBtn = $("#clRoomStart");
      startBtn.classList.toggle("hidden", !(state.is_host && state.status === "lobby"));
      $("#clRoomHint").textContent = state.status === "lobby"
        ? (state.is_host
            ? `Share code ${state.code}. Start when everyone's in.`
            : `Waiting for the host to start. ${rows.length} in the room.`)
        : "Round in progress.";
    }

    async function pollRoom() {
      if (!room) return;
      let state;
      try {
        state = await api(`/crash-ledger/room/${room.room_id}/state`);
      } catch (err) {
        showMsg(err.message);
        stopRoomPoll();
        return;
      }
      room = state;

      if (state.status === "lobby") {
        renderRoomPlayers(state);
        show("roomLobby");
      } else if (state.status === "active") {
        if (state.round) {
          // First time we see an active round, drop into it.
          if (views.round.classList.contains("hidden")) {
            show("round");
            renderRound(state.round, state.me ? state.me.score : 0,
                        state.me ? state.me.streak : 0, "");
          }
          renderLive(markMe(state.standings));
        } else {
          renderLive(markMe(state.standings));
        }
      } else if (state.status === "finished") {
        finishRoom(state);
      }
    }

    function waitForRoom() {
      // This player is done; keep watching until everyone else finishes.
      const reveal = $("#clReveal");
      reveal.className = "cl-reveal cl-reveal-ok";
      reveal.innerHTML = "<strong>All your rounds are in.</strong> Waiting for the rest of the room…";
      reveal.classList.remove("hidden");
      $("#clNextBtn").classList.add("hidden");
    }

    function finishRoom(state) {
      stopRoomPoll();
      const rows = markMe(state.standings);
      const me = rows.find((r) => r.is_me);
      const winner = rows[0];
      $("#clFinalBody").innerHTML = `
        <div class="cl-final-score">${me ? me.score.toLocaleString() : "—"}</div>
        <div class="cl-final-label">your points · room ${esc(state.code)}</div>
        <div class="cl-final-detail">
          ${me ? `${me.correct} of ${me.total} correct` : ""}
          ${winner ? ` &middot; winner <strong>${esc(winner.username)}</strong> on ${winner.score.toLocaleString()}` : ""}
        </div>
        <table class="cl-board cl-final-board"><tbody>
          ${rows.map((r) => `
            <tr class="${r.is_me ? "cl-row-me" : ""}">
              <td class="cl-num">${r.rank}</td>
              <td>${esc(r.username)}</td>
              <td class="cl-num">${r.correct}/${r.total} held</td>
              <td class="cl-num">${r.score.toLocaleString()}</td>
            </tr>`).join("")}
        </tbody></table>
        <button class="btn primary" id="clPlayAgain">Back to Crash Call</button>`;
      $("#clPlayAgain").addEventListener("click", () => {
        room = null;
        show("start");
        loadProfile();
        loadBoard();
      });
      show("final");
    }

    async function enterRoom(state) {
      room = state;
      stopRoomPoll();
      renderRoomPlayers(state);
      show("roomLobby");
      roomPoll = setInterval(pollRoom, 2000);
    }

    async function createRoom() {
      msg.classList.add("hidden");
      try {
        enterRoom(await postJSON("/crash-ledger/room/create", {}));
      } catch (err) {
        showMsg(err.status === 401 ? "Please log in to open a room." : err.message);
      }
    }

    async function joinRoom() {
      const code = ($("#clRoomCode").value || "").trim().toUpperCase();
      if (!code) { showMsg("Enter a room code."); return; }
      msg.classList.add("hidden");
      try {
        enterRoom(await postJSON("/crash-ledger/room/join", { code }));
      } catch (err) {
        showMsg(err.status === 401 ? "Please log in to join a room." : err.message);
      }
    }

    async function startRoom() {
      if (!room) return;
      try {
        const state = await postJSON(`/crash-ledger/room/${room.room_id}/start`, {});
        room = state;
        if (state.round) {
          show("round");
          renderRound(state.round, 0, 0, "");
          renderLive(markMe(state.standings));
        }
      } catch (err) {
        showMsg(err.message);
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
        <div class="cl-final-detail">${f.correct} of ${f.total} markets held &middot; best run 🔥 ${f.best_streak}
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
    $("#clRoomCreate")?.addEventListener("click", createRoom);
    $("#clRoomJoin")?.addEventListener("click", joinRoom);
    $("#clRoomCode")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") joinRoom();
    });
    $("#clRoomStart")?.addEventListener("click", startRoom);
    $("#clRoomLeave")?.addEventListener("click", () => {
      stopRoomPoll();
      room = null;
      show("start");
    });
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
