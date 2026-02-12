# AlphaBook

A real-time mini stock exchange simulator built with **FastAPI**, **Firebase**, and **WebSockets**.

Trade equities (AAPL, AMZN, GOOGL, META, MSFT, NVDA) with a simulated order book, live price updates, and portfolio tracking.

## Tech Stack

| Layer       | Technology                                    |
|-------------|-----------------------------------------------|
| Backend     | Python / FastAPI                              |
| Database    | Cloud Firestore                               |
| Auth        | Firebase Authentication (Email/Password)      |
| Hosting     | Google Cloud Run (backend) + Firebase Hosting  |
| Real-time   | WebSockets                                    |

## Quick Start (Local Development)

```bash
# 1. Clone and setup
git clone <repo-url> && cd alphaBook
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your Firebase credentials (see SETUP.md)

# 3. Run
python -m uvicorn app.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000)

## Deployment

AlphaBook deploys to **Google Cloud Run** (backend) + **Firebase Hosting** (frontend proxy).

### Deploy Backend
```bash
gcloud run deploy alphabook-api \
  --source . \
  --region us-central1 \
  --allow-unauthenticated
```

### Deploy Frontend
```bash
firebase deploy --only hosting
```

### Live URL
[https://alphabook-5ef4e.web.app](https://alphabook-5ef4e.web.app)

## Architecture

```
Browser  →  Firebase Hosting (alphabook-5ef4e.web.app)
                │  rewrites all requests
                ▼
            Cloud Run (alphabook-api)
                │  runs Docker container
                ▼
            FastAPI App (Python)
                │  reads/writes data
                ▼
            Cloud Firestore + Firebase Auth
```

> **Note:** Firebase Hosting only forwards cookies named `__session`. The app uses this cookie name for session management.

## Setup

See [SETUP.md](SETUP.md) for detailed Firebase configuration and environment setup.
