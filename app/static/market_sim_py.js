/* Market Simulation Py — lobby + live run (client-side execution model).
 *
 * Players run their strategy on their own machine and trade over the API; this
 * page is the lobby, the "connect your bot" panel, and the live leaderboard.
 * window.RUN_ID is only defined on the run page.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);

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

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

  const money = (v) => (v == null ? "—" : (v < 0 ? "-" : "") + "$" + Math.abs(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }));

  const px = (v) => (v == null ? "—" : Number(v).toFixed(2));

  function showMsg(el, text, kind) {
    if (!el) return;
    el.textContent = text;
    el.className = "msp-msg msp-msg-" + (kind || "info");
  }

  async function initUser() {
    try {
      const me = await api("/me");
      const el = $("#userName");
      if (el) el.textContent = me.username || me.name || "user";
    } catch {
      window.location.href = "/login";
    }
  }

  // ── Rules / lobby page (create, join, browse) ─────────────────────────
  function initLobbyPage() {
    const msg = $("#lobbyMsg");

    $("#createBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const name = $("#runName")?.value.trim() || "Market Simulation Py";
        const res = await postJSON("/market-sim-py/create", { name });
        window.location.href = `/market-sim-py/run/${res.run_id}`;
      } catch (err) {
        showMsg(msg, err.message, "error");
        e.target.disabled = false;
      }
    });

    $("#joinBtn")?.addEventListener("click", async (e) => {
      const code = ($("#joinCode")?.value || "").trim().toUpperCase();
      if (code.length !== 6) {
        showMsg(msg, "Join codes are six characters.", "error");
        return;
      }
      e.target.disabled = true;
      try {
        const res = await postJSON("/market-sim-py/join", { join_code: code });
        window.location.href = `/market-sim-py/run/${res.run_id}`;
      } catch (err) {
        showMsg(msg, err.message, "error");
        e.target.disabled = false;
      }
    });

    $("#joinCode")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("#joinBtn")?.click();
    });

    async function loadOpenRuns() {
      const box = $("#openRuns");
      if (!box) return;
      try {
        const { runs } = await api("/market-sim-py/open");
        $("#openCount").textContent = runs.length ? `${runs.length} open` : "";
        if (!runs.length) {
          box.innerHTML = `<p class="msp-muted">No runs open right now — create one above.</p>`;
          return;
        }
        box.innerHTML = runs.map((r) => `
          <a class="msp-run-row" href="/market-sim-py/run/${encodeURIComponent(r.run_id)}">
            <span class="msp-run-name">${esc(r.name)}</span>
            <span class="msp-code">${esc(r.join_code)}</span>
            <span class="msp-muted">${r.players} player${r.players === 1 ? "" : "s"}</span>
            <span class="msp-pill ${r.status === "running" ? "msp-pill-live" : ""}">${esc(r.status)}</span>
            <span class="msp-muted">${r.joined ? "joined" : ""}</span>
          </a>`).join("");
      } catch (err) {
        box.innerHTML = `<p class="msp-muted">${esc(err.message)}</p>`;
      }
    }

    loadOpenRuns();
    setInterval(loadOpenRuns, 5000);
  }

  // ── The connect-your-bot panel ────────────────────────────────────────
  // Rendered from a <template> into both the lobby and the live view. Fetches
  // the player's API token once and wires the copy / reveal buttons.
  function mountConnectPanel(container, runId, creds) {
    if (!container || container.dataset.mounted) return;
    const tpl = $("#connectTemplate");
    if (!tpl) return;
    container.appendChild(tpl.content.cloneNode(true));
    container.dataset.mounted = "1";

    $(".js-base", container).textContent = creds.base;
    $(".js-run", container).textContent = creds.runId;
    const tokenEl = $(".js-token", container);
    const realToken = creds.token;
    let revealed = false;

    $(".js-reveal", container)?.addEventListener("click", (e) => {
      revealed = !revealed;
      tokenEl.textContent = revealed ? realToken : "••••••••••••••••";
      e.target.textContent = revealed ? "Hide" : "Reveal";
    });

    container.querySelectorAll(".msp-copy").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const target = btn.dataset.copy;
        const value = target === "js-token" ? realToken : $("." + target, container).textContent;
        try {
          await navigator.clipboard.writeText(value);
          const was = btn.textContent;
          btn.textContent = "Copied";
          setTimeout(() => { btn.textContent = was; }, 1200);
        } catch { /* clipboard blocked; user can select manually */ }
      });
    });
  }

  // ── Run page ──────────────────────────────────────────────────────────
  function initRunPage(runId) {
    const msg = $("#msg");
    let lastStatus = null;
    let pollTimer = null;
    let creds = null;
    const pnlHistory = [];   // {tick, pnl} points for the live chart

    async function ensureCreds() {
      if (creds) return creds;
      const res = await api(`/market-sim-py/run/${runId}/token`);
      creds = { base: window.location.origin, runId, token: res.token };
      return creds;
    }

    async function mountConnectInto(sel) {
      try {
        const c = await ensureCreds();
        mountConnectPanel($(sel), runId, c);
      } catch (err) {
        showMsg(msg, "Could not load your bot token: " + err.message, "error");
      }
    }

    $("#startBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        await postJSON(`/market-sim-py/run/${runId}/start`, {});
        poll();
      } catch (err) {
        showMsg(msg, err.message, "error");
      } finally {
        e.target.disabled = false;
      }
    });

    $("#stopBtn")?.addEventListener("click", async (e) => {
      if (!window.confirm("End the run now and score it as it stands?")) return;
      e.target.disabled = true;
      try {
        await postJSON(`/market-sim-py/run/${runId}/stop`, {});
        poll();
      } catch (err) {
        showMsg(msg, err.message, "error");
        e.target.disabled = false;
      }
    });

    // -- rendering -------------------------------------------------------
    function renderClock(seconds) {
      const el = $("#runClock");
      if (!el) return;
      el.classList.remove("hidden");
      const s = Math.max(0, Math.round(seconds));
      el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      el.classList.toggle("msp-clock-low", s <= 60);
    }

    function renderPlayers(players) {
      const box = $("#playerList");
      if (!box) return;
      $("#playerCount").textContent = `${players.length} joined`;
      box.innerHTML = players.map((p) => `
        <div class="msp-player">
          <span>${esc(p.username)}</span>
          <span class="msp-pill ${p.connected ? "msp-pill-ok" : ""}">${p.connected ? "bot live" : "not connected"}</span>
        </div>`).join("") || `<p class="msp-muted">Nobody has joined yet.</p>`;
    }

    function renderLeaderboard(rows) {
      const table = $("#leaderboard");
      if (!table) return;
      table.innerHTML = `
        <thead><tr><th>#</th><th>Player</th><th class="msp-num">P&amp;L</th>
        <th class="msp-num">Fills</th><th></th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="${r.is_bot ? "msp-bot-row" : ""}">
            <td>${r.rank}</td>
            <td>${esc(r.username)}${r.is_bot ? ' <span class="msp-tag">bot</span>' : ""}</td>
            <td class="msp-num ${r.pnl >= 0 ? "msp-up" : "msp-down"}">${money(r.pnl)}</td>
            <td class="msp-num">${r.fills}</td>
            <td>${!r.is_bot && r.connected ? '<span class="msp-dot" title="bot connected"></span>' : ""}</td>
          </tr>`).join("")}</tbody>`;
    }

    function renderMarket(items, revealed) {
      const table = $("#marketTable");
      if (!table) return;
      $("#fairNote").textContent = revealed ? "fair values revealed" : "fair values hidden while live";
      table.innerHTML = `
        <thead><tr><th>Item</th><th class="msp-num">Bid</th><th class="msp-num">Ask</th>
        <th class="msp-num">Last</th>${revealed ? '<th class="msp-num">Fair</th>' : ""}</tr></thead>
        <tbody>${items.map((it) => `
          <tr>
            <td><strong>${esc(it.item)}</strong><div class="msp-muted msp-sub">${esc(it.name)}</div></td>
            <td class="msp-num msp-up">${px(it.bid)}</td>
            <td class="msp-num msp-down">${px(it.ask)}</td>
            <td class="msp-num">${px(it.last)}</td>
            ${revealed ? `<td class="msp-num">${px(it.fair)}</td>` : ""}
          </tr>`).join("")}</tbody>`;
    }

    function renderMe(me, items, runStatus) {
      const stats = $("#myStats");
      const positions = $("#myPositions");
      const status = $("#myStatus");
      if (!me) {
        if (stats) stats.innerHTML = `<p class="msp-muted">You are watching this run, not trading it.</p>`;
        if (positions) positions.innerHTML = "";
        if (status) { status.textContent = "spectator"; status.className = "msp-pill"; }
        return;
      }

      if (status) {
        const done = runStatus === "finished";
        status.textContent = done ? "final" : "trading";
        status.className = "msp-pill " + (done ? "" : "msp-pill-ok");
      }

      if (stats) {
        stats.innerHTML = `
          <div class="msp-stat"><span class="msp-stat-label">P&amp;L</span>
            <span class="msp-stat-value ${me.pnl >= 0 ? "msp-up" : "msp-down"}">${money(me.pnl)}</span></div>
          <div class="msp-stat"><span class="msp-stat-label">Cash</span>
            <span class="msp-stat-value">${money(me.cash)}</span></div>
          <div class="msp-stat"><span class="msp-stat-label">Fills</span>
            <span class="msp-stat-value">${me.fills}</span></div>
          <div class="msp-stat"><span class="msp-stat-label">Orders</span>
            <span class="msp-stat-value">${me.orders_accepted}
              ${me.orders_rejected ? `<span class="msp-down">/ ${me.orders_rejected} rej</span>` : ""}</span></div>`;
      }

      if (positions) {
        const limit = window.POSITION_LIMIT || 1000;
        const restingNote = (o) => {
          const parts = [];
          if (o && o.buy) parts.push(`${o.buy} bid`);
          if (o && o.sell) parts.push(`${o.sell} ask`);
          return parts.length ? `<div class="msp-muted msp-sub">${parts.join(" · ")} resting</div>` : "";
        };
        positions.innerHTML = `
          <thead><tr><th>Item</th><th class="msp-num">Position</th><th>Limit use</th></tr></thead>
          <tbody>${items.map((it) => {
            const q = me.positions[it.item] || 0;
            const pct = Math.min(100, Math.round((Math.abs(q) / limit) * 100));
            return `<tr>
              <td>${esc(it.item)}${restingNote(me.open_orders && me.open_orders[it.item])}</td>
              <td class="msp-num ${q > 0 ? "msp-up" : q < 0 ? "msp-down" : ""}">${q}</td>
              <td><div class="msp-meter"><div class="msp-meter-fill${pct >= 95 ? " msp-meter-full" : ""}"
                style="width:${pct}%"></div></div></td>
            </tr>`;
          }).join("")}</tbody>`;
      }

      if (me.last_reject) {
        // surface the most recent rejection so a broken bot is diagnosable
        const s = $("#myStatus");
        if (s) s.title = "last rejected order: " + me.last_reject;
      }
    }

    // A dependency-free moving line chart of the player's P&L, drawn on a
    // canvas each poll. Green above zero, red below, with a faded area fill.
    function drawPnlChart() {
      const canvas = $("#pnlChart");
      if (!canvas || !pnlHistory.length) return;
      const now = $("#pnlNow");
      const latest = pnlHistory[pnlHistory.length - 1].pnl;
      if (now) {
        now.textContent = money(latest);
        now.className = "msp-num " + (latest >= 0 ? "msp-up" : "msp-down");
      }

      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth || 600;
      const cssH = canvas.clientHeight || 200;
      canvas.width = cssW * dpr;
      canvas.height = cssH * dpr;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);

      const padX = 6, padTop = 12, padBot = 12;
      const w = cssW - padX * 2;
      const h = cssH - padTop - padBot;
      const data = pnlHistory.map((p) => p.pnl);
      let lo = Math.min(0, ...data);
      let hi = Math.max(0, ...data);
      if (hi === lo) { hi += 1; lo -= 1; }
      const range = hi - lo;

      const X = (i) => padX + (data.length === 1 ? w / 2 : (i / (data.length - 1)) * w);
      const Y = (v) => padTop + (1 - (v - lo) / range) * h;

      // zero baseline
      ctx.strokeStyle = "rgba(255,255,255,.14)";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padX, Y(0)); ctx.lineTo(padX + w, Y(0)); ctx.stroke();

      const up = latest >= 0;
      const stroke = up ? "#2ecc71" : "#ff6b6b";

      // area fill down to the zero line
      const grad = ctx.createLinearGradient(0, padTop, 0, padTop + h);
      grad.addColorStop(0, up ? "rgba(46,204,113,.28)" : "rgba(255,107,107,.28)");
      grad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.beginPath();
      data.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.lineTo(X(data.length - 1), Y(0));
      ctx.lineTo(X(0), Y(0));
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      // the line itself
      ctx.beginPath();
      data.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.stroke();

      // marker on the latest point
      ctx.fillStyle = stroke;
      ctx.beginPath();
      ctx.arc(X(data.length - 1), Y(latest), 3, 0, Math.PI * 2);
      ctx.fill();
    }

    function renderTape(tape) {
      const table = $("#tapeTable");
      if (!table) return;
      table.innerHTML = `
        <thead><tr><th>Item</th><th class="msp-num">Price</th><th class="msp-num">Qty</th>
        <th>Buyer</th><th>Seller</th></tr></thead>
        <tbody>${tape.map((t) => `
          <tr>
            <td>${esc(t.item)}</td>
            <td class="msp-num ${t.taker_side === "BUY" ? "msp-up" : "msp-down"}">${px(t.price)}</td>
            <td class="msp-num">${t.qty}</td>
            <td>${esc(t.buyer)}</td>
            <td>${esc(t.seller)}</td>
          </tr>`).join("")}</tbody>`;
    }

    // -- polling ---------------------------------------------------------
    function schedule(status) {
      clearTimeout(pollTimer);
      if (status === "finished") return;
      pollTimer = setTimeout(poll, status === "running" ? 1000 : 3000);
    }

    async function poll() {
      let state;
      try {
        state = await api(`/market-sim-py/run/${runId}/state`);
      } catch (err) {
        showMsg(msg, err.message, "error");
        schedule("lobby");
        return;
      }
      $("#msg")?.classList.add("hidden");

      $("#runName").textContent = state.name;
      $("#runCode").textContent = state.join_code;
      $("#runStatus").textContent = state.status === "lobby"
        ? "waiting to start"
        : state.status === "running" ? `tick ${state.tick} of ${state.total_ticks}` : "finished";

      const inLobby = state.status === "lobby";
      $("#lobbyView").classList.toggle("hidden", !inLobby);
      $("#liveView").classList.toggle("hidden", inLobby);
      $("#startBtn").classList.toggle("hidden", !(inLobby && state.can_control));
      $("#stopBtn").classList.toggle("hidden", !(state.status === "running" && state.can_control));

      if (inLobby) {
        mountConnectInto("#lobbyConnect");
        renderPlayers(state.players);
        $("#runClock")?.classList.add("hidden");
      } else {
        mountConnectInto("#liveConnect");
        renderClock(state.seconds_left);
        renderLeaderboard(state.leaderboard);
        renderMarket(state.market, state.status === "finished");
        renderMe(state.me, state.market, state.status);
        if (state.me) {
          const lastPoint = pnlHistory[pnlHistory.length - 1];
          // one point per tick, so catching up multiple ticks still adds once
          if (!lastPoint || lastPoint.tick !== state.tick) {
            pnlHistory.push({ tick: state.tick, pnl: state.me.pnl });
            if (pnlHistory.length > 600) pnlHistory.shift();
          }
          drawPnlChart();
        }
        renderTape(state.tape);
        if (state.status === "finished" && lastStatus === "running") {
          showMsg(msg, "Run complete — fair values are revealed and the leaderboard is final.", "ok");
        }
      }

      lastStatus = state.status;
      schedule(state.status);
    }

    poll();
  }

  // ── Boot ──────────────────────────────────────────────────────────────
  initUser();
  if (window.RUN_ID) {
    initRunPage(window.RUN_ID);
  } else {
    initLobbyPage();
  }
})();
