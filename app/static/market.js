(function () {
    "use strict";

    const $ = (sel, root = document) => root.querySelector(sel);
    // Two decimals, always — a mid of $299.575 printed as three decimals just
    // looked like a rounding bug on the board.
    const fmt = (n) => {
        if (n === null || n === undefined) return "--";
        const v = +n;
        if (!isFinite(v)) return "--";
        return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };

    const fetchJSON = async (url, init) => {
        const r = await fetch(url, { credentials: "include", ...init });
        if (!r.ok) throw new Error(String(r.status));
        const txt = await r.text();
        try { return JSON.parse(txt); } catch { return {}; }
    };

    const prices = {};
    const GAMES = window.MARKET_GAMES || [];
    const SYMS = window.SYMBOLS || [];

    function buildStockCards() {
        const grid = $("#stockGrid");
        if (!grid) return;
        grid.innerHTML = "";

        GAMES.forEach(game => {
            const card = document.createElement("div");
            card.className = "equity-card";
            card.setAttribute("role", "link");
            card.setAttribute("tabindex", "0");

            card.innerHTML = `
        <div class="equity-icon">${game.symbol}</div>
        <div class="equity-name">${game.name.replace(/\s+Trading$/, "")}</div>
        <div class="equity-price">
          <span class="equity-last" id="price-${game.symbol}">—</span>
          <span class="equity-change" id="change-${game.symbol}"></span>
        </div>
      `;

            const go = () => { window.location.href = `/trade/${game.symbol}`; };
            card.addEventListener("click", go);
            card.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
            });

            grid.appendChild(card);
            fetchPrice(game.symbol);
        });
    }

    async function fetchPrice(sym) {
        try {
            const book = await fetchJSON(`/book/${sym}`);
            const bids = book.bids || [];
            const asks = book.asks || [];

            let price = null;
            if (bids.length > 0 && asks.length > 0) {
                const bestBid = parseFloat(bids[0].px);
                const bestAsk = parseFloat(asks[0].px);
                price = (bestBid + bestAsk) / 2;
            }

            const priceEl = $(`#price-${sym}`);
            const changeEl = $(`#change-${sym}`);

            if (priceEl) {
                if (price !== null && price !== undefined) {
                    const oldPrice = prices[sym];
                    prices[sym] = price;

                    priceEl.textContent = `$${fmt(price)}`;

                    if (changeEl && oldPrice !== undefined) {
                        const change = ((price - oldPrice) / oldPrice) * 100;
                        const changeText = (change >= 0 ? "+" : "") + change.toFixed(2) + "%";
                        changeEl.textContent = changeText;
                        changeEl.className = "equity-change " + (change >= 0 ? "positive" : "negative");
                    }
                } else {
                    priceEl.textContent = "—";
                    if (changeEl) {
                        changeEl.textContent = "no orders yet";
                        changeEl.className = "equity-change";
                    }
                }
            }
        } catch (e) {
            console.error(`Error fetching price for ${sym}:`, e);
        }
    }

    function updatePrices() {
        SYMS.forEach(sym => fetchPrice(sym));
    }

    // Auth UI
    async function initAuthUI() {
        const loginBox = $("#loginBox");
        const userBox = $("#userBox");
        const userNameEl = $("#userName");
        const adminLink = $("#adminLink");

        function showGuest() {
            loginBox?.classList.remove("hidden");
            userBox?.classList.add("hidden");
            if (adminLink) adminLink.style.display = "none";
        }

        function showUser(nameLike, isAdmin) {
            if (userNameEl) userNameEl.textContent = String(nameLike || "user");
            loginBox?.classList.add("hidden");
            userBox?.classList.remove("hidden");
            if (adminLink) {
                adminLink.style.display = isAdmin ? "inline-block" : "none";
            }
        }

        try {
            const me = await fetchJSON("/me");
            const nameLike = me?.username || me?.name || me?.email || me?.id || "user";
            const isAdmin = me?.is_admin || false;
            showUser(nameLike, isAdmin);
        } catch {
            showGuest();
        }
    }

    // Initialize
    buildStockCards();
    initAuthUI();
    setInterval(updatePrices, 3000);
})();
