(function () {
  "use strict";

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

  const SYMBOL = window.SYMBOL;
  const DEPTH = window.TOP_DEPTH || 10;
  const IS_CUSTOM_GAME = window.SYMBOL.startsWith('GAME');
  let isAuthed = false;
  let lastRef = null;
  let lastMid = null;

  // WebSocket connection
  function connectWS() {
    const wsProto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${wsProto}://${location.host}/ws/book/${SYMBOL}`);

    ws.onopen = () => setMeta("connected • live");

    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data.type === "snapshot") {
          renderLadder(data.book);
          setRef(data.ref_price);
          setMeta(`updated ${new Date().toLocaleTimeString()}`);
          updatePosition();
        }
      } catch (e) {
        console.error(e);
      }
    };

    ws.onclose = () => {
      setMeta("disconnected — retrying…");
      setTimeout(connectWS, 1500);
    };

    ws.onerror = () => ws.close();
  }

  function setMeta(text) {
    const el = $(`#meta-${SYMBOL}`);
    if (el) el.textContent = text;
  }

  function setRef(price) {
    const el = $(`#ref-${SYMBOL}`);
    if (!el) return; // Element doesn't exist for custom games

    // For custom games, compare against last mid price
    const old = IS_CUSTOM_GAME ? lastMid : parseFloat(el.dataset.v || "NaN");

    el.dataset.v = price;
    lastRef = price;

    if (IS_CUSTOM_GAME) {
      lastMid = price;
    }

    el.textContent = fmt(price);

    if (!isNaN(old) && !isNaN(price)) {
      el.classList.remove("up", "down", "blink");
      if (price > old) {
        el.classList.add("up");
      } else if (price < old) {
        el.classList.add("down");
      }
      // If price === old, don't add any class
      el.classList.add("blink");
      setTimeout(() => el.classList.remove("blink"), 400);
    }
  }

  function renderLadder(book) {
    const body = $(`#ladder-body-${SYMBOL}`);
    if (!body) return;

    console.log('renderLadder called with book:', book); // DEBUG

    body.innerHTML = "";

    const asks = (book.asks || []).slice(0, DEPTH).sort((a,b)=>parseFloat(a.px)-parseFloat(b.px));
    const bids = (book.bids || []).slice(0, DEPTH).sort((a,b)=>parseFloat(b.px)-parseFloat(a.px));

    console.log('Processed asks:', asks, 'bids:', bids); // DEBUG

    // Calculate mid price from order book
    const bestAsk = asks[0] ? parseFloat(asks[0].px) : null;
    const bestBid = bids[0] ? parseFloat(bids[0].px) : null;
    const mid = (bestAsk !== null && bestBid !== null) ? (bestAsk + bestBid) / 2 : null;

    // For custom games, update the price display with mid price
    if (IS_CUSTOM_GAME && mid !== null) {
      setRef(mid);
    }

    // Asks block
    for (let i = asks.length - 1; i >= 0; i--) {
      const a = asks[i];
      // Ensure valid values
      const askPx = a.px || a.price || 0;
      const askQty = a.qty || a.quantity || 0;

      const tr = document.createElement("tr");
      tr.className = "row-ask";
      tr.innerHTML = `
        <td class="action-cell">
          <button class="trade-btn buy-btn" data-side="BUY" data-px="${askPx}" data-qty="${askQty}">Buy</button>
        </td>
        <td>${fmt(askQty)}</td>
        <td>${fmt(askPx)}</td>
        <td class="div"></td>
        <td></td>
        <td></td>
        <td class="action-cell"></td>
      `;
      body.appendChild(tr);
    }

    // Mid row
    const sp = (bestAsk!=null && bestBid!=null) ? (bestAsk - bestBid) : null;

    const midtr = document.createElement("tr");
    midtr.className = "midrow";
    midtr.innerHTML = `
      <td colspan="7">
        ${bestBid!=null && bestAsk!=null
          ? `Spread: ${fmt(sp)} • Mid: ${fmt(mid)} • Best Bid: ${fmt(bestBid)} • Best Ask: ${fmt(bestAsk)}`
          : `Waiting for depth…`}
      </td>`;
    body.appendChild(midtr);

    // Bids block
    for (let i = 0; i < bids.length; i++) {
      const b = bids[i];
      // Ensure valid values
      const bidPx = b.px || b.price || 0;
      const bidQty = b.qty || b.quantity || 0;

      const tr = document.createElement("tr");
      tr.className = "row-bid";
      tr.innerHTML = `
        <td class="action-cell"></td>
        <td></td>
        <td></td>
        <td class="div"></td>
        <td>${fmt(bidPx)}</td>
        <td>${fmt(bidQty)}</td>
        <td class="action-cell">
          <button class="trade-btn sell-btn" data-side="SELL" data-px="${bidPx}" data-qty="${bidQty}">Sell</button>
        </td>
      `;
      body.appendChild(tr);
    }
  }

  // Quick trade modal
  const quickTradeDlg = $("#quick-trade-modal");
  const qtSlider = $("#qt-qty-slider");
  const qtQtyDisplay = $("#qt-qty-display");
  const qtNotional = $("#qt-notional");
  const qtHint = $("#qt-hint");

  let qtState = { side: '', price: 0, qty: 1, maxQty: 100 };

  function updateQuickTradeDisplay() {
    qtQtyDisplay.textContent = qtState.qty;
    const notional = qtState.price * qtState.qty;
    qtNotional.textContent = `$${fmt(notional)}`;
  }

  qtSlider?.addEventListener('input', (e) => {
    qtState.qty = parseInt(e.target.value);
    updateQuickTradeDisplay();
  });

  $("#qt-close")?.addEventListener('click', () => quickTradeDlg.close());
  $("#qt-cancel")?.addEventListener('click', () => quickTradeDlg.close());

  $("#qt-submit")?.addEventListener('click', async () => {
    if (!isAuthed) {
      qtHint.textContent = "Please log in to place orders.";
      setTimeout(() => (location.href = "/login"), 800);
      return;
    }

    // Validate qtState
    if (!qtState.side || !qtState.symbol) {
      qtHint.textContent = "Invalid order parameters. Please try again.";
      return;
    }

    const payload = {
      symbol: SYMBOL,
      side: qtState.side,
      price: String(qtState.price.toFixed(4)),
      qty: String(qtState.qty)
    };

    console.log("Submitting order:", payload); // DEBUG

    qtHint.textContent = "Submitting...";
    const submitBtn = $("#qt-submit");
    if (submitBtn) submitBtn.disabled = true;

    try {
      const res = await fetch("/orders", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify(payload),
        credentials: "include"
      });

      console.log("Response status:", res.status); // DEBUG

      if (res.status === 401) {
        qtHint.textContent = "You need to log in to place orders.";
        setTimeout(() => (location.href = "/login"), 800);
        return;
      }

      const text = await res.text();
      console.log("Response text:", text); // DEBUG

      if (!res.ok) {
        qtHint.textContent = "Error: " + (text || res.status);
        if (submitBtn) submitBtn.disabled = false;
        return;
      }

      let ack;
      try {
        ack = JSON.parse(text);
      } catch (e) {
        console.error("JSON parse error:", e, text);
        qtHint.textContent = "Error: Invalid server response";
        if (submitBtn) submitBtn.disabled = false;
        return;
      }

      qtHint.textContent = `Success! Order ${ack.order_id}. Trades: ${ack.trades?.length || 0}`;

      if (ack?.snapshot) renderLadder(ack.snapshot);
      updatePosition();
      loadMyOrders(); // Reload orders after placing order

      setTimeout(() => {
        quickTradeDlg.close();
        qtHint.textContent = "";
        if (submitBtn) submitBtn.disabled = false;
      }, 700);
    } catch (err) {
      console.error("Order submission error:", err);
      qtHint.textContent = "Network error: " + err.message;
      if (submitBtn) submitBtn.disabled = false;
    }
  });

  function openQuickTrade(side, price, maxQty) {
    if (!isAuthed) {
      location.href = "/login";
      return;
    }

    // Validate and normalize inputs
    const normalizedSide = (side || '').trim().toUpperCase();
    if (!normalizedSide || (normalizedSide !== 'BUY' && normalizedSide !== 'SELL')) {
      console.error('Invalid side parameter:', side, 'normalized to:', normalizedSide);
      alert('Invalid trade side. Please try again.');
      return;
    }

    const max = Math.max(1, Math.floor(parseFloat(maxQty) || 100));

    qtState = {
      side: normalizedSide,
      price: parseFloat(price),
      qty: 1,
      maxQty: max
    };

    console.log('openQuickTrade called with qtState:', qtState); // DEBUG

    const titleEl = $("#qt-title");
    if (titleEl) titleEl.textContent = `Quick ${qtState.side}`;

    const sideLabel = $("#qt-side-label");
    if (sideLabel) {
      sideLabel.textContent = qtState.side;
      const sideClass = qtState.side.toLowerCase();
      // Clear existing classes first
      sideLabel.className = '';
      // Add new classes one at a time
      sideLabel.classList.add('qt-side');
      sideLabel.classList.add(sideClass);
    }

    const symbolEl = $("#qt-symbol");
    if (symbolEl) symbolEl.textContent = SYMBOL;

    const priceEl = $("#qt-price");
    if (priceEl) priceEl.textContent = fmt(price);

    qtSlider.max = max;
    qtSlider.value = 1;
    qtState.qty = 1;

    const sliderLabels = $(".slider-labels");
    if (sliderLabels) {
      sliderLabels.innerHTML = `
        <span>1</span>
        <span></span>
        <span>${max}</span>
      `;
    }

    qtHint.textContent = "";
    updateQuickTradeDisplay();
    quickTradeDlg.showModal();
  }

  // Event delegation for trade buttons
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.trade-btn');
    if (!btn) return;

    const side = btn.dataset.side;
    const px = parseFloat(btn.dataset.px);
    const qty = btn.dataset.qty;

    console.log('Trade button clicked:', { side, px, qty, dataset: btn.dataset }); // DEBUG

    if (side && side.trim() && isFinite(px) && qty) {
      openQuickTrade(side.trim(), px, qty);
    } else {
      console.error('Invalid trade button data:', { side, px, qty });
    }
  });

  // Order modal
  const dlg = $("#order-modal");
  const openBtn = $("#open-order");
  const closeBtn = $("#close-order");
  const cancelBtn = $("#cancel-order");
  const form = $("#order-form");
  const inpPx = $("#ord-price");
  const inpQty = $("#ord-qty");
  const hint = $("#ord-hint");

  function prefill() {
    if (lastRef != null && isFinite(lastRef)) {
      inpPx.value = Number(lastRef).toFixed(2);
    }
    if (!inpQty.value) inpQty.value = "1";
    hint.textContent = `Tip: price defaults to current ref for ${SYMBOL}.`;
  }

  openBtn?.addEventListener("click", () => {
    if (!isAuthed) { location.href = "/login"; return; }
    if (!dlg.open) { prefill(); dlg.showModal(); }
  });

  closeBtn?.addEventListener("click", () => dlg.close());
  cancelBtn?.addEventListener("click", () => dlg.close());

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
      symbol: SYMBOL,
      side,
      price: String(priceNum.toFixed(4)),
      qty: String(qtyNum)
    };

    console.log("Submitting order (modal):", payload); // DEBUG

    hint.textContent = "Submitting…";
    try {
      const res = await fetch("/orders", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify(payload),
        credentials: "include"
      });

      console.log("Response status (modal):", res.status); // DEBUG

      if (res.status === 401) {
        hint.textContent = "You need to log in to place orders.";
        setTimeout(() => (location.href = "/login"), 800);
        return;
      }

      const text = await res.text();
      console.log("Response text (modal):", text); // DEBUG

      if (!res.ok) {
        hint.textContent = "Error: " + (text || res.status);
        return;
      }

      let ack;
      try {
        ack = JSON.parse(text);
      } catch (e) {
        console.error("JSON parse error:", e, text);
        hint.textContent = "Error: Invalid server response";
        return;
      }

      hint.textContent = `Placed! Order ${ack.order_id}. Trades: ${ack.trades?.length || 0}`;
      inpQty.value = "";

      if (ack?.snapshot) renderLadder(ack.snapshot);
      updatePosition();
      loadMyOrders(); // Reload orders after placing order

      setTimeout(() => dlg.close(), 700);
    } catch (err) {
      console.error("Order submission error (modal):", err);
      hint.textContent = "Network error: " + err.message;
    }
  });

  // Update position for this symbol
  async function updatePosition() {
    if (!isAuthed) return;

    try {
      const data = await fetchJSON("/me/metrics");
      const metrics = data.metrics || {};
      const symMetrics = metrics[SYMBOL];

      const positionCard = $("#position-summary");
      const posQty = $("#pos-qty");

      if (!positionCard || !posQty) return;

      // Always show position card when authenticated
      positionCard.classList.remove("hidden");

      if (symMetrics) {
        const qty = parseFloat(symMetrics.position || 0);

        // Format with + or - prefix (except for 0)
        let qtyFormatted;
        if (qty === 0) {
          qtyFormatted = "0.00";
          posQty.className = "position-value";
        } else if (qty > 0) {
          qtyFormatted = `+${fmt(qty)}`;
          posQty.className = "position-value positive";
        } else {
          qtyFormatted = fmt(qty);
          posQty.className = "position-value negative";
        }

        posQty.textContent = qtyFormatted;
      } else {
        // No metrics for this symbol, show 0
        posQty.textContent = "0.00";
        posQty.className = "position-value";
      }
    } catch (e) {
      console.error("Error updating position:", e);
    }
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
      $("#position-summary")?.classList.add("hidden");
    }

    function showUser(nameLike) {
      isAuthed = true;
      if (userNameEl) userNameEl.textContent = String(nameLike || "user");
      loginBox?.classList.add("hidden");
      userBox?.classList.remove("hidden");
      $("#position-summary")?.classList.remove("hidden");
      updatePosition();
      loadMyOrders(); // Load orders when user is shown
    }

    try {
      const me = await fetchJSON("/me");
      const nameLike = me?.username || me?.name || me?.email || me?.id || "user";
      showUser(nameLike);
    } catch {
      showGuest();
    }
  }

  async function loadNews() {
    const box = $("#news-content");
    if (!box) return; // 自定义 game 的页面是 game-info-card，没有 news-content，直接跳过

    try {
      const res = await fetch("/news?limit=20", { credentials: "include" });
      if (!res.ok) throw new Error("HTTP " + res.status);

      const items = await res.json();

      if (!items.length) {
        box.innerHTML = `
          <div class="news-item">
            <div class="news-text">No news yet.</div>
          </div>
        `;
        return;
      }

      box.innerHTML = items
        .map((n) => {
          const dt = new Date(n.created_at);
          const ts = dt.toLocaleTimeString();
          return `
            <div class="news-item">
              <div class="news-time">${ts}</div>
              <div class="news-text">${n.content}</div>
            </div>
          `;
        })
        .join("");
    } catch (err) {
      console.error("loadNews failed", err);
      box.innerHTML = `
        <div class="news-item">
          <div class="news-text">Failed to load news.</div>
        </div>
      `;
    }
  }


  // Load my orders for this symbol
  async function loadMyOrders() {
    if (!isAuthed) {
      const container = $("#my-orders-list");
      if (container) {
        container.innerHTML = `
          <div class="small" style="text-align: center; color: var(--muted); padding: 20px;">
            Please log in to view your orders
          </div>
        `;
      }
      return;
    }

    try {
      const res = await fetchJSON("/me/orders");
      const allOrders = Array.isArray(res) ? res : [];

      // Filter orders for current symbol
      const myOrders = allOrders.filter(o => o.symbol === SYMBOL && o.status === 'OPEN');

      const container = $("#my-orders-list");
      if (!container) return;

      if (myOrders.length === 0) {
        container.innerHTML = `
          <div class="small" style="text-align: center; color: var(--muted); padding: 20px;">
            No open orders for ${SYMBOL}
          </div>
        `;
        return;
      }

      container.innerHTML = myOrders.map(order => {
        const side = order.side.toUpperCase();
        const sideClass = side === 'BUY' ? 'buy' : 'sell';
        const orderClass = side === 'BUY' ? 'buy-order' : 'sell-order';
        const remaining = parseFloat(order.qty) - parseFloat(order.filled_qty || 0);

        return `
          <div class="order-item ${orderClass}">
            <div class="order-info">
              <div class="order-side ${sideClass}">${side}</div>
              <div class="order-details">
                <span class="order-price">@${fmt(order.price)}</span>
                <span class="order-qty">Qty: ${fmt(remaining)}</span>
              </div>
            </div>
            <button class="cancel-order-btn" data-order-id="${order.id}" onclick="cancelMyOrder('${order.id}')">
              Cancel
            </button>
          </div>
        `;
      }).join('');

    } catch (err) {
      console.error("Error loading orders:", err);
      const container = $("#my-orders-list");
      if (container) {
        container.innerHTML = `
          <div class="small" style="text-align: center; color: var(--muted); padding: 20px;">
            Error loading orders
          </div>
        `;
      }
    }
  }

  // Cancel an order
  window.cancelMyOrder = async function(orderId) {
    const btn = document.querySelector(`button[data-order-id="${orderId}"]`);
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Canceling...';
    }

    try {
      const res = await fetch(`/me/orders/${orderId}/cancel`, {
        method: 'POST',
        credentials: 'include'
      });

      if (!res.ok) {
        throw new Error('Failed to cancel order');
      }

      // Reload orders
      await loadMyOrders();

      // Update position
      updatePosition();

      // Refresh the order book by fetching latest snapshot
      try {
        const bookData = await fetchJSON(`/book/${SYMBOL}`);
        if (bookData) {
          renderLadder(bookData);

          // For custom games, update the reference price from the new book
          if (IS_CUSTOM_GAME) {
            const bids = bookData.bids || [];
            const asks = bookData.asks || [];
            if (bids.length > 0 && asks.length > 0) {
              const bestBid = parseFloat(bids[0].px);
              const bestAsk = parseFloat(asks[0].px);
              const mid = (bestBid + bestAsk) / 2;
              setRef(mid);
            } else {
              // No more orders - clear the price
              const refEl = $(`#ref-${SYMBOL}`);
              if (refEl) refEl.textContent = "--";
            }
          }
        }
      } catch (err) {
        console.error("Error refreshing book:", err);
      }

    } catch (err) {
      console.error("Error canceling order:", err);
      alert('Failed to cancel order: ' + err.message);

      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Cancel';
      }
    }
  };

  // Initialize
  connectWS();
  initAuthUI();
  loadNews();
  setInterval(loadNews, 5000);    // refresh every 5 seconds

  // Load orders immediately and refresh periodically
  loadMyOrders();
  setInterval(() => {
    if (isAuthed) {
      loadMyOrders();
      updatePosition();
    }
  }, 3000); // refresh every 3 seconds
})();