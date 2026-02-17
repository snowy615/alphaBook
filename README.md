# AlphaBook

A real-time mini stock exchange simulator built with **FastAPI**, **Firebase**, and **WebSockets**.

Trade equities (AAPL, AMZN, GOOGL, META, MSFT, NVDA) with a simulated order book, live price updates, and portfolio tracking.

## Tech Stack Overview

| Layer       | Technology                                    |
|-------------|-----------------------------------------------|
| Backend     | Python 3.11 / FastAPI                         |
| Frontend    | Jinja2 templates + vanilla JavaScript         |
| Database    | Google Cloud Firestore (NoSQL)                |
| Auth        | Firebase Authentication (Email/Password)      |
| Real-time   | WebSockets (`websockets` library)             |
| Market Data | Alpha Vantage API + synthetic tick engine     |
| Hosting     | Google Cloud Run (backend) + Firebase Hosting |
| Containerization | Docker                                   |

---

### Backend

The backend is a **Python FastAPI** application served by **Uvicorn**. It's organized into several modules under the `app/` directory:

- **`main.py`** — The core FastAPI app. Sets up routes for pages (landing, trading), REST endpoints for orders/portfolio, and a WebSocket endpoint (`/ws/{symbol}`) that broadcasts live order book updates to connected clients.
- **`auth.py`** — Handles authentication using **Firebase Admin SDK** to verify ID tokens. Sessions are managed via a `__session` cookie (required by Firebase Hosting). Includes login, signup, and logout routes.
- **`order_book.py`** — An **in-memory limit order book** engine. Supports price-time priority matching for BUY/SELL limit orders, partial fills, and order cancellation.
- **`market_data.py`** — Fetches real stock prices from the **Alpha Vantage API** and runs a synthetic tick engine that simulates small price movements between API calls (every ~1.5 seconds).
- **`me.py`** — User-specific endpoints: portfolio summary, P&L calculation, open orders, and order cancellation.
- **`admin.py`** — Admin dashboard and management: user leaderboard, custom game CRUD, news management, user blacklisting, and full reset functionality.
- **`models.py`** — **Pydantic** data models: `User`, `Order`, `Trade`, `CustomGame`, `MarketNews`.
- **`db.py`** — Initializes the Firebase Admin SDK and the Firestore `AsyncClient` connection.
- **`state.py`** — Shared in-memory state (order books dict, helper functions).
- **`schemas.py`** — Request/response schemas for API validation.

Key libraries: `fastapi`, `uvicorn`, `pydantic`, `firebase-admin`, `google-cloud-firestore`, `yfinance`, `python-jose` (JWT), `httpx`, `websockets`.

---

### Frontend

The frontend is **server-side rendered** using **Jinja2 templates** with client-side interactivity via **vanilla JavaScript** (no framework). Static assets live in `app/static/` and templates in `app/templates/`.

**Templates** (`app/templates/`):
- `index.html` — Landing page / lobby showing available trading games
- `trading.html` — The main trading interface for a specific symbol
- `login.html` / `signup.html` — Auth pages using the Firebase JS SDK for client-side authentication
- `admin.html` — Admin dashboard with leaderboard, user management, and game controls

**JavaScript** (`app/static/`):
- `app.js` — Core app logic: authentication state, API calls, shared utilities
- `trading.js` — Trading page: order form, order book visualization, open orders, P&L chart, WebSocket connection for real-time book updates
- `dashboard.js` — Portfolio dashboard rendering
- `landing.js` — Landing page interactivity

**Styling**: A single `style.css` handles all styling.

**Real-time Updates**: The trading page opens a WebSocket connection to `/ws/{symbol}`. The server broadcasts the order book snapshot to all subscribers whenever a trade executes, so the UI updates instantly without polling.

---

### Database

The database is **Google Cloud Firestore**, a serverless NoSQL document database. The app uses the **async Python client** (`firestore.AsyncClient`) for non-blocking database operations.

**Collections:**

| Collection      | Purpose                              | Key Fields                                                   |
|-----------------|--------------------------------------|--------------------------------------------------------------|
| `users`         | User accounts                        | `username`, `balance`, `firebase_uid`, `is_admin`, `is_blacklisted` |
| `orders`        | Order history                        | `order_id`, `user_id`, `symbol`, `side`, `price`, `qty`, `filled_qty`, `status` |
| `trades`        | Executed trade records               | `symbol`, `buyer_id`, `seller_id`, `price`, `qty`, `buy_order_id`, `sell_order_id` |
| `custom_games`  | Admin-created trading games          | `symbol`, `name`, `instructions`, `expected_value`, `is_active`, `is_visible`, `is_paused` |
| `market_news`   | News items shown on trading page     | `content`, `created_at`                                       |

Users start with a **$10,000 simulated balance**. All prices are stored as strings to preserve decimal precision.

---

## Run Locally

```bash
source venv/bin/activate
python -m uvicorn app.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000)

---

## Deploy

```bash
# Deploy backend to Cloud Run
gcloud run deploy alphabook-api \
  --source . \
  --region us-central1 \
  --allow-unauthenticated

# Deploy frontend to Firebase Hosting
firebase deploy --only hosting
```

### Live URL

[https://alphabook-5ef4e.web.app](https://alphabook-5ef4e.web.app)

