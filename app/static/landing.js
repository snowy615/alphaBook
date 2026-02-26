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

  // ---- Build flat game grid ----
  function buildGameGrid() {
    const grid = $("#gameGrid");
    if (!grid) return;
    grid.innerHTML = "";

    const groups = window.GAME_GROUPS || {};

    // 1) Market Simulation card (always first)
    const marketGames = groups.market || [];
    if (marketGames.length > 0) {
      const card = createCard({
        icon: "📈",
        name: "Market Simulation",
        subtitle: `${marketGames.length} stocks`,
        accentFrom: "#9d8cff",
        accentTo: "#6c5ce7",
        onClick: () => navigate("/market"),
      });
      grid.appendChild(card);
    }

    // 2) 5Os card (always present)
    const fiveOsCard = createCard({
      icon: "🎲",
      name: "5Os",
      subtitle: "Card Game",
      accentFrom: "#ffb088",
      accentTo: "#e17055",
      onClick: () => navigate("/5os"),
    });
    grid.appendChild(fiveOsCard);

    // 3) Headline card (always present)
    const headlineCard = createCard({
      icon: "📰",
      name: "Headline",
      subtitle: "Trading Game",
      accentFrom: "#a29bfe",
      accentTo: "#6c5ce7",
      onClick: () => navigate("/headline"),
    });
    grid.appendChild(headlineCard);

    // 3) Custom games (each as its own card)
    const otherGames = groups.other || [];
    otherGames.forEach(game => {
      const card = createCard({
        icon: game.name.charAt(0).toUpperCase(),
        name: game.name,
        subtitle: "Custom Game",
        accentFrom: "#88ffcc",
        accentTo: "#00cec9",
        onClick: () => navigate(`/trade/${game.symbol}`),
      });
      grid.appendChild(card);
    });
  }

  function createCard({ icon, name, subtitle, accentFrom, accentTo, comingSoon, onClick }) {
    const card = document.createElement("div");
    card.className = "equity-card" + (comingSoon ? " coming-soon-card" : "");

    card.innerHTML = `
      <div class="equity-icon" style="background: radial-gradient(circle at 30% 30%, ${accentFrom}, ${accentTo});">${icon}</div>
      <div class="equity-name">${name}</div>
      <div class="equity-price ${comingSoon ? 'coming-soon-label' : ''}" style="font-size: 16px; ${comingSoon ? '' : 'color: var(--muted);'}">${subtitle}</div>
    `;

    if (onClick) {
      card.addEventListener("click", () => {
        if (!isAuthed) {
          window.location.href = "/login";
        } else {
          onClick();
        }
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