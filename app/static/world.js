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
  const DEFAULT_HINT = "Hover a tile to read it, click to act on it.";

  // Terrain fills, keyed to the site's navy palette so the map sits inside the
  // page rather than on top of it. They are spread further apart in lightness
  // than the palette would suggest, because an ownership wash goes over the top
  // and squashes the difference between them.
  // Deliberately desaturated. Terrain has to be distinguishable from terrain,
  // but a vivid ground competes with the player colours washed over it, and it
  // is the ownership that has to win that fight.
  const TERRAIN_FILL = {
    water:  "#0a1424",   // darkest, so open water reads as a hole in the board
    plain:  "#1b2436",
    forest: "#1a2f24",
    hills:  "#302c40",
  };

  // Per-tile detail colours, a touch lighter than the ground they sit on.
  const TERRAIN_DETAIL = {
    water:  "#16233a",
    plain:  "#243049",
    forest: "#2c4a36",
    hills:  "#443e5c",
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
      hint.textContent = DEFAULT_HINT;
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

  // Unit silhouettes. A shape per role reads at a glance; a coloured dot for
  // everything would tell you who owns it and nothing about what it is.
  function unitShape(ctx, kind, cx, cy, r) {
    ctx.beginPath();
    switch (kind) {
      case "explorer":                       // a scout's pennant
        ctx.moveTo(cx, cy - r); ctx.lineTo(cx + r, cy + r * 0.7);
        ctx.lineTo(cx - r, cy + r * 0.7);
        break;
      case "settler":                        // a cart
        ctx.rect(cx - r * 0.85, cy - r * 0.7, r * 1.7, r * 1.4);
        break;
      case "soldier":                        // a shield
        ctx.arc(cx, cy, r * 0.9, 0, Math.PI * 2);
        break;
      case "cavalry":                        // a lance point
        ctx.moveTo(cx, cy - r); ctx.lineTo(cx + r, cy);
        ctx.lineTo(cx, cy + r); ctx.lineTo(cx - r, cy);
        break;
      case "cannon": {                       // a hexagon
        for (let i = 0; i < 6; i++) {
          const a = (Math.PI / 3) * i - Math.PI / 2;
          const fn = i ? "lineTo" : "moveTo";
          ctx[fn](cx + r * Math.cos(a), cy + r * Math.sin(a));
        }
        break;
      }
      default:
        ctx.arc(cx, cy, r * 0.8, 0, Math.PI * 2);
    }
    ctx.closePath();
  }

  // ── Drawing ────────────────────────────────────────────────────────────
  let cell = 0;
  let originX = 0;
  let originY = 0;
  let hover = null;

  // The terrain never changes during a run, so it is painted once into an
  // offscreen canvas and blitted each frame. That keeps the per-frame work to
  // ownership, buildings and units, and lets the ground carry texture that
  // would be far too expensive to redraw on every poll.
  let terrainLayer = null;
  let terrainKey = "";

  /* A stable pseudo-random value per tile, so the texture is identical on every
   * redraw. Math.random() here would make the ground shimmer on each poll. */
  function tileNoise(x, y) {
    const n = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
    return n - Math.floor(n);
  }

  function buildTerrainLayer(b, size) {
    const key = `${b.side}:${cell}:${size}`;
    if (terrainLayer && terrainKey === key) return;
    terrainKey = key;

    const layer = document.createElement("canvas");
    layer.width = size;
    layer.height = size;
    const g = layer.getContext("2d");

    for (let y = 0; y < b.side; y++) {
      for (let x = 0; x < b.side; x++) {
        const px = x * cell;
        const py = y * cell;
        const kind = terrainAt(x, y);
        const n = tileNoise(x, y);

        g.fillStyle = TERRAIN_FILL[kind] || TERRAIN_FILL.plain;
        g.fillRect(px, py, cell, cell);

        // A faint lightness wobble stops large stretches reading as one flat
        // slab of colour.
        g.globalAlpha = 0.05 + n * 0.06;
        g.fillStyle = TERRAIN_DETAIL[kind] || TERRAIN_DETAIL.plain;
        g.fillRect(px, py, cell, cell);
        g.globalAlpha = 1;

        if (cell < 8) continue;              // too small for detail to help
        g.fillStyle = TERRAIN_DETAIL[kind] || TERRAIN_DETAIL.plain;
        g.strokeStyle = TERRAIN_DETAIL[kind] || TERRAIN_DETAIL.plain;
        g.lineWidth = 1;

        if (kind === "forest") {
          // Two or three little conifers, placed from the tile's own noise.
          // Held under full opacity so a forest belt reads as texture rather
          // than as a second, competing block of colour.
          g.globalAlpha = 0.8;
          const trees = 2 + Math.floor(n * 2);
          for (let i = 0; i < trees; i++) {
            const tx = px + cell * (0.22 + tileNoise(x + i * 7, y) * 0.56);
            const ty = py + cell * (0.26 + tileNoise(x, y + i * 13) * 0.5);
            const s = cell * 0.17;
            g.beginPath();
            g.moveTo(tx, ty - s);
            g.lineTo(tx + s * 0.8, ty + s * 0.7);
            g.lineTo(tx - s * 0.8, ty + s * 0.7);
            g.closePath();
            g.fill();
          }
          g.globalAlpha = 1;
        } else if (kind === "hills") {
          // A ridge line: two humps, offset by the tile's noise.
          g.beginPath();
          const base = py + cell * 0.68;
          g.moveTo(px + cell * 0.14, base);
          g.quadraticCurveTo(px + cell * (0.3 + n * 0.08), py + cell * 0.32,
                             px + cell * 0.5, base);
          g.quadraticCurveTo(px + cell * (0.66 + n * 0.08), py + cell * 0.4,
                             px + cell * 0.86, base);
          g.stroke();
        } else if (kind === "water") {
          // Two short swells.
          g.globalAlpha = 0.65;
          for (let i = 0; i < 2; i++) {
            const wy = py + cell * (0.34 + i * 0.28 + n * 0.1);
            g.beginPath();
            g.moveTo(px + cell * 0.18, wy);
            g.quadraticCurveTo(px + cell * 0.34, wy - cell * 0.1,
                               px + cell * 0.5, wy);
            g.quadraticCurveTo(px + cell * 0.66, wy + cell * 0.1,
                               px + cell * 0.82, wy);
            g.stroke();
          }
          g.globalAlpha = 1;
        } else if (n > 0.72) {
          // Sparse grass tufts, so plains are not featureless.
          g.globalAlpha = 0.5;
          const gx = px + cell * (0.3 + n * 0.4);
          const gy = py + cell * (0.4 + tileNoise(y, x) * 0.3);
          g.fillRect(gx, gy, Math.max(1, cell * 0.07), Math.max(1, cell * 0.07));
          g.globalAlpha = 1;
        }
      }
    }
    terrainLayer = layer;
  }

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
    const size = cell * b.side;
    originX = Math.floor((w - size) / 2);
    originY = Math.floor((h - size) / 2);

    buildTerrainLayer(b, size);
    ctx.drawImage(terrainLayer, originX, originY);

    // A frame, so the board reads as an object rather than a bleed.
    ctx.strokeStyle = "rgba(255,255,255,.07)";
    ctx.lineWidth = 1;
    ctx.strokeRect(originX + .5, originY + .5, size - 1, size - 1);

    const mine = me().index;

    // Ownership wash. Your own ground is a touch stronger than everyone
    // else's, so you can find yourself on a crowded board immediately.
    for (let y = 0; y < b.side; y++) {
      for (let x = 0; x < b.side; x++) {
        const owner = ownerAt(x, y);
        if (!owner) continue;
        const p = playerByIndex(owner);
        if (!p) continue;
        ctx.globalAlpha = owner === mine ? 0.32 : 0.2;
        ctx.fillStyle = p.colour;
        ctx.fillRect(originX + x * cell, originY + y * cell, cell, cell);
      }
    }
    ctx.globalAlpha = 1;

    // Territory outlines: only the edges where the owner changes, so the board
    // shows borders rather than graph paper.
    ctx.lineJoin = "miter";
    for (let y = 0; y < b.side; y++) {
      for (let x = 0; x < b.side; x++) {
        const owner = ownerAt(x, y);
        if (!owner) continue;
        const p = playerByIndex(owner);
        if (!p) continue;
        ctx.strokeStyle = p.colour;
        ctx.globalAlpha = owner === mine ? 1 : 0.8;
        ctx.lineWidth = owner === mine ? 2 : 1.25;
        const px = originX + x * cell;
        const py = originY + y * cell;
        if (ownerAt(x, y - 1) !== owner) line(ctx, px, py, px + cell, py);
        if (ownerAt(x, y + 1) !== owner) line(ctx, px, py + cell, px + cell, py + cell);
        if (ownerAt(x - 1, y) !== owner) line(ctx, px, py, px, py + cell);
        if (ownerAt(x + 1, y) !== owner) line(ctx, px + cell, py, px + cell, py + cell);
      }
    }
    ctx.globalAlpha = 1;

    // Buildings, each on a dark plate so the glyph stays legible over forest,
    // hills and an ownership wash alike.
    (b.buildings || []).forEach((bl) => {
      const p = playerByIndex(bl.o);
      const px = originX + bl.x * cell;
      const py = originY + bl.y * cell;
      const cx = px + cell / 2;
      const cy = py + cell / 2;
      const colour = p ? p.colour : "#8f9bb3";

      ctx.fillStyle = "rgba(6,10,18,.62)";
      ctx.fillRect(px + cell * 0.1, py + cell * 0.1, cell * 0.8, cell * 0.8);

      // A base is the thing worth defending, so it gets a ring of its own.
      if (bl.b === "base") {
        ctx.strokeStyle = colour;
        ctx.lineWidth = Math.max(1.2, cell * 0.09);
        ctx.beginPath();
        ctx.arc(cx, cy, cell * 0.44, 0, Math.PI * 2);
        ctx.stroke();
      }

      ctx.fillStyle = colour;
      ctx.strokeStyle = colour;
      ctx.lineWidth = Math.max(1, cell * 0.08);
      glyph(ctx, bl.b, cx, cy, Math.max(2, cell * 0.26));

      if (bl.hp < bl.max) {
        const frac = Math.max(0, bl.hp / bl.max);
        const barY = py + cell - Math.max(2, cell * 0.12);
        const barH = Math.max(2, cell * 0.09);
        ctx.fillStyle = "rgba(6,10,18,.85)";
        ctx.fillRect(px + 1, barY, cell - 2, barH);
        ctx.fillStyle = frac > 0.5 ? "#35c98b" : "#f2555a";
        ctx.fillRect(px + 1, barY, (cell - 2) * frac, barH);
      }
    });

    // Units sit in the tile's top-right so they never hide the building under
    // them. Where several share a tile, one marker carries the count.
    const stacks = {};
    (b.units || []).forEach((u) => {
      const key = `${u.x},${u.y}`;
      (stacks[key] = stacks[key] || []).push(u);
    });

    Object.values(stacks).forEach((group) => {
      const u = group[0];
      const p = playerByIndex(u.o);
      const cx = originX + u.x * cell + cell * 0.72;
      const cy = originY + u.y * cell + cell * 0.28;
      const r = Math.max(2, cell * 0.2);

      ctx.beginPath();
      ctx.arc(cx, cy, r + 1.5, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(6,10,18,.75)";
      ctx.fill();

      unitShape(ctx, u.k, cx, cy, r);
      ctx.fillStyle = p ? p.colour : "#8f9bb3";
      ctx.fill();
      ctx.strokeStyle = "rgba(6,10,18,.9)";
      ctx.lineWidth = 1;
      ctx.stroke();

      if (group.length > 1 && cell >= 14) {
        ctx.fillStyle = "#fff";
        ctx.font = `600 ${Math.max(8, cell * 0.32)}px ui-monospace, Menlo, monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(String(group.length),
                     originX + u.x * cell + cell * 0.3,
                     originY + u.y * cell + cell * 0.72);
      }

      if (group.some((g) => g.id === selectedUnit)) {
        ctx.beginPath();
        ctx.arc(cx, cy, r + 4, 0, Math.PI * 2);
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    });

    // Where the unit in hand could actually reach this tick.
    const holding = (b.units || []).find((u) => u.id === selectedUnit);
    if (holding && holding.o === mine && holding.moves_left > 0) {
      ctx.fillStyle = "rgba(255,255,255,.10)";
      reachable(holding).forEach((key) => {
        const [x, y] = key.split(",").map(Number);
        ctx.fillRect(originX + x * cell + 1, originY + y * cell + 1, cell - 2, cell - 2);
      });
    }

    if (hover && !(selected && hover.x === selected.x && hover.y === selected.y)) {
      ctx.strokeStyle = "rgba(255,255,255,.45)";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(originX + hover.x * cell + 1, originY + hover.y * cell + 1,
                     cell - 2, cell - 2);
    }

    if (selected) {
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 2;
      ctx.strokeRect(originX + selected.x * cell + 1, originY + selected.y * cell + 1,
                     cell - 2, cell - 2);
      // Corner ticks, so the selection stays findable over a bright tint.
      ctx.lineWidth = 3;
      const px = originX + selected.x * cell;
      const py = originY + selected.y * cell;
      const t = Math.max(3, cell * 0.3);
      [[0, 0, 1, 1], [cell, 0, -1, 1], [0, cell, 1, -1], [cell, cell, -1, -1]]
        .forEach(([ox, oy, sx, sy]) => {
          line(ctx, px + ox, py + oy, px + ox + t * sx, py + oy);
          line(ctx, px + ox, py + oy, px + ox, py + oy + t * sy);
        });
    }
  }

  /* Every tile a unit can still step onto this tick.
   *
   * Rough ground costs more to enter, so a plain radius would promise moves the
   * server then refuses. This walks the same cost-weighted flood the engine
   * does, reading the costs out of the server's own catalogue rather than
   * hardcoding them, so the highlight cannot quietly disagree with the rules.
   */
  function reachable(unit) {
    const b = board();
    const budget = unit.moves_left;
    const start = `${unit.x},${unit.y}`;
    const best = { [start]: 0 };
    let frontier = [[unit.x, unit.y]];

    while (frontier.length) {
      const next = [];
      frontier.forEach(([x, y]) => {
        const spent = best[`${x},${y}`];
        for (let dy = -1; dy <= 1; dy++) {
          for (let dx = -1; dx <= 1; dx++) {
            if (!dx && !dy) continue;
            const nx = x + dx;
            const ny = y + dy;
            if (nx < 0 || ny < 0 || nx >= b.side || ny >= b.side) continue;
            const t = terrainAt(nx, ny);
            if (!catalogue.terrain[t].passable) continue;
            const cost = spent + catalogue.terrain[t].move_cost;
            if (cost > budget) continue;
            const key = `${nx},${ny}`;
            if (best[key] === undefined || cost < best[key]) {
              best[key] = cost;
              next.push([nx, ny]);
            }
          }
        }
      });
      frontier = next;
    }

    delete best[start];
    return Object.keys(best);
  }

  function line(ctx, x1, y1, x2, y2) {
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  // ── Hover tooltip ──────────────────────────────────────────────────────
  /* Reading a tile should not cost a click. The tooltip answers the three
   * questions you have while scanning the board — what is this ground, whose
   * is it, what is on it — and the panel stays for the things you act on. */
  function showTip(at) {
    const tip = $("#wldTip");
    if (!tip) return;
    if (!at || !board()) {
      tip.classList.add("hidden");
      return;
    }

    const { x, y } = at;
    const terrain = terrainAt(x, y);
    const owner = ownerAt(x, y);
    const p = owner ? playerByIndex(owner) : null;
    const bl = buildingAt(x, y);
    const here = unitsAt(x, y);

    const rows = [
      `<div class="wld-tip-h">(${x}, ${y}) · ${esc(catalogue.terrain[terrain].label)}</div>`,
      p ? `<div><i class="wld-dot" style="background:${esc(p.colour)}"></i>${
            esc(p.name)}${owner === me().index ? " (you)" : ""}</div>`
        : `<div class="msp-muted">unclaimed</div>`,
    ];
    if (bl) {
      rows.push(`<div>${esc(catalogue.buildings[bl.b].label)}
        <span class="msp-muted">${Math.round(bl.hp)}/${bl.max} hp</span></div>`);
    }
    if (here.length) {
      const counts = {};
      here.forEach((u) => { counts[u.k] = (counts[u.k] || 0) + 1; });
      rows.push(`<div class="msp-muted">${Object.entries(counts)
        .map(([k, n]) => `${n}x ${esc(catalogue.units[k].label)}`).join(", ")}</div>`);
    }
    const def = catalogue.terrain[terrain].defense;
    if (def > 0) {
      rows.push(`<div class="msp-muted">+${Math.round(def * 100)}% defence</div>`);
    }

    tip.innerHTML = rows.join("");
    tip.classList.remove("hidden");

    // Keep the tooltip inside the map panel rather than letting it push the
    // page sideways at the right-hand edge.
    const wrap = tip.offsetParent.getBoundingClientRect();
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    let left = at.clientX - wrap.left + 14;
    let top = at.clientY - wrap.top + 14;
    if (left + w > wrap.width - 6) left = at.clientX - wrap.left - w - 14;
    if (top + h > wrap.height - 6) top = at.clientY - wrap.top - h - 14;
    tip.style.left = `${Math.max(6, left)}px`;
    tip.style.top = `${Math.max(6, top)}px`;
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
      const inside = x >= 0 && y >= 0 && x < side() && y < side();
      canvas.style.cursor = inside ? "pointer" : "default";

      const changed = inside
        ? (!hover || hover.x !== x || hover.y !== y)
        : hover !== null;
      hover = inside ? { x, y } : null;
      showTip(inside ? { x, y, clientX: e.clientX, clientY: e.clientY } : null);
      if (changed) draw();
    });

    canvas.addEventListener("mouseleave", () => {
      hover = null;
      showTip(null);
      draw();
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
