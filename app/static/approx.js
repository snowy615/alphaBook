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
  const ordinal = (n) => { const t = n % 100, u = n % 10;
    return n + (t >= 11 && t <= 13 ? "th" : u === 1 ? "st" : u === 2 ? "nd" : u === 3 ? "rd" : "th"); };

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
  // Two modes. Normal: intervals around answers the host knows, the Estimathon
  // score, lowest wins. Modified: normal questions alongside closest-number
  // ones whose answers can come from the game itself; teams are ranked on
  // each part and the places add up, highest wins.
  const MODES = {
    normal: { random: 13, lede: "Pick from the question bank or write your own with the answer. You run the game and " +
      "watch the leaderboard; you don't play." },
    modified: { random: 10, lede: "Normal questions (intervals, score shown as you go) alongside closest-number ones whose " +
      "answers come from the game itself. Random 10 gives five of each; add, remove or write your own." },
  };
  // Closest-number questions whose answer is made of the teams' own answers.
  const CROWD_KINDS = new Set(["mean", "median", "sum", "max_identical", "distinct", "digit_count", "lowest_score", "highest_score"]);

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

    // The host's question list, one per mode: bank questions (answer kept on
    // the server), the modified mode's own, and the host's.
    let mode = "normal";
    const rowsOf = { normal: [], modified: [] };
    const rows = () => rowsOf[mode];
    let bank = [], modBank = [], kinds = [], categories = [], modCategory = "";
    let attemptsTouched = false;
    const minutesEl = $("#minutes"), attemptsEl = $("#attempts");
    attemptsEl.addEventListener("input", () => { attemptsTouched = true; });

    const kindOptions = (sel) => kinds.map((k) =>
      `<option value="${esc(k.kind)}"${k.kind === sel ? " selected" : ""}>${esc(k.label)}</option>`).join("");
    const field = (cls, ph, val, label) =>
      `<input class="ax-input ${cls}" placeholder="${esc(ph)}" value="${esc(val || "")}" aria-label="${esc(label)}">`;

    function rowHtml(r, i) {
      if (r.bank_id && r.kind) {           // the modified mode's own bank
        return `<div class="ax-row-text">${esc(r.text)}
          <div class="ax-muted ax-row-meta">${esc(r.kind_label)}${r.kind === "fixed" ? "" : " · worked out by the game"}</div>
          ${r.kind === "fixed" ? field("ax-a ax-row-answer", "Answer (or set it during the game)", r.answer, `Answer to question ${i + 1}`) : ""}</div>`;
      }
      if (r.bank_id) {
        return `<div class="ax-row-text">${esc(r.text)}<div class="ax-muted ax-row-meta">${esc(r.category || "Question bank")} · answer filled in for you${mode === "modified" ? " · normal: intervals" : ""}</div></div>`;
      }
      if (mode === "normal") {
        return `<div class="ax-row-fields">
          <input class="ax-input ax-q" placeholder="Your question" value="${esc(r.text)}" maxlength="300" aria-label="Question ${i + 1}">
          ${field("ax-a", "Answer, e.g. 1.5e3", r.answer, `Answer to question ${i + 1}`)}</div>`;
      }
      const k = r.kind || "fixed";
      return `<div class="ax-row-fields ax-row-mod">
        <input class="ax-input ax-q" placeholder="Your question" value="${esc(r.text)}" maxlength="300" aria-label="Question ${i + 1}">
        <select class="ax-input ax-kind-select" aria-label="Where the answer to question ${i + 1} comes from">${kindOptions(k)}</select>
        <div class="ax-params">
          ${k === "interval" ? field("ax-a", "Answer (needed), e.g. 1.5e3", r.answer, "Answer") : ""}
          ${k === "fixed" ? field("ax-a", "Answer (or set it later)", r.answer, "Answer") : ""}
          ${k === "mean" ? field("ax-factor", "× factor, e.g. 2/3", r.factor, "Factor") : ""}
          ${k === "digit_count" ? field("ax-digit", "Digit, e.g. 1", r.digit, "Digit") : ""}
          ${k === "interval" ? "" : field("ax-min", k === "random" ? "From" : "Lowest answer allowed", r.min, "Lowest")}
          ${k === "interval" ? "" : field("ax-max", k === "random" ? "To" : "Highest answer allowed", r.max, "Highest")}
        </div></div>`;
    }

    function draw() {
      const box = $("#hostRows");
      const list = rows(), n = list.length;
      $("#qCount").textContent = `${n} question${n === 1 ? "" : "s"}`;
      $("#qCount").classList.toggle("is-set", n > 0);
      box.classList.toggle("ax-rows", n > 0);
      if (!n) {
        box.innerHTML = `<div class="ax-empty">No questions yet. Browse the bank, write your own, or take a random ${MODES[mode].random}.</div>`;
      } else {
        box.innerHTML = list.map((r, i) => `
          <div class="ax-row" data-i="${i}">
            <span class="ax-num-badge">${i + 1}</span>
            ${rowHtml(r, i)}
            <button type="button" class="btn ghost ax-remove" title="Remove" aria-label="Remove question ${i + 1}">✕</button>
          </div>`).join("");
      }
      box.querySelectorAll(".ax-row").forEach((el) => {
        const i = +el.dataset.i, r = list[i];
        el.querySelector(".ax-remove").addEventListener("click", () => { list.splice(i, 1); draw(); drawBank(); });
        const bind = (sel, key) => { const inp = el.querySelector(sel); if (inp) inp.addEventListener("input", () => { r[key] = inp.value; }); };
        bind(".ax-q", "text"); bind(".ax-a", "answer"); bind(".ax-factor", "factor"); bind(".ax-digit", "digit");
        bind(".ax-min", "min"); bind(".ax-max", "max");
        const kind = el.querySelector(".ax-kind-select");
        if (kind) kind.addEventListener("change", () => { r.kind = kind.value; draw(); });
      });
      if (!attemptsTouched) attemptsEl.value = n ? n + 5 : "";
      $("#attemptsHint").textContent = n ? "questions + 5" : "";
    }

    let category = "";
    function drawBank() {
      const cats = $("#bankCats");
      const all = mode === "modified" ? modBank.concat(bank) : bank;
      const catList = mode === "modified" ? [modCategory, ...categories] : categories;
      if (!catList.includes(category)) category = "";
      if (cats && cats.dataset.drawn !== mode && catList.length) {
        cats.dataset.drawn = mode;
        cats.innerHTML = ["", ...catList].map((c) =>
          `<button type="button" class="ax-cat${c === category ? " is-on" : ""}" data-cat="${esc(c)}">${esc(c || "All")}</button>`).join("");
        cats.querySelectorAll(".ax-cat").forEach((b) => b.addEventListener("click", () => {
          category = b.dataset.cat;
          cats.querySelectorAll(".ax-cat").forEach((x) => x.classList.toggle("is-on", x === b));
          drawBank();
        }));
      }
      const q = ($("#bankSearch").value || "").toLowerCase();
      const taken = new Set(rows().map((r) => r.bank_id).filter(Boolean));
      $("#bankList").innerHTML = all
        .filter((b) => (!category || b.category === category) && (!q || b.text.toLowerCase().includes(q)))
        .map((b) => `
        <div class="ax-bank-item">
          <span>${esc(b.text)}<span class="ax-bank-cat">${esc(b.kind ? `${b.category} · ${b.kind_label}` : b.category)}</span></span>
          <button type="button" class="btn ${taken.has(b.id) ? "ghost" : ""}" data-id="${esc(b.id)}" ${taken.has(b.id) ? "disabled" : ""}>
            ${taken.has(b.id) ? "Added" : "Add"}</button>
        </div>`).join("") || '<p class="ax-muted" style="padding:12px;">Nothing matches.</p>';
      $("#bankList").querySelectorAll("button[data-id]").forEach((btn) => btn.addEventListener("click", () => {
        const b = all.find((x) => x.id === btn.dataset.id);
        rows().push(b.kind ? { bank_id: b.id, text: b.text, kind: b.kind, kind_label: b.kind_label, answer: "" }
                           : { bank_id: b.id, text: b.text, category: b.category });
        draw(); drawBank();
      }));
    }

    async function loadBank() {
      if (bank.length) return;
      const d = await api("/approx/bank");
      bank = d.questions; categories = d.categories || [];
      modBank = d.modified || []; kinds = d.kinds || []; modCategory = d.modified_category || "";
    }

    // Normal or modified: the host's list, the rules shown, the settings.
    async function setMode(m) {
      // The rules switch whoever is looking; only the host's builder needs the
      // bank (and so a login) for the modified question kinds.
      if (m === "modified") {
        try { await loadBank(); } catch (err) { $("#hostMsg").textContent = err.message; }
      }
      mode = m;
      document.querySelectorAll(".ax-mode-btn").forEach((b) => {
        b.classList.toggle("is-on", b.dataset.mode === m);
        b.setAttribute("aria-pressed", b.dataset.mode === m ? "true" : "false");
      });
      $("#modeLede").textContent = MODES[m].lede;
      $("#rulesNormal").classList.toggle("hidden", m !== "normal");
      $("#rulesModified").classList.toggle("hidden", m !== "modified");
      $("#attemptsField").classList.toggle("hidden", m !== "normal");
      $("#random13").textContent = `Random ${MODES[m].random}`;
      draw(); drawBank();
    }
    document.querySelectorAll(".ax-mode-btn").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

    $("#addOwn").addEventListener("click", async () => {
      if (mode === "modified") { try { await loadBank(); } catch (err) { $("#hostMsg").textContent = err.message; return; } }
      rows().push(mode === "normal" ? { text: "", answer: "" } : { text: "", kind: "fixed", answer: "" }); draw();
      const last = $("#hostRows").querySelectorAll(".ax-q"); if (last.length) last[last.length - 1].focus();
    });
    $("#openBank").addEventListener("click", async () => {
      try { await loadBank(); } catch (err) { $("#hostMsg").textContent = err.message; return; }
      $("#bank").classList.toggle("hidden"); drawBank();
      $("#openBank").textContent = $("#bank").classList.contains("hidden") ? "Browse the question bank" : "Hide the question bank";
    });
    $("#bankSearch").addEventListener("input", drawBank);
    $("#random13").addEventListener("click", async () => {
      try { await loadBank(); } catch (err) { $("#hostMsg").textContent = err.message; return; }
      const count = MODES[mode].random;
      if (rows().length && !confirm(`Replace your current questions with ${count} random ones from the bank?`)) return;
      const shuffled = (list) => {
        const pool = list.slice();
        for (let i = pool.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [pool[i], pool[j]] = [pool[j], pool[i]]; }
        return pool;
      };
      // The modified mode: half normal bank questions, half answered by what the teams submit.
      const picked = mode === "modified"
        ? shuffled(bank).slice(0, count / 2).concat(shuffled(modBank.filter((b) => CROWD_KINDS.has(b.kind))).slice(0, count / 2))
        : shuffled(bank).slice(0, count);
      rowsOf[mode] = picked.map((b) => b.kind
        ? { bank_id: b.id, text: b.text, kind: b.kind, kind_label: b.kind_label, answer: "" }
        : { bank_id: b.id, text: b.text, category: b.category });
      draw(); drawBank();
    });

    const payload = (r) => {
      if (r.bank_id) return r.kind ? { bank_id: r.bank_id, answer: String(r.answer || "") } : { bank_id: r.bank_id };
      if (mode === "normal") return { text: r.text, answer: String(r.answer || "") };
      const out = { text: r.text, kind: r.kind || "fixed" };
      for (const k of ["answer", "min", "max", "factor", "digit"]) if (String(r[k] || "").trim()) out[k] = String(r[k]).trim();
      return out;
    };
    $("#startHosting").addEventListener("click", async () => {
      const btn = $("#startHosting"), msg = $("#hostMsg");
      msg.textContent = ""; msg.className = "ax-msg";
      if (!rows().length) { msg.textContent = "Add at least one question."; msg.className = "ax-msg is-bad"; return; }
      btn.disabled = true;
      try {
        const d = await api("/approx/create", {
          mode, questions: rows().map(payload),
          minutes: +minutesEl.value || 30,
          attempts: mode === "normal" && attemptsEl.value ? +attemptsEl.value : null,
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
          main.innerHTML = `<section class="ax-card ax-lobby ax-center"><p>${esc(err.message)}</p><a class="btn primary" href="/approx">Go to Approx</a></section>`;
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
      const meter = $("#axTimeMeter");
      if (meter && st && st.duration_s) {
        meter.firstElementChild.style.width = `${Math.max(0, Math.min(100, 100 * left / st.duration_s))}%`;
        meter.classList.toggle("is-low", left <= 60);
      }
    }, 250);

    function render() {
      const key = `${st.is_host ? "host" : "team"}:${st.status}`;
      if (key !== drawn) { drawn = key; (st.is_host ? drawHost : drawTeam)(); }
      (st.is_host ? updateHost : updateTeam)();
    }

    // Host ───────────────────────────────────────────────────────────────────
    const qList = () => (st.questions || []).map((q, i) => `
        <li><span class="ax-num-badge">${i + 1}</span><span class="ax-q-text">${esc(q.text)}</span>
            <span class="ax-answer">${fmt(q.answer)}</span>${q.note ? `<span class="ax-note">${esc(q.note)}</span>` : ""}</li>`).join("");

    // The modified mode: where each answer comes from, what it is so far, the
    // closest teams, and a box to set the host's own answers.
    const qListMod = () => (st.questions || []).map((q, i) => `
        <li><span class="ax-num-badge">${i + 1}</span>
          <span class="ax-q-text">${esc(q.text)} <span class="ax-kind">${esc(q.kind_label)}</span></span>
          <span class="ax-answer">${q.answer == null
            ? (q.kind === "fixed" ? '<span class="ax-bad-text">not set</span>' : '<span class="ax-muted">none yet</span>')
            : `${q.provisional ? '<span class="ax-muted">so far </span>' : ""}${fmt(q.answer)}`}</span>
          ${q.kind === "fixed" || q.kind === "interval" ? `<form class="ax-set" data-q="${i}">
            <input class="ax-input" placeholder="${q.answer == null ? "Set the answer" : "Change the answer"}" inputmode="decimal" autocomplete="off" required>
            <button class="btn">Set</button></form>` : ""}
          <span class="ax-note">${st.status === "lobby" ? esc(q.hint)
            : q.interval ? `${q.answered} with an interval · ${q.good} good`
            : `${q.answered} answered${(q.closest || []).length ? ` · closest: ${q.closest.map((c) => `${esc(c.name)} (${esc(c.shown)})`).join(", ")}` : ""}`}${q.note ? ` · ${esc(q.note)}` : ""}</span></li>`).join("");
    const isMod = () => st.mode === "modified";

    let qListDrawn = "";
    function refreshQList() {
      const box = $("#axQList");
      if (!box || !isMod()) return;
      const html = qListMod();
      // Leave it alone while the host is typing an answer into it.
      if (html === qListDrawn || box.contains(document.activeElement)) return;
      qListDrawn = html;
      box.innerHTML = html;
      box.querySelectorAll(".ax-set").forEach((form) => form.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
          await api(`/approx/game/${gameId}/answer`, { question: +form.dataset.q, answer: form.querySelector("input").value });
          flash(`Question ${+form.dataset.q + 1}: answer set`);
          form.querySelector("input").blur();
          poll();
        } catch (err) { flash(err.message, true); }
      }));
    }

    function drawHost() {
      qListDrawn = "";
      if (st.status === "lobby") {
        main.innerHTML = `
          <section class="ax-card ax-lobby ax-center">
            <div class="ax-card-label">Players join at alphabook.uk/approx with code</div>
            <div class="ax-bigcode">${esc(st.join_code)}</div>
            <div class="ax-lobby-meta">
              <div><strong>${st.n_questions}</strong><span>questions</span></div>
              <div><strong>${Math.round(st.duration_s / 60)} min</strong><span>time limit</span></div>
              ${isMod() ? `<div><strong>Modified</strong><span>${st.attempts ? `${st.attempts} intervals + 1 per closest-number` : "one answer per question"}</span></div>`
                : `<div><strong>${st.attempts}</strong><span>submissions each</span></div>`}
            </div>
            <div id="axTeams" class="ax-teams"></div>
            <button class="btn primary ax-start" id="axStart">Start the game</button>
          </section>
          <details class="ax-card" style="margin-top:14px;" ${isMod() ? "open" : ""}><summary>Questions and answers (only you can see these)</summary>
            <ol class="ax-qlist" id="axQList">${isMod() ? "" : qList()}</ol></details>`;
        $("#axStart").addEventListener("click", async (e) => {
          e.target.disabled = true;
          try { await api(`/approx/game/${gameId}/start`, {}); poll(); } catch (err) { flash(err.message, true); e.target.disabled = false; }
        });
        return;
      }
      main.innerHTML = `
        <section class="ax-card">
          <div class="ax-section-head"><h3>${st.status === "finished" ? "Final leaderboard" : "Leaderboard"}</h3>
            ${st.status === "playing" ? '<button class="btn" id="axEnd">End the game now</button>' : ""}</div>
          <div id="axBoard" class="ax-board-wrap"></div>
        </section>
        <details class="ax-card" style="margin-top:14px;" ${st.status === "finished" ? "open" : ""}><summary>Questions and answers${isMod() && st.status === "playing" ? " (live: keep it closed if your screen is on show)" : ""}</summary>
          <ol class="ax-qlist" id="axQList">${isMod() ? "" : qList()}</ol></details>`;
      const end = $("#axEnd");
      if (end) end.addEventListener("click", async () => {
        if (!confirm("End the game now for everyone?")) return;
        try { await api(`/approx/game/${gameId}/end`, {}); poll(); } catch (err) { flash(err.message, true); }
      });
    }

    function boardHtml(rows, mine) {
      if (isMod()) return boardHtmlMod(rows, mine);
      if (!rows || !rows.length) return '<p class="ax-muted">No teams yet.</p>';
      const fair = st.is_host;          // only the host sees each team's fair-play record
      return `<table class="ax-board"><thead><tr><th>Rank</th><th>Team</th><th>Score</th><th>Good</th><th>Used</th><th></th>
        ${fair ? "<th>Fair play</th>" : ""}</tr></thead><tbody>
        ${rows.map((r) => `<tr class="${r.name === mine ? "is-mine" : ""}">
          <td><span class="ax-rank${r.rank <= 3 ? ` is-${r.rank}` : ""}">${r.rank}</span></td>
          <td>${esc(r.name)}</td><td class="ax-score-cell">${esc(r.score_label)}</td>
          <td>${r.good}/${st.n_questions}</td><td>${r.attempts_used}/${st.attempts}</td>
          <td>${r.finished ? '<span class="ax-tag is-done">done</span>' : ""}</td>
          ${fair ? `<td>${(r.flags_text || []).length
            ? r.flags_text.map((t) => `<span class="ax-tag is-warn">${esc(t)}</span>`).join(" ")
            : '<span class="ax-muted">clean</span>'}</td>` : ""}</tr>`).join("")}</tbody></table>`;
    }

    const fairCell = (r) => `<td>${(r.flags_text || []).length
      ? r.flags_text.map((t) => `<span class="ax-tag is-warn">${esc(t)}</span>`).join(" ")
      : '<span class="ax-muted">clean</span>'}</td>`;
    // The modified board: with both kinds of question, the total of the two
    // places first, then each part; with one kind, just that part.
    function boardHtmlMod(rows, mine) {
      if (!rows || !rows.length) return '<p class="ax-muted">No teams yet.</p>';
      const fair = st.is_host;
      const live = st.status === "playing";
      const both = st.has_interval && st.has_crowd;
      const cols = [
        both && [`Total${live ? " so far" : ""}`, (r) => `<td class="ax-score-cell" title="${r.interval} for the normal part + ${r.crowd} for the closest-number part">${r.total}</td>`],
        st.has_interval && ["Normal score", (r) => `<td${both ? "" : ' class="ax-score-cell"'}>${esc(r.score_label)} <span class="ax-muted">(${r.good} good)</span></td>`],
        st.has_crowd && [`Closest pts${live && !both ? " so far" : ""}`, (r) => `<td${both ? "" : ' class="ax-score-cell"'}>${r.points} <span class="ax-muted">(closest on ${r.firsts})</span></td>`],
      ].filter(Boolean);
      return `<table class="ax-board"><thead><tr><th>Rank</th><th>Team</th>${cols.map(([h]) => `<th>${h}</th>`).join("")}
        <th>Answered</th><th></th>
        ${fair ? "<th>Fair play</th>" : ""}</tr></thead><tbody>
        ${rows.map((r) => `<tr class="${r.name === mine ? "is-mine" : ""}">
          <td><span class="ax-rank${r.rank <= 3 ? ` is-${r.rank}` : ""}">${r.rank}</span></td>
          <td>${esc(r.name)}</td>${cols.map(([, cell]) => cell(r)).join("")}
          <td>${r.answered}/${st.n_questions}</td>
          <td>${r.finished ? '<span class="ax-tag is-done">done</span>' : ""}</td>
          ${fair ? fairCell(r) : ""}</tr>`).join("")}</tbody></table>`;
    }

    function updateHost() {
      refreshQList();
      const teams = st.teams_joined || [];
      if (st.status === "lobby") {
        $("#axTeams").innerHTML = teams.length
          ? `<div class="ax-muted" style="margin-bottom:6px;">${teams.length} team${teams.length === 1 ? "" : "s"} in</div>` +
            teams.map((t) => `<span class="ax-chip">${esc(t)}</span>`).join("")
          : '<span class="ax-waiting">Waiting for teams to join</span>';
        $("#axStart").disabled = !teams.length;
        bar.classList.add("hidden");
        return;
      }
      const lb = st.leaderboard || [];
      const done = lb.filter((r) => r.finished).length;
      bar.classList.remove("hidden");
      bar.innerHTML = st.status === "playing"
        ? `<div class="ax-stat ax-hero-stat"><span>Time left</span><strong id="axClock">${clock(secondsLeft)}</strong>
             <div class="ax-meter" id="axTimeMeter"><i style="width:${timePct()}%"></i></div></div>
           <div class="ax-stat"><span>Teams</span><strong>${lb.length}</strong></div>
           <div class="ax-stat"><span>Finished</span><strong>${done}/${lb.length}</strong></div>
           <div class="ax-stat"><span>Code</span><strong>${esc(st.join_code)}</strong></div>`
        : `<div class="ax-stat ax-hero-stat" style="grid-column:1/-1;"><span>Game over</span><strong>${endWords(st.end_reason)}</strong></div>`;
      $("#axBoard").innerHTML = boardHtml(lb);
    }

    // Share of the time limit left, for the meter under the clock.
    function timePct() {
      if (secondsLeft == null || !st.duration_s) return 0;
      const left = Math.max(0, secondsLeft - Math.floor((Date.now() - tickAt) / 1000));
      return Math.max(0, Math.min(100, 100 * left / st.duration_s));
    }

    const endWords = (r) => r === "time" ? "Time's up" : r === "all_finished" ? "Every team finished" : r === "host" ? "Ended by the host" : "Finished";

    // Team ───────────────────────────────────────────────────────────────────
    function drawTeam() {
      if (st.status === "lobby") {
        main.innerHTML = `<section class="ax-card ax-lobby ax-center">
          <div class="ax-card-label">You're in as</div>
          <h3 style="font-size:28px;margin:6px 0 14px;">${esc(st.team_name)}</h3>
          <span class="ax-waiting">Waiting for ${esc(st.host_name || "the host")} to start</span>
          <div class="ax-lobby-meta">
            <div><strong>${st.n_questions}</strong><span>questions</span></div>
            <div><strong>${Math.round(st.duration_s / 60)} min</strong><span>time limit</span></div>
            ${isMod() ? `<div><strong>${st.attempts ? `${st.attempts} + 1` : "1"}</strong><span>${st.attempts ? "intervals + 1 per closest-number" : "answer per question"}</span></div>`
              : `<div><strong>${st.attempts}</strong><span>submissions</span></div>`}
          </div>
          <p class="ax-muted">${isMod()
            ? "Modified mode. Normal questions: intervals as usual, your score on them shown as you go (lowest wins). Closest-number questions: one number each, final once you lock it in; the closest team gets as many points as there are teams, the next one fewer, and equally close goes to whoever answered first. Their answers come from the game itself, so they're shown at the end. Teams are ranked on each part and the places add up: highest total wins."
            : "Only your last interval for a question counts."} It runs in full screen with copy and paste off.</p>
          <div id="axTeams" class="ax-teams"></div></section>`;
        return;
      }
      if (isMod()) { drawTeamMod(); return; }
      const playing = st.status === "playing";
      main.innerHTML = `
        ${st.status === "finished" ? '<section class="ax-card" style="margin-bottom:14px;"><h3>Final leaderboard</h3><div id="axBoard" class="ax-board-wrap"></div></section>' : ""}
        <div class="ax-qs">${(st.questions || []).map((q, i) => `
          <div class="ax-qcard" data-q="${i}" id="axCard${i}">
            <div class="ax-qhead"><span class="ax-num-badge">${i + 1}</span><span class="ax-q-text">${esc(q.text)}</span></div>
            <div class="ax-current" id="axCur${i}"></div>
            ${playing ? `<form class="ax-submit" data-q="${i}">
              <input class="ax-input ax-lo" placeholder="Low" inputmode="decimal" autocomplete="off" required>
              <span class="ax-to">to</span>
              <input class="ax-input ax-hi" placeholder="High" inputmode="decimal" autocomplete="off" required>
              <button class="btn primary">Submit</button>
            </form>
            <div class="ax-preview" id="axPrev${i}"></div>` : `<div class="ax-reveal">Answer: <strong>${fmt(q.answer)}</strong>
              ${q.note ? `<span class="ax-note">${esc(q.note)}</span>` : ""}</div>`}
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

    // The modified mode. Normal questions take intervals as in the normal game;
    // closest-number ones take one number, locked in with two clicks (a stray
    // Enter shouldn't spend the only answer).
    function drawTeamMod() {
      const playing = st.status === "playing";
      const intervalForm = (i) => `<form class="ax-submit ax-int" data-q="${i}" id="axForm${i}">
          <input class="ax-input ax-lo" placeholder="Low" inputmode="decimal" autocomplete="off" required>
          <span class="ax-to">to</span>
          <input class="ax-input ax-hi" placeholder="High" inputmode="decimal" autocomplete="off" required>
          <button class="btn primary">Submit</button>
        </form>
        <div class="ax-preview" id="axPrev${i}"></div>`;
      const numberForm = (i) => `<form class="ax-submit ax-num-form" data-q="${i}" id="axForm${i}">
          <input class="ax-input ax-val" placeholder="Your answer" inputmode="decimal" autocomplete="off" required aria-label="Your answer to question ${i + 1}">
          <button class="btn primary">Lock in</button>
        </form>`;
      main.innerHTML = `
        ${st.status === "finished" ? '<section class="ax-card" style="margin-bottom:14px;"><h3>Final leaderboard</h3><div id="axBoard" class="ax-board-wrap"></div></section>' : ""}
        <div class="ax-qs">${(st.questions || []).map((q, i) => `
          <div class="ax-qcard" data-q="${i}" id="axCard${i}">
            <div class="ax-qhead"><span class="ax-num-badge">${i + 1}</span><span class="ax-q-text">${esc(q.text)}</span></div>
            <div class="ax-hint">${q.interval ? "Normal: give an interval" : "Closest number wins"}${q.hint ? ` · ${esc(q.hint)}` : ""}</div>
            <div class="ax-current" id="axCur${i}"></div>
            ${playing ? (q.interval ? intervalForm(i) : numberForm(i))
              : `<div class="ax-reveal">Answer: <strong>${q.answer == null ? "to be set by the host" : fmt(q.answer)}</strong>
              ${q.note ? `<span class="ax-note">${esc(q.note)}</span>` : ""}</div>`}
          </div>`).join("")}</div>
        ${playing ? '<div class="ax-done"><button class="btn" id="axFinish">We\'re done: lock in our answers</button></div>' : ""}`;
      main.querySelectorAll(".ax-int").forEach((form) => {
        const i = +form.dataset.q;
        form.querySelectorAll("input").forEach((inp) => inp.addEventListener("input", () => preview(i)));
        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const btn = form.querySelector("button");
          btn.disabled = true;
          try {
            const d = await api(`/approx/game/${gameId}/submit`, {
              question: i, low: form.querySelector(".ax-lo").value, high: form.querySelector(".ax-hi").value,
            });
            st.standing = d.standing;
            if (d.status !== st.status) { poll(); } else { updateTeam(); }
            const pq = d.standing.per_question[i];
            flash(pq && pq.good ? `Question ${i + 1}: good interval ✓` : `Question ${i + 1}: not in that interval ✗`, !(pq && pq.good));
            form.reset();
            preview(i);
          } catch (err) { flash(err.message, true); }
          btn.disabled = false;
        });
      });
      main.querySelectorAll(".ax-num-form").forEach((form) => {
        const btn = form.querySelector("button"), inp = form.querySelector(".ax-val");
        const reset = () => { delete btn.dataset.sure; btn.textContent = "Lock in"; };
        inp.addEventListener("input", reset);
        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const i = +form.dataset.q, v = inp.value.trim();
          if (!btn.dataset.sure) {
            btn.dataset.sure = "1"; btn.textContent = `Sure? Lock in ${v}`;
            setTimeout(() => { if (btn.isConnected && btn.dataset.sure) reset(); }, 4000);
            return;
          }
          btn.disabled = true;
          try {
            const d = await api(`/approx/game/${gameId}/submit`, { question: i, value: v });
            st.standing = d.standing;
            if (d.status !== st.status) { poll(); } else { updateTeam(); }
            flash(`Question ${i + 1}: ${d.standing.per_question[i].shown} locked in ✓`);
          } catch (err) { flash(err.message, true); reset(); }
          btn.disabled = false;
        });
      });
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

    function updateTeamMod() {
      const s = st.standing || {}, iv = s.interval;
      const playing = st.status === "playing", over = st.status === "finished";
      const stat = (label, value, extra = "", id = "") =>
        `<div class="ax-stat"><span>${label}</span><strong${id ? ` id="${id}"` : ""}>${value}</strong>${extra}</div>`;
      const meter = (pct, low) => `<div class="ax-meter${low ? " is-low" : ""}"><i style="width:${pct}%"></i></div>`;
      const cells = [];
      if (over) {
        const parts = s.parts;
        cells.push(parts
          ? `<div class="ax-stat ax-hero-stat"><span>Your total · highest wins</span><strong>${parts.total}</strong></div>`
          : iv ? `<div class="ax-stat ax-hero-stat"><span>Your score · lowest wins</span><strong>${esc(iv.score_label)}</strong></div>`
          : `<div class="ax-stat ax-hero-stat"><span>Your points · highest wins</span><strong>${s.points ?? 0}</strong></div>`);
        if (parts) cells.push(stat("Normal score", `${esc(iv.score_label)} <small>→ ${parts.interval}</small>`),
                              stat("Closest pts", `${s.points} <small>→ ${parts.crowd}</small>`));
        else if (iv) cells.push(stat("Good", `${iv.good}/${iv.n}`));
        else cells.push(stat("Teams", s.n_teams || 0));
        cells.push(stat("Game over", endWords(st.end_reason)));
      } else {
        if (iv) {
          cells.push(`<div class="ax-stat ax-hero-stat"><span>Normal score · lowest wins</span><strong>${esc(iv.score_label)}</strong></div>`,
                     stat("Good", `${iv.good}/${iv.n}`),
                     stat("Intervals left", iv.attempts_left, meter(st.attempts ? 100 * iv.attempts_left / st.attempts : 0, iv.attempts_left <= 2)));
        }
        if (s.n_crowd) {
          const answered = stat(iv ? "Closest-number" : "Answered", `${s.answered || 0}/${s.n_crowd}`,
            meter(100 * (s.answered || 0) / s.n_crowd));
          cells.push(iv ? answered : answered.replace('class="ax-stat"', 'class="ax-stat ax-hero-stat"'));
          if (!iv) cells.push(stat("Closest gets", `${s.n_teams || 0} pts`));
        }
        cells.push(stat("Time left", clock(secondsLeft),
          `<div class="ax-meter" id="axTimeMeter"><i style="width:${timePct()}%"></i></div>`, "axClock"));
      }
      bar.classList.remove("hidden");
      bar.style.setProperty("--cols", `2fr repeat(${cells.length - 1}, 1fr)`);
      bar.innerHTML = cells.join("");
      (st.questions || []).forEach((q, i) => {
        const pq = (s.per_question || [])[i];
        const el = $(`#axCur${i}`), card = $(`#axCard${i}`), form = $(`#axForm${i}`);
        if (!el) return;
        if (q.interval) {
          if (card) { card.classList.toggle("is-good", !!(pq && pq.good)); card.classList.toggle("is-bad", !!(pq && !pq.good)); }
          if (form) form.querySelectorAll("input, button").forEach((x) => { x.disabled = !!s.finished || !iv || iv.attempts_left <= 0; });
          el.innerHTML = pq
            ? `<span class="ax-tag ${pq.good ? "is-good" : "is-bad"}">${pq.good ? `✓ good · +${pq.points}` : "✗ missed"}</span>
               <span>${fmt(pq.low)} to ${fmt(pq.high)}</span>`
            : '<span class="ax-muted">No interval yet</span>';
          preview(i);
          return;
        }
        if (card) { card.classList.toggle("is-good", !!(pq && over && pq.rank === 1)); card.classList.toggle("is-bad", !!(over && !pq)); }
        if (form) form.classList.toggle("hidden", !!pq || !!s.finished);
        el.innerHTML = pq
          ? (over ? `<span class="ax-tag ${pq.rank === 1 ? "is-good" : ""}">${pq.rank ? `${ordinal(pq.rank)} closest · +${pq.points}` : "not scored yet"}</span>
                     <span>You said ${esc(pq.shown)}</span>`
                  : `<span class="ax-tag is-done">locked in</span><span>${esc(pq.shown)}</span>`)
          : `<span class="ax-muted">${over ? "No answer: 0 points" : s.finished ? "Not answered" : "Not answered yet"}</span>`;
      });
      const fin = $("#axFinish");
      if (fin) { fin.disabled = !!s.finished; if (s.finished) fin.textContent = "Answers locked in: waiting for the other teams"; }
      const board = $("#axBoard");
      if (board) board.innerHTML = boardHtml(st.leaderboard, st.team_name);
      guard(playing && !s.finished);
    }

    // What the score would be if this interval turns out good, and if not,
    // before it's submitted. Replaces whatever interval the question has now.
    function preview(i) {
      const el = $(`#axPrev${i}`), form = main.querySelector(`.ax-submit[data-q="${i}"]`);
      if (!el || !form || !st.standing || !form.querySelector(".ax-lo")) return;
      const loRaw = form.querySelector(".ax-lo").value, hiRaw = form.querySelector(".ax-hi").value;
      if (!loRaw.trim() && !hiRaw.trim()) { el.innerHTML = ""; return; }
      const lo = parseQ(loRaw), hi = parseQ(hiRaw);
      if (lo == null || hi == null) { el.innerHTML = '<span class="ax-muted">Enter two positive numbers (e.g. 1500, 1.5e3, 3x10^6)</span>'; return; }
      if (lo > hi) { el.innerHTML = '<span class="ax-bad-text">Low is above high</span>'; return; }
      const others = (st.standing.per_question || []).filter((pq, j) => j !== i && pq && pq.good).map((pq) => pq.points);
      // Scored over the interval questions only, in a modified game.
      const n = (st.standing.interval || {}).n || st.n_questions, pts = Math.floor(hi / lo * (1 + 1e-12));
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
      if (isMod()) { updateTeamMod(); return; }
      const s = st.standing || {};
      bar.classList.remove("hidden");
      const usedPct = st.attempts ? 100 * (st.attempts - s.attempts_left) / st.attempts : 0;
      bar.innerHTML = `
        <div class="ax-stat ax-hero-stat"><span>Your score · lowest wins</span><strong>${esc(s.score_label)}</strong></div>
        <div class="ax-stat"><span>Good</span><strong>${s.good}/${st.n_questions}</strong></div>
        <div class="ax-stat"><span>Submissions left</span><strong>${s.attempts_left}</strong>
          <div class="ax-meter${s.attempts_left <= 2 ? " is-low" : ""}"><i style="width:${100 - usedPct}%"></i></div></div>
        ${st.status === "playing" ? `<div class="ax-stat"><span>Time left</span><strong id="axClock">${clock(secondsLeft)}</strong>
             <div class="ax-meter" id="axTimeMeter"><i style="width:${timePct()}%"></i></div></div>`
          : `<div class="ax-stat"><span>Game over</span><strong>${endWords(st.end_reason)}</strong></div>`}`;
      (s.per_question || []).forEach((pq, i) => {
        const el = $(`#axCur${i}`), card = $(`#axCard${i}`);
        if (!el) return;
        if (card) { card.classList.toggle("is-good", !!(pq && pq.good)); card.classList.toggle("is-bad", !!(pq && !pq.good)); }
        el.innerHTML = pq
          ? `<span class="ax-tag ${pq.good ? "is-good" : "is-bad"}">${pq.good ? `✓ good · +${pq.points}` : "✗ missed"}</span>
             <span>${fmt(pq.low)} to ${fmt(pq.high)}</span>`
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
