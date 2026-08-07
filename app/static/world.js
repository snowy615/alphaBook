/* The empire half of the coding game.
 *
 * Draws the shared map, and gives you the manual controls your algorithm has
 * through the API: build, train, move, attack, trade. Anything you can do here
 * you can do from `on_world_tick`, and the other way round — the panel is a
 * second hand on the same wheel, never a shortcut past the rules.
 *
 * The map is a canvas rather than a DOM grid: at 40x40 that is 1,600 nodes to
 * restyle every two seconds, and the browser will not keep up.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  const POLL_MS = 2000;

  // Terrain fills, keyed to the site's navy palette so the map sits inside the
  // page rather than on top of it. They are spread further apart in lightness
  // than the palette would suggest, because an ownership wash goes over the top
  // and squashes the difference between them.
  const TERRAIN_FILL = {
    water:  "#0a1424",   // darkest, so open water reads as a hole in the board
    plain:  "#1b2436",
    forest: "#17381f",
    hills:  "#332e47",
  };

  let state = null;        // last /world payload
  let catalogue = null;    // costs and stats, fetched once
  let selected = null;     // {x, y}
  let selectedUnit = null; // unit id
  let timer = null;
  let live = false;

  // ── Networking ─────────────────────────────────────────────────────────
  async function api(path, body) {
    const opts = { credentials: "include" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    const txt = await r.text();
    let data = {};
    try { data = txt ? JSON.parse(txt) : {}; } catch { /* non-JSON */ }
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  }

  const runId = () => window.RUN_ID;

  async function send(actions) {
    try {
      const res = await api(`/market-sim-py/run/${runId()}/world/actions`, { actions });
      const bad = (res.results || []).find((r) => !r.ok);
      if (bad) flash(bad.error, true);
      await refresh();
    } catch (e) {
      flash(e.message, true);
    }
  }

  function flash(text, bad) {
    const hint = $("#wldHint");
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle("is-bad", !!bad);
    clearTimeout(flash.t);
    flash.t = setTimeout(() => {
      hint.textContent = "Click any tile to inspect it.";
      hint.classList.remove("is-bad");
    }, 4000);
  }

  // ── Map lookups ────────────────────────────────────────────────────────
  const board = () => (state && state.map) || null;
  const me = () => (state && state.me) || {};
  const side = () => (board() ? board().side : 0);

  function terrainAt(x, y) {
    const b = board();
    if (!b || x < 0 || y < 0 || x >= b.side || y >= b.side) return null;
    return b.terrain_key[b.terrain[y * b.side + x]];
  }

  function ownerAt(x, y) {
    const b = board();
    if (!b || x < 0 || y < 0 || x >= b.side || y >= b.side) return 0;
    return b.owners[y * b.side + x];
  }

  const playerByIndex = (i) =>
    (board().players || []).find((p) => p.i === i) || null;

  const buildingAt = (x, y) =>
    (board().buildings || []).find((b) => b.x === x && b.y === y) || null;

  const unitsAt = (x, y) =>
    (board().units || []).filter((u) => u.x === x && u.y === y);

  const isMine = (x, y) => ownerAt(x, y) === me().index;

  const chebyshev = (a, b) => Math.max(Math.abs(a[0] - b[0]), Math.abs(a[1] - b[1]));

  // ── Building glyphs ────────────────────────────────────────────────────
  // Drawn shapes rather than letters: an initial in a box reads as a
  // placeholder, and at this size a silhouette is quicker to scan anyway.
  function glyph(ctx, kind, cx, cy, r) {
    ctx.beginPath();
    switch (kind) {
      case "base":           // keep: a diamond
        ctx.moveTo(cx, cy - r); ctx.lineTo(cx + r, cy);
        ctx.lineTo(cx, cy + r); ctx.lineTo(cx - r, cy);
        ctx.closePath();
        break;
      case "farm":           // furrows
        for (let i = -1; i <= 1; i++) {
          ctx.moveTo(cx - r, cy + i * r * 0.6);
          ctx.lineTo(cx + r, cy + i * r * 0.6);
        }
        ctx.stroke();
        return;
      case "lumber":         // a tree
        ctx.moveTo(cx, cy - r); ctx.lineTo(cx + r, cy + r * 0.6);
        ctx.lineTo(cx - r, cy + r * 0.6);
        ctx.closePath();
        break;
      case "mine":           // an inverted tip
        ctx.moveTo(cx, cy + r); ctx.lineTo(cx + r, cy - r * 0.6);
        ctx.lineTo(cx - r, cy - r * 0.6);
        ctx.closePath();
        break;
      case "house":          // gable
        ctx.moveTo(cx, cy - r); ctx.lineTo(cx + r, cy - r * 0.1);
        ctx.lineTo(cx + r, cy + r); ctx.lineTo(cx - r, cy + r);
        ctx.lineTo(cx - r, cy - r * 0.1);
        ctx.closePath();
        break;
      case "market":         // a ring
        ctx.arc(cx, cy, r * 0.85, 0, Math.PI * 2);
        break;
      case "factory":        // shed with a chimney
        ctx.moveTo(cx - r, cy + r); ctx.lineTo(cx - r, cy);
        ctx.lineTo(cx + r * 0.3, cy); ctx.lineTo(cx + r * 0.3, cy + r);
        ctx.closePath();
        ctx.moveTo(cx + r * 0.6, cy + r); ctx.lineTo(cx + r * 0.6, cy - r);
        ctx.lineTo(cx + r, cy - r); ctx.lineTo(cx + r, cy + r);
        ctx.closePath();
        break;
      case "barracks":       // a cross
        ctx.moveTo(cx - r, cy - r * 0.35); ctx.lineTo(cx + r, cy - r * 0.35);
        ctx.lineTo(cx + r, cy + r * 0.35); ctx.lineTo(cx - r, cy + r * 0.35);
        ctx.closePath();
        ctx.moveTo(cx - r * 0.35, cy - r); ctx.lineTo(cx + r * 0.35, cy - r);
        ctx.lineTo(cx + r * 0.35, cy + r); ctx.lineTo(cx - r * 0.35, cy + r);
        ctx.closePath();
        break;
      case "fort":           // crenellations
        ctx.moveTo(cx - r, cy + r); ctx.lineTo(cx - r, cy - r);
        ctx.lineTo(cx - r * 0.4, cy - r); ctx.lineTo(cx - r * 0.4, cy - r * 0.5);
        ctx.lineTo(cx + r * 0.4, cy - r * 0.5); ctx.lineTo(cx + r * 0.4, cy - r);
        ctx.lineTo(cx + r, cy - r); ctx.lineTo(cx + r, cy + r);
        ctx.closePath();
        break;
      default:
        ctx.arc(cx, cy, r * 0.6, 0, Math.PI * 2);
    }
    ctx.fill();
  }

  // ── Drawing ────────────────────────────────────────────────────────────
  let cell = 0;
  let originX = 0;
  let originY = 0;

  function draw() {
    const canvas = $("#wldMap");
    const b = board();
    if (!canvas || !b) return;

    const box = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(120, box.width);
    const h = Math.max(120, box.height - 26);   // room for the hint line

    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    cell = Math.floor(Math.min(w, h) / b.side);
    if (cell < 1) return;
    originX = Math.floor((w - cell * b.side) / 2);
    originY = Math.floor((h - cell * b.side) / 2);

    // Terrain, then an ownership wash on top. Two passes rather than one so
    // the tint is a consistent overlay and territory reads as territory.
    for (let y = 0; y < b.side; y++) {
      for (let x = 0; x < b.side; x++) {
        const px = originX + x * cell;
        const py = originY + y * cell;
        ctx.fillStyle = TERRAIN_FILL[terrainAt(x, y)] || TERRAIN_FILL.plain;
        ctx.fillRect(px, py, cell, cell);

        const owner = ownerAt(x, y);
        if (owner) {
          const p = playerByIndex(owner);
          if (p) {
            ctx.globalAlpha = p.i === me().index ? 0.34 : 0.24;
            ctx.fillStyle = p.colour;
            ctx.fillRect(px, py, cell, cell);
            ctx.globalAlpha = 1;
          }
        }
      }
    }

    // Territory outlines: only the edges between different owners, so the
    // board shows borders rather than graph paper.
    ctx.lineWidth = 1;
    for (let y = 0; y < b.side; y++) {
      for (let x = 0; x < b.side; x++) {
        const owner = ownerAt(x, y);
        if (!owner) continue;
        const p = playerByIndex(owner);
        if (!p) continue;
        ctx.strokeStyle = p.colour;
        ctx.globalAlpha = 0.85;
        const px = originX + x * cell;
        const py = originY + y * cell;
        if (ownerAt(x, y - 1) !== owner) line(ctx, px, py, px + cell, py);
        if (ownerAt(x, y + 1) !== owner) line(ctx, px, py + cell, px + cell, py + cell);
        if (ownerAt(x - 1, y) !== owner) line(ctx, px, py, px, py + cell);
        if (ownerAt(x + 1, y) !== owner) line(ctx, px + cell, py, px + cell, py + cell);
        ctx.globalAlpha = 1;
      }
    }

    // Buildings.
    (b.buildings || []).forEach((bl) => {
      const p = playerByIndex(bl.o);
      const cx = originX + bl.x * cell + cell / 2;
      const cy = originY + bl.y * cell + cell / 2;
      ctx.fillStyle = p ? p.colour : "#8f9bb3";
      ctx.strokeStyle = p ? p.colour : "#8f9bb3";
      ctx.lineWidth = Math.max(1, cell * 0.08);
      glyph(ctx, bl.b, cx, cy, Math.max(2, cell * 0.3));

      // A damage bar, but only once something has actually taken a hit.
      if (bl.hp < bl.max) {
        const frac = Math.max(0, bl.hp / bl.max);
        ctx.fillStyle = "#0b0f18";
        ctx.fillRect(originX + bl.x * cell + 1, originY + bl.y * cell + cell - 3,
                     cell - 2, 2);
        ctx.fillStyle = frac > 0.5 ? "#35c98b" : "#f2555a";
        ctx.fillRect(originX + bl.x * cell + 1, originY + bl.y * cell + cell - 3,
                     (cell - 2) * frac, 2);
      }
    });

    // Units, stacked count shown when more than one shares a tile.
    const stacks = {};
    (b.units || []).forEach((u) => {
      const key = `${u.x},${u.y}`;
      stacks[key] = (stacks[key] || 0) + 1;
    });
    (b.units || []).forEach((u) => {
      const p = playerByIndex(u.o);
      const cx = originX + u.x * cell + cell * 0.76;
      const cy = originY + u.y * cell + cell * 0.24;
      const r = Math.max(1.5, cell * 0.16);
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fillStyle = p ? p.colour : "#8f9bb3";
      ctx.fill();
      ctx.strokeStyle = "#090d15";
      ctx.lineWidth = 1;
      ctx.stroke();
      if (u.id === selectedUnit) {
        ctx.beginPath();
        ctx.arc(cx, cy, r + 2.5, 0, Math.PI * 2);
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    });

    // Selection.
    if (selected) {
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 2;
      ctx.strokeRect(originX + selected.x * cell + 1, originY + selected.y * cell + 1,
                     cell - 2, cell - 2);
    }
  }

  function line(ctx, x1, y1, x2, y2) {
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  // ── Panels ─────────────────────────────────────────────────────────────
  function renderLegend() {
    const b = board();
    if (!b) return;
    $("#wldLegend").innerHTML = (b.players || []).map((p) =>
      `<span class="wld-key${p.alive ? "" : " is-out"}">
         <i style="background:${esc(p.colour)}"></i>${esc(p.name)}${p.alive ? "" : " (out)"}
       </span>`).join("");
  }

  const myUserId = () =>
    ((board().players || []).find((p) => p.i === me().index) || {}).user_id;

  function renderStandings() {
    const rows = state.standings || [];
    const mine = myUserId();
    $("#wldStandings").innerHTML = `
      <thead><tr><th>#</th><th>Player</th><th class="rec-num">Score</th>
        <th class="rec-num">Land</th><th class="rec-num">Fact.</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr class="${r.user_id === mine ? "is-me" : ""}${r.alive ? "" : " is-out"}">
          <td>${r.rank}</td>
          <td><i class="wld-dot" style="background:${esc(r.colour)}"></i>${esc(r.name)}</td>
          <td class="rec-num"><strong>${r.score}</strong></td>
          <td class="rec-num">${r.tiles}</td>
          <td class="rec-num">${r.factories}</td>
        </tr>`).join("")}</tbody>`;
  }

  function renderLog() {
    const entries = (me().log || []).slice().reverse();
    $("#wldLog").innerHTML = entries.length
      ? entries.map((l) => `<div class="wld-line">${esc(l)}</div>`).join("")
      : '<p class="msp-muted">Nothing has happened yet. Build something.</p>';
  }

  function affordable(cost) {
    const m = me();
    return (cost.credits || 0) <= (m.credits || 0)
      && (cost.materials || 0) <= (m.materials || 0)
      && (cost.food || 0) <= (m.food || 0);
  }

  const priceTag = (cost) => Object.entries(cost)
    .map(([k, v]) => `${v} ${k === "credits" ? "cr" : k.slice(0, 3)}`).join(" · ") || "free";

  function renderSelection() {
    const title = $("#wldSelTitle");
    const sub = $("#wldSelSub");
    const body = $("#wldSelBody");
    if (!selected) {
      title.textContent = "Nothing selected";
      sub.textContent = "";
      body.innerHTML = `<p class="msp-muted">Pick a tile on the map to build on it, or pick
        one of your units to move it.</p>`;
      return;
    }

    const { x, y } = selected;
    const terrain = terrainAt(x, y);
    const owner = ownerAt(x, y);
    const ownerP = owner ? playerByIndex(owner) : null;
    const bl = buildingAt(x, y);
    const here = unitsAt(x, y);
    const mine = isMine(x, y);

    title.textContent = `(${x}, ${y}) · ${catalogue.terrain[terrain].label}`;
    sub.textContent = ownerP ? (mine ? "yours" : ownerP.name) : "unclaimed";

    const parts = [];

    // What is standing here.
    if (bl) {
      const spec = catalogue.buildings[bl.b];
      parts.push(`<div class="wld-block">
        <div class="wld-block-h">${esc(spec.label)}</div>
        <div class="msp-muted">${esc(spec.desc)}</div>
        <div class="wld-hp"><span style="width:${Math.max(0, 100 * bl.hp / bl.max)}%"></span></div>
        <div class="msp-muted">${bl.hp} / ${bl.max} hp</div>
      </div>`);
    }

    // Units on the tile — click one to take control of it.
    if (here.length) {
      parts.push(`<div class="wld-block">
        <div class="wld-block-h">Units here</div>
        <div class="wld-units">${here.map((u) => {
          const own = u.o === me().index;
          const spec = catalogue.units[u.k];
          return `<button class="wld-unit${u.id === selectedUnit ? " is-on" : ""}"
            data-unit="${esc(u.id)}" ${own ? "" : "disabled"}
            title="${own ? "Select this unit" : "Not yours"}">
            <i style="background:${esc((playerByIndex(u.o) || {}).colour || "#888")}"></i>
            ${esc(spec.label)} <span class="msp-muted">${u.hp} hp</span>
          </button>`;
        }).join("")}</div>
      </div>`);
    }

    // Orders for the unit currently in hand.
    const holding = (board().units || []).find((u) => u.id === selectedUnit);
    if (holding && holding.o === me().index) {
      const dist = chebyshev([holding.x, holding.y], [x, y]);
      const spec = catalogue.units[holding.k];
      const enemyHere = here.some((u) => u.o !== me().index);
      const enemyBuilding = bl && bl.o !== me().index;
      const orders = [];
      if (dist > 0) {
        orders.push(`<button class="btn ghost wld-act" data-do="move">Move here</button>`);
      }
      if (dist === 1 && (enemyHere || enemyBuilding) && spec.attack > 0) {
        orders.push(`<button class="btn danger wld-act" data-do="attack">Attack</button>`);
      }
      if (dist === 0 && spec.label === "Settler") {
        orders.push(`<button class="btn primary wld-act" data-do="found">Found outpost</button>`);
      }
      if (orders.length) {
        parts.push(`<div class="wld-block">
          <div class="wld-block-h">${esc(spec.label)} ${esc(holding.id)}
            <span class="msp-muted">${holding.moves_left ?? "?"} moves left</span></div>
          <div class="wld-acts">${orders.join("")}</div>
        </div>`);
      }
    }

    // Build menu — only where it could ever apply, so the panel is short.
    if (mine && !bl) {
      const options = Object.entries(catalogue.buildings)
        .filter(([k, s]) => k !== "base" && s.terrain.includes(terrain))
        .map(([k, s]) => {
          const ok = affordable(s.cost);
          return `<button class="wld-opt${ok ? "" : " is-off"}" data-build="${k}"
            title="${esc(s.desc)}">
            <span class="wld-opt-n">${esc(s.label)}</span>
            <span class="wld-opt-c">${esc(priceTag(s.cost))}</span>
          </button>`;
        });
      parts.push(`<div class="wld-block">
        <div class="wld-block-h">Build here</div>
        ${options.length ? `<div class="wld-opts">${options.join("")}</div>`
          : `<p class="msp-muted">Nothing can be built on
             ${esc(catalogue.terrain[terrain].label.toLowerCase())}.</p>`}
      </div>`);
    } else if (mine && bl && bl.b !== "base") {
      parts.push(`<div class="wld-block">
        <button class="btn ghost wld-act" data-do="demolish">Demolish</button>
        <span class="msp-muted">half the materials back</span>
      </div>`);
    } else if (!mine && !owner) {
      parts.push(`<p class="msp-muted">Unclaimed. Walk an explorer over it to take it.</p>`);
    }

    // Training, when you are standing on somewhere that can train.
    if (mine && bl && (bl.b === "base" || bl.b === "barracks")) {
      const trainable = Object.entries(catalogue.units).filter(
        ([, s]) => !s.needs || bl.b === "barracks" || hasBuilding(s.needs));
      parts.push(`<div class="wld-block">
        <div class="wld-block-h">Train here</div>
        <div class="wld-opts">${trainable.map(([k, s]) => {
          const ok = affordable(s.cost) && (me().workers_free ?? 0) >= (s.workers || 0);
          return `<button class="wld-opt${ok ? "" : " is-off"}" data-train="${k}"
            title="${esc(s.desc)}">
            <span class="wld-opt-n">${esc(s.label)}</span>
            <span class="wld-opt-c">${esc(priceTag(s.cost))}</span>
          </button>`;
        }).join("")}</div>
      </div>`);
    }

    body.innerHTML = parts.join("") || '<p class="msp-muted">Nothing to do here.</p>';
    bindSelection();
  }

  const hasBuilding = (kind) => (me().buildings || []).some((b) => b.kind === kind);

  function bindSelection() {
    const body = $("#wldSelBody");
    body.querySelectorAll("[data-build]").forEach((el) =>
      el.addEventListener("click", () => send([{
        type: "build", building: el.dataset.build, x: selected.x, y: selected.y,
      }])));

    body.querySelectorAll("[data-train]").forEach((el) =>
      el.addEventListener("click", () => send([{
        type: "train", unit: el.dataset.train, count: 1, x: selected.x, y: selected.y,
      }])));

    body.querySelectorAll("[data-unit]").forEach((el) =>
      el.addEventListener("click", () => {
        selectedUnit = selectedUnit === el.dataset.unit ? null : el.dataset.unit;
        renderSelection();
        draw();
      }));

    body.querySelectorAll("[data-do]").forEach((el) =>
      el.addEventListener("click", () => {
        const what = el.dataset.do;
        if (what === "demolish") {
          send([{ type: "demolish", x: selected.x, y: selected.y }]);
        } else if (what === "found") {
          send([{ type: "found", unit_id: selectedUnit }]);
          selectedUnit = null;
        } else {
          send([{ type: what, unit_id: selectedUnit, x: selected.x, y: selected.y }]);
        }
      }));
  }

  // ── The resource exchange ──────────────────────────────────────────────
  function openExchange() {
    const ex = catalogue.exchange;
    const body = $("#wldSelBody");
    $("#wldSelTitle").textContent = "Exchange";
    $("#wldSelSub").textContent = "convert credits into supplies, or back";
    selected = null;
    body.innerHTML = `
      <p class="msp-muted">Buy and sell at a fixed spread. Round-tripping costs you the
        difference, so it is a way to unblock a build, not a way to make money.</p>
      ${Object.entries(ex).map(([res, px]) => `
        <div class="wld-block">
          <div class="wld-block-h is-resource">${esc(res)}
            <span class="msp-muted">buy ${px.buy} · sell ${px.sell}</span></div>
          <div class="wld-trade">
            <input class="msp-input" type="number" min="1" step="1" value="20"
              id="qty-${esc(res)}" aria-label="quantity of ${esc(res)}">
            <button class="btn ghost" data-trade="buy" data-res="${esc(res)}">Buy</button>
            <button class="btn ghost" data-trade="sell" data-res="${esc(res)}">Sell</button>
          </div>
        </div>`).join("")}`;

    body.querySelectorAll("[data-trade]").forEach((el) =>
      el.addEventListener("click", () => {
        const res = el.dataset.res;
        const qty = Number($(`#qty-${res}`).value || 0);
        send([{ type: "trade", side: el.dataset.trade, resource: res, qty }]);
      }));
    draw();
  }

  // ── Interaction ────────────────────────────────────────────────────────
  function bindCanvas() {
    const canvas = $("#wldMap");
    canvas.addEventListener("click", (e) => {
      if (!board() || !cell) return;
      const box = canvas.getBoundingClientRect();
      const x = Math.floor((e.clientX - box.left - originX) / cell);
      const y = Math.floor((e.clientY - box.top - originY) / cell);
      if (x < 0 || y < 0 || x >= side() || y >= side()) return;

      selected = { x, y };
      // Picking up your own lone unit on a tile saves a second click, which
      // matters when you are moving something across the map.
      const mineHere = unitsAt(x, y).filter((u) => u.o === me().index);
      if (mineHere.length === 1 && !selectedUnit) selectedUnit = mineHere[0].id;

      renderSelection();
      draw();
    });

    canvas.addEventListener("mousemove", (e) => {
      if (!board() || !cell) return;
      const box = canvas.getBoundingClientRect();
      const x = Math.floor((e.clientX - box.left - originX) / cell);
      const y = Math.floor((e.clientY - box.top - originY) / cell);
      canvas.style.cursor =
        (x >= 0 && y >= 0 && x < side() && y < side()) ? "pointer" : "default";
    });

    window.addEventListener("resize", draw);
  }

  // ── Poll loop ──────────────────────────────────────────────────────────
  async function refresh() {
    try {
      state = await api(`/market-sim-py/run/${runId()}/world`);
    } catch {
      return;   // the market poll surfaces connection trouble already
    }
    if (!state.map) return;
    renderLegend();
    renderStandings();
    renderLog();
    if (selected) renderSelection();
    draw();
    updateStrip();
  }

  /* The empire keeps its own strip rather than sharing the trading one: five
   * more numbers in that row is exactly the crowding the board is meant to
   * avoid, and the two halves are one click apart. */
  function updateStrip() {
    const m = me();
    const box = $("#wldStrip");
    if (!box) return;
    if (!m.joined) {
      box.innerHTML = `<div class="msp-stat msp-stat-wide">
        <span class="msp-stat-value">Spectating</span>
        <span class="msp-stat-label">join the run to be given a base</span></div>`;
      return;
    }
    const stat = (label, value, sub, key) =>
      `<div class="msp-stat${key ? " msp-stat-key" : ""}">
         <span class="msp-stat-label">${label}</span>
         <span class="msp-stat-value">${value}${
           sub ? `<span class="msp-stat-sub"> ${sub}</span>` : ""}</span></div>`;

    const d = m.development;
    const fromTrading = Math.round(m.pnl_credits);
    box.innerHTML =
      stat("Credits", Math.round(m.credits).toLocaleString(),
           `<span class="${fromTrading >= 0 ? "msp-up" : "msp-down"}">${
             fromTrading >= 0 ? "+" : ""}${fromTrading.toLocaleString()} traded</span>`) +
      stat("Materials", Math.round(m.materials)) +
      stat("Food", Math.round(m.food)) +
      stat("Workers", `${m.workers}/${m.pop_cap}`, `${m.workers_free} free`) +
      stat("Land", d.tiles, `${d.buildings} built`) +
      stat("Development", d.score, rankLine(), true);
  }

  function rankLine() {
    const rows = state.standings || [];
    const mine = rows.find((r) => r.user_id === myUserId());
    return mine ? `rank ${mine.rank} of ${rows.length}` : "";
  }

  function show(which) {
    const world = which === "world";
    $("#marketBoard").classList.toggle("hidden", world);
    $("#worldBoard").classList.toggle("hidden", !world);
    $("#tabMarket").classList.toggle("is-on", !world);
    $("#tabWorld").classList.toggle("is-on", world);
    $("#tabMarket").setAttribute("aria-selected", String(!world));
    $("#tabWorld").setAttribute("aria-selected", String(world));
    if (world) {
      refresh();
      // Two frames: one for the panel to take its width, one to measure it.
      requestAnimationFrame(draw);
      setTimeout(draw, 60);
    }
  }

  async function start() {
    if (live) return;
    live = true;
    try {
      catalogue = await api("/market-sim-py/world/catalogue");
    } catch {
      live = false;
      return;
    }
    $("#boardSwitch").classList.remove("hidden");
    bindCanvas();
    $("#tabMarket").addEventListener("click", () => show("market"));
    $("#tabWorld").addEventListener("click", () => show("world"));
    $("#wldExchange").addEventListener("click", openExchange);
    await refresh();
    timer = setInterval(() => {
      if (!$("#worldBoard").classList.contains("hidden")) refresh();
      else updateStrip();
    }, POLL_MS);
  }

  function stop() {
    clearInterval(timer);
    timer = null;
  }

  // The market script owns the run lifecycle; this hooks onto it rather than
  // polling for a status of its own.
  window.AlphaWorld = { start, stop, refresh, show };

  document.addEventListener("DOMContentLoaded", () => {
    if (window.RUN_ID) start();
  });
})();
