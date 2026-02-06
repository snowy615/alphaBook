import os
import logging
from google.cloud import firestore
import firebase_admin
from firebase_admin import credentials, initialize_app, storage

# Global Firestore client
db: firestore.AsyncClient = None
bucket = None

def init_firestore():
    """base connection to Firestore"""
    global db, bucket
    
    # Initialize Firebase Admin
    # It attempts to use GOOGLE_APPLICATION_CREDENTIALS automatically
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    
    # We need to capture the credentials object to pass to AsyncClient explicitly
    # if we are not relying on GOOGLE_APPLICATION_CREDENTIALS env var.
    client_creds = None

    try:
        if not len(firebase_admin._apps):
            if cred_json:
                import json
                cred_dict = json.loads(cred_json)
                cred = credentials.Certificate(cred_dict)
                initialize_app(cred, {
                    'storageBucket': os.getenv("FIREBASE_STORAGE_BUCKET")
                })
                client_creds = cred.get_credential()
            elif cred_path and os.path.exists(cred_path):
                cred = credentials.Certificate(cred_path)
                initialize_app(cred, {
                    'storageBucket': os.getenv("FIREBASE_STORAGE_BUCKET")
                })
                client_creds = cred.get_credential()
            else:
                # Use default credentials (good for Cloud Run / GKE)
                initialize_app(options={
                    'storageBucket': os.getenv("FIREBASE_STORAGE_BUCKET")
                })
        else:
            # If already initialized (e.g. reload), try to get creds from app? 
            # Simplified: assuming we rely on what we just did or env vars.
            pass
            
        logging.info("Firebase Admin initialized successfully.")
        
        # Initialize Async Firestore Client
        # This will also use GOOGLE_APPLICATION_CREDENTIALS
        project_id = os.getenv("FIREBASE_PROJECT_ID")
        
        if client_creds:
             db = firestore.AsyncClient(project=project_id, credentials=client_creds)
        elif project_id:
             db = firestore.AsyncClient(project=project_id)
        else:
             db = firestore.AsyncClient()
             
        logging.info(f"Firestore AsyncClient initialized. Project: {db.project}")

        # Initialize Storage Bucket
        bucket = storage.bucket()
        logging.info(f"Storage bucket initialized: {bucket.name}")

    except Exception as e:
        logging.error(f"Failed to initialize Firebase/Firestore: {e}")
        # Re-raise to stop startup if critical
        raise

async def close_firestore():
    global db
    if db:
        db.close()