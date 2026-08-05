/**
 * Shared renderer for end-of-game coaching.
 *
 * Every mode's analyser returns the same shape, so every final screen shows
 * the same thing in the same place: a graded headline, a row of stat tiles,
 * and the notes. Modes call `AB.feedback.render(container, payload)` from
 * wherever they draw their result screen.
 */
(function () {
  "use strict";

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  const GRADE = {
    great: { label: "Excellent", cls: "fbk-great" },
    good: { label: "Solid", cls: "fbk-good" },
    mixed: { label: "Mixed", cls: "fbk-mixed" },
    poor: { label: "Rough", cls: "fbk-poor" },
  };

  const KIND = {
    win: { icon: "✓", label: "What worked" },
    gap: { icon: "!", label: "What cost you" },
    tip: { icon: "→", label: "Try next time" },
  };

  function markup(fb) {
    if (!fb || (!fb.headline && !(fb.notes || []).length)) return "";
    const g = GRADE[fb.grade] || GRADE.mixed;

    const stats = (fb.stats || []).map((s) => `
      <div class="fbk-stat">
        <div class="fbk-stat-label">${esc(s.label)}</div>
        <div class="fbk-stat-value">${esc(s.value)}</div>
      </div>`).join("");

    const notes = (fb.notes || []).map((n) => {
      const k = KIND[n.kind] || KIND.tip;
      return `
        <li class="fbk-note fbk-note-${esc(n.kind)}">
          <span class="fbk-note-icon" aria-hidden="true">${k.icon}</span>
          <span class="fbk-note-body">
            <span class="fbk-note-kind">${k.label}</span>
            <span class="fbk-note-text">${esc(n.text)}</span>
          </span>
        </li>`;
    }).join("");

    return `
      <div class="fbk ${g.cls}">
        <div class="fbk-head">
          <span class="fbk-grade">${g.label}</span>
          <h4 class="fbk-headline">${esc(fb.headline)}</h4>
        </div>
        ${stats ? `<div class="fbk-stats">${stats}</div>` : ""}
        ${notes ? `<ul class="fbk-notes">${notes}</ul>` : ""}
      </div>`;
  }

  function render(container, fb) {
    const el = typeof container === "string" ? document.querySelector(container) : container;
    if (!el) return;
    const html = markup(fb);
    el.innerHTML = html;
    el.classList.toggle("hidden", !html);
  }

  window.AB = window.AB || {};
  window.AB.feedback = { render, markup };
})();
