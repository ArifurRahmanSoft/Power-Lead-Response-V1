import asyncio
from dataclasses import dataclass
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings


class ObjectStorageError(Exception):
    pass


class ObjectStorageNotConfiguredError(ObjectStorageError):
    pass


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> StoredObject: ...

    async def delete(self, key: str) -> None: ...


class S3ObjectStorage:
    def _configuration(self) -> tuple[str, object]:
        bucket = (settings.object_storage_bucket or "").strip()
        if not bucket:
            raise ObjectStorageNotConfiguredError
        access = settings.object_storage_access_key_id
        secret = settings.object_storage_secret_access_key
        if (access is None) != (secret is None):
            raise ObjectStorageNotConfiguredError

        options: dict[str, object] = {
            "service_name": "s3",
            "region_name": settings.object_storage_region,
            "endpoint_url": settings.object_storage_endpoint_url,
            "use_ssl": settings.object_storage_use_ssl,
            "config": Config(
                connect_timeout=5,
                read_timeout=15,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        }
        if access is not None and secret is not None:
            options["aws_access_key_id"] = access.get_secret_value()
            options["aws_secret_access_key"] = secret.get_secret_value()
        return bucket, boto3.client(**options)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        bucket, client = self._configuration()
        try:
            await asyncio.to_thread(
                client.put_object,
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                ServerSideEncryption="AES256",
            )
        except (BotoCoreError, ClientError, OSError) as exception:
            raise ObjectStorageError from exception

    async def get(self, key: str) -> StoredObject:
        bucket, client = self._configuration()
        try:
            response = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
            data = await asyncio.to_thread(response["Body"].read)
            return StoredObject(
                data=data,
                content_type=str(response.get("ContentType") or "application/octet-stream"),
            )
        except (BotoCoreError, ClientError, OSError, KeyError) as exception:
            raise ObjectStorageError from exception

    async def delete(self, key: str) -> None:
        bucket, client = self._configuration()
        try:
            await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError, OSError) as exception:
            raise ObjectStorageError from exception


object_storage = S3ObjectStorage()


def get_object_storage() -> ObjectStorage:
    return object_storage
