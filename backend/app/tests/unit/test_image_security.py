import importlib.util
from importlib import import_module
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from app.assets.errors import AssetValidationError
from app.assets.image_security import sanitize_image


def test_image_security_modules_are_available():
    assert importlib.util.find_spec("app.assets.errors") is not None
    assert importlib.util.find_spec("app.assets.image_security") is not None


def test_image_security_exposes_sanitizer_contract():
    errors = import_module("app.assets.errors")
    security = import_module("app.assets.image_security")

    assert getattr(errors, "AssetValidationError", None) is not None
    assert callable(getattr(security, "sanitize_image", None))
    assert getattr(security, "SanitizedImage", None) is not None


def _image_bytes(
    image_format: str,
    *,
    size=(32, 16),
    exif=None,
) -> bytes:
    output = BytesIO()
    image = Image.new("RGB", size, color=(18, 92, 140))
    save_kwargs = {"exif": exif} if exif is not None else {}
    image.save(output, format=image_format, **save_kwargs)
    return output.getvalue()


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    [
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
        ("WEBP", "image/webp"),
    ],
)
def test_sanitizer_accepts_and_reencodes_supported_raster_images(
    image_format,
    media_type,
):
    sanitized = sanitize_image(
        BytesIO(_image_bytes(image_format)),
        claimed_media_type=media_type,
        max_image_bytes=1024 * 1024,
        max_image_pixels=1_000_000,
    )

    assert sanitized.media_type == media_type
    assert (sanitized.width, sanitized.height) == (32, 16)
    assert sanitized.sha256 == sha256(sanitized.data).hexdigest()
    with Image.open(BytesIO(sanitized.data)) as decoded:
        decoded.load()
        assert getattr(decoded, "n_frames", 1) == 1


def test_sanitizer_rejects_claimed_media_type_mismatch():
    with pytest.raises(AssetValidationError) as exc_info:
        sanitize_image(
            BytesIO(_image_bytes("JPEG")),
            claimed_media_type="image/png",
            max_image_bytes=1024 * 1024,
            max_image_pixels=1_000_000,
        )

    assert exc_info.value.code == "unsupported_image_type"


def test_sanitizer_stops_reading_immediately_after_size_limit():
    class RecordingStream(BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.bytes_returned = 0

        def read(self, size=-1):
            chunk = super().read(size)
            self.bytes_returned += len(chunk)
            return chunk

    stream = RecordingStream(b"x" * 10_000)

    with pytest.raises(AssetValidationError) as exc_info:
        sanitize_image(
            stream,
            claimed_media_type="image/png",
            max_image_bytes=32,
            max_image_pixels=1_000_000,
        )

    assert exc_info.value.code == "asset_too_large"
    assert stream.bytes_returned == 33


def test_sanitizer_rejects_truncated_decodable_type():
    truncated = _image_bytes("JPEG")[:24]

    with pytest.raises(AssetValidationError) as exc_info:
        sanitize_image(
            BytesIO(truncated),
            claimed_media_type="image/jpeg",
            max_image_bytes=1024,
            max_image_pixels=1_000_000,
        )

    assert exc_info.value.code == "image_decode_failed"


def test_sanitizer_rejects_images_above_pixel_limit():
    with pytest.raises(AssetValidationError) as exc_info:
        sanitize_image(
            BytesIO(_image_bytes("PNG", size=(100, 100))),
            claimed_media_type="image/png",
            max_image_bytes=1024 * 1024,
            max_image_pixels=9_999,
        )

    assert exc_info.value.code == "image_too_many_pixels"


def test_sanitizer_rejects_animated_allowed_format():
    output = BytesIO()
    first = Image.new("RGB", (16, 16), color="red")
    second = Image.new("RGB", (16, 16), color="blue")
    first.save(
        output,
        format="PNG",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )

    with pytest.raises(AssetValidationError) as exc_info:
        sanitize_image(
            BytesIO(output.getvalue()),
            claimed_media_type="image/png",
            max_image_bytes=1024 * 1024,
            max_image_pixels=1_000_000,
        )

    assert exc_info.value.code == "animated_image_not_allowed"


def test_sanitizer_removes_exif_and_trailing_payload():
    exif = Image.Exif()
    exif[0x010E] = "sensitive-description"
    source = _image_bytes("JPEG", exif=exif) + b"trailing-private-payload"

    sanitized = sanitize_image(
        BytesIO(source),
        claimed_media_type="image/jpeg",
        max_image_bytes=1024 * 1024,
        max_image_pixels=1_000_000,
    )

    assert b"trailing-private-payload" not in sanitized.data
    assert b"sensitive-description" not in sanitized.data
    with Image.open(BytesIO(sanitized.data)) as decoded:
        assert len(decoded.getexif()) == 0


def test_sanitizer_rejects_svg_even_when_claimed_as_png():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'

    with pytest.raises(AssetValidationError) as exc_info:
        sanitize_image(
            BytesIO(svg),
            claimed_media_type="image/png",
            max_image_bytes=1024,
            max_image_pixels=1_000_000,
        )

    assert exc_info.value.code == "unsupported_image_type"
