(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);

  const esc = (s) => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const num = (v, unit) => {
    if (v === null || v === undefined || !isFinite(+v)) return "—";
    const n = +v;
    const body = Math.abs(n) >= 1000
      ? n.toLocaleString(undefined, { maximumFractionDigits: 0 })
      : n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (unit === "$") return (n < 0 ? "-$" : "$") + body.replace("-", "");
    if (unit === "%") return body + "%";
    return body;
  };

  const TREND = {
    improving: { icon: "▲", cls: "lbx-up", label: "improving" },
    slipping: { icon: "▼", cls: "lbx-down", label: "slipping" },
    steady: { icon: "―", cls: "lbx-flat", label: "steady" },
    new: { icon: "·", cls: "lbx-flat", label: "too few games" },
  };

  let DATA = null;
  let active = "overall";

  function ratingCell(rating, provisional) {
    const pct = Math.max(0, Math.min(100, +rating || 0));
    return `
      <div class="lbx-rating">
        <span class="lbx-rating-num${provisional ? " lbx-prov" : ""}">${(+rating).toFixed(1)}</span>
        <span class="lbx-bar"><span class="lbx-bar-fill" style="width:${pct}%"></span></span>
        ${provisional ? '<span class="lbx-prov-tag" title="Provisional until enough games">prov</span>' : ""}
      </div>`;
  }

  function renderTabs() {
    const tabs = [{ key: "overall", label: "Overall" }].concat(
      DATA.modes.map((m) => ({ key: m.key, label: m.label }))
    );
    $("#lbTabs").innerHTML = tabs.map((t) => {
      const n = t.key === "overall"
        ? DATA.overall.length
        : (DATA.mode_boards[t.key] || []).length;
      return `<button class="lbx-tab${t.key === active ? " lbx-tab-on" : ""}"
                data-tab="${esc(t.key)}">${esc(t.label)}<span class="lbx-tab-n">${n}</span></button>`;
    }).join("");

    $("#lbTabs").querySelectorAll(".lbx-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        active = btn.dataset.tab;
        try { localStorage.setItem("lbTab", active); } catch (_) { }
        renderTabs();
        renderBoard();
      });
    });
  }

  function renderOverall() {
    const modes = DATA.modes;
    $("#lbHead").innerHTML = `
      <tr>
        <th class="lbx-w-rank">#</th>
        <th>Player</th>
        <th class="lbx-w-rating">Overall</th>
        <th class="lbx-w-mid">Modes</th>
        <th class="lbx-w-mid">Games</th>
        <th>Per-mode ratings</th>
      </tr>`;

    if (!DATA.overall.length) {
      $("#lbBody").innerHTML = `<tr><td colspan="6" class="lbx-empty">
        No scored games yet. Finish any mode and the board fills in.</td></tr>`;
      return;
    }

    $("#lbBody").innerHTML = DATA.overall.map((r) => {
      const chips = modes
        .filter((m) => r.ratings && r.ratings[m.key])
        .map((m) => {
          const g = r.ratings[m.key];
          return `<span class="lbx-chip${g.provisional ? " lbx-chip-prov" : ""}"
                    title="${esc(m.label)} · rating ${g.rating} over ${g.games} games">
                    ${esc(m.label.split(" ")[0])} ${g.rating}</span>`;
        }).join("");
      return `
        <tr class="${r.is_me ? "lbx-me" : ""}">
          <td class="lbx-rank">${r.rank}</td>
          <td class="lbx-name">${esc(r.username)}${r.is_me ? '<span class="lbx-you">you</span>' : ""}</td>
          <td>${ratingCell(r.overall, false)}</td>
          <td class="lbx-mid">${r.modes_played}/${modes.length}</td>
          <td class="lbx-mid">${r.total_games}</td>
          <td class="lbx-chips">${chips || '<span class="msp-muted">—</span>'}</td>
        </tr>`;
    }).join("");
  }

  // How someone trades in a mode, over their games: their usual style and
  // whether their edge or their direction made the money.
  const STYLE = { market_maker: "Market maker", directional: "Directional", taker: "Liquidity taker",
                  mixed: "Mixed", idle: "Didn't trade" };
  function strategyCell(st, unit) {
    if (!st) return '<span class="msp-muted">—</span>';
    const parts = [];
    if (st.style) parts.push(`<strong>${esc(STYLE[st.style] || st.style)}</strong>${st.games > 1 ? ` <span class="msp-muted">${st.style_games}/${st.games}</span>` : ""}`);
    // Your own row has the figures; other players' only coarse words, so
    // nobody can copy someone else's numbers.
    if (st.edge_per_game != null || st.direction_per_game != null) {
      parts.push(`<span title="Your average per game: trading edge (prices against fair) and direction (inventory as fair moved)">edge ${num(st.edge_per_game, unit)} · direction ${num(st.direction_per_game, unit)}</span>`);
    } else if (st.made_on) {
      parts.push(`<span title="Whether pricing or holding inventory drove their results">made on ${esc(st.made_on)}</span>`);
    }
    if (st.direction_hit != null) parts.push(`<span title="Share of what you traded that went the way the price then went">right way ${Math.round(100 * st.direction_hit)}%</span>`);
    else if (st.call) parts.push(`<span title="How often their position went the way the price then went">direction ${esc(st.call)}</span>`);
    return `<div class="lbx-strategy">${parts.join("<br>")}</div>`;
  }

  function renderMode(key) {
    const meta = DATA.modes.find((m) => m.key === key) || {};
    const rows = DATA.mode_boards[key] || [];
    const calibrated = meta.kind === "per_game";
    const hasStrategy = rows.some((r) => r.strategy);
    const cols = 6 + (calibrated ? 1 : 0) + (hasStrategy ? 1 : 0);

    $("#lbHead").innerHTML = `
      <tr>
        <th class="lbx-w-rank">#</th>
        <th>Player</th>
        <th class="lbx-w-rating">Rating</th>
        ${calibrated ? '<th class="lbx-w-mid" title="Calibrated skill: results rated against who and what each game was played against. Everyone starts at 1000.">Skill</th>' : ""}
        ${hasStrategy ? "<th>Strategy</th>" : ""}
        <th class="lbx-w-mid" title="Raw average, before calibration">Avg ${esc((meta.metric || "Score").toLowerCase())}</th>
        <th class="lbx-w-mid">Games</th>
        <th class="lbx-w-mid">Trend</th>
      </tr>`;

    if (!rows.length) {
      $("#lbBody").innerHTML = `<tr><td colspan="${cols}" class="lbx-empty">
        Nobody has finished ${esc(meta.label || "this mode")} yet —
        <a href="${esc(meta.href || "/")}">be first</a>.</td></tr>`;
      return;
    }

    $("#lbBody").innerHTML = rows.map((r) => {
      const t = TREND[r.trend] || TREND.new;
      return `
        <tr class="${r.is_me ? "lbx-me" : ""}">
          <td class="lbx-rank">${r.rank}</td>
          <td class="lbx-name">${esc(r.username)}${r.is_me ? '<span class="lbx-you">you</span>' : ""}</td>
          <td>${ratingCell(r.rating, r.provisional)}</td>
          ${calibrated ? `<td class="lbx-mid lbx-skill">${r.skill != null ? r.skill : "—"}</td>` : ""}
          ${hasStrategy ? `<td>${strategyCell(r.strategy, meta.unit)}</td>` : ""}
          <td class="lbx-mid">${num(r.value, meta.unit)}</td>
          <td class="lbx-mid">${r.games}</td>
          <td class="lbx-mid ${t.cls}" title="${t.label}">${t.icon}</td>
        </tr>`;
    }).join("");
  }

  function renderBoard() {
    if (active === "overall") {
      $("#lbBoardTitle").textContent = "Overall";
      $("#lbBoardNote").textContent = "average of modes played + breadth bonus";
      renderOverall();
    } else {
      const meta = DATA.modes.find((m) => m.key === active) || {};
      $("#lbBoardTitle").textContent = meta.label || active;
      $("#lbBoardNote").textContent = meta.kind === "per_game"
        ? "ranked on calibrated skill, not the raw average" : (meta.blurb || "");
      renderMode(active);
    }
  }

  function renderMe() {
    $("#lbTotal").textContent = DATA.total_players;
    $("#lbMinGames").textContent = DATA.min_games_full;

    const me = DATA.me;
    if (!me) {
      $("#lbMyRank").textContent = "—";
      $("#lbMyRankSub").textContent = DATA.signed_in ? "no scored games yet" : "sign in to see yours";
      $("#lbMyOverall").textContent = "—";
      $("#lbMyBest").textContent = "—";
      return;
    }

    $("#lbMyRank").textContent = "#" + me.rank;
    $("#lbMyRankSub").textContent = `of ${DATA.total_players}`;
    $("#lbMyOverall").textContent = (+me.overall).toFixed(1);
    $("#lbMyModes").textContent = `${me.modes_played} of ${DATA.modes.length} modes`;

    const entries = Object.entries(me.ratings || {});
    if (entries.length) {
      entries.sort((a, b) => b[1].rating - a[1].rating);
      const [key, val] = entries[0];
      const meta = DATA.modes.find((m) => m.key === key) || {};
      $("#lbMyBest").textContent = meta.label || key;
      $("#lbMyBestSub").textContent = `rating ${val.rating} over ${val.games} games`;
    }
  }

  async function load() {
    try {
      const res = await fetch("/api/leaderboard", { credentials: "include" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      DATA = await res.json();

      try {
        const saved = localStorage.getItem("lbTab");
        if (saved && (saved === "overall" || DATA.mode_boards[saved])) active = saved;
      } catch (_) { }

      renderMe();
      renderTabs();
      renderBoard();

      const nameEl = $("#userName");
      if (nameEl && DATA.me) nameEl.textContent = DATA.me.username;
    } catch (err) {
      const box = $("#msg");
      box.textContent = "Could not load the leaderboard: " + err.message;
      box.classList.remove("hidden");
      box.classList.add("msp-msg-error");
      $("#lbBody").innerHTML = '<tr><td colspan="6" class="lbx-empty">—</td></tr>';
    }
  }

  load();
  setInterval(load, 30000);
})();
