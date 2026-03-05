(function () {
    "use strict";

    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
    const GAME_ID = window.GAME_ID;
    const POLL_MS = 1500;

    let gameState = null;
    let currentQ = 0;
    let timerInterval = null;
    let timerRemaining = 0;
    let totalQuestions = 0;
    let timePerQ = 15;
    let myAnswers = {}; // {index: {submitted, correct}}
    let gameFinished = false;

    const fetchJSON = async (url, init) => {
        const r = await fetch(url, { credentials: "include", ...init });
        if (!r.ok) {
            const data = await r.json().catch(() => ({}));
            throw new Error(data.detail || String(r.status));
        }
        return r.json();
    };

    // ---- Auth ----
    async function initAuth() {
        try {
            const me = await fetchJSON("/me");
            const nameEl = $("#userName");
            if (nameEl) nameEl.textContent = me.username || "user";
            return me;
        } catch {
            window.location.href = "/login";
        }
    }

    // ---- Polling ----
    async function pollState() {
        try {
            gameState = await fetchJSON(`/mental-math/game/${GAME_ID}/state`);
            render();
        } catch (e) {
            console.error("Poll error:", e);
        }
    }

    let pollTimer = null;
    function startPolling() {
        pollState();
        pollTimer = setInterval(pollState, POLL_MS);
    }
    function stopPolling() {
        if (pollTimer) clearInterval(pollTimer);
    }

    // ---- Render ----
    function render() {
        const area = $("#gameArea");
        if (!gameState) { area.innerHTML = `<div class="mm-loading">Loading...</div>`; return; }

        switch (gameState.status) {
            case "lobby": renderLobby(area); break;
            case "playing": renderPlaying(area); break;
            case "finished": renderFinished(area); break;
            default: area.innerHTML = `<div class="mm-loading">Unknown state</div>`;
        }
    }

    // ---- Lobby ----
    function renderLobby(area) {
        const players = gameState.players || [];
        const settings = gameState.settings || {};
        const isAdmin = gameState.is_admin;

        area.innerHTML = `
      <div class="mm-lobby">
        <div class="mm-lobby-header">
          <h2>🧮 Mental Math</h2>
          <div class="mm-join-code-display">
            <span class="mm-code-label">JOIN CODE</span>
            <span class="mm-code-value">${gameState.join_code}</span>
          </div>
        </div>

        <div class="mm-settings-display">
          <div class="mm-setting-pill">📋 ${settings.num_questions || 10} questions</div>
          <div class="mm-setting-pill">⏱️ ${settings.time_per_question || 15}s each</div>
          <div class="mm-setting-pill">📊 ${(settings.difficulty || "medium").charAt(0).toUpperCase() + (settings.difficulty || "medium").slice(1)}</div>
          <div class="mm-setting-pill">🧩 ${(settings.question_types || []).join(", ")}</div>
        </div>

        <div class="mm-players-list">
          <h3>Players (${players.length})</h3>
          ${players.map(p => `
            <div class="mm-player-item">
              <span class="mm-player-avatar">${p.username.charAt(0).toUpperCase()}</span>
              <span class="mm-player-name">${p.username}</span>
            </div>
          `).join("")}
          ${players.length === 0 ? `<p class="mm-waiting">Waiting for players to join...</p>` : ""}
        </div>

        ${isAdmin ? `
          <button onclick="window._mmStartGame()" class="btn primary mm-start-btn" id="startBtn"
            ${players.length === 0 ? "disabled" : ""}>
            ▶️ Start Game
          </button>
        ` : `
          <div class="mm-waiting-msg">
            <div class="mm-pulse"></div>
            Waiting for admin to start the game...
          </div>
        `}
      </div>
    `;
    }

    window._mmStartGame = async function () {
        const btn = $("#startBtn");
        if (btn) { btn.disabled = true; btn.textContent = "Starting..."; }
        try {
            await fetchJSON(`/mental-math/game/${GAME_ID}/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
            });
            stopPolling();
            await pollState();
            // Stop polling during play — we manage state locally
            stopPolling();
            startGame();
        } catch (e) {
            if (btn) { btn.textContent = e.message || "Error"; btn.disabled = false; }
        }
    };

    // ---- Playing ----
    function startGame() {
        if (!gameState || gameState.status !== "playing") return;
        totalQuestions = (gameState.questions || []).length;
        timePerQ = (gameState.settings || {}).time_per_question || 15;
        currentQ = 0;
        myAnswers = {};
        gameFinished = false;

        // Restore any previously answered questions
        if (gameState.my_results && gameState.my_results.answers) {
            for (const a of gameState.my_results.answers) {
                myAnswers[a.index] = { submitted: a.submitted, correct: a.correct };
            }
            // Find the first unanswered question
            for (let i = 0; i < totalQuestions; i++) {
                if (!(i in myAnswers)) { currentQ = i; break; }
                if (i === totalQuestions - 1) { currentQ = totalQuestions; }
            }
        }

        if (currentQ >= totalQuestions) {
            finishGame();
            return;
        }

        renderQuestion();
    }

    function renderPlaying(area) {
        // Called from poll — during play we mostly render locally
        if (gameState.status === "playing" && !gameFinished && totalQuestions === 0) {
            // First render after status change
            stopPolling();
            startGame();
            return;
        }
        if (gameFinished) {
            // Player finished but game may not be over yet
            area.innerHTML = `
        <div class="mm-waiting-finish">
          <h2>✅ Done!</h2>
          <p class="mm-score-big">${getMyScore()} / ${totalQuestions}</p>
          <div class="mm-waiting-msg">
            <div class="mm-pulse"></div>
            Waiting for other players to finish...
          </div>
        </div>
      `;
        }
    }

    function getMyScore() {
        let score = 0;
        for (const k in myAnswers) {
            if (myAnswers[k].correct) score++;
        }
        return score;
    }

    function renderQuestion() {
        if (currentQ >= totalQuestions) {
            finishGame();
            return;
        }

        const area = $("#gameArea");
        const q = gameState.questions[currentQ];
        const score = getMyScore();

        // Format question text (handle multiline for comparison/pattern)
        const lines = q.text.split("\n");
        let questionHTML;
        if (lines.length > 1) {
            questionHTML = lines.map((line, i) =>
                i === 0
                    ? `<div class="mm-q-label">${line}</div>`
                    : `<div class="mm-q-option">${line}</div>`
            ).join("");
        } else {
            questionHTML = `<div class="mm-q-text">${q.text}</div>`;
        }

        const isComparison = q.type === "comparison";

        area.innerHTML = `
      <div class="mm-play-container">
        <div class="mm-play-header">
          <div class="mm-progress-info">
            <span class="mm-q-counter">Question ${currentQ + 1} of ${totalQuestions}</span>
            <span class="mm-score-display">Score: ${score}</span>
          </div>
          <div class="mm-progress-bar-bg">
            <div class="mm-progress-bar-fill" style="width: ${((currentQ) / totalQuestions) * 100}%"></div>
          </div>
        </div>

        <div class="mm-question-card">
          <div class="mm-type-indicator">${getTypeIcon(q.type)} ${q.type}</div>
          <div class="mm-question-content">
            ${questionHTML}
            <span class="mm-equals">=</span>
            <span class="mm-q-mark">?</span>
          </div>

          <div class="mm-timer-bar-bg">
            <div class="mm-timer-bar-fill" id="timerBar"></div>
          </div>
          <div class="mm-timer-text" id="timerText">${timePerQ}s</div>

          <div class="mm-answer-area">
            ${isComparison ? `
              <div class="mm-comparison-btns">
                <button onclick="window._mmSubmitAnswer('A')" class="btn mm-choice-btn">A</button>
                <button onclick="window._mmSubmitAnswer('B')" class="btn mm-choice-btn">B</button>
              </div>
            ` : `
              <input type="text" id="answerInput" class="mm-answer-input"
                placeholder="Your answer" autocomplete="off" inputmode="numeric"
                onkeydown="if(event.key==='Enter') window._mmSubmitAnswer()">
              <button onclick="window._mmSubmitAnswer()" class="btn primary mm-submit-btn">Submit</button>
            `}
          </div>
        </div>
      </div>
    `;

        // Focus input
        const input = $("#answerInput");
        if (input) setTimeout(() => input.focus(), 50);

        // Start timer
        startTimer();
    }

    function getTypeIcon(type) {
        const icons = {
            addition: "➕", subtraction: "➖", multiplication: "✖️",
            division: "➗", exponent: "📐", comparison: "⚖️",
            pattern: "🔢", percentage: "💯"
        };
        return icons[type] || "🧮";
    }

    // ---- Timer ----
    function startTimer() {
        clearTimer();
        timerRemaining = timePerQ;
        const bar = $("#timerBar");
        const text = $("#timerText");

        if (bar) {
            bar.style.transition = "none";
            bar.style.width = "100%";
            bar.offsetHeight; // force reflow
            bar.style.transition = `width ${timePerQ}s linear`;
            bar.style.width = "0%";
        }

        timerInterval = setInterval(() => {
            timerRemaining--;
            if (text) text.textContent = `${Math.max(0, timerRemaining)}s`;

            if (timerRemaining <= 5 && bar) {
                bar.classList.add("mm-timer-urgent");
            }

            if (timerRemaining <= 0) {
                clearTimer();
                // Time's up — skip
                showFeedback(false, "⏰ Time's up!", () => {
                    currentQ++;
                    renderQuestion();
                });
            }
        }, 1000);
    }

    function clearTimer() {
        if (timerInterval) {
            clearInterval(timerInterval);
            timerInterval = null;
        }
    }

    // ---- Submit answer ----
    window._mmSubmitAnswer = async function (forcedAnswer) {
        const input = $("#answerInput");
        const answer = forcedAnswer || (input ? input.value.trim() : "");
        if (!answer) return;

        clearTimer();

        // Disable inputs
        if (input) input.disabled = true;
        $$(".mm-submit-btn, .mm-choice-btn").forEach(b => b.disabled = true);

        try {
            const result = await fetchJSON(`/mental-math/game/${GAME_ID}/answer`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ question_index: currentQ, answer }),
            });

            myAnswers[currentQ] = { submitted: answer, correct: result.correct };

            const correctAnswer = gameState.questions[currentQ]?.answer || "?";
            showFeedback(result.correct,
                result.correct ? "✅ Correct!" : `❌ Wrong! Answer: ${correctAnswer}`,
                () => {
                    currentQ++;
                    renderQuestion();
                }
            );
        } catch (e) {
            console.error("Submit error:", e);
            currentQ++;
            renderQuestion();
        }
    };

    function showFeedback(correct, message, callback) {
        const area = $(".mm-question-card");
        if (!area) { callback(); return; }

        const overlay = document.createElement("div");
        overlay.className = `mm-feedback ${correct ? "mm-correct" : "mm-wrong"}`;
        overlay.innerHTML = `<span>${message}</span>`;
        area.appendChild(overlay);

        setTimeout(() => {
            overlay.remove();
            callback();
        }, correct ? 800 : 1500);
    }

    // ---- Finish ----
    async function finishGame() {
        gameFinished = true;
        clearTimer();

        const area = $("#gameArea");
        area.innerHTML = `
      <div class="mm-waiting-finish">
        <h2>✅ All Done!</h2>
        <p class="mm-score-big">${getMyScore()} / ${totalQuestions}</p>
        <div class="mm-waiting-msg">
          <div class="mm-pulse"></div>
          Submitting results...
        </div>
      </div>
    `;

        try {
            await fetchJSON(`/mental-math/game/${GAME_ID}/finish`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
            });
        } catch (e) {
            console.error("Finish error:", e);
        }

        // Resume polling to get final results
        startPolling();
    }

    // ---- Finished ----
    function renderFinished(area) {
        stopPolling();
        clearTimer();

        const scoreboard = gameState.scoreboard || [];
        const questions = gameState.questions || [];
        const myResults = gameState.my_results || { score: 0, answers: [] };

        // Build answer lookup
        const myAnswerMap = {};
        for (const a of (myResults.answers || [])) {
            myAnswerMap[a.index] = a;
        }

        area.innerHTML = `
      <div class="mm-finished">
        <div class="mm-results-header">
          <h2>🏆 Results</h2>
        </div>

        <div class="mm-scoreboard-card">
          <h3>Scoreboard</h3>
          <div class="mm-scoreboard">
            ${scoreboard.map((p, i) => `
              <div class="mm-score-row ${i === 0 ? 'mm-winner' : ''}">
                <span class="mm-rank">${i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : (i + 1)}</span>
                <span class="mm-sb-name">${p.username}</span>
                <span class="mm-sb-score">${p.score} / ${p.total}</span>
                <span class="mm-sb-pct">${Math.round(p.score / p.total * 100)}%</span>
              </div>
            `).join("")}
          </div>
        </div>

        <div class="mm-review-card">
          <h3>Your Answers</h3>
          <div class="mm-review-list">
            ${questions.map((q, i) => {
            const a = myAnswerMap[i];
            const answered = !!a;
            const correct = a?.correct || false;
            return `
                <div class="mm-review-item ${correct ? 'mm-r-correct' : 'mm-r-wrong'}">
                  <div class="mm-review-num">${i + 1}</div>
                  <div class="mm-review-body">
                    <div class="mm-review-question">${q.text.replace(/\n/g, ' | ')}</div>
                    <div class="mm-review-answers">
                      <span class="mm-review-correct-label">Answer: <strong>${q.answer}</strong></span>
                      ${answered
                    ? `<span class="mm-review-yours">You: <strong>${a.submitted}</strong> ${correct ? '✅' : '❌'}</span>`
                    : `<span class="mm-review-yours">Skipped ⏭️</span>`
                }
                    </div>
                  </div>
                </div>
              `;
        }).join("")}
          </div>
        </div>

        <a href="/mental-math" class="btn primary" style="margin-top: 24px; display: inline-block;">
          ← Play Again
        </a>
      </div>
    `;
    }

    // ---- Init ----
    async function init() {
        await initAuth();
        startPolling();
    }

    init();
})();
