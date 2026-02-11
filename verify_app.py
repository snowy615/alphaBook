import requests
import sys

BASE_URL = "http://127.0.0.1:8000"

def test_endpoint(path, expected_status=200, description=""):
    url = f"{BASE_URL}{path}"
    try:
        response = requests.get(url, timeout=5)
        print(f"Testing {description} ({path})... ", end="")
        if response.status_code == expected_status:
            print(f"✅ OK ({response.status_code})")
            return True
        else:
            print(f"❌ FAILED. Expected {expected_status}, got {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return False

def main():
    print(f"Verifying application at {BASE_URL}...\n")
    
    results = [
        test_endpoint("/", 200, "Home Page"),
        test_endpoint("/health", 200, "Health Check"),
        test_endpoint("/static/style.css", 200, "Static CSS"),
        test_endpoint("/reference/AAPL", 200, "Market Data API"),
        test_endpoint("/symbols", 200, "Symbols List")
    ]
    
    if all(results):
        print("\n✅ All checks passed! Application is running correctly.")
        sys.exit(0)
    else:
        print("\n❌ One or more checks failed.")
        sys.exit(1)

if __name__ == "__main__":
    main()
