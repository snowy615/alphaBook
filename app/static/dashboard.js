(function () {
  const root = document.getElementById("root");
  const symbols = JSON.parse(root.dataset.symbols || "[]");
  const grid = document.getElementById("grid");
  const wsProto = location.protocol === "https:" ? "wss" : "ws";

  function cardTemplate(s) {
    return `
      <div class="card" id="card-${s}">
        <div class="row"><div class="sym">${s}</div><div class="val" id="ref-${s}">ref: —</div></div>
        <div class="row"><div class="label">Best Bid</div><div class="val bid" id="bid-${s}">—</div></div>
        <div class="row"><div class="label">Best Ask</div><div class="val ask" id="ask-${s}">—</div></div>
        <div class="row"><div class="label">Mid</div><div class="val" id="mid-${s}">—</div></div>
        <div class="muted" id="ts-${s}">waiting for data…</div>
      </div>`;
  }

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    const x = Number(n);
    if (!isFinite(x)) return "—";
    return x.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 });
  }

  function updateSnapshot(sym, book, ref) {
    const bids = book.bids || [];
    const asks = book.asks || [];
    const bestBidPx = bids.length ? Number(bids[0].px) : null;
    const bestAskPx = asks.length ? Number(asks[0].px) : null;
    const mid = (bestBidPx !== null && bestAskPx !== null) ? (bestBidPx + bestAskPx) / 2 : null;

    document.getElementById(`bid-${sym}`).textContent =
      bestBidPx !== null ? fmt(bestBidPx) + " @ " + (bids[0].qty || "—") : "—";
    document.getElementById(`ask-${sym}`).textContent =
      bestAskPx !== null ? fmt(bestAskPx) + " @ " + (asks[0].qty || "—") : "—";
    document.getElementById(`mid-${sym}`).textContent = fmt(mid);
    if (ref !== undefined) {
      document.getElementById(`ref-${sym}`).textContent = "ref: " + fmt(ref);
    }
    document.getElementById(`ts-${sym}`).textContent = "updated " + new Date().toLocaleTimeString();
  }

  async function pollRef(sym) {
    try {
      const r = await fetch(`/reference/${sym}`);
      const j = await r.json();
      document.getElementById(`ref-${sym}`).textContent = "ref: " + fmt(j.price);
    } catch (e) {
      // ignore
    }
  }

  function connectWS(sym) {
    const ws = new WebSocket(`${wsProto}://${location.host}/ws/book/${sym}`);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          updateSnapshot(sym, msg.book, msg.ref_price);
        }
      } catch (e) {}
    };
    ws.onclose = () => setTimeout(() => connectWS(sym), 1500);
  }

  function bootstrap() {
    grid.innerHTML = symbols.map(cardTemplate).join("");
    symbols.forEach((sym) => {
      connectWS(sym);
      pollRef(sym);
      setInterval(() => pollRef(sym), 60000); // 60s to be gentle on free data
    });
  }

  bootstrap();
})();
