(function () {
  const root = document.getElementById("root");
  const symbols = JSON.parse(root.dataset.symbols || "[]");
  const DEPTH = Number(root.dataset.depth || "10");
  const grid = document.getElementById("grid");
  const wsProto = location.protocol === "https:" ? "wss" : "ws";

  function cardTemplate(s) {
    return `
      <div class="card" id="card-${s}">
        <div class="card-head">
          <div class="sym">${s}</div>
          <div class="ref" id="ref-${s}">ref: —</div>
        </div>
        <table aria-label="orderbook ${s}">
          <thead>
            <tr>
              <th class="right"># Bid</th>
              <th class="center">Price</th>
              <th class="left"># Ask</th>
            </tr>
          </thead>
          <tbody id="tb-${s}"></tbody>
        </table>
      </div>`;
  }

  function fmtNum(n, dp = 2) {
    if (n === null || n === undefined) return "—";
    const x = Number(n);
    if (!isFinite(x)) return "—";
    return x.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: 6 });
  }
  const fmtQty = (q) => fmtNum(q, 0);
  const fmtPx  = (p) => fmtNum(p, 2);

  // Merge bids/asks into a single descending price ladder
  function buildLadder(bids, asks) {
    const bidMap = new Map(bids.map(l => [Number(l.px), Number(l.qty)]));
    const askMap = new Map(asks.map(l => [Number(l.px), Number(l.qty)]));

    const bidPrices = bids.slice(0, DEPTH).map(l => Number(l.px));
    const askPrices = asks.slice(0, DEPTH).map(l => Number(l.px));

    const prices = Array.from(new Set([...bidPrices, ...askPrices]))
      .sort((a, b) => b - a); // high -> low (like most ladders)

    // scale bars to the largest visible qty
    let maxQty = 0;
    for (const p of prices) {
      maxQty = Math.max(maxQty, bidMap.get(p) || 0, askMap.get(p) || 0);
    }
    if (maxQty === 0) maxQty = 1;

    const rows = prices.map(p => {
      const bq = bidMap.get(p);
      const aq = askMap.get(p);
      const bpct = Math.min(100, Math.round((bq || 0) / maxQty * 100));
      const apct = Math.min(100, Math.round((aq || 0) / maxQty * 100));
      return `
        <tr>
          <td class="right">
            ${bq ? `<span class="qty bid" style="--pct:${bpct}">${fmtQty(bq)}</span>`
                 : `<span class="qty empty"></span>`}
          </td>
          <td class="center price">${fmtPx(p)}</td>
          <td class="left">
            ${aq ? `<span class="qty ask" style="--pct:${apct}">${fmtQty(aq)}</span>`
                 : `<span class="qty empty"></span>`}
          </td>
        </tr>`;
    });

    return rows.join("");
  }

  function updateSnapshot(sym, book, ref) {
    const tbody = document.getElementById(`tb-${sym}`);
    tbody.innerHTML = buildLadder(book.bids || [], book.asks || []);
    if (ref !== undefined) {
      document.getElementById(`ref-${sym}`).textContent = "ref: " + fmtNum(ref);
    }
  }

  async function pollRef(sym) {
    try {
      const r = await fetch(`/reference/${sym}`);
      const j = await r.json();
      document.getElementById(`ref-${sym}`).textContent = "ref: " + fmtNum(j.price);
    } catch {}
  }

  function connectWS(sym) {
    const ws = new WebSocket(`${wsProto}://${location.host}/ws/book/${sym}`);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          updateSnapshot(sym, msg.book, msg.ref_price);
        }
      } catch {}
    };
    ws.onclose = () => setTimeout(() => connectWS(sym), 1500);
  }

  function bootstrap() {
    grid.innerHTML = symbols.map(cardTemplate).join("");
    symbols.forEach((sym) => {
      connectWS(sym);
      pollRef(sym);
      setInterval(() => pollRef(sym), 60000); // be gentle on free data
    });
  }

  bootstrap();
})();
