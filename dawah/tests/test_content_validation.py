"""Bounded PDF / DOCX / image validators (no storage, no database)."""
import io
import random
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
        noise = random.Random(7).randbytes(1024 * 1024)  # incompressible, ratio ~1
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
        self.assertEqual(self.code(files.png_declaring(4000, 3999)), 'INVALID_IMAGE')
        self.assertEqual(self.code(files.png_declaring(4000, 4001)), 'IMAGE_TOO_LARGE')  # 16 MP cap


class KindSpecTests(SimpleTestCase):
    def test_file_names(self):
        image = uploads.SPECS['image']
        self.assertEqual(image.file_name('My Photo.JPEG', 'image/jpeg'), 'My-Photo.jpg')
        self.assertEqual(image.file_name('a.png', 'image/png'), 'a.png')
        document = uploads.SPECS['document']
        self.assertEqual(document.file_name('../Agenda (v2).docx', DOCX_TYPE), 'Agenda-v2.docx')
        self.assertEqual(document.file_name('عربى.pdf', 'application/pdf'), 'document.pdf')


class ImageSanitizationTests(SimpleTestCase):
    """Featured images are published as a re-encoded, metadata-free derivative."""

    def derivative(self, data, content_type):
        from PIL import Image
        info = images.validate_image(io.BytesIO(data), len(data), content_type)
        return info, Image.open(io.BytesIO(info.sanitized))

    def test_jpeg_gps_exif_comment_xmp_removed_and_orientation_applied(self):
        from PIL import ExifTags, Image
        source = files.jpeg_with_metadata()
        original = Image.open(io.BytesIO(source))
        self.assertTrue(original.getexif().get_ifd(ExifTags.IFD.GPSInfo))  # the fixture really has GPS
        self.assertIn(b'secret-comment', source)
        info, clean = self.derivative(source, 'image/jpeg')
        for marker in (b'Exif\x00\x00', b'GPS', b'SecretCam', b'secret-comment', b'secret-xmp', b'xmpmeta'):
            self.assertNotIn(marker, info.sanitized)
        self.assertEqual(dict(clean.getexif()), {})
        self.assertNotIn('comment', clean.info)
        # Orientation 6 (rotate 90 CW): 60x40 becomes 40x60, red (left) ends up on top.
        self.assertEqual((clean.size, (info.width, info.height)), ((40, 60), (40, 60)))
        top, bottom = clean.convert('RGB').getpixel((20, 5)), clean.convert('RGB').getpixel((20, 55))
        self.assertGreater(top[0], 200)
        self.assertGreater(bottom[2], 200)
        self.assertEqual(clean.format, 'JPEG')

    def test_png_text_and_exif_chunks_removed_icc_kept(self):
        from PIL import ImageCms
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        source = files.png_with_metadata(icc=icc)
        self.assertTrue({b'tEXt', b'iTXt', b'eXIf'} <= set(files.png_chunk_types(source)))
        info, clean = self.derivative(source, 'image/png')
        chunks = set(files.png_chunk_types(info.sanitized))
        self.assertFalse(chunks & {b'tEXt', b'iTXt', b'zTXt', b'eXIf', b'tIME'})
        self.assertIn(b'iCCP', chunks)
        self.assertEqual(clean.info.get('icc_profile'), icc)
        self.assertNotIn(b'secret', info.sanitized)

    def test_single_frame_and_palette_transparency(self):
        info, clean = self.derivative(files.animated_png(), 'image/png')
        self.assertEqual(getattr(clean, 'n_frames', 1), 1)
        self.assertNotIn(b'acTL', info.sanitized)
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('P', (8, 8), 0).save(buffer, 'PNG', transparency=0)
        info, clean = self.derivative(buffer.getvalue(), 'image/png')
        self.assertEqual(clean.mode, 'RGBA')
        self.assertEqual(clean.getpixel((0, 0))[3], 0)

    def test_large_images_are_downscaled_jpeg_via_draft(self):
        from PIL import JpegImagePlugin
        source = files.image_bytes('JPEG', size=(4800, 3000))
        with mock.patch.object(JpegImagePlugin.JpegImageFile, 'draft',
                               autospec=True, side_effect=JpegImagePlugin.JpegImageFile.draft) as draft:
            info, clean = self.derivative(source, 'image/jpeg')
        self.assertTrue(draft.called)
        self.assertEqual(clean.size, (4096, 2560))
        info, clean = self.derivative(files.image_bytes('PNG', size=(5000, 1000)), 'image/png')
        self.assertEqual(clean.size, (4096, 819))

    def test_cmyk_jpeg_becomes_rgb(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('CMYK', (10, 10), (0, 255, 255, 0)).save(buffer, 'JPEG')
        info, clean = self.derivative(buffer.getvalue(), 'image/jpeg')
        self.assertEqual(clean.mode, 'RGB')


class PdfActiveContentTests(SimpleTestCase):
    def code(self, data):
        return rejected(self, documents.validate_pdf, io.BytesIO(data), len(data))

    def test_active_content_is_refused(self):
        js_action = b'<< /Type /Action /S /JavaScript /JS (app.alert(1)) >>'
        cases = {
            'open action js': files.pdf_bytes(catalog_extra=b' /OpenAction 4 0 R', extra=[js_action]),
            'JS key only': files.pdf_bytes(extra=[b'<< /S /Foo /JS 5 0 R >>']),
            'hex-escaped name': files.pdf_bytes(extra=[b'<< /S /J#61vaScript >>']),
            'launch': files.pdf_bytes(extra=[b'<< /S /Launch /F (calc.exe) >>']),
            'embedded files': files.pdf_bytes(catalog_extra=b' /Names << /EmbeddedFiles 4 0 R >>',
                                             extra=[b'<< /Names [] >>']),
            'embedded file stream': files.pdf_bytes(
                extra=[b'<< /Type /EmbeddedFile /Length 0 >>\nstream\n\nendstream']),
            'rich media': files.pdf_bytes(extra=[b'<< /Subtype /RichMedia >>']),
            'inside object stream': files.pdf_bytes(extra=[files.object_stream(b'5 0 ' + js_action)]),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertEqual(self.code(data), 'PDF_ACTIVE_CONTENT')

    def test_clean_object_streams_and_lookalike_names_pass(self):
        ok = files.pdf_bytes(extra=[files.object_stream(b'5 0 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'),
                                    b'<< /JSON (not a key) /Launcher 1 /JavaScripts 2 >>'])
        documents.validate_pdf(io.BytesIO(ok), len(ok))
        goto = files.pdf_bytes(catalog_extra=b' /OpenAction [3 0 R /Fit]')
        documents.validate_pdf(io.BytesIO(goto), len(goto))

    def test_uninspectable_or_oversized_object_streams_are_invalid(self):
        hex_stream = files.pdf_bytes(extra=[files.object_stream(b'5 0 << >>', filter_name=b'/ASCIIHexDecode')])
        self.assertEqual(self.code(hex_stream), 'INVALID_PDF')
        broken = files.pdf_bytes(
            extra=[b'<< /Type /ObjStm /Filter /FlateDecode /Length 5 >>\nstream\nnope!\nendstream'])
        self.assertEqual(self.code(broken), 'INVALID_PDF')
        bomb = files.pdf_bytes(extra=[files.object_stream(b'5 0 ' + b' ' * (2 * 1024 * 1024))])
        with mock.patch.object(documents, 'OBJSTM_MAX_INFLATED', 1024 * 1024):
            self.assertEqual(self.code(bomb), 'INVALID_PDF')
        documents.validate_pdf(io.BytesIO(bomb), len(bomb))


class DocxAggregateLimitTests(SimpleTestCase):
    def test_aggregate_ratio_limit(self):
        # Each entry stays under the per-entry ratio threshold (1 MiB), the archive does not.
        entries = {f'word/media/z{i}.bin': b'\x00' * (900 * 1024) for i in range(6)}
        data = files.docx_bytes(extra=entries)
        self.assertEqual(rejected(self, documents.validate_docx, io.BytesIO(data), len(data)),
                         'DOCX_LIMITS_EXCEEDED')
        self.assertEqual(documents.DOCX_MAX_TOTAL_BYTES, 60 * 1024 * 1024)
