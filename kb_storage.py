# kb_storage.py
"""
S3-compatible object storage helper for KB files.

Supports:
- AWS S3
- Cloudflare R2 (S3-compatible)
- Any S3-compatible endpoint via KB_S3_ENDPOINT_URL

This module is intentionally sync/blocking. Control Plane endpoints that call it should be
regular `def` routes (FastAPI runs them in a threadpool), or explicitly offload to a thread.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import BinaryIO, Optional, Tuple
from uuid import uuid4

import boto3
from botocore.client import Config


class KBStorageError(Exception):
    pass


@dataclass(frozen=True)
class S3Config:
    bucket: str
    region: str
    endpoint_url: Optional[str]
    access_key_id: str
    secret_access_key: str
    prefix: str
    max_file_bytes: int


def load_s3_config() -> S3Config:
    bucket = (os.getenv("KB_S3_BUCKET") or "").strip()
    if not bucket:
        raise KBStorageError("KB_S3_BUCKET is not configured")

    access_key_id = (os.getenv("KB_S3_ACCESS_KEY_ID") or "").strip()
    secret_access_key = (os.getenv("KB_S3_SECRET_ACCESS_KEY") or "").strip()
    if not access_key_id or not secret_access_key:
        raise KBStorageError("KB_S3_ACCESS_KEY_ID / KB_S3_SECRET_ACCESS_KEY are not configured")

    region = (os.getenv("KB_S3_REGION") or "").strip() or "us-east-1"
    endpoint_url = (os.getenv("KB_S3_ENDPOINT_URL") or "").strip() or None
    prefix = (os.getenv("KB_S3_PREFIX") or "").strip() or "kb"
    max_file_bytes = int((os.getenv("KB_MAX_FILE_BYTES") or "").strip() or (25 * 1024 * 1024))

    return S3Config(
        bucket=bucket,
        region=region,
        endpoint_url=endpoint_url,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        prefix=prefix.strip("/"),
        max_file_bytes=max_file_bytes,
    )


def _sanitize_filename(name: str) -> str:
    # Keep it predictable for S3 keys + download headers.
    name = name.strip().replace("\\", "/")
    name = name.split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] or "file"


def build_storage_key(cfg: S3Config, *, tenant_id: str, file_id: str, filename: str) -> str:
    safe = _sanitize_filename(filename)
    return f"{cfg.prefix}/{tenant_id}/{file_id}/{safe}"


class HashingReader:
    """
    File-like wrapper that:
    - updates a sha256 hash as data is read
    - counts bytes read
    - optionally enforces a max bytes limit
    """

    def __init__(self, raw: BinaryIO, *, max_bytes: int | None = None):
        self._raw = raw
        self._h = hashlib.sha256()
        self.bytes_read = 0
        self._max = max_bytes

    def read(self, n: int = -1) -> bytes:
        chunk = self._raw.read(n)
        if not chunk:
            return chunk
        self.bytes_read += len(chunk)
        if self._max is not None and self.bytes_read > self._max:
            raise KBStorageError(f"File exceeds max size ({self._max} bytes)")
        self._h.update(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def get_s3_client(cfg: S3Config):
    # Signature v4 works for AWS and R2. For R2, region is ignored but required by some clients.
    session = boto3.session.Session(
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name=cfg.region,
    )
    return session.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        config=Config(signature_version="s3v4"),
    )


def upload_fileobj(
    *,
    tenant_id: str,
    filename: str,
    content_type: Optional[str],
    fileobj: BinaryIO,
) -> Tuple[str, str, int, str, str]:
    """
    Uploads a file-like object to S3 and returns:
      (file_id, bucket, size_bytes, sha256_hex, storage_key)
    """
    cfg = load_s3_config()
    s3 = get_s3_client(cfg)

    file_id = str(uuid4())
    key = build_storage_key(cfg, tenant_id=tenant_id, file_id=file_id, filename=filename)

    reader = HashingReader(fileobj, max_bytes=cfg.max_file_bytes)

    extra = {}
    if content_type:
        extra["ContentType"] = content_type

    # boto3 reads from reader until EOF.
    s3.upload_fileobj(reader, cfg.bucket, key, ExtraArgs=extra or None)

    return file_id, cfg.bucket, reader.bytes_read, reader.hexdigest(), key


def stream_download(*, bucket: str, key: str):
    """
    Returns a tuple: (content_type, content_length, streaming_body)
    Where streaming_body is a file-like object that can be iterated.
    """
    cfg = load_s3_config()
    s3 = get_s3_client(cfg)

    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"]  # botocore.response.StreamingBody
    content_type = obj.get("ContentType") or "application/octet-stream"
    content_length = obj.get("ContentLength")
    return content_type, content_length, body


def delete_object(*, bucket: str, key: str) -> None:
    cfg = load_s3_config()
    s3 = get_s3_client(cfg)
    s3.delete_object(Bucket=bucket, Key=key)
