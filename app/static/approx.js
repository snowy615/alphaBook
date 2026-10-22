// Approx: the landing page (host / join) and the game page (host or team).
(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(url, body) {
    const r = await fetch(url, {
      method: body === undefined ? "GET" : "POST", credentials: "include", cache: "no-store",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(r.status === 401 ? "Log in first." : (d.detail || `HTTP ${r.status}`));
      err.status = r.status;
      throw err;
    }
    return d;
  }

  // 1234567 -> "1,234,567"; tiny or huge -> "8.07 × 10^67".
  function fmt(x) {
    if (x == null || !isFinite(x)) return "—";
    const a = Math.abs(x);
    if (a !== 0 && (a >= 1e9 || a < 1e-3)) {
      const [m, e] = x.toExponential(3).split("e");
      return `${parseFloat(m)} × 10<sup>${parseInt(e, 10)}</sup>`;
    }
    // No thousands separator below 10,000, so a year reads 2007, not 2,007.
    return x.toLocaleString(undefined, { maximumSignificantDigits: 8, useGrouping: a >= 10000 });
  }
  const clock = (s) => s == null ? "—" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

  // ── Landing: host a game or join one ───────────────────────────────────────
  async function landing() {
    try {
      const me = await api("/me");
      $("#userName").textContent = me.username || "";
      $("#userBox").classList.remove("hidden");
    } catch (e) {
      $("#loginBox").classList.remove("hidden");
    }

    $("#joinForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("#joinBtn"), msg = $("#joinMsg");
      btn.disabled = true; msg.textContent = "";
      try {
        const d = await api("/approx/join", { join_code: $("#joinCode").value.trim(), team_name: $("#teamName").value.trim() });
        location.href = `/approx/game/${d.game_id}`;
      } catch (err) {
        msg.textContent = err.message; msg.className = "ax-msg is-bad";
        btn.disabled = false;
      }
    });

    // The host's question list: bank questions (answer kept on the server)
    // and their own, each with the answer they know.
    let rows = [];
    let bank = [];
    let attemptsTouched = false;
    const minutesEl = $("#minutes"), attemptsEl = $("#attempts");
    attemptsEl.addEventListener("input", () => { attemptsTouched = true; });

    function draw() {
      const box = $("#hostRows");
      if (!rows.length) {
        box.innerHTML = '<p class="ax-muted ax-empty">No questions yet. Add some from the bank or write your own.</p>';
      } else {
        box.innerHTML = rows.map((r, i) => `
          <div class="ax-row" data-i="${i}">
            <span class="ax-num-badge">${i + 1}</span>
            ${r.bank_id
              ? `<div class="ax-row-text">${esc(r.text)}<div class="ax-muted ax-from-bank">From the bank: answer filled in for you</div></div>`
              : `<div class="ax-row-fields">
                   <input class="ax-input ax-q" placeholder="Question, e.g. Number of piano keys in Oxford" value="${esc(r.text)}" maxlength="300">
                   <input class="ax-input ax-a" placeholder="Answer, e.g. 1500 or 1.5e3" value="${esc(r.answer)}">
                 </div>`}
            <button type="button" class="btn ghost ax-remove" title="Remove">✕</button>
          </div>`).join("");
      }
      box.querySelectorAll(".ax-row").forEach((el) => {
        const i = +el.dataset.i;
        el.querySelector(".ax-remove").addEventListener("click", () => { rows.splice(i, 1); draw(); drawBank(); });
        const q = el.querySelector(".ax-q"), a = el.querySelector(".ax-a");
        if (q) q.addEventListener("input", () => { rows[i].text = q.value; });
        if (a) a.addEventListener("input", () => { rows[i].answer = a.value; });
      });
      if (!attemptsTouched) attemptsEl.value = rows.length ? rows.length + 5 : "";
      $("#attemptsHint").textContent = rows.length ? `${rows.length} question${rows.length === 1 ? "" : "s"}` : "";
    }

    function drawBank() {
      const q = ($("#bankSearch").value || "").toLowerCase();
      const taken = new Set(rows.map((r) => r.bank_id).filter(Boolean));
      $("#bankList").innerHTML = bank.filter((b) => !q || b.text.toLowerCase().includes(q)).map((b) => `
        <div class="ax-bank-item">
          <span>${esc(b.text)}</span>
          <button type="button" class="btn ${taken.has(b.id) ? "ghost" : ""}" data-id="${esc(b.id)}" ${taken.has(b.id) ? "disabled" : ""}>
            ${taken.has(b.id) ? "Added" : "Add"}</button>
        </div>`).join("") || '<p class="ax-muted">Nothing matches.</p>';
      $("#bankList").querySelectorAll("button[data-id]").forEach((btn) => btn.addEventListener("click", () => {
        const b = bank.find((x) => x.id === btn.dataset.id);
        rows.push({ bank_id: b.id, text: b.text }); draw(); drawBank();
      }));
    }

    async function loadBank() {
      if (bank.length) return;
      bank = (await api("/approx/bank")).questions;
    }

    $("#addOwn").addEventListener("click", () => {
      rows.push({ text: "", answer: "" }); draw();
      const last = $("#hostRows").querySelectorAll(".ax-q"); if (last.length) last[last.length - 1].focus();
    });
    $("#openBank").addEventListener("click", async () => {
      try { await loadBank(); } catch (err) { $("#hostMsg").textContent = err.message; return; }
      $("#bank").classList.toggle("hidden"); drawBank();
    });
    $("#bankSearch").addEventListener("input", drawBank);
    $("#random13").addEventListener("click", async () => {
      try { await loadBank(); } catch (err) { $("#hostMsg").textContent = err.message; return; }
      if (rows.length && !confirm("Replace your current questions with 13 random ones from the bank?")) return;
      const pool = bank.slice();
      for (let i = pool.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [pool[i], pool[j]] = [pool[j], pool[i]]; }
      rows = pool.slice(0, 13).map((b) => ({ bank_id: b.id, text: b.text }));
      draw(); drawBank();
    });

    $("#startHosting").addEventListener("click", async () => {
      const btn = $("#startHosting"), msg = $("#hostMsg");
      msg.textContent = ""; msg.className = "ax-msg";
      if (!rows.length) { msg.textContent = "Add at least one question."; msg.className = "ax-msg is-bad"; return; }
      btn.disabled = true;
      try {
        const d = await api("/approx/create", {
          questions: rows.map((r) => r.bank_id ? { bank_id: r.bank_id } : { text: r.text, answer: String(r.answer || "") }),
          minutes: +minutesEl.value || 30,
          attempts: attemptsEl.value ? +attemptsEl.value : null,
        });
        location.href = `/approx/game/${d.game_id}`;
      } catch (err) {
        msg.textContent = err.message; msg.className = "ax-msg is-bad";
        btn.disabled = false;
      }
    });
    draw();
  }

  // ── The game: host view or team view ───────────────────────────────────────
  function game(gameId) {
    let st = null;            // last state from the server
    let drawn = "";           // which screen is drawn: role + status
    let secondsLeft = null, tickAt = 0;
    const main = $("#axMain"), bar = $("#axBar");
    const flash = (text, bad) => {
      const m = $("#axMsg"); m.textContent = text; m.className = "ax-msg " + (bad ? "is-bad" : "is-good");
      clearTimeout(flash.t); flash.t = setTimeout(() => { m.textContent = ""; }, 4000);
    };

    async function poll() {
      try {
        st = await api(`/approx/game/${gameId}/state`);
        secondsLeft = st.seconds_left; tickAt = Date.now();
        render();
      } catch (err) {
        if (err.status === 403 || err.status === 404) {
          main.innerHTML = `<div class="card ax-panel"><p>${esc(err.message)}</p><a class="btn primary" href="/approx">Go to Approx</a></div>`;
          bar.classList.add("hidden");
          return;                       // stop polling
        }
      }
      setTimeout(poll, st && st.status === "finished" ? 30000 : (st && st.is_host ? 2000 : 3000));
    }

    setInterval(() => {
      const el = $("#axClock");
      if (!el || secondsLeft == null) return;
      const left = Math.max(0, secondsLeft - Math.floor((Date.now() - tickAt) / 1000));
      el.textContent = clock(left);
      el.classList.toggle("is-low", left <= 60);
    }, 250);

    function render() {
      const key = `${st.is_host ? "host" : "team"}:${st.status}`;
      if (key !== drawn) { drawn = key; (st.is_host ? drawHost : drawTeam)(); }
      (st.is_host ? updateHost : updateTeam)();
    }

    // Host ───────────────────────────────────────────────────────────────────
    function drawHost() {
      const qs = (st.questions || []).map((q, i) => `
        <li><span class="ax-num-badge">${i + 1}</span><span class="ax-q-text">${esc(q.text)}</span>
            <span class="ax-answer">${fmt(q.answer)}</span></li>`).join("");
      if (st.status === "lobby") {
        main.innerHTML = `
          <div class="card ax-panel ax-center">
            <div class="ax-muted">Join at <strong>alphabook.uk/approx</strong> with code</div>
            <div class="ax-bigcode">${esc(st.join_code)}</div>
            <div id="axTeams" class="ax-teams"></div>
            <button class="btn primary ax-start" id="axStart">Start the game</button>
            <p class="ax-muted">${st.n_questions} questions · ${Math.round(st.duration_s / 60)} minutes · ${st.attempts} submissions per team</p>
          </div>
          <details class="card ax-panel"><summary>Questions and answers (only you can see these)</summary><ol class="ax-qlist">${qs}</ol></details>`;
        $("#axStart").addEventListener("click", async (e) => {
          e.target.disabled = true;
          try { await api(`/approx/game/${gameId}/start`, {}); poll(); } catch (err) { flash(err.message, true); e.target.disabled = false; }
        });
        return;
      }
      main.innerHTML = `
        <div class="card ax-panel">
          <div class="ax-lb-head"><h3>${st.status === "finished" ? "Final leaderboard" : "Leaderboard"}</h3>
            ${st.status === "playing" ? '<button class="btn" id="axEnd">End the game now</button>' : ""}</div>
          <div id="axBoard"></div>
        </div>
        <details class="card ax-panel" ${st.status === "finished" ? "open" : ""}><summary>Questions and answers</summary><ol class="ax-qlist">${qs}</ol></details>`;
      const end = $("#axEnd");
      if (end) end.addEventListener("click", async () => {
        if (!confirm("End the game now for everyone?")) return;
        try { await api(`/approx/game/${gameId}/end`, {}); poll(); } catch (err) { flash(err.message, true); }
      });
    }

    function boardHtml(rows, mine) {
      if (!rows || !rows.length) return '<p class="ax-muted">No teams yet.</p>';
      return `<table class="ax-board"><thead><tr><th>#</th><th>Team</th><th>Score</th><th>Good</th><th>Submissions</th><th></th></tr></thead><tbody>
        ${rows.map((r) => `<tr class="${r.name === mine ? "is-mine" : ""}">
          <td>${r.rank}</td><td>${esc(r.name)}</td><td class="ax-score-cell">${esc(r.score_label)}</td>
          <td>${r.good}/${st.n_questions}</td><td>${r.attempts_used}/${st.attempts}</td>
          <td>${r.finished ? '<span class="ax-tag">done</span>' : ""}</td></tr>`).join("")}</tbody></table>`;
    }

    function updateHost() {
      const teams = st.teams_joined || [];
      if (st.status === "lobby") {
        $("#axTeams").innerHTML = teams.length
          ? `<div class="ax-muted">${teams.length} team${teams.length === 1 ? "" : "s"} joined</div>` + teams.map((t) => `<span class="ax-chip">${esc(t)}</span>`).join("")
          : '<div class="ax-muted">Waiting for teams to join…</div>';
        $("#axStart").disabled = !teams.length;
        bar.classList.add("hidden");
        return;
      }
      const lb = st.leaderboard || [];
      const done = lb.filter((r) => r.finished).length;
      bar.classList.remove("hidden");
      bar.innerHTML = st.status === "playing"
        ? `<div class="ax-stat"><span>Time left</span><strong id="axClock">${clock(secondsLeft)}</strong></div>
           <div class="ax-stat"><span>Teams</span><strong>${lb.length}</strong></div>
           <div class="ax-stat"><span>Finished</span><strong>${done}/${lb.length}</strong></div>
           <div class="ax-stat"><span>Code</span><strong>${esc(st.join_code)}</strong></div>`
        : `<div class="ax-stat ax-wide"><span>Game over</span><strong>${endWords(st.end_reason)}</strong></div>`;
      $("#axBoard").innerHTML = boardHtml(lb);
    }

    const endWords = (r) => r === "time" ? "Time's up" : r === "all_finished" ? "Every team finished" : r === "host" ? "Ended by the host" : "Finished";

    // Team ───────────────────────────────────────────────────────────────────
    function drawTeam() {
      if (st.status === "lobby") {
        main.innerHTML = `<div class="card ax-panel ax-center">
          <h3>You're in as ${esc(st.team_name)}</h3>
          <p class="ax-muted">Waiting for ${esc(st.host_name || "the host")} to start. The questions appear here when it does.</p>
          <div id="axTeams" class="ax-teams"></div></div>`;
        return;
      }
      const playing = st.status === "playing";
      main.innerHTML = `
        ${st.status === "finished" ? '<div class="card ax-panel"><h3>Final leaderboard</h3><div id="axBoard"></div></div>' : ""}
        <div class="ax-qs">${(st.questions || []).map((q, i) => `
          <div class="card ax-qcard" data-q="${i}">
            <div class="ax-qhead"><span class="ax-num-badge">${i + 1}</span><span class="ax-q-text">${esc(q.text)}</span></div>
            <div class="ax-current" id="axCur${i}"></div>
            ${playing ? `<form class="ax-submit" data-q="${i}">
              <input class="ax-input ax-lo" placeholder="Low" inputmode="decimal" autocomplete="off" required>
              <span class="ax-to">to</span>
              <input class="ax-input ax-hi" placeholder="High" inputmode="decimal" autocomplete="off" required>
              <button class="btn primary">Submit</button>
            </form>` : `<div class="ax-reveal">Answer: <strong>${fmt(q.answer)}</strong></div>`}
          </div>`).join("")}</div>
        ${playing ? '<div class="ax-done"><button class="btn" id="axFinish">We\'re done: lock in our answers</button></div>' : ""}`;
      main.querySelectorAll(".ax-submit").forEach((form) => form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const btn = form.querySelector("button");
        btn.disabled = true;
        try {
          const d = await api(`/approx/game/${gameId}/submit`, {
            question: +form.dataset.q, low: form.querySelector(".ax-lo").value, high: form.querySelector(".ax-hi").value,
          });
          st.standing = d.standing;
          if (d.status !== st.status) { poll(); } else { updateTeam(); }
          const pq = d.standing.per_question[+form.dataset.q];
          flash(pq && pq.good ? `Question ${+form.dataset.q + 1}: good interval ✓` : `Question ${+form.dataset.q + 1}: not in that interval ✗`, !(pq && pq.good));
          form.reset();
        } catch (err) { flash(err.message, true); }
        btn.disabled = false;
      }));
      const fin = $("#axFinish");
      if (fin) fin.addEventListener("click", async () => {
        if (!confirm("Lock in your answers? You won't be able to submit any more.")) return;
        try { await api(`/approx/game/${gameId}/finish`, {}); poll(); } catch (err) { flash(err.message, true); }
      });
    }

    function updateTeam() {
      if (st.status === "lobby") {
        const teams = st.teams_joined || [];
        $("#axTeams").innerHTML = teams.map((t) => `<span class="ax-chip">${esc(t)}</span>`).join("");
        bar.classList.add("hidden");
        return;
      }
      const s = st.standing || {};
      bar.classList.remove("hidden");
      bar.innerHTML = `
        <div class="ax-stat ax-wide"><span>Score (lowest wins)</span><strong>${esc(s.score_label)}</strong></div>
        <div class="ax-stat"><span>Good</span><strong>${s.good}/${st.n_questions}</strong></div>
        <div class="ax-stat"><span>Submissions left</span><strong>${s.attempts_left}</strong></div>
        ${st.status === "playing" ? `<div class="ax-stat"><span>Time left</span><strong id="axClock">${clock(secondsLeft)}</strong></div>`
          : `<div class="ax-stat"><span>Game over</span><strong>${endWords(st.end_reason)}</strong></div>`}`;
      (s.per_question || []).forEach((pq, i) => {
        const el = $(`#axCur${i}`);
        if (!el) return;
        el.innerHTML = pq
          ? `<span class="ax-tag ${pq.good ? "is-good" : "is-bad"}">${pq.good ? "✓ good" : "✗ missed"}</span>
             Your interval: <strong>${fmt(pq.low)}</strong> to <strong>${fmt(pq.high)}</strong>${pq.good ? ` <span class="ax-muted">(+${pq.points})</span>` : ""}`
          : '<span class="ax-muted">No interval yet</span>';
      });
      const playing = st.status === "playing";
      const locked = playing && (s.finished || s.attempts_left <= 0);
      main.querySelectorAll(".ax-submit button, .ax-submit input").forEach((el) => { el.disabled = locked; });
      const fin = $("#axFinish");
      if (fin) { fin.disabled = !!s.finished; if (s.finished) fin.textContent = "Answers locked in: waiting for the other teams"; }
      const board = $("#axBoard");
      if (board) board.innerHTML = boardHtml(st.leaderboard, st.team_name);
    }

    poll();
  }

  window.Approx = { landing, game };
})();
