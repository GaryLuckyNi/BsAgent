import re
from pathlib import PurePosixPath
from typing import Protocol


class ObjectStoreError(RuntimeError):
    code = "object_store_error"


class ObjectNotFoundError(ObjectStoreError):
    code = "object_not_found"


class InvalidObjectKeyError(ObjectStoreError):
    code = "invalid_object_key"


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, media_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def check_health(self) -> None: ...

    def close(self) -> None: ...


OBJECT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$")


def validate_object_key(key: str) -> str:
    if (
        not isinstance(key, str)
        or not OBJECT_KEY_PATTERN.fullmatch(key)
        or "\\" in key
        or "%" in key
        or "//" in key
    ):
        raise InvalidObjectKeyError("对象存储 key 不合法。")
    path = PurePosixPath(key)
    if path.is_absolute() or str(path) != key or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise InvalidObjectKeyError("对象存储 key 不合法。")
    return key
