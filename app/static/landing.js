(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const fetchJSON = async (url, init) => {
    const r = await fetch(url, { credentials: "include", ...init });
    if (!r.ok) throw new Error(String(r.status));
    const txt = await r.text();
    try { return JSON.parse(txt); } catch { return {}; }
  };

  let isAuthed = false;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

  // ---- Build flat game grid ----
  function buildGameGrid() {
    const grid = $("#gameGrid");
    if (!grid) return;
    grid.innerHTML = "";

    const groups = window.GAME_GROUPS || {};
    const marketGames = groups.market || [];

    // Fixed modes, in the order they are meant to be discovered. The tag is a
    // monogram, not an emoji — it reads as an instrument code on the board.
    const modes = [
      marketGames.length ? {
        tag: "MKT",
        name: "Market Simulation",
        subtitle: `Live limit order book across ${marketGames.length} stocks`,
        href: "/market",
      } : null,
      {
        tag: "MSC",
        name: "Market Simulation Coding",
        subtitle: "Write a bot, run it locally, trade it for ten minutes",
        href: "/market-sim-py",
      },
      {
        tag: "SWE",
        name: "SWE Prep",
        subtitle: "Write Python in the browser, sandboxed on the server",
        href: "/swe-prep",
      },
      {
        tag: "HDL",
        name: "Headline",
        subtitle: "News hits the wire — trade the story before the bell",
        href: "/headline",
      },
      {
        tag: "5OS",
        name: "5Os",
        subtitle: "Estimate the five statistics of a hidden hand",
        href: "/5os",
      },
      {
        tag: "PKR",
        name: "Poker Auction",
        subtitle: "Sealed-bid auctions, then trade the hands you build",
        href: "/poker-auction",
      },
      {
        tag: "MM",
        name: "Mental Math",
        subtitle: "Timed arithmetic drills, configurable difficulty",
        href: "/mental-math",
      },
    ].filter(Boolean);

    modes.forEach((m) => grid.appendChild(createCard({
      tag: m.tag,
      name: m.name,
      subtitle: m.subtitle,
      onClick: () => navigate(m.href),
    })));

    // Custom games created by an instructor, each as its own card.
    (groups.other || []).forEach((game) => {
      grid.appendChild(createCard({
        tag: String(game.symbol || game.name).slice(0, 4).toUpperCase(),
        name: game.name,
        subtitle: "Custom game",
        onClick: () => navigate(`/trade/${game.symbol}`),
      }));
    });
  }

  function createCard({ tag, name, subtitle, onClick }) {
    const card = document.createElement("div");
    card.className = "equity-card";
    card.setAttribute("role", "link");
    card.setAttribute("tabindex", "0");

    card.innerHTML = `
      <div class="equity-icon">${esc(tag)}</div>
      <div class="equity-name">${esc(name)}</div>
      <div class="equity-price">${esc(subtitle)}</div>
    `;

    if (onClick) {
      const go = () => {
        if (!isAuthed) window.location.href = "/login";
        else onClick();
      };
      card.addEventListener("click", go);
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    }

    return card;
  }

  function navigate(path) {
    window.location.href = path;
  }

  // ---- Auth UI ----
  async function initAuthUI() {
    const loginBox = $("#loginBox");
    const userBox = $("#userBox");
    const userNameEl = $("#userName");
    const adminLink = $("#adminLink");

    function showGuest() {
      isAuthed = false;
      loginBox?.classList.remove("hidden");
      userBox?.classList.add("hidden");
      if (adminLink) adminLink.style.display = "none";
    }

    function showUser(nameLike, isAdmin) {
      isAuthed = true;
      if (userNameEl) userNameEl.textContent = String(nameLike || "user");
      loginBox?.classList.add("hidden");
      userBox?.classList.remove("hidden");
      if (adminLink) {
        adminLink.style.display = isAdmin ? "inline-block" : "none";
      }
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

  // Initialize
  buildGameGrid();
  initAuthUI();
})();