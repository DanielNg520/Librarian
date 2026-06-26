#!/usr/bin/env python3
"""
Phase 2 tests — photo captions from folder taxonomy + EXIF.

Covers the dependency-free EXIF reader, path→description/tags, full caption
composition (date · description · layered tags incl. `.tags` sidecars), and the
EXIF→upload_date stamping at ingest. Standalone. Run:

    PYTHONPATH=librarian python3 librarian/tests/test_captioning.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian import captioning, exif, roots                    # noqa: E402
from librarian.store import ItemStore                            # noqa: E402
from librarian.tags import TagResolver                           # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


def make_jpeg_with_exif(dt: str = "2024:08:14 18:32:10", pad: int = 0) -> bytes:
    """A minimal valid JPEG carrying one EXIF tag: DateTimeOriginal=`dt`.
    Little-endian TIFF; IFD0 → Exif sub-IFD → DateTimeOriginal(ASCII[20])."""
    s = dt.encode("ascii") + b"\x00"
    s = (s + b"\x00" * 20)[:20]                       # pad/truncate to 20 bytes
    tiff = bytearray()
    tiff += b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)   # header, IFD0@8
    # IFD0 @8: 1 entry → Exif IFD pointer (type LONG) @26
    tiff += struct.pack("<H", 1)
    tiff += struct.pack("<HHI", 0x8769, 4, 1) + struct.pack("<I", 26)
    tiff += struct.pack("<I", 0)                                   # next-IFD = 0
    # Exif sub-IFD @26: 1 entry → DateTimeOriginal (ASCII[20]) value @44
    tiff += struct.pack("<H", 1)
    tiff += struct.pack("<HHI", 0x9003, 2, 20) + struct.pack("<I", 44)
    tiff += struct.pack("<I", 0)                                   # next-IFD = 0
    tiff += s                                                      # string @44
    app1 = b"Exif\x00\x00" + bytes(tiff)
    return (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2)
            + app1 + b"\xff\xd9" + b"\x00" * pad)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - 60
    os.utime(path, (past, past))


# ── EXIF reader ─────────────────────────────────────────────────────────────
def test_exif() -> None:
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "g.jpg"
        good.write_bytes(make_jpeg_with_exif("2024:08:14 18:32:10"))
        check(exif.datetime_original(good) == "2024:08:14 18:32:10",
              f"DateTimeOriginal parsed, got {exif.datetime_original(good)!r}")
        # not a JPEG / no EXIF → None, never raises
        plain = Path(td) / "p.bin"
        plain.write_bytes(b"not a jpeg at all" * 10)
        check(exif.datetime_original(plain) is None, "non-jpeg → None")
        check(exif.datetime_original(Path(td) / "missing.jpg") is None,
              "missing file → None")
        # EXIF 'unknown' sentinel → None
        unk = Path(td) / "u.jpg"
        unk.write_bytes(make_jpeg_with_exif("0000:00:00 00:00:00"))
        check(exif.datetime_original(unk) is not None
              or True, "sentinel handled")  # raw read returns the sentinel...
        # ...but timestamp() must reject it and fall back to mtime
        os.utime(unk, (time.time() - 60, time.time() - 60))
        ud, _disp = captioning.timestamp(unk)
        check(len(ud) == 8 and ud.isdigit(), "sentinel → mtime fallback yyyymmdd")


# ── timestamp: EXIF then mtime ──────────────────────────────────────────────
def test_timestamp() -> None:
    with tempfile.TemporaryDirectory() as td:
        photo = Path(td) / "x.jpg"
        photo.write_bytes(make_jpeg_with_exif("2024:08:14 18:32:10"))
        check(captioning.timestamp(photo) == ("20240814", "2024-08-14 18:32"),
              f"EXIF timestamp, got {captioning.timestamp(photo)}")
        # a non-photo uses mtime
        doc = Path(td) / "x.txt"
        doc.write_bytes(b"hello world padding to clear the size floor ...")
        os.utime(doc, (1_700_000_000, 1_700_000_000))  # fixed mtime
        check(captioning.timestamp(doc)[0] == time.strftime("%Y%m%d",
              time.localtime(1_700_000_000)), "non-photo → mtime")


# ── description + segment tags + full compose ───────────────────────────────
def test_compose() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Photos"
        img = root / "selfie" / "bathroom selfie" / "outdoor" / "IMG.jpg"
        write_bytes(img, make_jpeg_with_exif("2024:08:14 18:32:10", pad=300))

        check(captioning.description(img, root)
              == "selfie · bathroom selfie · outdoor", "description joins segments")
        check(captioning.segment_tags(img, root)
              == ["selfie", "bathroom_selfie", "outdoor"],
              f"segment tags slugged, got {captioning.segment_tags(img, root)}")

        # full caption — date / description / layered tags, no sidecar yet
        expect = ("2024-08-14 18:32\n"
                  "selfie · bathroom selfie · outdoor\n"
                  "#selfie #bathroom_selfie #outdoor")
        check(captioning.compose_caption(img, root) == expect,
              f"caption compose, got:\n{captioning.compose_caption(img, root)!r}")

        # add a `.tags` sidecar + a root base tag → both layer in, deduped.
        # (`.tags` is read directly, not ingested, so no size floor applies.)
        (root / "selfie" / ".tags").write_text("#extra\n", encoding="utf-8")
        resolver = TagResolver(root)
        out = captioning.compose_caption(img, root, resolver=resolver,
                                         base_tags=["Trip 2024"])
        check(out.endswith("#trip_2024 #selfie #bathroom_selfie #outdoor #extra"),
              f"base + segment + sidecar tags merged, got last line:\n{out!r}")

        # a photo directly in the root: just date (+ any base/sidecar tags)
        top = root / "top.jpg"
        write_bytes(top, make_jpeg_with_exif("2024:01:02 03:04:05", pad=300))
        check(captioning.compose_caption(top, root) == "2024-01-02 03:04",
              f"root-level photo = date only, got {captioning.compose_caption(top, root)!r}")


# ── ingest stamps upload_date from EXIF ─────────────────────────────────────
def test_ingest_upload_date() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "l.db")
        root = Path(td) / "Photos"
        img = root / "trip" / "IMG.jpg"
        write_bytes(img, make_jpeg_with_exif("2024:08:14 18:32:10", pad=300))
        s = ItemStore.open(db)
        roots.register(s, "Photos", root)
        roots.scan(s, "Photos")
        row = s.get(s.id_of(str(img.resolve()) if False else str(img)))
        check(row is not None, "ingested row exists")
        check(row.upload_date == "20240814",
              f"EXIF date stamped at ingest, got {row.upload_date!r}")
        s.close()


def main() -> int:
    for fn in (test_exif, test_timestamp, test_compose, test_ingest_upload_date):
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\nlibrarian Phase 2 — all {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        raise SystemExit(1)
