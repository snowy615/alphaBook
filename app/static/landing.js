(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const fetchJSON = async (url, init) => {
    const r = await fetch(url, { credentials: "include", ...init });
    if (!r.ok) throw new Error(String(r.status));
    const txt = await r.text();
    try { return JSON.parse(txt); } catch { return {}; }
  };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

  let isAuthed = false;

  // ── Line-art marks, one per mode. Drawn rather than lettered: an initial
  //    in a box says nothing about what the mode actually is. ──────────────
  const svg = (body) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

  const ICON = {
    // candlesticks
    market: svg('<path d="M7 3v3M7 16v5M17 3v5M17 18v3"/><rect x="4.5" y="6" width="5" height="10"/><rect x="14.5" y="8" width="5" height="10"/>'),
    // angle brackets over a slash
    code: svg('<path d="M8.5 8L4 12l4.5 4M15.5 8l4.5 4-4.5 4M13.5 5l-3 14"/>'),
    // terminal window
    terminal: svg('<rect x="3" y="4" width="18" height="16"/><path d="M3 8h18M7 12.5l2.5 2.5L7 17.5M13 17.5h4"/>'),
    // newspaper
    news: svg('<path d="M17 6H4v13a1 1 0 0 0 1 1h13"/><path d="M17 6h3v11a3 3 0 0 1-3 3"/><path d="M7 10h7M7 13h7M7 16h4"/>'),
    // box-and-whisker
    stats: svg('<path d="M3 12h4M17 12h4M3 9v6M21 9v6"/><rect x="7" y="7.5" width="10" height="9"/><path d="M12.5 7.5v9"/>'),
    // two cards, one behind the other
    cards: svg('<rect x="3" y="7" width="11" height="14" rx="1"/><path d="M8.5 4h9.5a2 2 0 0 1 2 2v11"/><path d="M8.5 14h1"/>'),
    // calculator
    calc: svg('<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M8 7.5h8M8 12h.01M12 12h.01M16 12h.01M8 16h.01M12 16h.01M16 16h.01"/>'),
    // a line breaking down through a floor, with the rebound after it
    risk: svg('<path d="M3 6l4 5 3-2 4 9 3-4 4 2"/><path d="M3 20h18"/><path d="M14 18l3-4"/>'),
    // a downward trend with an arrowhead — the crash ledger
    crash: svg('<path d="M4 5v14h16"/><path d="M8 9l3 4 3-3 4 5"/><path d="M18 15v-3h-3"/>'),
  };

  // ---- Mode board ----
  function buildGameGrid() {
    const grid = $("#gameGrid");
    if (!grid) return;
    grid.innerHTML = "";

    const groups = window.GAME_GROUPS || {};
    const marketGames = groups.market || [];

    const modes = [
      marketGames.length ? {
        icon: ICON.market,
        name: "Market Simulation",
        blurb: "Trade " + marketGames.length + " stocks on a live limit order book anchored to " +
               "real prices. A market-maker bot always quotes, so it works solo too.",
        chips: ["Multiplayer", "Live order book", marketGames.length + " stocks"],
        href: "/market",
        featured: true,
      } : null,
      {
        icon: ICON.code,
        name: "Market Simulation Coding",
        blurb: "Write a trading bot, run it on your own machine, and trade it against everyone else's for ten minutes.",
        chips: ["Multiplayer", "Python", "10 min"],
        href: "/market-sim-py",
      },
      {
        icon: ICON.terminal,
        name: "SWE Prep",
        blurb: "The same market, but you write Python in the browser and the server runs it. Nothing to install.",
        chips: ["Solo", "Sandboxed", "No setup"],
        href: "/swe-prep",
      },
      {
        icon: ICON.risk,
        name: "Risks",
        blurb: "Run a market-neutral book through a synthetic crash. Beta is published, the panic is not.",
        chips: ["Multiplayer", "Portfolio", "Drawdown scored"],
        href: "/risks",
      },
      {
        icon: ICON.news,
        name: "Headline Trading",
        blurb: "News hits the wire and the price moves. Read the story and take a position before the bell.",
        chips: ["Multiplayer", "5 min", "Futures"],
        href: "/headline",
      },
      {
        icon: ICON.stats,
        name: "5Os",
        blurb: "Estimate the five statistics of a hidden hand of cards, then trade your estimate against the room.",
        chips: ["Multiplayer", "Calibration", "5 rounds"],
        href: "/5os",
      },
      {
        icon: ICON.cards,
        name: "Poker Auction",
        blurb: "Bid sealed-envelope for cards, build the best hand, then trade the hands in a post-auction market.",
        chips: ["Teams", "Auction", "Pricing"],
        href: "/poker-auction",
      },
      {
        icon: ICON.calc,
        name: "Mental Math",
        blurb: "Timed arithmetic drills at the speed a trading floor expects. Configurable types and difficulty.",
        chips: ["Solo or group", "Timed", "Adjustable"],
        href: "/mental-math",
      },
      {
        icon: ICON.crash,
        name: "Crash Ledger",
        blurb: "Make a market on how real S&P 500 names behaved in past crashes. Quote a bid and an ask; the house trades against you at the true number.",
        chips: ["Market making", "6 crashes", "Head-to-head"],
        href: "/crash-ledger",
      },
    ].filter(Boolean);

    modes.forEach((m) => grid.appendChild(createCard(m)));

    // Instructor-created games sit after the built-in modes.
    (groups.other || []).forEach((game) => {
      grid.appendChild(createCard({
        icon: ICON.market,
        name: game.name,
        blurb: "A custom market set up for this session.",
        chips: ["Custom game"],
        href: `/trade/${game.symbol}`,
      }));
    });

    const modesEl = $("#factModes");
    if (modesEl) modesEl.textContent = String(modes.length);
    const stocksEl = $("#factStocks");
    if (stocksEl && marketGames.length) stocksEl.textContent = String(marketGames.length);
  }

  function createCard({ icon, name, blurb, chips, href, featured }) {
    const card = document.createElement("div");
    card.className = "mode-card" + (featured ? " mode-card-featured" : "");
    card.setAttribute("role", "link");
    card.setAttribute("tabindex", "0");

    card.innerHTML = `
      <div class="mode-head">
        <span class="mode-icon">${icon}</span>
        <h3>${esc(name)}</h3>
      </div>
      <p class="mode-blurb">${esc(blurb)}</p>
      <div class="mode-chips">${(chips || []).map((c) => `<span>${esc(c)}</span>`).join("")}</div>
      <span class="mode-go">Open</span>
    `;

    const go = () => {
      if (!isAuthed) window.location.href = "/login";
      else window.location.href = href;
    };
    card.addEventListener("click", go);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
    });
    return card;
  }

  // ---- Live market strip ----
  // Real mid prices off the same books the trading pages use, so the landing
  // page shows the product actually running rather than a static screenshot.
  const last = {};

  function midOf(book) {
    const bid = (book.bids || [])[0];
    const ask = (book.asks || [])[0];
    if (!bid || !ask) return null;
    return (parseFloat(bid.px) + parseFloat(ask.px)) / 2;
  }

  const money = (v) => "$" + v.toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });

  function buildTicker() {
    const box = $("#tickerRows");
    if (!box) return;
    const syms = (window.SYMBOLS || []).slice(0, 8);
    if (!syms.length) {
      box.innerHTML = `<p class="ticker-empty">No markets open right now.</p>`;
      return;
    }
    box.innerHTML = syms.map((s) => `
      <div class="ticker-row" data-sym="${esc(s)}">
        <span class="ticker-sym">${esc(s)}</span>
        <span class="ticker-px" id="tk-${esc(s)}">—</span>
        <span class="ticker-spread" id="tks-${esc(s)}"></span>
      </div>`).join("");
  }

  async function pollTicker() {
    const syms = (window.SYMBOLS || []).slice(0, 8);
    if (!syms.length) return;
    let ok = 0;

    await Promise.all(syms.map(async (sym) => {
      try {
        const book = await fetchJSON(`/book/${sym}`);
        const mid = midOf(book);
        const pxEl = $(`#tk-${sym}`);
        const spEl = $(`#tks-${sym}`);
        if (!pxEl) return;
        if (mid == null) {
          pxEl.textContent = "—";
          if (spEl) spEl.textContent = "no book";
          return;
        }
        ok += 1;

        const prev = last[sym];
        pxEl.textContent = money(mid);
        pxEl.classList.remove("tick-up", "tick-down");
        if (prev != null && mid !== prev) {
          pxEl.classList.add(mid > prev ? "tick-up" : "tick-down");
        }
        last[sym] = mid;

        if (spEl) {
          const bid = parseFloat(book.bids[0].px);
          const ask = parseFloat(book.asks[0].px);
          spEl.textContent = (ask - bid).toFixed(2) + " wide";
        }
      } catch { /* one bad symbol should not blank the strip */ }
    }));

    const status = $("#tickerStatus");
    if (status) {
      status.textContent = ok ? `${ok} live` : "waiting for quotes";
      status.classList.toggle("is-live", ok > 0);
    }
  }

  // ---- Auth UI ----
  async function initAuthUI() {
    const loginBox = $("#loginBox");
    const userBox = $("#userBox");
    const userNameEl = $("#userName");
    const adminLink = $("#adminLink");
    const heroCta = $("#heroPrimary");

    function showGuest() {
      isAuthed = false;
      loginBox?.classList.remove("hidden");
      userBox?.classList.add("hidden");
      if (adminLink) adminLink.style.display = "none";
      if (heroCta) { heroCta.textContent = "Start trading"; heroCta.href = "/signup"; }
    }

    function showUser(nameLike, isAdmin) {
      isAuthed = true;
      if (userNameEl) userNameEl.textContent = String(nameLike || "user");
      loginBox?.classList.add("hidden");
      userBox?.classList.remove("hidden");
      if (adminLink) adminLink.style.display = isAdmin ? "inline-block" : "none";
      if (heroCta) { heroCta.textContent = "Open the market"; heroCta.href = "/market"; }
    }

    try {
      const me = await fetchJSON("/me");
      showUser(me?.username || me?.name || me?.email || me?.id || "user", me?.is_admin || false);
    } catch {
      showGuest();
    }
  }

  buildGameGrid();
  buildTicker();
  initAuthUI();
  pollTicker();
  setInterval(pollTicker, 4000);
})();
