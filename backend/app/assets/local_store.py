import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.assets.object_store import (
    InvalidObjectKeyError,
    ObjectNotFoundError,
    ObjectStoreError,
    validate_object_key,
)


class LocalObjectStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes, *, media_type: str) -> None:
        del media_type
        if not isinstance(data, bytes):
            raise ObjectStoreError("对象内容必须是 bytes。")
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
                temp_path = Path(temp.name)
            os.replace(temp_path, path)
        except OSError as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise ObjectStoreError("本地对象写入失败。") from exc

    def get(self, key: str) -> bytes:
        path = self._safe_path(key)
        try:
            if not path.is_file():
                raise ObjectNotFoundError("对象不存在。")
            return path.read_bytes()
        except ObjectNotFoundError:
            raise
        except OSError as exc:
            raise ObjectStoreError("本地对象读取失败。") from exc

    def delete(self, key: str) -> None:
        path = self._safe_path(key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ObjectStoreError("本地对象删除失败。") from exc

    def check_health(self) -> None:
        if not self._root.is_dir() or not os.access(self._root, os.R_OK | os.W_OK):
            raise ObjectStoreError("本地对象存储不可用。")

    def close(self) -> None:
        return None

    def _safe_path(self, key: str) -> Path:
        validated = validate_object_key(key)
        candidate = (self._root / validated).resolve(strict=False)
        if not candidate.is_relative_to(self._root):
            raise InvalidObjectKeyError("对象存储 key 逃逸根目录。")
        return candidate
