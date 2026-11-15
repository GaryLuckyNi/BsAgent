import importlib.util
from importlib import import_module

import pytest
from botocore.exceptions import ClientError

from app.assets.local_store import LocalObjectStore
from app.assets.object_store import InvalidObjectKeyError, ObjectNotFoundError, ObjectStoreError
from app.assets.s3_store import S3ObjectStore


def test_object_store_modules_are_available():
    assert importlib.util.find_spec("app.assets.object_store") is not None
    assert importlib.util.find_spec("app.assets.local_store") is not None
    assert importlib.util.find_spec("app.assets.s3_store") is not None


def test_object_store_modules_expose_adapters_and_errors():
    contract = import_module("app.assets.object_store")
    local = import_module("app.assets.local_store")
    s3 = import_module("app.assets.s3_store")

    assert getattr(contract, "ObjectStore", None) is not None
    assert getattr(contract, "InvalidObjectKeyError", None) is not None
    assert getattr(local, "LocalObjectStore", None) is not None
    assert getattr(s3, "S3ObjectStore", None) is not None


def test_local_object_store_round_trips_and_deletes_idempotently(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")

    store.put("tenants/t1/assets/a1.png", b"image-data", media_type="image/png")

    assert store.get("tenants/t1/assets/a1.png") == b"image-data"
    store.delete("tenants/t1/assets/a1.png")
    store.delete("tenants/t1/assets/a1.png")
    with pytest.raises(ObjectNotFoundError):
        store.get("tenants/t1/assets/a1.png")


@pytest.mark.parametrize(
    "key",
    ["", ".", "../escape", "/absolute", "a/../../escape", "a\\escape", "a//b", "a/%2e%2e/b"],
)
def test_local_object_store_rejects_unsafe_keys(tmp_path, key):
    store = LocalObjectStore(tmp_path / "objects")

    with pytest.raises(InvalidObjectKeyError):
        store.put(key, b"value", media_type="image/png")


def test_local_object_store_rejects_symlink_escape(tmp_path):
    root = tmp_path / "objects"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    store = LocalObjectStore(root)

    with pytest.raises(InvalidObjectKeyError):
        store.put("link/escape.png", b"value", media_type="image/png")


class FakeS3Client:
    def __init__(self):
        self.put_calls = []
        self.get_calls = []
        self.delete_calls = []
        self.head_bucket_calls = []
        self.closed = False
        self.objects = {}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        if kwargs["Key"] not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )

        class Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": Body(self.objects[kwargs["Key"]])}

    def delete_object(self, **kwargs):
        self.delete_calls.append(kwargs)
        self.objects.pop(kwargs["Key"], None)

    def head_bucket(self, **kwargs):
        self.head_bucket_calls.append(kwargs)

    def close(self):
        self.closed = True


def test_s3_object_store_uses_private_encrypted_operations_without_acl():
    client = FakeS3Client()
    store = S3ObjectStore(
        client=client,
        bucket="private-assets",
        server_side_encryption="AES256",
    )

    store.put("tenants/t1/assets/a1.png", b"image-data", media_type="image/png")

    assert store.get("tenants/t1/assets/a1.png") == b"image-data"
    assert client.put_calls == [
        {
            "Bucket": "private-assets",
            "Key": "tenants/t1/assets/a1.png",
            "Body": b"image-data",
            "ContentType": "image/png",
            "ServerSideEncryption": "AES256",
        }
    ]
    assert "ACL" not in client.put_calls[0]
    store.delete("tenants/t1/assets/a1.png")
    store.delete("tenants/t1/assets/a1.png")


def test_s3_object_store_maps_not_found_and_provider_errors():
    client = FakeS3Client()
    store = S3ObjectStore(
        client=client,
        bucket="private-assets",
        server_side_encryption="AES256",
    )

    with pytest.raises(ObjectNotFoundError):
        store.get("tenants/t1/assets/missing.png")

    def fail_put(**_):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "secret diagnostics"}},
            "PutObject",
        )

    client.put_object = fail_put
    with pytest.raises(ObjectStoreError) as exc_info:
        store.put("tenants/t1/assets/a1.png", b"data", media_type="image/png")
    assert "secret diagnostics" not in str(exc_info.value)


def test_s3_object_store_adds_kms_key_only_for_kms_encryption():
    client = FakeS3Client()
    store = S3ObjectStore(
        client=client,
        bucket="private-assets",
        server_side_encryption="aws:kms",
        kms_key_id="alias/agent-assets",
    )

    store.put("tenants/t1/assets/a1.png", b"data", media_type="image/png")

    assert client.put_calls[0]["SSEKMSKeyId"] == "alias/agent-assets"


def test_object_store_health_checks_and_s3_client_shutdown(tmp_path):
    local = LocalObjectStore(tmp_path / "objects")
    client = FakeS3Client()
    s3 = S3ObjectStore(
        client=client,
        bucket="private-assets",
        server_side_encryption="AES256",
    )

    local.check_health()
    s3.check_health()
    s3.close()

    assert client.head_bucket_calls == [{"Bucket": "private-assets"}]
    assert client.closed is True
