"""
Cloudflare R2 storage service — S3-compatible object storage.
Used for: statement PDFs, generated reports, exports, uploaded files.
Metadata stays in Postgres. Binary objects go to R2.
"""

import io
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.config import settings

# boto3 is optional — only imported if R2 is configured
_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    if not settings.R2_ACCESS_KEY_ID or not settings.R2_SECRET_ACCESS_KEY:
        return None

    try:
        import boto3
        _s3_client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
        return _s3_client
    except ImportError:
        return None


def r2_available() -> bool:
    return _get_s3() is not None


def generate_r2_key(prefix: str, filename: str, user_id: str = "") -> str:
    """Generate a unique R2 object key like: statements/user123/uuid_filename.pdf"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    uid = uuid.uuid4().hex[:8]
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
    if user_id:
        return f"{prefix}/{user_id}/{ts}_{uid}_{safe_name}"
    return f"{prefix}/{ts}_{uid}_{safe_name}"


async def upload_to_r2(key: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
    s3 = _get_s3()
    if not s3:
        return False
    try:
        s3.put_object(
            Bucket=settings.R2_BUCKET_NAME,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return True
    except Exception as e:
        print(f"R2 upload failed for {key}: {e}")
        return False


async def download_from_r2(key: str) -> Optional[bytes]:
    s3 = _get_s3()
    if not s3:
        return None
    try:
        response = s3.get_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
        return response["Body"].read()
    except Exception as e:
        print(f"R2 download failed for {key}: {e}")
        return None


async def generate_presigned_upload_url(key: str, content_type: str = "application/pdf", expires: int = 3600) -> Optional[str]:
    s3 = _get_s3()
    if not s3:
        return None
    try:
        url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.R2_BUCKET_NAME,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires,
        )
        return url
    except Exception as e:
        print(f"R2 presigned upload URL failed: {e}")
        return None


async def generate_presigned_download_url(key: str, expires: int = 3600) -> Optional[str]:
    s3 = _get_s3()
    if not s3:
        return None
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.R2_BUCKET_NAME,
                "Key": key,
            },
            ExpiresIn=expires,
        )
        return url
    except Exception as e:
        print(f"R2 presigned download URL failed: {e}")
        return None


async def delete_from_r2(key: str) -> bool:
    s3 = _get_s3()
    if not s3:
        return False
    try:
        s3.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
        return True
    except Exception as e:
        print(f"R2 delete failed for {key}: {e}")
        return False
