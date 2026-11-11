"""Bounded PDF/DOCX content validation (D2: R2 does not inspect content; Django does).

Both validators work on the seekable spooled copy of the immutable validated
object (see :func:`dawah.media.spool_stream`), never on client-writable bytes.
Nothing is executed, rendered or extracted to disk.

PDF: byte-level structure sanity (no stream is decoded, so no Flate bomb):
header ``%PDF-1.0``..``%PDF-2.0`` at offset 0, ``%%EOF`` near the end, a final
``startxref`` whose offset lies inside the file and points at a classic
``xref`` table (with a ``trailer`` naming ``/Root``) or a cross-reference
stream object (``/Type /XRef`` with ``/Root``), and at least one indirect
object.

DOCX: a ZIP (OOXML) archive. The end-of-central-directory record is read
first so the entry count is capped *before* the central directory is
parsed. Every entry must be stored/deflated, unencrypted, with a sane name;
declared sizes are capped per entry, in total and by compression ratio, then
every entry is decompressed in bounded chunks (verifying CRCs and that the
real size equals the declared one, so a forged header cannot hide a bomb).
``[Content_Types].xml`` and ``word/document.xml`` are required and the main
part must be a plain (not macro-enabled/template) document. VBA projects,
ActiveX parts and external relationships other than hyperlinks are refused.
XML parts that are parsed are size-capped and may not contain a DTD.
"""
from __future__ import annotations

import re
import struct
import zipfile
import zlib
from xml.etree import ElementTree

from .media import MediaRejected

PDF_CONTENT_TYPE = 'application/pdf'
DOCX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
DOCUMENT_MAX_BYTES = 20 * 1024 * 1024  # D2 engineering limit: 20 MiB

# ---------------------------------------------------------------- PDF

PDF_HEADER = re.compile(rb'%PDF-(1\.[0-7]|2\.0)')
PDF_TAIL_BYTES = 4096
PDF_PROBE_BYTES = 64 * 1024


def _invalid_pdf():
    return MediaRejected('INVALID_PDF', 'The file is not a valid PDF document.')


def validate_pdf(fileobj, size):
    if size <= 0:
        raise MediaRejected('EMPTY_FILE', 'The file is empty.')
    try:
        fileobj.seek(0)
        head = fileobj.read(min(size, 1024))
        if not PDF_HEADER.match(head):
            raise _invalid_pdf()
        tail_start = max(0, size - PDF_TAIL_BYTES)
        fileobj.seek(tail_start)
        tail = fileobj.read(PDF_TAIL_BYTES)
        if b'%%EOF' not in tail[-1024:]:
            raise _invalid_pdf()
        match = None
        for match in re.finditer(rb'startxref\s+(\d{1,10})\s+%%EOF', tail):
            pass
        if match is None:
            raise _invalid_pdf()
        offset = int(match.group(1))
        if offset <= 0 or offset >= tail_start + match.start():
            raise _invalid_pdf()
        fileobj.seek(offset)
        probe = fileobj.read(PDF_PROBE_BYTES)
        if probe.startswith(b'xref'):
            trailer = probe.find(b'trailer')
            if trailer < 0:
                # A large classic table: the trailer precedes startxref in the tail.
                trailer_tail = tail.rfind(b'trailer')
                if trailer_tail < 0 or b'/Root' not in tail[trailer_tail:]:
                    raise _invalid_pdf()
            elif b'/Root' not in probe[trailer:]:
                raise _invalid_pdf()
        else:
            xref_obj = re.match(rb'\d{1,10}\s+\d{1,5}\s+obj\b', probe)
            dictionary = probe[:4096]
            if (xref_obj is None or not re.search(rb'/Type\s*/XRef\b', dictionary)
                    or b'/Root' not in dictionary):
                raise _invalid_pdf()
        fileobj.seek(0)
        body = fileobj.read(PDF_PROBE_BYTES)
        if not re.search(rb'\d+\s+\d+\s+obj\b', body):
            raise _invalid_pdf()
        if b'endobj' not in body and b'endobj' not in tail and not _has_endobj(fileobj):
            raise _invalid_pdf()
    except MediaRejected:
        raise
    except Exception:
        raise _invalid_pdf() from None


def _has_endobj(fileobj):
    """Bounded scan for an ``endobj`` token (objects larger than the probe)."""
    fileobj.seek(0)
    carry = b''
    while True:
        chunk = fileobj.read(1024 * 1024)
        if not chunk:
            return False
        if b'endobj' in carry + chunk:
            return True
        carry = chunk[-8:]


# ---------------------------------------------------------------- DOCX

DOCX_MAX_ENTRIES = 1000
DOCX_MAX_ENTRY_BYTES = 50 * 1024 * 1024       # uncompressed, per entry
DOCX_MAX_TOTAL_BYTES = 100 * 1024 * 1024      # uncompressed, whole archive
DOCX_MAX_RATIO = 100                           # uncompressed / compressed, per entry
DOCX_RATIO_MIN_BYTES = 1024 * 1024             # tiny entries may compress arbitrarily well
DOCX_MAX_XML_BYTES = 4 * 1024 * 1024           # parsed XML parts ([Content_Types].xml, *.rels)
DOCX_MAX_NAME_LENGTH = 512
READ_CHUNK = 256 * 1024

MAIN_DOCUMENT_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
HYPERLINK_SUFFIX = '/hyperlink'
CT_NS = '{http://schemas.openxmlformats.org/package/2006/content-types}'
REL_NS = '{http://schemas.openxmlformats.org/package/2006/relationships}'
MACRO_NAMES = re.compile(r'(^|/)(vbaProject\.bin|vbaData\.xml)$|(^|/)activeX/', re.IGNORECASE)
EOCD_SIGNATURE = b'PK\x05\x06'
EOCD_SEARCH = 65535 + 22


