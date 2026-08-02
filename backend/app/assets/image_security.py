import warnings
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import BinaryIO

import filetype
from PIL import Image, ImageOps, UnidentifiedImageError

from app.assets.errors import AssetValidationError


SUPPORTED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
PIL_FORMAT_TO_MEDIA_TYPE = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


@dataclass(frozen=True)
class SanitizedImage:
    data: bytes
    media_type: str
    width: int
    height: int
    sha256: str


def sanitize_image(
    stream: BinaryIO,
    *,
    claimed_media_type: str,
    max_image_bytes: int,
    max_image_pixels: int,
    allowed_media_types: tuple[str, ...] | frozenset[str] = SUPPORTED_MEDIA_TYPES,
) -> SanitizedImage:
    source = _read_bounded(stream, max_image_bytes=max_image_bytes)
    claimed = claimed_media_type.partition(";")[0].strip().lower()
    allowed = frozenset(allowed_media_types) & SUPPORTED_MEDIA_TYPES
    kind = filetype.guess(source)
    detected = kind.mime.lower() if kind is not None else None
    if detected == "image/apng":
        detected = "image/png"
    if detected not in allowed or claimed != detected:
        raise AssetValidationError(
            code="unsupported_image_type",
            message="图片类型不受支持或声明类型与内容不一致。",
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            _verify_image(
                source,
                expected_media_type=detected,
                max_image_pixels=max_image_pixels,
            )
            normalized, width, height = _decode_and_normalize(
                source,
                expected_media_type=detected,
                max_image_pixels=max_image_pixels,
            )
    except AssetValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise AssetValidationError(
            code="image_too_many_pixels",
            message="图片像素数量超过限制。",
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, TypeError, ValueError) as exc:
        raise AssetValidationError(
            code="image_decode_failed",
            message="图片无法完整解码。",
        ) from exc

    encoded = _encode_image(normalized, media_type=detected)
    return SanitizedImage(
        data=encoded,
        media_type=detected,
        width=width,
        height=height,
        sha256=sha256(encoded).hexdigest(),
    )


def _read_bounded(stream: BinaryIO, *, max_image_bytes: int) -> bytes:
    if max_image_bytes < 1:
        raise ValueError("max_image_bytes must be positive")
    chunks: list[bytes] = []
    remaining = max_image_bytes + 1
    while remaining > 0:
        chunk = stream.read(min(64 * 1024, remaining))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise AssetValidationError(
                code="image_decode_failed",
                message="上传流必须返回二进制数据。",
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > max_image_bytes:
        raise AssetValidationError(
            code="asset_too_large",
            message="图片大小超过限制。",
        )
    return data


def _verify_image(
    source: bytes,
    *,
    expected_media_type: str,
    max_image_pixels: int,
) -> None:
    with Image.open(BytesIO(source)) as image:
        _validate_decoded_contract(
            image,
            expected_media_type=expected_media_type,
            max_image_pixels=max_image_pixels,
        )
        image.verify()


def _decode_and_normalize(
    source: bytes,
    *,
    expected_media_type: str,
    max_image_pixels: int,
) -> tuple[Image.Image, int, int]:
    with Image.open(BytesIO(source)) as image:
        _validate_decoded_contract(
            image,
            expected_media_type=expected_media_type,
            max_image_pixels=max_image_pixels,
        )
        image.load()
        transposed = ImageOps.exif_transpose(image)
        normalized = _normalize_color_mode(transposed, media_type=expected_media_type)
        normalized.load()
        return normalized.copy(), normalized.width, normalized.height


def _validate_decoded_contract(
    image: Image.Image,
    *,
    expected_media_type: str,
    max_image_pixels: int,
) -> None:
    decoded_media_type = PIL_FORMAT_TO_MEDIA_TYPE.get(image.format or "")
    if decoded_media_type != expected_media_type:
        raise AssetValidationError(
            code="unsupported_image_type",
            message="图片解码格式与 Magic Bytes 不一致。",
        )
    if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
        raise AssetValidationError(
            code="animated_image_not_allowed",
            message="不允许上传动图或多帧图片。",
        )
    width, height = image.size
    if width < 1 or height < 1:
        raise AssetValidationError(
            code="image_decode_failed",
            message="图片尺寸无效。",
        )
    if width * height > max_image_pixels:
        raise AssetValidationError(
            code="image_too_many_pixels",
            message="图片像素数量超过限制。",
        )


def _normalize_color_mode(image: Image.Image, *, media_type: str) -> Image.Image:
    if media_type == "image/jpeg":
        if "A" in image.getbands():
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            return background
        return image.convert("RGB")
    target_mode = "RGBA" if "A" in image.getbands() else "RGB"
    return image.convert(target_mode)


def _encode_image(image: Image.Image, *, media_type: str) -> bytes:
    output = BytesIO()
    if media_type == "image/jpeg":
        image.save(output, format="JPEG", quality=90, optimize=True, progressive=False)
    elif media_type == "image/png":
        image.save(output, format="PNG", optimize=True, compress_level=9)
    elif media_type == "image/webp":
        image.save(output, format="WEBP", quality=90, method=6)
    else:
        raise AssetValidationError(
            code="unsupported_image_type",
            message="图片类型不受支持。",
        )
    return output.getvalue()
