/* Risks — crash-survival portfolio game.
 *
 * One file serves both pages: the rules/lobby page (create, join, browse) and
 * the live round. window.GAME_ID is only defined on the round page.
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

  const money = (v) => (v == null ? "—" : (v < 0 ? "-" : "") + "$" +
    Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 }));
  const money2 = (v) => (v == null ? "—" : (v < 0 ? "-" : "") + "$" +
    Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const pct = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%");
  const signCls = (v) => (v > 0 ? "msp-up" : v < 0 ? "msp-down" : "");

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

  // ── Rules / lobby page ────────────────────────────────────────────────
  function initLobbyPage() {
    const msg = $("#msg");

    $("#createBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const res = await postJSON("/risks/create", {
          universe: $("#universe")?.value || null,
          seconds_per_day: Number($("#spd")?.value || 4),
          name: $("#runName")?.value || null,
        });
        window.location.href = `/risks/game/${res.game_id}`;
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
        const res = await postJSON("/risks/join", { join_code: code });
        window.location.href = `/risks/game/${res.game_id}`;
      } catch (err) {
        showMsg(msg, err.message, "error");
        e.target.disabled = false;
      }
    });

    $("#joinCode")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("#joinBtn")?.click();
    });

    async function loadOpen() {
      const box = $("#openRuns");
      if (!box) return;
      try {
        const { runs } = await api("/risks/open");
        $("#openCount").textContent = runs.length ? `${runs.length} open` : "";
        if (!runs.length) {
          box.innerHTML = `<p class="msp-muted">No rounds open right now — create one above.</p>`;
          return;
        }
        box.innerHTML = runs.map((r) => `
          <a class="msp-run-row" href="/risks/game/${encodeURIComponent(r.game_id)}">
            <span class="msp-run-name">${esc(r.name)}</span>
            <span class="msp-code">${esc(r.join_code)}</span>
            <span class="msp-muted">${esc(r.universe_label)} · ${r.days}d</span>
            <span class="msp-pill ${r.status === "active" ? "msp-pill-live" : ""}">${esc(r.status)}</span>
            <span class="msp-muted">${r.joined ? "joined" : ""}</span>
          </a>`).join("");
      } catch (err) {
        box.innerHTML = `<p class="msp-muted">${esc(err.message)}</p>`;
      }
    }

    loadOpen();
    setInterval(loadOpen, 5000);
  }

  // ── Round page ────────────────────────────────────────────────────────
  function initGamePage(gameId) {
    const msg = $("#msg");
    let pollTimer = null;
    let lastStatus = null;
    let state = null;          // latest server state
    let modalTicker = null;

    // -- host controls ---------------------------------------------------
    $("#startBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        await postJSON(`/risks/game/${gameId}/start`, {});
        poll();
      } catch (err) {
        showMsg(msg, err.message, "error");
      } finally {
        e.target.disabled = false;
      }
    });

    $("#stopBtn")?.addEventListener("click", async (e) => {
      if (!window.confirm("End the round now and score it where it stands?")) return;
      e.target.disabled = true;
      try {
        await postJSON(`/risks/game/${gameId}/stop`, {});
        poll();
      } catch (err) {
        showMsg(msg, err.message, "error");
        e.target.disabled = false;
      }
    });

    // -- trade modal -----------------------------------------------------
    function closeModal() {
      modalTicker = null;
      $("#tradeModal")?.classList.add("hidden");
    }

    function openModal(ticker) {
      if (!state || state.status !== "active" || !state.me) return;
      const row = (state.names || []).find((n) => n.ticker === ticker);
      if (!row) return;
      modalTicker = ticker;

      const held = (state.me.positions || {})[ticker] || 0;
      $("#tradeTicker").textContent = ticker;
      $("#tradeFacts").innerHTML = `
        <div><span>Price</span><strong>${row.price.toFixed(2)}</strong></div>
        <div><span>Published beta</span><strong>${row.published_beta.toFixed(2)}</strong></div>
        <div><span>Since day 1</span><strong class="${signCls(row.total_pct)}">${pct(row.total_pct)}</strong></div>
        <div><span>You hold</span><strong>${held}</strong></div>`;

      const input = $("#tradeTarget");
      input.value = String(held);

      // Quick sizes scaled to the gross budget, so a click is a sensible clip.
      const clip = Math.max(100, Math.round((window.GROSS_LIMIT / 12) / row.price / 100) * 100);
      $("#tradeQuick").innerHTML = [-2, -1, 0, 1, 2]
        .map((m) => {
          const v = m * clip;
          const label = m === 0 ? "Flat" : (v > 0 ? `+${v}` : `${v}`);
          return `<button class="btn ghost" data-target="${v}">${label}</button>`;
        }).join("");
      $("#tradeQuick").querySelectorAll("[data-target]").forEach((b) => {
        b.addEventListener("click", () => {
          input.value = b.dataset.target;
          updateHint();
        });
      });

      updateHint();
      $("#tradeModal").classList.remove("hidden");
      input.focus();
      input.select();
    }

    // Live preview of what the order would do to the two limits, so a player
    // sees the breach before the server rejects it.
    function updateHint() {
      const hint = $("#tradeHint");
      if (!hint || !state || !state.me || !modalTicker) return;
      const target = Number($("#tradeTarget").value || 0);
      const prices = {};
      (state.names || []).forEach((n) => { prices[n.ticker] = n.price; });

      const pos = { ...(state.me.positions || {}) };
      if (target) pos[modalTicker] = target; else delete pos[modalTicker];

      let gross = 0, net = 0;
      Object.entries(pos).forEach(([t, q]) => {
        gross += Math.abs(q) * (prices[t] || 0);
        net += q * (prices[t] || 0);
      });

      const overGross = gross > window.GROSS_LIMIT;
      const overNet = Math.abs(net) > window.NET_LIMIT;
      hint.innerHTML =
        `Gross <strong class="${overGross ? "msp-down" : ""}">${money(gross)}</strong> / ${money(window.GROSS_LIMIT)}` +
        ` · Net <strong class="${overNet ? "msp-down" : ""}">${money(net)}</strong> / ±${money(window.NET_LIMIT)}` +
        (overGross || overNet ? ` — over the limit, this will be rejected` : "");
      $("#tradeSubmit").disabled = overGross || overNet;
    }

    $("#tradeTarget")?.addEventListener("input", updateHint);
    $("#tradeClose")?.addEventListener("click", closeModal);
    $("#tradeModal")?.addEventListener("click", (e) => {
      if (e.target.id === "tradeModal") closeModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeModal();
    });

    $("#tradeSubmit")?.addEventListener("click", async (e) => {
      if (!modalTicker) return;
      const target = Math.round(Number($("#tradeTarget").value || 0));
      e.target.disabled = true;
      try {
        await postJSON(`/risks/game/${gameId}/trade`, { ticker: modalTicker, target });
        closeModal();
        poll();
      } catch (err) {
        showMsg(msg, err.message, "error");
        e.target.disabled = false;
      }
    });

    $("#tradeTarget")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("#tradeSubmit")?.click();
    });

    // -- rendering -------------------------------------------------------
    function renderStrip(me, s) {
      const box = $("#myStats");
      if (!box) return;
      if (!me) {
        box.innerHTML = `<div class="msp-stat msp-stat-wide">
          <span class="msp-stat-label">Spectating</span>
          <span class="msp-stat-value">You are watching this round, not trading it.</span></div>`;
        return;
      }
      const grossPct = Math.min(100, (me.gross / s.gross_limit) * 100);
      const netPct = Math.min(100, (Math.abs(me.net) / s.net_limit) * 100);
      box.innerHTML = `
        <div class="msp-stat"><span class="msp-stat-label">Day</span>
          <span class="msp-stat-value">${s.day + 1} / ${s.total_days}</span></div>
        <div class="msp-stat msp-stat-key"><span class="msp-stat-label">P&amp;L</span>
          <span class="msp-stat-value ${signCls(me.pnl)}">${money2(me.pnl)}</span></div>
        <div class="msp-stat"><span class="msp-stat-label">Max drawdown</span>
          <span class="msp-stat-value ${me.max_drawdown > 0 ? "msp-down" : ""}">${money(me.max_drawdown)}</span></div>
        <div class="msp-stat"><span class="msp-stat-label">Score</span>
          <span class="msp-stat-value ${signCls(me.score)}">${money(me.score)}</span></div>
        <div class="msp-stat"><span class="msp-stat-label">Gross ${money(me.gross)}</span>
          <div class="msp-meter"><div class="msp-meter-fill${grossPct >= 99 ? " msp-meter-full" : ""}"
            style="width:${grossPct}%"></div></div></div>
        <div class="msp-stat"><span class="msp-stat-label">Net ${money(me.net)}</span>
          <div class="msp-meter"><div class="msp-meter-fill${netPct >= 99 ? " msp-meter-full" : ""}"
            style="width:${netPct}%"></div></div></div>`;
    }

    function renderBasket(names, me, live) {
      const table = $("#basketTable");
      if (!table) return;

      // The index is the equal-weighted basket level. Without it a player
      // cannot tell a good book from a falling market.
      const idx = $("#basketIndex");
      if (idx && state && state.index && state.index.length) {
        const level = state.index[state.index.length - 1];
        const move = (level / state.index[0] - 1) * 100;
        idx.innerHTML = `index <strong>${level.toFixed(1)}</strong> ` +
          `<span class="${signCls(move)}">${pct(move)}</span>`;
      }
      const positions = (me && me.positions) || {};
      const anySector = names.some((n) => n.sector);
      table.innerHTML = `
        <thead><tr>
          <th>Name</th>${anySector ? "<th>Cohort</th>" : ""}
          <th class="msp-num">Beta</th><th class="msp-num">Price</th>
          <th class="msp-num">Day</th><th class="msp-num">Total</th>
          <th class="msp-num">Position</th><th class="msp-num">Value</th>
        </tr></thead>
        <tbody>${names.map((n) => {
          const q = positions[n.ticker] || 0;
          const value = q * n.price;
          return `<tr class="${live ? "risk-row" : ""} ${q ? "risk-row-held" : ""}"
              ${live ? `data-ticker="${esc(n.ticker)}" tabindex="0" role="button"` : ""}>
            <td><strong>${esc(n.ticker)}</strong></td>
            ${anySector ? `<td class="risk-cohort">${esc(n.sector || "—")}</td>` : ""}
            <td class="msp-num">${n.published_beta.toFixed(2)}</td>
            <td class="msp-num">${n.price.toFixed(2)}</td>
            <td class="msp-num ${signCls(n.day_pct)}">${pct(n.day_pct)}</td>
            <td class="msp-num ${signCls(n.total_pct)}">${pct(n.total_pct)}</td>
            <td class="msp-num ${q > 0 ? "msp-up" : q < 0 ? "msp-down" : ""}">${q || "—"}</td>
            <td class="msp-num msp-muted">${q ? money(value) : "—"}</td>
          </tr>`;
        }).join("")}</tbody>`;

      if (!live) return;
      table.querySelectorAll("[data-ticker]").forEach((tr) => {
        tr.addEventListener("click", () => openModal(tr.dataset.ticker));
        tr.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openModal(tr.dataset.ticker); }
        });
      });
    }

    // The day's commentary. Confidence is the generator's own read of how
    // strong the signal is, so it is worth surfacing next to the words.
    function renderWire(wire) {
      const bar = $("#riskWire");
      if (!bar) return;
      if (!wire || !wire.text) { bar.classList.add("hidden"); return; }
      bar.classList.remove("hidden");
      $("#wireText").textContent = wire.text;
      const conf = $("#wireConf");
      conf.textContent = wire.confidence;
      conf.className = "risk-wire-conf risk-conf-" + String(wire.confidence).toLowerCase();
    }

    function renderLeaderboard(rows, myUid) {
      const table = $("#leaderboard");
      if (!table) return;
      table.innerHTML = `
        <thead><tr><th>#</th><th>Player</th><th class="msp-num">Score</th>
        <th class="msp-num">P&amp;L</th><th class="msp-num">Max DD</th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="${r.user_id === myUid ? "msp-me-row" : ""}">
            <td>${r.rank}</td>
            <td>${esc(r.username)}</td>
            <td class="msp-num ${signCls(r.score)}">${money(r.score)}</td>
            <td class="msp-num ${signCls(r.pnl)}">${money(r.pnl)}</td>
            <td class="msp-num msp-muted">${money(r.max_drawdown)}</td>
          </tr>`).join("")}</tbody>`;
    }

    // A 100-day episode can have fifty consecutive rebound days; listing them
    // one by one runs off the end of the line. Collapse runs into ranges.
    function dayRanges(days) {
      if (!days || !days.length) return "none";
      const nums = [...days].map((d) => d + 1).sort((a, b) => a - b);
      const parts = [];
      let start = nums[0];
      let prev = nums[0];
      for (let i = 1; i <= nums.length; i++) {
        const n = nums[i];
        if (n !== prev + 1) {
          parts.push(start === prev ? `${start}` : `${start}–${prev}`);
          start = n;
        }
        prev = n;
      }
      return "day" + (nums.length > 1 ? "s " : " ") + parts.join(", ");
    }

    function renderReveal(reveal) {
      const view = $("#revealView");
      const table = $("#revealTable");
      if (!view || !table || !reveal) return;
      view.classList.remove("hidden");

      // Which historical crashes this path was blended from, and the phases.
      const blend = $("#revealBlend");
      if (blend) {
        const bits = [];
        if (reveal.blend && reveal.blend.length) {
          bits.push(`<div class="risk-blend-row"><span>Blended from</span><div>` +
            reveal.blend.map((b) =>
              `<span class="risk-blend-chip">${esc(b.period)}
                 <strong>${Math.round(b.weight * 100)}%</strong></span>`).join("") +
            `</div></div>`);
        }
        if (reveal.phases) {
          const order = ["crash", "stabilisation", "recovery"];
          bits.push(`<div class="risk-blend-row"><span>Phases</span><div>` +
            order.filter((k) => reveal.phases[k]).map((k) =>
              `<span class="risk-blend-chip risk-phase-${k}">${k}
                 <strong>day ${reveal.phases[k][0] + 1}–${reveal.phases[k][1]}</strong></span>`).join("") +
            `</div></div>`);
        }
        if (reveal.severity != null) {
          bits.push(`<div class="risk-blend-row"><span>Severity</span><div>` +
            `<span class="risk-blend-chip"><strong>${reveal.severity.toFixed(2)}×</strong></span></div></div>`);
        }
        blend.innerHTML = bits.join("");
        blend.classList.toggle("hidden", !bits.length);
      }

      // Dated macro events, when the generator recorded them.
      const evCard = $("#eventsCard");
      const evTable = $("#eventsTable");
      if (evCard && evTable) {
        const events = reveal.events || [];
        evCard.classList.toggle("hidden", !events.length);
        if (events.length) {
          evTable.innerHTML = `
            <thead><tr><th class="msp-num">Day</th><th>Kind</th><th>Signal</th>
            <th class="msp-num">Reading</th><th>Hits</th></tr></thead>
            <tbody>${events.map((e) => `
              <tr>
                <td class="msp-num">${e.day + 1}</td>
                <td><span class="risk-event risk-event-${esc(e.kind)}">${esc(e.kind)}</span></td>
                <td>${esc(e.label)}</td>
                <td class="msp-num">${e.value == null ? "—" : esc(e.value) + " " + esc(e.unit)}</td>
                <td class="msp-muted">${(e.hits || []).map(esc).join(", ")}</td>
              </tr>`).join("")}</tbody>`;
        }
      }

      $("#revealSummary").textContent =
        `index ${pct(reveal.index_return_pct)}, trough ${pct(reveal.index_drawdown_pct)} · ` +
        `panic ${dayRanges(reveal.panic_days)} · rebound ${dayRanges(reveal.rebound_days)}`;

      table.innerHTML = `
        <thead><tr>
          <th>Name</th><th class="msp-num">Published beta</th><th class="msp-num">Realised beta</th>
          <th>Shock group</th><th class="msp-num">Total</th><th class="msp-num">Drawdown</th>
        </tr></thead>
        <tbody>${reveal.names.map((n) => {
          const drift = n.realised_beta - n.published_beta;
          return `<tr>
            <td><strong>${esc(n.ticker)}</strong></td>
            <td class="msp-num">${n.published_beta.toFixed(2)}</td>
            <td class="msp-num">${n.realised_beta.toFixed(2)}
              <span class="msp-sub ${Math.abs(drift) > 0.2 ? "msp-down" : "msp-muted"}">
                ${drift >= 0 ? "+" : ""}${drift.toFixed(2)}</span></td>
            <td><span class="risk-group risk-group-${esc(n.shock_group)}">${esc(n.shock_group)}</span></td>
            <td class="msp-num ${signCls(n.total_pct)}">${pct(n.total_pct)}</td>
            <td class="msp-num msp-down">${pct(n.drawdown_pct)}</td>
          </tr>`;
        }).join("")}</tbody>`;
    }

    // Equity curve against the starting line, drawn straight onto a canvas.
    function drawEquity(curve, startEquity) {
      const canvas = $("#equityChart");
      if (!canvas || !curve || !curve.length) return;
      const now = $("#equityNow");
      const latest = curve[curve.length - 1];
      if (now) {
        now.textContent = money2(latest - startEquity);
        now.className = "msp-num " + signCls(latest - startEquity);
      }

      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth || 600;
      const cssH = canvas.clientHeight || 180;
      canvas.width = cssW * dpr;
      canvas.height = cssH * dpr;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);

      const padX = 6, padTop = 12, padBot = 12;
      const w = cssW - padX * 2;
      const h = cssH - padTop - padBot;

      let lo = Math.min(startEquity, ...curve);
      let hi = Math.max(startEquity, ...curve);
      if (hi === lo) { hi += 1; lo -= 1; }
      const pad = (hi - lo) * 0.08;
      lo -= pad; hi += pad;

      const X = (i) => padX + (curve.length === 1 ? w / 2 : (i / (curve.length - 1)) * w);
      const Y = (v) => padTop + (1 - (v - lo) / (hi - lo)) * h;

      // starting-equity line
      ctx.strokeStyle = "rgba(255,255,255,.16)";
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padX, Y(startEquity));
      ctx.lineTo(padX + w, Y(startEquity));
      ctx.stroke();
      ctx.setLineDash([]);

      const up = latest >= startEquity;
      const stroke = up ? "#35c98b" : "#f2555a";

      const grad = ctx.createLinearGradient(0, padTop, 0, padTop + h);
      grad.addColorStop(0, up ? "rgba(53,201,139,.26)" : "rgba(242,85,90,.26)");
      grad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.beginPath();
      curve.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.lineTo(X(curve.length - 1), Y(startEquity));
      ctx.lineTo(X(0), Y(startEquity));
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      curve.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.stroke();

      ctx.fillStyle = stroke;
      ctx.beginPath();
      ctx.arc(X(curve.length - 1), Y(latest), 3, 0, Math.PI * 2);
      ctx.fill();
    }

    function renderPlayers(players) {
      const box = $("#playerList");
      if (!box) return;
      $("#playerCount").textContent = `${players.length} joined`;
      box.innerHTML = players.map((p) => `
        <div class="msp-player"><span>${esc(p.username)}</span></div>`).join("")
        || `<p class="msp-muted">Nobody has joined yet.</p>`;
    }

    // -- polling ---------------------------------------------------------
    function schedule(status, spd) {
      clearTimeout(pollTimer);
      if (status === "finished") return;
      const wait = status === "active" ? Math.max(1000, (spd || 4) * 400) : 3000;
      pollTimer = setTimeout(poll, wait);
    }

    async function poll() {
      let s;
      try {
        s = await api(`/risks/game/${gameId}/state`);
      } catch (err) {
        showMsg(msg, err.message, "error");
        schedule("lobby");
        return;
      }
      state = s;
      $("#msg")?.classList.add("hidden");

      $("#runName").textContent = s.name;
      $("#runCode").textContent = s.join_code;
      $("#runStatus").textContent = s.status === "lobby"
        ? "waiting to start"
        : s.status === "active" ? `day ${s.day + 1} of ${s.total_days}` : "finished";
      const lobbyUni = $("#lobbyUniverse");
      if (lobbyUni) lobbyUni.textContent = s.universe_label;

      const inLobby = s.status === "lobby";
      $("#lobbyView").classList.toggle("hidden", !inLobby);
      $("#liveView").classList.toggle("hidden", inLobby);
      $("#startBtn").classList.toggle("hidden", !(inLobby && s.can_control));
      $("#stopBtn").classList.toggle("hidden", !(s.status === "active" && s.can_control));
      // The board is a fixed-height dashboard while trading. Once the round
      // is scored the reveal needs the page to scroll instead.
      document.body.classList.toggle("msp-live", !inLobby && s.status === "active");
      document.body.classList.toggle("risk-review", s.status === "finished");

      const clock = $("#dayClock");
      if (clock) {
        clock.classList.toggle("hidden", inLobby);
        clock.textContent = inLobby ? "" : `Day ${s.day + 1}/${s.total_days}`;
        clock.classList.toggle("msp-clock-low", !inLobby && s.days_left <= 3);
      }

      if (inLobby) {
        renderPlayers(s.players || []);
      } else {
        renderStrip(s.me, s);
        renderWire(s.wire);
        renderBasket(s.names || [], s.me, s.status === "active");
        renderLeaderboard(s.leaderboard || [], s.my_uid);
        drawEquity(s.my_equity_curve || [], s.start_equity);
        if (s.status === "finished") {
          renderReveal(s.reveal);
          if (lastStatus === "active") {
            showMsg(msg, "Round complete — realised betas and shock groups are revealed below.", "ok");
          }
          closeModal();
        }
      }

      lastStatus = s.status;
      schedule(s.status, s.seconds_per_day);
    }

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (state) drawEquity(state.my_equity_curve || [], state.start_equity);
      }, 120);
    });

    poll();
  }

  // ── Boot ──────────────────────────────────────────────────────────────
  initUser();
  if (window.GAME_ID) {
    initGamePage(window.GAME_ID);
  } else {
    initLobbyPage();
  }
})();
