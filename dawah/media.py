"""Bounded MP3 content validation (R2 does not inspect media; Django does).

The immutable server-side copy is streamed into a bounded spooled temporary
file, then checked twice: a strict MPEG audio Layer III frame-chain walk (so
arbitrary bytes or a renamed file with one lucky sync word are rejected) and
mutagen's MPEG stream parser, which also supplies the duration.

ID3 tags are never parsed: their frames may be zlib-compressed (a small file
can expand to gigabytes). The tag is skipped using only its 10-byte header,
whose declared size is capped, and ``MPEGInfo`` starts after it.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass

MP3_CONTENT_TYPE = 'audio/mpeg'
MP3_MAX_BYTES = 100 * 1024 * 1024  # D2 engineering limit: 100 MiB
CHUNK_BYTES = 1024 * 1024
SPOOL_BYTES = 8 * 1024 * 1024
SYNC_SEARCH_BYTES = 64 * 1024
REQUIRED_FRAMES = 4
ID3_MAX_BYTES = 16 * 1024 * 1024  # a larger leading tag is rejected, never parsed

_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


class MediaRejected(Exception):
    """Content failed validation. ``code`` and ``message`` are safe to return."""

    def __init__(self, code, message):
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AudioInfo:
    duration_seconds: int | None
    bitrate: int | None
    sample_rate: int | None


def spool_stream(stream, *, expected_size, max_bytes=MP3_MAX_BYTES):
    """Copy at most ``max_bytes`` (and exactly ``expected_size``) into a temp file."""
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
    total = 0
    try:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes or total > expected_size:
                raise MediaRejected('FILE_TOO_LARGE', 'The stored file is larger than declared.')
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    finally:
        stream.close()
    if total != expected_size:
        spool.close()
        raise MediaRejected('SIZE_MISMATCH', 'The stored file size does not match the upload.')
    spool.seek(0)
    return spool


def _id3v2_length(head):
    if len(head) >= 10 and head[:3] == b'ID3':
        size = 0
        for byte in head[6:10]:
            if byte & 0x80:
                raise MediaRejected('INVALID_MP3', 'The file is not valid MP3 audio.')
            size = (size << 7) | byte
        footer = 10 if head[5] & 0x10 else 0
        if size + footer > ID3_MAX_BYTES:
            raise MediaRejected('INVALID_MP3', 'The file is not valid MP3 audio.')
        return 10 + size + footer
    return 0


def _frame_length(header):
    """Length of the Layer III frame starting with this 4-byte header, else None."""
    if len(header) < 4 or header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
        return None
    version = (header[1] >> 3) & 0x03  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5, 1=reserved
    layer = (header[1] >> 1) & 0x03     # 1=Layer III
    bitrate_index = (header[2] >> 4) & 0x0F
    rate_index = (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01
    if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    if version == 3:
        bitrate = _BITRATES_V1_L3[bitrate_index] * 1000
        coefficient = 144
    else:
        bitrate = _BITRATES_V2_L3[bitrate_index] * 1000
        coefficient = 72
    sample_rate = _SAMPLE_RATES[version][rate_index]
    return (coefficient * bitrate // sample_rate + padding), (version, sample_rate)


def audio_start(fileobj, size):
    """Offset of the audio after a (bounded, unparsed) leading ID3v2 tag."""
    fileobj.seek(0)
    start = _id3v2_length(fileobj.read(10))
    if start >= size:
        raise MediaRejected('INVALID_MP3', 'The file is not valid MP3 audio.')
    return start


def check_frame_chain(fileobj, size, start=None):
    """Require REQUIRED_FRAMES consecutive, consistent Layer III frames."""
    if start is None:
        start = audio_start(fileobj, size)
    fileobj.seek(start)
    window = fileobj.read(SYNC_SEARCH_BYTES + 4)
    for offset in range(max(0, len(window) - 3)):
        if window[offset] != 0xFF:
            continue
        position, signature, frames = start + offset, None, 0
        while frames < REQUIRED_FRAMES:
            fileobj.seek(position)
            parsed = _frame_length(fileobj.read(4))
            if parsed is None:
                break
            length, frame_signature = parsed
            if signature is not None and frame_signature != signature:
                break
            signature = frame_signature
            frames += 1
            position += length
            if position > size:
                break
        if frames >= REQUIRED_FRAMES:
            return
    raise MediaRejected('INVALID_MP3', 'The file is not valid MP3 audio.')


def validate_mp3(fileobj, size) -> AudioInfo:
    """Validate an MP3 in a seekable file of ``size`` bytes; raises MediaRejected."""
    if size <= 0:
        raise MediaRejected('EMPTY_FILE', 'The file is empty.')
    if size > MP3_MAX_BYTES:
        raise MediaRejected('FILE_TOO_LARGE', 'The file exceeds the 100 MiB audio limit.')
    try:
        start = audio_start(fileobj, size)
        check_frame_chain(fileobj, size, start)
        from mutagen.mp3 import MPEGInfo

        info = MPEGInfo(fileobj, offset=start)  # MPEG stream only; never ID3 frames
        length = getattr(info, 'length', 0) or 0
    except MediaRejected:
        raise
    except Exception:
        # Any parser/IO surprise on untrusted bytes is a rejection, never a 500.
        raise MediaRejected('INVALID_MP3', 'The file is not valid MP3 audio.') from None
    if length <= 0:
        raise MediaRejected('INVALID_MP3', 'The file is not valid MP3 audio.')
    duration = max(1, round(length))
    return AudioInfo(duration_seconds=duration, bitrate=getattr(info, 'bitrate', None),
                     sample_rate=getattr(info, 'sample_rate', None))
