"""Bounded featured-image validation (JPEG/PNG) with Pillow.

R2 performs no image validation or transformation; Django verifies the
immutable validated copy before it can become a featured image:

* magic bytes must be JPEG or PNG and match the declared type;
* Pillow may open only that one format (no other decoder is reachable);
* dimensions are read from the header and capped (``IMAGE_MAX_PIXELS``,
  ``IMAGE_MAX_SIDE``) *before* any pixel data is decoded, so a small file
  that declares a huge canvas (a decompression bomb) is refused cheaply;
  Pillow's own ``DecompressionBombWarning`` is escalated to a rejection;
* PNG chunk CRCs are verified, then the image is fully decoded once
  (truncated or corrupt data is refused).

The stored bytes are served unchanged (nothing is stripped or re-encoded);
only bytes that passed these checks are ever attached or published.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

from PIL import Image

from .media import MediaRejected

JPEG_CONTENT_TYPE = 'image/jpeg'
PNG_CONTENT_TYPE = 'image/png'
IMAGE_MAX_BYTES = 10 * 1024 * 1024  # D2 engineering limit: 10 MiB
IMAGE_MAX_PIXELS = 30_000_000       # strict cap, well below Pillow's default bomb threshold
IMAGE_MAX_SIDE = 12_000

MAGIC = {
    PNG_CONTENT_TYPE: b'\x89PNG\r\n\x1a\n',
    JPEG_CONTENT_TYPE: b'\xff\xd8\xff',
}
PIL_FORMATS = {PNG_CONTENT_TYPE: ('PNG', {'PNG'}), JPEG_CONTENT_TYPE: ('JPEG', {'JPEG', 'MPO'})}


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    format: str


def _invalid():
    return MediaRejected('INVALID_IMAGE', 'The file is not a valid JPEG or PNG image.')


def sniff(head):
    for content_type, magic in MAGIC.items():
        if head.startswith(magic):
            return content_type
    return None


def validate_image(fileobj, size, content_type) -> ImageInfo:
    """Validate a JPEG/PNG of ``size`` bytes in a seekable file; raises MediaRejected."""
    if size <= 0:
        raise MediaRejected('EMPTY_FILE', 'The file is empty.')
    if size > IMAGE_MAX_BYTES:
        raise MediaRejected('FILE_TOO_LARGE', 'The file exceeds the 10 MiB image limit.')
    fileobj.seek(0)
    actual = sniff(fileobj.read(16))
    if actual is None:
        raise _invalid()
    if actual != content_type:
        raise MediaRejected('CONTENT_TYPE_MISMATCH', 'The image content does not match the declared type.')
    plugin, accepted = PIL_FORMATS[content_type]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            fileobj.seek(0)
            with Image.open(fileobj, formats=[plugin]) as image:
                if image.format not in accepted:
                    raise _invalid()
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise _invalid()
                if width > IMAGE_MAX_SIDE or height > IMAGE_MAX_SIDE or width * height > IMAGE_MAX_PIXELS:
                    raise MediaRejected('IMAGE_TOO_LARGE', 'The image dimensions exceed the allowed limit '
                                                           f'({IMAGE_MAX_PIXELS // 1_000_000} megapixels).')
                image.verify()  # PNG: chunk CRCs; must run on a freshly opened image
            fileobj.seek(0)
            with Image.open(fileobj, formats=[plugin]) as image:
                if image.size != (width, height):
                    raise _invalid()
                image.load()  # full decode of the (first) frame
                found = image.format
    except MediaRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MediaRejected('IMAGE_TOO_LARGE', 'The image dimensions exceed the allowed limit.') from None
    except Exception:
        # Any decoder surprise on untrusted bytes is a rejection, never a 500.
        raise _invalid() from None
    return ImageInfo(width=width, height=height, format=found)
