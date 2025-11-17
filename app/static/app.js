(function () {
  const grid = document.getElementById("grid");
  const DEPTH = window.TOP_DEPTH || 10;
  const SYMS = (window.SYMBOLS || []).map(s => s.toUpperCase());

  // --- State for refs so we can prefill order price
  const lastRef = Object.create(null);

  // Build a card with a single aligned ladder (asks block, mid row, bids block)
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

  function fmt(n) {
    if (n === null || n === undefined) return "--";
    const v = +n;
    if (!isFinite(v)) return "--";
    if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
    return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
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

  function setMeta(sym, text) {
    const el = document.getElementById(`meta-${sym}`);
    if (el) el.textContent = text;
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
      tr.innerHTML = `
        <td>${fmt(a.qty)}</td>
        <td>${fmt(a.px)}</td>
        <td class="div"></td>
        <td></td>
        <td></td>
      `;
      body.appendChild(tr);
    }

    // mid row with spread
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
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/book/${sym}`);
    ws.onopen = () => setMeta(sym, "connected • live");
    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data.type === "snapshot") {
          renderLadder(sym, data.book);
          setRef(sym, data.ref_price);
          const ts = new Date().toLocaleTimeString();
          setMeta(sym, `updated ${ts}`);
        }
      } catch (e) {
        console.error(e);
      }
    };
    ws.onclose = () => setMeta(sym, "disconnected — retrying…") || setTimeout(() => connect(sym), 1500);
    ws.onerror = () => ws.close();
  }

  // ----- Order modal & submission -----
  const dlg = document.getElementById("order-modal");
  const openBtn = document.getElementById("open-order");
  const closeBtn = document.getElementById("close-order");
  const cancelBtn = document.getElementById("cancel-order");
  const form = document.getElementById("order-form");
  const selSym = document.getElementById("ord-symbol");
  const inpPx = document.getElementById("ord-price");
  const inpQty = document.getElementById("ord-qty");
  const hint = document.getElementById("ord-hint");

  // populate symbol list
  (window.SYMBOLS || []).forEach(s => {
    const opt = document.createElement("option");
    opt.value = opt.textContent = s.toUpperCase();
    selSym.appendChild(opt);
  });

  function prefill() {
    const sym = selSym.value;
    const ref = lastRef[sym];
    if (ref != null && isFinite(ref)) {
      inpPx.value = Number(ref).toFixed(4);
    }
    if (!inpQty.value) inpQty.value = "1";
    hint.textContent = `Tip: price defaults to current ref for ${sym}.`;
  }

  openBtn?.addEventListener("click", () => { if (!dlg.open) { prefill(); dlg.showModal(); } });
  closeBtn?.addEventListener("click", () => dlg.close());
  cancelBtn?.addEventListener("click", () => dlg.close());
  selSym?.addEventListener("change", prefill);

  form?.addEventListener("submit", async (e) => {
  e.preventDefault();

  const formData = new FormData(form);
  const side = String(formData.get("side") || "BUY").toUpperCase();

  // Pydantic wants strings for price/qty; also include a dummy user_id.
  const priceNum = parseFloat(inpPx.value);
  const qtyNum = parseFloat(inpQty.value);

  if (!isFinite(priceNum) || !isFinite(qtyNum) || qtyNum <= 0) {
    hint.textContent = "Please enter a valid price and quantity.";
    return;
  }

  const payload = {
    user_id: "browser",                 // required by OrderIn (ignored by server logic)
    symbol: selSym.value,
    side,                               // "BUY" | "SELL"
    price: String(priceNum.toFixed(4)), // strings, not numbers
    qty: String(qtyNum)                 // string
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
    if (!res.ok) {
      // show server validation details if any
      hint.textContent = "Error: " + (text || res.status);
      return;
    }

    const ack = JSON.parse(text);
    hint.textContent = `Placed! Order ${ack.order_id}. Trades: ${ack.trades?.length || 0}`;
    // Optional: clear qty; keep price near ref for convenience
    inpQty.value = "";
    setTimeout(() => dlg.close(), 700);
  } catch (err) {
    console.error(err);
    hint.textContent = "Network error submitting order.";
  }
});

})();
