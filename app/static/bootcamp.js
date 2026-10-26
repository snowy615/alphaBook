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
  }
  load();
})();
