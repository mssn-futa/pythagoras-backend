"""Broadcast content rules, rich-text sanitization and the email template (D4).

One pipeline produces everything a recipient or an admin sees:

* ``sanitize_rich_html(raw)`` turns author HTML into the canonical stored
  ``body_html`` plus its plain-text fallback (``body``). It runs on save and
  again before preview and queue.
* ``render_content(...)`` / ``render(broadcast)`` build the exact subject,
  text part and HTML document that preview returns and queue freezes into the
  ``snapshot_*`` fields. The outbox worker sends those frozen fields verbatim,
  so preview, the stored snapshot and the Resend payload are byte-identical.

Sanitizer (authoritative; the editor is only a convenience):

1. ``nh3`` (ammonia, an HTML5-spec parser) keeps only the D4 allowlist
   ``p br strong em h2 h3 ul ol li blockquote a`` and ``a[href]`` with an
   absolute ``http``/``https`` URL. Comments, every other attribute (event
   handlers, ``style``, ``class``, ``target``...) and tags are dropped; the
   content of script/style/iframe/form/svg/math/... is removed entirely.
2. The ammonia output is parsed into a small tree and normalized: inline runs
   at block level are wrapped in ``<p>``, list children are ``<li>``, blocks
   inside inline formatting are flattened, links are re-validated and
   normalized (lower-case scheme/host, IDNA host, percent-encoded path), empty
   formatting/blocks are removed and whitespace is collapsed.
3. The tree is serialized by this module (escaped text and attribute values,
   allowlisted tag names only, no raw-text elements), so re-parsing it cannot
   change its structure (no mXSS) and ``sanitize(sanitize(x)) == sanitize(x)``.

Links carry ``rel="noopener noreferrer nofollow"`` and no ``target``: mail
clients already open links outside the message, and the preview sandbox has
its own base target.

``TEMPLATE_VERSION`` names the email template. Changing the template (styles,
wrapper, text fallback rules) must bump it; queued broadcasts keep the
snapshot and version they were frozen with.
"""
import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

import nh3

SUBJECT_MAX_LENGTH = 200
BODY_MAX_LENGTH = 50_000
RICH_HTML_MAX_LENGTH = 100_000
MAX_NESTING_DEPTH = 20  # of the canonical tree (e.g. ul > li > ul > li > p > strong > a is 7)
LINK_MAX_LENGTH = 2000
LINK_REL = 'noopener noreferrer nofollow'
TEMPLATE_VERSION = 'email-v2'  # COMM-01: 'plain-v1' (bare escaped paragraphs)

ALLOWED_TAGS = frozenset({'p', 'br', 'strong', 'em', 'h2', 'h3', 'ul', 'ol', 'li', 'blockquote', 'a'})
LINK_SCHEMES = frozenset({'http', 'https'})
# Removed together with everything inside them (not just the tag).
REMOVED_WITH_CONTENT = frozenset({
    'script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'noscript', 'noembed',
    'noframes', 'template', 'form', 'select', 'textarea', 'button', 'svg', 'math', 'title', 'head', 'xmp',
    'plaintext', 'canvas', 'video', 'audio', 'picture', 'map',
})

EMPTY_MESSAGE = 'The message has no text left after removing unsupported content.'
TOO_LONG_MESSAGE = f'The formatted message must be {RICH_HTML_MAX_LENGTH:,} characters or fewer.'
TOO_DEEP_MESSAGE = 'The formatted message is nested too deeply. Simplify lists and quotes.'
TOO_COMPLEX_MESSAGE = 'The formatted message has too many tags. Simplify the formatting.'
# Cheap bound on author input checked before parsing (``check_rich_input``).
RICH_HTML_MAX_TAGS = 20_000
# The rendered email document (template + inline styles) must stay bounded too.
EMAIL_HTML_MAX_LENGTH = 400_000

