import requests
import sys

BASE_URL = "http://127.0.0.1:8000"

def test_health():
    print(f"Testing GET {BASE_URL}/health ...")
    try:
        resp = requests.get(f"{BASE_URL}/health", timeout=5)
        print(f"Status: {resp.status_code}")
        print(f"Body: {resp.text}")
    except Exception as e:
        print(f"❌ Failed: {e}")

def test_unauth_order():
    print(f"\nTesting POST {BASE_URL}/orders (unauthenticated) ...")
    try:
        resp = requests.post(f"{BASE_URL}/orders", json={
            "symbol": "AAPL",
            "side": "BUY",
            "price": "150.00",
            "qty": "1"
        }, timeout=5)
        print(f"Status: {resp.status_code}")
        print(f"Body: {resp.text}")
    except Exception as e:
        print(f"❌ Failed: {e}")

if __name__ == "__main__":
    test_health()
    test_unauth_order()
