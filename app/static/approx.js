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

  // The same reading of a number as the server's parse_quantity: plain,
  // 1,234, 1.5e6, 3x10^6 (x, ×, *, ·), 10^7. null if it isn't one.
  function parseQ(raw) {
    let t = String(raw || "").trim().toLowerCase().replace(/[,_\s]/g, "").replace(/[×·*]/g, "x");
    const m = /^(?:(\d+(?:\.\d*)?|\.\d+)x)?10\^([+-]?\d+)$/.exec(t);
    const v = m ? parseFloat(m[1] || "1") * Math.pow(10, parseInt(m[2], 10)) : (t === "" ? NaN : Number(t));
    return isFinite(v) && v > 0 ? v : null;
  }
  // (10 + sum of points over good intervals) * 2^(questions - good), as the server scores.
  const scoreOf = (n, points) => (10 + points.reduce((a, b) => a + b, 0)) * Math.pow(2, n - points.length);
  const WORDS = [[1e17, "∞ squared"], [1e15, "∞ + 1"], [1e13, "∞?"], [1e11, "almost ∞"], [1e9, "a TON"], [1e7, "a lot"]];
  const scoreLabel = (x) => { for (const [t, w] of WORDS) if (x >= t) return w; return Math.round(x).toLocaleString(); };

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
      const fair = st.is_host;          // only the host sees each team's fair-play record
      return `<table class="ax-board"><thead><tr><th>#</th><th>Team</th><th>Score</th><th>Good</th><th>Submissions</th><th></th>
        ${fair ? "<th>Fair play</th>" : ""}</tr></thead><tbody>
        ${rows.map((r) => `<tr class="${r.name === mine ? "is-mine" : ""}">
          <td>${r.rank}</td><td>${esc(r.name)}</td><td class="ax-score-cell">${esc(r.score_label)}</td>
          <td>${r.good}/${st.n_questions}</td><td>${r.attempts_used}/${st.attempts}</td>
          <td>${r.finished ? '<span class="ax-tag">done</span>' : ""}</td>
          ${fair ? `<td>${(r.flags_text || []).length
            ? r.flags_text.map((t) => `<span class="ax-tag is-warn">${esc(t)}</span>`).join(" ")
            : '<span class="ax-muted">clean</span>'}</td>` : ""}</tr>`).join("")}</tbody></table>`;
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
            </form>
            <div class="ax-preview" id="axPrev${i}"></div>` : `<div class="ax-reveal">Answer: <strong>${fmt(q.answer)}</strong></div>`}
          </div>`).join("")}</div>
        ${playing ? '<div class="ax-done"><button class="btn" id="axFinish">We\'re done: lock in our answers</button></div>' : ""}`;
      main.querySelectorAll(".ax-submit").forEach((form) => {
        const i = +form.dataset.q;
        form.querySelectorAll("input").forEach((inp) => inp.addEventListener("input", () => preview(i)));
      });
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
          preview(+form.dataset.q);
        } catch (err) { flash(err.message, true); }
        btn.disabled = false;
      }));
      // Click twice to lock in, rather than a browser pop-up: a pop-up can take
      // the page out of full screen, which would wrongly count against them.
      const fin = $("#axFinish");
      if (fin) fin.addEventListener("click", async () => {
        if (!fin.dataset.sure) {
          fin.dataset.sure = "1";
          fin.textContent = "Sure? Click again to lock in (no more submissions after this)";
          setTimeout(() => { if (fin.isConnected && !fin.disabled) { delete fin.dataset.sure; fin.textContent = "We're done: lock in our answers"; } }, 4000);
          return;
        }
        try { await api(`/approx/game/${gameId}/finish`, {}); poll(); } catch (err) { flash(err.message, true); }
      });
    }

    // What the score would be if this interval turns out good, and if not,
    // before it's submitted. Replaces whatever interval the question has now.
    function preview(i) {
      const el = $(`#axPrev${i}`), form = main.querySelector(`.ax-submit[data-q="${i}"]`);
      if (!el || !form || !st.standing) return;
      const loRaw = form.querySelector(".ax-lo").value, hiRaw = form.querySelector(".ax-hi").value;
      if (!loRaw.trim() && !hiRaw.trim()) { el.innerHTML = ""; return; }
      const lo = parseQ(loRaw), hi = parseQ(hiRaw);
      if (lo == null || hi == null) { el.innerHTML = '<span class="ax-muted">Enter two positive numbers (e.g. 1500, 1.5e3, 3x10^6)</span>'; return; }
      if (lo > hi) { el.innerHTML = '<span class="ax-bad-text">Low is above high</span>'; return; }
      const others = (st.standing.per_question || []).filter((pq, j) => j !== i && pq && pq.good).map((pq) => pq.points);
      const n = st.n_questions, pts = Math.floor(hi / lo * (1 + 1e-12));
      el.innerHTML = `If it's in: <strong class="ax-good-text">${scoreLabel(scoreOf(n, others.concat([pts])))}</strong>
        <span class="ax-muted">(+${pts})</span> · if not: <strong class="ax-bad-text">${scoreLabel(scoreOf(n, others))}</strong>`;
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
      (st.questions || []).forEach((_, i) => preview(i));
      guard(playing && !s.finished);
    }

    // ── Fair play, while a team is playing ────────────────────────────────
    // Full screen is required (the questions are covered until it's on);
    // copy, cut, paste, the copy/paste shortcuts and right-click are blocked;
    // any key that isn't part of typing a number counts as irregular; leaving
    // full screen or switching tab is noted. All of it is counted and sent
    // to the server in batches for the host to see.
    const ALLOWED_KEYS = new Set(["Backspace", "Delete", "Tab", "Enter", "ArrowLeft", "ArrowRight", "ArrowUp",
      "ArrowDown", "Home", "End", "Shift", "CapsLock", "Escape"]);
    const NUMBER_CHARS = /^[0-9.,eExX*^+\-×· ]$/;
    let guarding = false, pending = {}, sendTimer = null, fsWaived = false;
    const note = (kind) => {
      pending[kind] = (pending[kind] || 0) + 1;
      if (!sendTimer) sendTimer = setTimeout(sendFlags, 1500);
    };
    async function sendFlags() {
      sendTimer = null;
      const counts = pending; pending = {};
      if (!Object.keys(counts).length) return;
      try { await api(`/approx/game/${gameId}/flags`, { counts }); }
      catch (e) { for (const [k, v] of Object.entries(counts)) pending[k] = (pending[k] || 0) + v; }
    }
    const fsSupported = () => !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
    const inFullscreen = () => !!(document.fullscreenElement || document.webkitFullscreenElement);
    function cover() {
      let el = $("#axCover");
      const need = guarding && fsSupported() && !inFullscreen() && !fsWaived;
      if (!need) { if (el) el.remove(); return; }
      if (el) return;
      el = document.createElement("div");
      el.id = "axCover";
      el.className = "ax-cover";
      el.innerHTML = `<div class="ax-cover-box"><h3>Approx runs in full screen</h3>
        <p class="ax-muted">The questions stay hidden until it's on. Leaving full screen during the game is shown to the host.</p>
        <button class="btn primary" id="axGoFull">Enter full screen</button></div>`;
      document.body.appendChild(el);
      $("#axGoFull").addEventListener("click", () => {
        const root = document.documentElement;
        try { Promise.resolve((root.requestFullscreen || root.webkitRequestFullscreen).call(root)).catch(() => {}); } catch (e) { /* below */ }
        // Some browsers (embedded ones, some phones) won't go full screen at
        // all. Rather than lock the team out, let them play on; the host sees it.
        setTimeout(() => {
          if (inFullscreen() || !$("#axCover")) return;
          const box = $("#axCover .ax-cover-box");
          if (!box || $("#axPlayAnyway")) return;
          box.insertAdjacentHTML("beforeend", `<p class="ax-bad-text" style="margin-top:14px;">This browser didn't switch to full screen.</p>
            <button class="btn" id="axPlayAnyway">Play anyway (the host will see this)</button>`);
          $("#axPlayAnyway").addEventListener("click", () => { fsWaived = true; note("no_fullscreen"); cover(); });
        }, 1000);
      });
    }
    const block = (kind) => (e) => { if (!guarding) return; e.preventDefault(); note(kind); flash("That's turned off during the game.", true); };
    document.addEventListener("copy", block("copy"));
    document.addEventListener("cut", block("copy"));
    document.addEventListener("paste", block("paste"));
    document.addEventListener("contextmenu", block("right_click"));
    document.addEventListener("keydown", (e) => {
      if (!guarding) return;
      const k = e.key || "";
      // Phone keyboards report most keys as "Unidentified"; what they type is
      // checked as it goes in (beforeinput, below) instead.
      if (k === "Unidentified" || k === "Process" || k === "Dead") return;
      if ((e.ctrlKey || e.metaKey) && ["c", "v", "x"].includes(k.toLowerCase())) {
        e.preventDefault(); note("shortcut"); flash("Copy and paste are turned off during the game.", true); return;
      }
      // Other shortcuts (select all, undo are fine while typing a number).
      if ((e.ctrlKey || e.metaKey || e.altKey) && k.length === 1 && !["a", "z", "y"].includes(k.toLowerCase())) {
        note("odd_keys"); return;
      }
      if (k.length === 1 && !e.ctrlKey && !e.metaKey && !NUMBER_CHARS.test(k)) { e.preventDefault(); note("odd_keys"); }
    }, true);
    // Whatever is about to go into a box, however it was typed (any keyboard,
    // autofill, drag and drop): anything that isn't part of a number is
    // refused and counted.
    const NOT_NUMBER = /[^0-9.,eExX*^+\-×· ]/;
    document.addEventListener("beforeinput", (e) => {
      if (!guarding || !e.data || !NOT_NUMBER.test(e.data)) return;
      e.preventDefault();
      note(/paste|drop/i.test(e.inputType || "") ? "paste" : "odd_keys");
    }, true);
    const fsChange = () => { if (guarding && !inFullscreen()) note("fullscreen_exit"); cover(); };
    document.addEventListener("fullscreenchange", fsChange);
    document.addEventListener("webkitfullscreenchange", fsChange);
    // Leaving the page (another tab, another app) fires both "blur" and a
    // hidden visibilitychange: count each trip away once, not twice.
    let away = false;
    const leave = () => { if (guarding && !away) { away = true; note("left_page"); } };
    const back = () => { away = false; };
    document.addEventListener("visibilitychange", () => (document.visibilityState === "hidden" ? leave() : back()));
    window.addEventListener("blur", leave);
    window.addEventListener("focus", back);
    function guard(on) {
      guarding = on;
      document.body.classList.toggle("ax-guarded", on);
      cover();
      if (!on && Object.keys(pending).length) sendFlags();
      if (!on && inFullscreen() && document.exitFullscreen) document.exitFullscreen().catch(() => {});
    }

    poll();
  }

  window.Approx = { landing, game };
})();