# Header injection: any line break / control / Unicode separator in a subject.
_SUBJECT_FORBIDDEN = re.compile('[\x00-\x1f\x7f-\x9f  ]')
# Body: printable text plus tab and newline (CR is normalized away first).
_BODY_FORBIDDEN = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_BLANK_LINES = re.compile(r'\n[ \t]*\n+')
_HTML_SPACE = re.compile('[ \t\n\r\f]+')
_CONTROL = re.compile('[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]')
_INVISIBLE = re.compile('[​-‏⁠-⁤﻿­\\s]+')
# Pasted structure outside the allowlist that is mapped rather than merged
# into one paragraph: headings to h2/h3, row/term/preformatted blocks to p,
# containers to a transparent "div" (never emitted), <hr> to a block break.
_STRUCTURAL = {
    'h1': 'h2', 'h4': 'h3', 'h5': 'h3', 'h6': 'h3',
    'pre': 'p', 'tr': 'p', 'dt': 'p', 'dd': 'p', 'figcaption': 'p', 'caption': 'p',
    **{tag: 'div' for tag in ('div', 'section', 'article', 'header', 'footer', 'main', 'aside', 'nav',
                              'figure', 'address', 'center', 'table', 'thead', 'tbody', 'tfoot', 'dl',
                              'td', 'th', 'details', 'summary', 'hgroup', 'fieldset', 'legend')},
    'hr': 'hr',
}
_VOID = frozenset({'br', 'hr'})
_BLOCK_TAGS = frozenset({'p', 'h2', 'h3', 'ul', 'ol', 'li', 'blockquote', 'div', 'hr'})
_TEXT_BLOCKS = frozenset({'p', 'h2', 'h3'})
_PARSE_DEPTH_LIMIT = 2 * MAX_NESTING_DEPTH  # bound recursion before normalization

_CLEANER = nh3.Cleaner(
    tags=set(ALLOWED_TAGS) | set(_STRUCTURAL), clean_content_tags=set(REMOVED_WITH_CONTENT), attributes={'a': {'href'}},
    url_schemes=set(LINK_SCHEMES), url_relative='deny', link_rel=None, strip_comments=True,
    generic_attribute_prefixes=set(), allowed_classes={}, filter_style_properties=set(),
)


class ContentError(ValueError):
    """Rich content that cannot be accepted; ``str(error)`` is user-safe."""


# ---------------------------------------------------------------- plain text

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


def plain_to_html(text):
    """Escaped paragraphs: blank lines separate paragraphs, single newlines are <br>."""
    paragraphs = [p.strip('\n') for p in _BLANK_LINES.split(text) if p.strip()]
    return ''.join('<p>' + '<br>'.join(html.escape(line) for line in p.split('\n')) + '</p>'
                   for p in paragraphs)


# ---------------------------------------------------------------- tree

class _Node:
    __slots__ = ('tag', 'href', 'children')

    def __init__(self, tag, children=None, href=None):
        self.tag, self.href, self.children = tag, href, children if children is not None else []

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'<{self.tag} {self.children!r}>'


