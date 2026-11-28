/* Lone Bid — table client.
 *
 * Two pages share this file: the rules page (create/join) and the table.
 * The table polls once a second; the server closes a phase whose time is up
 * on read, so the clock keeps going even if a browser goes away.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  async function api(url, body) {
    const opts = { credentials: "include" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(url, opts);
    const txt = await r.text();
    let data = {};
    try { data = txt ? JSON.parse(txt) : {}; } catch { /* not JSON */ }
    if (!r.ok) {
      const e = new Error(data.detail || `HTTP ${r.status}`);
      e.status = r.status;
      throw e;
    }
    return data;
  }

  function flash(text, bad) {
    const box = $("#msg");
    if (!box) return;
    box.textContent = text;
    box.className = "msp-msg msp-msg-" + (bad ? "error" : "ok");
    box.classList.remove("hidden");
    clearTimeout(flash.t);
    flash.t = setTimeout(() => box.classList.add("hidden"), 4200);
  }

  const CARDS = [1, 2, 3, 4, 5, 6, 7, 8];
  const STATUS = {
    survived: ["lbd-b-safe", "survived"],
    winner: ["lbd-b-prize", "won the prize"],
    runner_up: ["lbd-b-safe", "survived the final"],
    eliminated: ["lbd-b-out", "eliminated"],
  };
  const LEVEL_HINT = {
    noob: "plays at random",
    normal: "sensible, a little noisy",
    good: "always takes the best odds",
    cracked: "assumes you're thinking too",
  };

  // ── Rules page ────────────────────────────────────────────────────────
  function initRulesPage() {
    $("#createBtn")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const res = await api("/lone-bid/create", {});
        window.location.href = `/lone-bid/game/${res.game_id}`;
      } catch (err) {
        flash(err.status === 401 ? "Please log in first." : err.message, true);
        e.target.disabled = false;
      }
    });
    const join = async () => {
      const code = ($("#joinCode")?.value || "").trim().toUpperCase();
      if (code.length !== 6) { flash("Join codes are six characters.", true); return; }
      try {
        const res = await api("/lone-bid/join", { join_code: code });
        window.location.href = `/lone-bid/game/${res.game_id}`;
      } catch (err) {
        flash(err.status === 401 ? "Please log in first." : err.message, true);
      }
    };
    $("#joinBtn")?.addEventListener("click", join);
    $("#joinCode")?.addEventListener("keydown", (e) => { if (e.key === "Enter") join(); });
  }

  // ── Table ─────────────────────────────────────────────────────────────
  function initTable(gameId) {
    let state = null;
    let picked = [];           // cards chosen to reveal, before sending
    let pickKey = "";          // round+phase the picks belong to
    let busy = false;
    let botLevel = "normal";

    async function poll() {
      if (busy) return;
      try {
        state = await api(`/lone-bid/game/${gameId}/state`);
        render();
      } catch (err) {
        if (err.status === 401) window.location.href = "/login";
      }
    }

    async function send(path, body) {
      busy = true;
      let fresh = false;
      try {
        const res = await api(`/lone-bid/game/${gameId}/${path}`, body);
        if (res && res.players) state = res; else fresh = true;   // start returns no table
        picked = [];
      } catch (err) {
        flash(err.message, true);
      }
      busy = false;
      if (fresh) await poll(); else render();
    }

    const name = (p) => `${esc(p.username)}${p.is_me ? ' <span class="lbd-you">you</span>' : ""}`;

    // A hand as eight little cards: gone ones dim, the locked one outlined.
    function handStrip(p) {
      return `<span class="lbd-strip">${CARDS.map((c) => {
        const gone = !p.hand.includes(c);
        const locked = p.locked === c && !gone;
        return `<span class="lbd-mini${gone ? " is-gone" : ""}${locked ? " is-locked" : ""}"
          title="${gone ? `${c} played` : locked ? `${c} locked ${state.phase === "result" ? "next" : "this"} round` : `${c} in hand`}">${c}</span>`;
      }).join("")}</span>`;
    }

    function badge(p) {
      if (STATUS[p.status]) {
        const [cls, text] = STATUS[p.status];
        return `<span class="lbd-badge ${cls}">${text}${p.status === "survived" && p.out_round ? ` · R${p.out_round}` : ""}</span>`;
      }
      if (p.ready === true) return '<span class="lbd-badge lbd-b-ready">ready</span>';
      if (p.ready === false) return '<span class="lbd-badge lbd-b-wait">choosing</span>';
      return "";
    }

    function pairCell(p) {
      const r = state.result;
      if (!p.revealed) return '<span class="msp-muted">—</span>';
      return p.revealed.map((c) => {
        const played = p.played === c && (state.phase === "result" || state.phase === "done" || p.is_me);
        const cancelled = played && r && r.cancelled.includes(c);
        const won = played && r && r.winner_id === p.user_id;
        return `<span class="lbd-card-sm${played ? " is-played" : ""}${cancelled ? " is-cancelled" : ""}${won ? " is-won" : ""}">${c}</span>`;
      }).join("");
    }

    function tableRows() {
      const live = state.players.filter((p) => p.status === "active");
      const done = state.players.filter((p) => p.status !== "active");
      const rows = [...live.sort((a, b) => b.score - a.score || b.tokens - a.tokens), ...done];
      // In the final, where each player would finish if it ended now.
      const PLACE = [["lbd-b-prize", "1st · prize"], ["lbd-b-safe", "2nd · safe"], ["lbd-b-out", "3rd · eliminated"]];
      const place = (p) => {
        if (!state.final_stage || p.status !== "active" || live.length !== 3) return "";
        const [cls, text] = PLACE[live.indexOf(p)] || [];
        return cls ? `<span class="lbd-badge lbd-place ${cls}">${text}</span>` : "";
      };
      return rows.map((p) => {
        const strip = p.status === "active" ? handStrip(p) : '<span class="msp-muted">left the table</span>';
        return `
        <tr class="${p.is_me ? "is-me" : ""}${p.status !== "active" ? " is-off" : ""}${state.result && state.result.winner_id === p.user_id ? " is-winner" : ""}">
          <td class="lbd-name">${name(p)} ${badge(p)} ${place(p)}<div class="lbd-hand-inline">${strip}</div></td>
          <td class="lbd-num">${p.status === "active" ? p.score : "—"}</td>
          <td class="lbd-num">${p.tokens}</td>
          <td class="lbd-pair">${pairCell(p)}</td>
          <td class="lbd-hand-col">${strip}</td>
        </tr>`;
      }).join("");
    }

    function header() {
      const steps = [["reveal", "Reveal two"], ["play", "Play one"], ["result", "Result"]];
      const left = state.seconds_left, total = state.phase_total || 1;
      const pct = Math.max(0, Math.min(100, (left / total) * 100));
      const urgent = state.phase !== "result" && left <= 10;
      let cp;
      if (state.final_stage) cp = `The final: after round ${state.rounds} the top scorer wins the prize and last place is eliminated.`;
      else if (state.is_checkpoint_round) cp = "Checkpoint round: after this round the top scorer survives and leaves.";
      else cp = `Next checkpoint after round ${state.next_checkpoint}: the top scorer then survives.`;
      const live = state.players.filter((p) => p.status === "active");
      const top = live.slice().sort((a, b) => b.score - a.score || b.tokens - a.tokens)[0];
      const leader = top && top.score > 0 ? ` Leading: <strong>${esc(top.username)}</strong> on ${top.score}.` : "";
      return `<div class="lbd-head card">
        <div class="lbd-head-main">
          <div class="lbd-round">Round ${state.round} <span>of ${state.rounds} · set ${state.set} of 3</span></div>
          <div class="lbd-cp${state.is_checkpoint_round || state.final_stage ? " is-hot" : ""}">${cp}${leader}</div>
          <ol class="lbd-steps">${steps.map(([k, label], i) =>
            `<li class="${state.phase === k ? "is-on" : ""}">${i + 1} · ${label}</li>`).join("")}</ol>
        </div>
        <div class="lbd-clock${urgent ? " is-urgent" : ""}"><div class="lbd-secs">${left}<span>s</span></div>
          <div class="lbd-bar"><span style="width:${pct}%"></span></div></div>
      </div>`;
    }

    const SETTLED = { tokens: " (level on points, ahead on tokens)", draw: " (level on points and tokens, settled by a draw)" };

    function resultPanel() {
      const r = state.result;
      if (!r) return "";
      const head = r.winner
        ? `<strong>${esc(r.winner)}</strong> wins round ${r.round} with a lone <strong>${r.value}</strong> (+${r.value} and a token).`
        : `Round ${r.round}: every number was matched, so nobody wins.`;
      const extra = [];
      if (r.cancelled.length) extra.push(`Cancelled: ${r.cancelled.join(", ")}.`);
      let banner = "";
      if (r.checkpoint) {
        banner = `<div class="lbd-banner">Checkpoint: <strong>${esc(r.checkpoint.username)}</strong> survives with
          ${r.checkpoint.score} points and ${r.checkpoint.tokens} token${r.checkpoint.tokens === 1 ? "" : "s"}${SETTLED[r.checkpoint.decided_by] || (r.checkpoint.draw ? SETTLED.draw : "")}.
          Everyone else goes back to zero points and keeps their tokens.</div>`;
      }
      if (r.final) {
        const d = r.final_decided || {};
        const last = r.final[r.final.length - 1];
        banner = `<div class="lbd-banner">The final is over: <strong>${esc(r.final[0].username)}</strong> wins the prize${SETTLED[d.prize] || ""}${r.final.length > 2 ? `, ${esc(r.final[1].username)} survives` : ""}, and
          <strong>${esc(last.username)}</strong> is eliminated${SETTLED[d.elimination] || ""}.</div>`;
      }
      if (r.reset) extra.push("Hands reset: everyone has 1 to 8 again.");
      const me = state.players.find((p) => p.is_me);
      let mine = "";
      if (me && me.played) {
        if (r.winner_id === me.user_id) mine = `Your ${me.played} won the round.`;
        else if (r.cancelled.includes(me.played)) mine = `Your ${me.played} was matched, so it cancelled out.`;
        else if (r.value) mine = `Your ${me.played} stood alone, but ${r.value} was lower.`;
      }
      return `<div class="lbd-result">${head} <span class="msp-muted">${extra.join(" ")}</span>
        ${mine ? `<div class="lbd-mine">${mine}</div>` : ""}${banner}</div>`;
    }

    function previousLine() {
      const r = state.previous;
      if (!r) return "";
      const what = r.winner ? `${esc(r.winner)} won with a lone ${r.value}` : "nobody won (every number was matched)";
      return `<div class="lbd-prev">Last round: ${what}${r.cancelled.length ? `; ${r.cancelled.join(", ")} cancelled` : ""}.</div>`;
    }

    // How many of the others showed each number, for the play decision.
    function shownByOthers() {
      const counts = {};
      state.players.forEach((p) => {
        if (p.is_me || p.status !== "active" || !p.revealed) return;
        p.revealed.forEach((c) => { counts[c] = (counts[c] || 0) + 1; });
      });
      return counts;
    }

    function actionPanel() {
      const my = state.my;
      if (!my) return '<div class="lbd-action msp-muted">You\'re watching this table.</div>';
      if (state.phase === "result") return "";
      if (my.status !== "active") {
        const OUT = {
          survived: "You survived the checkpoint and are safe.",
          winner: "You won the prize.",
          runner_up: "You survived the final.",
          eliminated: "You've been eliminated.",
        };
        return `<div class="lbd-action">${OUT[my.status] || "You're out of this game."} You can keep watching.</div>`;
      }
      const key = `${state.round}-${state.phase}`;
      if (key !== pickKey) {
        pickKey = key;
        picked = my.available.length === 2 ? my.available.slice() : [];   // no choice to make
      }
      const waiting = state.players.filter((p) => p.status === "active" && p.ready === false && !p.is_me).length;

      if (state.phase === "reveal") {
        if (my.revealed) {
          return `<div class="lbd-action">You revealed <span class="lbd-card-sm">${my.revealed[0]}</span>
            <span class="lbd-card-sm">${my.revealed[1]}</span>. Waiting for ${waiting} more…</div>`;
        }
        return `<div class="lbd-action">
          <div class="lbd-prompt">${my.available.length === 2
            ? `Only ${my.available[0]} and ${my.available[1]} can be shown this round, so they're picked for you.`
            : "Pick two cards to show the table. You'll play one of them."}</div>
          <div class="lbd-hand">${CARDS.map((c) => {
            const has = my.hand.includes(c), locked = my.locked === c && has, ok = my.available.includes(c);
            return `<button type="button" class="lbd-card${picked.includes(c) ? " is-picked" : ""}${locked ? " is-locked" : ""}${!has ? " is-gone" : ""}"
              data-card="${c}" ${ok ? "" : "disabled"} title="${!has ? "Played this set" : locked ? "Locked this round" : ""}">${c}${locked ? '<span class="lbd-lock">locked</span>' : ""}</button>`;
          }).join("")}</div>
          <button class="btn primary" id="revealBtn" ${picked.length === 2 ? "" : "disabled"}>${picked.length === 2 ? `Reveal ${picked.slice().sort((a, b) => a - b).join(" and ")}` : "Pick two"}</button>
        </div>`;
      }
      if (state.phase === "play") {
        if (my.played) {
          return `<div class="lbd-action">You played <span class="lbd-card-sm is-played">${my.played}</span>. Waiting for ${waiting} more…</div>`;
        }
        const shown = shownByOthers();
        const others = state.players.filter((p) => !p.is_me && p.status === "active").length;
        return `<div class="lbd-action">
          <div class="lbd-prompt">Play one of your two. The other is locked next round.</div>
          <div class="lbd-hand">${(my.revealed || []).map((c) => {
            const n = shown[c] || 0;
            return `<button type="button" class="lbd-card lbd-card-play" data-play="${c}">${c}
              <span class="lbd-play-label">Play ${c}</span>
              <span class="lbd-shown${n ? " is-risky" : ""}">${n ? `${n} other${n === 1 ? "" : "s"} showed ${c}` : "nobody else showed it"}</span></button>`;
          }).join("")}</div>
          <div class="lbd-tally">On show from the other ${others}: ${CARDS.filter((c) => shown[c]).map((c) =>
            `<span class="lbd-tally-item"><strong>${c}</strong>×${shown[c]}</span>`).join("") || "nothing"}</div>
        </div>`;
      }
      return "";
    }

    function lobby() {
      const empty = state.seats - state.players.length;
      return `<div class="lbd-lobby">
        <div class="card msp-panel"><div class="head"><h3>Table</h3><span class="msp-muted">${state.players.length} of ${state.seats} seated</span></div>
          <div class="msp-panel-body">
            <div class="lbd-code-row"><span class="msp-muted">Join code</span><strong class="lbd-code">${esc(state.join_code)}</strong>
              <button class="btn ghost" id="copyCode" type="button">Copy</button></div>
            <p class="msp-muted" style="margin:0 0 8px;">Friends join at alphabook.uk/lone-bid with this code.</p>
            <ul class="lbd-seats">${state.players.map((p) => `<li>${name(p)}</li>`).join("")}
              ${Array.from({ length: empty }, () => '<li class="msp-muted">empty seat: a bot takes it</li>').join("")}</ul>
          </div></div>
        <div class="card msp-panel"><div class="head"><h3>${state.is_host ? "Start" : "Waiting for the host"}</h3></div>
          <div class="msp-panel-body">
          ${state.is_host ? `
            <label class="msp-label" for="botLevel">Bots for the empty seats</label>
            <select id="botLevel" class="msp-input">${state.bot_levels.map((l) => `<option value="${l}"${l === botLevel ? " selected" : ""}>${l}: ${LEVEL_HINT[l]}</option>`).join("")}</select>
            <button class="btn primary" id="startBtn" style="margin-top:10px;">${empty ? `Start with ${empty} bot${empty === 1 ? "" : "s"}` : "Start"}</button>
            <p class="msp-muted">Share the code first if you want friends at the table. Once it starts, nobody else can join.</p>`
          : `<p class="msp-muted">${esc(state.host_name)} will start the game. Empty seats are filled with bots.</p>
             ${state.joined ? '<button class="btn ghost" id="leaveBtn">Give up my seat</button>' : ""}`}
          </div></div>
        <div class="card msp-panel lbd-howto-wide">${howTo()}</div>
      </div>`;
    }

    function howTo() {
      return `<div class="head"><h3>How to win a round</h3></div><div class="msp-panel-body"><ol class="lbd-howto">
        <li>Pick two cards to show. Everyone's pairs appear at once.</li>
        <li>Play one of your two. The other can't be shown next round.</li>
        <li>Numbers played twice or more cancel. The <strong>lowest number left</strong> wins its value in points, plus a token.</li>
        <li>Top scorer after rounds 3, 6, 9 and 12 survives. Points reset, tokens stay and break ties.</li>
      </ol></div>`;
    }

    function finished() {
      const rows = (state.results || []).map((r, i) => `
        <tr class="${r.user_id === state.me ? "is-me" : ""}">
          <td>${i + 1}</td><td>${esc(r.username)}${r.user_id === state.me ? ' <span class="lbd-you">you</span>' : ""}</td>
          <td><span class="lbd-badge ${(STATUS[r.status] || [""])[0]}">${esc(r.label)}${r.status === "survived" ? ` · round ${r.out_round}` : ""}</span></td>
          <td class="lbd-num">${r.rounds_won}</td><td class="lbd-num">${r.points_total}</td></tr>`).join("");
      return `<div class="card msp-panel"><div class="head"><h3>Game over</h3></div>
        <div class="msp-panel-body"><div class="lbd-scroll"><table class="msp-table lbd-final">
          <thead><tr><th></th><th>Player</th><th>Outcome</th><th class="lbd-num">Rounds won (tokens)</th><th class="lbd-num">Points scored</th></tr></thead>
          <tbody>${rows}</tbody></table></div>
          <div id="fbkBox" style="margin-top:14px;"></div>
          <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;">
            <button class="btn primary" id="againBtn" type="button">Play again</button>
            <a class="btn ghost" href="/leaderboard">Leaderboard</a></div></div></div>`;
    }

    function sidePanel() {
      const hist = (state.history || []).slice().reverse().map((h) =>
        `<li><span class="msp-muted">R${h.round}</span> ${h.winner ? `${esc(h.winner)} · ${h.value}` : '<span class="msp-muted">no winner</span>'}${h.survivor
          ? `<div class="lbd-hist-cp">Checkpoint: ${esc(h.survivor)} survives</div>` : ""}</li>`).join("");
      return `<div class="card msp-panel"><div class="head"><h3>Rounds</h3></div>
        <div class="msp-panel-body"><ul class="lbd-hist">${hist || '<li class="msp-muted">No rounds yet.</li>'}</ul></div></div>
        <div class="card msp-panel">${howTo()}</div>`;
    }

    // Only redraw what changed: the clock ticks every second, but rebuilding
    // the cards or an open dropdown under someone's mouse would lose clicks.
    const drawn = {};
    function put(id, html, after) {
      const el = document.getElementById(id);
      if (!el || drawn[id] === html) return;
      drawn[id] = html;
      el.innerHTML = html;
      if (after) after();
    }

    function render() {
      const area = $("#gameArea");
      if (!state || !area) return;
      const mode = state.status === "playing" ? "table" : state.status;
      if (area.dataset.mode !== mode) {
        area.dataset.mode = mode;
        Object.keys(drawn).forEach((k) => delete drawn[k]);
        area.innerHTML = mode === "table"
          ? `<div id="lbdHead"></div><div class="lbd-board"><div class="lbd-main" id="lbdMain"></div>
               <div class="lbd-side" id="lbdSide"></div></div>`
          : '<div id="lbdWhole"></div>';
      }
      if (mode === "lobby") { put("lbdWhole", lobby()); return; }
      if (mode === "finished") {
        put("lbdWhole", finished(), () => { if (window.AB && AB.feedback) AB.feedback.render("#fbkBox", state.feedback); });
        return;
      }
      put("lbdHead", header());
      put("lbdMain", `${resultPanel()}${previousLine()}${actionPanel()}
        <div class="card lbd-scroll"><table class="msp-table lbd-table">
          <thead><tr><th>Player</th><th class="lbd-num"><span class="lbd-long">Points</span><span class="lbd-short">Pts</span></th>
            <th class="lbd-num"><span class="lbd-long">Tokens</span><span class="lbd-short">Tok</span></th>
            <th>${state.phase === "reveal" ? "Pair" : "Pair · played"}</th><th class="lbd-hand-col">Cards left</th></tr></thead>
          <tbody>${tableRows()}</tbody></table>
          <div class="lbd-key"><span><span class="lbd-card-sm is-won">2</span> won</span>
            <span><span class="lbd-card-sm is-cancelled">3</span> matched, cancelled</span>
            <span><span class="lbd-card-sm is-played">5</span> played</span>
            <span><span class="lbd-mini is-locked">4</span> locked ${state.phase === "result" ? "next" : "this"} round</span>
            <span><span class="lbd-mini is-gone">6</span> gone this set</span></div></div>`);
      put("lbdSide", sidePanel());
    }

    document.addEventListener("click", (e) => {
      const card = e.target.closest("[data-card]");
      if (card && !card.disabled) {
        const c = Number(card.dataset.card);
        if (picked.includes(c)) picked = picked.filter((x) => x !== c);
        else if (picked.length < 2) picked.push(c);
        else picked = [picked[1], c];
        render();
        return;
      }
      const playBtn = e.target.closest("[data-play]");
      if (playBtn) { send("play", { card: Number(playBtn.dataset.play) }); return; }
      if (e.target.closest("#revealBtn") && picked.length === 2) { send("reveal", { cards: picked }); return; }
      if (e.target.closest("#startBtn")) { send("start", { bot_level: botLevel }); return; }
      if (e.target.closest("#copyCode")) {
        navigator.clipboard.writeText(state.join_code).then(() => flash("Code copied."), () => flash(`The code is ${state.join_code}`));
        return;
      }
      if (e.target.closest("#againBtn")) {
        api("/lone-bid/create", {}).then((res) => { window.location.href = `/lone-bid/game/${res.game_id}`; },
                                         (err) => flash(err.message, true));
        return;
      }
      if (e.target.closest("#leaveBtn")) {
        api(`/lone-bid/game/${gameId}/leave`, {}).then(() => { window.location.href = "/lone-bid"; }, (err) => flash(err.message, true));
      }
    });
    document.addEventListener("change", (e) => { if (e.target.id === "botLevel") botLevel = e.target.value; });

    poll();
    setInterval(poll, 1000);
  }

  (async () => {
    try {
      const me = await api("/me");
      const el = $("#userName");
      if (el) el.textContent = me.username || "user";
    } catch { /* the rules page reads fine signed out */ }
    if (window.GAME_ID) initTable(window.GAME_ID);
    else initRulesPage();
  })();
})();
