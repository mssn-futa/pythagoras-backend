"""Structural YouTube link parsing (D2 "YouTube links and playback").

Only a validated 11-character video ID is ever persisted. Watch/embed URLs are
constructed here from that ID; submitted text is never stored or echoed as
markup, and no URL is ever fetched, resolved or sent to YouTube.
"""
import re
from urllib.parse import urlsplit

VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}', re.ASCII)
MAX_URL_LENGTH = 2048

YOUTUBE_HOSTS = frozenset({'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'})
SHORT_HOST = 'youtu.be'
NOCOOKIE_HOSTS = frozenset({'www.youtube-nocookie.com'})

WATCH_URL = 'https://www.youtube.com/watch?v={}'
EMBED_URL = 'https://www.youtube-nocookie.com/embed/{}'


class InvalidYouTubeUrl(ValueError):
    """``str()`` is a safe, user-facing message."""


def _fail(message='Enter a valid YouTube video link (youtube.com watch, embed or shorts, or youtu.be).'):
    raise InvalidYouTubeUrl(message)


def _single_segment(path, prefix):
    """``<prefix><id>`` with an optional trailing slash, else None."""
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    return rest[:-1] if rest.endswith('/') else rest


def parse_youtube_url(value):
    """Return the video ID of a supported YouTube HTTPS link or raise InvalidYouTubeUrl."""
    if not isinstance(value, str):
        _fail()
    value = value.strip()
    if not value:
        _fail('This field may not be blank.')
    if len(value) > MAX_URL_LENGTH:
        _fail('This link is too long.')
    # ASCII only, no whitespace/control characters, no backslashes (parsers disagree on them).
    if not value.isascii() or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch == '\\' for ch in value):
        _fail()
    if not value.lower().startswith('https://'):
        _fail('Only https:// YouTube links are accepted.')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        _fail()
    if parts.username is not None or parts.password is not None or '@' in parts.netloc or port is not None:
        _fail()
    host = parts.hostname or ''
    if parts.netloc.lower() != host:  # rejects stray ':' , trailing dots, brackets
        _fail()

    video_id = None
    if host == SHORT_HOST:
        video_id = _single_segment(parts.path, '/')
    elif host in YOUTUBE_HOSTS:
        if parts.path == '/watch':
            # Raw tokens: a percent-encoded or ';'-joined ID never decodes into a valid one.
            ids = [token[2:] for token in parts.query.split('&') if token.startswith('v=')]
            if len(ids) != 1:
                _fail('The link must contain exactly one video ID.')
            video_id = ids[0]
        else:
            video_id = _single_segment(parts.path, '/embed/')
            if video_id is None:
                video_id = _single_segment(parts.path, '/shorts/')
    elif host in NOCOOKIE_HOSTS:
        video_id = _single_segment(parts.path, '/embed/')
    # "videoseries" is YouTube's playlist-embed token and happens to be 11 characters long.
    if video_id is None or not VIDEO_ID.fullmatch(video_id) or video_id == 'videoseries':
        _fail()
    return video_id


def is_valid_video_id(value):
    return isinstance(value, str) and VIDEO_ID.fullmatch(value) is not None


def playback_for(video_id):
    """Typed playback metadata constructed only from a validated ID."""
    if not is_valid_video_id(video_id):
        return None
    return {'provider': 'youtube', 'video_id': video_id,
            'watch_url': WATCH_URL.format(video_id), 'embed_url': EMBED_URL.format(video_id)}