class _TreeBuilder(HTMLParser):
    """Builds a tree from ammonia output (well-formed; allowlisted and
    structural tags only, no attributes except a[href]). Structural tags are
    renamed (``_STRUCTURAL``) so normalization keeps their block boundaries."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node('#root')
        self.stack = [self.root]
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'pre':
            self.pre += 1
        tag = _STRUCTURAL.get(tag, tag)
        if tag not in ALLOWED_TAGS and tag not in ('div', 'hr'):  # defensive: never expected after nh3
            return
        href = dict(attrs).get('href') if tag == 'a' else None
        node = _Node(tag, href=href)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)
            if len(self.stack) > _PARSE_DEPTH_LIMIT:
                raise ContentError(TOO_DEEP_MESSAGE)

    def handle_startendtag(self, tag, attrs):
        tag = _STRUCTURAL.get(tag, tag)
        if tag in _VOID:
            self.stack[-1].children.append(_Node(tag))

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.pre = max(0, self.pre - 1)
        tag = _STRUCTURAL.get(tag, tag)
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        children = self.stack[-1].children
        if not self.pre:
            children.append(data)
            return
        for index, line in enumerate(data.replace('\r\n', '\n').split('\n')):  # keep preformatted lines
            if index:
                children.append(_Node('br'))
            if line:
                children.append(line)


def _parse(fragment):
    builder = _TreeBuilder()
    builder.feed(fragment)
    builder.close()
    return builder.root.children


def _visible(text):
    return bool(_INVISIBLE.sub('', text))


def _has_text(nodes):
    return any(_visible(node) if isinstance(node, str) else _has_text(node.children) for node in nodes)


def _depth(nodes):
    return max((1 + _depth(node.children) for node in nodes if isinstance(node, _Node)), default=0)


# ---------------------------------------------------------------- links

def _hidden_or_space(char):
    return char.isspace() or unicodedata.category(char)[0] in 'CZ'


_HOST = re.compile(r'[a-z0-9_-][a-z0-9_.-]*')
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + '?'
_FRAGMENT_SAFE = _QUERY_SAFE + '#'
_BAD_PERCENT = re.compile('%(?![0-9A-Fa-f]{2})')


def normalize_link(href):
    """Absolute http(s) URL in canonical form, or ``None`` when unsafe/invalid.

    Rejects other schemes (javascript:, data:, vbscript:, mailto: ...),
    relative and scheme-relative URLs, user info, whitespace and any hidden
    character (so entity/whitespace obfuscation never reaches a scheme check
    by a different parser)."""
    if not isinstance(href, str):
        return None
    value = href.strip(' \t\n\r\f')
    if not value or len(value) > LINK_MAX_LENGTH or any(_hidden_or_space(char) for char in value):
        return None
    if '\\' in value or _BAD_PERCENT.search(value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in LINK_SCHEMES or not value[len(scheme) + 1:].startswith('//'):
        return None
    if '@' in parts.netloc or not parts.hostname:
        return None
    host = parts.hostname
    if '[' in parts.netloc and ':' not in host:  # brackets are only for IPv6 literals
        return None
    if ':' in host:  # IPv6 literal
        host = f'[{host}]'
    else:
        try:
            host = host.encode('idna').decode('ascii').lower()
        except UnicodeError:
            return None
        if not _HOST.fullmatch(host) or '..' in host:
            return None
    if port is not None and (scheme, port) not in {('http', 80), ('https', 443)}:
        host = f'{host}:{port}'
    url = f'{scheme}://{host}{quote(parts.path, safe=_PATH_SAFE) or "/"}'
    if parts.query:
        url += '?' + quote(parts.query, safe=_QUERY_SAFE)
    if parts.fragment:
        url += '#' + quote(parts.fragment, safe=_FRAGMENT_SAFE)
    if len(url) > LINK_MAX_LENGTH:
        return None
    return url


# ---------------------------------------------------------------- normalization

def _clean_text(text):
    return _HTML_SPACE.sub(' ', _CONTROL.sub('', text))


def _inline(nodes, in_link=False):
    """Inline content: text, br, strong, em, a. Blocks found here are flattened
    (their content kept, separated by line breaks)."""
    out = []
    for node in nodes:
        if isinstance(node, str):
            text = _clean_text(node)
            if text:
                out.append(text)
            continue
        if node.tag == 'br' or node.tag == 'hr':
            if node.tag == 'br' or out:
                out.append(_Node('br'))
            continue
        if node.tag in _BLOCK_TAGS:
            inner = _inline(node.children, in_link)
            if inner:
                if out:
                    out.append(_Node('br'))
                out.extend(inner)
            continue
        href = normalize_link(node.href) if node.tag == 'a' and not in_link else None
        inner = _inline(node.children, in_link or href is not None)
        if (node.tag == 'a' and href is None) or not _has_text(inner):
            out.extend(inner)  # unwrap: invalid/nested link or formatting without text
        else:
            out.append(_Node(node.tag, inner, href=href))
    return _merge_text(out)


def _merge_text(nodes):
    merged = []
    for node in nodes:
        if isinstance(node, str) and merged and isinstance(merged[-1], str):
            merged[-1] = _HTML_SPACE.sub(' ', merged[-1] + node)
        else:
            merged.append(node)
    return merged


def _trim(nodes, leading):
    """Strip whitespace and line breaks at one edge of a text block (recursing
    into the first/last inline element). Returns the trimmed list."""
    nodes = list(nodes)
    while nodes:
        index = 0 if leading else -1
        node = nodes[index]
        if isinstance(node, str):
            text = node.lstrip(' ') if leading else node.rstrip(' ')
            if text:
                nodes[index] = text
                return nodes
            nodes.pop(index)
        elif node.tag == 'br':
            nodes.pop(index)
        else:
            children = _trim(node.children, leading)
            if _has_text(children):
                nodes[index] = _Node(node.tag, children, href=node.href)
                return nodes
            nodes.pop(index)  # spaces/breaks only: edge padding
    return nodes


def _around_breaks(nodes):
    """No spaces directly around a line break inside one inline list."""
    out = list(nodes)
    for i, node in enumerate(out):
        if isinstance(node, str):
            if i + 1 < len(out) and isinstance(out[i + 1], _Node) and out[i + 1].tag == 'br':
                node = node.rstrip(' ')
            if i > 0 and isinstance(out[i - 1], _Node) and out[i - 1].tag == 'br':
                node = node.lstrip(' ')
            out[i] = node
        elif node.tag != 'br':
            out[i] = _Node(node.tag, _around_breaks(node.children), href=node.href)
    return [node for node in out if node != '']


def _text_block(tag, children):
    inline = _trim(_trim(_around_breaks(_inline(children)), True), False)
    return _Node(tag, inline) if _has_text(inline) else None


def _flow(nodes):
    """Block content of the root, a blockquote or a list item."""
    out, run, items = [], [], []

    def flush_run():
        if run:
            block = _text_block('p', run)
            run.clear()
            if block:
                out.append(block)

    def flush_items():
        if items:
            block = _list('ul', items)
            items.clear()
            if block:
                out.append(block)

    for node in nodes:
        if isinstance(node, _Node) and node.tag == 'li':
            flush_run()
            items.append(node)
            continue
        if isinstance(node, str) and not _visible(node) and not run and items:
            continue  # whitespace between stray list items
        flush_items()
        if isinstance(node, str) or node.tag not in _BLOCK_TAGS:
            run.append(node)
            continue
        flush_run()
        if node.tag == 'div':
            out.extend(_flow(node.children))
        elif node.tag != 'hr':
            block = _block(node)
            if block:
                out.append(block)
    flush_run()
    flush_items()
    return out


def _list(tag, nodes):
    items, loose = [], []

    def flush_loose():
        if loose:
            content = _flow(loose)
            loose.clear()
            if content:
                items.append(_Node('li', content))

    for node in nodes:
        if isinstance(node, _Node) and node.tag == 'li':
            flush_loose()
            content = _flow(node.children)
            if content:
                items.append(_Node('li', content))
        elif isinstance(node, str) and not _visible(node) and not loose:
            continue
        else:
            loose.append(node)
    flush_loose()
    return _Node(tag, items) if items else None


def _block(node):
    if node.tag in _TEXT_BLOCKS:
        return _text_block(node.tag, node.children)
    if node.tag in ('ul', 'ol'):
        return _list(node.tag, node.children)
    content = _flow(node.children)  # blockquote
    return _Node('blockquote', content) if content else None


# ---------------------------------------------------------------- serialization

def _attr(value):
    return html.escape(value, quote=False).replace('"', '&quot;')


def _serialize(nodes, styles=None, parent='#root'):
    parts = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(html.escape(node, quote=False))
            continue
        if node.tag == 'br':
            parts.append('<br>')
            continue
        attrs = ''
        if node.tag == 'a':
            attrs = f' href="{_attr(node.href)}" rel="{LINK_REL}"'
        if styles:
            style = styles.get(f'{parent}>{node.tag}') or styles.get(node.tag)
            if style:
                attrs += f' style="{_attr(style)}"'
        parts.append(f'<{node.tag}{attrs}>{_serialize(node.children, styles, node.tag)}</{node.tag}>')
    return ''.join(parts)


# ---------------------------------------------------------------- text fallback

def _inline_text(nodes):
    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif node.tag == 'br':
            out.append('\n')
        elif node.tag == 'a':
            text = _inline_text(node.children)
            bare = text.strip()
            same = bare == node.href or normalize_link(bare) == node.href  # e.g. autolinked "https://x.com"
            out.append(text if same else f'{text} ({node.href})')
        else:
            out.append(_inline_text(node.children))
    return ''.join(out)


def _block_text(node):
    if node.tag in ('ul', 'ol'):
        lines = []
        for index, item in enumerate(node.children, start=1):
            prefix = f'{index}. ' if node.tag == 'ol' else '- '
            lines.append(prefix + '\n'.join(text for text in map(_block_text, item.children) if text))
        return '\n'.join(lines)
    if node.tag == 'blockquote':
        return '\n\n'.join(text for text in map(_block_text, node.children) if text)
    return _inline_text(node.children)


def text_fallback(blocks):
    """Frozen COMM-05 format: blocks separated by a blank line; list items
    prefixed "- " or "1. " (one per line); links "text (href)", or just the
    text when it equals the URL; <br> becomes a newline; quotes and headings
    are their text."""
    return '\n\n'.join(text for text in map(_block_text, blocks) if text.strip()).strip()


# ---------------------------------------------------------------- public API

@dataclass(frozen=True)
class SanitizedRich:
    html: str  # canonical body_html
    text: str  # generated body (text fallback)


def _sanitized_tree(raw):
    if not isinstance(raw, str):
        raise ContentError(EMPTY_MESSAGE)
    blocks = _flow(_parse(_CLEANER.clean(raw)))
    if not blocks or not text_fallback(blocks):
        raise ContentError(EMPTY_MESSAGE)
    if _depth(blocks) > MAX_NESTING_DEPTH:
        raise ContentError(TOO_DEEP_MESSAGE)
    if len(_serialize(blocks)) > RICH_HTML_MAX_LENGTH:  # canonical output cap (links gain rel=...)
        raise ContentError(TOO_LONG_MESSAGE)
    return blocks


def check_rich_input(raw):
    """Cheap checks on author input before any parsing: size and a tag-count
    bound, so pathological markup (e.g. 25,000 nested lists) fails fast.
    Raises ``ContentError``. Stored canonical HTML is not re-checked here (its
    size is capped by ``sanitize_rich_html``)."""
    if not isinstance(raw, str):
        raise ContentError(EMPTY_MESSAGE)
    if len(raw) > RICH_HTML_MAX_LENGTH:
        raise ContentError(TOO_LONG_MESSAGE)
    if raw.count('<') > RICH_HTML_MAX_TAGS:
        raise ContentError(TOO_COMPLEX_MESSAGE)


def sanitize_rich_html(raw):
    """Author HTML → canonical safe HTML and its text fallback.

    Raises ``ContentError`` when nothing readable is left, the structure is
    too deep or the canonical HTML exceeds ``RICH_HTML_MAX_LENGTH``. Callers
    run ``check_rich_input`` on author input first. Canonical output is a
    fixed point, so re-sanitizing stored HTML gives the same result.
    """
    blocks = _sanitized_tree(raw)
    return SanitizedRich(html=_serialize(blocks), text=text_fallback(blocks))


@dataclass(frozen=True)
class Rendered:
    subject: str
    text: str
    html: str
    template_version: str


# Generated inline styles (never author CSS). Palette follows accounts/templates/emails.
_GREEN = '#2c5530'
_FONT = "'Segoe UI',Tahoma,Geneva,Verdana,sans-serif"
EMAIL_STYLES = {
    'p': 'margin:0 0 16px;',
    'li>p': 'margin:0;',
    'blockquote>p': 'margin:0 0 8px;',
    'h2': f'color:{_GREEN};font-size:22px;line-height:1.3;margin:24px 0 12px;',
    'h3': f'color:{_GREEN};font-size:18px;line-height:1.3;margin:20px 0 10px;',
    'ul': 'margin:0 0 16px;padding-left:24px;',
    'ol': 'margin:0 0 16px;padding-left:24px;',
    'li>ul': 'margin:4px 0;padding-left:24px;',
    'li>ol': 'margin:4px 0;padding-left:24px;',
    'li': 'margin:6px 0;',
    'blockquote': (f'margin:16px 0;padding:12px 16px;border-left:4px solid {_GREEN};'
                   'background-color:#f8f9fa;color:#555555;'),
    'a': f'color:{_GREEN};text-decoration:underline;',
    'strong': f'color:{_GREEN};',
}

_EMAIL_DOCUMENT = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
    '<meta name="color-scheme" content="light"><title>{title}</title></head>'
    f'<body style="margin:0;padding:20px;background-color:#f4f4f4;color:#333333;font-family:{_FONT};'
    'font-size:15px;line-height:1.6;">'
    '<div style="max-width:600px;margin:0 auto;background-color:#ffffff;border-radius:10px;padding:32px;">'
    f'<div style="text-align:center;border-bottom:3px solid {_GREEN};padding-bottom:16px;margin-bottom:24px;">'
    f'<div style="color:{_GREEN};font-size:24px;font-weight:bold;">Pythagoras</div>'
    '<div style="color:#666666;font-size:13px;margin-top:4px;">The MSSN FUTA Digital Platform</div></div>'
    '<div style="overflow-wrap:anywhere;">{content}</div>'
    '<div style="text-align:center;margin-top:32px;padding-top:16px;border-top:1px solid #eeeeee;'
    'color:#888888;font-size:12px;">'
    '<div>MSSN FUTA - Muslim Students&#39; Society of Nigeria</div>'
    '<div>Federal University of Technology, Akure</div></div>'
    '</div></body></html>'
)


def email_html(subject, blocks):
    """The single HTML wrapper for every broadcast/automated email."""
    return _EMAIL_DOCUMENT.format(title=html.escape(subject, quote=True),
                                  content=_serialize(blocks, EMAIL_STYLES))


def render_content(subject, body_format, body='', body_html=None):
    """Subject, text part and HTML document exactly as preview shows, queue
    freezes and the worker sends. Rich content is re-sanitized here; plain
    text is escaped. Shared with automated messages (COMM-06)."""
    if body_format == 'rich':
        blocks = _sanitized_tree(body_html)
        text = text_fallback(blocks)
    else:
        text = normalize_body(body or '')
        # Our own escaped markup, still passed through the allowlist cleaner.
        blocks = _parse(_CLEANER.clean(plain_to_html(text)))
    return Rendered(subject=subject, text=text, html=email_html(subject, blocks),
                    template_version=TEMPLATE_VERSION)


def render(broadcast):
    """Rendered subject/text/HTML exactly as preview shows and queue freezes."""
    return render_content(broadcast.subject, broadcast.body_format, broadcast.body, broadcast.body_html)
