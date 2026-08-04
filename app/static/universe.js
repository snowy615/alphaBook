/* Universe browser — every name the crash simulator models.
 *
 * Search and cohort filter over the full list, and a chart on click. The chart
 * is a TradingView embed of the real listed company; the cohort is the
 * simulator's factor bucket, which is a different thing and is labelled as such
 * on the page.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

  const DATA = window.UNIVERSE || { cohorts: [], stocks: [] };
  const STOCKS = DATA.stocks || [];
  const COHORTS = DATA.cohorts || [];

  const state = { query: "", cohort: "all" };

  const root = $("#uniRoot");
  const chips = $("#uniChips");
  const empty = $("#uniEmpty");
  const countEl = $("#uniCount");
  const search = $("#uniSearch");

  const counts = COHORTS.reduce((acc, c) => {
    acc[c] = STOCKS.filter((s) => s.c === c).length;
    return acc;
  }, {});

  function label(cohort) {
    // The generator's own names, spaced out where it ran words together.
    return cohort.replace(/([a-z])([A-Z])/g, "$1 $2");
  }

  function matches() {
    const q = state.query.trim().toUpperCase();
    return STOCKS.filter((s) => {
      if (state.cohort !== "all" && s.c !== state.cohort) return false;
      if (!q) return true;
      return s.t.toUpperCase().includes(q) || s.n.toUpperCase().includes(q);
    });
  }

  function renderChips() {
    const all = `<button class="uni-chip ${state.cohort === "all" ? "is-on" : ""}"
      data-cohort="all" type="button">All <span>${STOCKS.length}</span></button>`;
    chips.innerHTML = all + COHORTS.map((c) => `
      <button class="uni-chip ${state.cohort === c ? "is-on" : ""}"
        data-cohort="${esc(c)}" type="button">${esc(label(c))} <span>${counts[c]}</span></button>`
    ).join("");
    chips.querySelectorAll("[data-cohort]").forEach((b) => {
      b.addEventListener("click", () => {
        state.cohort = b.dataset.cohort;
        render();
      });
    });
  }

  function render() {
    const rows = matches();
    countEl.textContent = rows.length === STOCKS.length
      ? `${STOCKS.length} names`
      : `${rows.length} of ${STOCKS.length}`;

    empty.classList.toggle("hidden", rows.length > 0);

    const grouped = new Map();
    rows.forEach((s) => {
      if (!grouped.has(s.c)) grouped.set(s.c, []);
      grouped.get(s.c).push(s);
    });

    const order = COHORTS.filter((c) => grouped.has(c));
    root.innerHTML = order.map((c) => {
      const names = grouped.get(c);
      return `<section class="uni-group">
        <div class="uni-group-head">
          <h3>${esc(label(c))}</h3>
          <span>${names.length} name${names.length === 1 ? "" : "s"}</span>
        </div>
        <div class="uni-grid">${names.map((s) => `
          <button class="uni-name" type="button"
            data-ticker="${esc(s.t)}" data-name="${esc(s.n)}" data-cohort="${esc(c)}">
            <span class="uni-ticker">${esc(s.t)}</span>
            <span class="uni-company">${esc(s.n)}</span>
          </button>`).join("")}</div>
      </section>`;
    }).join("");

    root.querySelectorAll(".uni-name").forEach((b) => {
      b.addEventListener("click", () => openChart(b.dataset.ticker, b.dataset.name, b.dataset.cohort));
    });
    renderChips();
  }

  // ---- chart ----
  function openChart(ticker, name, cohort) {
    $("#uniModalTicker").textContent = ticker;
    $("#uniModalName").textContent = name;
    $("#uniModalCohort").textContent = label(cohort) + " cohort";
    $("#uniYahoo").href = `https://finance.yahoo.com/quote/${encodeURIComponent(ticker)}/`;

    const symbol = encodeURIComponent(ticker);
    const dark = document.documentElement.getAttribute("data-theme") !== "light";
    const src = "https://s.tradingview.com/widgetembed/?" + [
      "details=0", "hide_side_toolbar=1", "interval=D", "save_image=0", "style=1",
      `symbol=${symbol}`, `theme=${dark ? "dark" : "light"}`, "timezone=Etc%2FUTC",
      "withdateranges=1", "hide_legend=0",
    ].join("&");

    // Fallback first in the DOM so it renders underneath the frame.
    $("#uniChartHost").innerHTML =
      `<p class="uni-chart-fallback">The chart could not load. Some networks block the
        TradingView embed — the Yahoo Finance link below always works.</p>
       <iframe src="${src}" title="${esc(ticker)} chart" frameborder="0"
        allowtransparency="true" scrolling="no" loading="lazy"></iframe>`;

    $("#uniModal").classList.remove("hidden");
    document.body.classList.add("tut-locked");
  }

  function closeChart() {
    $("#uniModal").classList.add("hidden");
    // Drop the iframe so the embed stops running in the background.
    $("#uniChartHost").innerHTML = "";
    document.body.classList.remove("tut-locked");
  }

  $("#uniModalClose").addEventListener("click", closeChart);
  $("#uniModal").addEventListener("click", (e) => {
    if (e.target.id === "uniModal") closeChart();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#uniModal").classList.contains("hidden")) closeChart();
  });

  // ---- controls ----
  let debounce = null;
  search.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      state.query = search.value;
      render();
    }, 90);
  });

  $("#uniClear").addEventListener("click", () => {
    state.query = "";
    state.cohort = "all";
    search.value = "";
    render();
    search.focus();
  });

  // ---- auth chrome ----
  (async function initAuthUI() {
    const loginBox = $("#loginBox");
    const userBox = $("#userBox");
    try {
      const r = await fetch("/me", { credentials: "include" });
      if (!r.ok) throw new Error("guest");
      const me = await r.json();
      $("#userName").textContent = me.username || me.name || "user";
      userBox.classList.remove("hidden");
    } catch {
      loginBox.classList.remove("hidden");
    }
  })();

  render();
})();
