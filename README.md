# AlphaBook

**AlphaBook is an open-source financial education platform that puts students inside a live order book — trade real stocks, play structured trading games, and build the intuition that separates traders from guessers, with zero real-money risk.**

**Try it now: [alphabook.uk](https://alphabook.uk)** — sign up free and join a session in under a minute.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)

---

## Purpose

Quantitative trading and market making are among the most sought-after careers for STEM students, yet the education around them is broken: textbooks explain market microstructure in the abstract, and nothing lets a student *feel* it — place a limit order, watch it rest in the book, get picked off by a faster participant, and understand why.

AlphaBook closes that gap. We are students at the University of Oxford building the platform we wished existed: a real matching engine with real market data, wrapped in structured games that teach the specific skills trading firms actually test — probability calibration, reacting to news, valuation under uncertainty, and mental arithmetic under pressure.

Everything runs in the browser with virtual balances. Students get the full trading experience — market risk, position management, order flow — without installing anything or risking a penny.

## What's on the platform

**Market Simulation** — the core experience. Trade AAPL, MSFT, NVDA, AMZN, GOOGL, META, and TSLA on a live limit order book anchored to real market prices. Orders match by strict price-time priority — exactly how real exchanges work — and an automated market-maker bot keeps the book alive so there is always someone to trade against.

**Structured trading games**, each modelled on exercises used on real trading floors:

- **5Os** — a trading-floor classic: estimate the five key statistics (min, 25th percentile, median, 75th percentile, max) of a hidden hand of cards; the market resolves against the truth, so perfect calibration wins
- **Headline Trading** — news hits the wire and prices move; take directional positions in a futures market and close out before time runs out
- **Poker Auction** — a multi-round sealed-bid second-price auction where teams bid for cards, assemble hands, and trade in a post-auction market; combines probability, valuation, and game theory
- **Mental Math** — timed arithmetic drills with configurable question types and difficulty, because speed and accuracy are prerequisites on any trading floor

Every mode has a live leaderboard, so sessions run as competitions.

## How it's used

AlphaBook is built for **live, instructor-led sessions**: an admin creates a custom trading game, sets the scenario and news flow, and a room full of students competes on a shared order book while the leaderboard tracks P&L in real time. We run it this way for trading bootcamps and game nights with the Oxford student quant community, including the Oxford Alpha Fund.

It works equally well solo — anyone can sign up at [alphabook.uk](https://alphabook.uk) and practise against the market-maker bot.

## Impact & roadmap

Our goal is to democratize trading education: the exercises above are normally only accessible inside trading-firm internships and interviews. AlphaBook makes them free and open-source for any student, society, or classroom anywhere.

We are seeking grant funding to take AlphaBook from a simulator to an adaptive teaching platform:

- **AI-driven agents** — integrate LLMs to drive adaptive bot behaviour: market makers that adjust their quoting style to the student's skill level instead of following fixed rules
- **Real-time strategy feedback** — an AI coach that reviews a student's fills after each session and explains what a professional would have done differently
- **Pluggable strategy API** — let students write and backtest their own market-making bots against the live book
- **Session replay & analytics** — record full order-flow history so instructors can replay and dissect a session tick by tick

## Team

- **Xue (Snow) Yan** — project lead
- **Siyu (Sylvia) Li** — developer
- **Nick Wang** — developer
- **Fiona (Yiran) Zhang**
- **Xinyun Jiang**
- **Yiluan (Eylan) Zeng**

---

## Technical overview

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

- `main.py` — FastAPI app: page routes, REST endpoints, and the `/ws/{symbol}` WebSocket that broadcasts order book updates to all connected clients on every trade (no polling)
- `order_book.py` — the matching engine: limit orders, price-time priority, partial fills, cancellation
- `market_data.py` — real quotes from [Stooq](https://stooq.com) (15-min delayed) blended with a synthetic tick engine for smooth intra-poll price movement
- `market_maker.py` — the liquidity bot: passive two-sided quoting with realistic depth, gradual level updates, and delayed sweeps of stale orders
- `fiveos.py`, `headline.py`, `poker_auction.py`, `mental_math.py` — the game engines
- `admin.py` — game CRUD, market news publishing, leaderboard, user management
- `auth.py` / `db.py` — Firebase token verification (`__session` cookie) and the Firestore client
- `models.py` / `schemas.py` — Pydantic models and request/response validation

Firestore collections: `users`, `orders`, `trades`, `custom_games`, `market_news`. Users start with a $10,000 virtual balance; prices are stored as strings to preserve decimal precision.

## Run it locally

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

## Contributing

Pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
