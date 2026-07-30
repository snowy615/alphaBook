(function () {
    "use strict";

    const $ = (sel, root = document) => root.querySelector(sel);

    const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);

    // Two decimals, always. A mid of $299.575 printed at three decimals just
    // read as a rounding bug on the board.
    const money = (n) => {
        if (n == null || !isFinite(n)) return "—";
        return "$" + Number(n).toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        });
    };
    const px2 = (n) => (n == null || !isFinite(n) ? "—" : Number(n).toFixed(2));

    const fetchJSON = async (url, init) => {
        const r = await fetch(url, { credentials: "include", ...init });
        if (!r.ok) throw new Error(String(r.status));
        const txt = await r.text();
        try { return JSON.parse(txt); } catch { return {}; }
    };

    const prices = {};
    const GAMES = window.MARKET_GAMES || [];
    const SYMS = window.SYMBOLS || [];

    const COMPANY = {
        AAPL: "Apple", MSFT: "Microsoft", NVDA: "NVIDIA", AMZN: "Amazon",
        GOOGL: "Alphabet", META: "Meta Platforms", TSLA: "Tesla",
    };

    // Prefer a real company name; fall back to the game's own name, but never
    // repeat the ticker back at the reader.
    function subtitleFor(game) {
        if (COMPANY[game.symbol]) return COMPANY[game.symbol];
        const cleaned = String(game.name || "").replace(/\s+Trading$/, "").trim();
        return cleaned && cleaned.toUpperCase() !== game.symbol.toUpperCase() ? cleaned : "";
    }

    function buildStockCards() {
        const grid = $("#stockGrid");
        if (!grid) return;
        grid.innerHTML = "";

        GAMES.forEach(game => {
            const sym = game.symbol;
            const card = document.createElement("div");
            card.className = "stock-card";
            card.setAttribute("role", "link");
            card.setAttribute("tabindex", "0");

            // Everything a trader glances at before clicking in: where it is,
            // what it costs to cross, and how deep the top of book is.
            card.innerHTML = `
              <div class="stock-head">
                <span class="stock-sym">${esc(sym)}</span>
                <span class="stock-name">${esc(subtitleFor(game))}</span>
              </div>
              <div class="stock-price" id="price-${esc(sym)}">—</div>
              <div class="stock-book">
                <div class="stock-quote bid">
                  <span class="stock-lbl">Bid</span>
                  <span class="stock-val" id="bid-${esc(sym)}">—</span>
                  <span class="stock-qty" id="bq-${esc(sym)}"></span>
                </div>
                <div class="stock-quote ask">
                  <span class="stock-lbl">Ask</span>
                  <span class="stock-val" id="ask-${esc(sym)}">—</span>
                  <span class="stock-qty" id="aq-${esc(sym)}"></span>
                </div>
                <div class="stock-quote">
                  <span class="stock-lbl">Spread</span>
                  <span class="stock-val" id="sp-${esc(sym)}">—</span>
                  <span class="stock-qty"></span>
                </div>
              </div>
              <div class="stock-depth" title="Top-of-book size, bid against ask">
                <div class="stock-depth-bar">
                  <span class="stock-depth-bid" id="db-${esc(sym)}" style="width:50%"></span>
                  <span class="stock-depth-ask" id="da-${esc(sym)}" style="width:50%"></span>
                </div>
                <span class="stock-depth-lbl">Top-of-book size</span>
              </div>
              <span class="stock-go">Trade ${esc(sym)}</span>
            `;

            const go = () => { window.location.href = `/trade/${sym}`; };
            card.addEventListener("click", go);
            card.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
            });

            grid.appendChild(card);
        });
    }

    async function fetchPrice(sym) {
        try {
            const book = await fetchJSON(`/book/${sym}`);
            const bids = book.bids || [];
            const asks = book.asks || [];
            const bestBid = bids.length ? parseFloat(bids[0].px) : null;
            const bestAsk = asks.length ? parseFloat(asks[0].px) : null;

            const priceEl = $(`#price-${sym}`);
            const set = (id, txt) => { const e = $(id); if (e) e.textContent = txt; };

            if (bestBid == null || bestAsk == null) {
                if (priceEl) priceEl.textContent = "—";
                set(`#bid-${sym}`, "—");
                set(`#ask-${sym}`, "—");
                set(`#sp-${sym}`, "no book");
                return false;
            }

            const mid = (bestBid + bestAsk) / 2;

            if (priceEl) {
                const prev = prices[sym];
                priceEl.textContent = money(mid);
                // Tick colour is the honest live signal here: it says the book
                // just moved, rather than inventing a daily percentage.
                priceEl.classList.remove("tick-up", "tick-down");
                if (prev != null && mid !== prev) {
                    priceEl.classList.add(mid > prev ? "tick-up" : "tick-down");
                }
                prices[sym] = mid;
            }

            set(`#bid-${sym}`, px2(bestBid));
            set(`#ask-${sym}`, px2(bestAsk));
            set(`#sp-${sym}`, px2(bestAsk - bestBid));
            set(`#bq-${sym}`, bids[0].qty != null ? `${bids[0].qty}` : "");
            set(`#aq-${sym}`, asks[0].qty != null ? `${asks[0].qty}` : "");

            const bq = Number(bids[0].qty) || 0;
            const aq = Number(asks[0].qty) || 0;
            const total = bq + aq;
            const bidPct = total ? (bq / total) * 100 : 50;
            const dbEl = $(`#db-${sym}`);
            const daEl = $(`#da-${sym}`);
            if (dbEl) dbEl.style.width = bidPct.toFixed(1) + "%";
            if (daEl) daEl.style.width = (100 - bidPct).toFixed(1) + "%";
            return true;
        } catch (e) {
            console.error(`Error fetching book for ${sym}:`, e);
            return false;
        }
    }

    async function updatePrices() {
        const results = await Promise.all(SYMS.map(fetchPrice));
        const live = results.filter(Boolean).length;
        const status = $("#marketStatus");
        const dot = $("#marketDot");
        if (status) {
            status.textContent = live
                ? `${live} market${live === 1 ? "" : "s"} live`
                : "waiting for quotes";
        }
        if (dot) dot.classList.toggle("is-live", live > 0);
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
            if (adminLink) adminLink.style.display = isAdmin ? "inline-block" : "none";
        }

        try {
            const me = await fetchJSON("/me");
            showUser(me?.username || me?.name || me?.email || me?.id || "user", me?.is_admin || false);
        } catch {
            showGuest();
        }
    }

    buildStockCards();
    initAuthUI();
    updatePrices();
    setInterval(updatePrices, 3000);
})();
