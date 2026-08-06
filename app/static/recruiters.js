/* Recruiter dashboard.
 *
 * A table by default, because a recruiter is scanning and comparing rather
 * than browsing; cards stay available for a closer look. Contact is always one
 * click: copy an address, mail everyone shown, or export the lot to Excel.
 * Nothing is ever sent on AlphaBook's behalf.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  let students = [];
  let shown = [];
  let view = "table";

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

  const nameOf = (s) => s.full_name || s.username;

  const modeChips = (s, n) => (s.modes || []).slice(0, n).map((m) =>
    `<span class="rec-mode" title="${esc(m.label)} · ${m.games} games">${esc(m.label)}
       <strong>${m.rating}</strong>${m.provisional ? "*" : ""}</span>`).join("")
    || '<span class="msp-muted">—</span>';

  const contactCell = (s) => `
    ${s.email
      ? `<button class="btn ghost rec-copy" data-email="${esc(s.email)}"
           title="Copy ${esc(s.email)}">${esc(s.email)}</button>`
      : '<span class="msp-muted">no email on file</span>'}
    ${s.cv_uploaded
      ? `<a class="btn ghost" href="/recruiters/students/${esc(s.id)}/cv"
           target="_blank" rel="noopener">CV ↗</a>`
      : ""}`;

  // ---- summary ----
  function renderSummary() {
    const ranked = students.filter((s) => s.overall !== null && s.overall !== undefined);
    const avg = ranked.length
      ? Math.round(ranked.reduce((n, s) => n + s.overall, 0) / ranked.length)
      : null;
    const withCv = students.filter((s) => s.cv_uploaded).length;
    const years = students.map((s) => s.graduation_year).filter(Boolean);

    const tile = (num, label) =>
      `<div class="rec-stat"><span class="rec-stat-num">${num}</span>
         <span class="rec-stat-lbl">${label}</span></div>`;

    $("#summary").innerHTML =
      tile(students.length, "students listed") +
      tile(ranked.length, "with a rating") +
      tile(avg === null ? "—" : avg, "average rating") +
      tile(withCv, "CV on file") +
      tile(years.length ? Math.min(...years) : "—", "earliest graduation");
    $("#summary").classList.remove("hidden");
  }

  // ---- views ----
  function tableRow(s) {
    return `
      <tr>
        <td>
          <div class="rec-tname">${esc(nameOf(s))}</div>
          <div class="rec-tsub">${esc(s.username)}</div>
        </td>
        <td>${esc(s.membership)}</td>
        <td class="rec-num">${s.graduation_year || "—"}</td>
        <td class="rec-num">${s.overall === null || s.overall === undefined
          ? '<span class="msp-muted">—</span>'
          : `<strong>${s.overall}</strong> <span class="msp-muted">#${s.rank}</span>`}</td>
        <td class="rec-num">${s.total_games || 0}</td>
        <td class="rec-modecell">${modeChips(s, 3)}</td>
        <td class="rec-contact">${contactCell(s)}</td>
      </tr>`;
  }

  function card(s) {
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
            <div class="rec-name">${esc(nameOf(s))}</div>
            <div class="rec-sub">
              ${esc(s.membership)}${s.graduation_year ? ` · ${s.graduation_year}` : ""} · ${esc(s.club)}
            </div>
          </div>
          ${standing}
        </div>
        <div class="rec-modes">${modeChips(s, 3)}</div>
        <div class="rec-foot">${contactCell(s)}</div>
      </article>`;
  }

  function bindCopies(root) {
    root.querySelectorAll(".rec-copy").forEach((b) => {
      b.addEventListener("click", async () => {
        const was = b.textContent;
        try {
          await navigator.clipboard.writeText(b.dataset.email);
          b.textContent = "copied ✓";
          setTimeout(() => { b.textContent = was; }, 1200);
        } catch {
          window.location.href = "mailto:" + b.dataset.email;
        }
      });
    });
  }

  function render() {
    const q = ($("#search").value || "").trim().toLowerCase();
    const membership = $("#membershipFilter").value;
    const year = $("#yearFilter").value;
    const sort = $("#sort").value;
    const cvOnly = $("#cvOnly").checked;

    const rows = students.filter((s) => {
      if (membership && s.membership !== membership) return false;
      if (year && String(s.graduation_year || "") !== year) return false;
      if (cvOnly && !s.cv_uploaded) return false;
      if (!q) return true;
      return [s.full_name, s.username, s.membership, s.club, s.email]
        .some((v) => String(v || "").toLowerCase().includes(q));
    });

    if (sort === "name") {
      rows.sort((a, b) => nameOf(a).localeCompare(nameOf(b)));
    } else if (sort === "year") {
      rows.sort((a, b) => (a.graduation_year || 9999) - (b.graduation_year || 9999));
    }
    shown = rows;

    $("#count").textContent = `${rows.length} of ${students.length}`;

    const tableWrap = $("#tableWrap");
    const grid = $("#grid");
    const empty = '<div class="rec-empty">Nobody matches that.</div>';

    if (!rows.length) {
      grid.classList.add("hidden");
      tableWrap.classList.remove("hidden");
      tableWrap.innerHTML = empty;
      return;
    }

    if (view === "table") {
      grid.classList.add("hidden");
      tableWrap.classList.remove("hidden");
      tableWrap.innerHTML = '<table class="rec-table" id="table"></table>';
      $("#table").innerHTML = `
        <thead><tr>
          <th>Student</th><th>Membership</th><th class="rec-num">Grad</th>
          <th class="rec-num">Overall</th><th class="rec-num">Games</th>
          <th>Strongest modes</th><th>Contact</th>
        </tr></thead>
        <tbody>${rows.map(tableRow).join("")}</tbody>`;
      bindCopies(tableWrap);
    } else {
      tableWrap.classList.add("hidden");
      grid.classList.remove("hidden");
      grid.innerHTML = rows.map(card).join("");
      bindCopies(grid);
    }
  }

  const emailsShown = () => shown.map((s) => s.email).filter(Boolean);

  (async () => {
    try {
      const me = await api("/me");
      $("#userName").textContent = me.username || "";
    } catch { /* handled below */ }

    try {
      const data = await api("/recruiters/directory");
      students = data.students || [];

      (data.memberships || []).forEach((m) => {
        const o = document.createElement("option");
        o.value = m; o.textContent = m;
        $("#membershipFilter").appendChild(o);
      });

      [...new Set(students.map((s) => s.graduation_year).filter(Boolean))]
        .sort().forEach((y) => {
          const o = document.createElement("option");
          o.value = String(y); o.textContent = String(y);
          $("#yearFilter").appendChild(o);
        });

      $("#tools").classList.remove("hidden");
      ["search", "membershipFilter", "yearFilter", "sort", "cvOnly"].forEach((id) =>
        $("#" + id).addEventListener("input", render));

      $("#viewTable").addEventListener("click", () => {
        view = "table";
        $("#viewTable").classList.add("is-on");
        $("#viewCards").classList.remove("is-on");
        render();
      });
      $("#viewCards").addEventListener("click", () => {
        view = "cards";
        $("#viewCards").classList.add("is-on");
        $("#viewTable").classList.remove("is-on");
        render();
      });

      // Bulk contact. BCC, so recipients never see each other's addresses.
      $("#mailAll").addEventListener("click", () => {
        const list = emailsShown();
        if (!list.length) { alert("Nobody shown has an address on file."); return; }
        if (list.length > 40 && !confirm(
            `This opens your mail client with ${list.length} recipients in BCC. Continue?`)) return;
        window.location.href = `mailto:?bcc=${encodeURIComponent(list.join(","))}`;
      });

      $("#copyAll").addEventListener("click", async () => {
        const list = emailsShown();
        if (!list.length) { alert("Nobody shown has an address on file."); return; }
        const btn = $("#copyAll");
        const was = btn.textContent;
        try {
          await navigator.clipboard.writeText(list.join(", "));
          btn.textContent = `${list.length} copied ✓`;
        } catch {
          btn.textContent = "copy blocked";
        }
        setTimeout(() => { btn.textContent = was; }, 1400);
      });

      renderSummary();

      if (!students.length) {
        $("#tableWrap").classList.remove("hidden");
        $("#tableWrap").innerHTML = `<div class="rec-empty">
          No students to show yet. Everyone is listed unless they opt out in their settings.</div>`;
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
      ["exportBtn", "mailAll", "copyAll"].forEach((id) => $("#" + id)?.classList.add("hidden"));
    }
  })();
})();
