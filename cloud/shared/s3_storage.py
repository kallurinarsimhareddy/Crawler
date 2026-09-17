"""Object storage on any S3-compatible service: Supabase Storage (S3 protocol) or Cloudflare R2.

Uses S3 access keys, never the Supabase service-role key. For Supabase, create
them under Project Settings → Storage → S3 access keys. Supabase does not
scope these keys to one bucket: they reach storage, but not the database or
Auth admin.

The bucket must be **private**. :meth:`S3Storage.check_private` proves it by
fetching the stamp object anonymously and expecting a refusal. Downloads always
stream through the API, which checks ownership first; no presigned or public URL
is ever handed to a browser.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, BinaryIO, Optional

from cloud.shared.storage import ObjectStorage, StoredObject, validate_key

__all__ = ["S3Storage"]


class S3Storage(ObjectStorage):
    name = "s3"

    def __init__(self, *, bucket: str, namespace: str, client: Any, endpoint: Optional[str] = None) -> None:
        if not bucket or not namespace:
            raise ValueError("bucket and namespace are required")
        self.bucket = bucket
        self.namespace = namespace.strip("/")
        self.endpoint = endpoint
        self._client = client

    @classmethod
    def from_settings(
        cls,
        *,
        endpoint: str,
        region: str,
        bucket: str,
        namespace: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> "S3Storage":
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=60,
                signature_version="s3v4",
            ),
        )
        return cls(bucket=bucket, namespace=namespace, client=client, endpoint=endpoint)

    def _object_key(self, key: str) -> str:
        # The namespace (e.g. "staging") is a top-level prefix, so one bucket can
        # never mix environments even if two were ever pointed at it.
        return f"{self.namespace}/{validate_key(key)}"

    def put_file(self, key: str, source: Path, *, content_type: str) -> StoredObject:
        digest = hashlib.sha256()
        size = 0
        with Path(source).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        with Path(source).open("rb") as handle:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._object_key(key),
                Body=handle,
                ContentType=content_type,
                ContentLength=size,
            )
        return StoredObject(key=key, size_bytes=size, sha256=digest.hexdigest())

    def open(self, key: str) -> BinaryIO:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._object_key(key))
        except self._client.exceptions.NoSuchKey as missing:
            raise FileNotFoundError(key) from missing
        except Exception as error:  # botocore ClientError with 404
            if _is_not_found(error):
                raise FileNotFoundError(key) from error
            raise
        return response["Body"]

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._object_key(key))
            return True
        except Exception as error:
            if _is_not_found(error):
                return False
            raise

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._object_key(key))

    def ping(self) -> None:
        self._client.head_bucket(Bucket=self.bucket)

    def check_private(self, key: str) -> bool:
        """True if an unsigned request for ``key`` is refused."""
        import requests

        if not self.endpoint:
            raise ValueError("endpoint unknown; cannot test anonymous access")
        url = f"{self.endpoint.rstrip('/')}/{self.bucket}/{self._object_key(key)}"
        response = requests.get(url, timeout=15, allow_redirects=False)
        return response.status_code in (400, 401, 403, 404)


def _is_not_found(error: Exception) -> bool:
    response = getattr(error, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code", ""))
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404
