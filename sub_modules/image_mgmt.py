'''
sub_modules/image_mgmt.py
=========================
Image upload and retrieval via S3 (or local filesystem in dev).

All images are re-encoded via Pillow before storage — this strips
any embedded metadata (EXIF, GPS, etc.) and prevents image-based
injection attacks. Raw bytes from the upload are never stored.

Dev mode: if AWS_S3_BUCKET is not set, files are saved to
/tmp/fussballcamp_dev_uploads/ and served via a local route.
Swap to S3 by setting the env vars — no code changes needed.
'''

import io
import os
import uuid
from PIL import Image

from flask import current_app


# Supported input formats. Pillow re-encodes everything as JPEG or PNG
# on the way out regardless of input type.
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_DIMENSION       = 2048   # px — images resized down if larger
JPEG_QUALITY        = 85
THUMBNAIL_SIZE      = (400, 400)
DEV_UPLOAD_DIR      = '/tmp/fussballcamp_dev_uploads'


def _is_dev_mode() -> bool:
    return not current_app.config.get('AWS_S3_BUCKET')


def upload_image(file_storage, folder: str = 'announcements') -> str | None:
    '''
    Accept a FileStorage object (from request.files), validate, re-encode,
    and upload to S3 (or local dev storage).

    Returns the public URL string on success, or None on failure.

    folder: S3 key prefix, e.g. 'announcements' or 'avatars'
    '''
    if not file_storage or not file_storage.filename:
        return None

    ext = file_storage.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        current_app.logger.warning(
            f'[ImageMgmt] Rejected file with extension: {ext}'
        )
        return None

    try:
        # Read raw bytes and open with Pillow — validates it is a real image
        raw = file_storage.read()
        img = Image.open(io.BytesIO(raw))

        # Convert to RGB (handles RGBA PNGs, palette images, etc.)
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')

        # Resize down if too large (preserves aspect ratio)
        if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

        # Re-encode to JPEG — strips all metadata
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=JPEG_QUALITY, optimize=True)
        buffer.seek(0)

        filename = f'{folder}/{uuid.uuid4().hex}.jpg'

        if _is_dev_mode():
            return _save_local(buffer.read(), filename)
        else:
            return _upload_s3(buffer, filename)

    except Exception as e:
        current_app.logger.error(f'[ImageMgmt] Upload failed: {e}')
        return None


def delete_image(url: str) -> bool:
    '''
    Delete an image by its URL.
    Extracts the S3 key (or local path) from the URL and deletes it.
    Returns True on success.
    '''
    if not url:
        return False
    try:
        if _is_dev_mode():
            return _delete_local(url)
        else:
            return _delete_s3(url)
    except Exception as e:
        current_app.logger.error(f'[ImageMgmt] Delete failed: {e}')
        return False


# =============================================================================
# S3 BACKEND
# =============================================================================

def _upload_s3(buffer: io.BytesIO, key: str) -> str | None:
    import boto3
    bucket  = current_app.config['AWS_S3_BUCKET']
    region  = current_app.config.get('AWS_REGION', 'eu-central-1')

    s3 = boto3.client(
        's3',
        region_name=region,
        aws_access_key_id=current_app.config.get('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=current_app.config.get('AWS_SECRET_ACCESS_KEY'),
    )

    s3.upload_fileobj(
        buffer,
        bucket,
        key,
        ExtraArgs={
            'ContentType': 'image/jpeg',
            'CacheControl': 'max-age=31536000',
        }
    )

    return f'https://{bucket}.s3.{region}.amazonaws.com/{key}'


def _delete_s3(url: str) -> bool:
    import boto3
    bucket  = current_app.config['AWS_S3_BUCKET']
    region  = current_app.config.get('AWS_REGION', 'eu-central-1')

    # Extract key from URL
    prefix = f'https://{bucket}.s3.{region}.amazonaws.com/'
    if not url.startswith(prefix):
        return False
    key = url[len(prefix):]

    s3 = boto3.client(
        's3',
        region_name=region,
        aws_access_key_id=current_app.config.get('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=current_app.config.get('AWS_SECRET_ACCESS_KEY'),
    )
    s3.delete_object(Bucket=bucket, Key=key)
    return True


# =============================================================================
# LOCAL DEV BACKEND
# =============================================================================

def _save_local(data: bytes, filename: str) -> str:
    os.makedirs(DEV_UPLOAD_DIR, exist_ok=True)
    path = os.path.join(DEV_UPLOAD_DIR, filename.replace('/', '_'))
    with open(path, 'wb') as f:
        f.write(data)
    # Return a local URL that the dev_uploads route in public_bp will serve
    return f'/dev-uploads/{filename.replace("/", "_")}'


def _delete_local(url: str) -> bool:
    filename = url.replace('/dev-uploads/', '')
    path = os.path.join(DEV_UPLOAD_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
    return True
