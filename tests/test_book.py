#!/usr/bin/env python3
"""
Phase 6 tests — book enrichment (ISBN ladder, async fail-soft pass).

All rungs are exercised without any of the optional deps (pypdf/pdfminer/OCR)
installed, via dependency injection: the ISBN checksum core and filename parse
are pure; online lookup takes a fake fetcher; the ladder + pass take injected
extractors/enrichers. Covers the Verify goal directly — born-digital AND scanned
PDFs both resolve title/author/ISBN, and a no-ISBN book falls back to the
filename with no crash.

    PYTHONPATH=. python3 tests/test_book.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian import enrich, roots                              # noqa: E402
from librarian.captioning import book, isbn                      # noqa: E402
from librarian.captioning.book import BookMetadata               # noqa: E402
from librarian.store import ItemStore                            # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


# Canonical test book: Effective Java, ISBN-13 9780134685991 (10: 0134685997).
ISBN13 = "9780134685991"
ISBN10 = "0134685997"

_OPEN_LIBRARY = (
    '{"ISBN:9780134685991": {"title": "Effective Java",'
    ' "authors": [{"name": "Joshua Bloch"}],'
    ' "publish_date": "2018", "publishers": [{"name": "Addison-Wesley"}]}}')
_GOOGLE_BOOKS = (
    '{"items": [{"volumeInfo": {"title": "Effective Java",'
    ' "authors": ["Joshua Bloch"], "publishedDate": "2018-01-01",'
    ' "publisher": "Addison-Wesley"}}]}')


def fake_fetcher(*, ol: str | None = _OPEN_LIBRARY, gb: str | None = _GOOGLE_BOOKS):
    def get(url: str):
        if "openlibrary.org" in url:
            return ol
        if "googleapis.com" in url:
            return gb
        return None
    return get


# ── ISBN core ─────────────────────────────────────────────────────────────────
def test_isbn_checksums() -> None:
    check(isbn.is_valid_isbn13(ISBN13), "valid ISBN-13")
    check(not isbn.is_valid_isbn13("9780134685990"), "bad ISBN-13 checksum rejected")
    check(isbn.is_valid_isbn10(ISBN10), "valid ISBN-10")
    check(not isbn.is_valid_isbn10("0134685998"), "bad ISBN-10 checksum rejected")
    check(isbn.is_valid_isbn10("080442957X"), "ISBN-10 with X check digit")
    check(isbn.isbn10_to_13(ISBN10) == ISBN13, "ISBN-10 → 13 conversion")
    check(isbn.isbn10_to_13("bogus") is None, "invalid 10 → None")


def test_isbn_extraction() -> None:
    text = ("copyright page\nISBN 978-0-13-468599-1\nsome noise 1234567890\n"
            "also ISBN-10: 0-13-468599-7\n")
    found = isbn.find_isbns(text)
    check(found == [ISBN13], f"both forms of the same book dedup to one 13, got {found}")
    check(isbn.find_isbns("no isbns here, 999 pages") == [], "no valid ISBN → []")
    # a random 13-digit run that fails the checksum must not be reported
    check(isbn.find_isbns("barcode 9999999999999") == [], "invalid 13 rejected")


# ── filename parse ────────────────────────────────────────────────────────────
def test_parse_filename() -> None:
    m = book.parse_filename("Joshua Bloch - Effective Java (2018).pdf")
    check(m.title == "Effective Java" and m.author == "Joshua Bloch"
          and m.year == "2018" and m.source == "filename", "Author - Title (Year)")
    m2 = book.parse_filename("Effective Java.pdf")   # no author-dash pattern
    check(m2.title == "Effective Java" and m2.source == "stem", "bare stem fallback")


# ── online lookup ─────────────────────────────────────────────────────────────
def test_lookup_open_library() -> None:
    m = book.lookup_isbn(ISBN13, fetcher=fake_fetcher())
    check(m is not None and m.title == "Effective Java", "OL title")
    check(m.author == "Joshua Bloch" and m.year == "2018", "OL author+year")
    check(m.publisher == "Addison-Wesley" and m.isbn == ISBN13, "OL publisher+isbn")
    check(m.source == "isbn", "source marked isbn")


def test_lookup_falls_back_to_google() -> None:
    m = book.lookup_isbn(ISBN13, fetcher=fake_fetcher(ol=None))
    check(m is not None and m.title == "Effective Java" and m.year == "2018",
          "Google Books fallback when Open Library misses")


def test_lookup_both_miss() -> None:
    m = book.lookup_isbn(ISBN13, fetcher=fake_fetcher(ol=None, gb=None))
    check(m is None, "both providers miss → None")
    check(book.lookup_isbn(ISBN13, fetcher=fake_fetcher(ol="not json", gb=None))
          is None, "garbage body → None, no crash")


# ── the ladder (injected extractors — no deps needed) ─────────────────────────
def _ladder(text="", ocr="", embedded=None, filename="book.pdf", **kw):
    emb = embedded if embedded is not None else BookMetadata()
    return book.enrich(
        filename,
        fetcher=fake_fetcher(),
        text_extractor=lambda p, **k: text,
        ocr_extractor=lambda p, **k: ocr,
        embedded_reader=lambda p: emb,
        **kw)


def test_ladder_born_digital() -> None:
    # Text layer yields the ISBN → authoritative online metadata.
    m = _ladder(text=f"title page\nISBN {ISBN13}\n",
                filename="whatever.pdf")
    check(m.source == "isbn" and m.title == "Effective Java", "born-digital → ISBN")
    check(m.author == "Joshua Bloch" and m.isbn == ISBN13, "author+isbn from lookup")


def test_ladder_scanned_ocr() -> None:
    # No extractable text (scanned); OCR surfaces the ISBN.
    m = _ladder(text="", ocr=f"scanned copyright\nISBN {ISBN10}\n",
                filename="scan_0001.pdf")
    check(m.source == "isbn" and m.title == "Effective Java",
          "scanned PDF resolves via OCR → ISBN lookup")
    check(m.isbn == ISBN13, "OCR'd ISBN-10 up-converted and resolved")


def test_ladder_ocr_disabled() -> None:
    # OCR would surface the ISBN, but ocr=False skips that rung → filename fallback.
    m = book.enrich(
        "Ann Author - Some Title.pdf",
        fetcher=fake_fetcher(),
        text_extractor=lambda p, **k: "",
        ocr_extractor=lambda p, **k: f"ISBN {ISBN13}",
        embedded_reader=lambda p: BookMetadata(),
        ocr=False)
    check(m.source == "filename" and m.title == "Some Title",
          "ocr=False → OCR rung skipped, filename fallback used")


def test_ladder_no_isbn_filename_fallback() -> None:
    m = _ladder(text="a book with no identifiers",
                filename="Joshua Bloch - Effective Java (2018).pdf")
    check(m.source == "filename" and m.title == "Effective Java", "filename fallback")
    check(m.author == "Joshua Bloch" and m.isbn is None, "no ISBN stamped")


def test_ladder_embedded_over_stem() -> None:
    m = _ladder(text="no isbn", embedded=BookMetadata(title="Embedded Title",
                author="Meta Author", source="embedded"),
                filename="unhelpful_scan.pdf")
    check(m.title == "Embedded Title" and m.author == "Meta Author",
          "embedded metadata beats a bare stem")


def test_ladder_online_off() -> None:
    m = _ladder(text=f"ISBN {ISBN13}", filename="x.pdf", online=False)
    check(m.source != "isbn" and m.isbn == ISBN13,
          "online=False: ISBN still stamped, but no network lookup")


def test_ladder_last_resort_stem() -> None:
    m = _ladder(text="", ocr="", filename="mystery_document.pdf")
    check(m.title == "mystery_document" and m.source == "stem", "raw stem last resort")


# ── caption composition ───────────────────────────────────────────────────────
def test_compose_caption() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Books"
        (root / "programming").mkdir(parents=True)
        p = root / "programming" / "effective_java.pdf"
        p.write_bytes(b"%PDF" + b"x" * 200)
        meta = BookMetadata(title="Effective Java", author="Joshua Bloch",
                            year="2018", publisher="Addison-Wesley", isbn=ISBN13,
                            source="isbn")
        cap = book.compose_book_caption(meta, p, root, base_tags=["books"])
        lines = cap.splitlines()
        check(lines[0] == "Effective Java", "title line")
        check(lines[1] == "Joshua Bloch · 2018 · Addison-Wesley", "meta line")
        check(f"ISBN {ISBN13}" in lines, "ISBN line present")
        check("programming" in cap, "folder description included")
        check(lines[-1] == "#books #programming", "layered folder tags")


# ── the enrichment pass ───────────────────────────────────────────────────────
def make(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - 60
    os.utime(path, (past, past))


def test_enrich_pass() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "Books"
        make(root / "prog" / "ej.pdf", b"%PDF" + b"x" * 300)
        make(root / "notes.txt", b"n" * 300)             # document bucket, not a book
        make(root / "cover.jpg", b"j" * 300)             # not a document
        s = ItemStore.open(str(td / "l.db"))
        roots.register(s, "Books", root, tags="library")
        roots.scan(s, "Books")

        # Inject an enricher so the pass never touches pypdf/network.
        def enricher(path, **kw):
            return BookMetadata(title="Effective Java", author="Joshua Bloch",
                                year="2018", isbn=ISBN13, source="isbn")

        rep = enrich.enrich_pass(s, enricher=enricher)
        check(rep.scanned == 1, f"only the .pdf is a book-in-document bucket, {rep}")
        check(rep.enriched == 1 and rep.identified == 1, f"identified via ISBN, {rep}")

        pdf = s.get(s.id_of(str(root / "prog" / "ej.pdf")))
        check(pdf.title == "Effective Java", "title written back")
        check("Joshua Bloch" in pdf.caption and f"ISBN {ISBN13}" in pdf.caption,
              "caption composed with author + ISBN")
        check("#library" in pdf.caption and "#prog" in pdf.caption,
              "root base tag + folder tag layered onto book caption")

        # searchable via FTS now
        check(len(s.search("effective java")) == 1, "enriched book is findable")

        # second pass skips the already-captioned book (idempotent, cheap)
        rep2 = enrich.enrich_pass(s, enricher=enricher)
        check(rep2.enriched == 0 and rep2.skipped == 1, f"already captioned skipped, {rep2}")
        s.close()


def test_enrich_pass_fail_soft() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "Books"
        make(root / "boom.pdf", b"%PDF" + b"x" * 300)
        s = ItemStore.open(str(td / "l.db"))
        roots.register(s, "Books", root)
        roots.scan(s, "Books")

        def boom(path, **kw):
            raise RuntimeError("extraction exploded")

        rep = enrich.enrich_pass(s, enricher=boom)
        check(rep.failed == 1 and rep.enriched == 0, f"ladder crash swallowed, {rep}")
        pdf = s.get(s.id_of(str(root / "boom.pdf")))
        check(pdf.caption is None, "no caption written on failure (left for retry)")
        s.close()


def main() -> int:
    for fn in (test_isbn_checksums, test_isbn_extraction, test_parse_filename,
               test_lookup_open_library, test_lookup_falls_back_to_google,
               test_lookup_both_miss, test_ladder_born_digital,
               test_ladder_scanned_ocr, test_ladder_ocr_disabled,
               test_ladder_no_isbn_filename_fallback, test_ladder_embedded_over_stem,
               test_ladder_online_off, test_ladder_last_resort_stem,
               test_compose_caption, test_enrich_pass, test_enrich_pass_fail_soft):
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\nlibrarian Phase 6 — all {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        raise SystemExit(1)
