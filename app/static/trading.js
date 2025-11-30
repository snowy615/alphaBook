// FIXED trading.js - Key changes marked with // FIX:
// Replace the renderLadder function and order submission handlers

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

  // Toggle instructions collapse
  window.toggleInstructions = function() {
    const content = $("#game-instructions-content");
    const icon = $("#instructions-icon");

    if (!content || !icon) return;

    if (content.style.display === "none") {
      content.style.display = "block";
      icon.classList.add("expanded");
    } else {
      content.style.display = "none";
      icon.classList.remove("expanded");
    }
  };

  // WebSocket connection
  function connectWS() {
    const wsProto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${wsProto}://${location.host}/ws/book/${SYMBOL}`);

    ws.onopen = () => {
      console.log(`[WS] Connected to ${SYMBOL}`);
      setMeta("connected • live");
    };

    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        console.log(`[WS] Received message for ${SYMBOL}:`, data.type); // FIX: Added logging
        if (data.type === "snapshot") {
          console.log(`[WS] Book snapshot:`, data.book); // FIX: Added logging
          renderLadder(data.book);
          setRef(data.ref_price);
          setMeta(`updated ${new Date().toLocaleTimeString()}`);
          updatePosition();
        }
      } catch (e) {
        console.error('[WS] Error processing message:', e);
      }
    };

    ws.onclose = () => {
      console.log(`[WS] Disconnected from ${SYMBOL}, reconnecting...`);
      setMeta("disconnected — retrying…");
      setTimeout(connectWS, 1500);
    };

    ws.onerror = (err) => {
      console.error('[WS] Error:', err);
      ws.close();
    };
  }

  function setMeta(text) {
    const el = $(`#meta-${SYMBOL}`);
    if (el) el.textContent = text;
  }

  function setRef(price) {
    const el = $(`#ref-${SYMBOL}`);
    if (!el) return;

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
      el.classList.add("blink");
      setTimeout(() => el.classList.remove("blink"), 400);
    }
  }

  // FIX: Improved renderLadder with better data handling
  function renderLadder(book) {
    const body = $(`#ladder-body-${SYMBOL}`);
    if (!body) {
      console.error('[renderLadder] Body element not found for:', SYMBOL);
      return;
    }

    // FIX: Ensure book is valid
    if (!book || typeof book !== 'object') {
      console.error('[renderLadder] Invalid book data:', book);
      book = { bids: [], asks: [] };
    }

    console.log('[renderLadder] Processing book:', JSON.stringify(book)); // FIX: Detailed logging

    body.innerHTML = "";

    // FIX: Safely handle asks and bids arrays
    const rawAsks = Array.isArray(book.asks) ? book.asks : [];
    const rawBids = Array.isArray(book.bids) ? book.bids : [];

    console.log('[renderLadder] Raw asks:', rawAsks.length, 'Raw bids:', rawBids.length);

    // FIX: Parse and filter valid entries
    const asks = rawAsks
      .map(a => ({
        px: parseFloat(a.px || a.price || 0),
        qty: parseFloat(a.qty || a.quantity || 0)
      }))
      .filter(a => a.px > 0 && a.qty > 0)
      .slice(0, DEPTH)
      .sort((a, b) => a.px - b.px);

    const bids = rawBids
      .map(b => ({
        px: parseFloat(b.px || b.price || 0),
        qty: parseFloat(b.qty || b.quantity || 0)
      }))
      .filter(b => b.px > 0 && b.qty > 0)
      .slice(0, DEPTH)
      .sort((a, b) => b.px - a.px);

    console.log('[renderLadder] Processed asks:', asks.length, 'Processed bids:', bids.length);

    // Calculate mid price from order book
    const bestAsk = asks.length > 0 ? asks[0].px : null;
    const bestBid = bids.length > 0 ? bids[0].px : null;
    const mid = (bestAsk !== null && bestBid !== null) ? (bestAsk + bestBid) / 2 : null;

    console.log('[renderLadder] Best ask:', bestAsk, 'Best bid:', bestBid, 'Mid:', mid);

    // For custom games, update the price display with mid price
    if (IS_CUSTOM_GAME && mid !== null) {
      setRef(mid);
    }

    // Asks block (reversed so lowest ask is at bottom, closest to mid)
    for (let i = asks.length - 1; i >= 0; i--) {
      const a = asks[i];
      const tr = document.createElement("tr");
      tr.className = "row-ask";
      tr.innerHTML = `
        <td class="action-cell">
          <button class="trade-btn buy-btn" data-side="BUY" data-px="${a.px}" data-qty="${a.qty}">Buy</button>
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

    // Mid row
    const sp = (bestAsk !== null && bestBid !== null) ? (bestAsk - bestBid) : null;
    const midtr = document.createElement("tr");
    midtr.className = "midrow";

    let addAskButton, addBidButton;

    if (bestAsk !== null) {
      addAskButton = `<button class="mid-add-btn add-ask-btn" onclick="openPlaceOrder('SELL', ${bestAsk})" title="Add liquidity on ask side">+ Sell @ ${fmt(bestAsk)}</button>`;
    } else {
      addAskButton = `<button class="mid-add-btn add-ask-btn" onclick="openPlaceOrder('SELL', null)" title="Place sell order">+ Sell</button>`;
    }

    if (bestBid !== null) {
      addBidButton = `<button class="mid-add-btn add-bid-btn" onclick="openPlaceOrder('BUY', ${bestBid})" title="Add liquidity on bid side">+ Buy @ ${fmt(bestBid)}</button>`;
    } else {
      addBidButton = `<button class="mid-add-btn add-bid-btn" onclick="openPlaceOrder('BUY', null)" title="Place buy order">+ Buy</button>`;
    }

    midtr.innerHTML = `
      <td colspan="3" class="mid-action-left">
        ${addAskButton}
      </td>
      <td class="mid-divider">
        ${bestBid !== null && bestAsk !== null ? `Spread: ${fmt(sp)}` : '—'}
      </td>
      <td colspan="3" class="mid-action-right">
        ${addBidButton}
      </td>
    `;
    body.appendChild(midtr);

    // Bids block
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
          <button class="trade-btn sell-btn" data-side="SELL" data-px="${b.px}" data-qty="${b.qty}">Sell</button>
        </td>
      `;
      body.appendChild(tr);
    }

    console.log('[renderLadder] Rendered', asks.length, 'asks and', bids.length, 'bids');
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

    if (!qtState.side) {
      qtHint.textContent = "Invalid order side. Please try again.";
      console.error("Invalid qtState:", qtState);
      return;
    }

    if (!qtState.price || !isFinite(qtState.price) || qtState.price <= 0) {
      qtHint.textContent = "Invalid price. Please try again.";
      console.error("Invalid price in qtState:", qtState);
      return;
    }

    if (!qtState.qty || qtState.qty <= 0) {
      qtHint.textContent = "Invalid quantity. Please try again.";
      console.error("Invalid qty in qtState:", qtState);
      return;
    }

    const MAX_POSITION = 100;
    const currentPos = window.currentPosition || 0;

    if (qtState.side === "BUY") {
      const newPosition = currentPos + qtState.qty;
      if (newPosition > MAX_POSITION) {
        qtHint.textContent = `Would exceed max position of ${MAX_POSITION}. Current: ${currentPos.toFixed(2)}`;
        return;
      }
    } else if (qtState.side === "SELL") {
      const newPosition = currentPos - qtState.qty;
      if (newPosition < -MAX_POSITION) {
        qtHint.textContent = `Would exceed max short of ${MAX_POSITION}. Current: ${currentPos.toFixed(2)}`;
        return;
      }
    }

    const payload = {
      symbol: SYMBOL,
      side: qtState.side,
      price: String(qtState.price.toFixed(4)),
      qty: String(qtState.qty)
    };

    console.log("[QuickTrade] Submitting order:", payload);

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

      console.log("[QuickTrade] Response status:", res.status);

      if (res.status === 401) {
        qtHint.textContent = "You need to log in to place orders.";
        setTimeout(() => (location.href = "/login"), 800);
        return;
      }

      const text = await res.text();
      console.log("[QuickTrade] Response text:", text);

      if (!res.ok) {
        qtHint.textContent = "Error: " + (text || res.status);
        if (submitBtn) submitBtn.disabled = false;
        return;
      }

      let ack;
      try {
        ack = JSON.parse(text);
      } catch (e) {
        console.error("[QuickTrade] JSON parse error:", e, text);
        qtHint.textContent = "Error: Invalid server response";
        if (submitBtn) submitBtn.disabled = false;
        return;
      }

      console.log("[QuickTrade] Order ACK:", ack);

      qtHint.textContent = `Success! Order ${ack.order_id}. Trades: ${ack.trades?.length || 0}`;

      // FIX: Ensure snapshot is rendered correctly
      if (ack && ack.snapshot) {
        console.log("[QuickTrade] Rendering snapshot from ACK:", ack.snapshot);
        renderLadder(ack.snapshot);
      }

      updatePosition();
      loadMyOrders();

      setTimeout(() => {
        quickTradeDlg.close();
        qtHint.textContent = "";
        if (submitBtn) submitBtn.disabled = false;
      }, 700);
    } catch (err) {
      console.error("[QuickTrade] Order submission error:", err);
      qtHint.textContent = "Network error: " + err.message;
      if (submitBtn) submitBtn.disabled = false;
    }
  });

  function openQuickTrade(side, price, maxQty) {
    if (!isAuthed) {
      location.href = "/login";
      return;
    }

    const normalizedSide = (side || '').trim().toUpperCase();
    if (!normalizedSide || (normalizedSide !== 'BUY' && normalizedSide !== 'SELL')) {
      console.error('Invalid side parameter:', side, 'normalized to:', normalizedSide);
      alert('Invalid trade side. Please try again.');
      return;
    }

    const parsedPrice = parseFloat(price);
    if (!isFinite(parsedPrice) || parsedPrice <= 0) {
      console.error('Invalid price parameter:', price, 'parsed to:', parsedPrice);
      alert('Invalid price. Please try again.');
      return;
    }

    const max = Math.max(1, Math.floor(parseFloat(maxQty) || 100));

    qtState = {
      side: normalizedSide,
      price: parsedPrice,
      qty: 1,
      maxQty: max
    };

    console.log('[openQuickTrade] State:', qtState);

    const titleEl = $("#qt-title");
    if (titleEl) titleEl.textContent = `Quick ${qtState.side}`;

    const sideLabel = $("#qt-side-label");
    if (sideLabel) {
      sideLabel.textContent = qtState.side;
      sideLabel.className = 'qt-side ' + qtState.side.toLowerCase();
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

    console.log('[TradeBtn] Clicked:', { side, px, qty, dataset: btn.dataset });

    if (side && side.trim() && isFinite(px) && qty) {
      openQuickTrade(side.trim(), px, qty);
    } else {
      console.error('[TradeBtn] Invalid data:', { side, px, qty });
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

  window.openPlaceOrder = function(side, price) {
    if (!isAuthed) {
      alert("Please log in to place orders");
      return;
    }

    const sideRadios = document.querySelectorAll('input[name="side"]');
    sideRadios.forEach(radio => {
      if (radio.value === side) {
        radio.checked = true;
      }
    });

    if (inpPx) {
      if (price !== null && price !== undefined) {
        inpPx.value = price.toFixed(2);
      } else if (lastRef !== null && isFinite(lastRef)) {
        inpPx.value = Number(lastRef).toFixed(2);
      } else {
        inpPx.value = "";
      }
    }

    if (inpQty && !inpQty.value) {
      inpQty.value = "10";
    }

    if (hint) {
      hint.textContent = '';
    }

    if (dlg && !dlg.open) {
      dlg.showModal();
    }
  };

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

    const MAX_POSITION = 100;
    const currentPos = window.currentPosition || 0;

    if (side === "BUY") {
      const newPosition = currentPos + qtyNum;
      if (newPosition > MAX_POSITION) {
        hint.textContent = `Order would exceed max position limit of ${MAX_POSITION}. Current position: ${currentPos.toFixed(2)}`;
        return;
      }
    } else if (side === "SELL") {
      const newPosition = currentPos - qtyNum;
      if (newPosition < -MAX_POSITION) {
        hint.textContent = `Order would exceed max short position limit of ${MAX_POSITION}. Current position: ${currentPos.toFixed(2)}`;
        return;
      }
    }

    const payload = {
      symbol: SYMBOL,
      side,
      price: String(priceNum.toFixed(4)),
      qty: String(qtyNum)
    };

    console.log("[OrderModal] Submitting order:", payload);

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

      console.log("[OrderModal] Response status:", res.status);

      if (res.status === 401) {
        hint.textContent = "You need to log in to place orders.";
        setTimeout(() => (location.href = "/login"), 800);
        return;
      }

      const text = await res.text();
      console.log("[OrderModal] Response text:", text);

      if (!res.ok) {
        hint.textContent = "Error: " + (text || res.status);
        return;
      }

      let ack;
      try {
        ack = JSON.parse(text);
      } catch (e) {
        console.error("[OrderModal] JSON parse error:", e, text);
        hint.textContent = "Error: Invalid server response";
        return;
      }

      console.log("[OrderModal] Order ACK:", ack);

      hint.textContent = `Placed! Order ${ack.order_id}. Trades: ${ack.trades?.length || 0}`;
      inpQty.value = "";

      // FIX: Ensure snapshot is rendered
      if (ack && ack.snapshot) {
        console.log("[OrderModal] Rendering snapshot from ACK:", ack.snapshot);
        renderLadder(ack.snapshot);
      }

      updatePosition();
      loadMyOrders();

      setTimeout(() => dlg.close(), 700);
    } catch (err) {
      console.error("[OrderModal] Order submission error:", err);
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

      positionCard.classList.remove("hidden");

      if (symMetrics) {
        const qty = parseFloat(symMetrics.position || 0);
        window.currentPosition = qty;

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
        window.currentPosition = 0;
        posQty.textContent = "0.00";
        posQty.className = "position-value";
      }
    } catch (e) {
      console.error("Error updating position:", e);
      window.currentPosition = 0;
    }
  }

  // Auth UI
  async function initAuthUI() {
    const loginBox = $("#loginBox");
    const userBox = $("#userBox");
    const userNameEl = $("#userName");
    const adminLink = $("#adminLink");

    function showGuest() {
      isAuthed = false;
      loginBox?.classList.remove("hidden");
      userBox?.classList.add("hidden");
      $("#position-summary")?.classList.add("hidden");
      if (adminLink) adminLink.style.display = "none";
    }

    function showUser(nameLike, isAdmin) {
      isAuthed = true;
      if (userNameEl) userNameEl.textContent = String(nameLike || "user");
      loginBox?.classList.add("hidden");
      userBox?.classList.remove("hidden");
      $("#position-summary")?.classList.remove("hidden");

      if (adminLink) {
        adminLink.style.display = isAdmin ? "inline-block" : "none";
      }

      updatePosition();
      loadMyOrders();
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

  async function loadNews() {
    const box = $("#news-content");
    if (!box) return;

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

      const sortedItems = items.sort((a, b) => {
        return new Date(b.created_at) - new Date(a.created_at);
      });

      box.innerHTML = sortedItems
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

      const myOrders = allOrders.filter(o => o.symbol === SYMBOL && o.status === 'OPEN');

      myOrders.sort((a, b) => {
        const dateA = new Date(a.created_at || 0);
        const dateB = new Date(b.created_at || 0);
        return dateB - dateA;
      });

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

        let remaining;
        if (order.remaining_qty !== undefined && order.remaining_qty !== null) {
          remaining = parseFloat(order.remaining_qty);
        } else if (order.remaining !== undefined && order.remaining !== null) {
          remaining = parseFloat(order.remaining);
        } else if (order.filled_qty !== undefined && order.filled_qty !== null) {
          remaining = parseFloat(order.qty) - parseFloat(order.filled_qty);
        } else {
          remaining = parseFloat(order.qty);
        }

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

  window.cancelMyOrder = async function(orderId) {
    const btn = document.querySelector(`button[data-order-id="${orderId}"]`);
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Canceling...';
    }

    try {
      const res = await fetch(`/orders/${orderId}`, {
        method: 'DELETE',
        credentials: 'include'
      });

      if (!res.ok) {
        let msg = 'Failed to cancel order';
        try {
          const data = await res.json();
          if (data && data.detail) {
            msg = data.detail;
          }
        } catch (_) {}

        alert(msg);

        if (btn) {
          btn.disabled = false;
          btn.textContent = 'Cancel';
        }
        return;
      }

      await loadMyOrders();
      updatePosition();

      // FIX: Refresh the order book after cancel
      try {
        const bookData = await fetchJSON(`/book/${SYMBOL}`);
        if (bookData) {
          console.log('[Cancel] Refreshing book:', bookData);
          renderLadder(bookData);

          if (IS_CUSTOM_GAME) {
            const bids = bookData.bids || [];
            const asks = bookData.asks || [];
            if (bids.length > 0 && asks.length > 0) {
              const bestBid = parseFloat(bids[0].px);
              const bestAsk = parseFloat(asks[0].px);
              const mid = (bestBid + bestAsk) / 2;
              setRef(mid);
            } else {
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
  console.log(`[Init] Starting for symbol: ${SYMBOL}, IS_CUSTOM_GAME: ${IS_CUSTOM_GAME}`);
  connectWS();
  initAuthUI();
  loadNews();
  setInterval(loadNews, 5000);

  loadMyOrders();
  setInterval(() => {
    if (isAuthed) {
      loadMyOrders();
      updatePosition();
    }
  }, 3000);
})();
