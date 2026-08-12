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
    let catalog = null;      // bot archetypes + skills, fetched once
    const pnlHistory = [];   // {tick, pnl} points for the live chart
    let lastPnlSeen = null;  // for detecting a gain/loss between polls, to flash #pnlNow
    let pnlFlashTimer = null;
    let pnlTweenRaf = null;
    let gainStreak = 0;      // consecutive up-ticks; resets on any down-tick
    let avgMoveSize = null;  // running average of |change|, so "big win" scales to what's normal for this game
    const prevRanks = new Map(); // uid -> last-seen rank, for the leaderboard FLIP animation
    let firstTradeToasted = false;
    let bestPnlSeen = null;       // best P&L this run; only a big-enough jump above it toasts
    let wasInTop3 = false;
    const seenActiveBots = new Set(); // uids already announced as "entered"
    let botsSeeded = false;           // first poll just records who's already live, doesn't announce them
    let winBannerShown = false;       // one-shot: the podium-finish celebration

    async function ensureCatalog() {
      if (!catalog) {
        try { catalog = await api("/market-sim-py/bots/catalog"); } catch { catalog = null; }
      }
      return catalog;
    }

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

    // -- setup drawer ----------------------------------------------------
    // Connecting a bot and stocking house bots are pre-run chores. Once the
    // run is live the drawer starts shut so the board owns the screen; the
    // choice is remembered per browser.
    const SETUP_KEY = "msp-setup-open";
    function setSetupOpen(open) {
      const drawer = $("#setupDrawer");
      const btn = $("#setupToggle");
      if (!drawer || !btn) return;
      drawer.classList.toggle("hidden", !open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.classList.toggle("msp-toggle-on", open);
      btn.textContent = open ? "Hide setup" : "Setup";
      try { localStorage.setItem(SETUP_KEY, open ? "1" : "0"); } catch { /* private mode */ }
      if (open) drawPnlChart();
    }

    $("#setupToggle")?.addEventListener("click", () => {
      setSetupOpen($("#setupDrawer").classList.contains("hidden"));
    });

    let setupInitialised = false;
    function initSetupDrawer() {
      if (setupInitialised) return;
      setupInitialised = true;
      let stored = null;
      try { stored = localStorage.getItem(SETUP_KEY); } catch { /* private mode */ }
      setSetupOpen(stored === "1");
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
      el.classList.toggle("msp-clock-critical", s > 0 && s <= 10);
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

    function renderLeaderboard(rows, myUid) {
      const table = $("#leaderboard");
      if (!table) return rows.find((r) => r.user_id === myUid);

      const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      // Pin the player's own rank at the top so it's always visible.
      const meRow = rows.find((r) => r.user_id === myUid);
      const meEl = $("#lbMe");
      if (meEl) {
        if (meRow) {
          // Read this before the innerHTML rebuild below touches anything —
          // prevRanks still holds the *previous* poll's ranks at this point,
          // it isn't cleared/repopulated until the end of this function.
          const prevMeRank = prevRanks.get(String(myUid));
          meEl.classList.remove("hidden");
          meEl.innerHTML = `
            <span class="msp-lb-rank">#${meRow.rank}</span>
            <span class="msp-lb-you">You</span>
            <span class="msp-num ${meRow.pnl >= 0 ? "msp-up" : "msp-down"}">${money(meRow.pnl)}</span>
            <span class="msp-num msp-muted">${meRow.fills} fills</span>`;
          // The pinned strip is what a player actually watches — it should
          // not be the one row on the board with zero feedback on a rank
          // change just because it isn't part of the table.
          if (!reduceMotion && prevMeRank !== undefined && prevMeRank !== meRow.rank) {
            meEl.classList.remove("msp-rank-up", "msp-rank-down");
            void meEl.offsetWidth;
            meEl.classList.add(meRow.rank < prevMeRank ? "msp-rank-up" : "msp-rank-down");
          }
        } else {
          meEl.classList.add("hidden");
        }
      }

      // FLIP: capture each row's current on-screen position, keyed by a
      // stable id, before the table gets fully rebuilt below.
      const oldRects = new Map();
      table.querySelectorAll("tbody tr[data-uid]").forEach((tr) => {
        oldRects.set(tr.dataset.uid, tr.getBoundingClientRect());
      });

      table.innerHTML = `
        <thead><tr><th>#</th><th>Player</th><th class="msp-num">P&amp;L</th>
        <th class="msp-num">Fills</th><th></th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="${r.is_bot ? "msp-bot-row" : ""} ${r.user_id === myUid ? "msp-me-row" : ""}" data-uid="${esc(String(r.user_id))}">
            <td>${r.rank}</td>
            <td>${esc(r.username)}${r.is_bot ? ' <span class="msp-tag">bot</span>' : ""}</td>
            <td class="msp-num ${r.pnl >= 0 ? "msp-up" : "msp-down"}">${money(r.pnl)}</td>
            <td class="msp-num">${r.fills}</td>
            <td>${!r.is_bot && r.connected ? '<span class="msp-dot" title="bot connected"></span>' : ""}</td>
          </tr>`).join("")}</tbody>`;

      // Play: slide each row from where it used to be to where it is now —
      // a rank change reads as movement, not a flicker — and flash it green
      // or red if its rank actually improved or worsened since last poll.
      table.querySelectorAll("tbody tr[data-uid]").forEach((tr) => {
        const uid = tr.dataset.uid;
        const oldRect = oldRects.get(uid);
        const prevRank = prevRanks.get(uid);
        const row = rows.find((r) => String(r.user_id) === uid);

        if (!reduceMotion && oldRect) {
          const newRect = tr.getBoundingClientRect();
          const deltaY = oldRect.top - newRect.top;
          if (Math.abs(deltaY) > 1) {
            tr.animate(
              [{ transform: `translateY(${deltaY}px)` }, { transform: "translateY(0)" }],
              { duration: 420, easing: "cubic-bezier(.2,.8,.3,1)" },
            );
          }
        }
        if (row && prevRank !== undefined && prevRank !== row.rank) {
          tr.classList.add(row.rank < prevRank ? "msp-rank-up" : "msp-rank-down");
        }
      });

      prevRanks.clear();
      rows.forEach((r) => prevRanks.set(String(r.user_id), r.rank));

      return meRow;
    }

    // Creates the stack on first use, so no template markup is needed for it.
    function showMilestoneToast(text, kind) {
      let stack = document.getElementById("mspToastStack");
      if (!stack) {
        stack = document.createElement("div");
        stack.id = "mspToastStack";
        stack.className = "msp-toast-stack";
        // These are rare, meaningful call-outs (first fill, a new best,
        // top 3) — worth announcing to a screen reader, not just sighted
        // players. "polite" so it queues behind whatever's already being
        // read instead of interrupting it.
        stack.setAttribute("role", "status");
        stack.setAttribute("aria-live", "polite");
        document.body.appendChild(stack);
      }
      const el = document.createElement("div");
      el.className = `msp-toast ${kind || ""}`;
      const dot = document.createElement("span");
      dot.className = "msp-toast-dot";
      const label = document.createElement("span");
      label.textContent = text; // textContent, not innerHTML — text is user-influenced (username-adjacent context)
      el.append(dot, label);
      stack.appendChild(el);
      setTimeout(() => {
        el.classList.add("out");
        setTimeout(() => el.remove(), 260);
      }, 3600);
    }

    // Three rare, edge-triggered events worth calling out on their own —
    // ordinary rank/P&L fluctuation already has its own feedback (the FLIP
    // slide, the #pnlNow flash) and doesn't need a toast on top of it.
    function checkMilestones(meRow) {
      if (!meRow) return;

      if (!firstTradeToasted && meRow.fills >= 1) {
        firstTradeToasted = true;
        showMilestoneToast("First trade placed", "milestone-first");
      }

      if (bestPnlSeen === null) {
        bestPnlSeen = meRow.pnl;
      } else if (meRow.pnl > bestPnlSeen && meRow.pnl > 0) {
        const improvement = meRow.pnl - bestPnlSeen;
        // Only worth a toast if the new high is a real jump, not a routine
        // tick — same "how big is this move, for this game" comparison
        // #pnlNow's flash uses, so a string of tiny new-highs doesn't spam.
        if (avgMoveSize === null || improvement >= avgMoveSize * 1.2) {
          showMilestoneToast(`New personal best: ${money(meRow.pnl)}`, "milestone-pnl");
        }
        bestPnlSeen = meRow.pnl;
      }

      if (meRow.rank <= 3) {
        if (!wasInTop3) {
          wasInTop3 = true;
          showMilestoneToast(`Climbed to #${meRow.rank} on the leaderboard`, "milestone-rank");
        }
      } else {
        wasInTop3 = false;
      }
    }

    // Scheduled bots currently just appear in the roster with no callout — a
    // new competitor showing up mid-run is exactly the kind of thing worth a
    // toast, same mechanism as the personal milestones above. The first poll
    // only records who's already live (joining a run in progress shouldn't
    // fire a toast per existing bot); only a bot that flips from not-active
    // to active on a later poll counts as "just entered."
    function checkNewBots(bots) {
      if (!botsSeeded) {
        bots.forEach((b) => { if (b.active) seenActiveBots.add(b.uid); });
        botsSeeded = true;
        return;
      }
      for (const b of bots) {
        if (b.active && !seenActiveBots.has(b.uid)) {
          seenActiveBots.add(b.uid);
          showMilestoneToast(`${b.name} just entered the market`, "milestone-bot");
        }
      }
    }

    // The podium — the one thing in a run players don't finish more than
    // once — gets its own center-screen moment instead of stacking into
    // the same toast queue as "cracked the top 3" mid-run. All three ranks
    // reward the player, but not equally: #1 gets the full banner, bigger
    // confetti burst and a longer hold; #2 and #3 step down in every one
    // of those at once so the hierarchy is legible at a glance, not just
    // implied by the number in the title.
    const WIN_TIERS = {
      1: { confetti: 28, holdMs: 4200, chartW: 220, chartH: 46 },
      2: { confetti: 16, holdMs: 3400, chartW: 180, chartH: 38 },
      3: { confetti: 8, holdMs: 2800, chartW: 150, chartH: 32 },
    };

    function spawnConfetti(count) {
      if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const colors = ["var(--brand)", "var(--green)", "#e8c468"];
      for (let i = 0; i < count; i++) {
        const p = document.createElement("span");
        p.className = "msp-confetti-piece";
        p.style.left = Math.random() * 100 + "vw";
        p.style.background = colors[i % colors.length];
        document.body.appendChild(p);
        const fall = 340 + Math.random() * 260;
        const drift = (Math.random() - 0.5) * 180;
        const rot = 180 + Math.random() * 540;
        const anim = p.animate(
          [
            { transform: "translate(0, 0) rotate(0deg)", opacity: 1 },
            { transform: `translate(${drift}px, ${fall}px) rotate(${rot}deg)`, opacity: 0 },
          ],
          { duration: 1400 + Math.random() * 700, easing: "cubic-bezier(.3,.6,.4,1)" },
        );
        anim.onfinish = () => p.remove();
      }
    }

    // The coordinate mapping is shared between the initial paint and every
    // hover redraw, so tracing the line lands exactly on the line rather
    // than drifting from a second, slightly different computation of it.
    function miniChartLayout(cssW, cssH, history) {
      const padX = 3, padY = 4;
      const w = cssW - padX * 2;
      const h = cssH - padY * 2;
      const data = history.map((p) => p.pnl);
      let lo = Math.min(0, ...data);
      let hi = Math.max(0, ...data);
      if (hi === lo) { hi += 1; lo -= 1; }
      const range = hi - lo;
      return {
        data, padX, padY, w, h,
        X: (i) => padX + (data.length === 1 ? w / 2 : (i / (data.length - 1)) * w),
        Y: (v) => padY + (1 - (v - lo) / range) * h,
      };
    }

    // hoverIdx draws a dashed guide line + dot at that point on top of the
    // same fill+line every paint does — called on every mousemove, so this
    // is a full clear-and-redraw rather than an incremental overlay.
    function paintMiniChart(ctx, cssW, cssH, layout, hoverIdx) {
      ctx.clearRect(0, 0, cssW, cssH);
      const { data, X, Y, padY, h } = layout;
      const up = data[data.length - 1] >= 0;
      const stroke = up ? "#2ecc71" : "#ff6b6b";

      const grad = ctx.createLinearGradient(0, padY, 0, padY + h);
      grad.addColorStop(0, up ? "rgba(46,204,113,.25)" : "rgba(255,107,107,.25)");
      grad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.beginPath();
      data.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.lineTo(X(data.length - 1), Y(0));
      ctx.lineTo(X(0), Y(0));
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      data.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.stroke();

      if (hoverIdx != null) {
        const x = X(hoverIdx), y = Y(data[hoverIdx]);
        ctx.save();
        ctx.setLineDash([2, 2]);
        ctx.strokeStyle = "rgba(255,255,255,.35)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, layout.padY);
        ctx.lineTo(x, layout.padY + h);
        ctx.stroke();
        ctx.restore();

        ctx.beginPath();
        ctx.fillStyle = stroke;
        ctx.arc(x, y, 2.6, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // A tiny, static version of drawPnlChart()'s line — the podium moment
    // is exactly when "how did the run go", not just "where did it end up",
    // is worth showing. Draws once, then wires hover: moving the cursor
    // across it traces the run tick by tick, same idea as the live chart's
    // marker on its latest point, just movable to any point in the run.
    function drawMiniPnlChart(wrap, history) {
      const canvas = wrap.querySelector(".msp-win-chart");
      const tip = wrap.querySelector(".msp-win-chart-tip");
      if (!canvas || !history.length) return;

      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.width, cssH = canvas.height;
      canvas.width = cssW * dpr;
      canvas.height = cssH * dpr;
      canvas.style.width = cssW + "px";
      canvas.style.height = cssH + "px";
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const layout = miniChartLayout(cssW, cssH, history);
      paintMiniChart(ctx, cssW, cssH, layout);

      if (history.length < 2) return; // nothing to trace across a single point

      function nearestIndex(clientX) {
        const rect = canvas.getBoundingClientRect();
        const localX = ((clientX - rect.left) / rect.width) * cssW;
        const t = (localX - layout.padX) / layout.w;
        return Math.max(0, Math.min(layout.data.length - 1, Math.round(t * (layout.data.length - 1))));
      }

      canvas.addEventListener("mousemove", (e) => {
        const idx = nearestIndex(e.clientX);
        paintMiniChart(ctx, cssW, cssH, layout, idx);
        const point = history[idx];
        tip.textContent = `tick ${point.tick} · ${money(point.pnl)}`;
        tip.style.left = layout.X(idx) + "px";
        tip.style.top = layout.Y(layout.data[idx]) + "px";
        tip.classList.remove("hidden");
      });

      canvas.addEventListener("mouseleave", () => {
        paintMiniChart(ctx, cssW, cssH, layout);
        tip.classList.add("hidden");
      });
    }

    function showWinBanner(meRow) {
      const rank = meRow.rank;
      const tier = WIN_TIERS[rank];
      if (!tier) return;
      const peak = pnlHistory.length ? Math.max(...pnlHistory.map((p) => p.pnl)) : meRow.pnl;
      const el = document.createElement("div");
      el.className = `msp-win-banner tier-${rank}`;
      el.innerHTML = `
        <div class="msp-win-eyebrow">Run complete</div>
        <div class="msp-win-title">You finished #${rank}</div>
        <div class="msp-win-chart-wrap">
          <canvas class="msp-win-chart" width="${tier.chartW}" height="${tier.chartH}"></canvas>
          <div class="msp-win-chart-tip hidden"></div>
        </div>
        <div class="msp-win-stats">
          <div class="msp-win-stat"><span class="msp-win-stat-label">P&amp;L</span>
            <span class="msp-win-stat-value ${meRow.pnl >= 0 ? "msp-up" : "msp-down"}">${money(meRow.pnl)}</span></div>
          <div class="msp-win-stat"><span class="msp-win-stat-label">Peak</span>
            <span class="msp-win-stat-value">${money(peak)}</span></div>
          <div class="msp-win-stat"><span class="msp-win-stat-label">Fills</span>
            <span class="msp-win-stat-value">${meRow.fills}</span></div>
        </div>
        <div class="msp-win-sub">Hover the chart to trace it · click to dismiss</div>`;
      function dismiss() {
        if (!el.isConnected) return;
        el.classList.add("out");
        setTimeout(() => el.remove(), 320);
      }
      el.addEventListener("click", dismiss);
      document.body.appendChild(el);
      drawMiniPnlChart(el.querySelector(".msp-win-chart-wrap"), pnlHistory);
      spawnConfetti(tier.confetti);
      // Tracing the run shouldn't get cut off by the auto-dismiss timer —
      // pause it for as long as the banner is being looked at, not just
      // while the cursor sits still on the chart. Always clear before
      // rearming: a stray extra mouseleave (or a fast enter/leave/enter)
      // must never leave a stale earlier timer still armed underneath the
      // one that looks active.
      let dismissTimer;
      function armDismiss() {
        clearTimeout(dismissTimer);
        dismissTimer = setTimeout(dismiss, tier.holdMs);
      }
      armDismiss();
      el.addEventListener("mouseenter", () => clearTimeout(dismissTimer));
      el.addEventListener("mouseleave", armDismiss);
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

    // The always-visible stat strip along the top of the board. Everything a
    // player checks mid-run lives here so nothing has to be scrolled to.
    function renderMe(me, items, runStatus, meRow) {
      const stats = $("#myStats");
      const positions = $("#myPositions");
      const status = $("#myStatus");
      if (!me) {
        if (stats) {
          stats.innerHTML = `<div class="msp-stat msp-stat-wide">
            <span class="msp-stat-label">Spectating</span>
            <span class="msp-stat-value">You are watching this run, not trading it.</span></div>`;
        }
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
        const rank = meRow ? `#${meRow.rank}` : "—";
        const gross = items.reduce((n, it) => n + Math.abs(me.positions[it.item] || 0), 0);
        stats.innerHTML = `
          <div class="msp-stat"><span class="msp-stat-label">Rank</span>
            <span class="msp-stat-value">${rank}</span></div>
          <div class="msp-stat msp-stat-key"><span class="msp-stat-label">P&amp;L</span>
            <span class="msp-stat-value ${me.pnl >= 0 ? "msp-up" : "msp-down"}">${money(me.pnl)}</span></div>
          <div class="msp-stat"><span class="msp-stat-label">Cash</span>
            <span class="msp-stat-value">${money(me.cash)}</span></div>
          <div class="msp-stat"><span class="msp-stat-label">Fills</span>
            <span class="msp-stat-value">${me.fills}</span></div>
          <div class="msp-stat"><span class="msp-stat-label">Orders</span>
            <span class="msp-stat-value">${me.orders_accepted}${
              me.orders_rejected ? `<span class="msp-down msp-stat-sub"> / ${me.orders_rejected} rej</span>` : ""
            }</span></div>
          <div class="msp-stat"><span class="msp-stat-label">Gross position</span>
            <span class="msp-stat-value">${gross}</span></div>`;
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

    // -- house bots (admin roster + scheduled entry) ---------------------
    const fmtTime = (sec) => `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;

    function parseWhen(s) {
      s = (s || "").trim().toLowerCase();
      if (!s || s === "now") return 0;
      if (s.includes(":")) {
        const [m, sec] = s.split(":").map(Number);
        return Math.max(0, (m || 0) * 60 + (sec || 0));
      }
      const n = Number(s);
      return isFinite(n) ? Math.max(0, Math.round(n)) : 0;
    }

    function botRow(b, canControl) {
      const entry = b.active
        ? '<span class="msp-dot"></span> live'
        : (b.enters_at === 0 ? "from start" : `enters ${fmtTime(b.enters_at)}`);
      const pnl = b.active
        ? `<span class="msp-num ${b.pnl >= 0 ? "msp-up" : "msp-down"}">${money(b.pnl)}</span>` : "<span></span>";
      const rm = (canControl && b.removable)
        ? `<button class="msp-bot-x" data-remove="${esc(b.uid)}" title="remove">×</button>` : "<span></span>";
      return `<div class="msp-roster-row">
        <span class="msp-bot-name">${esc(b.name)}</span>
        <span class="msp-skill msp-skill-${esc(b.skill)}">${esc(b.skill)}</span>
        <span class="msp-muted msp-bot-arch">${esc(b.archetype_label)}</span>
        <span class="msp-muted msp-bot-entry">${entry}</span>
        ${pnl}${rm}
      </div>`;
    }

    function addFormHtml(cat) {
      const arch = cat.archetypes
        .map((a) => `<option value="${esc(a.key)}" title="${esc(a.desc)}">${esc(a.label)}</option>`).join("");
      const skill = cat.skills
        .map((s) => `<option value="${esc(s)}"${s === "normal" ? " selected" : ""}>${esc(s)}</option>`).join("");
      return `<div class="msp-bot-form">
        <select class="msp-input js-bot-arch">${arch}</select>
        <select class="msp-input js-bot-skill">${skill}</select>
        <label class="msp-bot-when">enters at
          <input class="msp-input js-bot-when" type="text" value="now" placeholder="now or 2:30"></label>
        <button class="btn primary js-bot-add-btn">Add bot</button>
      </div>`;
    }

    async function addBot(root) {
      const btn = root.querySelector(".js-bot-add-btn");
      btn.disabled = true;
      try {
        await postJSON(`/market-sim-py/run/${runId}/bots`, {
          archetype: root.querySelector(".js-bot-arch").value,
          skill: root.querySelector(".js-bot-skill").value,
          activate_seconds: parseWhen(root.querySelector(".js-bot-when").value),
        });
        poll();
      } catch (err) {
        showMsg(msg, err.message, "error");
      } finally {
        btn.disabled = false;
      }
    }

    async function removeBot(uid) {
      try {
        await api(`/market-sim-py/run/${runId}/bots/${encodeURIComponent(uid)}`, { method: "DELETE" });
        poll();
      } catch (err) {
        showMsg(msg, err.message, "error");
      }
    }

    function renderBots(state) {
      // Render into whichever bots card is visible (lobby or live).
      const section = state.status === "lobby" ? $("#lobbyView") : $("#liveView");
      const root = section ? section.querySelector(".msp-bots-card") : null;
      if (!root) return;

      const bots = state.bots || [];
      const countEl = root.querySelector(".js-bots-count");
      if (countEl) countEl.textContent = `${bots.length} bot${bots.length === 1 ? "" : "s"}`;

      const listEl = root.querySelector(".js-bots-list");
      listEl.innerHTML = bots.map((b) => botRow(b, state.can_control)).join("")
        || '<p class="msp-muted">No bots in this run.</p>';
      listEl.querySelectorAll("[data-remove]").forEach((b) =>
        b.addEventListener("click", () => removeBot(b.dataset.remove)));

      const addEl = root.querySelector(".js-bots-add");
      if (state.can_control && catalog && !addEl.dataset.built) {
        addEl.dataset.built = "1";
        addEl.innerHTML = addFormHtml(catalog);
        addEl.querySelector(".js-bot-add-btn").addEventListener("click", () => addBot(root));
      } else if (!state.can_control) {
        addEl.innerHTML = "";
      }
    }

    // Counts #pnlNowText from its currently-displayed value up (or down) to
    // the real one over `duration`ms, instead of snapping straight to it —
    // a number that visibly moves reads as far more alive than a text swap.
    function tweenPnlText(el, from, to, duration) {
      cancelAnimationFrame(pnlTweenRaf);
      const start = performance.now();
      const startVal = isFinite(from) ? from : to;
      const step = (t0) => {
        const t = Math.min(1, (t0 - start) / duration);
        const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
        el.textContent = money(startVal + (to - startVal) * eased);
        if (t < 1) {
          pnlTweenRaf = requestAnimationFrame(step);
        } else {
          el.textContent = money(to); // land exactly on the real value, no float drift
        }
      };
      pnlTweenRaf = requestAnimationFrame(step);
    }

    // A handful of small sparks burst out of the number and fade — the
    // "coins flying" beat that makes a gain feel like a reward, not just a
    // color change. Gains only, on purpose: a loss shouldn't feel rewarding.
    // Element.animate() isn't covered by the prefers-reduced-motion CSS
    // rule below, so it's checked here directly.
    function spawnPnlParticles(container, count, power) {
      if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      // A burst that hasn't finished fading yet when the next one fires
      // (a fast run of gains in a row) shouldn't let particle nodes pile up
      // without bound — cap how many can be live on screen at once.
      const already = container.querySelectorAll(".msp-particle").length;
      count = Math.max(0, Math.min(count, 24 - already));
      // Same "how big is this move" factor the jump animation scales by —
      // a routine tick throws a few small, close sparks; a real swing
      // throws bigger ones that travel further, not just more of them.
      const scale = 0.8 + Math.max(0.4, Math.min(1.8, power || 0.6)) * 0.28;
      for (let i = 0; i < count; i++) {
        const p = document.createElement("span");
        p.className = "msp-particle";
        p.textContent = "•";
        container.appendChild(p);
        const dx = (Math.random() - 0.5) * 46 * scale;
        const dy = -(22 + Math.random() * 28) * scale;
        const rot = (Math.random() - 0.5) * 70;
        const anim = p.animate(
          [
            { transform: `translate(-50%, -50%) rotate(0deg) scale(${scale.toFixed(2)})`, opacity: 1 },
            { transform: `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px)) rotate(${rot}deg) scale(${(scale * 0.4).toFixed(2)})`, opacity: 0 },
          ],
          { duration: 650 + Math.random() * 200, easing: "cubic-bezier(.2,.7,.3,1)" },
        );
        anim.onfinish = () => p.remove();
      }
    }

    // The streak chip: consecutive gains build it up (bigger pop, brighter
    // glow, each one re-plays the pop animation), any loss clears it. This
    // is the escalating-reward loop — the same shape as a game combo meter.
    function updateStreakBadge(el, streak) {
      if (!el) return;
      if (streak >= 2) {
        el.textContent = `×${streak}`;
        el.style.setProperty("--streak-tier", String(Math.min(streak, 6)));
        el.classList.remove("is-on");
        void el.offsetWidth; // restart the pop animation on every new streak step
        el.classList.add("is-on");
      } else {
        el.classList.remove("is-on");
      }
    }

    // A dependency-free moving line chart of the player's P&L, drawn on a
    // canvas each poll. Green above zero, red below, with a faded area fill.
    function drawPnlChart() {
      const canvas = $("#pnlChart");
      if (!canvas || !pnlHistory.length) return;
      const now = $("#pnlNow");
      const nowText = $("#pnlNowText");
      const streakEl = $("#pnlStreak");
      const latest = pnlHistory[pnlHistory.length - 1].pnl;
      if (now && nowText) {
        // A jump-and-flash-green on a gain, a squeeze-and-flash-red on a
        // loss, whenever the figure actually moves since the last poll.
        const changed = lastPnlSeen !== null && latest !== lastPnlSeen;
        let power = 0.6;
        if (changed) {
          const moveSize = Math.abs(latest - lastPnlSeen);
          // Self-calibrating "how big is this move, for this game" — an
          // exponential running average of recent move sizes — rather than
          // a fixed dollar amount, since P&L scale depends on what's traded.
          avgMoveSize = avgMoveSize === null ? moveSize : avgMoveSize * 0.8 + moveSize * 0.2;
          power = avgMoveSize > 0 ? Math.max(0.4, Math.min(1.8, moveSize / avgMoveSize)) : 0.6;
          now.style.setProperty("--pop", power.toFixed(2));
        }
        // Computed from the same power as the jump/particles above, so a
        // real swing gets a beat longer on screen to register, not just a
        // bigger jump — a routine tick still lands close to instantly.
        const tweenMs = changed ? Math.round(300 + power * 130) : 380;
        tweenPnlText(nowText, lastPnlSeen === null ? latest : lastPnlSeen, latest, tweenMs);

        now.classList.remove("msp-up", "msp-down");
        now.classList.add(latest >= 0 ? "msp-up" : "msp-down");

        // classList add/remove (not a full className reset) so an
        // in-progress flash from the previous tick isn't cut short if this
        // tick's value happens to repeat it.
        if (changed) {
          const isGain = latest > lastPnlSeen;

          const flashClass = isGain ? "flash-gain" : "flash-loss";
          now.classList.remove("flash-gain", "flash-loss");
          void now.offsetWidth; // restart the animation even if it's the same class as last time
          now.classList.add(flashClass);
          clearTimeout(pnlFlashTimer);
          pnlFlashTimer = setTimeout(() => now.classList.remove(flashClass), 750);

          if (isGain) {
            gainStreak += 1;
            spawnPnlParticles(now, Math.min(3 + gainStreak, 9), power);
          } else {
            gainStreak = 0;
          }
          updateStreakBadge(streakEl, gainStreak);
        }
        lastPnlSeen = latest;
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
      $("#setupToggle").classList.toggle("hidden", inLobby);
      // Both boards live inside #liveView, so the Market/Empire switch would be
      // a dead control in the lobby. It appears with the boards it controls.
      $("#boardSwitch")?.classList.toggle("hidden", inLobby);
      document.body.classList.toggle("msp-live", !inLobby);

      if (state.can_control) await ensureCatalog();

      if (inLobby) {
        mountConnectInto("#lobbyConnect");
        renderPlayers(state.players);
        renderBots(state);
        $("#runClock")?.classList.add("hidden");
      } else {
        initSetupDrawer();
        mountConnectInto("#liveConnect");
        renderClock(state.seconds_left);
        renderBots(state);
        checkNewBots(state.bots);
        const meRow = renderLeaderboard(state.leaderboard, state.my_uid);
        renderMarket(state.market, state.status === "finished");
        renderMe(state.me, state.market, state.status, meRow);
        checkMilestones(meRow);
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
        if (window.AB && AB.feedback) AB.feedback.render("#fbkBox", state.feedback);
        if (state.status === "finished" && lastStatus === "running") {
          showMsg(msg, "Run complete — fair values are revealed and the leaderboard is final.", "ok");
          if (!winBannerShown && meRow && meRow.rank <= 3) {
            winBannerShown = true;
            showWinBanner(meRow);
          }
        }
      }

      lastStatus = state.status;
      schedule(state.status);
    }

    // The board is sized off the viewport, so the canvas has to be redrawn
    // when the window changes shape.
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(drawPnlChart, 120);
    });

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