def _invalid_docx():
    return MediaRejected('INVALID_DOCX', 'The file is not a valid Word (.docx) document.')


def _limits():
    return MediaRejected('DOCX_LIMITS_EXCEEDED',
                         'The document archive exceeds the allowed entry count, size or compression limits.')


def _eocd_entry_count(fileobj, size):
    """Entry count from the end-of-central-directory record, read before zipfile parses."""
    fileobj.seek(max(0, size - EOCD_SEARCH))
    tail = fileobj.read(EOCD_SEARCH)
    position = tail.rfind(EOCD_SIGNATURE)
    if position < 0 or len(tail) - position < 22:
        raise _invalid_docx()
    disk, cd_disk, disk_entries, total_entries, cd_size, cd_offset = struct.unpack(
        '<HHHHII', tail[position + 4:position + 20])
    if (disk, cd_disk) != (0, 0) or disk_entries != total_entries:
        raise _invalid_docx()  # multi-disk archives are not documents
    if total_entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        raise _limits()  # ZIP64 is never needed below the 20 MiB document limit
    if total_entries > DOCX_MAX_ENTRIES:
        raise _limits()
    if cd_size > size or cd_offset > size:
        raise _invalid_docx()
    return total_entries


def _safe_name(name):
    return (0 < len(name) <= DOCX_MAX_NAME_LENGTH and not name.startswith('/') and '\\' not in name
            and '\x00' not in name and '..' not in name.split('/') and ':' not in name)


def _read_bounded(archive, info, limit):
    """Decompress one entry in chunks, never past its (capped) declared size.

    The CRC is recomputed here: zipfile only checks it when the compressed
    stream ends, so a forged smaller size would otherwise pass unverified."""
    total = 0
    crc = 0
    parts = [] if limit else None
    with archive.open(info) as handle:
        while True:
            chunk = handle.read(READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            crc = zlib.crc32(chunk, crc)
            if total > info.file_size:
                raise _limits()
            if parts is not None:
                if total > limit:
                    raise _limits()
                parts.append(chunk)
    if total != info.file_size or crc != info.CRC:
        raise _invalid_docx()
    return b''.join(parts) if parts is not None else None


def _parse_xml(data):
    lowered = data[:4096].lower()
    if b'<!doctype' in lowered or b'<!entity' in data.lower():
        raise _invalid_docx()  # OOXML never needs a DTD; refuse entity expansion outright
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise _invalid_docx() from None


def validate_docx(fileobj, size):
    if size <= 0:
        raise MediaRejected('EMPTY_FILE', 'The file is empty.')
    try:
        fileobj.seek(0)
        if fileobj.read(4) != b'PK\x03\x04':
            raise _invalid_docx()
        _eocd_entry_count(fileobj, size)
        fileobj.seek(0)
        archive = zipfile.ZipFile(fileobj)
        infos = archive.infolist()
        if not infos or len(infos) > DOCX_MAX_ENTRIES:
            raise _limits()
        names = set()
        total = 0
        for info in infos:
            name = info.filename
            if not _safe_name(name) or name in names:
                raise _invalid_docx()
            names.add(name)
            if info.flag_bits & 0x1:
                raise _invalid_docx()  # encrypted entry
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise _invalid_docx()
            if info.file_size > DOCX_MAX_ENTRY_BYTES:
                raise _limits()
            total += info.file_size
            if total > DOCX_MAX_TOTAL_BYTES:
                raise _limits()
            if (info.file_size > DOCX_RATIO_MIN_BYTES
                    and info.file_size > DOCX_MAX_RATIO * max(info.compress_size, 1)):
                raise _limits()
            if MACRO_NAMES.search(name):
                raise MediaRejected('DOCX_MACROS_NOT_ALLOWED', 'Documents with macros or ActiveX content are not '
                                                               'accepted. Save it as a plain .docx file.')
        if '[Content_Types].xml' not in names or 'word/document.xml' not in names:
            raise _invalid_docx()

        xml_parts = {}
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            wanted = name == '[Content_Types].xml' or name.endswith('.rels')
            data = _read_bounded(archive, info, DOCX_MAX_XML_BYTES if wanted else 0)
            if wanted:
                xml_parts[name] = data

        _check_content_types(_parse_xml(xml_parts['[Content_Types].xml']))
        for name, data in xml_parts.items():
            if name.endswith('.rels'):
                _check_relationships(_parse_xml(data))
    except MediaRejected:
        raise
    except Exception:
        # zipfile.BadZipFile (CRC/overlap/truncation), struct errors, zlib errors, ...
        raise _invalid_docx() from None


def _check_content_types(root):
    if root.tag != f'{CT_NS}Types':
        raise _invalid_docx()
    main = None
    for element in root:
        content_type = (element.get('ContentType') or '')
        lowered = content_type.lower()
        if 'macroenabled' in lowered or 'vbaproject' in lowered or 'activex' in lowered:
            raise MediaRejected('DOCX_MACROS_NOT_ALLOWED', 'Documents with macros or ActiveX content are not '
                                                           'accepted. Save it as a plain .docx file.')
        if element.tag == f'{CT_NS}Override' and element.get('PartName') == '/word/document.xml':
            main = content_type
    if main != MAIN_DOCUMENT_TYPE:
        raise _invalid_docx()


def _check_relationships(root):
    if root.tag != f'{REL_NS}Relationships':
        raise _invalid_docx()
    for element in root:
        if (element.get('TargetMode') or '').lower() == 'external':
            if not (element.get('Type') or '').endswith(HYPERLINK_SUFFIX):
                raise MediaRejected('DOCX_EXTERNAL_CONTENT', 'Documents that load external content (linked '
                                                             'templates, objects or images) are not accepted.')
