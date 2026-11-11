"""Bounded PDF / DOCX / image validators (no storage, no database)."""
import io
import zipfile
from unittest import mock

from django.test import SimpleTestCase

from dawah import documents, images, uploads
from dawah.media import MediaRejected

from . import files
from .files import DOCX_TYPE, OFFICE_REL


def rejected(test, func, *args):
    with test.assertRaises(MediaRejected) as caught:
        func(*args)
    return caught.exception.code


class PdfValidationTests(SimpleTestCase):
    def check(self, data):
        documents.validate_pdf(io.BytesIO(data), len(data))

    def code(self, data):
        return rejected(self, documents.validate_pdf, io.BytesIO(data), len(data))

    def test_valid_classic_and_xref_stream_pdfs(self):
        self.check(files.pdf_bytes())
        self.check(files.pdf_bytes(pages=3, header=b'%PDF-1.7'))
        self.check(files.pdf_bytes(header=b'%PDF-2.0'))
        self.check(files.pdf_bytes(xref_stream=True))
        self.check(files.pdf_bytes() + b'\r\n')  # trailing EOL after %%EOF

    def test_malformed_pdfs_are_rejected(self):
        valid = files.pdf_bytes()
        xref_at = int(valid.rsplit(b'startxref\n', 1)[1].split(b'\n', 1)[0])
        cases = {
            'no header': valid[9:],
            'junk before header': b'junk' + valid,
            'unknown version': valid.replace(b'%PDF-1.4', b'%PDF-3.1', 1),
            'no eof marker': valid.replace(b'%%EOF', b'%%EOX'),
            'no startxref': valid.replace(b'startxref', b'startxrex'),
            'startxref past end': valid.replace(b'startxref\n%d' % xref_at, b'startxref\n%d' % (len(valid) * 2)),
            'startxref to nowhere': valid.replace(b'startxref\n%d' % xref_at, b'startxref\n%d' % 20),
            'trailer without root': valid.replace(b'/Root 1 0 R', b'/Info 1 0 R'),
            'truncated': valid[:len(valid) // 2],
            'zip renamed to pdf': files.docx_bytes(),
            'html': b'<html><script>alert(1)</script></html>',
            'empty-ish header only': b'%PDF-1.4\n%%EOF\n',
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertEqual(self.code(data), 'INVALID_PDF')
        self.assertEqual(self.code(files.pdf_bytes(xref_stream=True).replace(b'/Type /XRef', b'/Type /XXef')),
                         'INVALID_PDF')
        self.assertEqual(rejected(self, documents.validate_pdf, io.BytesIO(b''), 0), 'EMPTY_FILE')

    def test_parser_surprises_are_rejections(self):
        class Exploding(io.BytesIO):
            def read(self, *args):
                raise RuntimeError('boom')
        self.assertEqual(rejected(self, documents.validate_pdf, Exploding(b'x'), 10), 'INVALID_PDF')


class DocxValidationTests(SimpleTestCase):
    def check(self, data):
        documents.validate_docx(io.BytesIO(data), len(data))

    def code(self, data):
        return rejected(self, documents.validate_docx, io.BytesIO(data), len(data))

    def test_valid_docx_with_hyperlink(self):
        self.check(files.docx_bytes())
        self.check(files.docx_bytes(document_rels=[(f'{OFFICE_REL}/hyperlink', 'https://example.test/',
                                                    ' TargetMode="External"')],
                                    extra={'word/media/image1.png': files.image_bytes()}))

    def test_required_parts(self):
        self.assertEqual(self.code(files.docx_bytes(drop=['[Content_Types].xml'])), 'INVALID_DOCX')
        self.assertEqual(self.code(files.docx_bytes(drop=['word/document.xml'])), 'INVALID_DOCX')
        self.assertEqual(self.code(files.zip_bytes({'hello.txt': b'hi'})), 'INVALID_DOCX')
        # A template or another OOXML kind renamed to .docx is not a Word document.
        template = 'application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml'
        self.assertEqual(self.code(files.docx_bytes(main=template)), 'INVALID_DOCX')
        self.assertEqual(self.code(files.pdf_bytes()), 'INVALID_DOCX')
        self.assertEqual(self.code(b'PK\x03\x04' + b'\x00' * 100), 'INVALID_DOCX')

    def test_zip_bomb_entry_size_and_ratio(self):
        # 60 MiB of zeros compresses to ~60 KiB: over the per-entry and ratio limits.
        bomb = files.docx_bytes(extra={'word/media/bomb.bin': b'\x00' * (60 * 1024 * 1024)})
        self.assertLess(len(bomb), documents.DOCUMENT_MAX_BYTES)
        self.assertEqual(self.code(bomb), 'DOCX_LIMITS_EXCEEDED')
        # 2 MiB of zeros: under the per-entry cap, but a ~1000:1 ratio.
        ratio = files.docx_bytes(extra={'word/media/zeros.bin': b'\x00' * (2 * 1024 * 1024)})
        self.assertEqual(self.code(ratio), 'DOCX_LIMITS_EXCEEDED')

    def test_total_uncompressed_limit(self):
        noise = bytes(range(256)) * 4096  # ~1 MiB, ratio well below the limit (stored)
        entries = {f'word/media/n{i}.bin': noise for i in range(3)}
        data = files.docx_bytes(extra=entries)
        self.check(data)
        with mock.patch.object(documents, 'DOCX_MAX_TOTAL_BYTES', 2 * 1024 * 1024):
            self.assertEqual(self.code(data), 'DOCX_LIMITS_EXCEEDED')

    def test_entry_count_is_capped_before_parsing_the_central_directory(self):
        many = files.docx_bytes(extra={f'customXml/item{i}.xml': b'<a/>' for i in range(documents.DOCX_MAX_ENTRIES)})
        with mock.patch.object(zipfile, 'ZipFile', side_effect=AssertionError('parsed')):
            self.assertEqual(self.code(many), 'DOCX_LIMITS_EXCEEDED')
        self.check(files.docx_bytes(extra={f'customXml/item{i}.xml': b'<a/>' for i in range(100)}))

    def test_macros_and_activex_are_rejected(self):
        cases = [
            files.docx_bytes(extra={'word/vbaProject.bin': b'\xd0\xcf\x11\xe0' + b'\x00' * 100}),
            files.docx_bytes(extra={'word/VBAPROJECT.BIN': b'x'}),
            files.docx_bytes(extra={'word/vbaData.xml': b'<a/>'}),
            files.docx_bytes(extra={'word/activeX/activeX1.xml': b'<a/>'}),
            files.docx_bytes(main='application/vnd.ms-word.document.macroEnabled.main+xml'),
            files.docx_bytes(extra_types='<Override PartName="/word/x.bin" '
                                         'ContentType="application/vnd.ms-office.vbaProject"/>'),
        ]
        for index, data in enumerate(cases):
            with self.subTest(index):
                self.assertEqual(self.code(data), 'DOCX_MACROS_NOT_ALLOWED')

    def test_external_relationships_other_than_hyperlinks_are_rejected(self):
        for rel_type in ('attachedTemplate', 'oleObject', 'image', 'subDocument', 'frame'):
            with self.subTest(rel_type):
                data = files.docx_bytes(document_rels=[(f'{OFFICE_REL}/{rel_type}', 'https://evil.test/x',
                                                        ' TargetMode="External"')])
                self.assertEqual(self.code(data), 'DOCX_EXTERNAL_CONTENT')
        settings_rels = files.relationships((f'{OFFICE_REL}/attachedTemplate', 'file:///c:/x.dotm',
                                             ' TargetMode="External"'))
        self.assertEqual(self.code(files.docx_bytes(extra={'word/_rels/settings.xml.rels': settings_rels})),
                         'DOCX_EXTERNAL_CONTENT')

    def test_hostile_archive_structure(self):
        dtd = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
               b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">&b;'
               b'</Relationships>')
        self.assertEqual(self.code(files.docx_bytes(extra={'word/_rels/x.rels': dtd})), 'INVALID_DOCX')
        self.assertEqual(self.code(files.docx_bytes(extra={'../evil.xml': b'<a/>'})), 'INVALID_DOCX')
        self.assertEqual(self.code(files.docx_bytes(extra={'/abs.xml': b'<a/>'})), 'INVALID_DOCX')
        self.assertEqual(self.code(files.docx_bytes()[:-30]), 'INVALID_DOCX')  # truncated directory

        # Corrupt stored entry bytes: CRC check while streaming fails.
        entries = files.docx_entries()
        entries['word/media/blob.bin'] = b'A' * 5000
        stored = bytearray(files.zip_bytes(entries, compression=zipfile.ZIP_STORED))
        stored[stored.find(b'A' * 5000) + 10] = ord('B')
        self.assertEqual(self.code(bytes(stored)), 'INVALID_DOCX')

        # A forged (smaller) declared size cannot hide extra decompressed bytes.
        entries = files.docx_entries()
        entries['word/media/blob.bin'] = bytes(range(256)) * 64
        forged = bytearray(files.zip_bytes(entries))
        archive = zipfile.ZipFile(io.BytesIO(bytes(forged)))
        info = archive.getinfo('word/media/blob.bin')
        for signature, size_offset in ((b'PK\x03\x04', 22), (b'PK\x01\x02', 24)):
            start = 0
            while True:
                start = forged.find(signature, start)
                if start < 0:
                    break
                name_len = int.from_bytes(forged[start + (26 if signature == b'PK\x03\x04' else 28):][:2], 'little')
                name_at = start + (30 if signature == b'PK\x03\x04' else 46)
                if bytes(forged[name_at:name_at + name_len]) == b'word/media/blob.bin':
                    forged[start + size_offset:start + size_offset + 4] = (100).to_bytes(4, 'little')
                start += 4
        self.assertEqual(info.file_size, 256 * 64)
        self.assertEqual(self.code(bytes(forged)), 'INVALID_DOCX')

    def test_encrypted_entries_are_rejected(self):
        data = bytearray(files.docx_bytes())
        start = data.find(b'PK\x01\x02')
        data[start + 8] |= 0x1  # central directory general-purpose flag: encrypted
        self.assertEqual(self.code(bytes(data)), 'INVALID_DOCX')


class ImageValidationTests(SimpleTestCase):
    def check(self, data, content_type):
        return images.validate_image(io.BytesIO(data), len(data), content_type)

    def code(self, data, content_type='image/png'):
        return rejected(self, images.validate_image, io.BytesIO(data), len(data), content_type)

    def test_valid_png_and_jpeg(self):
        info = self.check(files.image_bytes('PNG'), 'image/png')
        self.assertEqual((info.width, info.height, info.format), (64, 48, 'PNG'))
        info = self.check(files.image_bytes('JPEG'), 'image/jpeg')
        self.assertEqual((info.width, info.height, info.format), (64, 48, 'JPEG'))
        self.check(files.image_bytes('PNG', mode='L'), 'image/png')

    def test_type_mismatch_and_garbage(self):
        self.assertEqual(self.code(files.image_bytes('JPEG'), 'image/png'), 'CONTENT_TYPE_MISMATCH')
        self.assertEqual(self.code(files.image_bytes('PNG'), 'image/jpeg'), 'CONTENT_TYPE_MISMATCH')
        self.assertEqual(self.code(files.image_bytes('GIF'), 'image/png'), 'INVALID_IMAGE')
        self.assertEqual(self.code(b'<svg onload="alert(1)"/>', 'image/png'), 'INVALID_IMAGE')
        self.assertEqual(self.code(files.pdf_bytes(), 'image/jpeg'), 'INVALID_IMAGE')
        png = files.image_bytes('PNG', size=(400, 300))
        self.assertEqual(self.code(png[:len(png) // 2]), 'INVALID_IMAGE')  # truncated
        corrupt = bytearray(png)
        corrupt[40] ^= 0xFF  # chunk CRC mismatch
        self.assertEqual(self.code(bytes(corrupt)), 'INVALID_IMAGE')
        self.assertEqual(self.code(b'', 'image/png'), 'EMPTY_FILE')

    def test_pixel_bombs_rejected_before_decoding(self):
        with mock.patch('PIL.ImageFile.ImageFile.load', side_effect=AssertionError('decoded')):
            # Over Pillow's own bomb threshold (open raises) and under it (our stricter cap).
            self.assertEqual(self.code(files.png_declaring(30000, 30000)), 'IMAGE_TOO_LARGE')
            self.assertEqual(self.code(files.png_declaring(6000, 6000)), 'IMAGE_TOO_LARGE')
            self.assertEqual(self.code(files.png_declaring(13000, 10)), 'IMAGE_TOO_LARGE')  # side cap
            self.assertEqual(self.code(files.jpeg_declaring(10000, 10000), 'image/jpeg'), 'IMAGE_TOO_LARGE')
        self.assertLess(len(files.png_declaring(30000, 30000)), 200)
        # Just under the cap, with no pixel data: header passes, decoding fails safely.
        self.assertEqual(self.code(files.png_declaring(5000, 5000)), 'INVALID_IMAGE')


class KindSpecTests(SimpleTestCase):
    def test_file_names(self):
        image = uploads.SPECS['image']
        self.assertEqual(image.file_name('My Photo.JPEG', 'image/jpeg'), 'My-Photo.jpg')
        self.assertEqual(image.file_name('a.png', 'image/png'), 'a.png')
        document = uploads.SPECS['document']
        self.assertEqual(document.file_name('../Agenda (v2).docx', DOCX_TYPE), 'Agenda-v2.docx')
        self.assertEqual(document.file_name('عربى.pdf', 'application/pdf'), 'document.pdf')
