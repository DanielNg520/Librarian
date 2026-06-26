"""
librarian.exif
──────────────
A tiny, DEPENDENCY-FREE reader for the one EXIF field Librarian needs: the
photo's capture timestamp (DateTimeOriginal, falling back to DateTime). Parsing
just this tag from a JPEG's APP1/TIFF block is small and well-understood, so we
avoid taking on Pillow/piexif for Phase 2's "no new deps" rule.

Scope: JPEG/Exif only (the common phone/camera case). HEIC/PNG/etc. carry their
timestamp differently or not at all; for those `datetime_original` returns None
and the captioner falls back to file mtime. Never raises — any malformed/short
buffer yields None.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)

# Read enough from the file head to contain SOI + APP0(JFIF) + APP1(Exif). An
# APP1 segment's length field is 16-bit (≤ 65535), and Exif always precedes the
# compressed image data, so 256 KiB is a generous, safe bound.
_HEAD_BYTES = 256 * 1024

_TAG_DATETIME          = 0x0132   # IFD0
_TAG_EXIF_IFD_POINTER  = 0x8769   # IFD0 → offset of the Exif sub-IFD
_TAG_DATETIME_ORIGINAL = 0x9003   # Exif sub-IFD
_TYPE_ASCII            = 2


def datetime_original(path) -> str | None:
    """Return the raw EXIF datetime string ('YYYY:MM:DD HH:MM:SS') for a JPEG,
    or None. Prefers DateTimeOriginal; falls back to DateTime."""
    try:
        with open(path, "rb") as f:
            buf = f.read(_HEAD_BYTES)
    except OSError as e:
        log.debug("exif: read failed on %s: %s", path, e)
        return None
    if buf[:2] != b"\xff\xd8":          # not a JPEG
        return None

    i, n = 2, len(buf)
    while i + 4 <= n:
        if buf[i] != 0xFF:
            return None                 # marker misalignment → give up
        marker = buf[i + 1]
        if marker in (0xD9, 0xDA):      # EOI / start-of-scan → no more headers
            return None
        seg_len = struct.unpack(">H", buf[i + 2:i + 4])[0]
        seg_start, seg_end = i + 4, i + 2 + seg_len
        if marker == 0xE1 and buf[seg_start:seg_start + 6] == b"Exif\x00\x00":
            return _parse_tiff(buf[seg_start + 6:seg_end])
        i = seg_end
    return None


def _parse_tiff(t: bytes) -> str | None:
    if len(t) < 8:
        return None
    if t[:2] == b"II":
        e = "<"
    elif t[:2] == b"MM":
        e = ">"
    else:
        return None
    (ifd0_off,) = struct.unpack(e + "I", t[4:8])

    dt = dt_orig = None
    exif_ptr: int | None = None
    for tag, typ, cnt, raw in _read_ifd(t, ifd0_off, e):
        if tag == _TAG_DATETIME and typ == _TYPE_ASCII:
            dt = _ascii(t, cnt, raw, e)
        elif tag == _TAG_EXIF_IFD_POINTER:
            (exif_ptr,) = struct.unpack(e + "I", raw)
    if exif_ptr is not None:
        for tag, typ, cnt, raw in _read_ifd(t, exif_ptr, e):
            if tag == _TAG_DATETIME_ORIGINAL and typ == _TYPE_ASCII:
                dt_orig = _ascii(t, cnt, raw, e)
    return dt_orig or dt


def _read_ifd(t: bytes, off: int, e: str) -> list[tuple[int, int, int, bytes]]:
    """Yield (tag, type, count, value/offset-bytes) for each 12-byte entry."""
    if off < 0 or off + 2 > len(t):
        return []
    (count,) = struct.unpack(e + "H", t[off:off + 2])
    out, base = [], off + 2
    for k in range(count):
        ent = base + k * 12
        if ent + 12 > len(t):
            break
        tag, typ, cnt = struct.unpack(e + "HHI", t[ent:ent + 8])
        out.append((tag, typ, cnt, t[ent + 8:ent + 12]))
    return out


def _ascii(t: bytes, cnt: int, raw: bytes, e: str) -> str | None:
    """Decode an ASCII EXIF value: inline when ≤4 bytes, else at the offset in
    `raw`. EXIF uses '0000:00:00 00:00:00' for 'unknown' → treated as None."""
    if cnt <= 4:
        data = raw[:cnt]
    else:
        (offset,) = struct.unpack(e + "I", raw)
        data = t[offset:offset + cnt]
    s = data.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    if not s or s.startswith("0000:00:00"):
        return None
    return s
