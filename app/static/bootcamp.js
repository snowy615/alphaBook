// The Bootcamp board: lectures and announcements. Bootcamp members read and
// download; Analysts also post, edit, attach and delete.
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  const api = async (url, init) => {
    const r = await fetch(url, { credentials: "include", cache: "no-store", ...init });
    const txt = await r.text();
    let body = {};
    try { body = txt ? JSON.parse(txt) : {}; } catch { /* non-JSON error page */ }
    if (!r.ok) { const e = new Error(body.detail || `HTTP ${r.status}`); e.status = r.status; throw e; }
    return body;
  };
  const send = (method, url, payload) => api(url, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}),
  });

  const msg = $("#msg");
  function showMsg(text, ok) {
    msg.textContent = text;
    msg.className = "msp-msg msp-msg-" + (ok ? "ok" : "error");
    clearTimeout(showMsg.t);
    if (ok) showMsg.t = setTimeout(() => msg.classList.add("hidden"), 4000);
  }

  let data = null;            // { can_edit, posts, sessions }
  let filter = "";
  let editingId = null;
  let newKind = "lecture";

  const sizeLabel = (n) => n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
  const when = (iso) => iso ? new Date(iso).toLocaleString(undefined,
    { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  // Links in a post become clickable; everything else stays as typed.
  const linkify = (text) => esc(text).replace(/https?:\/\/[^\s<]+[^\s<.,;:!?)"']/g,
    (u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);

  // ── Session strip ────────────────────────────────────────────────────────
  function drawNext() {
    const s = data.sessions || [];
    if (!s.length) { $("#next").innerHTML = ""; return; }
    const next = s.find((x) => x.status === "next");
    $("#next").innerHTML = `<div class="bc-next">
      <div>${next ? `<div class="bc-next-label">Next session</div>
        <div class="bc-next-when">${esc(next.date_label)}, ${esc(next.time_label)}</div>
        <div class="bc-next-where">${esc(next.venue)}${next.title ? ` · ${esc(next.title)}` : ""}</div>`
        : '<div class="bc-next-label">Sessions</div><div class="bc-next-when">All six sessions done</div>'}</div>
      <div class="bc-dates">${s.map((x) => `<span class="bc-date is-${x.status}" title="${esc(x.date_label)}, ${esc(x.time_label)}">${esc(x.date_label.replace(/^\w+ /, ""))}</span>`).join("")}</div>
    </div>`;
  }

  // ── Posts ────────────────────────────────────────────────────────────────
  // Each file: its name opens it to read (in the browser, or Microsoft's
  // Office viewer for slides, documents and spreadsheets), plus View and
  // Download. Files the browser can't show (zips, notebooks) only download.
  function fileRow(post, f) {
    const ext = (f.name.split(".").pop() || "").slice(0, 5);
    const base = `/bootcamp/posts/${esc(post.id)}/files/${esc(f.id)}`;
    const viewTitle = f.view === "office" ? "Opens in Microsoft's Office viewer, in a new tab" : "Opens in a new tab";
    return `<div class="bc-file">
      <span class="bc-file-ext">${esc(ext)}</span>
      ${f.view ? `<a href="${base}/view" target="_blank" rel="noopener" title="${viewTitle}">${esc(f.name)}</a>`
               : `<a href="${base}" download>${esc(f.name)}</a>`}
      <span class="bc-file-size">${sizeLabel(f.size || 0)}</span>
      ${f.view ? `<a class="btn ghost bc-file-btn" href="${base}/view" target="_blank" rel="noopener" title="${viewTitle}">View</a>` : ""}
      <a class="btn ghost bc-file-btn" href="${base}" download>Download</a>
      ${data.can_edit ? `<button class="btn ghost bc-file-x" data-act="rmfile" data-id="${esc(post.id)}" data-file="${esc(f.id)}" title="Remove ${esc(f.name)}" aria-label="Remove ${esc(f.name)}">✕</button>` : ""}
    </div>`;
  }

  function postHtml(p) {
    if (editingId === p.id) return editHtml(p);
    const edited = p.updated_at ? ` · edited${p.updated_by_name ? ` by ${esc(p.updated_by_name)}` : ""} ${esc(when(p.updated_at))}` : "";
    return `<article class="card bc-post" id="post-${esc(p.id)}">
      <div class="bc-post-top">
        <span class="bc-kind is-${esc(p.kind)}">${p.kind === "lecture" ? "Lecture" : "Announcement"}</span>
        <span class="bc-meta">${esc(p.author_name)} · ${esc(when(p.created_at))}${edited}</span>
      </div>
      <h3>${esc(p.title)}</h3>
      ${p.body ? `<div class="bc-body">${linkify(p.body)}</div>` : ""}
      ${p.files.length ? `<div class="bc-files">${p.files.map((f) => fileRow(p, f)).join("")}</div>` : ""}
      ${data.can_edit ? `<div class="bc-edit-row">
        <button class="btn ghost" data-act="edit" data-id="${esc(p.id)}">Edit</button>
        <label class="btn ghost" style="cursor:pointer;">Add files
          <input type="file" multiple hidden data-act="addfiles" data-id="${esc(p.id)}"></label>
        <button class="btn ghost" data-act="email" data-id="${esc(p.id)}">${p.email && p.email.done ? "Email again" : p.email ? "Finish emailing" : "Email to Bootcamp"}</button>
        <button class="btn ghost" data-act="delete" data-id="${esc(p.id)}">Delete</button>
        <span class="bc-progress" id="prog-${esc(p.id)}"></span>
      </div>
      ${emailLine(p)}` : ""}
    </article>`;
  }

  // Whether (and how far) a post has been emailed: Analysts only.
  function emailLine(p) {
    const e = p.email;
    if (!e) return "";
    const failed = e.failed ? ` · ${e.failed} couldn't be sent` : "";
    return e.done
      ? `<div class="bc-emailed is-done">✓ Emailed to ${e.sent} member${e.sent === 1 ? "" : "s"}${e.by_name ? ` by ${esc(e.by_name)}` : ""} · ${esc(when(e.at))}${failed}</div>`
      : `<div class="bc-emailed">Emailing stopped part-way: ${e.sent} of ${e.total} sent${failed}. Press Finish emailing to send the rest.</div>`;
  }

  // Email a post to the Bootcamp, a batch per request, until everyone has it.
  async function emailPost(postId, again, progressEl) {
    let first = true, sent = 0;
    for (;;) {
      const d = await send("POST", `/bootcamp/posts/${encodeURIComponent(postId)}/email`, { again: again && first });
      first = false;
      replacePost(d.post);
      sent = d.post.email ? d.post.email.sent : sent;
      const total = d.post.email ? d.post.email.total : 0;
      if (progressEl && progressEl.isConnected) progressEl.textContent = d.remaining ? `Emailing… ${sent} of ${total}` : "";
      if (!d.remaining) break;
    }
    drawFeed();
    const e = (data.posts.find((p) => p.id === postId) || {}).email || {};
    const members = (n) => `${n} member${n === 1 ? "" : "s"}`;
    showMsg(e.failed ? `Emailed ${members(e.sent)}; ${e.failed} couldn't be sent.` : `Emailed to ${members(e.sent)} of the Bootcamp.`, !e.failed);
  }

  function editHtml(p) {
    return `<article class="card bc-post bc-form" id="post-${esc(p.id)}">
      <div class="bc-kinds" role="group" aria-label="Kind of post">
        ${["lecture", "announcement"].map((k) => `<button type="button" class="bc-tab${p.kind === k ? " is-on" : ""}" data-editkind="${k}">${k === "lecture" ? "Lecture" : "Announcement"}</button>`).join("")}
      </div>
      <input class="msp-input" id="editTitle" maxlength="150" value="${esc(p.title)}" aria-label="Title">
      <textarea class="msp-input" id="editBody" maxlength="10000" aria-label="Post">${esc(p.body)}</textarea>
      <div class="bc-actions">
        <button class="btn primary" data-act="save" data-id="${esc(p.id)}">Save</button>
        <button class="btn ghost" data-act="cancel">Cancel</button>
      </div>
    </article>`;
  }

  function drawFeed() {
    const posts = (data.posts || []).filter((p) => !filter || p.kind === filter);
    $("#filters").classList.toggle("hidden", !(data.posts || []).length);
    $("#feed").innerHTML = posts.length ? posts.map(postHtml).join("")
      : `<p class="bc-empty">${(data.posts || []).length ? "Nothing here yet."
        : data.can_edit ? "No posts yet. Write the first one above." : "Nothing posted yet. Lectures and announcements will appear here."}</p>`;
  }

  function replacePost(post) {
    const i = data.posts.findIndex((x) => x.id === post.id);
    if (i >= 0) data.posts[i] = post; else data.posts.unshift(post);
  }

  // Attach files one at a time (each is its own request, so one bad file
  // doesn't lose the rest), showing progress beside the post.
  async function upload(postId, files, progressEl) {
    let post = null, failed = [];
    for (let i = 0; i < files.length; i++) {
      if (progressEl) progressEl.textContent = `Uploading ${i + 1} of ${files.length}: ${files[i].name}…`;
      const form = new FormData();
      form.append("file", files[i]);
      try {
        post = (await api(`/bootcamp/posts/${encodeURIComponent(postId)}/files`, { method: "POST", body: form })).post;
      } catch (err) { failed.push(`${files[i].name} (${err.message})`); }
    }
    if (progressEl) progressEl.textContent = "";
    if (failed.length) showMsg(`Couldn't attach ${failed.join("; ")}`, false);
    return post;
  }

  // ── Actions ──────────────────────────────────────────────────────────────
  document.addEventListener("click", async (e) => {
    const kindBtn = e.target.closest("#composer [data-kind]");
    if (kindBtn) {
      newKind = kindBtn.dataset.kind;
      document.querySelectorAll("#composer [data-kind]").forEach((b) => {
        b.classList.toggle("is-on", b === kindBtn); b.setAttribute("aria-pressed", b === kindBtn ? "true" : "false");
      });
      return;
    }
    const editKind = e.target.closest("[data-editkind]");
    if (editKind) {
      editKind.parentElement.querySelectorAll("[data-editkind]").forEach((b) => b.classList.toggle("is-on", b === editKind));
      return;
    }
    const filt = e.target.closest("#filters [data-filter]");
    if (filt) {
      filter = filt.dataset.filter;
      document.querySelectorAll("#filters [data-filter]").forEach((b) => b.classList.toggle("is-on", b === filt));
      drawFeed();
      return;
    }
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    try {
      if (btn.dataset.act === "edit") { editingId = id; drawFeed(); $("#editTitle").focus(); }
      else if (btn.dataset.act === "cancel") { editingId = null; drawFeed(); }
      else if (btn.dataset.act === "save") {
        const card = btn.closest("article");
        const kind = (card.querySelector("[data-editkind].is-on") || {}).dataset?.editkind || "announcement";
        btn.disabled = true;
        const d = await send("PUT", `/bootcamp/posts/${encodeURIComponent(id)}`,
          { kind, title: $("#editTitle").value, body: $("#editBody").value });
        replacePost(d.post); editingId = null; drawFeed(); showMsg("Saved.", true);
      } else if (btn.dataset.act === "delete") {
        // Two clicks, not a pop-up.
        if (!btn.dataset.sure) {
          btn.dataset.sure = "1"; btn.textContent = "Sure? Click again to delete it and its files";
          setTimeout(() => { if (btn.isConnected) { delete btn.dataset.sure; btn.textContent = "Delete"; } }, 4000);
          return;
        }
        await send("DELETE", `/bootcamp/posts/${encodeURIComponent(id)}`);
        data.posts = data.posts.filter((p) => p.id !== id); drawFeed(); showMsg("Deleted.", true);
      } else if (btn.dataset.act === "email") {
        const post = data.posts.find((p) => p.id === id) || {};
        const again = !!(post.email && post.email.done);
        if (!btn.dataset.sure) {
          btn.dataset.sure = "1";
          btn.textContent = `Sure? Click again to email ${data.member_count || "all"} Bootcamp member${data.member_count === 1 ? "" : "s"}${again ? " again" : ""}`;
          setTimeout(() => { if (btn.isConnected && !btn.disabled) { delete btn.dataset.sure; drawFeed(); } }, 5000);
          return;
        }
        btn.disabled = true;
        await emailPost(id, again, $(`#prog-${CSS.escape(id)}`));
      } else if (btn.dataset.act === "rmfile") {
        if (!btn.dataset.sure) {
          btn.dataset.sure = "1"; btn.textContent = "Remove?";
          setTimeout(() => { if (btn.isConnected) { delete btn.dataset.sure; btn.textContent = "✕"; } }, 4000);
          return;
        }
        const d = await send("DELETE", `/bootcamp/posts/${encodeURIComponent(id)}/files/${encodeURIComponent(btn.dataset.file)}`);
        replacePost(d.post); drawFeed();
      }
    } catch (err) { showMsg(err.message, false); btn.disabled = false; }
  });

  document.addEventListener("change", async (e) => {
    const inp = e.target.closest('input[data-act="addfiles"]');
    if (!inp || !inp.files.length) return;
    const id = inp.dataset.id;
    const post = await upload(id, [...inp.files], $(`#prog-${CSS.escape(id)}`));
    if (post) { replacePost(post); drawFeed(); showMsg("Files attached.", true); }
  });

  $("#newPost").addEventListener("click", async () => {
    const btn = $("#newPost"), files = [...$("#newFiles").files];
    btn.disabled = true;
    try {
      let post = (await send("POST", "/bootcamp/posts",
        { kind: newKind, title: $("#newTitle").value, body: $("#newBody").value })).post;
      replacePost(post); drawFeed();
      if (files.length) post = (await upload(post.id, files, $("#newProgress"))) || post;
      replacePost(post); drawFeed();
      const email = $("#newEmail").checked;
      $("#newTitle").value = ""; $("#newBody").value = ""; $("#newFiles").value = ""; $("#newEmail").checked = false;
      showMsg("Posted. Bootcamp members can see it now.", true);
      if (email) await emailPost(post.id, false, $("#newProgress"));
    } catch (err) { showMsg(err.message, false); }
    btn.disabled = false;
  });

  // ── Attendance ───────────────────────────────────────────────────────────
  // Members see their own marks and percentage; Analysts mark a session at a
  // time and see everyone. Present counts as a full session, late as 0.8 of
  // one (as the server counts it); excused doesn't count either way; unmarked
  // sessions don't count yet.
  const MARK_LABEL = { present: "Present", late: "Late", absent: "Absent", excused: "Excused" };
  const MARK_KEY = { present: "P", late: "L", absent: "A", excused: "E" };
  let att = null, attSession = null, attSearch = "";
  const pctText = (p) => p == null ? "—" : `${p}%`;
  const LOW = 60;                                 // below this, shown in red

  const CREDIT = { present: 1, late: 0.8, absent: 0 };
  function summary(marks) {
    const counted = Object.values(marks).filter((m) => m in CREDIT);
    const attended = Math.round(10 * counted.reduce((a, m) => a + CREDIT[m], 0)) / 10;
    return { attended, counted: counted.length, percent: counted.length ? Math.round(100 * attended / counted.length) : null };
  }

  function drawMine() {
    const me = att.mine, sum = summary(me.marks);
    $("#attendanceView").innerHTML = `
      <div class="card bc-att-me">
        <div class="bc-pct${sum.percent != null && sum.percent < LOW ? " is-low" : ""}">${pctText(sum.percent)}</div>
        <div class="bc-pct-sub">${sum.counted
          ? `You've attended <strong>${sum.attended} of ${sum.counted}</strong> session${sum.counted === 1 ? "" : "s"} so far.`
          : "Nothing recorded yet. Your attendance shows here once the Analysts take the register."}
          <br>Present counts as a full session and late as 0.8 of one; excused sessions don't count against you.</div>
      </div>
      <div class="card">${att.sessions.map((x) => {
        const m = me.marks[x.date];
        const tag = m ? `<span class="bc-mark is-${m}">${MARK_LABEL[m]}</span>`
          : `<span class="bc-mark">${x.status === "done" ? "Not recorded" : x.status === "next" ? "Next" : "Upcoming"}</span>`;
        return `<div class="bc-sess"><div><strong>Session ${x.number}</strong>${x.title ? ` · ${esc(x.title)}` : ""}
          <div class="bc-sess-when">${esc(x.date_label)}, ${esc(x.time_label)}</div></div>${tag}</div>`;
      }).join("")}</div>`;
  }

  function defaultSession() {
    const done = att.sessions.filter((x) => x.status === "done");
    return (done[done.length - 1] || att.sessions[0] || {}).date;
  }

  function drawRegister() {
    const sid = attSession;
    const sess = att.sessions.find((x) => x.date === sid) || {};
    const q = attSearch.trim().toLowerCase();
    const rows = att.members.filter((m) => !q || m.name.toLowerCase().includes(q) || (m.email || "").toLowerCase().includes(q));
    const counts = { present: 0, late: 0, absent: 0, excused: 0, none: 0 };
    att.members.forEach((m) => { counts[m.marks[sid] || "none"]++; });
    const grid = `<div class="bc-grid-wrap"><table class="bc-grid">
      <thead><tr><th>Everyone</th>${att.sessions.map((x) => `<th title="${esc(x.date_label)}">S${x.number}</th>`).join("")}<th>Attendance</th></tr></thead>
      <tbody>${att.members.map((m) => {
        const sum = summary(m.marks);
        return `<tr><td>${esc(m.name)}</td>${att.sessions.map((x) => {
          const k = m.marks[x.date];
          return `<td class="${k ? `m-${k}` : ""}">${k ? MARK_KEY[k] : "·"}</td>`;
        }).join("")}<td class="${sum.percent != null && sum.percent < LOW ? "is-low" : ""}">${pctText(sum.percent)}</td></tr>`;
      }).join("")}</tbody></table></div>
      <p class="bc-key">P present · L late · A absent · E excused · · not recorded. Attendance = (present + 0.8 × late) ÷ sessions recorded, not counting excused.</p>`;
    $("#attendanceView").innerHTML = `
      <div class="bc-att-sessions" role="group" aria-label="Session">${att.sessions.map((x) =>
        `<button type="button" class="bc-tab${x.date === sid ? " is-on" : ""}" data-sess="${esc(x.date)}">S${x.number} · ${esc(x.date_label.replace(/^\w+ /, ""))}</button>`).join("")}</div>
      <div class="bc-att-tools">
        <input class="msp-input" id="attSearch" placeholder="Search by name or email" value="${esc(attSearch)}" aria-label="Search members">
        <button class="btn ghost" data-att-all="present">Unrecorded → Present</button>
        <button class="btn ghost" data-att-all="absent">Unrecorded → Absent</button>
        <button class="btn ghost" id="attCsv">Download CSV</button>
      </div>
      <p class="bc-att-count"><strong>Session ${sess.number || ""}${sess.title ? `: ${esc(sess.title)}` : ""}</strong> · ${esc(sess.date_label || "")} ·
        ${counts.present} present · ${counts.late} late · ${counts.absent} absent · ${counts.excused} excused · ${counts.none} not recorded</p>
      <div class="card bc-reg">${rows.map((m) => {
        const sum = summary(m.marks), cur = m.marks[sid];
        return `<div class="bc-reg-row">
          <div class="bc-reg-who"><strong>${esc(m.name)}</strong><span>${esc(m.email)}</span></div>
          <span class="bc-reg-pct${sum.percent != null && sum.percent < LOW ? " is-low" : ""}" title="Attendance so far">${pctText(sum.percent)}</span>
          <div class="bc-seg" role="group" aria-label="${esc(m.name)}">${att.marks.map((k) =>
            `<button type="button" class="${cur === k ? `is-${k}` : ""}" data-mark="${k}" data-uid="${esc(m.uid)}"
               title="${MARK_LABEL[k]}${cur === k ? " (click again to clear)" : ""}" aria-pressed="${cur === k}">${MARK_KEY[k]}</button>`).join("")}</div>
        </div>`;
      }).join("") || '<p class="bc-empty" style="padding:14px;">Nobody matches.</p>'}</div>
      ${grid}`;
    const search = $("#attSearch");
    search.addEventListener("input", () => {
      attSearch = search.value; const pos = search.selectionStart;
      drawRegister(); const again = $("#attSearch"); again.focus(); again.setSelectionRange(pos, pos);
    });
  }

  async function saveMarks(changes) {
    const sid = attSession, before = {};
    att.members.forEach((m) => { if (m.uid in changes) before[m.uid] = m.marks[sid]; });
    const apply = (vals) => att.members.forEach((m) => {
      if (!(m.uid in vals)) return;
      if (vals[m.uid]) m.marks[sid] = vals[m.uid]; else delete m.marks[sid];
    });
    apply(changes); drawRegister();               // show it straight away
    try {
      await send("PUT", `/bootcamp/attendance/${encodeURIComponent(sid)}`, { marks: changes });
    } catch (err) {
      apply(Object.fromEntries(Object.keys(changes).map((u) => [u, before[u] || null]))); drawRegister();
      showMsg(err.message, false);
    }
  }

  function csv() {
    const head = ["Name", "Email", ...att.sessions.map((x) => `S${x.number} ${x.date}`), "Attended", "Counted", "Attendance %"];
    const lines = att.members.map((m) => {
      const sum = summary(m.marks);
      return [m.name, m.email, ...att.sessions.map((x) => MARK_LABEL[m.marks[x.date]] || ""), sum.attended, sum.counted,
              sum.percent == null ? "" : sum.percent];
    });
    const cell = (v) => /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v);
    const blob = new Blob([[head, ...lines].map((r) => r.map(cell).join(",")).join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "bootcamp_attendance.csv"; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  document.addEventListener("click", (e) => {
    if (!att || !att.can_edit) return;
    const sess = e.target.closest("[data-sess]");
    if (sess) { attSession = sess.dataset.sess; drawRegister(); return; }
    const mark = e.target.closest("[data-mark]");
    if (mark) {
      const m = att.members.find((x) => x.uid === mark.dataset.uid);
      const k = mark.dataset.mark;
      saveMarks({ [m.uid]: m.marks[attSession] === k ? null : k });   // clicking the current mark clears it
      return;
    }
    const all = e.target.closest("[data-att-all]");
    if (all) {
      const changes = {};
      att.members.forEach((m) => { if (!m.marks[attSession]) changes[m.uid] = all.dataset.attAll; });
      if (!Object.keys(changes).length) { showMsg("Everyone already has a mark for this session.", true); return; }
      saveMarks(changes);
      return;
    }
    if (e.target.closest("#attCsv")) csv();
  });

  async function loadAttendance() {
    try {
      att = await api("/bootcamp/attendance");
    } catch (err) { $("#attendanceView").innerHTML = `<p class="bc-empty">Couldn't load attendance: ${esc(err.message)}</p>`; return; }
    if (att.can_edit) { attSession = attSession || defaultSession(); drawRegister(); } else drawMine();
  }

  // ── Group projects ───────────────────────────────────────────────────────
  // Members start a project (name + description), share its code to let
  // people straight in, or accept requests to join. Everyone sees every
  // project and its members' Oxford emails. One project per member.
  let pj = null, pjEditing = false, pjLeaving = null;

  const memberRows = (p) => `<div class="bc-pj-members">${p.members.map((m) => `
    <div class="bc-pj-member"><span>${esc(m.name)}${m.is_owner ? '<span class="bc-pj-lead">runs it</span>' : ""}</span>
      ${m.email ? `<a href="mailto:${esc(m.email)}">${esc(m.email)}</a>` : '<span class="msp-muted">no email on file</span>'}</div>`).join("")}</div>`;

  function myProjectHtml(p) {
    const edit = pjEditing && p.is_owner;
    return `<article class="card bc-pj is-mine" id="pj-${esc(p.id)}">
      <div class="bc-pj-top"><span class="bc-pj-tag">Your project</span>
        <span class="bc-meta">${p.members.length} member${p.members.length === 1 ? "" : "s"}</span></div>
      ${edit ? `<div class="bc-form" style="margin-top:10px;">
          <input class="msp-input" id="pjEditName" maxlength="80" value="${esc(p.name)}" aria-label="Project name">
          <textarea class="msp-input" id="pjEditDesc" maxlength="2000" aria-label="Description">${esc(p.description)}</textarea>
          <div class="bc-actions"><button class="btn primary" data-pj="save" data-id="${esc(p.id)}">Save</button>
            <button class="btn ghost" data-pj="cancel-edit">Cancel</button></div></div>`
        : `<h3 style="margin-top:8px;">${esc(p.name)}</h3>${p.description ? `<p class="bc-pj-desc">${linkify(p.description)}</p>` : ""}`}
      ${p.is_owner ? `<div class="bc-pj-code"><strong>${esc(p.code)}</strong>
          <span>Your join code. Anyone in the Bootcamp who enters it joins straight away, so only share it with people you want in.</span>
          <button class="btn ghost" data-pj="copy" data-code="${esc(p.code)}">Copy</button>
          <button class="btn ghost" data-pj="newcode" data-id="${esc(p.id)}" title="The old code stops working">New code</button></div>` : ""}
      ${p.is_owner && (p.requests || []).length ? `<div class="bc-pj-sub">Asking to join</div>${p.requests.map((r) => `
        <div class="bc-pj-req"><div><strong>${esc(r.name)}</strong> · <a href="mailto:${esc(r.email)}">${esc(r.email)}</a></div>
          <button class="btn primary" data-pj="accept" data-id="${esc(p.id)}" data-uid="${esc(r.uid)}">Accept</button>
          <button class="btn ghost" data-pj="decline" data-id="${esc(p.id)}" data-uid="${esc(r.uid)}">Decline</button></div>`).join("")}` : ""}
      <div class="bc-pj-sub">Members</div>${memberRows(p)}
      ${pjLeaving === p.id ? `<div class="bc-pj-confirm"><span>Leave <strong>${esc(p.name)}</strong>?
          ${p.members.length === 1 ? "You're the only member, so the project will be removed." : p.is_owner ? "The longest-standing member will take over running it." : "You'll need the code or a new request to rejoin."}</span>
          <button class="btn bc-danger" data-pj="leave-yes" data-id="${esc(p.id)}">Leave</button>
          <button class="btn ghost" data-pj="leave-no">Cancel</button></div>`
        : `<div class="bc-pj-actions">${p.is_owner && !edit ? `<button class="btn ghost" data-pj="edit">Edit name and description</button>` : ""}
          <button class="btn ghost" data-pj="leave" data-id="${esc(p.id)}">Leave project</button></div>`}
    </article>`;
  }

  function otherProjectHtml(p) {
    const canAsk = pj.is_member && !pj.in_project;
    return `<article class="card bc-pj">
      <div class="bc-pj-top"><h3>${esc(p.name)}</h3>
        <span class="bc-meta">${p.members.length} member${p.members.length === 1 ? "" : "s"} · run by ${esc(p.owner_name)}</span></div>
      ${p.description ? `<p class="bc-pj-desc">${linkify(p.description)}</p>` : ""}
      ${memberRows(p)}
      <div class="bc-pj-actions">
        ${canAsk ? (p.requested
          ? `<span class="bc-meta">Request sent. ${esc(p.owner_name)} will see it.</span><button class="btn ghost" data-pj="unrequest" data-id="${esc(p.id)}">Cancel request</button>`
          : `<button class="btn" data-pj="request" data-id="${esc(p.id)}">Request to join</button>`) : ""}
        ${pj.can_moderate ? `<button class="btn ghost" data-pj="remove" data-id="${esc(p.id)}">Remove project</button>` : ""}
      </div>
    </article>`;
  }

  // Everyone not in a project yet: where people looking for a group, or for
  // teammates, find each other.
  function undecidedHtml() {
    const list = pj.undecided || [];
    return `<h4 class="evt-section-title" style="font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:26px 0 10px;">
        Not in a project yet · ${list.length}</h4>
      ${list.length ? `<div class="card" style="padding:4px 18px;"><div class="bc-pj-members" style="border-top:0;">${list.map((m) => `
        <div class="bc-pj-member"><span>${esc(m.name)}${m.uid === pj.me ? '<span class="bc-pj-lead">you</span>' : ""}</span>
          ${m.email ? `<a href="mailto:${esc(m.email)}">${esc(m.email)}</a>` : '<span class="msp-muted">no email on file</span>'}</div>`).join("")}
        </div></div>` : '<p class="bc-empty">Everyone is in a project.</p>'}`;
  }

  function drawProjects() {
    const mine = pj.projects.filter((p) => p.is_member), others = pj.projects.filter((p) => !p.is_member);
    const start = pj.is_member && !pj.in_project ? `<div class="bc-pj-start">
        <div class="card bc-form"><h3>Start a project</h3>
          <input class="msp-input" id="pjName" maxlength="80" placeholder="Project name (you can change it later)" aria-label="Project name">
          <textarea class="msp-input" id="pjDesc" maxlength="2000" placeholder="What you want to build or research, and who you're looking for" aria-label="Description"></textarea>
          <div class="bc-actions"><button class="btn primary" data-pj="create">Start project</button></div></div>
        <div class="card bc-form"><h3>Have a join code?</h3>
          <input class="msp-input bc-pj-code-in" id="pjCode" maxlength="6" placeholder="CODE" autocomplete="off" aria-label="Join code">
          <div class="bc-actions"><button class="btn primary" data-pj="join">Join</button></div>
          <p class="bc-hint">Ask whoever runs a project for its code, or press Request to join on it below.</p></div>
      </div>` : "";
    $("#projectsView").innerHTML = `${start}${mine.map(myProjectHtml).join("")}
      ${others.length ? `<h4 class="evt-section-title" style="font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:22px 0 10px;">${mine.length ? "Other projects" : "Projects"}</h4>` : ""}
      ${others.map(otherProjectHtml).join("")}
      ${!pj.projects.length ? `<p class="bc-empty">No projects yet.${pj.is_member ? " Start the first one above." : ""}</p>` : ""}
      ${undecidedHtml()}`;
  }

  async function loadProjects() {
    try { pj = await api("/bootcamp/projects"); } catch (err) {
      $("#projectsView").innerHTML = `<p class="bc-empty">Couldn't load projects: ${esc(err.message)}</p>`; return;
    }
    drawProjects();
  }

  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-pj]");
    if (!b || !pj) return;
    const act = b.dataset.pj, id = b.dataset.id ? encodeURIComponent(b.dataset.id) : "";
    try {
      if (act === "edit") { pjEditing = true; drawProjects(); return; }
      if (act === "cancel-edit") { pjEditing = false; drawProjects(); return; }
      if (act === "leave") { pjLeaving = b.dataset.id; drawProjects(); return; }
      if (act === "leave-no") { pjLeaving = null; drawProjects(); return; }
      if (act === "copy") {
        try { await navigator.clipboard.writeText(b.dataset.code); showMsg("Code copied.", true); }
        catch { showMsg(`The code is ${b.dataset.code}`, true); }
        return;
      }
      if (act === "remove" && !b.dataset.sure) {
        b.dataset.sure = "1"; b.textContent = "Sure? Click again to remove it for everyone";
        setTimeout(() => { if (b.isConnected) { delete b.dataset.sure; b.textContent = "Remove project"; } }, 4000);
        return;
      }
      b.disabled = true;
      if (act === "create") {
        await send("POST", "/bootcamp/projects", { name: $("#pjName").value, description: $("#pjDesc").value });
        showMsg("Project started. Share its code with the people you want in.", true);
      } else if (act === "join") {
        const d = await send("POST", "/bootcamp/projects/join", { code: $("#pjCode").value });
        showMsg(`You've joined ${d.name || "the project"}.`, true);
      } else if (act === "save") {
        await send("PUT", `/bootcamp/projects/${id}`, { name: $("#pjEditName").value, description: $("#pjEditDesc").value });
        pjEditing = false; showMsg("Saved.", true);
      } else if (act === "newcode") {
        await send("POST", `/bootcamp/projects/${id}/code`); showMsg("New code made. The old one no longer works.", true);
      } else if (act === "request") {
        await send("POST", `/bootcamp/projects/${id}/request`); showMsg("Request sent.", true);
      } else if (act === "unrequest") {
        await send("DELETE", `/bootcamp/projects/${id}/request`);
      } else if (act === "accept" || act === "decline") {
        await send("POST", `/bootcamp/projects/${id}/requests/${encodeURIComponent(b.dataset.uid)}`, { accept: act === "accept" });
      } else if (act === "leave-yes") {
        const d = await send("POST", `/bootcamp/projects/${id}/leave`);
        pjLeaving = null; showMsg(d.removed ? "You've left, and the project has been removed." : "You've left the project.", true);
      } else if (act === "remove") {
        await send("DELETE", `/bootcamp/projects/${id}`); showMsg("Project removed.", true);
      }
      await loadProjects();
    } catch (err) { showMsg(err.message, false); b.disabled = false; }
  });

  // Posts / Attendance / Group projects, remembered in the address.
  const VIEWS = { posts: "#postsView", attendance: "#attendanceView", projects: "#projectsView" };
  function showView(view) {
    for (const [name, sel] of Object.entries(VIEWS)) $(sel).classList.toggle("hidden", name !== view);
    document.querySelectorAll("#mainTabs [data-view]").forEach((b) => {
      b.classList.toggle("is-on", b.dataset.view === view); b.setAttribute("aria-selected", b.dataset.view === view ? "true" : "false");
    });
    if (view === "attendance") loadAttendance();
    if (view === "projects") loadProjects();
    history.replaceState(null, "", view === "posts" ? location.pathname : `#${view}`);
  }
  document.querySelectorAll("#mainTabs [data-view]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

  // ── Load ─────────────────────────────────────────────────────────────────
  async function load() {
    try {
      data = await api("/bootcamp/posts");
    } catch (err) {
      $("#feed").innerHTML = "";
      const box = $("#denied");
      box.classList.remove("hidden");
      box.innerHTML = err.status === 401
        ? `<p>Log in to see the Bootcamp page.</p><a class="btn primary" href="/login?next=/bootcamp">Log in</a>`
        : err.status === 403
          ? `<p>This page is for Quant Bootcamp members and the Analysts who run it.</p>
             <a class="btn" href="/apply">About the Bootcamp</a>`
          : `<p>Couldn't load the page: ${esc(err.message)}</p>`;
      return;
    }
    $("#composer").classList.toggle("hidden", !data.can_edit);
    if (data.can_edit) $("#newEmailLabel").textContent = `Also email it to the Bootcamp (${data.member_count} member${data.member_count === 1 ? "" : "s"})`;
    if (data.can_edit) $("#lede").textContent = "Post lectures and announcements for the Bootcamp. Members see them here and can download the materials.";
    drawNext();
    drawFeed();
    $("#mainTabs").classList.remove("hidden");
    const hash = location.hash.slice(1);
    if (hash in VIEWS && hash !== "posts") showView(hash);
  }
  load();
})();
