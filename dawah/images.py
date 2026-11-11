"""Bounded featured-image validation and sanitization (JPEG/PNG) with Pillow.

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

The uploaded bytes are never published. A sanitized derivative is
re-encoded server-side from the decoded pixels: EXIF orientation applied,
EXIF/GPS, XMP, IPTC, comments and PNG text chunks dropped (only the ICC
colour profile is kept), first frame only, long side at most
``OUTPUT_MAX_SIDE`` (JPEG decodes at a reduced DCT scale via ``draft`` when
the source is larger). That derivative becomes the featured image.

Memory bound per finalize: decoded pixels at most 16 MP x 4 bytes (64 MiB;
a JPEG drafted to the output size uses far less), at most one transformed
copy of the same size, plus the encoded output (capped at
``OUTPUT_MAX_BYTES``) and the spooled input (8 MiB in memory, rest on disk).
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import io
import math

from PIL import Image, ImageOps

from .media import MediaRejected

JPEG_CONTENT_TYPE = 'image/jpeg'
PNG_CONTENT_TYPE = 'image/png'
IMAGE_MAX_BYTES = 10 * 1024 * 1024  # D2 engineering limit: 10 MiB
IMAGE_MAX_PIXELS = 16_000_000       # strict cap, well below Pillow's default bomb threshold
IMAGE_MAX_SIDE = 12_000
OUTPUT_MAX_SIDE = 4096              # sanitized derivative: long side at most this
OUTPUT_MAX_BYTES = 24 * 1024 * 1024
JPEG_QUALITY = 88
ICC_MAX_BYTES = 1024 * 1024

MAGIC = {
    PNG_CONTENT_TYPE: b'\x89PNG\r\n\x1a\n',
    JPEG_CONTENT_TYPE: b'\xff\xd8\xff',
}
PIL_FORMATS = {PNG_CONTENT_TYPE: ('PNG', {'PNG'}), JPEG_CONTENT_TYPE: ('JPEG', {'JPEG', 'MPO'})}


@dataclass(frozen=True)
class ImageInfo:
    width: int   # of the sanitized derivative
    height: int
    format: str
    sanitized: bytes = b''  # re-encoded derivative (same MIME type as the upload)


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
                scale = min(1.0, OUTPUT_MAX_SIDE / max(width, height))
                if plugin == 'JPEG' and scale < 1:
                    # Decode at the smallest DCT scale still >= the output size.
                    image.draft(None, (max(1, math.ceil(width * scale)), max(1, math.ceil(height * scale))))
                image.load()  # full decode of the (first) frame: corrupt/truncated data fails here
                found = image.format
                sanitized, out_size = _sanitize(image, content_type)
    except MediaRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MediaRejected('IMAGE_TOO_LARGE', 'The image dimensions exceed the allowed limit.') from None
    except Exception:
        # Any decoder surprise on untrusted bytes is a rejection, never a 500.
        raise _invalid() from None
    return ImageInfo(width=out_size[0], height=out_size[1], format=found, sanitized=sanitized)


def _sanitize(image, content_type):
    """Re-encode the decoded first frame without metadata. Returns (bytes, size)."""
    icc = image.info.get('icc_profile')
    try:
        oriented = ImageOps.exif_transpose(image)  # applies and drops the Orientation tag
    except Exception:  # noqa: BLE001 - a malformed EXIF block only loses orientation
        oriented = image.copy()
    if max(oriented.size) > OUTPUT_MAX_SIDE:
        oriented.thumbnail((OUTPUT_MAX_SIDE, OUTPUT_MAX_SIDE), Image.Resampling.LANCZOS)
    if content_type == JPEG_CONTENT_TYPE:
        if oriented.mode not in ('L', 'RGB'):
            # CMYK/YCCK/other: convert; a CMYK colour profile no longer applies.
            if oriented.mode == 'CMYK':
                icc = None
            oriented = oriented.convert('RGB')
    elif oriented.mode not in ('1', 'L', 'LA', 'RGB', 'RGBA') or 'transparency' in oriented.info:
        # Palette/16-bit/tRNS images become explicit RGBA (no ancillary chunks needed).
        oriented = oriented.convert('RGBA')
    oriented.info = {}  # never carry EXIF/XMP/comments/text chunks into the encoder
    options = {}
    if icc and len(icc) <= ICC_MAX_BYTES:
        options['icc_profile'] = icc
    buffer = io.BytesIO()
    if content_type == JPEG_CONTENT_TYPE:
        oriented.save(buffer, 'JPEG', quality=JPEG_QUALITY, optimize=True, exif=b'', comment=b'', **options)
    else:
        oriented.save(buffer, 'PNG', optimize=False, **options)
    data = buffer.getvalue()
    if not data or len(data) > OUTPUT_MAX_BYTES:
        raise MediaRejected('IMAGE_TOO_LARGE', 'The image could not be prepared within the size limit.')
    return data, oriented.size
