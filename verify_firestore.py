import os
import asyncio
from app import db as db_module
from app.db import init_firestore

# Mock env if needed or rely on existing .env
from dotenv import load_dotenv
load_dotenv()

async def test_firestore():
    print("Initializing Firestore...")
    try:
        init_firestore()
    except Exception as e:
        print(f"Failed to init: {e}")
        return

    db = db_module.db
    if db is None:
        print("DB object is None afer init!")
        return

    print(f"Project: {db.project}")
    
    # Try a simple write
    print("Attempting to write to 'test_connectivity'...")
    try:
        ref = db.collection("test_connectivity").document("ping")
        await ref.set({"timestamp": "now", "status": "ok"})
        print("✅ Write successful.")
    except Exception as e:
        print(f"❌ Write failed: {e}")
        return

    # Try a simple read
    print("Attempting to read from 'test_connectivity'...")
    try:
        doc = await ref.get()
        print(f"✅ Read successful: {doc.to_dict()}")
    except Exception as e:
        print(f"❌ Read failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_firestore())
