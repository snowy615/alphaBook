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
          title="${gone ? `${c} played` : locked ? `${c} locked this round` : `${c} in hand`}">${c}</span>`;
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
      return rows.map((p) => `
        <tr class="${p.is_me ? "is-me" : ""}${p.status !== "active" ? " is-off" : ""}${state.result && state.result.winner_id === p.user_id ? " is-winner" : ""}">
          <td class="lbd-name">${name(p)} ${badge(p)}<div class="lbd-hand-inline">${handStrip(p)}</div></td>
          <td class="lbd-num">${p.status === "active" ? p.score : "—"}</td>
          <td class="lbd-num">${p.tokens}</td>
          <td class="lbd-pair">${pairCell(p)}</td>
          <td class="lbd-hand-col">${handStrip(p)}</td>
        </tr>`).join("");
    }

    function header() {
      const phaseLabel = { reveal: "Reveal two", play: "Play one", result: "Result" }[state.phase] || "";
      const cp = state.final_stage
        ? `Final: rounds 13–${state.rounds}. Top scorer wins the prize, last is eliminated.`
        : `Checkpoint after round ${state.next_checkpoint}: the top scorer survives.`;
      return `<div class="lbd-head card">
        <div><div class="lbd-round">Round ${state.round} <span>of ${state.rounds}</span></div>
          <div class="msp-muted">Set ${state.set} of 3 · ${esc(cp)}</div></div>
        <div class="lbd-clock"><div class="lbd-phase">${phaseLabel}</div>
          <div class="lbd-secs">${state.seconds_left}<span>s</span></div></div>
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
      return `<div class="lbd-result">${head} <span class="msp-muted">${extra.join(" ")}</span>${banner}</div>`;
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
          <div class="lbd-prompt">Pick two cards to show the table. You'll play one of them.</div>
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
        return `<div class="lbd-action">
          <div class="lbd-prompt">Everyone's pairs are showing below. Play one of yours: the other is locked next round.</div>
          <div class="lbd-hand">${(my.revealed || []).map((c) => `<button type="button" class="lbd-card lbd-card-play" data-play="${c}">${c}</button>`).join("")}</div>
        </div>`;
      }
      return "";
    }

    function lobby() {
      const empty = state.seats - state.players.length;
      return `<div class="lbd-lobby">
        <div class="card msp-panel"><div class="head"><h3>Table</h3><span class="msp-muted">${state.players.length} of ${state.seats} seated</span></div>
          <div class="msp-panel-body">
            <div class="lbd-code-row"><span class="msp-muted">Join code</span><strong class="lbd-code">${esc(state.join_code)}</strong></div>
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
      </div>`;
    }

    function finished() {
      const rows = (state.results || []).map((r, i) => `
        <tr class="${r.user_id === state.me ? "is-me" : ""}">
          <td>${i + 1}</td><td>${esc(r.username)}${r.user_id === state.me ? ' <span class="lbd-you">you</span>' : ""}</td>
          <td><span class="lbd-badge ${(STATUS[r.status] || [""])[0]}">${esc(r.label)}${r.status === "survived" ? ` · round ${r.out_round}` : ""}</span></td>
          <td class="lbd-num">${r.rounds_won}</td><td class="lbd-num">${r.tokens}</td></tr>`).join("");
      return `<div class="card msp-panel"><div class="head"><h3>Game over</h3></div>
        <div class="msp-panel-body"><div class="lbd-scroll"><table class="msp-table lbd-final">
          <thead><tr><th></th><th>Player</th><th>Outcome</th><th>Rounds won</th><th>Tokens</th></tr></thead>
          <tbody>${rows}</tbody></table></div>
          <div id="fbkBox" style="margin-top:14px;"></div>
          <div style="margin-top:14px;"><a class="btn primary" href="/lone-bid">New table</a></div></div></div>`;
    }

    function sidePanel() {
      const hist = (state.history || []).slice().reverse().map((h) =>
        `<li><span class="msp-muted">R${h.round}</span> ${h.winner ? `${esc(h.winner)} · ${h.value}` : '<span class="msp-muted">no winner</span>'}${h.survivor
          ? `<div class="lbd-hist-cp">Checkpoint: ${esc(h.survivor)} survives</div>` : ""}</li>`).join("");
      return `<div class="card msp-panel"><div class="head"><h3>Rounds</h3></div>
        <div class="msp-panel-body"><ul class="lbd-hist">${hist || '<li class="msp-muted">No rounds yet.</li>'}</ul></div></div>`;
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
      put("lbdMain", `${resultPanel()}${actionPanel()}
        <div class="card lbd-scroll"><table class="msp-table lbd-table">
          <thead><tr><th>Player</th><th>Pts</th><th>Tok</th><th>${state.phase === "reveal" ? "Pair" : "Pair · played"}</th><th class="lbd-hand-col">Hand</th></tr></thead>
          <tbody>${tableRows()}</tbody></table>
          <p class="msp-muted lbd-key">Dim cards are gone for this set; the outlined one is locked for this round.</p></div>`);
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
