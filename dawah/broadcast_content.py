"""Broadcast content rules and rendering (D4).

COMM-01 supports plain-text bodies only: the HTML alternative is derived by
escaping the text, so no author-supplied markup ever reaches preview or email.
COMM-04 adds sanitized rich HTML; it replaces ``render`` for ``rich`` drafts and
bumps ``TEMPLATE_VERSION`` when the template changes (queued snapshots keep the
version they were frozen with).
"""
import html
import re
from dataclasses import dataclass

SUBJECT_MAX_LENGTH = 200
BODY_MAX_LENGTH = 50_000
TEMPLATE_VERSION = 'plain-v1'

# Header injection: any line break / control / Unicode separator in a subject.
_SUBJECT_FORBIDDEN = re.compile('[\x00-\x1f\x7f-\x9f  ]')
# Body: printable text plus tab and newline (CR is normalized away first).
_BODY_FORBIDDEN = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_BLANK_LINES = re.compile(r'\n[ \t]*\n+')


def subject_error(value):
    if _SUBJECT_FORBIDDEN.search(value):
        return 'Subject must be a single line without control characters.'
    return None


def normalize_body(value):
    """Canonical plain text: LF line endings, no trailing whitespace at the end."""
    return value.replace('\r\n', '\n').replace('\r', '\n').rstrip()


def body_error(value):
    if _BODY_FORBIDDEN.search(value):
        return 'Message text must not contain control characters.'
    return None


@dataclass(frozen=True)
class Rendered:
    subject: str
    text: str
    html: str
    template_version: str


def plain_to_html(text):
    """Escaped paragraphs: blank lines separate paragraphs, single newlines are <br>."""
    paragraphs = [p.strip('\n') for p in _BLANK_LINES.split(text) if p.strip()]
    return ''.join('<p>' + '<br>'.join(html.escape(line) for line in p.split('\n')) + '</p>'
                   for p in paragraphs)


def render(broadcast):
    """Rendered subject/text/HTML exactly as preview shows and queue freezes."""
    text = normalize_body(broadcast.body)
    return Rendered(subject=broadcast.subject, text=text, html=plain_to_html(text),
                    template_version=TEMPLATE_VERSION)
