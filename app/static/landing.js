(function () {
  "use strict";

  // Utility helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const fmt = (n) => {
    if (n === null || n === undefined) return "--";
    const v = +n;
    if (!isFinite(v)) return "--";
    if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
    return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  };

  const fetchJSON = async (url, init) => {
    const r = await fetch(url, { credentials: "include", ...init });
    if (!r.ok) throw new Error(String(r.status));
    const txt = await r.text();
    try { return JSON.parse(txt); } catch { return {}; }
  };

  let isAuthed = false;
  const prices = {};

  // Build equity cards
  function buildEquityCards() {
    const grid = $("#equityGrid");
    if (!grid) return;

    grid.innerHTML = "";

    const SYMS = window.SYMBOLS || [];

    SYMS.forEach(sym => {
      const card = document.createElement("div");
      card.className = "equity-card";
      card.innerHTML = `
        <div class="equity-icon">${sym.charAt(0)}</div>
        <div class="equity-name">${sym}</div>
        <div class="equity-price" id="price-${sym}">Loading...</div>
        <div class="equity-change" id="change-${sym}">--</div>
      `;

      card.addEventListener("click", () => {
        if (!isAuthed) {
          // Redirect to login if not authenticated
          window.location.href = "/login";
        } else {
          // Navigate to trading page
          window.location.href = `/trade/${sym}`;
        }
      });

      grid.appendChild(card);

      // Fetch initial price
      fetchPrice(sym);
    });
  }

  async function fetchPrice(sym) {
    try {
      const data = await fetchJSON(`/reference/${sym}`);
      const price = data.price;

      const priceEl = $(`#price-${sym}`);
      const changeEl = $(`#change-${sym}`);

      if (priceEl && price !== null && price !== undefined) {
        const oldPrice = prices[sym];
        prices[sym] = price;

        priceEl.textContent = `$${fmt(price)}`;

        if (oldPrice !== undefined) {
          const change = ((price - oldPrice) / oldPrice) * 100;
          const changeText = (change >= 0 ? "+" : "") + change.toFixed(2) + "%";
          changeEl.textContent = changeText;
          changeEl.className = "equity-change " + (change >= 0 ? "positive" : "negative");
        }
      }
    } catch (e) {
      console.error(`Error fetching price for ${sym}:`, e);
    }
  }

  function updatePrices() {
    const SYMS = window.SYMBOLS || [];
    SYMS.forEach(sym => fetchPrice(sym));
  }

  // Auth UI
  async function initAuthUI() {
    const loginBox = $("#loginBox");
    const userBox = $("#userBox");
    const userNameEl = $("#userName");

    function showGuest() {
      isAuthed = false;
      loginBox?.classList.remove("hidden");
      userBox?.classList.add("hidden");
    }

    function showUser(nameLike) {
      isAuthed = true;
      if (userNameEl) userNameEl.textContent = String(nameLike || "user");
      loginBox?.classList.add("hidden");
      userBox?.classList.remove("hidden");
    }

    try {
      const me = await fetchJSON("/me");
      const nameLike = me?.username || me?.name || me?.email || me?.id || "user";
      showUser(nameLike);
    } catch {
      showGuest();
    }
  }

  // Initialize
  buildEquityCards();
  initAuthUI();

  // Update prices every 3 seconds
  setInterval(updatePrices, 3000);
})();