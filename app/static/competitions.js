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
    draft: { label: "Draft", cls: "cmp-pill-draft" },
    scheduled: { label: "Scheduled", cls: "cmp-pill-soon" },
    running: { label: "Live", cls: "cmp-pill-live" },
    finished: { label: "Finished", cls: "cmp-pill-done" },
  };

  const fmtWhen = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toLocaleString(undefined, {
      weekday: "short", day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit",
    });
  };

  // What the event covers, in one line.
  function formatLine(c) {
    const bits = [];
    if (c.mode_labels && c.mode_labels.length) {
      bits.push(c.mode_labels.length <= 3
        ? c.mode_labels.join(", ")
        : `${c.mode_labels.slice(0, 3).join(", ")} +${c.mode_labels.length - 3}`);
    } else {
      bits.push("every mode");
    }
    if (c.starts_at && c.status === "scheduled") bits.push(`opens ${fmtWhen(c.starts_at)}`);
    if (c.ends_at) bits.push(`closes ${fmtWhen(c.ends_at)}`);
    return bits.join(" · ");
  }

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
            <div class="cmp-row-format">${esc(formatLine(c))}</div>
          </div>
          <div class="cmp-row-actions">
            <button class="btn ghost" data-board="${esc(c.id)}">Board</button>
            ${mine && (c.status === "draft" || c.status === "scheduled")
              ? `<button class="btn" data-start="${esc(c.id)}">Open now</button>` : ""}
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
    // The setup form is built from the server's spec, so a new mode or a new
    // scenario appears here without this file being touched.
    if (state.can_host && !SPEC) {
      try {
        SPEC = await api("/competitions/spec");
        renderModes();
        renderSettings();
      } catch { /* the rest of the page still works */ }
    }
    const { competitions } = await api("/competitions/list");
    renderList(competitions);
    if (selected) loadBoard(selected);
  }

  // ── Setup form ────────────────────────────────────────────────────────
  let SPEC = null;                    // {modes:[...], settings:{mode:[fields]}}

  const chosenModes = () =>
    [...document.querySelectorAll("#cmpModes input:checked")].map((i) => i.value);

  function renderModes() {
    $("#cmpModes").innerHTML = SPEC.modes.map((m) => `
      <label class="cmp-mode" title="${esc(m.blurb || "")}">
        <input type="checkbox" value="${esc(m.key)}">
        <span>${esc(m.label)}${SPEC.settings[m.key] ? ' <span class="cmp-cog">⚙</span>' : ""}</span>
      </label>`).join("");

    document.querySelectorAll("#cmpModes input").forEach((i) =>
      i.addEventListener("change", renderSettings));
  }

  // Only modes that are both selected and configurable get a settings block.
  function renderSettings() {
    const picked = chosenModes().filter((m) => SPEC.settings[m]);
    const field = $("#cmpSettingsField");
    if (!picked.length) { field.hidden = true; $("#cmpSettings").innerHTML = ""; return; }
    field.hidden = false;

    $("#cmpSettings").innerHTML = picked.map((mode) => {
      const label = (SPEC.modes.find((m) => m.key === mode) || {}).label || mode;
      const fields = SPEC.settings[mode].map((f) => {
        const id = `set_${mode}_${f.key}`;
        if (f.type === "select") {
          return `<label class="cmp-set">
            <span>${esc(f.label)}</span>
            <select id="${id}" class="msp-input" data-mode="${esc(mode)}" data-key="${esc(f.key)}">
              ${f.options.map((o) => `<option value="${esc(o.value)}"${o.value === f.default ? " selected" : ""}>${esc(o.label)}</option>`).join("")}
            </select></label>`;
        }
        if (f.type === "number") {
          return `<label class="cmp-set">
            <span>${esc(f.label)}</span>
            <input id="${id}" class="msp-input" type="number" min="${f.min}" max="${f.max}"
                   value="${f.default}" data-mode="${esc(mode)}" data-key="${esc(f.key)}"></label>`;
        }
        // multi
        return `<div class="cmp-set cmp-set-multi">
          <span>${esc(f.label)}</span>
          <div class="cmp-multi" data-mode="${esc(mode)}" data-key="${esc(f.key)}">
            ${f.options.map((o) => `
              <label class="cmp-chip">
                <input type="checkbox" value="${esc(o.value)}"
                  ${(f.default || []).includes(o.value) ? "checked" : ""}>
                <span>${esc(o.label)}</span>
              </label>`).join("")}
          </div></div>`;
      }).join("");

      return `<div class="cmp-setblock">
        <div class="cmp-setblock-head">${esc(label)}</div>
        <div class="cmp-setgrid">${fields}</div>
      </div>`;
    }).join("");
  }

  function collectSettings() {
    const out = {};
    document.querySelectorAll("#cmpSettings [data-mode][data-key]").forEach((el) => {
      const { mode, key } = el.dataset;
      out[mode] = out[mode] || {};
      if (el.classList.contains("cmp-multi")) {
        out[mode][key] = [...el.querySelectorAll("input:checked")].map((i) => i.value);
      } else {
        out[mode][key] = el.type === "number" ? Number(el.value) : el.value;
      }
    });
    return out;
  }

  // The three timing choices drive one pair of inputs.
  function whenMode() {
    return (document.querySelector('input[name="cmpWhen"]:checked') || {}).value || "now";
  }

  document.querySelectorAll('input[name="cmpWhen"]').forEach((r) =>
    r.addEventListener("change", () => {
      const w = whenMode();
      $("#cmpTimes").classList.toggle("hidden", w !== "later");
      $("#cmpWhenHint").textContent = w === "now"
        ? "Opens the moment you create it."
        : w === "later"
          ? "Opens by itself at the time you set — no need to be at a computer."
          : "Saved without a start time. Open it by hand whenever you like.";
    }));

  // A datetime-local value is wall-clock in the viewer's zone; send it as a
  // real instant so a host in one timezone and a player in another agree.
  const localToIso = (v) => (v ? new Date(v).toISOString() : null);

  $("#cmpCreate")?.addEventListener("click", async (e) => {
    const name = ($("#cmpName").value || "").trim() || "Competition";
    const w = whenMode();
    const payload = {
      name,
      modes: chosenModes(),
      settings: collectSettings(),
      start_now: w === "now",
      starts_at: w === "later" ? localToIso($("#cmpStart").value) : null,
      ends_at: w === "later" ? localToIso($("#cmpEnd").value) : null,
    };
    if (w === "later" && !payload.starts_at) {
      showMsg("Pick a time for it to open, or choose Start now.");
      return;
    }

    e.target.disabled = true;
    try {
      const res = await post("/competitions/create", payload);
      const c = res.competition;
      $("#cmpName").value = "";
      document.querySelectorAll("#cmpModes input:checked").forEach((i) => { i.checked = false; });
      renderSettings();
      await refresh();
      showMsg(c.status === "running"
        ? `“${c.name}” is live — share code ${c.code}.`
        : c.status === "scheduled"
          ? `“${c.name}” opens ${fmtWhen(c.starts_at)}. Code ${c.code}.`
          : `“${c.name}” saved as a draft.`, "ok");
    } catch (err) {
      showMsg(err.message);
    } finally {
      e.target.disabled = false;
    }
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
