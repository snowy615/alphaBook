/* Apply to a quant programme: choose, upload a CV, sit the assessment.
 *
 * The clocks drawn here are cosmetic. The server stamps when the sitting
 * started, when the written section started, and when each question was
 * served, and it is the only clock that decides anything. This file polls
 * the state endpoint once a second to stay in step (and to pick up an
 * expiry the candidate's own client missed — a closed tab, a stalled
 * request), and runs a local 250ms ticker so the numbers on screen count
 * down smoothly between polls.
 *
 * Two rules shape the rendering: never redraw a section while the candidate
 * is typing into it (only the clock text is refreshed in place), and always
 * fire one auto-submit of whatever is currently typed when the local clock
 * reaches zero — the server decides whether that arrived in time.
 */
(function () {
  "use strict";

  const CFG = window.APPLY || {};
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  const PROGRAMME_BLURB = {
    "Fundamental Bootcamp":
      "The taught track for fundamental investing: company analysis, valuation and " +
      "stock-picking, with the games on this site as the practical half.",
    "Quant Bootcamp":
      "The taught track: a term of sessions on probability, market making and " +
      "systematic trading, with the games on this site as the practical half.",
    "Fundamental Analyst":
      "The research track for fundamental investing: you pitch and defend your own " +
      "ideas, and go into the CV book firms read.",
    "Quant Analyst":
      "The research track: you run your own ideas, contribute to the fund's " +
      "quant work, and go into the CV book firms read.",
  };

  function mmss(seconds) {
    const s = Math.max(0, Math.ceil(seconds));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  async function api(url, body, method) {
    const opts = { credentials: "include" };
    if (body !== undefined) {
      opts.method = method || "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(url, opts);
    const txt = await r.text();
    let data = {};
    try { data = txt ? JSON.parse(txt) : {}; } catch { /* non-JSON */ }
    if (!r.ok) {
      const e = new Error(data.detail || `HTTP ${r.status}`);
      e.status = r.status;
      throw e;
    }
    return data;
  }

  function flash(text, bad) {
    const box = $("#msg");
    if (!box) return;
    box.textContent = text;
    box.className = "msp-msg msp-msg-" + (bad ? "error" : "ok");
    box.classList.remove("hidden");
    clearTimeout(flash.t);
    flash.t = setTimeout(() => box.classList.add("hidden"), 5000);
  }

  // ── State ──────────────────────────────────────────────────────────────────
  let pollTimer = null;
  let tickTimer = null;
  let drawnKey = "";          // what the DOM currently shows
  let sessionLeft = 0;        // local mirror of the session clock
  let sectionLeft = 0;        // local mirror of the motivation / estimation clock
  let autoFiredKey = "";      // guards against firing the same auto-submit twice
  let busy = false;
  let draftTimer = null;
  let pickedProgramme = null;
  // True from the moment "Apply again" is clicked until a fresh /apply/start
  // actually succeeds. Nothing server-side has changed in between, so the
  // background poll would otherwise redraw straight back to the old decision
  // screen the instant it landed — this tells render() to leave it alone.
  let reapplying = false;

  // ── Progress rail ──────────────────────────────────────────────────────────
  const STEP_ORDER = ["choose", "cv", "oa", "done"];
  const STEP_LABEL = { choose: "Choose a programme", cv: "Event & CV", oa: "Assessment", done: "Submitted" };

  function stepFor(state) {
    if (state.status === "none") return "choose";
    if (state.status === "cv") return "cv";
    if (state.status === "oa_ready" || state.status === "oa_active") return "oa";
    return "done";
  }

  function renderSteps(state) {
    const now = stepFor(state);
    const at = STEP_ORDER.indexOf(now);
    $("#steps").innerHTML = `<ol class="apl-steps">${STEP_ORDER.map((k, i) => {
      const cls = i < at ? "is-done" : (i === at ? "is-now" : "");
      return `<li class="${cls}"><span class="apl-n">${i < at ? "✓" : i + 1}</span>${STEP_LABEL[k]}</li>`;
    }).join("")}</ol>`;
  }

  // ── Screens ────────────────────────────────────────────────────────────────

  function panel(title, body, meta) {
    return `
      <div class="card msp-panel">
        <div class="head">
          <h3>${title}</h3>
          ${meta ? `<span class="msp-muted">${meta}</span>` : ""}
        </div>
        <div class="msp-panel-body">${body}</div>
      </div>`;
  }

  function renderIneligible(state) {
    // An admin visiting /apply has nothing to submit either, but they do
    // have somewhere useful to go — same message a Quant Analyst gets.
    if (state.is_reviewer) { renderAnalyst(state); return; }
    $("#app").innerHTML = panel("Applications are for general accounts", `
      <p class="msp-muted" style="margin-top:0;">
        Your account is set to <strong>${esc(state.membership)}</strong>, so there is
        nothing here to apply for. If that is wrong, change it on your
        <a href="/profile">profile</a> or ask an admin.
      </p>`);
  }

  // Quant Analyst is the ceiling — nothing left to apply for. The apply page
  // retires itself and points straight at the review page instead of ever
  // showing a stale "accepted" screen.
  function renderAnalyst() {
    $("#app").innerHTML = panel("You're a Quant Analyst", `
      <p class="msp-muted" style="margin-top:0;">
        There's nothing left here to apply for — you're already at the top of
        the programme. Help review this round's applicants instead.
      </p>
      <a class="btn primary" href="/apply/admin">Open the review page →</a>`);
  }

  // General public applicants have nothing else on the account vouching for
  // them being a current Oxford student, so they confirm it directly. A
  // General Alpha Fund member has already cleared that bar to get their
  // membership, so there's nothing to re-ask.
  const GENERAL_PUBLIC = "General public";

  function renderChoose(state) {
    cvScreen = null;   // a fresh application starts the CV step with a clean decision
    const disabled = new Set(state.disabled_programmes || []);
    const options = (state.programmes || []).map((p) => {
      const isDisabled = disabled.has(p);
      return `
      <label class="apl-choice${isDisabled ? " is-disabled" : ""}" data-programme="${esc(p)}">
        <input type="radio" name="programme" value="${esc(p)}" ${isDisabled ? "disabled" : ""}>
        <strong>${esc(p)}</strong>
        <span class="apl-blurb">${esc(PROGRAMME_BLURB[p] || "")}</span>
        ${isDisabled ? '<span class="apl-soon">Not available right now — will become available next term.</span>' : ""}
      </label>`;
    }).join("");

    const isGeneralPublic = state.membership === GENERAL_PUBLIC;
    const categoryBanner = `
      <div class="apl-category">
        Applying as <strong>${esc(state.membership || GENERAL_PUBLIC)}</strong>
        ${isGeneralPublic
          ? "— open to anyone currently studying at the University of Oxford."
          : "— you're already an Alpha Fund member, so there's nothing extra to confirm here."}
      </div>`;

    // An account that signed up with an Oxford address already has one on
    // file; everyone else has to name one, since that is the only address
    // the committee will send updates and a decision to.
    const oxfordField = state.needs_oxford_email ? `
      <div class="field-group" style="margin-top:16px;">
        <label>Your Oxford email</label>
        <input type="email" id="oxfordEmail" placeholder="you@some-college.ox.ac.uk"
               value="${esc(state.oxford_email || "")}">
        <p class="apl-hint" id="oxfordHint">
          Your account is signed up with ${state.account_email ? esc(state.account_email) : "a non-Oxford address"}.
          Updates about this application — and the decision — go to your Oxford email instead.
        </p>
      </div>` : "";

    // Only General public applicants need this — a member's eligibility was
    // already established when their membership was granted.
    const studentConfirm = isGeneralPublic ? `
      <label class="apl-ack" style="margin-top:16px;">
        <input type="checkbox" id="confirmOxfordStudent">
        <span>I confirm I am currently studying at the University of Oxford. Applications
          from anyone else will not be considered eligible.</span>
      </label>` : "";

    $("#app").innerHTML = panel("Apply to Alpha Fund", `
      ${categoryBanner}
      <p class="msp-muted" style="margin-top:0;">
        Pick the programme you want. Next, register for the outreach event if you'd like to,
        then put an up-to-date CV on your profile and sit a ${CFG.sessionMinutes}-minute
        assessment — you can start it whenever suits you, but once it starts it runs to the
        end in one sitting. Want both Fundamental and Quant? Apply to them separately, one
        at a time — there's no combined option, but once this one is decided you can come
        back and apply for the other.
      </p>
      ${options}
      ${oxfordField}
      ${studentConfirm}
      <button class="btn primary" id="applyBtn" style="margin-top:16px;" disabled>Continue</button>`);

    function refreshApplyBtn() {
      const okStudent = !isGeneralPublic || $("#confirmOxfordStudent").checked;
      $("#applyBtn").disabled = !pickedProgramme || !okStudent;
    }

    $("#app").querySelectorAll(".apl-choice").forEach((el) => {
      if (el.classList.contains("is-disabled")) return;
      el.addEventListener("click", () => {
        pickedProgramme = el.dataset.programme;
        $("#app").querySelectorAll(".apl-choice").forEach((o) => o.classList.remove("is-picked"));
        el.classList.add("is-picked");
        refreshApplyBtn();
      });
    });
    if (isGeneralPublic) {
      $("#confirmOxfordStudent").addEventListener("change", refreshApplyBtn);
    }

    $("#applyBtn").addEventListener("click", async (e) => {
      if (!pickedProgramme) return;
      const payload = { programme: pickedProgramme };
      if (isGeneralPublic) {
        payload.confirms_oxford_student = !!$("#confirmOxfordStudent").checked;
        if (!payload.confirms_oxford_student) {
          flash("Confirm you're currently studying at Oxford to continue.", true);
          return;
        }
      }
      const oxInput = $("#oxfordEmail");
      if (oxInput) {
        const value = oxInput.value.trim();
        if (!/^[^\s@]+@[^\s@]+\.ox\.ac\.uk$|^[^\s@]+@ox\.ac\.uk$/i.test(value)) {
          flash("Enter a valid Oxford email address (ending in ox.ac.uk).", true);
          oxInput.focus();
          return;
        }
        payload.oxford_email = value;
      }
      e.target.disabled = true;
      try {
        await api("/apply/start", payload);
        reapplying = false;   // server state has actually moved now
        await refresh();
      } catch (err) {
        flash(err.message, true);
        e.target.disabled = false;
      }
    });
  }

  // ── Outreach event registration ───────────────────────────────────────────
  // The first thing shown once a programme is picked, before the CV step —
  // General Attendance, Fast-Track CV Clinic (capped, first-come), or skip
  // straight to the online application. Once chosen it's locked in server
  // side (see /apply/event-ticket), so this screen only ever shows once.
  let pickedTicket = null;

  function renderEventChoice(state) {
    pickedTicket = null;
    const fastFull = !!state.fast_track_full;
    const capacity = state.fast_track_capacity || 50;
    const fastBlurb = fastFull
      ? "Limit reached — no longer available."
      : `Have your CV reviewed in person by an analyst at the event, and skip the written ` +
        `assessment entirely — you'll go straight into the same CV-scoring and interview process ` +
        `as everyone else. ${state.fast_track_remaining} of ${capacity} places left.`;

    $("#app").innerHTML = panel("OAF Quant Bootcamp — Outreach Event", `
      <p class="msp-muted" style="margin-top:0;">
        Applying for <strong>${esc(state.programme || "")}</strong>. Before your CV, let us know
        whether you'd like to come to the outreach event.
      </p>
      <label class="apl-choice${fastFull ? " is-disabled" : ""}" data-ticket="fast_track">
        <input type="radio" name="eventTicket" value="fast_track" ${fastFull ? "disabled" : ""}>
        <strong>CV Review + Fast-Track Interview</strong>
        <span class="apl-blurb">${esc(fastBlurb)}</span>
        ${fastFull ? '<span class="apl-soon">Limit reached — no longer available.</span>' : ""}
      </label>
      <label class="apl-choice" data-ticket="general">
        <input type="radio" name="eventTicket" value="general">
        <strong>Talk &amp; Networking Only</strong>
        <span class="apl-blurb">Come to the presentation and meet the team, then apply online in the
          usual way afterwards.</span>
      </label>
      <button class="btn primary" id="eventNext" style="margin-top:16px;" disabled>Continue</button>
      <p style="margin-top:14px;">
        <button type="button" class="btn ghost" id="eventSkip">Not attending — continue to the online application</button>
      </p>`);

    $("#app").querySelectorAll(".apl-choice").forEach((el) => {
      if (el.classList.contains("is-disabled")) return;
      el.addEventListener("click", () => {
        pickedTicket = el.dataset.ticket;
        $("#app").querySelectorAll(".apl-choice").forEach((o) => o.classList.remove("is-picked"));
        el.classList.add("is-picked");
        $("#eventNext").disabled = false;
      });
    });

    async function submitTicket(ticket, btn) {
      btn.disabled = true;
      try {
        await api("/apply/event-ticket", { ticket });
        await refresh();
      } catch (err) {
        flash(err.message, true);
        btn.disabled = false;
        if (ticket === "fast_track") await refresh();   // capacity may have just filled — re-check
      }
    }
    $("#eventNext").addEventListener("click", (e) => { if (pickedTicket) submitTicket(pickedTicket, e.target); });
    $("#eventSkip").addEventListener("click", (e) => submitTicket("none", e.target));
  }

  // Which CV screen is showing, independent of whether a CV happens to be
  // on file at this exact instant — that distinction matters because a
  // first-time upload flips cv_uploaded to true mid-flow, and without a
  // separate flag the next redraw would mistake "just uploaded" for
  // "already had one" and ask a question that was just answered by uploading.
  //   null    — not yet decided; renderCv derives it from whether a CV exists
  //   "first" — no CV existed when this screen opened; show Continue, no Back
  //   "replace" — a CV already existed; uploading auto-confirms, Back cancels
  //   "info"  — CV in hand; collecting college/degree/year/LinkedIn before cv-confirm
  let cvScreen = null;

  function renderCv(state) {
    const has = state.cv_uploaded;
    if (cvScreen === "info") { renderCvInfo(state); return; }
    if (cvScreen === "first" || cvScreen === "replace") { renderCvUpload(state, cvScreen); return; }
    if (has) { renderCvAsk(state); return; }
    cvScreen = "first";
    renderCvUpload(state, "first");
  }

  // A CV is already on file: ask outright rather than leaving it to a
  // "Replace" button someone might miss and upload over by accident.
  function renderCvAsk(state) {
    $("#app").innerHTML = panel("Your CV is on file", `
      <p class="msp-muted" style="margin-top:0;">
        Applying for <strong>${esc(state.programme || "")}</strong>. You already have a CV
        on your AlphaBook profile — this is the copy the committee would read.
      </p>
      <div class="cv-status-banner uploaded"
           style="display:flex;align-items:center;gap:12px;padding:14px 18px;margin-bottom:16px;font-size:14px;font-weight:600;
                  border:1px solid var(--green);color:var(--green);">
        <span>✓</span><span>A CV is on file</span>
      </div>
      <div id="cvPreviewArea"></div>
      <p style="font-size:15px;font-weight:600;margin:20px 0 12px;">Would you like to update it?</p>
      <div class="btn-row" style="display:flex;gap:10px;flex-wrap:wrap;">
        <button class="btn primary" id="cvUpdateYes">Yes, upload a new one</button>
        <button class="btn" id="cvUpdateNo">No, use this one</button>
      </div>`);

    wireCvPreview($("#cvPreviewArea"));

    $("#cvUpdateYes").addEventListener("click", () => { cvScreen = "replace"; renderCvUpload(state, "replace"); });
    $("#cvUpdateNo").addEventListener("click", () => { cvScreen = "info"; renderCvInfo(state); });
  }

  // A small "View CV" toggle that renders the PDF inline, so reading it
  // never means leaving the apply flow — a plain link to open it in a new
  // tab is offered alongside for whoever would rather have that.
  function wireCvPreview(container) {
    container.innerHTML = `
      <div class="btn-row" style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:4px;">
        <button class="btn ghost" id="cvToggle">View CV</button>
        <a class="btn ghost" href="/me/cv" target="_blank" rel="noopener">Open in new tab ↗</a>
      </div>
      <div id="cvFrameWrap" style="display:none;margin-top:12px;border:1px solid var(--border);">
        <iframe id="cvFrame" src="" style="width:100%;height:520px;border:none;display:block;"></iframe>
      </div>`;
    const toggle = container.querySelector("#cvToggle");
    const wrap = container.querySelector("#cvFrameWrap");
    const frame = container.querySelector("#cvFrame");
    toggle.addEventListener("click", () => {
      const showing = wrap.style.display !== "none";
      if (showing) {
        wrap.style.display = "none"; frame.src = ""; toggle.textContent = "View CV";
      } else {
        frame.src = "/me/cv?t=" + Date.now();
        wrap.style.display = "block"; toggle.textContent = "Hide CV";
      }
    });
  }

  function renderCvUpload(state, screen) {
    const isReplace = screen === "replace";
    $("#app").innerHTML = panel(isReplace ? "Upload a new CV" : "Upload your CV", `
      <p class="msp-muted" style="margin-top:0;">
        Applying for <strong>${esc(state.programme || "")}</strong>. Put your most
        up-to-date CV on your profile — this is the copy the committee reads, and
        it is the same file that appears everywhere else on your profile.
      </p>
      <div class="cv-status-banner ${state.cv_uploaded ? "uploaded" : "missing"}"
           style="display:flex;align-items:center;gap:12px;padding:14px 18px;margin-bottom:16px;font-size:14px;font-weight:600;
                  border:1px solid ${state.cv_uploaded ? "var(--green)" : "var(--border)"};color:${state.cv_uploaded ? "var(--green)" : "var(--muted)"};">
        <span>${state.cv_uploaded ? "✓" : "✗"}</span>
        <span id="cvText">${state.cv_uploaded ? "A CV is on file" : "No CV uploaded yet"}</span>
      </div>
      <input type="file" id="cvFile" accept="application/pdf,.pdf" style="display:none">
      <div class="btn-row" style="display:flex;gap:10px;flex-wrap:wrap;">
        <button class="btn" id="pickBtn">${isReplace ? "Choose a replacement PDF" : "Choose PDF"}</button>
        ${isReplace ? '<button class="btn ghost" id="cvBack">Back</button>' : ""}
      </div>
      <p class="apl-hint">PDF only · max 10 MB</p>
      ${isReplace ? "" : `
      <hr class="divider" style="border:none;border-top:1px solid var(--border);margin:22px 0;">
      <button class="btn primary" id="cvNext" ${state.cv_uploaded ? "" : "disabled"}>
        This is my up-to-date CV — continue
      </button>`}`);

    $("#pickBtn").addEventListener("click", () => $("#cvFile").click());
    $("#cvBack")?.addEventListener("click", () => { cvScreen = null; renderCv(state); });
    $("#cvFile").addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      if (!file.name.toLowerCase().endsWith(".pdf")) { flash("Only PDF files are accepted", true); return; }
      if (file.size > 10 * 1024 * 1024) { flash("That file is over the 10 MB limit", true); return; }

      $("#pickBtn").disabled = true;
      $("#cvText").textContent = "Uploading…";
      const form = new FormData();
      form.append("file", file);
      try {
        const r = await fetch("/me/cv", { method: "POST", credentials: "include", body: form });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          throw new Error(d.detail || "Upload failed");
        }
        if (isReplace) {
          // They already answered "yes, update it" — the upload itself was
          // the confirmation, so this moves straight to the info step.
          flash("CV updated", false);
          cvScreen = "info";
          renderCvInfo(state);
        } else {
          flash("CV uploaded", false);
          await refresh();   // redraws this same screen with Continue enabled
        }
      } catch (err) {
        flash(err.message, true);
        $("#pickBtn").disabled = false;
        $("#cvText").textContent = "Upload failed";
      }
    });

    $("#cvNext")?.addEventListener("click", () => { cvScreen = "info"; renderCvInfo(state); });
  }

  const OXFORD_COLLEGES = [
    "All Souls", "Balliol", "Blackfriars", "Brasenose", "Campion Hall", "Christ Church",
    "Corpus Christi", "Exeter", "Green Templeton", "Harris Manchester", "Hertford", "Jesus",
    "Keble", "Kellogg", "Lady Margaret Hall", "Linacre", "Lincoln", "Magdalen", "Mansfield",
    "Merton", "New College", "Nuffield", "Oriel", "Pembroke", "Queen's", "Regent's Park",
    "Reuben", "St Anne's", "St Antony's", "St Catherine's", "St Cross", "St Edmund Hall",
    "St Hilda's", "St Hugh's", "St John's", "St Peter's", "St Stephen's House", "Somerville",
    "Trinity", "University College", "Wadham", "Wolfson", "Worcester", "Wycliffe Hall",
  ];

  // The last stop before cv-confirm, whichever ticket they picked — Fast-
  // Track applicants need this exactly as much as anyone else (they're just
  // skipping the written assessment afterward, not this).
  function renderCvInfo(state) {
    const years = state.year_of_study_options || [];
    // A native <select> beats an <input list>+<datalist> combo here — that
    // combo's "dropdown" is really just autocomplete-while-typing, and
    // several browsers (Safari in particular) never show a clickable list
    // for it at all, which is what made the college field look broken.
    const isOtherCollege = !!state.college && !OXFORD_COLLEGES.includes(state.college);
    $("#app").innerHTML = panel("A few details", `
      <p class="msp-muted" style="margin-top:0;">
        Applying for <strong>${esc(state.programme || "")}</strong>.
      </p>
      <div class="field-group">
        <label>College</label>
        <select id="infoCollege">
          <option value="">Choose one</option>
          ${OXFORD_COLLEGES.map((c) => `<option value="${esc(c)}" ${state.college === c ? "selected" : ""}>${esc(c)}</option>`).join("")}
          <option value="__other__" ${isOtherCollege ? "selected" : ""}>Other (not listed)</option>
        </select>
        <input type="text" id="infoCollegeOther" placeholder="Enter your college" style="margin-top:8px;${isOtherCollege ? "" : "display:none;"}"
               value="${esc(isOtherCollege ? state.college : "")}">
      </div>
      <div class="field-group" style="margin-top:14px;">
        <label>Degree</label>
        <input type="text" id="infoDegree" value="${esc(state.degree || "")}" placeholder="e.g. Computer Science">
      </div>
      <div class="field-group" style="margin-top:14px;">
        <label>Year of study</label>
        <select id="infoYear">
          <option value="">Choose one</option>
          ${years.map((y) => `<option value="${esc(y)}" ${state.year_of_study === y ? "selected" : ""}>${esc(y)}</option>`).join("")}
        </select>
      </div>
      <div class="field-group" style="margin-top:14px;">
        <label>LinkedIn / GitHub (optional)</label>
        <input type="text" id="infoLinkedin" value="${esc(state.linkedin || "")}" placeholder="Link to either or both">
      </div>
      <div class="btn-row" style="display:flex;gap:10px;flex-wrap:wrap;margin-top:20px;">
        <button class="btn primary" id="infoNext">Continue</button>
        <button class="btn ghost" id="infoBack">Back</button>
      </div>`);

    const collegeSelect = $("#infoCollege");
    const collegeOther = $("#infoCollegeOther");
    collegeSelect.addEventListener("change", () => {
      const other = collegeSelect.value === "__other__";
      collegeOther.style.display = other ? "block" : "none";
      if (other) collegeOther.focus();
    });

    $("#infoBack").addEventListener("click", () => { cvScreen = null; renderCv(state); });
    $("#infoNext").addEventListener("click", async (e) => {
      const college = collegeSelect.value === "__other__" ? collegeOther.value.trim() : collegeSelect.value;
      const degree = $("#infoDegree").value.trim();
      const year_of_study = $("#infoYear").value;
      if (!college || !degree || !year_of_study) {
        flash("Fill in your college, degree and year of study to continue.", true);
        return;
      }
      e.target.disabled = true;
      try {
        await api("/apply/cv-confirm", { college, degree, year_of_study, linkedin: $("#infoLinkedin").value.trim() });
        cvScreen = null;
        await refresh();
      } catch (err) {
        flash(err.message, true);
        e.target.disabled = false;
      }
    });
  }

  function renderOaGate(state) {
    const r = state.rules || {};
    $("#app").innerHTML = panel("Your assessment is ready", `
      <p class="msp-muted" style="margin-top:0;">
        Applying for <strong>${esc(state.programme || "")}</strong>. Start this whenever
        you like — there is no window and no deadline on the button. But once you
        press it the clock runs to the end whether the tab is open or not, so sit
        down somewhere quiet with ${CFG.sessionMinutes} clear minutes first.
      </p>

      <ul class="apl-rules">
        <li><strong>${Math.round((r.session_seconds || CFG.sessionMinutes * 60) / 60)} minutes in total</strong>, in one sitting. One attempt, two questions.</li>
        <li><strong>Part one — ${Math.round((r.motivation_seconds || CFG.motivationMinutes * 60) / 60)} minutes.</strong>
            A behavioural question — something you've pursued seriously, and what it taught you.</li>
        <li><strong>Part two — ${Math.round((r.estimation_seconds || CFG.estimationMinutes * 60) / 60)} minutes.</strong>
            An estimation question. Show your reasoning — break the problem into smaller
            pieces you can actually estimate, and combine them.</li>
        <li>Both questions take plain text, and LaTeX if you want to show a formula —
            each has a Preview button to check it renders the way you mean.</li>
        <li>Submitting part one early moves straight to part two; it does not bank the
            leftover time. The clock does not pause, and you cannot go back.</li>
      </ul>

      <div class="apl-warn">
        <strong>No AI, and no outside help.</strong>
        You may not use ChatGPT, Claude, Copilot or any other AI tool, and you may not
        search the web or ask anyone else. We are interested in how you think, not what
        a model outputs. Pasting into either answer is disabled, and leaving the
        page is recorded and shown to the reviewer.
      </div>

      <label class="apl-ack" for="ack">
        <input type="checkbox" id="ack">
        <span>I will complete this on my own, in one sitting, without AI or any other help.</span>
      </label>

      <button class="btn primary" id="startOa" disabled>Start the assessment</button>`);

    $("#ack").addEventListener("change", (e) => { $("#startOa").disabled = !e.target.checked; });
    $("#startOa").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { await api("/apply/oa/start", {}); await refresh(); }
      catch (err) { flash(err.message, true); e.target.disabled = false; }
    });
  }

  // Where the application stands after submission, matching the language
  // used elsewhere ("committee is reviewing", "shortlisted", "decision").
  // Absorbed into one pipeline rather than the apply-flow stepper above,
  // since this tracks what the committee is doing, not what the applicant
  // did to get here.
  function renderReviewSteps(state) {
    const status = state.status;
    const decided = status === "accepted" || status === "rejected";
    const wasShortlisted = status === "shortlisted" || !!state.was_shortlisted;

    const items = [
      { label: "Committee is reviewing", done: status !== "submitted", now: status === "submitted" },
      { label: "Shortlisted for interview", done: wasShortlisted && decided,
        now: status === "shortlisted", skipped: decided && !wasShortlisted },
      { label: decided ? (status === "accepted" ? "Accepted" : "Not taken forward") : "Decision",
        done: decided, now: false },
    ];

    return `<ol class="apl-steps" style="margin-bottom:20px;">${items.map((it, i) => {
      const cls = it.skipped ? "is-skipped" : it.done ? "is-done" : it.now ? "is-now" : "";
      const mark = it.skipped ? "–" : it.done ? "✓" : String(i + 1);
      return `<li class="${cls}"><span class="apl-n">${mark}</span>${esc(it.label)}</li>`;
    }).join("")}</ol>`;
  }

  function fmtLocal(iso) {
    try {
      return new Date(iso).toLocaleString(undefined, {
        weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
      });
    } catch { return iso; }
  }

  // The interview block for a shortlisted applicant: nothing yet, a time
  // waiting for a response, or the outcome of that response.
  function renderInterviewBlock(interview) {
    if (!interview) {
      return `<p class="msp-muted">The committee will be in touch to arrange an interview.</p>`;
    }
    if (interview.status === "confirmed") {
      return `
        <div class="apl-interview is-confirmed">
          <p><strong>Interview confirmed</strong> for ${esc(fmtLocal(interview.when))}
             with ${esc(interview.interviewer_name || "")}.</p>
          <p class="msp-muted">A calendar invite has been sent to your email.</p>
        </div>`;
    }
    if (interview.status === "declined") {
      return `
        <div class="apl-interview is-declined">
          <p>You let us know that time doesn't work.
             ${esc(interview.interviewer_name || "The interviewer")} will be in touch directly
             to find another.</p>
        </div>`;
    }
    // proposed — waiting on the candidate
    const note = interview.message ? `
      <p class="apl-interview-note">A note from ${esc(interview.interviewer_name || "")}:
        &ldquo;${esc(interview.message)}&rdquo;</p>` : "";
    return `
      <div class="apl-interview is-proposed">
        <p><strong>Proposed time:</strong> ${esc(fmtLocal(interview.when))}<br>
           <strong>Interviewer:</strong> ${esc(interview.interviewer_name || "")}
           ${interview.interviewer_email ? `(<a href="mailto:${esc(interview.interviewer_email)}">${esc(interview.interviewer_email)}</a>)` : ""}
        </p>
        ${note}
        <div class="apl-interview-actions">
          <button class="btn primary" id="confirmInterviewBtn">Confirm this time</button>
          <button class="btn ghost" id="declineInterviewToggle">I can't make it</button>
        </div>
        <div id="declineForm" style="display:none;margin-top:12px;">
          <input type="text" id="declineNote" class="msp-input"
                 placeholder="What times might work better? (optional)">
          <button class="btn ghost" id="declineInterviewBtn" style="margin-top:8px;">Send</button>
        </div>
      </div>`;
  }

  function wireInterviewActions() {
    const confirmBtn = $("#confirmInterviewBtn");
    const declineToggle = $("#declineInterviewToggle");
    const declineForm = $("#declineForm");
    const declineBtn = $("#declineInterviewBtn");

    confirmBtn?.addEventListener("click", async () => {
      confirmBtn.disabled = true;
      try {
        await api("/apply/interview/confirm", {});
        flash("Interview confirmed — check your email for the calendar invite.", false);
        drawnKey = "";
        await refresh();
      } catch (err) { flash(err.message, true); confirmBtn.disabled = false; }
    });
    declineToggle?.addEventListener("click", () => {
      declineForm.style.display = declineForm.style.display === "none" ? "block" : "none";
    });
    declineBtn?.addEventListener("click", async () => {
      declineBtn.disabled = true;
      const note = $("#declineNote")?.value.trim() || "";
      try {
        await api("/apply/interview/decline", { note });
        flash("Got it — they'll be in touch to find another time.", false);
        drawnKey = "";
        await refresh();
      } catch (err) { flash(err.message, true); declineBtn.disabled = false; }
    });
  }

  // ── Interview availability (candidate side) ───────────────────────────────
  // A shortlisted candidate clicks every hour, over a fixed window in
  // October, when they could do a 30-minute interview. An analyst picks one
  // of those slots on the admin page to actually schedule it — see
  // availability.js for how both sides compute the same London-time grid.
  let availabilitySelected = null;   // Set of epoch-ms, live while this screen is up
  let availabilitySaveTimer = null;

  function renderAvailabilityGrid(selectedSet) {
    const days = window.AlphaAvailability.buildDays();
    if (!days.length) {
      return `<p class="msp-muted">The scheduling window has closed.</p>`;
    }
    let html = '<div class="aval-wrap"><table class="aval-grid"><thead><tr><th class="aval-daylabel">Day</th>';
    days[0].slots.forEach((s) => { html += `<th>${esc(s.label)}</th>`; });
    html += "</tr></thead><tbody>";
    days.forEach((day) => {
      html += `<tr><td class="aval-daylabel">${esc(day.label)}</td>`;
      day.slots.forEach((slot) => {
        const sel = selectedSet.has(slot.ts) ? " is-selected" : "";
        html += `<td><button type="button" class="aval-cell-btn${sel}" data-ts="${slot.ts}"
                    title="${esc(day.label)}, ${esc(slot.label)}" aria-label="${esc(day.label)}, ${esc(slot.label)}"></button></td>`;
      });
      html += "</tr>";
    });
    html += "</tbody></table></div>";
    return html;
  }

  function wireAvailabilityGrid(container, selectedSet) {
    container.querySelectorAll(".aval-cell-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const ts = Number(btn.dataset.ts);
        if (selectedSet.has(ts)) selectedSet.delete(ts); else selectedSet.add(ts);
        btn.classList.toggle("is-selected");
        saveAvailability(selectedSet);
      });
    });
  }

  function saveAvailability(selectedSet) {
    const note = $("#availSavedNote");
    if (note) note.textContent = "Saving…";
    clearTimeout(availabilitySaveTimer);
    availabilitySaveTimer = setTimeout(async () => {
      const slots = Array.from(selectedSet).sort((a, b) => a - b).map((ts) => new Date(ts).toISOString());
      try {
        await api("/apply/availability", { slots });
        const n = $("#availSavedNote");
        if (n) n.textContent = "Saved.";
      } catch (err) {
        flash(err.message, true);
        const n = $("#availSavedNote");
        if (n) n.textContent = "Couldn't save — try again.";
      }
    }, 500);
  }

  function renderDone(state) {
    stopTicking();
    const decided = state.status === "accepted" || state.status === "rejected";
    const shortlisted = state.status === "shortlisted";
    const availabilityLocked = !!state.availability_locked;
    const showAvailability = shortlisted && !availabilityLocked;

    let body;
    if (state.status === "accepted") {
      body = `<p>Your application to <strong>${esc(state.programme || "")}</strong> was
             <strong style="color:var(--green);">accepted</strong>. Your membership has been
             updated — have a look at your <a href="/profile">profile</a>.</p>`;
    } else if (state.status === "rejected") {
      body = `<p>Your application to <strong>${esc(state.programme || "")}</strong> was not
             taken forward this time. You are welcome to keep playing and apply again
             in a future round.</p>`;
    } else if (shortlisted) {
      body = `<p>Your application to <strong>${esc(state.programme || "")}</strong> has been
             <strong style="color:var(--brand);">shortlisted for interview</strong>.</p>
             ${renderInterviewBlock(state.interview)}`;
      if (showAvailability) {
        availabilitySelected = new Set((state.availability || []).map((iso) => new Date(iso).getTime()));
        body += `
          <div style="border-top:1px solid var(--border);margin-top:18px;padding-top:16px;">
            <p class="apl-hint" style="margin:0 0 10px;font-size:13px;">
              <strong style="color:var(--text);">Your availability</strong> — click every hour,
              7am-7pm London time, when you could do a 30-minute interview. An analyst will
              pick one of these and confirm it with you.
            </p>
            <div id="availGrid">${renderAvailabilityGrid(availabilitySelected)}</div>
            <p class="apl-hint" id="availSavedNote">Times shown are London time. Saved automatically.</p>
          </div>`;
      } else if (state.interview && state.interview.status === "confirmed") {
        body += `<p class="aval-locked-note">Your interview time is confirmed, so availability is locked.</p>`;
      }
    } else if (state.is_fast_tracked) {
      body = `<p>Your application to <strong>${esc(state.programme || "")}</strong> is in. As a
           Fast-Track applicant there's no written assessment — the committee reviews your CV
           directly, and you'll hear a decision from there.</p>`;
    } else {
      body = `<p>Your application to <strong>${esc(state.programme || "")}</strong> is in, and your
           assessment has been submitted. The committee reads your CV, your written answer
           and your score together — nothing else is needed from you.</p>
         <p class="msp-muted">You will not see your own score. That is deliberate: it keeps
           the questions usable for the people applying after you.</p>`;
    }

    // Decided, but still eligible — accepted into Bootcamp with Analyst
    // still open, or rejected and free to try again. Nothing happens
    // automatically; this is the only way back into the choose-programme step.
    const canApplyAgain = decided && state.can_apply_again;
    const againLabel = (state.programmes || []).length === 1
      ? `Apply for ${state.programmes[0]}` : "Apply again";
    const againBtn = canApplyAgain
      ? `<button class="btn ghost" id="applyAgainBtn" style="margin-top:16px;margin-left:10px;">${esc(againLabel)}</button>`
      : "";

    $("#app").innerHTML = panel(decided ? "Decision" : "Application submitted", `
      ${renderReviewSteps(state)}
      <div class="apl-done-tick">✓</div>${body}
      <a class="btn" href="/" style="margin-top:16px;">Back to the trading floor</a>${againBtn}`);

    if (shortlisted && state.interview && state.interview.status === "proposed") {
      wireInterviewActions();
    }
    if (showAvailability) {
      wireAvailabilityGrid($("#availGrid"), availabilitySelected);
    }
    if (canApplyAgain) {
      $("#applyAgainBtn").addEventListener("click", () => {
        // Nothing has changed server-side yet, so the background poll needs
        // to be told to leave this screen alone until they actually submit —
        // see the `reapplying` flag.
        reapplying = true;
        renderChoose(state);
      });
    }
  }

  // ── The live assessment ────────────────────────────────────────────────────
  // Both questions are free text, so they share one essay-box template and
  // one submit path — the backend already knows which section is open and
  // saves into it accordingly.

  // A compact cheat-sheet of the LaTeX a candidate is realistically likely
  // to reach for on an estimation question — not exhaustive, just common.
  const LATEX_REFERENCE = [
    ["Fraction", "\\frac{a}{b}"],
    ["Exponent", "x^{2}"],
    ["Subscript", "x_{1}"],
    ["Square root", "\\sqrt{x}"],
    ["Multiply", "a \\times b"],
    ["Divide", "a \\div b"],
    ["Approx. equal", "a \\approx b"],
    ["Sum", "\\sum_{i=1}^{n} x_i"],
    ["Product", "\\prod_{i=1}^{n} x_i"],
    ["Greek letters", "\\pi, \\mu, \\sigma, \\alpha"],
    ["Infinity", "\\infty"],
    ["Less/greater or equal", "a \\leq b,\\ a \\geq b"],
    ["Not equal", "a \\neq b"],
  ];

  function renderLatexReference(id) {
    const rows = LATEX_REFERENCE.map(([label, code]) => `
      <div class="apl-latex-row">
        <span class="apl-latex-label">${esc(label)}</span>
        <code>${esc(code)}</code>
        <span class="apl-latex-eg">$${code}$</span>
      </div>`).join("");
    return `
      <div class="apl-latex-ref" id="${id}Ref" style="display:none;">
        <h4>LaTeX reference</h4>
        ${rows}
      </div>`;
  }

  function renderEssayBox(id, opts = {}) {
    const withRef = !!opts.latexRef;
    return `
      <div class="apl-essay-layout">
        <div class="apl-essay-col">
          <textarea class="apl-essay" id="${id}" placeholder="Take a minute to think, then write."
                    spellcheck="true"></textarea>
          <div class="apl-essay-meta">
            <span id="${id}Count">0 words</span>
            <span style="display:flex;gap:8px;">
              ${withRef ? `<button type="button" class="btn ghost apl-preview-btn" id="${id}RefToggle">LaTeX reference</button>` : ""}
              <button type="button" class="btn ghost apl-preview-btn" id="${id}PreviewToggle">Preview</button>
            </span>
          </div>
        </div>
        ${withRef ? renderLatexReference(id) : ""}
      </div>
      <div class="apl-preview" id="${id}Preview" style="display:none;"></div>
      <p class="apl-hint">
        Plain text is fine. For a formula, type LaTeX — <code>$x^2$</code> or
        <code>$$\\sum_i x_i$$</code> — and hit Preview to see how it will render.
        Saved automatically · pasting is disabled.
      </p>`;
  }

  // Renders LaTeX delimited by $...$ / $$...$$ inside otherwise-plain text.
  // textContent first (never innerHTML) so nothing the candidate typed is
  // interpreted as markup — auto-render then finds and replaces just the
  // math spans it recognises.
  function renderMathPreview(container, text) {
    container.textContent = text.trim() ? text : "(nothing written yet)";
    if (!window.renderMathInElement) return;
    try {
      renderMathInElement(container, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "$", right: "$", display: false },
        ],
        throwOnError: false,
      });
    } catch { /* a malformed formula just shows as typed */ }
  }

  function wireEssayBox(id, initialText, onInput, opts = {}) {
    const essay = $("#" + id);
    const countEl = $("#" + id + "Count");
    const toggle = $("#" + id + "PreviewToggle");
    const preview = $("#" + id + "Preview");

    essay.value = initialText || "";
    const updateCount = () => {
      const n = essay.value.trim() ? essay.value.trim().split(/\s+/).length : 0;
      countEl.textContent = n + (n === 1 ? " word" : " words");
    };
    updateCount();

    essay.addEventListener("input", () => {
      updateCount();
      onInput();
      if (preview.style.display !== "none") renderMathPreview(preview, essay.value);
    });
    essay.addEventListener("paste", (e) => {
      e.preventDefault();
      flash("Pasting is disabled for this answer — please type it yourself.", true);
      api("/apply/oa/flag", { kind: "paste" }).catch(() => {});
    });
    essay.addEventListener("drop", (e) => e.preventDefault());

    toggle.addEventListener("click", () => {
      const showing = preview.style.display !== "none";
      if (showing) {
        preview.style.display = "none";
        toggle.textContent = "Preview";
      } else {
        renderMathPreview(preview, essay.value);
        preview.style.display = "block";
        toggle.textContent = "Hide preview";
      }
    });

    if (opts.latexRef) {
      const refBtn = $("#" + id + "RefToggle");
      const refPanel = $("#" + id + "Ref");
      // Static content — render its math once rather than on every toggle.
      if (window.renderMathInElement) {
        try {
          renderMathInElement(refPanel, {
            delimiters: [{ left: "$", right: "$", display: false }],
            throwOnError: false,
          });
        } catch { /* the reference sheet still reads fine as raw LaTeX */ }
      }
      refBtn.addEventListener("click", () => {
        const showing = refPanel.style.display !== "none";
        refPanel.style.display = showing ? "none" : "block";
        refBtn.textContent = showing ? "LaTeX reference" : "Hide reference";
      });
    }
    return essay;
  }

  function scheduleDraft() {
    if (draftTimer) return;
    // Autosave, not on every keystroke: the wall clock runs whether the tab is
    // open or not, so a crash mid-answer should cost at most a few seconds of it.
    draftTimer = setTimeout(async () => {
      draftTimer = null;
      const essay = $("#essay");
      if (!essay) return;
      try { await api("/apply/oa/written", { text: essay.value, final: false }); }
      catch { /* the next autosave or the final submit carries it */ }
    }, 8000);
  }

  async function submitWritten(final) {
    if (busy) return;
    busy = true;
    const btn = $("#essayNext");
    if (btn) btn.disabled = true;
    if (draftTimer) { clearTimeout(draftTimer); draftTimer = null; }
    const essay = $("#essay");
    try {
      await api("/apply/oa/written", { text: essay ? essay.value : "", final: !!final });
    } catch { /* the next poll reconciles */ }
    busy = false;
    await refresh();
  }

  function renderMotivation(oa) {
    const w = oa.motivation || {};
    const key = "motivation";
    if (drawnKey !== key) {
      drawnKey = key;
      autoFiredKey = "";
      sectionLeft = w.seconds_left;
      sessionLeft = oa.session_seconds_left;
      $("#steps").innerHTML = "";
      $("#app").innerHTML = panel("Part one — behavioural", `
        <div class="apl-clocks">
          <span class="apl-clock-main" id="clockMain">${mmss(sectionLeft)}</span>
          <span class="apl-clock-sub">Part 1 of 2 · <span id="clockSession">${mmss(sessionLeft)}</span> left overall</span>
        </div>
        <div class="apl-progress"><span id="bar" style="width:100%"></span></div>
        <p class="apl-q-prompt">${esc(w.prompt || "")}</p>
        ${renderEssayBox("essay")}
        <button class="btn primary" id="essayNext" style="margin-top:14px;">
          Submit and go to part two
        </button>
        <p class="apl-hint">
          Moving on early does not add time to part two — the estimation question
          keeps its own ${CFG.estimationMinutes} minutes either way.
        </p>`, "part one of two");

      wireEssayBox("essay", w.text, scheduleDraft);
      $("#essay").focus();
      $("#essayNext").addEventListener("click", () => submitWritten(true));
      startTicking();
    } else {
      // Only nudge the clocks forward — never touch what they are typing.
      sectionLeft = Math.max(sectionLeft, w.seconds_left);
      sessionLeft = Math.max(sessionLeft, oa.session_seconds_left);
    }
  }

  function renderEstimation(oa) {
    const w = oa.estimation || {};
    const key = "estimation";
    if (drawnKey !== key) {
      drawnKey = key;
      autoFiredKey = "";
      sectionLeft = w.seconds_left;
      sessionLeft = oa.session_seconds_left;
      $("#steps").innerHTML = "";
      $("#app").innerHTML = panel("Part two — estimation", `
        <div class="apl-clocks">
          <span class="apl-clock-main" id="clockMain">${mmss(sectionLeft)}</span>
          <span class="apl-clock-sub">Part 2 of 2 · <span id="clockSession">${mmss(sessionLeft)}</span> left overall</span>
        </div>
        <div class="apl-progress"><span id="bar" style="width:100%"></span></div>
        <p class="apl-q-prompt">${esc(w.prompt || "")}</p>
        ${renderEssayBox("essay", { latexRef: true })}
        <button class="btn primary" id="essayNext" style="margin-top:14px;">Submit</button>
        <p class="apl-hint">This is the last question — submitting finishes the assessment.</p>`,
        "part two of two");

      wireEssayBox("essay", w.text, scheduleDraft, { latexRef: true });
      $("#essay").focus();
      $("#essayNext").addEventListener("click", () => submitWritten(true));
      startTicking();
    } else {
      sectionLeft = Math.max(sectionLeft, w.seconds_left);
      sessionLeft = Math.max(sessionLeft, oa.session_seconds_left);
    }
  }

  // ── Clock ticker ───────────────────────────────────────────────────────────

  function startTicking() {
    stopTicking();
    tickTimer = setInterval(() => {
      sectionLeft = Math.max(0, sectionLeft - 0.25);
      sessionLeft = Math.max(0, sessionLeft - 0.25);

      const main = $("#clockMain");
      const bar = $("#bar");
      const sess = $("#clockSession");
      const inMotivation = drawnKey === "motivation";
      const span = (inMotivation ? CFG.motivationMinutes : CFG.estimationMinutes) * 60;
      const lowThreshold = inMotivation ? 30 : 60;   // more warning on the longer question

      if (main) {
        main.textContent = mmss(sectionLeft);
        main.classList.toggle("is-low", sectionLeft <= lowThreshold);
      }
      if (bar) {
        bar.style.width = Math.max(0, Math.min(100, (sectionLeft / span) * 100)) + "%";
        bar.classList.toggle("is-low", sectionLeft <= lowThreshold);
      }
      if (sess) sess.textContent = mmss(sessionLeft);

      if (sectionLeft <= 0 && autoFiredKey !== drawnKey) {
        autoFiredKey = drawnKey;
        submitWritten(true);
      }
    }, 250);
  }

  function stopTicking() {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  }

  // ── Polling and dispatch ───────────────────────────────────────────────────

  /* One key per distinct screen. Polling runs every second, so a screen is
   * only rebuilt when its key changes — otherwise a poll landing between two
   * keystrokes would wipe the essay, untick the acknowledgement, or drop the
   * file the candidate just chose. */
  function keyFor(state) {
    if (state.status === "analyst") return "analyst";
    if (!state.eligible && state.status === "none") return "ineligible";
    switch (state.status) {
      case "none": return "choose";
      case "cv":
        if (!state.event_ticket) return "event-choice";
        return "cv:" + (state.cv_uploaded ? "yes" : "no");
      case "oa_ready": return "gate";
      case "oa_active": {
        const oa = state.oa || {};
        if (oa.section === "motivation") return "motivation";
        if (oa.section === "estimation") return "estimation";
        return "oa";
      }
      default: {
        // Interview status is folded in so a poll that picks up a fresh
        // proposal, or the candidate's own confirm/decline landing, forces
        // a redraw even though the outer application status hasn't moved.
        const iv = state.interview ? state.interview.status : "none";
        return "done:" + state.status + ":" + iv;
      }
    }
  }

  function render(state) {
    if (reapplying) return;   // mid "Apply again" — nothing server-side has moved yet
    const key = keyFor(state);
    const isLive = key === "motivation" || key === "estimation";

    if (isLive) {
      // These two draw themselves and then tick their own clocks in place.
      const oa = state.oa || {};
      if (oa.section === "motivation") renderMotivation(oa); else renderEstimation(oa);
      return;
    }

    stopTicking();
    if (key === drawnKey) return;
    drawnKey = key;

    // Neither of these is really "mid-apply-flow", so the choose/CV/
    // assessment/submitted stepper above would just be clutter.
    if (key === "analyst") { $("#steps").innerHTML = ""; renderAnalyst(state); return; }
    if (key === "ineligible") { $("#steps").innerHTML = ""; renderIneligible(state); return; }

    renderSteps(state);
    switch (state.status) {
      case "none": renderChoose(state); break;
      case "cv":
        if (!state.event_ticket) { renderEventChoice(state); break; }
        renderCv(state); break;
      case "oa_ready": renderOaGate(state); break;
      default: renderDone(state);
    }
  }

  function repoll(state) {
    // A second between polls while the clock is running; there is nothing to
    // watch for that closely on the static screens.
    const wanted = state.status === "oa_active" ? 1000 : 5000;
    if (pollTimer && pollTimer.every === wanted) return;
    if (pollTimer) clearInterval(pollTimer.id);
    pollTimer = { every: wanted, id: setInterval(refresh, wanted) };
  }

  async function refresh() {
    try {
      const state = await api("/apply/state");
      render(state);
      repoll(state);
    } catch (err) {
      // A session that expired mid-visit — genuinely different from a guest
      // who was never signed in, which the outer bootstrap below handles by
      // showing the public intro instead of bouncing straight to /login.
      if (err.status === 401) { window.location.href = "/login"; return; }
      flash(err.message, true);
    }
  }

  // The public-facing landing view for anyone not signed in — this is the
  // one link worth sharing: it explains the programme and points straight
  // at signing up, rather than bouncing a visitor to a bare login form
  // before they know what they'd be signing up for.
  function renderGuestIntro() {
    $("#steps").innerHTML = "";
    $("#profileLink")?.classList.add("hidden");
    if ($("#userName")) $("#userName").style.display = "none";
    $("#app").innerHTML = panel("Apply to Alpha Fund", `
      <p class="msp-muted" style="margin-top:0;">
        Alpha Fund is Oxford's student-run trading fund. Each year it runs a
        <strong>Quant Bootcamp</strong> — a term of sessions on probability, market
        making and systematic trading — and a <strong>Quant Analyst</strong> track, where
        you run your own ideas and go into the CV book firms read. Both are open to
        current University of Oxford students; a Fundamental track opens next term.
      </p>
      <p class="msp-muted">
        Applying takes an up-to-date CV and a short written assessment — no prior
        trading experience required, just how you think.
      </p>
      <div class="btn-row" style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
        <a class="btn primary" href="/signup">Sign up to apply</a>
        <a class="btn" href="/login">Log in</a>
      </div>
      <p class="apl-hint" style="margin-top:18px;">
        Read more about Alpha Fund at
        <a href="https://www.oxfordalphafund.com/" target="_blank" rel="noopener">oxfordalphafund.com ↗</a>.
      </p>`);
  }

  // Leaving the page mid-assessment is counted, not punished — a reviewer sees
  // the number next to the score and reads it alongside everything else.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && drawnKey && (drawnKey === "written" || drawnKey.startsWith("q"))) {
      api("/apply/oa/flag", { kind: "left_page" }).catch(() => {});
    }
  });

  (async () => {
    try {
      const me = await api("/me");
      const el = $("#userName");
      if (el) el.textContent = me.username || "user";
    } catch (err) {
      if (err.status === 401) { renderGuestIntro(); return; }
    }
    await refresh();   // schedules its own polling from here
  })();
})();
