"""Generated test files (valid and hostile) for document/image validation."""
import io
import struct
import zipfile
import zlib

from PIL import Image

DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
MAIN_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def pdf_bytes(pages=1, header=b'%PDF-1.4', xref_stream=False):
    """Minimal well-formed PDF with a correct cross-reference table/stream."""
    kids = ' '.join(f'{3 + i} 0 R' for i in range(pages))
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', f'<< /Type /Pages /Kids [{kids}] /Count {pages} >>'.encode()]
    objects += [b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>'] * pages
    out = header + b'\n%\xe2\xe3\xcf\xd3\n'
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    xref_at = len(out)
    if xref_stream:
        size = len(objects) + 2
        rows = b'\x00\x00\x00\xff' + b''.join(b'\x01' + struct.pack('>H', off) + b'\x00' for off in offsets)
        rows += b'\x01' + struct.pack('>H', xref_at) + b'\x00'
        data = zlib.compress(rows)
        out += (b'%d 0 obj\n<< /Type /XRef /Size %d /W [1 2 1] /Root 1 0 R /Filter /FlateDecode /Length %d >>\n'
                b'stream\n' % (size - 1, size, len(data)) + data + b'\nendstream\nendobj\n')
    else:
        out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
        out += b''.join(b'%010d 00000 n \n' % off for off in offsets)
        out += b'trailer\n<< /Size %d /Root 1 0 R >>\n' % (len(objects) + 1)
    out += b'startxref\n%d\n%%%%EOF\n' % xref_at
    return out


def content_types(main=MAIN_TYPE, extra=''):
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{main}"/>{extra}</Types>').encode()


def relationships(*rels):
    body = ''.join(f'<Relationship Id="rId{i}" Type="{t}" Target="{target}"{mode}/>'
                   for i, (t, target, mode) in enumerate(rels, start=1))
    return f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{REL_NS}">{body}</Relationships>'.encode()


DOCUMENT_XML = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                b'<w:body><w:p><w:r><w:t>Agenda</w:t></w:r></w:p></w:body></w:document>')


def docx_entries(document_rels=(), main=MAIN_TYPE, extra_types=''):
    return {
        '[Content_Types].xml': content_types(main, extra_types),
        '_rels/.rels': relationships((f'{OFFICE_REL}/officeDocument', 'word/document.xml', '')),
        'word/document.xml': DOCUMENT_XML,
        'word/_rels/document.xml.rels': relationships(*document_rels),
    }


def zip_bytes(entries, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def docx_bytes(extra=None, drop=(), **kwargs):
    entries = docx_entries(**kwargs)
    entries.update(extra or {})
    for name in drop:
        entries.pop(name)
    return zip_bytes(entries)


def image_bytes(fmt='PNG', size=(64, 48), mode='RGB'):
    buffer = io.BytesIO()
    Image.new(mode, size, (200, 30, 30) if mode == 'RGB' else 1).save(buffer, fmt)
    return buffer.getvalue()


def png_chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png_declaring(width, height):
    """Tiny PNG whose header declares a huge canvas (decompression bomb shape)."""
    ihdr = struct.pack('>IIBBBBB', width, height, 1, 0, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', ihdr) + png_chunk(b'IDAT', zlib.compress(b'\x00' * 64))
            + png_chunk(b'IEND', b''))


def jpeg_declaring(width, height):
    """Valid small JPEG with its SOF0 dimensions rewritten."""
    data = bytearray(image_bytes('JPEG', size=(16, 16)))
    index = data.find(b'\xff\xc0')
    struct.pack_into('>HH', data, index + 5, height, width)
    return bytes(data)
