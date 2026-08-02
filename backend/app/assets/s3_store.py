from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError

from app.assets.object_store import (
    ObjectNotFoundError,
    ObjectStoreError,
    validate_object_key,
)


class S3ObjectStore:
    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        server_side_encryption: Literal["AES256", "aws:kms"],
        kms_key_id: str | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("S3 bucket 不能为空。")
        if server_side_encryption == "aws:kms" and not kms_key_id:
            raise ValueError("aws:kms 加密必须配置 kms_key_id。")
        self._client = client
        self._bucket = bucket
        self._server_side_encryption = server_side_encryption
        self._kms_key_id = kms_key_id

    def put(self, key: str, data: bytes, *, media_type: str) -> None:
        validated = validate_object_key(key)
        arguments = {
            "Bucket": self._bucket,
            "Key": validated,
            "Body": data,
            "ContentType": media_type,
            "ServerSideEncryption": self._server_side_encryption,
        }
        if self._server_side_encryption == "aws:kms":
            arguments["SSEKMSKeyId"] = self._kms_key_id
        try:
            self._client.put_object(**arguments)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("S3 对象写入失败。") from exc

    def get(self, key: str) -> bytes:
        validated = validate_object_key(key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=validated)
            data = response["Body"].read()
        except ClientError as exc:
            if _client_error_code(exc) in {"NoSuchKey", "404", "NotFound"}:
                raise ObjectNotFoundError("对象不存在。") from exc
            raise ObjectStoreError("S3 对象读取失败。") from exc
        except (BotoCoreError, KeyError, AttributeError, TypeError) as exc:
            raise ObjectStoreError("S3 对象读取失败。") from exc
        if not isinstance(data, bytes):
            raise ObjectStoreError("S3 对象返回了非法内容。")
        return data

    def delete(self, key: str) -> None:
        validated = validate_object_key(key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=validated)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("S3 对象删除失败。") from exc

    def check_health(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("S3 对象存储不可用。") from exc

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def _client_error_code(exc: ClientError) -> str:
    try:
        return str(exc.response["Error"]["Code"])
    except (KeyError, TypeError):
        return ""
