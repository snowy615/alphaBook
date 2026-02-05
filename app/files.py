from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from app.auth import current_user
from app.models import User
from app.db import bucket
import uuid
import datetime as dt

router = APIRouter()

@router.post("/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    user: User = Depends(current_user)
):
    """Upload a file to Firebase Cloud Storage"""
    if not bucket:
        raise HTTPException(status_code=500, detail="Storage bucket not configured")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Generate unique filename
    ext = file.filename.split(".")[-1] if "." in file.filename else "bin"
    timestamp = int(dt.datetime.utcnow().timestamp())
    blob_name = f"uploads/{user.id}/{timestamp}_{uuid.uuid4().hex[:8]}.{ext}"
    
    blob = bucket.blob(blob_name)
    
    # Upload from file-like object
    # Note: async read from FastAPI UploadFile, then synchronous upload to Firebase Storage (which uses requests)
    # Ideally should use run_in_executor for blocking upload in async func
    content = await file.read()
    
    # Set public? Or generic access?
    # For now, let's just upload.
    blob.upload_from_string(content, content_type=file.content_type)
    
    # Make public (optional, depending on security)
    # blob.make_public()
    # url = blob.public_url

    # Or generic signed URL
    url = blob.generate_signed_url(expiration=dt.timedelta(days=7))

    return {
        "ok": True,
        "filename": blob_name,
        "url": url
    }
