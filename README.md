# AlphaBook

**A live exchange in your browser: real order book, real market data, structured trading games. Open-source, built at Oxford, free for any student, society, or classroom.**

**[alphabook.uk](https://alphabook.uk)** — sign up and trade in under a minute.

[![CI](https://github.com/snowy615/alphaBook/actions/workflows/ci.yml/badge.svg)](https://github.com/snowy615/alphaBook/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)

## Why

The skills trading firms actually test — market making, probability calibration, pricing under pressure — are taught almost nowhere outside the firms themselves. AlphaBook makes them trainable: a real price-time-priority matching engine, live market data, and games modelled on trading-floor exercises, all with virtual balances and zero real-money risk.

We use it to run live trading bootcamps and competitions for the Oxford student quant community, including the Oxford Alpha Fund: an instructor creates a game, scripts the news flow, and a room of students competes on a shared order book with a real-time P&L leaderboard.

## The platform

- **Market Simulation** — trade AAPL, MSFT, NVDA, AMZN, GOOGL, META, and TSLA on a live limit order book anchored to real prices; an automated market-maker bot keeps the book liquid
- **5Os** — estimate the five statistics (min, quartiles, median, max) of a hidden hand of cards; best calibration wins
- **Headline Trading** — news hits the wire, prices move; trade a futures market on the story before time runs out
- **Poker Auction** — sealed-bid second-price auctions for cards, then trade hands in a post-auction market
- **Mental Math** — timed arithmetic drills with configurable types and difficulty

Every mode has a live leaderboard. Works solo too — the market-maker bot is always on the other side.

## Roadmap

We are seeking grant funding to turn the simulator into an adaptive teaching platform:

- **LLM-driven agents** — market-maker bots that adapt their quoting to the student's skill level
- **AI strategy coach** — post-session review of a student's fills: what a professional would have done differently
- **Strategy API** — students write and backtest their own bots against the live book
- **Session replay** — full order-flow recording so instructors can dissect a session tick by tick

## Team

**Xue (Snow) Yan** (project lead) · **Siyu (Sylvia) Li** · **Nick Wang** · **Fiona (Yiran) Zhang** · **Xinyun Jiang** · **Yiluan (Eylan) Zeng**

---

## Architecture

| Layer | Technology |
|---|---|
| Backend | Python 3.11 / FastAPI / Uvicorn |
| Frontend | Jinja2 + vanilla JavaScript |
| Data & auth | Cloud Firestore (async) + Firebase Auth |
| Real-time | WebSockets (book snapshot broadcast on every trade) |
| Market data | Stooq quotes + synthetic tick engine |
| Deploy | Docker → Cloud Run + Firebase Hosting |

Key modules in `app/`: `order_book.py` (matching engine: price-time priority, partial fills), `market_maker.py` (two-sided quoting with realistic depth and delayed sweeps), `market_data.py` (real quotes blended with synthetic ticks), `fiveos.py` / `headline.py` / `poker_auction.py` / `mental_math.py` (game engines), `admin.py` (games, news, leaderboard, users). Tests live in `tests/`; CI runs ruff + pytest on every push.

## Run it locally

Prerequisites: Python 3.11+, a free [Firebase](https://console.firebase.google.com/) project (Auth + Firestore).

```bash
git clone https://github.com/snowy615/alphaBook.git
cd alphaBook
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in Firebase config — see SETUP.md
python -m uvicorn app.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000). Full Firebase setup and Cloud Run deployment: **[SETUP.md](SETUP.md)**.

## Contributing & license

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed ([LICENSE](LICENSE)).
