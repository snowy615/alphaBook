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
    "Quant Bootcamp":
      "The taught track: a term of sessions on probability, market making and " +
      "systematic trading, with the games on this site as the practical half.",
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
  let sessionLeft = 0;        // local mirror of the 15-minute clock
  let sectionLeft = 0;        // local mirror of the written / per-question clock
  let questionIndex = -1;
  let autoFiredKey = "";      // guards against firing the same auto-submit twice
  let busy = false;
  let draftTimer = null;
  let pickedProgramme = null;

  // ── Progress rail ──────────────────────────────────────────────────────────
  const STEP_ORDER = ["choose", "cv", "oa", "done"];
  const STEP_LABEL = { choose: "Choose a programme", cv: "Upload your CV", oa: "Assessment", done: "Submitted" };

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
    $("#app").innerHTML = panel("Applications are for general accounts", `
      <p class="msp-muted" style="margin-top:0;">
        Your account is set to <strong>${esc(state.membership)}</strong>, so there is
        nothing here to apply for. If that is wrong, change it on your
        <a href="/profile">profile</a> or ask an admin.
      </p>`);
  }

  // General public applicants have nothing else on the account vouching for
  // them being a current Oxford student, so they confirm it directly. A
  // General Alpha Fund member has already cleared that bar to get their
  // membership, so there's nothing to re-ask.
  const GENERAL_PUBLIC = "General public";

  function renderChoose(state) {
    const options = (state.programmes || []).map((p) => `
      <label class="apl-choice" data-programme="${esc(p)}">
        <input type="radio" name="programme" value="${esc(p)}">
        <strong>${esc(p)}</strong>
        <span class="apl-blurb">${esc(PROGRAMME_BLURB[p] || "")}</span>
      </label>`).join("");

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
        Pick the programme you want. You will then put an up-to-date CV on your
        profile and sit a ${CFG.sessionMinutes}-minute assessment — you can start
        it whenever suits you, but once it starts it runs to the end in one sitting.
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
        await refresh();
      } catch (err) {
        flash(err.message, true);
        e.target.disabled = false;
      }
    });
  }

  function renderCv(state) {
    const has = state.cv_uploaded;
    $("#app").innerHTML = panel("Upload your CV", `
      <p class="msp-muted" style="margin-top:0;">
        Applying for <strong>${esc(state.programme || "")}</strong>. Put your most
        up-to-date CV on your profile — this is the copy the committee reads, and
        it is the same file that appears everywhere else on your profile.
      </p>
      <div class="cv-status-banner ${has ? "uploaded" : "missing"}"
           style="display:flex;align-items:center;gap:12px;padding:14px 18px;margin-bottom:16px;font-size:14px;font-weight:600;
                  border:1px solid ${has ? "var(--green)" : "var(--border)"};color:${has ? "var(--green)" : "var(--muted)"};">
        <span>${has ? "✓" : "✗"}</span>
        <span id="cvText">${has ? "A CV is on file" : "No CV uploaded yet"}</span>
      </div>
      <input type="file" id="cvFile" accept="application/pdf,.pdf" style="display:none">
      <div class="btn-row" style="display:flex;gap:10px;flex-wrap:wrap;">
        <button class="btn" id="pickBtn">${has ? "Replace CV" : "Choose PDF"}</button>
        ${has ? '<a class="btn ghost" href="/me/cv" target="_blank" rel="noopener">View current CV ↗</a>' : ""}
      </div>
      <p class="apl-hint">PDF only · max 10 MB</p>
      <hr class="divider" style="border:none;border-top:1px solid var(--border);margin:22px 0;">
      <button class="btn primary" id="cvNext" ${has ? "" : "disabled"}>
        This is my up-to-date CV — continue
      </button>`);

    $("#pickBtn").addEventListener("click", () => $("#cvFile").click());
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
        flash("CV uploaded", false);
        await refresh();
      } catch (err) {
        flash(err.message, true);
        $("#pickBtn").disabled = false;
        $("#cvText").textContent = "Upload failed";
      }
    });

    $("#cvNext").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { await api("/apply/cv-confirm", {}); await refresh(); }
      catch (err) { flash(err.message, true); e.target.disabled = false; }
    });
  }

  const NUMERICAL_TOPICS = {
    "Quant Bootcamp": "Probability, expected value and pattern finding.",
    "Quant Analyst": "Probability, expected value, pattern finding, and basic quant concepts "
      + "(things like delta and Sharpe ratio — some multiple choice, some a short calculation).",
  };

  function renderOaGate(state) {
    const r = state.rules || {};
    const topics = NUMERICAL_TOPICS[state.programme] || NUMERICAL_TOPICS["Quant Bootcamp"];
    $("#app").innerHTML = panel("Your assessment is ready", `
      <p class="msp-muted" style="margin-top:0;">
        Applying for <strong>${esc(state.programme || "")}</strong>. Start this whenever
        you like — there is no window and no deadline on the button. But once you
        press it the clock runs to the end whether the tab is open or not, so sit
        down somewhere quiet with ${CFG.sessionMinutes} clear minutes first.
      </p>

      <ul class="apl-rules">
        <li><strong>${Math.round((r.session_seconds || 900) / 60)} minutes in total</strong>, in one sitting. One attempt.</li>
        <li><strong>Part one — ${Math.round((r.written_seconds || 300) / 60)} minutes of writing.</strong> A single question, in your own words.</li>
        <li><strong>Part two — ${r.numerical_questions || 20} questions at ${r.seconds_per_question || 30} seconds each.</strong>
            ${esc(topics)} Every answer is a whole number.</li>
        <li>Questions arrive one at a time. You cannot go back, and the clock does not pause.</li>
        <li>Pen and paper are fine. A calculator is not needed — nothing here requires one.</li>
      </ul>

      <div class="apl-warn">
        <strong>No AI, and no outside help.</strong>
        You may not use ChatGPT, Claude, Copilot or any other AI tool, and you may not
        search the web or ask anyone else. We are interested in how you think, not what
        a model outputs. Pasting into the written answer is disabled, and leaving the
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

  function renderDone(state) {
    stopTicking();
    const decided = state.status === "accepted" || state.status === "rejected";
    const shortlisted = state.status === "shortlisted";

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
    } else {
      body = `<p>Your application to <strong>${esc(state.programme || "")}</strong> is in, and your
           assessment has been submitted. The committee reads your CV, your written answer
           and your score together — nothing else is needed from you.</p>
         <p class="msp-muted">You will not see your own score. That is deliberate: it keeps
           the questions usable for the people applying after you.</p>`;
    }

    $("#app").innerHTML = panel(decided ? "Decision" : "Application submitted", `
      ${renderReviewSteps(state)}
      <div class="apl-done-tick">✓</div>${body}
      <a class="btn" href="/" style="margin-top:16px;">Back to the trading floor</a>`);

    if (shortlisted && state.interview && state.interview.status === "proposed") {
      wireInterviewActions();
    }
  }

  // ── The live assessment ────────────────────────────────────────────────────

  function renderWritten(oa) {
    const w = oa.written || {};
    const key = "written";
    if (drawnKey !== key) {
      drawnKey = key;
      autoFiredKey = "";
      sectionLeft = w.seconds_left;
      sessionLeft = oa.session_seconds_left;
      $("#steps").innerHTML = "";
      $("#app").innerHTML = panel("Part one — written", `
        <div class="apl-clocks">
          <span class="apl-clock-main" id="clockMain">${mmss(sectionLeft)}</span>
          <span class="apl-clock-sub">Part 1 of 2 · <span id="clockSession">${mmss(sessionLeft)}</span> left overall</span>
        </div>
        <div class="apl-progress"><span id="bar" style="width:100%"></span></div>
        <p class="apl-q-prompt">${esc(w.prompt || "")}</p>
        <textarea class="apl-essay" id="essay" placeholder="Take a minute to think, then write."
                  spellcheck="true"></textarea>
        <div class="apl-essay-meta">
          <span id="wordCount">0 words</span>
          <span>Saved automatically · pasting is disabled</span>
        </div>
        <button class="btn primary" id="essayNext" style="margin-top:14px;">
          Submit and go to part two
        </button>
        <p class="apl-hint">
          Moving on early does not add time to part two — each question there has its
          own ${CFG.secondsPerQuestion} seconds either way.
        </p>`, "one question");

      const essay = $("#essay");
      essay.value = w.text || "";
      essay.focus();
      updateWordCount();
      essay.addEventListener("input", () => { updateWordCount(); scheduleDraft(); });
      essay.addEventListener("paste", (e) => {
        e.preventDefault();
        flash("Pasting is disabled for this answer — please type it yourself.", true);
        api("/apply/oa/flag", { kind: "paste" }).catch(() => {});
      });
      essay.addEventListener("drop", (e) => e.preventDefault());
      $("#essayNext").addEventListener("click", () => submitWritten(true));
      startTicking();
    } else {
      // Only nudge the clocks forward — never touch what they are typing.
      sectionLeft = Math.max(sectionLeft, w.seconds_left);
      sessionLeft = Math.max(sessionLeft, oa.session_seconds_left);
    }
  }

  function updateWordCount() {
    const essay = $("#essay");
    const el = $("#wordCount");
    if (!essay || !el) return;
    const n = essay.value.trim() ? essay.value.trim().split(/\s+/).length : 0;
    el.textContent = n + (n === 1 ? " word" : " words");
  }

  function scheduleDraft() {
    if (draftTimer) return;
    // Autosave, not on every keystroke: the wall clock runs whether the tab is
    // open or not, so a crash mid-essay should cost at most a few seconds of it.
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

  function renderNumerical(oa) {
    const q = oa.question;
    if (!q) return;
    const key = "q" + q.index;
    if (drawnKey !== key) {
      drawnKey = key;
      autoFiredKey = "";
      questionIndex = q.index;
      sectionLeft = q.seconds_left;
      sessionLeft = oa.session_seconds_left;
      $("#steps").innerHTML = "";
      $("#app").innerHTML = panel(`Part two — question ${q.index + 1} of ${q.total}`, `
        <div class="apl-clocks">
          <span class="apl-clock-main" id="clockMain">${Math.ceil(sectionLeft)}s</span>
          <span class="apl-clock-sub"><span id="clockSession">${mmss(sessionLeft)}</span> left overall</span>
        </div>
        <div class="apl-progress"><span id="bar" style="width:100%"></span></div>
        <span class="apl-q-kind">${esc(q.kind)}</span>
        <p class="apl-q-prompt">${esc(q.prompt)}</p>
        <input class="apl-answer" id="answer" type="text" inputmode="numeric"
               autocomplete="off" placeholder="A whole number">
        <p class="apl-hint">Every answer is a whole number. Press Enter to submit.</p>
        <button class="btn primary" id="answerBtn" style="margin-top:12px;">Submit</button>`,
        `${q.seconds_per_question}s per question`);

      const input = $("#answer");
      input.focus();
      // Whole numbers only, enforced as they type so nobody wastes a second of
      // their thirty discovering the field rejected what they wrote.
      input.addEventListener("input", () => {
        const cleaned = input.value.replace(/[^0-9-]/g, "").replace(/(?!^)-/g, "");
        if (cleaned !== input.value) input.value = cleaned;
      });
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") submitAnswer(false); });
      $("#answerBtn").addEventListener("click", () => submitAnswer(false));
      startTicking();
    } else {
      sectionLeft = Math.max(sectionLeft, q.seconds_left);
      sessionLeft = Math.max(sessionLeft, oa.session_seconds_left);
    }
  }

  async function submitAnswer() {
    if (busy) return;
    busy = true;
    const btn = $("#answerBtn");
    if (btn) btn.disabled = true;
    const input = $("#answer");
    try {
      await api("/apply/oa/answer", { index: questionIndex, value: input ? input.value : "" });
    } catch { /* the next poll reconciles either way */ }
    busy = false;
    await refresh();
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
      const inWritten = drawnKey === "written";
      const span = inWritten ? CFG.writtenMinutes * 60 : CFG.secondsPerQuestion;

      if (main) {
        main.textContent = inWritten ? mmss(sectionLeft) : Math.ceil(sectionLeft) + "s";
        main.classList.toggle("is-low", sectionLeft <= (inWritten ? 30 : 10));
      }
      if (bar) {
        bar.style.width = Math.max(0, Math.min(100, (sectionLeft / span) * 100)) + "%";
        bar.classList.toggle("is-low", sectionLeft <= (inWritten ? 30 : 10));
      }
      if (sess) sess.textContent = mmss(sessionLeft);

      if (sectionLeft <= 0 && autoFiredKey !== drawnKey) {
        autoFiredKey = drawnKey;
        if (inWritten) submitWritten(true); else submitAnswer();
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
    if (!state.eligible && state.status === "none") return "ineligible";
    switch (state.status) {
      case "none": return "choose";
      case "cv": return "cv:" + (state.cv_uploaded ? "yes" : "no");
      case "oa_ready": return "gate";
      case "oa_active": {
        const oa = state.oa || {};
        if (oa.section === "written") return "written";
        if (oa.section === "numerical" && oa.question) return "q" + oa.question.index;
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
    const key = keyFor(state);
    const isLive = key === "written" || key.charAt(0) === "q";

    if (isLive) {
      // These two draw themselves and then tick their own clocks in place.
      const oa = state.oa || {};
      if (oa.section === "written") renderWritten(oa); else renderNumerical(oa);
      return;
    }

    stopTicking();
    if (key === drawnKey) return;
    drawnKey = key;
    renderSteps(state);

    if (key === "ineligible") { renderIneligible(state); return; }
    switch (state.status) {
      case "none": renderChoose(state); break;
      case "cv": renderCv(state); break;
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
      if (err.status === 401) { window.location.href = "/login"; return; }
      flash(err.message, true);
    }
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
      if (err.status === 401) { window.location.href = "/login"; return; }
    }
    await refresh();   // schedules its own polling from here
  })();
})();
