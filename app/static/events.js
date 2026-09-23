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
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    return body;
  };
  const send = (method, url, payload) => api(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });

  const msg = $("#msg");
  function showMsg(text, ok) {
    msg.textContent = text;
    msg.className = "msp-msg msp-msg-" + (ok ? "ok" : "error");
    msg.classList.remove("hidden");
  }

  let data = null;
  let editingId = null;
  const openSignups = new Set();   // events whose sign-up list is expanded

  // ── Header ───────────────────────────────────────────────────────────────
  function renderTopActions() {
    const box = $("#topActions");
    box.innerHTML = data.viewer
      ? `<a href="/" class="btn ghost">Back to Platform</a>
         <a href="/profile" class="btn ghost">Profile</a>
         <span>${esc(data.viewer.username)}</span>`
      : `<a href="/" class="btn ghost">Back to Platform</a>
         <a href="/login" class="btn">Login</a>
         <a href="/signup" class="btn primary">Sign up</a>`;
  }

  // ── One event ────────────────────────────────────────────────────────────
  function attendanceLine(ev) {
    if (ev.attending == null) return "";
    const places = ev.capacity ? ` / ${ev.capacity}` : "";
    const hiddenNote = data.can_manage && !ev.show_attendance ? " (only you can see this)" : "";
    const pending = ev.pending ? ` · ${ev.pending} awaiting approval` : "";
    return `<span>${ev.attending}${places} going${pending}${hiddenNote}</span>`;
  }

  // Quant Outreach: two tickets instead of a plain sign-up. The current one is
  // highlighted, and it's the same sign-up as the application's first step.
  function ticketControl(ev) {
    const id = esc(ev.id);
    const option = (t) => {
      const mine = ev.my_ticket === t.key;
      const full = t.full && !mine;
      const note = t.capacity
        ? `<span class="evt-opt-note">${full ? "Limit reached" : `${t.remaining} of ${t.capacity} places left`}</span>` : "";
      return `<button class="evt-opt${mine ? " is-mine" : ""}" ${full ? "disabled" : ""}
                data-act="ticket" data-id="${id}" data-ticket="${esc(t.key)}">
                <strong>${esc(t.label)}</strong>${note}${mine ? `<span class="evt-opt-note">Your ticket</span>` : ""}</button>`;
    };
    return `<div class="evt-opts">${ev.tickets.map(option).join("")}</div>
      <div class="evt-actions">
        ${ev.my_ticket ? `<button class="btn ghost" data-act="ticket" data-id="${id}" data-ticket="none">Not attending — cancel my sign-up</button>` : ""}
        <span class="evt-hint">Applying to the Quant Bootcamp? This is the same sign-up as the first step of
          <a href="/apply">the application</a> — whichever you do first carries over.</span>
      </div>`;
  }

  function signupControl(ev) {
    if (ev.is_past) return `<span class="evt-tag">Finished</span>`;
    if (!data.viewer) return `<a class="btn" href="/login">Log in to sign up</a>`;
    if (ev.tickets) return ticketControl(ev);
    switch (ev.my_status) {
      case "confirmed":
        return `<span class="evt-tag is-good">You're going</span>
                <button class="btn ghost" data-act="cancel" data-id="${esc(ev.id)}">Cancel sign-up</button>`;
      case "pending":
        return `<span class="evt-tag is-warn">Awaiting approval</span>
                <button class="btn ghost" data-act="cancel" data-id="${esc(ev.id)}">Withdraw request</button>`;
      case "declined":
        return `<span class="evt-tag is-bad">Not approved</span>`;
      default:
        if (ev.full) return `<button class="btn" disabled>Full</button>`;
        return `<button class="btn primary" data-act="signup" data-id="${esc(ev.id)}">
                  ${ev.signup_mode === "approval" ? "Request to join" : "Sign up"}</button>`;
    }
  }

  function manageButtons(ev) {
    const parts = [];
    if (data.can_manage) {
      parts.push(`<button class="btn ghost" data-act="toggle-su" data-id="${esc(ev.id)}">
        ${openSignups.has(ev.id) ? "Hide sign-ups" : "Sign-ups"}</button>`);
    }
    if (data.is_admin) {
      parts.push(`<button class="btn ghost" data-act="edit" data-id="${esc(ev.id)}">Edit</button>`);
      // Quant Outreach is tied to the application form, so it stays.
      if (!ev.tickets) parts.push(`<button class="btn ghost" data-act="delete" data-id="${esc(ev.id)}">Delete</button>`);
    }
    return parts.join("");
  }

  function renderEvent(ev) {
    const mode = ev.tickets ? "Choose a ticket"
      : ev.signup_mode === "approval" ? "Approval needed" : "First come, first served";
    return `
      <div class="card msp-panel evt-card ${ev.is_past ? "is-past" : ""}" id="ev-${esc(ev.id)}">
        <div class="msp-panel-body">
          <div class="evt-top">
            <div>
              <h3 class="evt-title">${esc(ev.title)}</h3>
              <div class="evt-when">${esc(ev.when_label)}</div>
              ${ev.location ? `<div class="evt-loc">${esc(ev.location)}</div>` : ""}
            </div>
            <div class="evt-actions" style="margin-top:0;">${manageButtons(ev)}</div>
          </div>
          ${ev.description ? `<p class="evt-desc">${esc(ev.description)}</p>` : ""}
          <div class="evt-meta">
            ${attendanceLine(ev)}
            <span>${mode}</span>
          </div>
          ${ev.tickets && data.viewer && !ev.is_past
            ? signupControl(ev)
            : `<div class="evt-actions">${signupControl(ev)}</div>`}
          <div class="evt-su ${openSignups.has(ev.id) ? "" : "hidden"}" id="su-${esc(ev.id)}"></div>
        </div>
      </div>`;
  }

  function renderList() {
    const upcoming = data.events.filter((e) => !e.is_past);
    const past = data.events.filter((e) => e.is_past);
    let html = "";
    if (!data.events.length) {
      html = `<p class="evt-empty">No events yet — check back soon.</p>`;
    } else {
      html += upcoming.length
        ? upcoming.map(renderEvent).join("")
        : `<p class="evt-empty">Nothing coming up right now.</p>`;
      if (past.length) {
        html += `<div class="evt-section-title">Past events</div>` + past.map(renderEvent).join("");
      }
    }
    $("#list").innerHTML = html;
    openSignups.forEach((id) => { if ($("#su-" + id)) loadSignups(id); });
  }

  // ── Sign-up list (admins and analysts) ───────────────────────────────────
  const STATUS_TAG = {
    confirmed: `<span class="evt-tag is-good">Going</span>`,
    pending: `<span class="evt-tag is-warn">Pending</span>`,
    declined: `<span class="evt-tag is-bad">Declined</span>`,
  };

  async function loadSignups(id) {
    const box = $("#su-" + id);
    if (!box) return;
    // Outreach places aren't approved — people pick their own ticket — so
    // there's nothing to decide there, just who's coming and on what.
    const ticketed = !!(data.events.find((x) => x.id === id) || {}).tickets;
    try {
      const { signups } = await api(`/events/${encodeURIComponent(id)}/signups`);
      box.innerHTML = signups.length ? signups.map((s) => `
        <div class="evt-su-row">
          <div class="evt-su-who">
            <strong>${esc(s.name)}</strong>
            <span>${esc(s.email || s.username)}${s.decided_by ? ` · decided by ${esc(s.decided_by)}` : ""}</span>
          </div>
          <div class="evt-actions" style="margin-top:0;">
            ${s.ticket ? `<span class="evt-tag">${esc(s.ticket)}</span>` : (STATUS_TAG[s.status] || "")}
            ${ticketed ? "" : s.status !== "confirmed" ? `<button class="btn primary" data-act="decide" data-id="${esc(id)}" data-uid="${esc(s.user_id)}" data-decision="approve">Approve</button>` : ""}
            ${ticketed ? "" : s.status !== "declined" ? `<button class="btn ghost" data-act="decide" data-id="${esc(id)}" data-uid="${esc(s.user_id)}" data-decision="decline">${s.status === "confirmed" ? "Remove" : "Decline"}</button>` : ""}
          </div>
        </div>`).join("") : `<p class="evt-empty">Nobody has signed up yet.</p>`;
    } catch (err) {
      box.innerHTML = `<p class="evt-empty">${esc(err.message)}</p>`;
    }
  }

  // ── Admin form ───────────────────────────────────────────────────────────
  const F = {
    title: $("#evTitle"), desc: $("#evDesc"), location: $("#evLocation"), date: $("#evDate"),
    start: $("#evStart"), end: $("#evEnd"), cap: $("#evCap"), mode: $("#evMode"), show: $("#evShow"),
  };

  function resetForm() {
    editingId = null;
    F.title.value = F.desc.value = F.location.value = F.date.value = F.start.value = F.end.value = F.cap.value = "";
    F.mode.value = "first_come";
    F.show.checked = false;
    $("#formTitle").textContent = "New event";
    $("#evSave").textContent = "Create event";
    $("#evCancel").classList.add("hidden");
    $("#evTicketFields").classList.remove("hidden");
    $("#evTicketNote").classList.add("hidden");
  }

  function fillForm(ev) {
    editingId = ev.id;
    F.title.value = ev.title;
    F.desc.value = ev.description;
    F.location.value = ev.location || "";
    F.date.value = ev.date;
    F.start.value = ev.start_time;
    F.end.value = ev.end_time;
    // Reviewers-only fields come back null when hidden, but an admin editing
    // always sees them (they're a reviewer too), so this is the real value.
    F.cap.value = ev.capacity == null ? "" : ev.capacity;
    F.mode.value = ev.signup_mode;
    F.show.checked = ev.show_attendance;
    $("#formTitle").textContent = "Edit event";
    $("#evSave").textContent = "Save changes";
    // Places and sign-up for Quant Outreach are the ticket system's.
    $("#evTicketFields").classList.toggle("hidden", !!ev.tickets);
    $("#evTicketNote").classList.toggle("hidden", !ev.tickets);
    $("#evCancel").classList.remove("hidden");
    $("#formCard").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  $("#evCancel").addEventListener("click", resetForm);
  $("#evSave").addEventListener("click", async () => {
    const payload = {
      title: F.title.value,
      description: F.desc.value,
      location: F.location.value,
      date: F.date.value,
      start_time: F.start.value,
      end_time: F.end.value,
      capacity: F.cap.value === "" ? null : Number(F.cap.value),
      show_attendance: F.show.checked,
      signup_mode: F.mode.value,
    };
    const btn = $("#evSave");
    btn.disabled = true;
    try {
      if (editingId) await send("PUT", `/events/${encodeURIComponent(editingId)}`, payload);
      else await send("POST", "/events/create", payload);
      showMsg(editingId ? "Event updated." : "Event created.", true);
      resetForm();
      await load();
    } catch (err) {
      showMsg(err.message, false);
    } finally {
      btn.disabled = false;
    }
  });

  // ── Actions on cards ─────────────────────────────────────────────────────
  $("#list").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const url = `/events/${encodeURIComponent(id)}`;
    try {
      switch (btn.dataset.act) {
        case "signup": {
          const r = await send("POST", url + "/signup");
          showMsg(r.status === "pending" ? "Request sent — you'll be told here once it's approved." : "You're signed up.", true);
          await load();
          break;
        }
        case "ticket": {
          const t = btn.dataset.ticket;
          await send("POST", url + "/ticket", { ticket: t });
          showMsg(t === "none" ? "Sign-up cancelled."
            : t === "fast_track" ? "You're signed up for the CV clinic + Fast-Track." : "You're signed up — general attendance.", true);
          await load();
          break;
        }
        case "cancel":
          await send("DELETE", url + "/signup");
          showMsg("Cancelled.", true);
          await load();
          break;
        case "toggle-su":
          if (openSignups.has(id)) openSignups.delete(id); else openSignups.add(id);
          renderList();
          break;
        case "decide":
          await send("POST", `${url}/signups/${encodeURIComponent(btn.dataset.uid)}/decision`,
            { decision: btn.dataset.decision });
          await load();
          break;
        case "edit":
          fillForm(data.events.find((x) => x.id === id));
          break;
        case "delete":
          if (!confirm("Delete this event and all its sign-ups? This can't be undone.")) return;
          await send("DELETE", url);
          showMsg("Event deleted.", true);
          await load();
          break;
      }
    } catch (err) {
      showMsg(err.message, false);
      await load();   // the list may have moved on (event filled up, etc.)
    }
  });

  // ── Load ─────────────────────────────────────────────────────────────────
  async function load() {
    data = await api("/events/list");
    renderTopActions();
    $("#formCard").classList.toggle("hidden", !data.is_admin);
    renderList();
  }

  load().catch((err) => {
    $("#list").innerHTML = `<p class="evt-empty">${esc(err.message)}</p>`;
  });
})();
