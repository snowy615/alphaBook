/* Market Simulation Py — lobby + live run.
 *
 * One file serves both pages: the rules/lobby page (create, join, browse runs)
 * and the run page (strategy editor while in the lobby, leaderboard once live).
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

  // ── Shared top bar ────────────────────────────────────────────────────
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

  // ── A minimal Python-friendly textarea ────────────────────────────────
  // Real code editing in a bare <textarea> is miserable — Tab leaves the
  // field and Enter drops you to column zero. This wires the few behaviours
  // that matter for writing indented Python without pulling in a library.
  const INDENT = "    ";

  function wireEditorKeys(el, onSubmit) {
    el.addEventListener("keydown", (e) => {
      // Cmd/Ctrl+Enter submits.
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        onSubmit();
        return;
      }

      const { selectionStart: start, selectionEnd: end, value } = el;

      if (e.key === "Tab") {
        e.preventDefault();
        const lineStart = value.lastIndexOf("\n", start - 1) + 1;
        if (e.shiftKey) {
          // Dedent the current line by up to one indent.
          const removable = value.slice(lineStart).match(/^[ ]{1,4}/);
          if (removable) {
            const n = removable[0].length;
            el.value = value.slice(0, lineStart) + value.slice(lineStart + n);
            el.selectionStart = el.selectionEnd = Math.max(lineStart, start - n);
          }
        } else if (start !== end) {
          // Indent every line touched by the selection.
          const block = value.slice(lineStart, end);
          const indented = block.replace(/^/gm, INDENT);
          el.value = value.slice(0, lineStart) + indented + value.slice(end);
          el.selectionStart = start + INDENT.length;
          el.selectionEnd = end + (indented.length - block.length);
        } else {
          el.value = value.slice(0, start) + INDENT + value.slice(end);
          el.selectionStart = el.selectionEnd = start + INDENT.length;
        }
        return;
      }

      if (e.key === "Enter") {
        // Carry the current line's indentation onto the new line, and add one
        // level after a colon — the usual Python cadence.
        const lineStart = value.lastIndexOf("\n", start - 1) + 1;
        const line = value.slice(lineStart, start);
        const indent = (line.match(/^[ \t]*/) || [""])[0];
        const extra = /:\s*$/.test(line) ? INDENT : "";
        if (indent || extra) {
          e.preventDefault();
          const insert = "\n" + indent + extra;
          el.value = value.slice(0, start) + insert + value.slice(end);
          el.selectionStart = el.selectionEnd = start + insert.length;
        }
      }
    });
  }

  // ── Run page ──────────────────────────────────────────────────────────
  function initRunPage(runId) {
    const msg = $("#msg");
    const editor = $("#codeEditor");
    let starter = "";
    let lastStatus = null;
    let editorLoaded = false;
    let pollTimer = null;

    // -- strategy editor -------------------------------------------------
    async function loadEditor() {
      if (editorLoaded) return;
      editorLoaded = true;
      try {
        const [tpl, mine] = await Promise.all([
          api("/market-sim-py/starter"),
          api(`/market-sim-py/run/${runId}/strategy`),
        ]);
        starter = tpl.code;
        editor.value = mine.code || starter;
        setCodeStatus(mine.status, mine.error);
      } catch (err) {
        showMsg(msg, err.message, "error");
      }
    }

    function setCodeStatus(status, error) {
      const pill = $("#codeStatus");
      if (!pill) return;
      if (status === "ready") {
        pill.textContent = "submitted";
        pill.className = "msp-pill msp-pill-ok";
      } else if (status === "error" || status === "disqualified") {
        pill.textContent = error || status;
        pill.className = "msp-pill msp-pill-bad";
      } else {
        pill.textContent = "not submitted";
        pill.className = "msp-pill";
      }
    }

    function renderCheck(problems, okText) {
      const out = $("#checkOut");
      if (!out) return;
      if (!problems.length) {
        out.className = "msp-check msp-check-ok";
        out.textContent = okText;
      } else {
        out.className = "msp-check msp-check-bad";
        out.innerHTML = problems.map((p) => `<div>${esc(p)}</div>`).join("");
      }
    }

    $("#checkBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const res = await postJSON("/market-sim-py/check", { code: editor.value });
        renderCheck(res.problems, "Looks good — this strategy compiles and defines on_tick(ctx).");
      } catch (err) {
        renderCheck([err.message]);
      } finally {
        e.target.disabled = false;
      }
    });

    $("#submitBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        await postJSON(`/market-sim-py/run/${runId}/strategy`, { code: editor.value });
        renderCheck([], "Strategy submitted. You are in.");
        setCodeStatus("ready");
      } catch (err) {
        renderCheck([err.message]);
        setCodeStatus("error", err.message);
      } finally {
        e.target.disabled = false;
      }
    });

    $("#resetBtn")?.addEventListener("click", () => {
      if (starter) editor.value = starter;
    });

    wireEditorKeys(editor, () => $("#submitBtn")?.click());

    // -- host controls ---------------------------------------------------
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
          <span class="msp-pill ${p.has_code ? "msp-pill-ok" : ""}">${p.has_code ? "ready" : "writing"}</span>
        </div>`).join("") || `<p class="msp-muted">Nobody has joined yet.</p>`;
    }

    // A player whose strategy is still "ready" has simply survived — say so in
    // the tense the run is actually in.
    function statusLabel(status, runStatus) {
      if (status !== "ready") return status;
      return runStatus === "finished" ? "finished" : "running";
    }

    function renderLeaderboard(rows, runStatus) {
      const table = $("#leaderboard");
      if (!table) return;
      table.innerHTML = `
        <thead><tr><th>#</th><th>Player</th><th class="msp-num">P&amp;L</th>
        <th class="msp-num">Fills</th><th>Status</th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="${r.is_bot ? "msp-bot-row" : ""}">
            <td>${r.rank}</td>
            <td>${esc(r.username)}${r.is_bot ? ' <span class="msp-tag">bot</span>' : ""}</td>
            <td class="msp-num ${r.pnl >= 0 ? "msp-up" : "msp-down"}">${money(r.pnl)}</td>
            <td class="msp-num">${r.fills}</td>
            <td>${r.status === "disqualified"
              ? `<span class="msp-pill msp-pill-bad" title="${esc(r.error)}">DQ</span>`
              : `<span class="msp-muted">${esc(r.is_bot ? r.status : statusLabel(r.status, runStatus))}</span>`}</td>
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
        if (status) status.textContent = "spectator";
        return;
      }

      if (status) {
        status.textContent = statusLabel(me.status, runStatus);
        status.className = "msp-pill " + (me.status === "ready" ? "msp-pill-ok" : "msp-pill-bad");
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
              ${me.orders_rejected ? `<span class="msp-down">/ ${me.orders_rejected} rejected</span>` : ""}</span></div>`;
      }

      if (positions) {
        const limit = window.POSITION_LIMIT || 1000;
        positions.innerHTML = `
          <thead><tr><th>Item</th><th class="msp-num">Position</th><th>Limit use</th></tr></thead>
          <tbody>${items.map((it) => {
            const q = me.positions[it.item] || 0;
            const pct = Math.min(100, Math.round((Math.abs(q) / limit) * 100));
            return `<tr>
              <td>${esc(it.item)}</td>
              <td class="msp-num ${q > 0 ? "msp-up" : q < 0 ? "msp-down" : ""}">${q}</td>
              <td><div class="msp-meter"><div class="msp-meter-fill${pct >= 95 ? " msp-meter-full" : ""}"
                style="width:${pct}%"></div></div></td>
            </tr>`;
          }).join("")}</tbody>`;
      }

      const log = $("#myLog");
      if (log) {
        const wasAtBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
        log.innerHTML = (me.logs || []).map((l) => `
          <div class="msp-log-line msp-log-${esc(l.kind)}">
            <span class="msp-log-tick">t${l.tick}</span>${esc(l.text)}
          </div>`).join("") || `<div class="msp-muted msp-log-empty">No output yet.</div>`;
        if (wasAtBottom) log.scrollTop = log.scrollHeight;
      }
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
      if (status === "finished") return;          // nothing left to advance
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
        loadEditor();
        renderPlayers(state.players);
        $("#runClock")?.classList.add("hidden");
      } else {
        renderClock(state.seconds_left);
        renderLeaderboard(state.leaderboard, state.status);
        renderMarket(state.market, state.status === "finished");
        renderMe(state.me, state.market, state.status);
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
