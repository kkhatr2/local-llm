"""Atomic object-storage to volume sync for GGUF weights.

This module is an edge adapter. It never imports the lifecycle lock and is
never called from a chat handler. Credentials are injected from the environment
by the CLI and are never placed in URLs, arguments, or log messages.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

from local_llm.config import ModelRecord
from local_llm.downloader import DownloaderError
from local_llm.paths import safe_model_path

LOGGER = logging.getLogger(__name__)


class ObjectStorageError(DownloaderError):
    """Raised when object-storage sync cannot produce complete weights."""


@dataclass(frozen=True)
class ObjectCredentials:
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signed_headers(
    *,
    url: str,
    key: str,
    region: str,
    credentials: ObjectCredentials,
    now: datetime,
) -> dict[str, str]:
    """Create AWS SigV4 GET headers without embedding secrets in the URL."""

    if not credentials.access_key_id or not credentials.secret_access_key:
        return {}

    parts = urlsplit(url)
    host = parts.netloc
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    canonical_uri = quote(parts.path or "/", safe="/-_.~")
    canonical_headers = (
        f"host:{host}\n"
        "x-amz-content-sha256:UNSIGNED-PAYLOAD\n"
        f"x-amz-date:{amzdate}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    scope = f"{datestamp}/{region}/s3/aws4_request"
    canonical_request = "\n".join(
        [
            "GET",
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            "UNSIGNED-PAYLOAD",
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amzdate,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = _hmac_sha256(
        _hmac_sha256(
            _hmac_sha256(
                ("AWS4" + credentials.secret_access_key).encode("utf-8"), datestamp
            ),
            region,
        ),
        "s3/aws4_request",
    )
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers = {
        "x-amz-date": amzdate,
        "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if credentials.session_token:
        headers["x-amz-security-token"] = credentials.session_token
    return headers


class ObjectStorageSync:
    """Fetch a GGUF object and atomically publish it into the model volume."""

    def __init__(
        self,
        http_client: Any,
        credentials: ObjectCredentials,
        bucket: str,
        prefix: str,
        region: str = "us-east-1",
        endpoint: str = "",
        *,
        model_dir: str = "models",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("registry.object_storage.bucket must be non-empty")
        self.http_client = http_client
        self.credentials = credentials
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self.endpoint = endpoint.strip()
        self.model_dir = model_dir
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _object_key(self, record: ModelRecord) -> str:
        return f"{self.prefix}/{record.filename}" if self.prefix else record.filename

    def _url(self, key: str) -> str:
        if self.endpoint:
            base = self.endpoint.rstrip("/")
            if not base.startswith(("http://", "https://")):
                base = f"https://{base}"
            return f"{base}/{quote(self.bucket, safe='')}/{quote(key, safe='/')}"
        return (
            f"https://{self.bucket}.s3.{self.region}.amazonaws.com/"
            f"{quote(key, safe='/')}"
        )

    def _destination(self, record: ModelRecord, root: Path | str) -> Path:
        return safe_model_path(root, self.model_dir, record.alias, record.filename)

    def exists(self, record: ModelRecord, root: Path | str) -> bool:
        destination = self._destination(record, root)
        return destination.is_file() and destination.stat().st_size > 0

    def _chunks(self, response: Any) -> Iterable[bytes]:
        iterator = getattr(response, "iter_bytes", None)
        if callable(iterator):
            return iterator()
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return (bytes(content),)
        raise ObjectStorageError("object storage response had no body")

    def sync(self, record: ModelRecord, root: Path | str) -> Path:
        """Fetch and atomically publish ``record``; leave no partial weights."""

        destination = self._destination(record, root)
        if self.exists(record, root):
            LOGGER.info("model already synced: alias=%s", record.alias)
            return destination

        key = self._object_key(record)
        url = self._url(key)
        headers = _signed_headers(
            url=url,
            key=key,
            region=self.region,
            credentials=self.credentials,
            now=self._clock(),
        )
        LOGGER.info(
            "syncing model: alias=%s repo_id=%s filename=%s",
            record.alias,
            record.repo_id,
            record.filename,
        )

        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd: int | None = None
        temp_name: str | None = None
        try:
            response = self.http_client.get(url, headers=headers)
            status = getattr(response, "status_code", 200)
            if status >= 400:
                raise ObjectStorageError(f"object storage GET failed: {status}")
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()

            fd, temp_name = tempfile.mkstemp(
                dir=destination.parent, prefix=".sync-", suffix=".part"
            )
            with os.fdopen(fd, "wb") as handle:
                fd = None
                for chunk in self._chunks(response):
                    if chunk:
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, destination)
            temp_name = None
            LOGGER.info("model sync complete: alias=%s", record.alias)
            return destination
        except Exception as exc:
            LOGGER.error(
                "model sync failed: alias=%s error=%s",
                record.alias,
                type(exc).__name__,
            )
            if isinstance(exc, ObjectStorageError):
                raise
            raise ObjectStorageError("object storage sync failed") from None
        finally:
            if fd is not None:
                os.close(fd)
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass


__all__ = ["ObjectCredentials", "ObjectStorageError", "ObjectStorageSync"]
