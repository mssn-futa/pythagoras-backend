"""Rich broadcast content: sanitizer, text fallback, email template, rich draft
API, preview/send equivalence and frozen snapshots (COMM-04)."""
import random
import re
from copy import deepcopy
from datetime import datetime, timezone as dt_timezone
from html.parser import HTMLParser
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, RefResolver

# Import the API module first (pre-existing circular import, see test_concurrency).
from dawah import views  # noqa: F401, I001
from dawah import broadcast_content as bc, broadcasts, examples, outbox
from dawah.broadcast_content import ContentError, sanitize_rich_html
from dawah.models import Broadcast, Delivery
from pythagoras.mail import memory

from .helpers import User
from .test_broadcasts import LIST, PAYLOAD_KEYS, BroadcastTestCase, detail, make_student
from .test_outbox import MAIL_SETTINGS, FakeTransport
from .test_schema import draft4

REL = 'rel="noopener noreferrer nofollow"'
ALLOWED = {'p', 'br', 'strong', 'em', 'h2', 'h3', 'ul', 'ol', 'li', 'blockquote', 'a'}


class _Inspector(HTMLParser):
    """Independent parse of sanitizer output: every tag/attribute it contains."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.comments = set(), [], []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attrs.extend((tag, name, value) for name, value in attrs)

    handle_startendtag = handle_starttag

    def handle_comment(self, data):
        self.comments.append(data)


def inspect(fragment):
    parser = _Inspector()
    parser.feed(fragment)
    parser.close()
    return parser


def rich_payload(**overrides):
    payload = {'subject': 'Weekly Da’wah summary', 'body_format': 'rich',
               'body_html': '<p><strong>Key lessons</strong> from <em>Friday</em></p>', 'audience': {'type': 'all'}}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- sanitizer

XSS_CORPUS = [
    '<script>alert(1)</script>safe',
    '<SCRIPT SRC=https://evil.example/x.js></SCRIPT>safe',
    '<img src=x onerror=alert(1)>safe',
    '<p onclick="alert(1)" onmouseover=alert(1)>safe</p>',
    '<svg onload=alert(1)><circle r="1"/></svg>safe',
    '<svg><script>alert(1)</script><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>safe',
    '<math><mtext><table><mglyph><style><img src=x onerror=alert(1)></style></mglyph></table></mtext></math>safe',
    '<iframe src="https://evil.example" srcdoc="<script>alert(1)</script>"></iframe>safe',
    '<object data="javascript:alert(1)"></object><embed src="javascript:alert(1)">safe',
    '<style>p{background:url(javascript:alert(1))}</style>safe',
    '<p style="background:expression(alert(1));color:red">safe</p>',
    '<div style="width: expression(alert(1))">safe</div>',
    '<link rel="stylesheet" href="https://evil.example/x.css"><meta http-equiv="refresh" content="0;url=javascript:alert(1)">safe',
    '<form action="https://evil.example"><input name="password"><button formaction="javascript:alert(1)">Go</button></form>safe',
    '<a href="javascript:alert(1)">safe</a>',
    '<a href="JaVaScRiPt:alert(1)">safe</a>',
    '<a href="jav&#x09;ascript:alert(1)">safe</a>',
    '<a href="jav&#x0A;ascript:alert(1)">safe</a>',
    '<a href="&#106;&#97;&#118;&#97;&#115;&#99;&#114;&#105;&#112;&#116;&#58;alert(1)">safe</a>',
    '<a href="&#x6A;avascript&colon;alert(1)">safe</a>',
    '<a href=" \x01javascript:alert(1)">safe</a>',
    '<a href="javascript&#58;alert(1)">safe</a>',
    '<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">safe</a>',
    '<a href="DATA:text/html,<script>alert(1)</script>">safe</a>',
    '<a href="vbscript:msgbox(1)">safe</a>',
    '<a href="file:///etc/passwd">safe</a>',
    '<a href="//evil.example/">safe</a>',
    '<a href="/relative">safe</a>',
    '<a href="https:\\\\evil.example">safe</a>',
    '<a href="https://good.example/" onclick="alert(1)" target="_top" style="color:red" class="x" id="y">safe</a>',
    '<a href="https://good.example/&quot; onmouseover=&quot;alert(1)">safe</a>',
    '<<script>script>alert(1)<</script>/script>safe',
    '<scr<script>ipt>alert(1)</script>safe',
    '<p>safe<!-- <script>alert(1)</script> --></p>',
    '<!--[if IE]><script>alert(1)</script><![endif]-->safe',
    '<!--[if gte mso 9]><xml><o:OfficeDocumentSettings/></xml><![endif]-->safe',
    '<![CDATA[<script>alert(1)</script>]]>safe',
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>">safe',
    '<template><script>alert(1)</script></template>safe',
    '<textarea><script>alert(1)</script></textarea>safe',
    '<title><script>alert(1)</script></title>safe',
    '<xmp><img src=x onerror=alert(1)></xmp>safe',
    '<select><option><img src=x onerror=alert(1)></option></select>safe',
    '<p><a href="https://good.example/"><a href="javascript:alert(1)">safe</a></a></p>',
    '<p title="x"><b title="</p><img src=x onerror=alert(1)>">safe</b></p>',
    '<form><math><mtext></form><form><mglyph><style></math><img src onerror=alert(1)>safe',
    '<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">safe',
    '<base href="javascript:alert(1)//"><a href="x">safe</a>',
    '<video><source onerror="alert(1)"></video><audio src=x onerror=alert(1)>safe',
    '<p>safe</p></p></li></ul></blockquote><script>alert(1)</script>',
    '<li><ul><p><strong><em><a href="https://good.example/">safe',
    '<details open ontoggle=alert(1)>safe</details>',
    '<isindex type=image src=1 onerror=alert(1)>safe',
    '<p>safe\x00<script>alert(1)</script></p>',
]
FORBIDDEN = re.compile(r'(?i)<\s*/?\s*(script|style|iframe|svg|math|img|form|input|object|embed|meta|link|base)\b'
                       r'|javascript:|vbscript:|data:|expression\(|\son\w+\s*=|style=|<!--')


class SanitizerSecurityTests(SimpleTestCase):
    def assert_safe(self, out):
        self.assertIsNone(FORBIDDEN.search(out), out)
        parsed = inspect(out)
        self.assertLessEqual(parsed.tags, ALLOWED, out)
        self.assertEqual(parsed.comments, [])
        for tag, name, value in parsed.attrs:
            self.assertEqual(tag, 'a', out)
            self.assertIn(name, {'href', 'rel'}, out)
            if name == 'href':
                self.assertRegex(value, r'^https?://[^\s/\\@]+/')
            else:
                self.assertEqual(value, 'noopener noreferrer nofollow')

    def test_xss_corpus_is_neutralized(self):
        accepted = 0
        for payload in XSS_CORPUS:
            with self.subTest(payload=payload):
                try:
                    result = sanitize_rich_html(payload)
                except ContentError:
                    continue  # nothing readable left (e.g. text swallowed by a removed raw-text element)
                accepted += 1
                self.assert_safe(result.html)
                self.assertIn('safe', result.text)
                self.assertEqual(sanitize_rich_html(result.html), result)  # idempotent
        self.assertGreater(accepted, len(XSS_CORPUS) - 8)

    def test_script_and_style_content_is_removed_not_shown(self):
        result = sanitize_rich_html('<p>a</p><script>alert(1)</script><style>p{}</style><iframe>inner</iframe><p>b</p>')
        self.assertEqual(result.html, '<p>a</p><p>b</p>')
        self.assertEqual(result.text, 'a\n\nb')

    def test_dangerous_links_are_unwrapped_and_text_kept(self):
        for href in ('javascript:alert(1)', 'data:text/html,x', 'vbscript:x', 'mailto:a@example.test',
                     'ftp://example.test/', '/path', '//example.test/', 'https://user:pw@example.test/',
                     'https://exa mple.test/', 'https://example.test/‮gnp.exe', 'https://example.test/​',
                     'https:example.test', 'https://', 'http://[::1', 'https://a<b>.test/', 'https://.test/',
                     'https://ex%41mple.test/', 'https://example.test:99999/',
                     'https://' + 'a' * 2000 + '.test/'):
            with self.subTest(href=href):
                result = sanitize_rich_html(f'<p>Go <a href="{href}">there</a></p>')
                self.assertEqual(result.html, '<p>Go there</p>')
                self.assertEqual(result.text, 'Go there')

    def test_escaped_text_stays_text(self):
        result = sanitize_rich_html('<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; 5 &gt; 3</p>')
        self.assertEqual(result.html, '<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; 5 &gt; 3</p>')
        self.assertEqual(result.text, '<script>alert(1)</script> & 5 > 3')

    def test_reparse_cannot_change_structure(self):
        """mXSS guard: our serialization escapes text/attributes, so an
        independent parse sees exactly the canonical tags (and nh3 agrees)."""
        import nh3
        for payload in XSS_CORPUS + ['<p>a<a href="https://e.test/?q=\'&quot;<>">l</a></p>']:
            try:
                out = sanitize_rich_html(payload).html
            except ContentError:
                continue
            again = nh3.clean(out, tags=ALLOWED, attributes={'a': {'href', 'rel'}}, link_rel=None,
                              url_schemes={'http', 'https'}).replace('&nbsp;', '\xa0')
            self.assertEqual(again, out)


class SanitizerFormattingTests(SimpleTestCase):
    def test_allowed_formatting_round_trips_unchanged(self):
        canonical = (
            '<h2>Weekly summary</h2><h3>Friday</h3>'
            '<p>Plain <strong>bold</strong> <em>italic</em> <strong><em>both</em></strong><br>next line</p>'
            '<ul><li><p>One</p></li><li><p>Two</p><ul><li><p>Nested</p></li></ul></li></ul>'
            '<ol><li><p>First</p></li><li><p>Second</p></li></ol>'
            '<blockquote><p>Quoted <a href="https://example.test/dawah/content/8" ' + REL + '>link</a></p></blockquote>'
        )
        self.assertEqual(sanitize_rich_html(canonical).html, canonical)

    def test_editor_output_gets_canonical_link_rel_and_no_target(self):
        result = sanitize_rich_html('<p><a target="_blank" rel="noopener" class="x" '
                                    'href="https://example.test/a">x</a></p>')
        self.assertEqual(result.html, f'<p><a href="https://example.test/a" {REL}>x</a></p>')

    def test_link_normalization(self):
        cases = {
            'https://Example.TEST': 'https://example.test/',
            'HTTP://example.test/Path?Q=1#Frag': 'http://example.test/Path?Q=1#Frag',
            '  https://example.test/a  ': 'https://example.test/a',
            'https://example.test:443/x': 'https://example.test/x',
            'http://example.test:80/': 'http://example.test/',
            'https://example.test:8443/': 'https://example.test:8443/',
            'https://bücher.example/ä?q=ü': 'https://xn--bcher-kva.example/%C3%A4?q=%C3%BC',
            'https://example.test/a%20b': 'https://example.test/a%20b',
            'https://example.test/"x"<y>': 'https://example.test/%22x%22%3Cy%3E',
            'https://[2001:db8::1]/': 'https://[2001:db8::1]/',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(bc.normalize_link(raw), expected)
                html = sanitize_rich_html(f'<p><a href="{raw.replace(chr(34), "&quot;")}">t</a></p>').html
                self.assertEqual(inspect(html).attrs[0], ('a', 'href', expected))

    def test_inline_runs_lists_and_blocks_are_normalized(self):
        cases = {
            'loose text <b>bold tag dropped</b>': '<p>loose text bold tag dropped</p>',
            '<li>a</li><li>b</li>': '<ul><li><p>a</p></li><li><p>b</p></li></ul>',
            '<ul>x<li>a</li></ul>': '<ul><li><p>x</p></li><li><p>a</p></li></ul>',
            '<ol start="5" type="a" reversed><li>x</li></ol>': '<ol><li><p>x</p></li></ol>',
            '<h1>Title</h1><h4>Sub</h4><h6>Small</h6>': '<h2>Title</h2><h3>Sub</h3><h3>Small</h3>',
            '<div>a</div><section>b<hr>c</section>': '<p>a</p><p>b</p><p>c</p>',
            '<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>': '<p>a<br>b</p><p>c</p>',
            '<pre>line 1\nline 2</pre>': '<p>line 1<br>line 2</p>',
            '<strong><p>x</p><p>y</p></strong>': '<p><strong>x<br>y</strong></p>',
            '<p>  spaced \n\t out  <br>  next  </p>': '<p>spaced out<br>next</p>',
            '<p><strong> </strong>a<em></em></p>': '<p>a</p>',
            '<p><br><br>a<br></p>': '<p>a</p>',
            '<p>a</p><p></p><p>&nbsp;</p><p><br></p><ul><li></li></ul><blockquote> </blockquote>': '<p>a</p>',
            '<span style="font-weight:bold">x</span><font color=red>y</font>': '<p>xy</p>',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_rich_html(raw).html, expected)

    def test_empty_after_sanitize_is_rejected(self):
        for raw in ('', '   ', '<p></p>', '<p>&nbsp;</p>', '<p><br></p>', '<br><br>', '<p>​</p>',
                    '<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '<!-- only a comment -->',
                    '<style>p{}</style>', '<ul><li></li></ul>', '<a href="https://example.test/"></a>',
                    '<iframe>text inside</iframe>', '<svg><text>x</text></svg>', None):
            with self.subTest(raw=raw), self.assertRaises(ContentError) as caught:
                sanitize_rich_html(raw)
            self.assertEqual(str(caught.exception), bc.EMPTY_MESSAGE)

    def test_nesting_depth_is_bounded(self):
        ok = '<blockquote>' * 15 + '<p>deep</p>' + '</blockquote>' * 15
        self.assertIn('deep', sanitize_rich_html(ok).text)
        for raw in ('<blockquote>' * 30 + 'x', '<ul><li>' * 15 + 'x', '<strong>' * 200 + 'x'):
            with self.subTest(raw=raw[:30]), self.assertRaises(ContentError) as caught:
                sanitize_rich_html(raw)
            self.assertEqual(str(caught.exception), bc.TOO_DEEP_MESSAGE)

    def test_idempotent_on_random_input(self):
        tokens = ['<p>', '</p>', '<b>', '</b>', '<strong>', '</strong>', '<em>', '</em>', '<ul>', '</ul>', '<ol>',
                  '</ol>', '<li>', '</li>', '<blockquote>', '</blockquote>', '<h1>', '<h2>', '</h2>', '<h3>',
                  '<div>', '</div>', '<br>', '<hr>', '<a href="https://e.test/p">', '<a href="javascript:x">',
                  '</a>', '<pre>', '</pre>', '<table>', '<td>', '<tr>', ' ', '\n', 'x', 'y z', '&amp;', '&nbsp;',
                  '<script>', '</script>', '<!--', '-->', '<img src=x onerror=1>', '"', "'", '<', '>', '&',
                  '<svg>', '<math>', '<style>', '<noscript>', '<template>', '<textarea>', '<select>', '<xmp>']
        rng = random.Random(20261111)
        checked = 0
        for _ in range(3000):
            raw = ''.join(rng.choice(tokens) for _ in range(rng.randint(1, 40)))
            try:
                first = sanitize_rich_html(raw)
            except ContentError:
                continue
            checked += 1
            self.assertEqual(sanitize_rich_html(first.html), first, raw)
            self.assertLessEqual(inspect(first.html).tags, ALLOWED)
        self.assertGreater(checked, 500)

    def test_large_input_is_bounded_in_time(self):
        import time
        raw = '<p>Text <strong>bold</strong> <a href="https://example.test/x">link</a></p>' * 1300
        self.assertLessEqual(len(raw), bc.RICH_HTML_MAX_LENGTH)
        started = time.monotonic()
        result = sanitize_rich_html(raw)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.html.count('<p>'), 1300)


class TextFallbackTests(SimpleTestCase):
    """Frozen COMM-05 format: blocks separated by a blank line; list items
    "- " / "1. "; links "text (href)" (href omitted when text equals URL);
    <br> → newline."""

    def text(self, raw):
        return sanitize_rich_html(raw).text

    def test_examples(self):
        cases = {
            '<p>One</p><p>Two</p>': 'One\n\nTwo',
            '<h2>Title</h2><p>Body</p>': 'Title\n\nBody',
            '<p>line<br>next</p>': 'line\nnext',
            '<ul><li><p>a</p></li><li><p>b</p></li></ul>': '- a\n- b',
            '<ol><li><p>a</p></li><li><p>b</p></li></ol>': '1. a\n2. b',
            '<p>See <a href="https://example.test/x">the notes</a>.</p>': 'See the notes (https://example.test/x).',
            '<p><a href="https://example.test/x">https://example.test/x</a></p>': 'https://example.test/x',
            '<p><a href="https://example.test">https://example.test/</a></p>': 'https://example.test/',
            '<p><strong>Bold</strong> and <em>it</em></p>': 'Bold and it',
            '<blockquote><p>q1</p><p>q2</p></blockquote><p>after</p>': 'q1\n\nq2\n\nafter',
            '<ul><li><p>a</p><ul><li><p>n</p></li></ul></li></ul>': '- a\n- n',
            '<p>Intro</p><ul><li><p>x</p></li></ul><p>Outro</p>': 'Intro\n\n- x\n\nOutro',
            '<p>5 &lt; 6 &amp; "quoted"</p>': '5 < 6 & "quoted"',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.text(raw), expected)

    def test_matches_comm05_fixture(self):
        raw = ('<p><strong>Key lessons</strong></p><ul><li><p>Read the '
               '<a href="https://example.test/dawah/content/8">recording</a>.</p></li></ul>')
        self.assertEqual(self.text(raw), 'Key lessons\n\n- Read the recording (https://example.test/dawah/content/8).')


class TemplateTests(SimpleTestCase):
    def test_email_document_is_safe_and_styled_by_us(self):
        rendered = bc.render_content('Notes <b>&</b>', 'rich', body_html='<h2>T</h2><p><a href="https://e.test/">x</a></p>'
                                     '<blockquote><p>q</p></blockquote><p onclick="x" style="color:red">p</p>')
        self.assertTrue(rendered.html.startswith('<!DOCTYPE html><html lang="en">'))
        self.assertIn('<title>Notes &lt;b&gt;&amp;&lt;/b&gt;</title>', rendered.html)
        self.assertIn(f'<h2 style="{bc.EMAIL_STYLES["h2"]}">T</h2>', rendered.html)
        self.assertIn(f'<a href="https://e.test/" {REL} style="{bc.EMAIL_STYLES["a"]}">x</a>', rendered.html)
        self.assertIn(f'<blockquote style="{bc.EMAIL_STYLES["blockquote"]}"><p style="'
                      f'{bc.EMAIL_STYLES["blockquote>p"]}">q</p></blockquote>', rendered.html)
        self.assertNotIn('color:red', rendered.html)
        self.assertNotIn('onclick', rendered.html)
        self.assertNotIn('<script', rendered.html)
        self.assertNotIn('<style', rendered.html)  # inline styles only, no stylesheet
        self.assertEqual(rendered.text, 'T\n\nx (https://e.test/)\n\nq\n\np')
        self.assertEqual(rendered.template_version, bc.TEMPLATE_VERSION)

    def test_plain_content_is_escaped_in_the_same_template(self):
        rendered = bc.render_content('S', 'plain', 'Hi <b>all</b> & "you"\r\nline\n\n\nnext  \n')
        self.assertEqual(rendered.text, 'Hi <b>all</b> & "you"\nline\n\n\nnext')
        self.assertIn('<p style="margin:0 0 16px;">Hi &lt;b&gt;all&lt;/b&gt; &amp; "you"<br>line</p>'
                      '<p style="margin:0 0 16px;">next</p>', rendered.html)
        self.assertEqual(inspect(rendered.html).tags & {'b', 'script'}, set())

    def test_rich_rendering_resanitizes(self):
        rendered = bc.render_content('S', 'rich', body_html='<p>ok<script>alert(1)</script><img src=x onerror=1></p>')
        self.assertIn('>ok</p>', rendered.html)
        self.assertNotIn('alert', rendered.html)
        with self.assertRaises(ContentError):
            bc.render_content('S', 'rich', body_html='<script>alert(1)</script>')


# ---------------------------------------------------------------- API

class RichDraftApiTests(BroadcastTestCase):
    def fields(self, response):
        return self.error(response, 400, 'VALIDATION_ERROR')['fields']

    def create_rich(self, **overrides):
        return self.data(self.client.post(LIST, rich_payload(**overrides), format='json'), 201)

    def patch(self, pk, body):
        return self.client.patch(detail(pk), body, format='json')

    def test_create_rich_returns_canonical_html_and_generated_body(self):
        data = self.create_rich(body_html='<p>Read <a href="https://Example.test/notes" target="_blank" '
                                          'onclick="x()">the notes</a><script>alert(1)</script></p>'
                                          '<ul><li>Patience</li><li><b>Gratitude</b></li></ul>')
        self.assertEqual(set(data), PAYLOAD_KEYS)
        self.assertEqual(data['body_format'], 'rich')
        self.assertEqual(data['body_html'], f'<p>Read <a href="https://example.test/notes" {REL}>the notes</a></p>'
                                            '<ul><li><p>Patience</p></li><li><p>Gratitude</p></li></ul>')
        self.assertEqual(data['body'], 'Read the notes (https://example.test/notes)\n\n- Patience\n- Gratitude')
        self.assertEqual((data['state'], data['version']), ('draft', 1))
        stored = Broadcast.objects.get(pk=data['id'])
        self.assertEqual((stored.body_html, stored.body), (data['body_html'], data['body']))
        # Reload returns the same canonical HTML.
        self.assertEqual(self.data(self.client.get(detail(data['id'])))['body_html'], data['body_html'])

    def test_rich_request_rules(self):
        count = Broadcast.objects.count()
        self.assertEqual(self.fields(self.client.post(LIST, rich_payload(body='client text'), format='json')),
                         {'body': [RICH_BODY_SENT]})
        payload = rich_payload()
        del payload['body_html']
        self.assertEqual(self.fields(self.client.post(LIST, payload, format='json')), {'body_html': ['Enter the message.']})
        self.assertEqual(set(self.fields(self.client.post(LIST, {'body_format': 'rich'}, format='json'))),
                         {'subject', 'body_html', 'audience'})
        for html in ('<script>alert(1)</script>', '<p> </p>', '<img src=x onerror=alert(1)>'):
            with self.subTest(html=html):
                self.assertEqual(self.fields(self.client.post(LIST, rich_payload(body_html=html), format='json')),
                                 {'body_html': [bc.EMPTY_MESSAGE]})
        self.assertEqual(self.fields(self.client.post(LIST, rich_payload(body_html=''), format='json')),
                         {'body_html': ['Enter the message.']})
        too_long = '<p>' + 'x' * bc.RICH_HTML_MAX_LENGTH + '</p>'
        self.assertEqual(self.fields(self.client.post(LIST, rich_payload(body_html=too_long), format='json')),
                         {'body_html': [bc.TOO_LONG_MESSAGE]})
        for value in (12, ['<p>x</p>'], None, {'html': 'x'}):
            with self.subTest(value=value):
                self.assertIn('body_html', self.fields(self.client.post(LIST, rich_payload(body_html=value),
                                                                        format='json')))
        self.assertIn('body_html', self.fields(self.client.post(
            LIST, rich_payload(body_html='<blockquote>' * 30 + 'x'), format='json')))
        self.assertIn('subject', self.fields(self.client.post(LIST, rich_payload(subject='a\r\nBcc: x@example.test'),
                                                              format='json')))
        self.assertEqual(Broadcast.objects.count(), count)

    def test_largest_allowed_input_is_accepted(self):
        block = '<p><strong><em>' + 'x' * 20 + '</em></strong></p>'  # mostly markup: text stays under the body limit
        html = block * (bc.RICH_HTML_MAX_LENGTH // len(block) - 1)
        html += '<p>' + 'y' * (bc.RICH_HTML_MAX_LENGTH - len(html) - 7) + '</p>'
        self.assertEqual(len(html), bc.RICH_HTML_MAX_LENGTH)
        created = self.create_rich(body_html=html)
        self.assertEqual(created['body_html'], html)
        # The generated text fallback shares the plain-text limit.
        too_much_text = '<p>' + 'x' * (bc.BODY_MAX_LENGTH + 1) + '</p>'
        self.assertEqual(self.fields(self.client.post(LIST, rich_payload(body_html=too_much_text), format='json')),
                         {'body_html': [f'The message text must be {bc.BODY_MAX_LENGTH:,} characters or fewer.']})

    def test_patch_rich_draft(self):
        created = self.create_rich()
        pk = created['id']
        data = self.data(self.patch(pk, {'body_html': '<h2>New</h2><p onclick=x>text</p>', 'version': 1}))
        self.assertEqual((data['body_format'], data['body_html'], data['body'], data['version']),
                         ('rich', '<h2>New</h2><p>text</p>', 'New\n\ntext', 2))
        data = self.data(self.patch(pk, {'subject': 'Renamed', 'version': 2}))
        self.assertEqual((data['subject'], data['body_html'], data['version']), ('Renamed', '<h2>New</h2><p>text</p>', 3))
        self.assertEqual(self.fields(self.patch(pk, {'body': 'plain now?', 'version': 3})), {'body': [RICH_BODY_SENT]})
        self.assertEqual(self.fields(self.patch(pk, {'body_html': '<p><br></p>', 'version': 3})),
                         {'body_html': [bc.EMPTY_MESSAGE]})
        self.assertEqual(Broadcast.objects.get(pk=pk).version, 3)

    def test_convert_between_plain_and_rich(self):
        plain = self.create()
        pk = plain['id']
        self.assertEqual(self.fields(self.patch(pk, {'body_format': 'rich', 'version': 1})),
                         {'body_html': ['Enter the message.']})
        self.assertEqual(self.fields(self.patch(pk, {'body_html': '<p>x</p>', 'version': 1})),
                         {'body_html': [HTML_FOR_PLAIN]})
        rich = self.data(self.patch(pk, {'body_format': 'rich', 'body_html': '<p><em>Now</em> rich</p>', 'version': 1}))
        self.assertEqual((rich['body_format'], rich['body_html'], rich['body']), ('rich', '<p><em>Now</em> rich</p>', 'Now rich'))
        back = self.data(self.patch(pk, {'body_format': 'plain', 'body': 'Plain again', 'version': 2}))
        self.assertEqual((back['body_format'], back['body_html'], back['body']), ('plain', None, 'Plain again'))
        self.data(self.patch(pk, {'body_format': 'rich', 'body_html': '<ul><li>kept</li></ul>', 'version': 3}))
        kept = self.data(self.patch(pk, {'body_format': 'plain', 'version': 4}))  # keeps the text fallback
        self.assertEqual((kept['body_format'], kept['body_html'], kept['body']), ('plain', None, '- kept'))

    def test_plain_drafts_are_unchanged(self):
        data = self.create(body='Hello <b>all</b>\r\n\r\nBye')
        self.assertEqual((data['body_format'], data['body_html'], data['body']), ('plain', None, 'Hello <b>all</b>\n\nBye'))
        data = self.data(self.patch(data['id'], {'body': 'Edited <i>x</i>', 'version': 1}))
        self.assertEqual((data['body_format'], data['body_html'], data['body']), ('plain', None, 'Edited <i>x</i>'))
        preview = self.data(self.client.post(detail(data['id'], 'preview/'), {'version': 2}, format='json'))
        self.assertEqual(preview['rendered_text'], 'Edited <i>x</i>')
        self.assertIn('Edited &lt;i&gt;x&lt;/i&gt;', preview['rendered_html'])

    def test_rich_preview_is_the_template_output_and_side_effect_free(self):
        make_student()
        created = self.create_rich(subject='Notes <today>', body_html='<h2>Hi</h2><p>See <a href="https://e.test/">it</a></p>')
        before = Broadcast.objects.get(pk=created['id'])
        data = self.data(self.client.post(detail(created['id'], 'preview/'), {'version': 1}, format='json'))
        self.assertEqual(set(data), {'subject', 'body', 'body_format', 'body_html', 'rendered_text', 'rendered_html',
                                     'audience_count'})
        expected = bc.render_content('Notes <today>', 'rich', body_html=created['body_html'])
        self.assertEqual((data['rendered_html'], data['rendered_text']), (expected.html, expected.text))
        self.assertEqual(data['rendered_text'], created['body'])
        self.assertEqual((data['subject'], data['body'], data['body_format'], data['body_html']),
                         ('Notes <today>', created['body'], 'rich', created['body_html']))
        after = Broadcast.objects.get(pk=created['id'])
        self.assertEqual((after.version, after.updated_at, after.snapshot_html), (before.version, before.updated_at, ''))
        self.assertEqual((Delivery.objects.count(), memory.outbox), (0, []))

    def test_preview_and_queue_resanitize_stored_html(self):
        """A rich draft changed outside the API is sanitized again before
        preview and the queue snapshot."""
        make_student()
        created = self.create_rich()
        Broadcast.objects.filter(pk=created['id']).update(
            body_html='<p>edited<script>alert(1)</script><img src=x onerror=alert(1)>'
                      '<a href="javascript:alert(1)">bad</a></p>')
        preview = self.data(self.client.post(detail(created['id'], 'preview/'), {'version': 1}, format='json'))
        self.assertIn('>editedbad</p>', preview['rendered_html'])
        self.assertEqual(preview['rendered_text'], 'editedbad')
        for needle in ('script', 'onerror', 'javascript'):
            self.assertNotIn(needle, preview['rendered_html'])
        self.data(self.queue(created['id'], 1), 202)
        queued = Broadcast.objects.get(pk=created['id'])
        self.assertEqual((queued.snapshot_html, queued.snapshot_text), (preview['rendered_html'], 'editedbad'))

    def test_preview_and_queue_reject_stored_html_that_sanitizes_to_nothing(self):
        make_student()
        created = self.create_rich()
        Broadcast.objects.filter(pk=created['id']).update(body_html='<script>alert(1)</script>')
        for suffix in ('preview/', 'queue/'):
            with self.subTest(suffix=suffix):
                response = self.client.post(detail(created['id'], suffix), {'version': 1}, format='json')
                self.assertEqual(self.fields(response), {'body_html': [bc.EMPTY_MESSAGE]})
        self.assertEqual((Broadcast.objects.get(pk=created['id']).state, Delivery.objects.count()), ('draft', 0))

    def test_queued_rich_broadcast_is_immutable(self):
        make_student()
        created = self.create_rich()
        self.data(self.queue(created['id'], 1), 202)
        response = self.patch(created['id'], {'body_html': '<p>changed</p>', 'version': 1})
        self.error(response, 409, 'BROADCAST_ALREADY_QUEUED')
        self.assertEqual(Broadcast.objects.get(pk=created['id']).body_html, created['body_html'])


RICH_BODY_SENT = 'Rich drafts do not accept body; the server generates the text fallback from body_html.'
HTML_FOR_PLAIN = 'body_html applies only to rich drafts; send body_format "rich" with it.'


@MAIL_SETTINGS
class PreviewSendEquivalenceTests(BroadcastTestCase):
    def worker(self, transport):
        return outbox.OutboxWorker(outbox.OutboxConfig(send_timeout=30), transport=transport, limiter=None)

    def preview_queue_send(self, payload):
        User.objects.filter(role='student').update(is_active=False)
        student = make_student()
        created = self.data(self.client.post(LIST, payload, format='json'), 201)
        preview = self.data(self.client.post(detail(created['id'], 'preview/'), {'version': 1}, format='json'))
        self.data(self.queue(created['id'], 1), 202)
        transport = FakeTransport()
        self.assertEqual(self.worker(transport).run_once().outcomes, {'accepted': 1})
        (message,) = transport.calls
        self.assertEqual(message.to, student.email)
        return preview, message, Broadcast.objects.get(pk=created['id'])

    def test_worker_payload_equals_preview_for_rich_and_plain(self):
        for payload in (rich_payload(body_html='<h2>Week</h2><p>Read <a href="https://e.test/r">this</a></p>'
                                               '<ol><li>one</li></ol><blockquote>q</blockquote>'),
                        {'subject': 'Plain <x>', 'body': 'Hi <b>all</b>\n\nBye', 'audience': {'type': 'all'}}):
            with self.subTest(format=payload.get('body_format', 'plain')):
                preview, message, broadcast = self.preview_queue_send(payload)
                self.assertEqual((message.subject, message.html, message.text),
                                 (preview['subject'], preview['rendered_html'], preview['rendered_text']))
                self.assertEqual((broadcast.snapshot_html, broadcast.snapshot_text, broadcast.snapshot_template_version),
                                 (preview['rendered_html'], preview['rendered_text'], bc.TEMPLATE_VERSION))
                delivery = Delivery.objects.get(broadcast=broadcast)
                self.assertEqual((delivery.request_snapshot['html'], delivery.request_snapshot['text']),
                                 (preview['rendered_html'], preview['rendered_text']))

    def test_queued_content_survives_template_change(self):
        make_student()
        created = self.data(self.client.post(LIST, rich_payload(), format='json'), 201)
        self.data(self.queue(created['id'], 1), 202)
        frozen = Broadcast.objects.get(pk=created['id'])
        frozen_html, frozen_text = frozen.snapshot_html, frozen.snapshot_text
        self.assertEqual(frozen.snapshot_template_version, 'email-v2')
        styles = {**bc.EMAIL_STYLES, 'p': 'margin:0;color:#ff0000;'}
        with mock.patch.object(bc, 'TEMPLATE_VERSION', 'email-v3'), mock.patch.object(bc, 'EMAIL_STYLES', styles):
            # The new template is live (a new draft previews with it) ...
            other = self.data(self.client.post(LIST, rich_payload(), format='json'), 201)
            new_preview = self.data(self.client.post(detail(other['id'], 'preview/'), {'version': 1}, format='json'))
            self.assertIn('color:#ff0000', new_preview['rendered_html'])
            # ... but the queued broadcast sends what it froze.
            transport = FakeTransport()
            self.worker(transport).run_once()
        self.assertTrue(transport.calls)
        for message in transport.calls:
            self.assertEqual((message.html, message.text), (frozen_html, frozen_text))
        frozen.refresh_from_db()
        self.assertEqual((frozen.snapshot_html, frozen.snapshot_template_version), (frozen_html, 'email-v2'))


class RichSchemaContractTests(BroadcastTestCase):
    """Frozen COMM-05 shapes: rich requests/responses and examples match the
    OpenAPI document."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.json_schema = draft4(deepcopy(SchemaGenerator().get_schema(public=True)))

    def validate(self, path, method, status, response):
        self.assertEqual(str(response.status_code), status, response.content)
        schema = self.json_schema['paths'][path][method]['responses'][status]['content']['application/json']['schema']
        Draft4Validator(schema, resolver=RefResolver.from_schema(self.json_schema)).validate(response.json())

    def test_rich_responses_match_schema(self):
        make_student()
        base = '/dawah/broadcasts/{broadcast_id}/'
        created = self.client.post(LIST, rich_payload(), format='json')
        self.validate('/dawah/broadcasts/', 'post', '201', created)
        pk = created.json()['data']['id']
        self.validate(base, 'patch', '200', self.client.patch(detail(pk), {'body_html': '<p>x</p>', 'version': 1},
                                                              format='json'))
        self.validate(base + 'preview/', 'post', '200',
                      self.client.post(detail(pk, 'preview/'), {'version': 2}, format='json'))
        self.validate(base + 'queue/', 'post', '202', self.client.post(detail(pk, 'queue/'), {'version': 2}, format='json'))

    def test_rich_examples_match_schema_and_code(self):
        components = self.json_schema['components']['schemas']
        resolver = RefResolver.from_schema(self.json_schema)
        for name, value in (('Broadcast', examples.BROADCAST_RICH_DRAFT), ('Preview', examples.BROADCAST_RICH_PREVIEW),
                            ('Preview', examples.BROADCAST_PREVIEW)):
            Draft4Validator(components[name], resolver=resolver).validate(value)
        for value in (examples.BROADCAST_RICH_CREATE, examples.BROADCAST_CREATE):
            Draft4Validator(components['BroadcastWrite'], resolver=resolver).validate(value)
        for value in (examples.BROADCAST_RICH_PATCH, examples.BROADCAST_PATCH):
            Draft4Validator(components['PatchedBroadcastWrite'], resolver=resolver).validate(value)
        self.assertEqual(components['BroadcastWrite']['properties']['body_format']['pattern'], '^(plain|rich)$')
        created = self.data(self.client.post(LIST, examples.BROADCAST_RICH_CREATE, format='json'), 201)
        self.assertEqual((created['body_html'], created['body']),
                         (examples.BROADCAST_RICH_DRAFT['body_html'], examples.BROADCAST_RICH_DRAFT['body']))
        self.assertNotIn('script', created['body_html'])
        self.assertNotIn('onclick', created['body_html'])


