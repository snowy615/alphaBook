(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  const api = async (url, init) => {
    const r = await fetch(url, { credentials: "include", ...init });
    const txt = await r.text();
    let body = {};
    try { body = txt ? JSON.parse(txt) : {}; } catch { /* non-JSON error page */ }
    if (!r.ok) {
      const e = new Error(body.detail || `HTTP ${r.status}`);
      e.status = r.status;
      throw e;
    }
    return body;
  };
  const post = (url, payload) => api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });

  const msg = $("#msg");
  function showMsg(text, kind = "error") {
    msg.textContent = text;
    msg.className = "msp-msg msp-msg-" + (kind === "ok" ? "ok" : "error");
    msg.classList.remove("hidden");
  }
  function clearMsg() { msg.classList.add("hidden"); }

  let state = null;
  let selected = null;

  // ── Where the player is right now ─────────────────────────────────────
  function renderState() {
    const box = $("#stateBody");
    const c = state.competition;

    if (!c || !c.scoring) {
      box.innerHTML = `
        <div class="cmp-now">
          <div>
            <span class="cmp-badge cmp-badge-practice">Practice</span>
            <span class="cmp-now-text">Your results build your ratings and feedback.</span>
          </div>
          <div class="cmp-join">
            <input id="joinCode" class="msp-input msp-code-input" type="text" maxlength="6"
              placeholder="CODE" autocomplete="off" spellcheck="false">
            <button class="btn primary" id="joinBtn">Join competition</button>
          </div>
        </div>
        ${c && !c.scoring ? `<p class="msp-muted cmp-hint">
          You were entered in <strong>${esc(c.name)}</strong>, which has finished — you're back in practice.</p>` : ""}`;
      $("#joinBtn").addEventListener("click", join);
      $("#joinCode").addEventListener("keydown", (e) => { if (e.key === "Enter") join(); });
    } else {
      box.innerHTML = `
        <div class="cmp-now">
          <div>
            <span class="cmp-badge cmp-badge-live">In competition</span>
            <span class="cmp-now-text"><strong>${esc(c.name)}</strong> ·
              code <span class="cmp-code">${esc(c.code)}</span> ·
              ${c.entrants} entrant${c.entrants === 1 ? "" : "s"}</span>
          </div>
          <div class="cmp-join">
            <button class="btn" id="boardBtn">View board</button>
            <button class="btn ghost" id="leaveBtn">Back to practice</button>
          </div>
        </div>
        <p class="msp-muted cmp-hint">Every game you finish now also counts here. Leaving keeps
          the results you've already posted.</p>`;
      $("#leaveBtn").addEventListener("click", leave);
      $("#boardBtn").addEventListener("click", () => loadBoard(c.id));
    }
    $("#hostCard").classList.toggle("hidden", !state.can_host);
  }

  async function join() {
    const code = ($("#joinCode").value || "").trim().toUpperCase();
    if (!code) { showMsg("Enter the competition code."); return; }
    clearMsg();
    try {
      await post("/competitions/join", { code });
      await refresh();
      showMsg("You're in. Results from now on count toward this competition.", "ok");
    } catch (e) {
      showMsg(e.status === 401 ? "Please log in first." : e.message);
    }
  }

  async function leave() {
    try {
      await post("/competitions/leave");
      await refresh();
      showMsg("Back in practice.", "ok");
    } catch (e) { showMsg(e.message); }
  }

  // ── The list ──────────────────────────────────────────────────────────
  const STATUS = {
    draft: { label: "Not started", cls: "cmp-pill-draft" },
    running: { label: "Live", cls: "cmp-pill-live" },
    finished: { label: "Finished", cls: "cmp-pill-done" },
  };

  function renderList(rows) {
    $("#cmpCount").textContent = rows.length
      ? `${rows.length} total` : "";
    const box = $("#cmpList");
    if (!rows.length) {
      box.innerHTML = `<div class="cmp-empty">No competitions yet.</div>`;
      return;
    }
    box.innerHTML = rows.map((c) => {
      const st = STATUS[c.status] || STATUS.draft;
      const mine = state && state.can_host;
      return `
        <div class="cmp-row${c.joined ? " cmp-row-mine" : ""}">
          <div class="cmp-row-main">
            <div class="cmp-row-name">${esc(c.name)}
              <span class="cmp-pill ${st.cls}">${st.label}</span>
              ${c.joined ? '<span class="cmp-pill cmp-pill-you">entered</span>' : ""}</div>
            <div class="cmp-row-meta">
              host ${esc(c.host_name || "—")} ·
              ${c.entrants} entrant${c.entrants === 1 ? "" : "s"}
              ${c.status !== "finished" ? ` · code <span class="cmp-code">${esc(c.code)}</span>` : ""}
            </div>
          </div>
          <div class="cmp-row-actions">
            <button class="btn ghost" data-board="${esc(c.id)}">Board</button>
            ${mine && c.status === "draft" ? `<button class="btn" data-start="${esc(c.id)}">Open it</button>` : ""}
            ${mine && c.status === "running" ? `<button class="btn" data-finish="${esc(c.id)}">Close it</button>` : ""}
          </div>
        </div>`;
    }).join("");

    box.querySelectorAll("[data-board]").forEach((b) =>
      b.addEventListener("click", () => loadBoard(b.dataset.board)));
    box.querySelectorAll("[data-start]").forEach((b) =>
      b.addEventListener("click", () => hostAction(b.dataset.start, "start")));
    box.querySelectorAll("[data-finish]").forEach((b) =>
      b.addEventListener("click", () => hostAction(b.dataset.finish, "finish")));
  }

  async function hostAction(id, action) {
    if (action === "finish" && !confirm("Close this competition? The board freezes and entrants go back to practice.")) return;
    try {
      await post(`/competitions/${id}/${action}`);
      await refresh();
      showMsg(action === "start" ? "Competition is open — share the code." : "Competition closed.", "ok");
    } catch (e) { showMsg(e.message); }
  }

  // ── The board ─────────────────────────────────────────────────────────
  async function loadBoard(id) {
    try {
      const data = await api(`/competitions/${id}/leaderboard`);
      selected = id;
      $("#boardCard").classList.remove("hidden");
      $("#boardTitle").textContent = data.competition.name;
      $("#boardNote").textContent = data.overall.length
        ? `${data.overall.length} ranked · ${data.played} results`
        : "no results yet";

      $("#boardHead").innerHTML = `
        <tr><th class="cmp-w-rank">#</th><th>Player</th><th class="cmp-w-rating">Rating</th>
          <th class="cmp-w-mid">Modes</th><th class="cmp-w-mid">Games</th></tr>`;

      if (!data.overall.length) {
        $("#boardBody").innerHTML = `<tr><td colspan="5" class="cmp-empty">
          Nothing scored yet — results appear as entrants finish games.</td></tr>`;
        return;
      }

      $("#boardBody").innerHTML = data.overall.map((r) => `
        <tr class="${r.is_me ? "lbx-me" : ""}">
          <td class="lbx-rank">${r.rank}</td>
          <td class="lbx-name">${esc(r.username)}${r.is_me ? '<span class="lbx-you">you</span>' : ""}</td>
          <td>
            <div class="lbx-rating">
              <span class="lbx-rating-num">${(+r.overall).toFixed(1)}</span>
              <span class="lbx-bar"><span class="lbx-bar-fill"
                style="width:${Math.max(0, Math.min(100, +r.overall))}%"></span></span>
            </div>
          </td>
          <td class="lbx-mid">${r.modes_played}</td>
          <td class="lbx-mid">${r.total_games}</td>
        </tr>`).join("");
      $("#boardCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (e) {
      showMsg(e.message);
    }
  }

  // ── Boot ──────────────────────────────────────────────────────────────
  async function refresh() {
    state = await api("/competitions/mine");
    renderState();
    const { competitions } = await api("/competitions/list");
    renderList(competitions);
    if (selected) loadBoard(selected);
  }

  $("#cmpCreate")?.addEventListener("click", async () => {
    const name = ($("#cmpName").value || "").trim() || "Competition";
    try {
      const res = await post("/competitions/create", { name, modes: [] });
      $("#cmpName").value = "";
      await refresh();
      showMsg(`Created “${res.competition.name}”. Open it when you're ready.`, "ok");
    } catch (e) { showMsg(e.message); }
  });

  (async () => {
    try {
      const me = await api("/me");
      $("#userName").textContent = me.username || "";
    } catch { /* signed out; join will prompt */ }
    try {
      await refresh();
    } catch (e) {
      $("#stateBody").innerHTML =
        `<span class="msp-muted">${e.status === 401
          ? 'Please <a href="/login">log in</a> to join a competition.'
          : esc(e.message)}</span>`;
      $("#cmpList").innerHTML = `<div class="cmp-empty">—</div>`;
    }
  })();
})();
