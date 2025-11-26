(function () {
  "use strict";

  // ---------- tiny helpers ----------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
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
  const setHidden = (el, hidden) => { if (!el) return; el.classList[hidden ? "add" : "remove"]("hidden"); };

  // ---------- globals from template ----------
  const grid = $("#grid");
  const DEPTH = window.TOP_DEPTH || 10;
  const SYMS = (window.SYMBOLS || []).map((s) => String(s || "").toUpperCase());

  // ---------- app state ----------
  const lastRef = Object.create(null);
  let isAuthed = false;
  let pnlChart = null;

  // ---------- build cards & connect streams ----------
  for (const sym of SYMS) {
    const el = document.createElement("article");
    el.className = "card";
    el.innerHTML = `
      <div class="head">
        <div class="sym">${sym}</div>
        <div class="ref" id="ref-${sym}">--</div>
      </div>
      <div class="meta">
        <span id="meta-${sym}">waiting for data…</span>
      </div>

      <table class="ladder" id="ladder-${sym}">
        <thead>
          <tr>    
            <th></th>
            <th>Ask&nbsp;Qty</th>
            <th>Ask&nbsp;Px</th>
            <th class="div">—</th>
            <th>Bid&nbsp;Px</th>
            <th>Bid&nbsp;Qty</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="ladder-body-${sym}"></tbody>
      </table>
    `;
    grid.appendChild(el);
    connect(sym);
  }

  function setMeta(sym, text) {
    const el = document.getElementById(`meta-${sym}`);
    if (el) el.textContent = text;
  }

  function setRef(sym, price) {
    const el = document.getElementById(`ref-${sym}`);
    if (!el) return;
    const old = parseFloat(el.dataset.v || "NaN");
    el.dataset.v = price;
    lastRef[sym] = price;
    el.textContent = fmt(price);
    if (!isNaN(old) && !isNaN(price)) {
      el.classList.remove("up", "down", "blink");
      el.classList.add(price > old ? "up" : price < old ? "down" : "");
      el.classList.add("blink");
      setTimeout(() => el.classList.remove("blink"), 400);
    }
  }

  function renderLadder(sym, book) {
    const body = document.getElementById(`ladder-body-${sym}`);
    if (!body) return;
    body.innerHTML = "";

    const asks = (book.asks || []).slice(0, DEPTH).sort((a,b)=>a.px-b.px);
    const bids = (book.bids || []).slice(0, DEPTH).sort((a,b)=>b.px-a.px);

    // asks block
    for (let i = asks.length - 1; i >= 0; i--) {
      const a = asks[i];
      const tr = document.createElement("tr");
      tr.className = "row-ask";
      tr.innerHTML = `
        <td class="action-cell">
          <button class="trade-btn buy-btn" data-sym="${sym}" data-side="BUY" data-px="${a.px}">Buy</button>
        </td>
        <td>${fmt(a.qty)}</td>
        <td>${fmt(a.px)}</td>
        <td class="div"></td>
        <td></td>
        <td></td>
        <td class="action-cell"></td>
      `;
      body.appendChild(tr);
    }

    const bestAsk = asks[0]?.px ?? null;
    const bestBid = bids[0]?.px ?? null;
    const sp = (bestAsk!=null && bestBid!=null) ? (bestAsk - bestBid) : null;
    const mid = (bestAsk!=null && bestBid!=null) ? (bestAsk + bestBid)/2 : lastRef[sym] ?? null;
    const midtr = document.createElement("tr");
    midtr.className = "midrow";
    midtr.innerHTML = `
      <td colspan="7">
        ${bestBid!=null && bestAsk!=null
          ? `Spread: ${fmt(sp)} • Mid: ${fmt(mid)} • Best Bid: ${fmt(bestBid)} • Best Ask: ${fmt(bestAsk)}`
          : `Waiting for depth…`}
      </td>`;
    body.appendChild(midtr);

    // bids block
    for (let i = 0; i < bids.length; i++) {
      const b = bids[i];
      const tr = document.createElement("tr");
      tr.className = "row-bid";
      tr.innerHTML = `
        <td class="action-cell"></td>
        <td></td>
        <td></td>
        <td class="div"></td>
        <td>${fmt(b.px)}</td>
        <td>${fmt(b.qty)}</td>
        <td class="action-cell">
          <button class="trade-btn sell-btn" data-sym="${sym}" data-side="SELL" data-px="${b.px}">Sell</button>
        </td>
      `;
      body.appendChild(tr);
      }
  }

  function connect(sym) {
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/book/${sym}`;
    const ws = new WebSocket(url);
    ws.onopen = () => setMeta(sym, "connected • live");
    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data.type === "snapshot") {
          renderLadder(sym, data.book);
          setRef(sym, data.ref_price);
          setMeta(sym, `updated ${new Date().toLocaleTimeString()}`);
        }
      } catch (e) {
        console.error(e);
      }
    };
    ws.onclose = () => {
      setMeta(sym, "disconnected — retrying…");
      setTimeout(() => connect(sym), 1500);
    };
    ws.onerror = () => ws.close();
  }

  // ---------- Order modal & submission ----------
  const dlg = $("#order-modal");
  const openBtn = $("#open-order");
  const closeBtn = $("#close-order");
  const cancelBtn = $("#cancel-order");
  const form = $("#order-form");
  const selSym = $("#ord-symbol");
  const inpPx = $("#ord-price");
  const inpQty = $("#ord-qty");
  const hint = $("#ord-hint");

  (window.SYMBOLS || []).forEach((s) => {
    const opt = document.createElement("option");
    opt.value = opt.textContent = String(s || "").toUpperCase();
    selSym.appendChild(opt);
  });

  function prefill(sideFromClick) {
    const sym = selSym.value || SYMS[0] || "";
    const ref = lastRef[sym];
    if (ref != null && isFinite(ref)) inpPx.value = Number(ref).toFixed(4);
    if (!inpQty.value) inpQty.value = "1";
    if (sideFromClick) {
      const radio = form.querySelector(`input[name="side"][value="${sideFromClick}"]`);
      if (radio) radio.checked = true;
    }
    hint.textContent = `Tip: price defaults to current ref for ${sym}.`;
  }

  openBtn?.addEventListener("click", () => {
    if (!isAuthed) { location.href = "/login"; return; }
    if (!dlg.open) { prefill(); dlg.showModal(); }
  });
  closeBtn?.addEventListener("click", () => dlg.close());
  cancelBtn?.addEventListener("click", () => dlg.close());
  selSym?.addEventListener("change", prefill);
/////////////////-- Quick Trade Slider Modal ----
// Add this to app/static/app.js - replace the grid click handler

// ---- Quick Trade Slider Modal ----
  const quickTradeDlg = document.createElement('dialog');
  quickTradeDlg.className = 'modal';
  quickTradeDlg.id = 'quick-trade-modal';
  quickTradeDlg.innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <h3 id="qt-title">Quick Trade</h3>
        <button class="icon-close" id="qt-close">✕</button>
      </div>
      <div class="panel-body">
        <div class="qt-info">
          <div class="qt-side" id="qt-side-label">BUY</div>
          <div class="qt-symbol" id="qt-symbol">AAPL</div>
          <div class="qt-price-label">at</div>
          <div class="qt-price" id="qt-price">150.00</div>
        </div>
        
        <div class="field">
          <label>Quantity: <span id="qt-qty-display">1</span></label>
          <input type="range" id="qt-qty-slider" min="1" max="100" value="1" step="1">
          <div class="slider-labels">
            <span>1</span>
            <span>50</span>
            <span>100</span>
          </div>
        </div>
        
        <div class="qt-summary">
          <div class="qt-summary-row">
            <span>Notional:</span>
            <span id="qt-notional">$150.00</span>
          </div>
        </div>
        
        <div class="small" id="qt-hint"></div>
      </div>
      <div class="panel-foot">
        <button type="button" class="btn ghost" id="qt-cancel">Cancel</button>
        <button type="button" class="btn primary" id="qt-submit">Submit Order</button>
      </div>
    </div>
  `;
  document.body.appendChild(quickTradeDlg);

  let qtState = {
    symbol: '',
    side: '',
    price: 0,
    qty: 1,
    maxQty: 100
  };

  const qtSlider = document.getElementById('qt-qty-slider');
  const qtQtyDisplay = document.getElementById('qt-qty-display');
  const qtNotional = document.getElementById('qt-notional');
  const qtHint = document.getElementById('qt-hint');

  function updateQuickTradeDisplay() {
    qtQtyDisplay.textContent = qtState.qty;
    const notional = qtState.price * qtState.qty;
    qtNotional.textContent = `$${fmt(notional)}`;
  }

  qtSlider?.addEventListener('input', (e) => {
    qtState.qty = parseInt(e.target.value);
    updateQuickTradeDisplay();
  });

  document.getElementById('qt-close')?.addEventListener('click', () => {
    quickTradeDlg.close();
  });

  document.getElementById('qt-cancel')?.addEventListener('click', () => {
    quickTradeDlg.close();
  });

  document.getElementById('qt-submit')?.addEventListener('click', async () => {
    if (!isAuthed) {
      qtHint.textContent = "Please log in to place orders.";
      setTimeout(() => (location.href = "/login"), 800);
      return;
    }

    const payload = {
      symbol: qtState.symbol,
      side: qtState.side,
      price: String(qtState.price.toFixed(4)),
      qty: String(qtState.qty)
    };

    qtHint.textContent = "Submitting...";
    document.getElementById('qt-submit').disabled = true;

    try {
      const res = await fetch("/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "same-origin"
      });

      if (res.status === 401) {
        qtHint.textContent = "You need to log in to place orders.";
        setTimeout(() => (location.href = "/login"), 800);
        return;
      }

      const text = await res.text();
      if (!res.ok) {
        qtHint.textContent = "Error: " + (text || res.status);
        document.getElementById('qt-submit').disabled = false;
        return;
      }

      const ack = JSON.parse(text);
      qtHint.textContent = `Success! Order ${ack.order_id}. Trades: ${ack.trades?.length || 0}`;

      if (typeof loadOrders === "function") loadOrders();
      if (ack?.snapshot) renderLadder(qtState.symbol, ack.snapshot);

      setTimeout(() => {
        quickTradeDlg.close();
        qtHint.textContent = "";
        document.getElementById('qt-submit').disabled = false;
      }, 700);
    } catch (err) {
      console.error(err);
      qtHint.textContent = "Network error submitting order.";
      document.getElementById('qt-submit').disabled = false;
    }
  });

  function openQuickTrade(symbol, side, price, maxQty) {
    if (!isAuthed) {
      location.href = "/login";
      return;
    }

    // Convert maxQty to a reasonable integer, minimum 1
    const max = Math.max(1, Math.floor(parseFloat(maxQty) || 100));

    qtState = {
      symbol: symbol,
      side: side,
      price: price,
      qty: 1,
      maxQty: max
    };

    document.getElementById('qt-title').textContent = `Quick ${side}`;
    document.getElementById('qt-side-label').textContent = side;
    document.getElementById('qt-side-label').className = `qt-side ${side.toLowerCase()}`;
    document.getElementById('qt-symbol').textContent = symbol;
    document.getElementById('qt-price').textContent = fmt(price);

    // Update slider max and reset to 1
    qtSlider.max = max;
    qtSlider.value = 1;
    qtState.qty = 1;

    // Update slider labels
    const sliderLabels = document.querySelector('.slider-labels');
    if (sliderLabels) {
      const mid = Math.floor(max / 2);
      sliderLabels.innerHTML = `
        <span>1</span>
        <span>${mid}</span>
        <span>${max}</span>
      `;
    }

    qtHint.textContent = "";

    updateQuickTradeDisplay();
    quickTradeDlg.showModal();
  }

  // Update the renderLadder function to add Buy/Sell buttons
  function renderLadder(sym, book) {
    const body = document.getElementById(`ladder-body-${sym}`);
    if (!body) return;
    body.innerHTML = "";

    const asks = (book.asks || []).slice(0, DEPTH).sort((a,b)=>a.px-b.px);
    const bids = (book.bids || []).slice(0, DEPTH).sort((a,b)=>b.px-a.px);

    // asks block
    for (let i = asks.length - 1; i >= 0; i--) {
      const a = asks[i];
      const tr = document.createElement("tr");
      tr.className = "row-ask";
      tr.innerHTML = `
        <td class="action-cell">
          <button class="trade-btn buy-btn" data-sym="${sym}" data-side="BUY" data-px="${a.px}" data-qty="${a.qty}">Yours</button>
        </td>
        <td>${fmt(a.qty)}</td>
        <td>${fmt(a.px)}</td>
        <td class="div"></td>
        <td></td>
        <td></td>
        <td class="action-cell"></td>
      `;
      body.appendChild(tr);
    }

    const bestAsk = asks[0]?.px ?? null;
    const bestBid = bids[0]?.px ?? null;
    const sp = (bestAsk!=null && bestBid!=null) ? (bestAsk - bestBid) : null;
    const mid = (bestAsk!=null && bestBid!=null) ? (bestAsk + bestBid)/2 : lastRef[sym] ?? null;
    const midtr = document.createElement("tr");
    midtr.className = "midrow";
    midtr.innerHTML = `
      <td colspan="7">
        ${bestBid!=null && bestAsk!=null
          ? `Spread: ${fmt(sp)} • Mid: ${fmt(mid)} • Best Bid: ${fmt(bestBid)} • Best Ask: ${fmt(bestAsk)}`
          : `Waiting for depth…`}
      </td>`;
    body.appendChild(midtr);

    // bids block
    for (let i = 0; i < bids.length; i++) {
      const b = bids[i];
      const tr = document.createElement("tr");
      tr.className = "row-bid";
      tr.innerHTML = `
        <td class="action-cell"></td>
        <td></td>
        <td></td>
        <td class="div"></td>
        <td>${fmt(b.px)}</td>
        <td>${fmt(b.qty)}</td>
        <td class="action-cell">
          <button class="trade-btn sell-btn" data-sym="${sym}" data-side="SELL" data-px="${b.px}" data-qty="${b.qty}">Mine</button>
        </td>
      `;
      body.appendChild(tr);
    }
  }

  // Event delegation for trade buttons
  grid.addEventListener('click', (e) => {
    const btn = e.target.closest('.trade-btn');
    if (!btn) return;

    const sym = btn.dataset.sym;
    const side = btn.dataset.side;
    const px = parseFloat(btn.dataset.px);
    const qty = btn.dataset.qty;

    if (sym && side && isFinite(px) && qty) {
      openQuickTrade(sym, side, px, qty);
    }
  });
  ///////////////

  form?.addEventListener("submit", async (e) => {
    e.preventDefault();

    const fd = new FormData(form);
    const side = String(fd.get("side") || "BUY").toUpperCase();
    const priceNum = parseFloat(inpPx.value);
    const qtyNum = parseFloat(inpQty.value);

    if (!isFinite(priceNum) || !isFinite(qtyNum) || qtyNum <= 0) {
      hint.textContent = "Please enter a valid price and quantity.";
      return;
    }

    const payload = {
      user_id: "browser",
      symbol: selSym.value,
      side,
      price: String(priceNum.toFixed(4)),
      qty: String(qtyNum)
    };

    hint.textContent = "Submitting…";
    try {
      const res = await fetch("/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "same-origin"
      });

      if (res.status === 401) {
        hint.textContent = "You need to log in to place orders.";
        setTimeout(() => (location.href = "/login"), 800);
        return;
      }

      const text = await res.text();
      if (!res.ok) { hint.textContent = "Error: " + (text || res.status); return; }

      const ack = JSON.parse(text);
      hint.textContent = `Placed! Order ${ack.order_id}. Trades: ${ack.trades?.length || 0}`;
      inpQty.value = "";

      loadOrders();
      refreshAccount();
      if (ack?.snapshot) renderLadder(selSym.value, ack.snapshot);

      setTimeout(() => dlg.close(), 700);
    } catch (err) {
      console.error(err);
      hint.textContent = "Network error submitting order.";
    }
  });

  // ---------- Account (positions + P&L) ----------
  function initPnlChart() {
    if (pnlChart) return pnlChart;
    const canvas = $("#pnlChart");
    if (!canvas || !window.Chart) return null;

    pnlChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        datasets: [{
          label: "P&L",
          data: [],
          borderColor: "#6c5ce7",
          backgroundColor: "rgba(108, 92, 231, 0.1)",
          borderWidth: 2,
          fill: true,
          pointRadius: 0,
          tension: 0.4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            mode: 'index',
            intersect: false,
          }
        },
        scales: {
          x: {
            type: "linear",
            ticks: {
              callback: (v) => new Date(+v).toLocaleTimeString(),
              color: "#9aa3b2"
            },
            grid: {
              color: "rgba(255, 255, 255, 0.05)"
            }
          },
          y: {
            ticks: {
              callback: (v) => fmt(v),
              color: "#9aa3b2"
            },
            grid: {
              color: "rgba(255, 255, 255, 0.05)"
            }
          }
        }
      }
    });
    return pnlChart;
  }

  function normalizePnlSeries(raw) {
    const pts = raw?.points || raw?.series || raw || [];
    const out = [];
    for (const p of pts) {
      if (!p) continue;
      if (Array.isArray(p) && p.length >= 2) {
        const ts = +p[0], y = +p[1];
        if (isFinite(ts) && isFinite(y)) out.push({ x: ts, y });
      } else if (typeof p === "object") {
        const ts = +(p.t ?? p.ts ?? p.time ?? p.x ?? Date.now());
        const y  = +(p.pnl ?? p.y ?? p.value ?? p.open ?? p.closed ?? p[1] ?? NaN);
        if (isFinite(ts) && isFinite(y)) out.push({ x: ts, y });
      }
    }
    return out.sort((a,b)=>a.x-b.x);
  }

  function renderSummary(sum) {
    const totals = sum?.totals || sum || {};
    const qty = +(totals.qty ?? totals.position_qty ?? 0);
    const notional = +(totals.notional ?? totals.gross ?? 0);
    const delta = +(totals.delta ?? 0);
    const pnlOpen = +(totals.pnl_open ?? totals.unrealized ?? 0);
    const pnlDay = +(totals.pnl_day ?? totals.daily ?? 0);
    const avg = +(totals.avg_cost ?? totals.avg ?? NaN);
    const cash = +(totals.cash ?? NaN);
    const equity = +(totals.equity ?? (isFinite(cash) ? cash + pnlOpen : NaN));

    function setStatValue(id, value, colorize = false) {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = fmt(value);

      if (colorize && isFinite(value)) {
        el.classList.remove("positive", "negative");
        if (value > 0) {
          el.classList.add("positive");
        } else if (value < 0) {
          el.classList.add("negative");
        }
      }
    }

    setStatValue("pos-qty", qty);
    setStatValue("pos-avg", isFinite(avg) ? avg : null);
    setStatValue("pos-notional", notional);
    setStatValue("pos-delta", delta);
    setStatValue("pnl-open", pnlOpen, true);
    setStatValue("pnl-day", pnlDay, true);
    setStatValue("cash", isFinite(cash) ? cash : null);
    setStatValue("equity", isFinite(equity) ? equity : null);
  }

  async function refreshAccount() {
    if (!isAuthed) return;
    try {
      const summary = await fetchJSON("/me/summary");
      renderSummary(summary);
    } catch {}
    try {
      const seriesRaw = await fetchJSON("/me/pnl");
      const series = normalizePnlSeries(seriesRaw);
      const chart = initPnlChart();
      if (chart) {
        chart.data.datasets[0].data = series;
        chart.update("none");
      }
    } catch {}
  }

  // ---------------- My Open Orders ----------------
  const ordersPanel = $("#orders-panel");
  const ordersBody = $("#orders-body");
  const ordersMeta = $("#orders-meta");
  const ordersCount = $("#orders-count");

  function renderOrders(rows) {
    if (!ordersBody) return;
    ordersBody.innerHTML = "";

    const arr = Array.isArray(rows) ? rows : [];
    ordersCount && (ordersCount.textContent = String(arr.length));

    if (arr.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="9" style="text-align:center;color:var(--muted);">No open orders</td>`;
      ordersBody.appendChild(tr);
      return;
    }

    for (const r of arr) {
      const qty = +(r.qty ?? r.quantity ?? 0);
      const filled = +(r.filled_qty ?? r.filled ?? r.executed_qty ?? 0);
      const remain = Math.max(qty - filled, 0);
      const side = String(r.side || "").toUpperCase();

      const tr = document.createElement("tr");
      tr.className = side === "BUY" ? "row-bid" : "row-ask";
      tr.innerHTML = `
        <td>${side || "--"}</td>
        <td>${r.symbol ?? r.sym ?? "--"}</td>
        <td>${fmt(r.price)}</td>
        <td>${fmt(qty)}</td>
        <td>${fmt(filled)}</td>
        <td>${fmt(remain)}</td>
        <td>${r.status ?? "--"}</td>
        <td>${r.created_at ? String(r.created_at).replace("T"," ").slice(0,19) : "--"}</td>
        <td style="text-align:center">
          <button class="btn ghost cancel-btn" data-oid="${r.id}">Cancel</button>
        </td>
      `;
      ordersBody.appendChild(tr);
    }

    // Add event listeners to cancel buttons
    ordersBody.querySelectorAll(".cancel-btn").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();

        const oid = btn.getAttribute("data-oid");
        if (!oid) return;

        btn.disabled = true;
        btn.textContent = "Canceling…";

        try {
          const res = await fetch(`/me/orders/${encodeURIComponent(oid)}/cancel`, {
            method: "POST",
            credentials: "include"
          });

          if (!res.ok) {
            throw new Error("Cancel failed");
          }

          // Refresh orders and account after successful cancel
          await loadOrders();
          await refreshAccount();
        } catch (err) {
          console.error("Cancel error:", err);
          btn.disabled = false;
          btn.textContent = "Cancel";
          alert("Failed to cancel order");
        }
      });
    });
  }

  async function loadOrders() {
    if (!ordersMeta) return;
    try {
      const res = await fetch("/me/orders", { credentials: "include" });
      if (!res.ok) {
        ordersMeta.textContent = "not signed in";
        setHidden(ordersPanel, true);
        renderOrders([]);
        return;
      }
      const rows = await res.json();
      renderOrders(rows);
      ordersMeta.textContent = `updated ${new Date().toLocaleTimeString()}`;
      setHidden(ordersPanel, !isAuthed);
    } catch {
      ordersMeta.textContent = "error loading";
    }
  }

  // ---------- Auth header UI ----------
  async function initAuthUI() {
    const loginBox = $("#loginBox");
    const userBox = $("#userBox");
    const userNameEl = $("#userName");
    const acct = $("#account");

    function showGuest() {
      isAuthed = false;
      setHidden(loginBox, false);
      setHidden(userBox, true);
      setHidden(acct, true);
      setHidden(ordersPanel, true);
    }

    function showUser(nameLike) {
      isAuthed = true;
      if (userNameEl) userNameEl.textContent = String(nameLike || "user");
      setHidden(loginBox, true);
      setHidden(userBox, false);
      setHidden(acct, false);
      setHidden(ordersPanel, false);
      refreshAccount();
      loadOrders();
    }

    try {
      const me = await fetchJSON("/me");
      const nameLike = me?.username || me?.name || me?.email || me?.id || "user";
      showUser(nameLike);
    } catch {
      showGuest();
    }

    setInterval(() => { if (isAuthed) refreshAccount(); }, 5000);
    setInterval(() => { if (isAuthed) loadOrders(); }, 4000);
  }

  initAuthUI();
})();