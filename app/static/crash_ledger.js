/* Crash Ledger — ticker tape + the TradingView chart-modal (graph analysis).
 * Behaviour is unchanged from the source page; the only additions are that the
 * embedded chart follows the site's light/dark theme and the topbar shows the
 * signed-in user.
 */
(function () {
  "use strict";

  // ---- topbar user ----
  (async function () {
    try {
      const r = await fetch("/me", { credentials: "include" });
      if (!r.ok) return;
      const me = await r.json();
      const el = document.getElementById("userName");
      if (el) el.textContent = me.username || me.name || "user";
    } catch (e) { /* not signed in — leave default */ }
  })();

  // ---- scrolling ticker tape ----
  (function () {
    const tapeData = [
      { t: "CVNA", name: "Carvana Co.", v: -52.6 },
      { t: "UAL", name: "United Airlines Holdings, Inc.", v: -55.9 },
      { t: "LVS", name: "Las Vegas Sands Corp.", v: -33.5 },
      { t: "BLDR", name: "Builders FirstSource, Inc.", v: -43.1 },
      { t: "NCLH", name: "Norwegian Cruise Line Holdings Ltd.", v: -57.3 },
      { t: "APO", name: "Apollo Global Management, Inc.", v: -10.2 },
      { t: "COIN", name: "Coinbase Global, Inc.", v: -85.9 },
      { t: "AKAM", name: "Akamai Technologies, Inc.", v: -42.6 },
      { t: "EQIX", name: "Equinix, Inc.", v: -36.2 },
      { t: "SBAC", name: "SBA Communications Corporation", v: -67.1 },
    ];
    const track = document.getElementById("tapeTrack");
    if (!track) return;
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
    const renderSet = () => tapeData.map((d) => (
      `<span class="tape-item"><span class="t-ticker">${esc(d.t)}</span>${esc(d.name)}` +
      `<span class="${d.v >= 0 ? "t-pos" : "t-neg"}">${d.v >= 0 ? "+" : ""}${d.v.toFixed(1)}%</span></span>`
    )).join("");
    track.innerHTML = renderSet() + renderSet();   // duplicate for a seamless loop
  })();

  // ---- chart modal ----
  (function () {
    const backdrop = document.getElementById("chartBackdrop");
    const chartHost = document.getElementById("chartHost");
    const mhTicker = document.getElementById("mhTicker");
    const mhName = document.getElementById("mhName");
    const mhYahoo = document.getElementById("mhYahoo");
    const mhClose = document.getElementById("mhClose");
    if (!backdrop) return;

    function chartTheme() {
      return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }

    function openChart(ticker, name, exchange) {
      mhTicker.textContent = ticker;
      mhName.textContent = name;
      mhYahoo.href = `https://finance.yahoo.com/quote/${encodeURIComponent(ticker)}/`;

      backdrop.classList.add("open");
      document.body.style.overflow = "hidden";

      const symbol = encodeURIComponent(`${exchange}:${ticker}`);
      const theme = chartTheme();
      const bg = theme === "light" ? "ffffff" : "111522";
      const iframeSrc = `https://s.tradingview.com/widgetembed/?details=0&hide_side_toolbar=1` +
        `&interval=D&save_image=0&studies=%5B%5D&style=1&symbol=${symbol}&theme=${theme}` +
        `&toolbar_bg=${bg}&timezone=exchange&withdateranges=1&hideideas=1&symboledit=0&locale=en`;

      chartHost.innerHTML = `
        <iframe
          title="TradingView chart for ${ticker}"
          src="${iframeSrc}"
          style="width:100%;height:100%;border:0;display:block;position:absolute;inset:0;"
          loading="eager"
          referrerpolicy="strict-origin-when-cross-origin"
          allow="fullscreen"></iframe>`;
    }

    function closeChart() {
      backdrop.classList.remove("open");
      document.body.style.overflow = "";
      chartHost.innerHTML = "";
    }

    document.querySelectorAll(".card-top .ticker").forEach((btn) => {
      btn.addEventListener("click", () => {
        openChart(btn.dataset.ticker, btn.dataset.name, btn.dataset.exchange);
      });
    });

    mhClose.addEventListener("click", closeChart);
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeChart(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeChart(); });
  })();
})();
