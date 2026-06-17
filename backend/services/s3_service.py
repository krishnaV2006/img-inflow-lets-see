"""
S3 storage service for generated images.
All operations fall back gracefully when S3 is not configured,
so the app still works in local-only mode.
"""

import os
import boto3
from botocore.exceptions import ClientError

_AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID")
_AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
_AWS_REGION            = os.getenv("AWS_REGION", "ap-south-1")
_S3_BUCKET             = os.getenv("S3_BUCKET_NAME")


def is_configured() -> bool:
    """Return True only when all required S3 env vars are present."""
    return bool(_AWS_ACCESS_KEY_ID and _AWS_SECRET_ACCESS_KEY and _S3_BUCKET)


def _client():
    return boto3.client(
        "s3",
        aws_access_key_id=_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=_AWS_SECRET_ACCESS_KEY,
        region_name=_AWS_REGION,
    )


def s3_key_for(filename: str) -> str:
    """Canonical S3 key for an image filename (e.g. 'abc123.png')."""
    return f"images/{filename}"


def upload_image(local_path: str, filename: str) -> bool:
    """Upload a local PNG to S3. Returns True on success."""
    if not is_configured():
        return False
    key = s3_key_for(filename)
    try:
        _client().upload_file(
            local_path, _S3_BUCKET, key,
            ExtraArgs={"ContentType": "image/png"},
        )
        print(f"[S3] ✅ Uploaded: {key}")
        return True
    except Exception as e:
        print(f"[S3] ❌ Upload failed for {key}: {e}")
        return False


def get_presigned_url(filename: str, expiration: int = 3600) -> str | None:
    """Return a time-limited presigned GET URL for an image, or None."""
    if not is_configured():
        return None
    key = s3_key_for(filename)
    try:
        url = _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": _S3_BUCKET, "Key": key},
            ExpiresIn=expiration,
        )
        return url
    except Exception as e:
        print(f"[S3] ❌ Presign failed for {key}: {e}")
        return None


def download_image(filename: str, local_path: str) -> bool:
    """Download an image from S3 to a local path. Returns True on success."""
    if not is_configured():
        return False
    key = s3_key_for(filename)
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        _client().download_file(_S3_BUCKET, key, local_path)
        print(f"[S3] ✅ Downloaded: {key} → {local_path}")
        return True
    except Exception as e:
        print(f"[S3] ❌ Download failed for {key}: {e}")
        return False


def delete_image(filename: str) -> bool:
    """Delete an image from S3. Returns True on success."""
    if not is_configured():
        return False
    key = s3_key_for(filename)
    try:
        _client().delete_object(Bucket=_S3_BUCKET, Key=key)
        print(f"[S3] ✅ Deleted: {key}")
        return True
    except Exception as e:
        print(f"[S3] ❌ Delete failed for {key}: {e}")
        return False


# ── Media file (original uploads) helpers ────────────────────────────────────

def media_s3_key(session_id: str, filename: str) -> str:
    return f"media/{session_id}/{filename}"


def upload_media(local_path: str, session_id: str, filename: str) -> str | None:
    """Upload original uploaded media/transcript to S3. Returns S3 key on success."""
    if not is_configured():
        return None
    key = media_s3_key(session_id, filename)
    try:
        _client().upload_file(local_path, _S3_BUCKET, key)
        print(f"[S3] ✅ Uploaded media: {key}")
        return key
    except Exception as e:
        print(f"[S3] ❌ Media upload failed for {key}: {e}")
        return None


def get_presigned_media_url(s3_key: str, expiration: int = 3600) -> str | None:
    """Return a time-limited presigned GET URL for a media file by its full S3 key."""
    if not is_configured():
        return None
    try:
        url = _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": _S3_BUCKET, "Key": s3_key},
            ExpiresIn=expiration,
        )
        return url
    except Exception as e:
        print(f"[S3] ❌ Presign failed for {s3_key}: {e}")
        return None


def download_media(s3_key: str, local_path: str) -> bool:
    """Download original media file from S3 to local path. Returns True on success."""
    if not is_configured():
        return False
    try:
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        _client().download_file(_S3_BUCKET, s3_key, local_path)
        print(f"[S3] ✅ Downloaded media: {s3_key} → {local_path}")
        return True
    except Exception as e:
        print(f"[S3] ❌ Media download failed for {s3_key}: {e}")
        return False
