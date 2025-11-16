(function () {
  const root = document.getElementById("root");
  const symbols = JSON.parse(root.dataset.symbols || "[]");
  const DEPTH = Number(root.dataset.depth || "10");
  const grid = document.getElementById("grid");
  const wsProto = location.protocol === "https:" ? "wss" : "ws";

  function makeRows(sym) {
    let rows = "";
    for (let i = 0; i < DEPTH; i++) {
      rows += `
        <tr>
          <td id="bidpx-${sym}-${i}" class="bid"></td>
          <td id="bidqty-${sym}-${i}" class="bid"></td>
          <td id="askpx-${sym}-${i}" class="ask"></td>
          <td id="askqty-${sym}-${i}" class="ask"></td>
        </tr>`;
    }
    return rows;
  }

  function cardTemplate(s) {
    return `
      <div class="card" id="card-${s}">
        <div class="row">
          <div class="sym left">${s}</div>
          <div class="val" id="ref-${s}">ref: —</div>
        </div>
        <table aria-label="orderbook ${s}">
          <thead>
            <tr>
              <th class="left">Bid Px</th>
              <th>Bid Qty</th>
              <th>Ask Px</th>
              <th>Ask Qty</th>
            </tr>
          </thead>
          <tbody>
            ${makeRows(s)}
          </tbody>
        </table>
      </div>`;
  }

  function fmt(n, dp = 2) {
    if (n === null || n === undefined) return "—";
    const x = Number(n);
    if (!isFinite(x)) return "—";
    return x.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: 6 });
  }

  function paintLevels(prefix, sym, levels) {
    for (let i = 0; i < DEPTH; i++) {
      const pxEl = document.getElementById(`${prefix}px-${sym}-${i}`);
      const qtyEl = document.getElementById(`${prefix}qty-${sym}-${i}`);
      if (!pxEl || !qtyEl) continue;

      if (i < levels.length) {
        const lv = levels[i];
        pxEl.textContent = fmt(lv.px);
        qtyEl.textContent = fmt(lv.qty, 0);
        pxEl.classList.remove("empty");
        qtyEl.classList.remove("empty");
      } else {
        pxEl.textContent = "—";
        qtyEl.textContent = "—";
        pxEl.classList.add("empty");
        qtyEl.classList.add("empty");
      }
    }
  }

  function updateSnapshot(sym, book, ref) {
    // book.bids: highest->lowest ; book.asks: lowest->highest (already sorted server-side)
    paintLevels("bid", sym, book.bids || []);
    paintLevels("ask", sym, book.asks || []);
    if (ref !== undefined) {
      document.getElementById(`ref-${sym}`).textContent = "ref: " + fmt(ref);
    }
  }

  async function pollRef(sym) {
    try {
      const r = await fetch(`/reference/${sym}`);
      const j = await r.json();
      document.getElementById(`ref-${sym}`).textContent = "ref: " + fmt(j.price);
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
