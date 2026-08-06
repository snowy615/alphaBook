(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  let students = [];

  async function api(url) {
    const r = await fetch(url, { credentials: "include" });
    const txt = await r.text();
    let body = {};
    try { body = txt ? JSON.parse(txt) : {}; } catch { /* non-JSON */ }
    if (!r.ok) {
      const e = new Error(body.detail || `HTTP ${r.status}`);
      e.status = r.status;
      throw e;
    }
    return body;
  }

  function card(s) {
    const modes = (s.modes || []).slice(0, 3).map((m) => `
      <span class="rec-mode" title="${esc(m.label)} · ${m.games} games">
        ${esc(m.label)} <strong>${m.rating}</strong>${m.provisional ? "*" : ""}</span>`).join("");

    const standing = s.overall !== null && s.overall !== undefined
      ? `<div class="rec-overall">
           <span class="rec-overall-num">${s.overall}</span>
           <span class="rec-overall-sub">overall · rank #${s.rank}</span>
         </div>`
      : `<div class="rec-overall rec-overall-none">
           <span class="rec-overall-sub">no scored games yet</span>
         </div>`;

    return `
      <article class="rec-card">
        <div class="rec-card-top">
          <div>
            <div class="rec-name">${esc(s.full_name || s.username)}</div>
            <div class="rec-sub">
              ${esc(s.membership)}${s.graduation_year ? ` · ${s.graduation_year}` : ""} · ${esc(s.club)}
            </div>
          </div>
          ${standing}
        </div>

        <div class="rec-modes">${modes || '<span class="msp-muted">—</span>'}</div>

        <div class="rec-foot">
          ${s.email
            ? `<button class="btn ghost rec-copy" data-email="${esc(s.email)}">${esc(s.email)}</button>`
            : '<span class="msp-muted">no email on file</span>'}
          ${s.cv_uploaded
            ? `<a class="btn ghost" href="/recruiters/students/${esc(s.id)}/cv" target="_blank" rel="noopener">CV ↗</a>`
            : ""}
        </div>
      </article>`;
  }

  function render() {
    const q = ($("#search").value || "").trim().toLowerCase();
    const membership = $("#membershipFilter").value;
    const sort = $("#sort").value;

    let rows = students.filter((s) => {
      if (membership && s.membership !== membership) return false;
      if (!q) return true;
      return [s.full_name, s.username, s.membership, s.club]
        .some((v) => String(v || "").toLowerCase().includes(q));
    });

    if (sort === "name") {
      rows.sort((a, b) => (a.full_name || a.username).localeCompare(b.full_name || b.username));
    } else if (sort === "year") {
      rows.sort((a, b) => (a.graduation_year || 9999) - (b.graduation_year || 9999));
    }

    $("#count").textContent = `${rows.length} of ${students.length}`;
    $("#grid").innerHTML = rows.length
      ? rows.map(card).join("")
      : `<div class="rec-empty">Nobody matches that.</div>`;

    // Click an address to copy it; recruiters mail from their own inbox.
    $("#grid").querySelectorAll(".rec-copy").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(b.dataset.email);
          const was = b.textContent;
          b.textContent = "copied ✓";
          setTimeout(() => { b.textContent = was; }, 1200);
        } catch {
          window.location.href = "mailto:" + b.dataset.email;
        }
      });
    });
  }

  (async () => {
    try {
      const me = await api("/me");
      $("#userName").textContent = me.username || "";
    } catch { /* handled below */ }

    try {
      const data = await api("/recruiters/directory");
      students = data.students || [];

      const sel = $("#membershipFilter");
      (data.memberships || []).forEach((m) => {
        const o = document.createElement("option");
        o.value = m; o.textContent = m;
        sel.appendChild(o);
      });

      $("#tools").classList.remove("hidden");
      ["search", "membershipFilter", "sort"].forEach((id) =>
        $("#" + id).addEventListener("input", render));

      if (!students.length) {
        $("#grid").innerHTML = `<div class="rec-empty">
          No students have opted in yet. They turn this on from their profile.</div>`;
        $("#count").textContent = "";
        return;
      }
      render();
    } catch (e) {
      const gate = $("#gate");
      gate.textContent = e.status === 401
        ? "Please log in to view the directory."
        : e.message;
      gate.className = "msp-msg msp-msg-error";
      gate.classList.remove("hidden");
    }
  })();
})();
