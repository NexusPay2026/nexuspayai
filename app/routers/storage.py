"""
Storage router — presigned URL generation for R2 uploads/downloads.
Frontend uploads files directly to R2 via presigned URLs.
Metadata stays in Postgres.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import UploadURLResponse, DownloadURLResponse
from app.services.auth_service import get_current_user
from app.services.r2_storage import (
    r2_available, generate_r2_key,
    generate_presigned_upload_url, generate_presigned_download_url,
)

router = APIRouter()


@router.get("/storage/status")
async def storage_status():
    return {"r2_available": r2_available()}


@router.post("/storage/upload-url", response_model=UploadURLResponse)
async def get_upload_url(
    filename: str = Query(..., min_length=1),
    content_type: str = Query("application/pdf"),
    prefix: str = Query("statements"),
    user: dict = Depends(get_current_user),
):
    if not r2_available():
        raise HTTPException(status_code=503, detail="R2 storage not configured — set R2 env vars in Render")

    r2_key = generate_r2_key(prefix, filename, user["sub"])
    url = await generate_presigned_upload_url(r2_key, content_type)

    if not url:
        raise HTTPException(status_code=500, detail="Failed to generate upload URL")

    return UploadURLResponse(upload_url=url, r2_key=r2_key)


@router.post("/storage/download-url", response_model=DownloadURLResponse)
async def get_download_url(
    r2_key: str = Query(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    if not r2_available():
        raise HTTPException(status_code=503, detail="R2 storage not configured")

    url = await generate_presigned_download_url(r2_key)
    if not url:
        raise HTTPException(status_code=500, detail="Failed to generate download URL")

    return DownloadURLResponse(download_url=url)
