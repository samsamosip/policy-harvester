from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import Settings, get_settings


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    storage_key: str
    byte_size: int


class ObjectStore(Protocol):
    def put(self, payload: bytes) -> StoredObject: ...
    def get(self, storage_key: str) -> bytes: ...
    def exists(self, storage_key: str) -> bool: ...


def content_key(digest: str) -> str:
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"


class FilesystemObjectStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, storage_key: str) -> Path:
        target = (self.root / storage_key).resolve()
        if self.root not in target.parents:
            raise ValueError("invalid object storage key")
        return target

    def put(self, payload: bytes) -> StoredObject:
        digest = hashlib.sha256(payload).hexdigest()
        key = content_key(digest)
        destination = self._path(key)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".incoming-")
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return StoredObject(digest, key, len(payload))

    def get(self, storage_key: str) -> bytes:
        return self._path(storage_key).read_bytes()

    def exists(self, storage_key: str) -> bool:
        return self._path(storage_key).is_file()


class S3ObjectStore:
    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=(settings.s3_access_key.get_secret_value()
                               if settings.s3_access_key else None),
            aws_secret_access_key=(settings.s3_secret_key.get_secret_value()
                                   if settings.s3_secret_key else None),
            config=Config(s3={"addressing_style": "path"}),
        )
        if settings.s3_auto_create_bucket:
            self._ensure_bucket(settings.s3_region)

    def _ensure_bucket(self, region: str) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status not in {400, 404} and code not in {"NoSuchBucket", "404"}:
                raise
        arguments = {"Bucket": self.bucket}
        if region != "us-east-1":
            arguments["CreateBucketConfiguration"] = {"LocationConstraint": region}
        self.client.create_bucket(**arguments)

    def put(self, payload: bytes) -> StoredObject:
        digest = hashlib.sha256(payload).hexdigest()
        key = content_key(digest)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                Metadata={"sha256": digest},
            )
        return StoredObject(digest, key, len(payload))

    def get(self, storage_key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=storage_key)["Body"].read()

    def exists(self, storage_key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=storage_key)
            return True
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise


def build_object_store(settings: Settings | None = None) -> ObjectStore:
    config = settings or get_settings()
    if config.object_store_backend == "s3":
        return S3ObjectStore(config)
    return FilesystemObjectStore(config.object_store_root)
