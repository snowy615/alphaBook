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
  const lastRef = Object.create(null);     // latest ref price per symbol
  let isAuthed = false;                    // login state
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
            <th>Ask&nbsp;Qty</th>
            <th>Ask&nbsp;Px</th>
            <th class="div">—</th>
            <th>Bid&nbsp;Px</th>
            <th>Bid&nbsp;Qty</th>
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

  // Render aligned ladder: asks (worst->best), mid row, bids (best->worst)
  function renderLadder(sym, book) {
    const body = document.getElementById(`ladder-body-${sym}`);
    if (!body) return;
    body.innerHTML = "";

    const asks = (book.asks || []).slice(0, DEPTH).sort((a,b)=>a.px-b.px); // low->high
    const bids = (book.bids || []).slice(0, DEPTH).sort((a,b)=>b.px-a.px); // high->low

    // asks block (worst -> best so best is closest to mid row)
    for (let i = asks.length - 1; i >= 0; i--) {
      const a = asks[i];
      const tr = document.createElement("tr");
      tr.className = "row-ask";
      tr.dataset.sym = sym;
      tr.dataset.px = a.px;
      tr.dataset.qty = a.qty;
      tr.dataset.side = "BUY";
      tr.innerHTML = `
        <td>${fmt(a.qty)}</td>
        <td>${fmt(a.px)}</td>
        <td class="div"></td>
        <td></td>
        <td></td>
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
      <td colspan="5">
        ${bestBid!=null && bestAsk!=null
          ? `Spread: ${fmt(sp)} • Mid: ${fmt(mid)} • Best Bid: ${fmt(bestBid)} • Best Ask: ${fmt(bestAsk)}`
          : `Waiting for depth…`}
      </td>`;
    body.appendChild(midtr);

    // bids block (best -> worst so best is closest to mid row)
    for (let i = 0; i < bids.length; i++) {
      const b = bids[i];
      const tr = document.createElement("tr");
      tr.className = "row-bid";
      tr.dataset.sym = sym;
      tr.dataset.px = b.px;
      tr.dataset.qty = b.qty;
      tr.dataset.side = "SELL";
      tr.innerHTML = `
        <td></td>
        <td></td>
        <td class="div"></td>
        <td>${fmt(b.px)}</td>
        <td>${fmt(b.qty)}</td>
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

  grid.addEventListener("click", (e) => {
    const tr = e.target && (e.target.closest("tr.row-ask") || e.target.closest("tr.row-bid"));
    if (!tr) return;
    const sym = tr.dataset.sym;
    const px = parseFloat(tr.dataset.px || "NaN");
    const side = tr.dataset.side || "BUY";
    if (!sym || !isFinite(px)) return;

    selSym.value = sym;
    inpPx.value = px.toFixed(4);
    inpQty.value ||= "1";
    const radio = form.querySelector(`input[name="side"][value="${side}"]`);
    if (radio) {
      radio.checked = true;
    }

    if (!isAuthed) { location.href = "/login"; return; }
    if (!dlg.open) dlg.showModal();
  });

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

      // ---- SUCCESS BRANCH (updated) ----
      const ack = JSON.parse(text);
      hint.textContent = `Placed! Order ${ack.order_id}. Trades: ${ack.trades?.length || 0}`;
      inpQty.value = "";

      // refresh My Orders and update this symbol’s ladder immediately
      if (typeof loadOrders === "function") loadOrders();
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
          borderWidth: 2,
          fill: false,
          pointRadius: 0,
          tension: 0.25
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { type: "linear", ticks: { callback: (v) => new Date(+v).toLocaleTimeString() }, grid: { display: false } },
          y: { ticks: { callback: (v) => fmt(v) } }
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

    $("#pos-qty") && ($("#pos-qty").textContent = fmt(qty));
    $("#pos-avg") && ($("#pos-avg").textContent = isFinite(avg) ? fmt(avg) : "--");
    $("#pos-notional") && ($("#pos-notional").textContent = fmt(notional));
    $("#pos-delta") && ($("#pos-delta").textContent = fmt(delta));
    $("#pnl-open") && ($("#pnl-open").textContent = fmt(pnlOpen));
    $("#pnl-day") && ($("#pnl-day").textContent = fmt(pnlDay));
    $("#cash") && ($("#cash").textContent = isFinite(cash) ? fmt(cash) : "--");
    $("#equity") && ($("#equity").textContent = isFinite(equity) ? fmt(equity) : "--");
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

  // ---------------- My Orders (user-scoped): list + cancel ----------------
  const ordersPanel = document.getElementById("orders-panel");
  const ordersBody  = document.getElementById("orders-body");
  const ordersMeta  = document.getElementById("orders-meta");
  const ordersCount = document.getElementById("orders-count");

  async function fetchOrders() {
    const r = await fetch("/me/orders", { credentials: "include" });
    if (!r.ok) throw new Error(String(r.status));
    const data = await r.json();
    return Array.isArray(data) ? data : (Array.isArray(data.orders) ? data.orders : []);
  }

  function renderOrders(rows) {
    if (!ordersBody) return;
    ordersBody.innerHTML = "";

    const list = Array.isArray(rows) ? rows : [];
    if (ordersCount) ordersCount.textContent = String(list.length);

    if (list.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="9" style="text-align:center;color:var(--muted);">No open orders</td>`;
      ordersBody.appendChild(tr);
      return;
    }

    for (const o of list) {
      const side   = String(o.side || "").toUpperCase();
      const qty    = +((o.qty ?? o.quantity) || 0);
      const filled = +((o.filled_qty ?? o.filled ?? 0) || 0);
      const remain = Math.max(qty - filled, 0);
      const cls    = side === "BUY" ? "row-bid" : "row-ask";

      const tr = document.createElement("tr");
      tr.className = cls;
      tr.innerHTML = `
        <td>${side || "--"}</td>
        <td>${o.symbol ?? o.sym ?? "--"}</td>
        <td>${fmt(o.price)}</td>
        <td>${fmt(qty)}</td>
        <td>${fmt(filled)}</td>
        <td>${fmt(remain)}</td>
        <td>${o.status ?? "--"}</td>
        <td>${o.created_at ? String(o.created_at).replace("T"," ").slice(0,19) : "--"}</td>
        <td style="text-align:center">
          <button class="btn ghost cancel" data-oid="${o.id}">Cancel</button>
        </td>
      `;
      ordersBody.appendChild(tr);
    }
  }

  // Event delegation for Cancel buttons (guarded)
  if (ordersBody) {
    ordersBody.addEventListener("click", async (e) => {
      const btn = e.target.closest && e.target.closest("button.cancel");
      if (!btn) return;
      const oid = btn.getAttribute("data-oid");
      if (!oid) return;

      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "Canceling…";
      try {
        const r = await fetch(`/me/orders/${encodeURIComponent(oid)}/cancel`, {
          method: "POST",
          credentials: "include",
        });
        if (!r.ok) throw new Error(String(r.status));
        await loadOrders(); // refresh after cancel
      } catch {
        btn.disabled = false;
        btn.textContent = original;
        alert("Cancel failed");
      }
    });
  }

  async function loadOrders() {
    if (!ordersPanel) return;
    try {
      const rows = await fetchOrders();
      renderOrders(rows);
      if (ordersMeta) ordersMeta.textContent = `updated ${new Date().toLocaleTimeString()}`;
    } catch {
      if (ordersMeta) ordersMeta.textContent = "not signed in";
      renderOrders([]);
    }
  }

  let _ordersTimer = null;
  function startOrdersRefresh() {
    clearInterval(_ordersTimer);
    setHidden(ordersPanel, false);
    loadOrders();
    _ordersTimer = setInterval(loadOrders, 4000);
  }
  function stopOrdersRefresh() {
    clearInterval(_ordersTimer);
    setHidden(ordersPanel, true);
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
      setHidden(ordersPanel, true);   // ✅ use the correct ID
      stopOrdersRefresh();
    }

    function showUser(nameLike) {
      isAuthed = true;
      if (userNameEl) userNameEl.textContent = String(nameLike || "user");
      setHidden(loginBox, true);
      setHidden(userBox, false);
      setHidden(acct, false);
      setHidden(ordersPanel, false);  // ✅ use the correct ID
      refreshAccount();
      startOrdersRefresh();           // poll orders from here
    }

    try {
      const me = await fetchJSON("/me");
      const nameLike = me?.username || me?.name || me?.email || me?.id || "user";
      showUser(nameLike);
    } catch {
      showGuest();
    }

    // keep only the account refresher; orders are handled by start/stop above
    setInterval(() => { if (isAuthed) refreshAccount(); }, 8000);
  }

  // Kick things off
  initAuthUI();
})();
