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

  // Keep what an Analyst is writing to a group through redraws.
  const draftOf = (id) => (mailDraft[id] = mailDraft[id] || { subject: "", body: "", off: new Set() });
  document.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset.mailSubject) draftOf(t.dataset.mailSubject).subject = t.value;
    else if (t.dataset.mailBody) draftOf(t.dataset.mailBody).body = t.value;
  });
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (!t.dataset.mailTo) return;
    const off = draftOf(t.dataset.mailTo).off;
    if (t.checked) off.delete(t.value); else off.add(t.value);
  });

  document.addEventListener("change", async (e) => {
    const inp = e.target.closest('input[data-act="addfiles"]');
    if (!inp || !inp.files.length) return;
    const id = inp.dataset.id;
    const post = await upload(id, [...inp.files], $(`#prog-${CSS.escape(id)}`));
    if (post) { replacePost(post); drawFeed(); showMsg(`${inp.files.length} file${inp.files.length === 1 ? "" : "s"} attached.`, true); }
  });

  // Files waiting to go with the new post. Picking again, or dropping more
  // in, adds to the list instead of replacing it (a file input on its own
  // forgets the last pick), and each can be taken off before posting.
  let staged = [];
  const MAX_BYTES = 25 * 1024 * 1024;
  function stage(files) {
    const tooBig = [];
    for (const f of files) {
      if (f.size > MAX_BYTES) { tooBig.push(f.name); continue; }
      if (!staged.some((s) => s.name === f.name && s.size === f.size)) staged.push(f);
    }
    if (tooBig.length) showMsg(`Over 25 MB, so not added: ${tooBig.join(", ")}`, false);
    drawStaged();
  }
  function drawStaged() {
    $("#newStaged").innerHTML = staged.map((f, i) => `<div class="bc-file">
        <span class="bc-file-ext">${esc((f.name.split(".").pop() || "").slice(0, 5))}</span>
        <span>${esc(f.name)}</span><span class="bc-file-size">${sizeLabel(f.size)}</span>
        <button type="button" class="btn ghost bc-file-x" data-unstage="${i}" title="Take ${esc(f.name)} off" aria-label="Take ${esc(f.name)} off">✕</button>
      </div>`).join("");
  }
  $("#newFiles").addEventListener("change", (e) => { stage([...e.target.files]); e.target.value = ""; });
  $("#newStaged").addEventListener("click", (e) => {
    const b = e.target.closest("[data-unstage]");
    if (b) { staged.splice(+b.dataset.unstage, 1); drawStaged(); }
  });
  // Drop files on the new-post box to add them, or on a post (Analysts) to attach them straight away.
  const isFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  document.addEventListener("dragover", (e) => {
    if (!isFiles(e) || !data || !data.can_edit) return;
    const zone = e.target.closest("#newDrop, .bc-post");
    if (!zone) return;
    e.preventDefault();
    document.querySelectorAll(".is-over").forEach((z) => z !== zone && z.classList.remove("is-over"));
    zone.classList.add("is-over");
  });
  document.addEventListener("dragleave", (e) => {
    const zone = e.target.closest("#newDrop, .bc-post");
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove("is-over");
  });
  document.addEventListener("drop", async (e) => {
    if (!isFiles(e) || !data || !data.can_edit) return;
    const zone = e.target.closest("#newDrop, .bc-post");
    if (!zone) return;
    e.preventDefault();
    zone.classList.remove("is-over");
    const files = [...e.dataTransfer.files];
    if (zone.id === "newDrop") { stage(files); return; }
    const id = zone.id.replace(/^post-/, "");
    const post = await upload(id, files, $(`#prog-${CSS.escape(id)}`));
    if (post) { replacePost(post); drawFeed(); showMsg(`${files.length} file${files.length === 1 ? "" : "s"} attached.`, true); }
  });

  $("#newPost").addEventListener("click", async () => {
    const btn = $("#newPost"), files = staged.slice();
    btn.disabled = true;
    try {
      let post = (await send("POST", "/bootcamp/posts",
        { kind: newKind, title: $("#newTitle").value, body: $("#newBody").value })).post;
      replacePost(post); drawFeed();
      if (files.length) post = (await upload(post.id, files, $("#newProgress"))) || post;
      replacePost(post); drawFeed();
      const email = $("#newEmail").checked;
      $("#newTitle").value = ""; $("#newBody").value = ""; staged = []; drawStaged(); $("#newEmail").checked = false;
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
  let pj = null, pjEditing = null, pjLeaving = null;      // pjEditing: the project being renamed

  const memberRows = (p) => `<div class="bc-pj-members">${p.members.map((m) => `
    <div class="bc-pj-member"><span>${esc(m.name)}${m.is_owner ? '<span class="bc-pj-lead">runs it</span>' : ""}</span>
      ${m.email ? `<a href="mailto:${esc(m.email)}">${esc(m.email)}</a>` : '<span class="msp-muted">no email on file</span>'}</div>`).join("")}</div>`;

  function myProjectHtml(p) {
    const edit = pjEditing === p.id && p.is_owner;
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
      ${w7MemberHtml(p)}
      ${submissionHtml(p)}
      <div class="bc-pj-sub">Members</div>${memberRows(p)}
      ${pjLeaving === p.id ? `<div class="bc-pj-confirm"><span>Leave <strong>${esc(p.name)}</strong>?
          ${p.members.length === 1 ? "You're the only member, so the project will be removed." : p.is_owner ? "The longest-standing member will take over running it." : "You'll need the code or a new request to rejoin."}</span>
          <button class="btn bc-danger" data-pj="leave-yes" data-id="${esc(p.id)}">Leave</button>
          <button class="btn ghost" data-pj="leave-no">Cancel</button></div>`
        : `<div class="bc-pj-actions">${p.is_owner && !edit ? `<button class="btn ghost" data-pj="edit" data-id="${esc(p.id)}">Edit name and description</button>` : ""}
          <button class="btn ghost" data-pj="leave" data-id="${esc(p.id)}">Leave project</button></div>`}
    </article>`;
  }

  function otherProjectHtml(p) {
    const canAsk = pj.is_member && !pj.in_project;
    return `<article class="card bc-pj">
      <div class="bc-pj-top"><h3>${esc(p.name)}</h3>
        <span class="bc-meta">${p.members.length} member${p.members.length === 1 ? "" : "s"} · run by ${p.owner ? esc(p.owner_name) : "the Analysts"}</span></div>
      ${p.description ? `<p class="bc-pj-desc">${linkify(p.description)}</p>` : ""}
      ${memberRows(p)}
      <div class="bc-pj-actions">
        ${canAsk ? (p.requested
          ? `<span class="bc-meta">Request sent. ${p.owner ? esc(p.owner_name) : "The Analysts"} will see it.</span><button class="btn ghost" data-pj="unrequest" data-id="${esc(p.id)}">Cancel request</button>`
          : `<button class="btn" data-pj="request" data-id="${esc(p.id)}">Request to join</button>`) : ""}
        ${pj.can_moderate ? `<button class="btn ghost" data-pj="remove" data-id="${esc(p.id)}">Remove project</button>` : ""}
      </div>
    </article>`;
  }

  // ── Group submissions ─────────────────────────────────────────────────────
  // The group's handed-in work: files and a note. Anyone in the group adds
  // or takes off files and edits the note; Analysts open them; nobody else
  // sees the section at all (the server doesn't send it to them).
  const subNote = {};                // project id -> the note being written, kept across redraws
  function subFileRow(p, f, canEdit) {
    const ext = (f.name.split(".").pop() || "").slice(0, 5);
    const base = `/bootcamp/projects/${esc(p.id)}/submission/files/${esc(f.id)}`;
    const viewTitle = f.view === "office" ? "Opens in Microsoft's Office viewer, in a new tab" : "Opens in a new tab";
    return `<div class="bc-file">
      <span class="bc-file-ext">${esc(ext)}</span>
      <span>${f.view ? `<a href="${base}/view" target="_blank" rel="noopener" title="${viewTitle}">${esc(f.name)}</a>`
                     : `<a href="${base}" download>${esc(f.name)}</a>`}
        <span class="bc-meta" style="display:block;">${esc(f.by_name)}${f.at ? ` · ${esc(when(f.at))}` : ""}</span></span>
      <span class="bc-file-size">${sizeLabel(f.size || 0)}</span>
      ${f.view ? `<a class="btn ghost bc-file-btn" href="${base}/view" target="_blank" rel="noopener" title="${viewTitle}">View</a>` : ""}
      <a class="btn ghost bc-file-btn" href="${base}" download>Download</a>
      ${canEdit ? `<button class="btn ghost bc-file-x" data-pj="sub-rm" data-id="${esc(p.id)}" data-file="${esc(f.id)}" data-name="${esc(f.name)}" title="Take ${esc(f.name)} off" aria-label="Take ${esc(f.name)} off">✕</button>` : ""}
    </div>`;
  }
  function submissionHtml(p) {
    const sub = p.submission;
    if (!sub) return "";
    const status = sub.files.length
      ? `${sub.files.length} file${sub.files.length === 1 ? "" : "s"}${sub.updated_at ? ` · last changed by ${esc(sub.updated_by_name)}, ${esc(when(sub.updated_at))}` : ""}`
      : "Nothing handed in yet";
    const note = subNote[p.id] !== undefined ? subNote[p.id] : sub.note;
    return `<div class="bc-notes bc-sub"><div class="bc-pj-sub">Submission · only ${sub.can_edit ? "your group" : "the group"} and the Analysts see this</div>
      <div class="bc-meta" style="margin:2px 0 4px;">${status}</div>
      ${sub.files.map((f) => subFileRow(p, f, sub.can_edit)).join("")}
      ${sub.can_edit ? `
        <div class="bc-w7-book"><label class="btn ghost" style="cursor:pointer;">Add files
          <input type="file" multiple hidden data-sub-files="${esc(p.id)}"></label>
          <span class="bc-meta" id="subProg-${esc(p.id)}">Anyone in the group can add files or take them off. Up to 25 MB each.</span></div>
        <textarea class="msp-input" data-sub-note="${esc(p.id)}" maxlength="3000" rows="3" placeholder="A note for the Analysts: what's in the files, where to start" aria-label="Submission note">${esc(note)}</textarea>
        <div class="bc-w7-book"><button class="btn ghost" data-pj="sub-note" data-id="${esc(p.id)}">Save note</button></div>`
        : sub.note ? `<p class="bc-pj-desc" style="white-space:pre-wrap;">${linkify(sub.note)}</p>` : ""}
    </div>`;
  }
  // Analysts: which groups have handed something in.
  function subSummaryHtml() {
    if (!pj.can_moderate) return "";
    const groups = pj.projects.filter((p) => p.submission && p.members.length);
    if (!groups.length) return "";
    const done = groups.filter((p) => p.submission.files.length), not = groups.filter((p) => !p.submission.files.length);
    return `<div class="card" style="padding:14px 18px;margin-bottom:18px;"><h3 style="margin:0 0 6px;">Submissions · ${done.length} of ${groups.length} groups</h3>
      ${done.length ? done.map((p) => `<div class="bc-meta"><a href="#pj-${esc(p.id)}">${esc(p.name)}</a>: ${p.submission.files.length} file${p.submission.files.length === 1 ? "" : "s"}${p.submission.updated_at ? `, last changed ${esc(when(p.submission.updated_at))}` : ""}</div>`).join("") : ""}
      ${not.length ? `<p class="bc-hint" style="margin-top:8px;">Nothing yet: ${not.map((p) => esc(p.name)).join(", ")}</p>` : ""}</div>`;
  }
  async function uploadSubmission(id, files) {
    const prog = $(`#subProg-${CSS.escape(id)}`), failed = [];
    let ok = 0;
    for (let i = 0; i < files.length; i++) {
      if (files[i].size > 25 * 1024 * 1024) { failed.push(`${files[i].name} (over 25 MB)`); continue; }
      if (prog) prog.textContent = `Uploading ${i + 1} of ${files.length}: ${files[i].name}…`;
      const form = new FormData();
      form.append("file", files[i]);
      try { await api(`/bootcamp/projects/${encodeURIComponent(id)}/submission/files`, { method: "POST", body: form }); ok++; }
      catch (err) { failed.push(`${files[i].name} (${err.message})`); }
    }
    if (failed.length) showMsg(`Couldn't upload ${failed.join("; ")}`, false);
    else if (ok) showMsg(`${ok} file${ok === 1 ? "" : "s"} added to the submission.`, true);
    await loadProjects();
  }
  document.addEventListener("change", (e) => {
    const inp = e.target.closest("input[data-sub-files]");
    if (inp && inp.files.length) { const files = [...inp.files]; inp.value = ""; uploadSubmission(inp.dataset.subFiles, files); }
  });
  document.addEventListener("input", (e) => {
    if (e.target.dataset && e.target.dataset.subNote) subNote[e.target.dataset.subNote] = e.target.value;
  });

  // ── Week 7 check-ins ──────────────────────────────────────────────────────
  // Members mark when they're free (the interview grid, for 22–28 Nov);
  // everyone in the group sees how many of them are free in each slot. An
  // Analyst picks a slot and books a 30-minute check-in with one Analyst,
  // which invites the whole group. Analysts' notes on a group stay theirs.
  const w7Mine = {};                 // project id -> Set of slot ms (the member's own, while editing)
  const w7Pick = {};                 // project id -> slot ms an Analyst has picked to book
  const mailDraft = {};              // project id -> {subject, body, off: Set of uids left off}, kept across redraws

  // Analysts: one email to the group, the sender copied, so everyone can
  // reply all on the same thread. Untick anyone it isn't for.
  function groupMailHtml(p) {
    if (!p.members.length) return "";
    const d = mailDraft[p.id] || {}, off = d.off || new Set();
    const sent = (p.week7 && p.week7.messages) || [];
    return `<div class="bc-notes bc-mail"><div class="bc-pj-sub">Email the group</div>
      <p class="bc-hint" style="margin:2px 0 6px;">One email with everyone ticked on it and you on Cc, so anyone can reply all, for example to sort out where to meet.</p>
      <div class="bc-mail-to">${p.members.map((m) => `<label><input type="checkbox" data-mail-to="${esc(p.id)}" value="${esc(m.uid)}"${off.has(m.uid) ? "" : " checked"}> ${esc(m.name)}</label>`).join("")}</div>
      <input class="msp-input" data-mail-subject="${esc(p.id)}" maxlength="150" placeholder="Subject (default: Bootcamp group: ${esc(p.name)})" aria-label="Subject" value="${esc(d.subject || "")}">
      <textarea class="msp-input" data-mail-body="${esc(p.id)}" maxlength="5000" rows="4" placeholder="Your message" aria-label="Message">${esc(d.body || "")}</textarea>
      <div class="bc-w7-book"><button class="btn primary" data-pj="mail" data-id="${esc(p.id)}">Send email</button></div>
      ${sent.length ? `<div class="bc-mail-sent">${sent.slice().reverse().map((m) => `<div class="bc-meta">${esc(when(m.at))} · ${esc(m.by_name)}: “${esc(m.subject)}” to ${esc((m.to || []).join(", "))}</div>`).join("")}</div>` : ""}
    </div>`;
  }
  let w7SaveTimer = null;
  const ms = (iso) => Date.parse(iso);

  function w7Counts(p) {
    const counts = new Map();
    for (const [key, names] of Object.entries(p.week7.availability.who || {})) counts.set(ms(key), names);
    return counts;
  }

  function w7Grid(p, mode) {
    const days = window.AlphaAvailability.buildDays(window.AlphaAvailability.GROUP_WINDOW);
    if (!days.length) return '<p class="bc-empty">Week 7 has passed.</p>';
    const counts = w7Counts(p), size = p.week7.availability.size || 1;
    const mine = w7Mine[p.id], picked = w7Pick[p.id];
    let html = '<div class="aval-wrap"><table class="aval-grid"><thead><tr><th class="aval-daylabel">Day</th>';
    days[0].slots.forEach((sl) => { html += `<th>${esc(sl.label)}</th>`; });
    html += "</tr></thead><tbody>";
    for (const day of days) {
      html += `<tr><td class="aval-daylabel">${esc(day.label)}</td>`;
      for (const sl of day.slots) {
        const names = counts.get(sl.ts) || [];
        const n = names.length;
        const heat = n ? `background: rgba(27, 117, 188, ${0.12 + 0.6 * n / size});` : "";
        const cls = [mode === "member" && mine && mine.has(sl.ts) ? "is-selected" : "",
                     n && n === size && size > 1 ? "is-all" : "", picked === sl.ts ? "is-picked" : ""].join(" ");
        const tip = `${day.label}, ${sl.label}${n ? ` · free: ${names.join(", ")}` : ""}`;
        html += `<td><button type="button" class="aval-cell-btn ${cls}" data-w7="${mode}" data-id="${esc(p.id)}" data-ts="${sl.ts}"
          style="${mode === "member" && mine && mine.has(sl.ts) ? "" : heat}" title="${esc(tip)}" aria-label="${esc(tip)}">${n ? n : ""}</button></td>`;
      }
      html += "</tr>";
    }
    return html + "</tbody></table></div>";
  }

  function w7Status(p) {
    const a = p.week7.availability;
    return `${a.filled.length} of ${a.size} have marked their times${a.missing.length ? ` · waiting on ${esc(a.missing.join(", "))}` : ""}`;
  }

  function w7Booked(b) {
    return `<div class="bc-w7-booked"><strong>Check-in booked: ${esc(b.label)}</strong> (30 min) with ${esc(b.analyst_name)}
      ${b.meet_link ? `<br>Google Meet: <a href="${esc(b.meet_link)}" target="_blank" rel="noopener">${esc(b.meet_link)}</a>` : ""}
      ${b.message ? `<br><span class="msp-muted">“${esc(b.message)}”</span>` : ""}</div>`;
  }

  function w7MemberHtml(p) {
    if (!p.week7) return "";
    if (!w7Mine[p.id]) w7Mine[p.id] = new Set((p.week7.mine || []).map(ms));
    const b = p.week7.booked;
    return `<div class="bc-w7"><div class="bc-pj-sub">Week 7 check-in · 22–28 November</div>
      ${b ? w7Booked(b) : `<p class="bc-hint">Click the half hours you're free (London time). The number in each slot is how many
        of your group are free then; an Analyst picks one and the whole group gets an invite. <span id="w7Saved-${esc(p.id)}"></span></p>
        ${w7Grid(p, "member")}<div class="bc-w7-key">${w7Status(p)} · green: you · outlined: everyone free</div>`}</div>`;
  }

  function w7AnalystHtml(p) {
    if (!p.week7) return "";
    const b = p.week7.booked, picked = w7Pick[p.id];
    const me = (pj.analysts || []).some((a) => a.id === pj.me) ? pj.me : "";
    const notes = p.week7.notes || [];
    return `<div class="bc-w7"><div class="bc-pj-sub">Week 7 check-in · 22–28 November</div>
      ${b ? w7Booked(b) : ""}
      <div class="bc-w7-key">${w7Status(p)}. Numbers are how many are free; outlined: the whole group. Click a slot to pick it.</div>
      ${w7Grid(p, "analyst")}
      <div class="bc-w7-book">
        <span class="bc-meta">${picked ? `Picked: ${esc(new Date(picked).toLocaleString("en-GB", { timeZone: "Europe/London", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))}` : "Pick a slot above"}</span>
        <select class="msp-input" id="w7Analyst-${esc(p.id)}" aria-label="Analyst taking the check-in">${(pj.analysts || []).map((a) =>
          `<option value="${esc(a.id)}"${(b ? b.analyst_id : me) === a.id ? " selected" : ""}>${esc(a.name)}</option>`).join("")}</select>
        <button class="btn primary" data-pj="w7-book" data-id="${esc(p.id)}" ${picked && p.members.length ? "" : "disabled"}>${b ? "Move check-in" : "Book check-in"}</button>
        ${b ? `<button class="btn ghost" data-pj="w7-cancel" data-id="${esc(p.id)}">Cancel check-in</button>` : ""}
      </div>
      <div class="bc-notes"><div class="bc-pj-sub">Analyst notes · only Analysts see these</div>
        ${notes.map((n) => `<div class="bc-note">${linkify(n.text)}<span class="bc-meta">${esc(n.by_name)} · ${esc(when(n.at))}
          ${n.by === pj.me ? ` · <a href="#" data-pj="note-del" data-id="${esc(p.id)}" data-note="${esc(n.id)}">delete</a>` : ""}</span></div>`).join("")
          || '<p class="bc-empty" style="margin:4px 0;">No notes yet.</p>'}
        <div class="bc-w7-book"><input class="msp-input" id="w7Note-${esc(p.id)}" maxlength="2000" placeholder="Add a note on this group" aria-label="Add a note">
          <button class="btn ghost" data-pj="note-add" data-id="${esc(p.id)}">Add note</button></div>
      </div></div>`;
  }

  // Analysts: every booked check-in in one place, soonest first.
  function w7SummaryHtml() {
    if (!pj.can_moderate || !pj.projects.length) return "";
    const booked = pj.projects.filter((p) => p.week7 && p.week7.booked).sort((a, b) => a.week7.booked.when.localeCompare(b.week7.booked.when));
    const waiting = pj.projects.filter((p) => p.week7 && !p.week7.booked && p.members.length);
    return `<div class="card" style="padding:14px 18px;margin-bottom:18px;"><h3 style="margin:0 0 6px;">Week 7 check-ins</h3>
      ${booked.length ? `<table class="bc-checkins" style="width:100%;border-collapse:collapse;"><thead><tr><th>When</th><th>Group</th><th>With</th><th>Meet</th></tr></thead><tbody>
        ${booked.map((p) => { const b = p.week7.booked; return `<tr class="${b.analyst_id === pj.me ? "is-mine" : ""}"><td>${esc(b.label)}</td><td>${esc(p.name)}</td>
          <td>${esc(b.analyst_name)}${b.analyst_id === pj.me ? " (you)" : ""}</td><td>${b.meet_link ? `<a href="${esc(b.meet_link)}" target="_blank" rel="noopener">join</a>` : "—"}</td></tr>`; }).join("")}
        </tbody></table>` : '<p class="bc-empty" style="margin:0;">None booked yet.</p>'}
      ${waiting.length ? `<p class="bc-hint" style="margin-top:8px;">Not booked yet: ${waiting.map((p) => esc(p.name)).join(", ")}</p>` : ""}</div>`;
  }

  // Analysts organising the groups: everything the person running a group
  // can do, plus putting people in, moving them between groups, taking them
  // out, and choosing a lead. Groups an Analyst made stay even when empty.
  const groupOptions = (exceptId, placeholder) => `<option value="">${esc(placeholder)}</option>${pj.projects
    .filter((g) => g.id !== exceptId).map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("")}`;

  function managedProjectHtml(p) {
    const edit = pjEditing === p.id;
    const runBy = p.owner ? `run by ${esc(p.owner_name)}` : "run by the Analysts";
    return `<article class="card bc-pj" id="pj-${esc(p.id)}">
      ${edit ? `<div class="bc-form">
          <input class="msp-input" id="pjEditName" maxlength="80" value="${esc(p.name)}" aria-label="Group name">
          <textarea class="msp-input" id="pjEditDesc" maxlength="2000" aria-label="Description">${esc(p.description)}</textarea>
          <div class="bc-actions"><button class="btn primary" data-pj="save" data-id="${esc(p.id)}">Save</button>
            <button class="btn ghost" data-pj="cancel-edit">Cancel</button></div></div>`
        : `<div class="bc-pj-top"><h3>${esc(p.name)}</h3>
            <span class="bc-meta">${p.members.length} member${p.members.length === 1 ? "" : "s"} · ${runBy}${p.managed ? " · made by an Analyst" : ""}</span></div>
          ${p.description ? `<p class="bc-pj-desc">${linkify(p.description)}</p>` : ""}`}
      <div class="bc-pj-code"><strong>${esc(p.code)}</strong>
        <span>Join code. Anyone in the Bootcamp who enters it joins this group straight away.</span>
        <button class="btn ghost" data-pj="copy" data-code="${esc(p.code)}">Copy</button>
        <button class="btn ghost" data-pj="newcode" data-id="${esc(p.id)}" title="The old code stops working">New code</button></div>
      ${(p.requests || []).length ? `<div class="bc-pj-sub">Asking to join</div>${p.requests.map((r) => `
        <div class="bc-pj-req"><div><strong>${esc(r.name)}</strong> · <a href="mailto:${esc(r.email)}">${esc(r.email)}</a></div>
          <button class="btn primary" data-pj="accept" data-id="${esc(p.id)}" data-uid="${esc(r.uid)}">Accept</button>
          <button class="btn ghost" data-pj="decline" data-id="${esc(p.id)}" data-uid="${esc(r.uid)}">Decline</button></div>`).join("")}` : ""}
      <div class="bc-pj-sub">Members</div>
      <div class="bc-pj-members">${p.members.map((m) => `
        <div class="bc-pj-member bc-pj-manage">
          <span>${esc(m.name)}${m.is_owner ? '<span class="bc-pj-lead">lead</span>' : ""}
            ${m.email ? `<br><a href="mailto:${esc(m.email)}">${esc(m.email)}</a>` : ""}</span>
          <span class="bc-pj-tools">
            ${m.is_owner ? "" : `<button class="btn ghost" data-pj="lead" data-id="${esc(p.id)}" data-uid="${esc(m.uid)}">Make lead</button>`}
            ${pj.projects.length > 1 ? `<select class="msp-input bc-pj-select" data-pj-move="${esc(m.uid)}" aria-label="Move ${esc(m.name)} to another group">${groupOptions(p.id, "Move to…")}</select>` : ""}
            <button class="btn ghost" data-pj="unassign" data-id="${esc(p.id)}" data-uid="${esc(m.uid)}" data-name="${esc(m.name)}">Remove</button>
          </span></div>`).join("") || '<p class="bc-empty" style="margin:8px 0;">Nobody yet.</p>'}</div>
      ${(pj.undecided || []).length ? `<select class="msp-input bc-pj-select bc-pj-add" data-pj-add="${esc(p.id)}" aria-label="Add someone to ${esc(p.name)}">
          <option value="">Add someone not in a group…</option>${pj.undecided.map((u) => `<option value="${esc(u.uid)}">${esc(u.name)}</option>`).join("")}</select>` : ""}
      ${submissionHtml(p)}
      ${groupMailHtml(p)}
      ${w7AnalystHtml(p)}
      <div class="bc-pj-actions">
        ${edit ? "" : `<button class="btn ghost" data-pj="edit" data-id="${esc(p.id)}">Edit name and description</button>`}
        <button class="btn ghost" data-pj="remove" data-id="${esc(p.id)}">Remove group</button>
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
          <span class="bc-pj-tools">${m.email ? `<a href="mailto:${esc(m.email)}">${esc(m.email)}</a>` : '<span class="msp-muted">no email on file</span>'}
          ${pj.can_moderate && pj.projects.length ? `<select class="msp-input bc-pj-select" data-pj-move="${esc(m.uid)}" aria-label="Put ${esc(m.name)} in a group">${groupOptions(null, "Assign to…")}</select>` : ""}</span></div>`).join("")}
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
    const createGroup = pj.can_moderate ? `<div class="card bc-form" style="padding:16px 18px;margin-bottom:18px;">
        <h3 style="margin:0;">Create a group</h3>
        <p class="bc-hint">Analysts only. Then add people to it below, or move them across from other groups.</p>
        <input class="msp-input" id="pjGroupName" maxlength="80" placeholder="Group name, e.g. Group 1: Momentum signals" aria-label="Group name">
        <textarea class="msp-input" id="pjGroupDesc" maxlength="2000" placeholder="What the group is working on (optional)" aria-label="Description"></textarea>
        <div class="bc-actions"><button class="btn primary" data-pj="create-group">Create group</button></div></div>` : "";
    $("#projectsView").innerHTML = `${start}${w7SummaryHtml()}${subSummaryHtml()}${createGroup}${mine.map(myProjectHtml).join("")}
      ${others.length ? `<h4 class="evt-section-title" style="font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:22px 0 10px;">${mine.length ? "Other projects" : pj.can_moderate ? "Groups" : "Projects"}</h4>` : ""}
      ${others.map(pj.can_moderate ? managedProjectHtml : otherProjectHtml).join("")}
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
      if (act === "edit") { pjEditing = b.dataset.id; drawProjects(); return; }
      if (act === "cancel-edit") { pjEditing = null; drawProjects(); return; }
      if (act === "unassign" && !b.dataset.sure) {
        b.dataset.sure = "1"; b.textContent = `Remove ${b.dataset.name}?`;
        setTimeout(() => { if (b.isConnected) { delete b.dataset.sure; b.textContent = "Remove"; } }, 4000);
        return;
      }
      if (act === "leave") { pjLeaving = b.dataset.id; drawProjects(); return; }
      if (act === "leave-no") { pjLeaving = null; drawProjects(); return; }
      if (act === "copy") {
        try { await navigator.clipboard.writeText(b.dataset.code); showMsg("Code copied.", true); }
        catch { showMsg(`The code is ${b.dataset.code}`, true); }
        return;
      }
      if (act === "sub-rm" && !b.dataset.sure) {
        b.dataset.sure = "1"; b.textContent = `Take off ${b.dataset.name}?`;
        setTimeout(() => { if (b.isConnected) { delete b.dataset.sure; b.textContent = "✕"; } }, 4000);
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
        pjEditing = null; showMsg("Saved.", true);
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
        await send("DELETE", `/bootcamp/projects/${id}`); showMsg("Removed.", true);
      } else if (act === "w7-book") {
        const d = await send("POST", `/bootcamp/projects/${id}/checkin`, {
          when: new Date(w7Pick[b.dataset.id]).toISOString(), analyst_id: $(`#w7Analyst-${CSS.escape(b.dataset.id)}`).value });
        delete w7Pick[b.dataset.id];
        showMsg(`Booked for ${d.checkin.label}.${d.emailed ? " The group and the Analyst have their invites." : " (The invite email couldn't be sent.)"}`, d.emailed);
      } else if (act === "w7-cancel") {
        if (!b.dataset.sure) { b.dataset.sure = "1"; b.disabled = false; b.textContent = "Sure? The group will be told"; return; }
        await send("DELETE", `/bootcamp/projects/${id}/checkin`); showMsg("Check-in cancelled and the group told.", true);
      } else if (act === "sub-rm") {
        await send("DELETE", `/bootcamp/projects/${id}/submission/files/${encodeURIComponent(b.dataset.file)}`);
        showMsg(`${b.dataset.name} taken off the submission.`, true);
      } else if (act === "sub-note") {
        await send("PUT", `/bootcamp/projects/${id}/submission/note`, { note: subNote[b.dataset.id] !== undefined ? subNote[b.dataset.id]
          : ((pj.projects.find((x) => x.id === b.dataset.id) || {}).submission || {}).note || "" });
        delete subNote[b.dataset.id]; showMsg("Note saved.", true);
      } else if (act === "mail") {
        const d = mailDraft[b.dataset.id] || {}, off = d.off || new Set();
        const p = pj.projects.find((x) => x.id === b.dataset.id);
        const to = p.members.map((m) => m.uid).filter((u) => !off.has(u));
        if (!to.length) { showMsg("Tick at least one member.", false); b.disabled = false; return; }
        if (!(d.body || "").trim()) { showMsg("Write the message first.", false); b.disabled = false; return; }
        const r = await send("POST", `/bootcamp/projects/${id}/message`, { subject: d.subject || "", body: d.body, members: to });
        delete mailDraft[b.dataset.id];
        showMsg(`Sent to ${r.sent_to.join(", ")}${r.cc ? `, with you (${r.cc}) on Cc` : ""}.`, true);
      } else if (act === "note-add") {
        await send("POST", `/bootcamp/projects/${id}/notes`, { text: $(`#w7Note-${CSS.escape(b.dataset.id)}`).value });
      } else if (act === "note-del") {
        e.preventDefault();
        await send("DELETE", `/bootcamp/projects/${id}/notes/${encodeURIComponent(b.dataset.note)}`);
      } else if (act === "create-group") {
        await send("POST", "/bootcamp/projects/groups", { name: $("#pjGroupName").value, description: $("#pjGroupDesc").value });
        showMsg("Group created. Add people to it below.", true);
      } else if (act === "lead") {
        await send("POST", `/bootcamp/projects/${id}/lead`, { uid: b.dataset.uid }); showMsg("Lead changed.", true);
      } else if (act === "unassign") {
        await send("DELETE", `/bootcamp/projects/${id}/members/${encodeURIComponent(b.dataset.uid)}`);
        showMsg(`${b.dataset.name} is out of the group.`, true);
      }
      await loadProjects();
    } catch (err) { showMsg(err.message, false); b.disabled = false; }
  });

  // Week-7 grid clicks: a member toggles their own half hours (saved a
  // moment later); an Analyst picks the slot to book.
  document.addEventListener("click", (e) => {
    const cell = e.target.closest("[data-w7]");
    if (!cell || !pj) return;
    const id = cell.dataset.id, ts = Number(cell.dataset.ts);
    const p = pj.projects.find((x) => x.id === id);
    if (!p) return;
    if (cell.dataset.w7 === "analyst") {
      w7Pick[id] = w7Pick[id] === ts ? undefined : ts;
      drawProjects();
      return;
    }
    const mine = w7Mine[id];
    if (mine.has(ts)) mine.delete(ts); else mine.add(ts);
    // Show the change straight away, counts included, then save.
    const who = p.week7.availability.who, key = Object.keys(who).find((k) => ms(k) === ts) || new Date(ts).toISOString();
    const myName = (p.members.find((m) => m.uid === pj.me) || {}).name || "you";
    who[key] = (who[key] || []).filter((n) => n !== myName).concat(mine.has(ts) ? [myName] : []);
    const filled = p.week7.availability.filled, missing = p.week7.availability.missing;
    if (mine.size && !filled.includes(myName)) { filled.push(myName); missing.splice(missing.indexOf(myName), 1); }
    drawProjects();
    const note = $(`#w7Saved-${CSS.escape(id)}`);
    if (note) note.textContent = "Saving…";
    clearTimeout(w7SaveTimer);
    w7SaveTimer = setTimeout(async () => {
      try {
        await send("PUT", `/bootcamp/projects/${encodeURIComponent(id)}/availability`,
          { slots: [...mine].sort((a, b) => a - b).map((t) => new Date(t).toISOString()) });
        const n = $(`#w7Saved-${CSS.escape(id)}`); if (n) n.textContent = "Saved.";
      } catch (err) { showMsg(err.message, false); }
    }, 600);
  });

  // Analysts' menus: put someone in a group (moving them out of any other),
  // or add someone not yet in one.
  document.addEventListener("change", async (e) => {
    const sel = e.target.closest("[data-pj-move], [data-pj-add]");
    if (!sel || !pj || !sel.value) return;
    const [projectId, uid] = sel.dataset.pjAdd ? [sel.dataset.pjAdd, sel.value] : [sel.value, sel.dataset.pjMove];
    sel.disabled = true;
    try {
      await send("POST", `/bootcamp/projects/${encodeURIComponent(projectId)}/members`, { uid });
      const group = pj.projects.find((g) => g.id === projectId);
      showMsg(`Added to ${group ? group.name : "the group"}.`, true);
      await loadProjects();
    } catch (err) { showMsg(err.message, false); sel.disabled = false; sel.value = ""; }
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