# ---------------------------------------------------------------- migration

class RichFormatMigrationTests(TransactionTestCase):
    BEFORE = [('dawah', '0008_delivery_attempt_evidence')]
    AFTER = [('dawah', '0009_rich_body_format_constraint')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_rows_become_plain_and_keep_snapshots(self):
        old = self.migrate(self.BEFORE)
        OldBroadcast = old.get_model('dawah', 'Broadcast')
        queued_at = datetime(2026, 11, 1, 9, tzinfo=dt_timezone.utc)
        draft = OldBroadcast.objects.create(subject='Draft', body='Plain <b>body</b>', audience_type='all')
        odd = OldBroadcast.objects.create(subject='Odd', body='text', body_format='rich', body_html=None,
                                          audience_type='all')
        stray = OldBroadcast.objects.create(subject='Stray', body='text', body_format='plain', body_html='<p>x</p>',
                                            audience_type='all')
        queued = OldBroadcast.objects.create(
            subject='Queued', body='Sent text', audience_type='all', state='completed', queued_at=queued_at,
            recipient_count=3, snapshot_subject='Queued', snapshot_text='Sent text', snapshot_html='<p>Sent text</p>',
            snapshot_template_version='plain-v1')

        new = self.migrate(self.AFTER)
        Migrated = new.get_model('dawah', 'Broadcast')
        rows = {row.pk: row for row in Migrated.objects.all()}
        self.assertEqual({(row.body_format, row.body_html) for row in rows.values()}, {('plain', None)})
        self.assertEqual(rows[draft.pk].body, 'Plain <b>body</b>')
        self.assertEqual((rows[odd.pk].body, rows[stray.pk].body), ('text', 'text'))
        kept = rows[queued.pk]
        self.assertEqual((kept.state, kept.snapshot_text, kept.snapshot_html, kept.snapshot_template_version,
                          kept.recipient_count, kept.queued_at),
                         ('completed', 'Sent text', '<p>Sent text</p>', 'plain-v1', 3, queued_at))
        for values in ({'body_format': 'plain', 'body_html': '<p>x</p>'}, {'body_format': 'rich', 'body_html': None},
                       {'body_format': 'rich', 'body_html': ''}, {'body_format': 'markdown', 'body_html': None}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                Migrated.objects.filter(pk=draft.pk).update(**values)
        Migrated.objects.filter(pk=draft.pk).update(body_format='rich', body_html='<p>ok</p>')


class QueueServiceTests(TestCase):
    """Service-level: rich queue snapshots freeze the re-sanitized rendering."""

    def test_service_queue_freezes_rendering(self):
        admin = User.objects.create_user(email='svc-admin@example.test', password=None, role='admin')
        User.objects.create_user(email='svc-student@example.test', password=None, role='student')
        canonical = sanitize_rich_html('<p>Hello <strong>you</strong></p>')
        draft = Broadcast.objects.create(created_by=admin, subject='S', body=canonical.text, body_format='rich',
                                         body_html=canonical.html, audience_type='all')
        broadcast, replayed = broadcasts.queue(draft.pk, 1, admin)
        rendered = bc.render(broadcast)
        self.assertFalse(replayed)
        self.assertEqual((broadcast.snapshot_subject, broadcast.snapshot_text, broadcast.snapshot_html),
                         (rendered.subject, 'Hello you', rendered.html))
