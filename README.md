
# Mini Exchange (Python)

Single-process FastAPI app that:
- pulls reference prices from yfinance,
- maintains a tiny in-memory order book per symbol,
- lets users submit limit orders,
- pushes snapshots over WebSocket.

## Quick start

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt

# start API
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs

In another terminal:
```bash
python client/example_strategy.py
```

Test with curl:
```bash
curl http://localhost:8000/health
curl http://localhost:8000/reference/AAPL
curl http://localhost:8000/book/AAPL
curl -X POST http://localhost:8000/orders -H "content-type: application/json"           -d '{"user_id":"u1","symbol":"AAPL","side":"BUY","price":"100.00","qty":"1"}'
```
# alphaBook
