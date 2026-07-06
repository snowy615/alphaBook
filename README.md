# AlphaBook

**AlphaBook is an open-source, extensible market-making simulator designed to democratize high-frequency trading education.**

It is a real-time mini stock exchange: students trade real equities (AAPL, AMZN, GOOGL, META, MSFT, NVDA) against each other and against an automated market-maker bot, on a live limit order book with price-time priority matching, real delayed market data, and instant WebSocket updates — the same mechanics that drive real electronic markets, in a sandbox with simulated money.

**Live demo:** [https://alphabook-5ef4e.web.app](https://alphabook-5ef4e.web.app)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)

---

## Why AlphaBook?

I am a student at the University of Oxford building AlphaBook to bridge the gap between academic theory and practical, algorithmic implementation. Textbooks explain market microstructure; almost nothing lets a student *feel* it — place a limit order, watch it rest in the book, get picked off by a faster participant, and understand why.

AlphaBook is already used to run live trading bootcamps and game sessions: an admin creates a custom trading game (e.g. "price this asset given these news headlines"), students compete on a shared order book, and a leaderboard tracks P&L in real time.

## Features

- **Limit order book engine** — in-memory price-time priority matching with partial fills and cancellation (`app/order_book.py`)
- **Real market data** — live quotes from [Stooq](https://stooq.com) (15-min delayed) blended with a synthetic tick engine for smooth intra-poll movement (`app/market_data.py`)
- **Market-maker bot** — passive two-sided quoting plus periodic sweeps of stale orders, so the book is always live even with few participants (`app/market_maker.py`)
- **Real-time updates** — every trade broadcasts the new book snapshot to all connected clients over WebSockets; no polling
- **Portfolio & P&L tracking** — each user starts with a $10,000 simulated balance
- **Admin dashboard** — create custom trading games, publish market news, manage users, view the leaderboard
- **Educational mini-games** — Mental Math (timed arithmetic under pressure), Headline (price an asset from news), Poker Auction, and FiveOs, each with its own rules page and scoreboard
- **Auth & persistence** — Firebase Authentication and Cloud Firestore, deployable to Google Cloud Run in one command

## Quickstart

Prerequisites: Python 3.11+, a free [Firebase](https://console.firebase.google.com/) project (Auth + Firestore).

```bash
# 1. Clone and install
git clone https://github.com/snowy615/alphaBook.git
cd alphaBook
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Fill in your Firebase web config and place your service-account.json
# in the project root — see SETUP.md for a click-by-click guide

# 3. Run
python -m uvicorn app.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000), sign up, and start trading.

Full setup instructions (Firebase project creation, service accounts, deployment to Cloud Run + Firebase Hosting) are in **[SETUP.md](SETUP.md)**.

## Architecture

| Layer            | Technology                                    |
|------------------|-----------------------------------------------|
| Backend          | Python 3.11 / FastAPI / Uvicorn               |
| Frontend         | Jinja2 templates + vanilla JavaScript         |
| Database         | Google Cloud Firestore (async client)         |
| Auth             | Firebase Authentication                       |
| Real-time        | WebSockets                                    |
| Market data      | Stooq quotes + synthetic tick engine          |
| Hosting          | Google Cloud Run + Firebase Hosting           |
| Containerization | Docker                                        |

Key backend modules (`app/`):

- `main.py` — FastAPI app: page routes, REST endpoints, and the `/ws/{symbol}` WebSocket that broadcasts order book updates
- `order_book.py` — the matching engine (limit orders, price-time priority, partial fills)
- `market_data.py` / `market_maker.py` — real price feed and the automated liquidity provider
- `admin.py` — game CRUD, news, leaderboard, user management
- `auth.py` / `db.py` — Firebase token verification (`__session` cookie) and Firestore client
- `mental_math.py`, `headline.py`, `poker_auction.py`, `fiveos.py` — the educational mini-games
- `models.py` / `schemas.py` — Pydantic models and request/response validation

Firestore collections: `users`, `orders`, `trades`, `custom_games`, `market_news`. Prices are stored as strings to preserve decimal precision.

## Roadmap: Future Integration

We are applying for grant funding to take AlphaBook from a simulator to an adaptive teaching platform:

- **AI-driven agents** — integrate OpenAI models to drive adaptive agent behavior: bots that adjust their quoting style to the student's skill level instead of following fixed rules
- **Real-time strategy feedback** — an LLM coach that reviews a student's fills after each session and explains what a professional market maker would have done differently
- **Pluggable strategy API** — let students write and backtest their own market-making bots against the live book
- **Session replay & analytics** — record full order-flow history so instructors can replay and dissect a session tick by tick

## Contributing

Pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
