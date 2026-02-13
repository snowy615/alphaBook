(function () {
  const root = document.getElementById("root");
  const symbols = JSON.parse(root.dataset.symbols || "[]");
  const DEPTH = Number(root.dataset.depth || "10");
  const gridCards = document.getElementById("gridCards");

  // ------- Ladder cards -------
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
  const fmtPx = (p) => fmtNum(p, 2);

  function buildLadder(bids, asks) {
    const bidMap = new Map(bids.map(l => [Number(l.px), Number(l.qty)]));
    const askMap = new Map(asks.map(l => [Number(l.px), Number(l.qty)]));
    const bidPrices = bids.slice(0, DEPTH).map(l => Number(l.px));
    const askPrices = asks.slice(0, DEPTH).map(l => Number(l.px));
    const prices = Array.from(new Set([...bidPrices, ...askPrices])).sort((a, b) => b - a);
    let maxQty = 1;
    for (const p of prices) maxQty = Math.max(maxQty, bidMap.get(p) || 0, askMap.get(p) || 0);
    return prices.map(p => {
      const bq = bidMap.get(p), aq = askMap.get(p);
      const bpct = Math.min(100, Math.round((bq || 0) / maxQty * 100));
      const apct = Math.min(100, Math.round((aq || 0) / maxQty * 100));
      return `
        <tr>
          <td class="right">
            ${bq ? `<span class="qty bid" style="--pct:${bpct}">${fmtQty(bq)}</span>` : `<span class="qty empty"></span>`}
          </td>
          <td class="center price">${fmtPx(p)}</td>
          <td class="left">
            ${aq ? `<span class="qty ask" style="--pct:${apct}">${fmtQty(aq)}</span>` : `<span class="qty empty"></span>`}
          </td>
        </tr>`;
    }).join("");
  }

  function updateSnapshot(sym, book, ref) {
    const tbody = document.getElementById(`tb-${sym}`);
    tbody.innerHTML = buildLadder(book.bids || [], book.asks || []);
    if (ref !== undefined) document.getElementById(`ref-${sym}`).textContent = "ref: " + fmtNum(ref);
  }

  async function pollRef(sym) {
    try {
      const r = await fetch(`/reference/${sym}`);
      const j = await r.json();
      document.getElementById(`ref-${sym}`).textContent = "ref: " + fmtNum(j.price);
    } catch { }
  }

  async function pollBook(sym) {
    try {
      const r = await fetch(`/book/${sym}`);
      const book = await r.json();
      updateSnapshot(sym, book);
    } catch { }
  }

  function bootstrapBooks() {
    gridCards.innerHTML = symbols.map(cardTemplate).join("");
    symbols.forEach((sym) => {
      pollBook(sym);
      pollRef(sym);
      setInterval(() => pollBook(sym), 2000);
      setInterval(() => pollRef(sym), 2000);
    });
  }

  // ------- Auth / Your PnL -------
  const authbox = document.getElementById("authbox");
  const mebox = document.getElementById("mebox");

  function renderSignedOut() {
    authbox.innerHTML = `<a class="btn" href="/login">Login</a> · <a class="btn" href="/signup">Sign up</a>`;
    mebox.innerHTML = `<div class="muted">Sign in to see your PnL and place orders.</div>`;
  }

  function renderMe(data) {
    const user = data.user;
    const metrics = data.metrics || {};
    const rows = Object.keys(metrics).sort().map(sym => {
      const m = metrics[sym];
      const total = Number(m.total_pnl || 0);
      return `
        <tr>
          <td>${sym}</td>
          <td class="right">${fmtNum(m.ref)}</td>
          <td class="right">${fmtNum(m.avg_price)}</td>
          <td class="right">${fmtNum(m.position, 0)}</td>
          <td class="right">${fmtNum(m.delta, 0)}</td>
          <td class="right">${fmtNum(m.realized)}</td>
          <td class="right ${total >= 0 ? "ok" : "bad"}">${fmtNum(m.total_pnl)}</td>
        </tr>`;
    }).join("");
    authbox.innerHTML = `
      <form method="post" action="/logout" style="display:flex; gap:8px; align-items:center;">
        <div>Signed in as <strong>${user}</strong></div>
        <button class="logout" type="submit">Logout</button>
      </form>`;
    mebox.innerHTML = `
      <table>
        <thead>
          <tr>
            <th class="left">Symbol</th>
            <th class="right">Ref</th>
            <th class="right">Avg</th>
            <th class="right">Pos</th>
            <th class="right">Δ</th>
            <th class="right">Realized</th>
            <th class="right">Total PnL</th>
          </tr>
        </thead>
        <tbody>${rows || `<tr><td colspan="7" class="muted">No trades yet.</td></tr>`}</tbody>
      </table>`;
  }

  async function refreshMe() {
    try {
      const r = await fetch("/me/metrics");
      if (r.status === 401) return renderSignedOut();
      const j = await r.json();
      renderMe(j);
    } catch {
      renderSignedOut();
    }
  }

  function bootstrap() {
    bootstrapBooks();
    refreshMe();
    setInterval(refreshMe, 2000);
  }

  bootstrap();
})();
