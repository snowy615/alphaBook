(function () {
  "use strict";

  const GAME_ID = window.GAME_ID;
  const $ = (sel, root = document) => root.querySelector(sel);
  let pollTimer = null;
  let lastStateHash = "";

  const fetchJSON = async (url, init) => {
    const r = await fetch(url, { credentials: "include", ...init });
    if (!r.ok) {
      const txt = await r.text();
      let detail = r.status;
      try { detail = JSON.parse(txt).detail || detail; } catch { }
      throw new Error(detail);
    }
    return r.json();
  };

  async function getMe() {
    try {
      const me = await fetchJSON("/me");
      $("#userName").textContent = me.username || "user";
      return me;
    } catch { return null; }
  }

  async function poll() {
    try {
      const state = await fetchJSON(`/poker-auction/game/${GAME_ID}/state`);
      render(state);
    } catch (e) {
      console.error("Poll error:", e);
    }
  }

  function startPolling() {
    poll();
    pollTimer = setInterval(poll, 2000);
  }

  function stateHash(s) {
    return s.status + "|" + s.round + "|" + s.round_phase + "|" + s.num_bids + "|" +
      (s.teams || []).map(t => t.team_id + ":" + t.money + ":" + t.card_count).join(",");
  }

  // ---- Card rendering ----
  function cardHTML(label, size = "normal") {
    if (!label) return "";
    const suit = label.slice(-1);
    const rank = label.slice(0, -1);
    const isRed = suit === "♥" || suit === "♦";
    const cls = `pa-card ${size} ${isRed ? "red" : "black"}`;
    return `<span class="${cls}"><span class="pa-card-rank">${rank}</span><span class="pa-card-suit">${suit}</span></span>`;
  }

  function cardsHTML(labels, size) {
    return (labels || []).map(l => cardHTML(l, size)).join("");
  }

  // ---- Main render ----
  function render(state) {
    const area = $("#gameArea");
    if (!area) return;

    const hash = stateHash(state);

    if (state.status === "lobby") {
      if (hash === lastStateHash) return;
      lastStateHash = hash;
      renderLobby(area, state);
    } else if (state.status === "active") {
      if (hash === lastStateHash) return;
      lastStateHash = hash;
      renderActive(area, state);
    } else if (state.status === "post_auction") {
      if (hash === lastStateHash) return;
      lastStateHash = hash;
      renderPostAuction(area, state);
    } else if (state.status === "post_bidding") {
      if (hash === lastStateHash) return;
      lastStateHash = hash;
      renderPostBidding(area, state);
    } else if (state.status === "finished") {
      if (hash === lastStateHash) return;
      lastStateHash = hash;
      renderFinished(area, state);
    }
  }

  // ---- LOBBY ----
  function renderLobby(area, state) {
    const teams = state.teams || [];
    const teamRows = teams.map(t => `
      <div class="lobby-player">
        <span class="lobby-name">${t.team_name}</span>
      </div>
    `).join("");

    const adminPanel = state.is_admin ? `
      <div class="admin-controls">
        <div class="join-code-display">
          <span class="label">Join Code</span>
          <span class="code">${state.join_code}</span>
        </div>
        <button onclick="paStartGame()" class="btn" id="startBtn" ${teams.length < 1 ? 'disabled title="Need 1+ teams"' : ''}>
          Start Game →
        </button>
      </div>
    ` : '';

    area.innerHTML = `
      <div class="fiveos-lobby">
        <h2>🃏 Poker Auction — Lobby</h2>
        ${adminPanel}
        <h3>Teams (${teams.length})</h3>
        <div class="lobby-players">${teamRows || '<p class="muted">Waiting for teams to join...</p>'}</div>
        ${!state.is_admin ? '<p class="muted" style="margin-top:16px;">Waiting for admin to start the game...</p>' : ''}
      </div>
    `;
  }

  // ---- ACTIVE (Round auction) ----
  function renderActive(area, state) {
    const round = state.round;
    const phase = state.round_phase;
    const roundCards = state.round_cards || [];
    const myCards = state.my_cards || [];
    const myMoney = state.my_money || 0;
    const teams = state.teams || [];

    const teamTable = teams.map(t => {
      const isMine = t.team_id === state.my_team_id;
      const hasBid = (state.bids_submitted || []).includes(t.team_id);
      return `
        <div class="pa-team-row ${isMine ? 'pa-mine' : ''}">
          <span class="pa-team-name">${t.team_name} ${isMine ? '(You)' : ''}</span>
          <span class="pa-team-cards">${t.card_count} cards</span>
          <span class="pa-team-money">$${t.money}</span>
          <span class="pa-team-bid-status">${phase === 'bidding' ? (hasBid ? '✅' : '⏳') : ''}</span>
        </div>
      `;
    }).join("");

    let actionHTML = '';
    if (phase === "bidding" && round > 0) {
      if (state.my_bid !== null && state.my_bid !== undefined) {
        actionHTML = `<div class="pa-bid-submitted">✅ Your bid: <strong>$${state.my_bid}</strong></div>`;
      } else {
        const minBid = (round - 1) * 5;
        actionHTML = `
          <div class="pa-bid-form">
            <label>Your bid for these cards:</label>
            <div style="display:flex;gap:8px;align-items:center;margin-top:6px;">
              <span style="font-size:20px;font-weight:700;color:var(--brand);">$</span>
              <input type="number" id="bidAmount" min="${minBid}" max="${myMoney}" placeholder="${minBid}"
                style="width:120px;padding:8px 12px;background:var(--card-bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:18px;text-align:center;">
              <button onclick="paSubmitBid()" class="btn" id="bidBtn">Submit Bid</button>
            </div>
            <p class="muted small" style="margin-top:4px;">Min bid: $${minBid} • Budget: $${myMoney} • Winner pays 2nd highest bid</p>
            <p id="bidMsg" class="small" style="margin-top:4px;"></p>
          </div>
        `;
      }
    } else if (phase === "result") {
      const winner = state.round_winner;
      const paid = state.round_paid || 0;
      const bids = state.round_bids || {};
      const bidsHTML = Object.entries(bids).map(([tid, amt]) => {
        const t = teams.find(x => x.team_id === tid);
        const isWinner = tid === winner;
        return `<span class="pa-bid-chip ${isWinner ? 'winner' : ''}">${t ? t.team_name : tid}: $${amt}</span>`;
      }).join("");

      actionHTML = `
        <div class="pa-result-box">
          <div class="pa-result-title">${winner ? `🎉 ${teams.find(t => t.team_id === winner)?.team_name || winner} wins!` : '❌ No bids — cards discarded'}</div>
          ${winner ? `<div class="pa-result-paid">Paid: <strong>$${paid}</strong> (2nd highest bid)</div>` : ''}
          <div class="pa-bids-row">${bidsHTML || '<span class="muted">No bids</span>'}</div>
        </div>
      `;
    }

    const adminBtn = state.is_admin ? `
      <button onclick="paAdvance()" class="btn" id="advanceBtn" style="margin-top:16px;">
        ${phase === 'bidding' ? '🔒 Close Bidding' : round < 13 ? '➡️ Next Round' : '📦 Start Post-Auction'}
      </button>
    ` : '';

    area.innerHTML = `
      <div class="pa-game">
        <div class="pa-header">
          <h2>🃏 Round ${round} / 13</h2>
          <div class="pa-round-info">${state.round_schedule ? state.round_schedule[round - 1] : '?'} cards</div>
        </div>

        <div class="pa-auction-section">
          <h3>Cards on Auction</h3>
          <div class="pa-cards-display">${roundCards.length > 0 ? cardsHTML(roundCards) : '<span class="muted">Waiting for round to start...</span>'}</div>
        </div>

        ${actionHTML}

        <div class="pa-my-section">
          <h3>Your Hand <span class="muted small">(${myCards.length} cards • $${myMoney})</span></h3>
          <div class="pa-cards-display">${cardsHTML(myCards, 'small')}</div>
        </div>

        <div class="pa-teams-section">
          <h3>Teams</h3>
          ${teamTable}
        </div>

        ${adminBtn}
      </div>
    `;
  }

  // ---- POST AUCTION ----
  function renderPostAuction(area, state) {
    const myCards = state.my_cards || [];
    const myMoney = state.my_money || 0;
    const teams = state.teams || [];
    const submitted = (state.post_submitted || []).includes(state.my_team_id);

    const cardCheckboxes = myCards.map((label, i) => `
      <label class="pa-card-checkbox">
        <input type="checkbox" name="card_${i}" value="${i}" id="card_chk_${i}">
        ${cardHTML(label, 'small')}
      </label>
    `).join("");

    const submitStatus = teams.map(t => {
      const done = (state.post_submitted || []).includes(t.team_id);
      return `<span class="pa-submit-status ${done ? 'done' : ''}">${t.team_name}: ${done ? '✅' : '⏳'}</span>`;
    }).join("");

    const adminBtn = state.is_admin ? `
      <button onclick="paAdvance()" class="btn" id="advanceBtn" style="margin-top:16px;">
        🔨 Close Post-Auction & Start Bidding
      </button>
    ` : '';

    area.innerHTML = `
      <div class="pa-game">
        <div class="pa-header">
          <h2>📦 Post-Auction Phase</h2>
        </div>

        <p style="color:var(--muted);margin-bottom:16px;">
          Select cards to sell to the host ($20 each) or auction to other teams.
          You must submit before buying from others.
        </p>

        <div class="pa-my-section">
          <h3>Your Cards <span class="muted small">($${myMoney})</span></h3>
          <div class="pa-card-select-grid">${cardCheckboxes}</div>
        </div>

        ${submitted ? '<div class="pa-bid-submitted">✅ Your post-auction orders submitted!</div>' : `
        <div class="pa-post-actions" style="margin-top:16px;">
          <div style="display:flex;gap:12px;flex-wrap:wrap;">
            <button onclick="paPostSubmit('sell')" class="btn pa-btn-sell">💰 Sell Selected to Host ($20 each)</button>
            <button onclick="paPostSubmit('auction')" class="btn pa-btn-auction">🔨 Auction Selected to Teams</button>
            <button onclick="paPostSubmit('none')" class="btn ghost">Skip — Keep All</button>
          </div>
          <p id="postMsg" class="small" style="margin-top:4px;"></p>
        </div>
        `}

        <div class="pa-teams-section" style="margin-top:20px;">
          <h3>Submission Status</h3>
          <div style="display:flex;flex-wrap:wrap;gap:8px;">${submitStatus}</div>
        </div>

        ${adminBtn}
      </div>
    `;
  }

  // ---- POST BIDDING ----
  function renderPostBidding(area, state) {
    const listings = state.card_listings || [];
    const myMoney = state.my_money || 0;
    const myPostBids = state.my_post_bids || {};
    const myCards = state.my_cards || [];

    const listingsHTML = listings.map((l, idx) => {
      const isOwnCard = l.team_id === state.my_team_id;
      const myBid = myPostBids[String(idx)];
      const bidCount = (state.post_bids_counts || {})[String(idx)] || 0;

      let bidArea = '';
      if (isOwnCard) {
        bidArea = `<div class="muted small">Your card</div>`;
      } else if (myBid !== undefined) {
        bidArea = `<div class="pa-bid-submitted" style="padding:6px 10px;margin:4px 0;">✅ $${myBid}</div>`;
      } else {
        bidArea = `
          <div style="display:flex;gap:6px;align-items:center;">
            <span style="font-weight:700;color:var(--brand);">$</span>
            <input type="number" id="postBid_${idx}" min="0" max="${myMoney}" placeholder="0"
              style="width:80px;padding:4px;background:var(--card-bg);border:1px solid var(--border);border-radius:6px;color:var(--text);text-align:center;">
            <button onclick="paPostBid(${idx})" class="btn small">Bid</button>
          </div>
        `;
      }

      return `
        <div class="pa-card-listing ${isOwnCard ? 'pa-own' : ''}">
          <div class="pa-card-listing-info">
            ${cardHTML(l.card)}
            <span class="muted small">${l.team_name}</span>
            ${bidCount > 0 ? `<span class="muted small">${bidCount} bid${bidCount !== 1 ? 's' : ''}</span>` : ''}
          </div>
          <div class="pa-card-listing-bid">${bidArea}</div>
        </div>
      `;
    }).join("");

    const adminBtn = state.is_admin ? `
      <button onclick="paAdvance()" class="btn" id="advanceBtn" style="margin-top:16px;">
        🏆 Close Bidding & Evaluate Hands
      </button>
    ` : '';

    area.innerHTML = `
      <div class="pa-game">
        <div class="pa-header">
          <h2>🔨 Post-Auction Bidding</h2>
        </div>

        <p style="color:var(--muted);margin-bottom:16px;">
          Bid on individual cards from other teams. Winner pays 2nd highest bid. If only 1 bid, it's free!
        </p>

        <div class="pa-my-section">
          <h3>Your Hand <span class="muted small">(${myCards.length} cards • $${myMoney})</span></h3>
          <div class="pa-cards-display">${cardsHTML(myCards, 'small')}</div>
        </div>

        ${listings.length > 0 ? `
          <div class="pa-listings-section">
            <h3>Available Cards (${listings.length})</h3>
            <div class="pa-card-listings-grid">${listingsHTML}</div>
          </div>
        ` : '<p class="muted" style="margin-top:16px;">No teams auctioned cards.</p>'}

        ${adminBtn}
      </div>
    `;
  }

  // ---- FINISHED ----
  function renderFinished(area, state) {
    const teams = (state.teams || []).slice().sort((a, b) => b.money - a.money);
    const hands = state.poker_hands || {};
    const handAwards = state.hand_awards || {};

    const standingsHTML = teams.map((t, i) => {
      const hand = hands[t.team_id] || {};
      const handCards = hand.cards || [];
      const award = hand.award || 0;
      const medals = ['🥇', '🥈', '🥉'];

      return `
        <div class="pa-final-row ${i < 3 ? 'pa-final-top' : ''}">
          <div class="pa-final-rank">${medals[i] || `#${i + 1}`}</div>
          <div class="pa-final-info">
            <div class="pa-final-name">${t.team_name}</div>
            <div class="pa-final-hand">
              <span class="pa-hand-name">${hand.rank_name || '?'}</span>
              <span class="pa-hand-cards">${handCards.map(c => cardHTML(c, 'tiny')).join('')}</span>
            </div>
          </div>
          <div class="pa-final-prize" style="color:${award > 0 ? '#00b894' : '#636e72'};">
            +$${award}
          </div>
          <div class="pa-final-money">
            <strong>$${t.money}</strong>
          </div>
        </div>
      `;
    }).join("");

    // Hand awards reference table
    const awardsTableHTML = Object.entries(handAwards).map(([name, amt]) => `
      <div class="pa-award-row">
        <span class="pa-award-hand">${name}</span>
        <span class="pa-award-amt ${amt > 0 ? '' : 'dim'}">$${amt}</span>
      </div>
    `).join("");

    // Round history
    const historyHTML = (state.round_history || []).map(h => `
      <div class="pa-history-row">
        <span class="pa-history-round">R${h.round}</span>
        <span class="pa-history-cards">${cardsHTML(h.cards, 'tiny')}</span>
        <span class="pa-history-winner">${h.winner_team ? teams.find(t => t.team_id === h.winner_team)?.team_name || h.winner_team : '—'}</span>
        <span class="pa-history-paid">$${h.paid}</span>
      </div>
    `).join("");

    area.innerHTML = `
      <div class="fiveos-finished">
        <h2>🏆 Poker Auction — Final Standings</h2>

        <div class="results-section">
          <h3>Final Rankings (by Total Money)</h3>
          <div class="pa-final-standings">${standingsHTML}</div>
        </div>

        <div class="results-section">
          <h3>🃏 Hand Awards</h3>
          <div class="pa-awards-table">${awardsTableHTML}</div>
        </div>

        <div class="results-section">
          <h3>📜 Auction History</h3>
          <div class="pa-history-list">${historyHTML}</div>
        </div>

        <a href="/poker-auction" class="btn" style="margin-top: 24px; display: inline-block;">← New Game</a>
      </div>
    `;
  }

  // ---- Actions ----
  window.paStartGame = async function () {
    const btn = document.getElementById("startBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Starting..."; }
    try {
      await fetchJSON(`/poker-auction/game/${GAME_ID}/start`, {
        method: "POST", headers: { "Content-Type": "application/json" },
      });
      lastStateHash = "";
      poll();
    } catch (e) {
      console.error("Start error:", e);
      if (btn) { btn.textContent = "Start Game →"; btn.disabled = false; }
    }
  };

  window.paAdvance = async function () {
    const btn = document.getElementById("advanceBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Processing..."; }
    try {
      await fetchJSON(`/poker-auction/game/${GAME_ID}/advance`, {
        method: "POST", headers: { "Content-Type": "application/json" },
      });
      lastStateHash = "";
      poll();
    } catch (e) {
      console.error("Advance error:", e);
      if (btn) { btn.disabled = false; }
    }
  };

  window.paSubmitBid = async function () {
    const input = document.getElementById("bidAmount");
    const msg = document.getElementById("bidMsg");
    const amount = parseInt(input?.value) || 0;
    try {
      await fetchJSON(`/poker-auction/game/${GAME_ID}/bid`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount }),
      });
      lastStateHash = "";
      poll();
    } catch (e) {
      if (msg) { msg.textContent = String(e.message); msg.style.color = "#ff6b6b"; }
    }
  };

  window.paPostSubmit = async function (action) {
    const msg = document.getElementById("postMsg");
    const checkboxes = document.querySelectorAll('input[type="checkbox"]:checked');
    const selectedIndices = Array.from(checkboxes).map(cb => parseInt(cb.value));

    let body = { sell_to_host: [], auction_cards: [] };
    if (action === "sell") {
      body.sell_to_host = selectedIndices;
    } else if (action === "auction") {
      body.auction_cards = selectedIndices;
    }

    try {
      await fetchJSON(`/poker-auction/game/${GAME_ID}/post-auction`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      lastStateHash = "";
      poll();
    } catch (e) {
      if (msg) { msg.textContent = String(e.message); msg.style.color = "#ff6b6b"; }
    }
  };

  window.paPostBid = async function (listingIdx) {
    const input = document.getElementById(`postBid_${listingIdx}`);
    const amount = parseInt(input?.value) || 0;
    try {
      await fetchJSON(`/poker-auction/game/${GAME_ID}/post-bid`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ listing_idx: listingIdx, amount }),
      });
      lastStateHash = "";
      poll();
    } catch (e) {
      alert("Error: " + e.message);
    }
  };

  // ---- Init ----
  getMe();
  startPolling();
})();
