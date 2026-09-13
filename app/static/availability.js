/* Shared grid math for the interview-availability picker.
 *
 * Both the candidate's apply page and the admin review page need to agree on
 * exactly the same set of hour slots for a given applicant, regardless of
 * which timezone each viewer's browser is in or which day each of them
 * happens to load the page. So the grid is anchored to a fixed instant (the
 * moment the applicant was shortlisted, in UTC) and built in UTC throughout;
 * only the on-screen labels are localised to whoever is looking at them.
 */
window.AlphaAvailability = (function () {
  "use strict";

  const DAYS = 14;
  const START_HOUR = 8;   // UTC, inclusive
  const END_HOUR = 20;    // UTC, inclusive — last slot runs 20:00-21:00 UTC

  function anchorMidnightUtcMs(anchorIso) {
    const d = anchorIso ? new Date(anchorIso) : new Date();
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  }

  function hourLabel(ts) {
    return new Date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function dayLabel(ts) {
    return new Date(ts).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  }

  // Returns an array of 14 { ts, label, slots: [{ ts, key, label }] } days,
  // each slot's `ts` a millisecond epoch and `key` its ISO string.
  function buildDays(anchorIso) {
    const base = anchorMidnightUtcMs(anchorIso);
    const days = [];
    for (let day = 0; day < DAYS; day++) {
      const dayStartMs = base + day * 86400000;
      const slots = [];
      for (let h = START_HOUR; h <= END_HOUR; h++) {
        const ts = dayStartMs + h * 3600000;
        slots.push({ ts, key: new Date(ts).toISOString(), label: hourLabel(ts) });
      }
      // The row's label is derived from its first slot, not from the UTC
      // midnight boundary itself: those hour slots (08:00-20:00 UTC) can
      // localise to a different calendar day than midnight UTC does for a
      // viewer behind UTC (e.g. the US), which would otherwise show a row
      // labelled "Mon" whose slots all land on "Tue" in that viewer's clock.
      days.push({ ts: dayStartMs, label: dayLabel(slots[0].ts), slots });
    }
    return days;
  }

  return { buildDays, DAYS, START_HOUR, END_HOUR };
})();
