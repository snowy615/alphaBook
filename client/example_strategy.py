
import asyncio
import json
import random
import websockets
import httpx

BASE = "http://localhost:8000"
WS   = "ws://localhost:8000"

USER = "alice"
SYMBOL = "AAPL"

async def watch_book():
    async with websockets.connect(f"{WS}/ws/book/{SYMBOL}") as ws:
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "snapshot":
                top_bid = msg["book"]["bids"][0]["px"] if msg["book"]["bids"] else None
                top_ask = msg["book"]["asks"][0]["px"] if msg["book"]["asks"] else None
                print(f"Top of book: bid={top_bid}, ask={top_ask}")

async def place_random_orders():
    async with httpx.AsyncClient() as client:
        for i in range(5):
            ref = (await client.get(f"{BASE}/reference/{SYMBOL}")).json().get("price") or 100.0
            side = random.choice(["BUY", "SELL"])
            px = round(ref * (1 + random.uniform(-0.002, 0.002)), 2)
            qty = random.choice([1, 2, 5])
            r = await client.post(f"{BASE}/orders", json={
                "user_id": USER, "symbol": SYMBOL, "side": side,
                "price": f"{px}", "qty": f"{qty}"
            })
            print("ACK:", r.json())
            await asyncio.sleep(0.5)

async def main():
    await asyncio.gather(watch_book(), place_random_orders())

if __name__ == "__main__":
    asyncio.run(main())
