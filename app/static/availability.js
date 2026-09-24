/* Shared grid math for the interview-availability picker.
 *
 * The window is fixed to this admissions cycle's dates and fixed to London
 * half hours (7am to 7pm), not the viewer's own browser timezone, so a candidate
 * and every analyst looking at the admin page always agree on exactly the
 * same set of slots, labelled the same way, regardless of where either of
 * them happens to be. Keep these two constants in step with
 * AVAILABILITY_WINDOW_START/END in app/applications.py.
 */
window.AlphaAvailability = (function () {
  "use strict";

  const TZ = "Europe/London";
  const WINDOW_START = { y: 2026, m: 10, d: 1 };    // October 1, 2026
  const WINDOW_END = { y: 2026, m: 10, d: 23 };      // October 23, 2026 (inclusive)
  const START_HOUR = 7;    // London wall-clock, first slot 07:00
  const END_HOUR = 18;     // London wall-clock, last slot 18:30 (ends 7pm)
  const SLOT_MINUTES = 30; // one slot is one interview; keep in step with AVAILABILITY_SLOT_MINUTES

  function cmp(a, b) {
    if (a.y !== b.y) return a.y - b.y;
    if (a.m !== b.m) return a.m - b.m;
    return a.d - b.d;
  }

  function addDays(p, n) {
    const d = new Date(Date.UTC(p.y, p.m - 1, p.d) + n * 86400000);
    return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate() };
  }

  // Today's calendar date *in London*, not the viewer's own timezone.
  function londonToday() {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(new Date());
    const get = (t) => +parts.find((p) => p.type === t).value;
    return { y: get("year"), m: get("month"), d: get("day") };
  }

  // The UTC offset London is at for a given instant, in milliseconds —
  // found by formatting that instant AS London time, then reinterpreting
  // those same numbers as UTC; the gap between the two is the offset. This
  // handles the BST/GMT switch correctly with no timezone library.
  function londonOffsetMsAt(utcMs) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: TZ, hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(new Date(utcMs));
    const get = (t) => parts.find((p) => p.type === t).value;
    const hour = get("hour") === "24" ? 0 : +get("hour");
    const asUtc = Date.UTC(+get("year"), +get("month") - 1, +get("day"), hour, +get("minute"), +get("second"));
    return asUtc - utcMs;
  }

  // A London wall-clock moment (e.g. "9am on 5 October 2026") to its
  // absolute UTC epoch millisecond.
  function londonWallClockToUtcMs(p, hour, minute) {
    const guess = Date.UTC(p.y, p.m - 1, p.d, hour, minute || 0, 0);
    return guess - londonOffsetMsAt(guess);
  }

  function hourLabel(ts) {
    return new Date(ts).toLocaleTimeString("en-GB", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
  }

  function dayLabel(p) {
    // Noon avoids ever landing on the wrong side of a date boundary.
    const utcMs = Date.UTC(p.y, p.m - 1, p.d, 12);
    return new Date(utcMs).toLocaleDateString("en-GB", { timeZone: TZ, weekday: "short", month: "short", day: "numeric" });
  }

  // Returns an array of days (today, floored at WINDOW_START, through
  // WINDOW_END) — each { label, slots: [{ ts, key, label }] }.
  function buildDays() {
    const start = cmp(londonToday(), WINDOW_START) > 0 ? londonToday() : WINDOW_START;
    const days = [];
    let cur = start;
    while (cmp(cur, WINDOW_END) <= 0) {
      const slots = [];
      for (let h = START_HOUR; h <= END_HOUR; h++) {
        for (let m = 0; m < 60; m += SLOT_MINUTES) {
          const ts = londonWallClockToUtcMs(cur, h, m);
          slots.push({ ts, key: new Date(ts).toISOString(), label: hourLabel(ts) });
        }
      }
      days.push({ label: dayLabel(cur), slots });
      cur = addDays(cur, 1);
    }
    return days;
  }

  return { buildDays };
})();
