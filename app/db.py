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
            logging.debug("init_firestore: no apps, initializing")
            if cred_json:
                logging.debug("init_firestore: using FIREBASE_CREDENTIALS_JSON")
                import json
                cred_dict = json.loads(cred_json)
                # Fix escaped newlines in private_key (common when passed via env vars)
                if "private_key" in cred_dict and "\\n" in cred_dict["private_key"]:
                    cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
                cred = credentials.Certificate(cred_dict)
                initialize_app(cred, {
                    'storageBucket': os.getenv("FIREBASE_STORAGE_BUCKET")
                })
                client_creds = cred.get_credential()
            elif cred_path and os.path.exists(cred_path):
                logging.debug("init_firestore: using cred_path %s", cred_path)
                cred = credentials.Certificate(cred_path)
                initialize_app(cred, {
                    'storageBucket': os.getenv("FIREBASE_STORAGE_BUCKET")
                })
                client_creds = cred.get_credential()
            else:
                logging.debug("init_firestore: using ADC / default creds")
                # Use default credentials (good for Cloud Run / GKE)
                initialize_app(options={
                    'storageBucket': os.getenv("FIREBASE_STORAGE_BUCKET")
                })
        else:
            logging.debug("init_firestore: app already initialized")
            
        logging.info("Firebase Admin initialized successfully.")
        
        # Initialize Async Firestore Client
        # This will also use GOOGLE_APPLICATION_CREDENTIALS
        logging.debug("init_firestore: initializing AsyncClient")
        project_id = os.getenv("FIREBASE_PROJECT_ID")
        
        if client_creds:
             logging.debug("init_firestore: using explicit client_creds")
             db = firestore.AsyncClient(project=project_id, credentials=client_creds)
        elif project_id:
             logging.debug("init_firestore: using project_id %s", project_id)
             db = firestore.AsyncClient(project=project_id)
        else:
             logging.debug("init_firestore: using default AsyncClient constructor")
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