"""
librarian.captioning.book
─────────────────────────
Deterministic-first identification of a book (PDF/EPUB) for its caption, via an
ISBN-driven extraction ladder. Runs in a SEPARATE, fail-soft enrichment pass (not
inline ingest or the send loop) because network + OCR are slow — see the pass in
librarian.enrich. Every rung degrades gracefully; the pass must never crash a
scan or block delivery.

THE LADDER (ADR-0001 D3), authoritative first, cheap fallback last:
  1. Embedded PDF metadata (/Title, /Author)         — a baseline, never trusted
                                                        alone.
  2. Extract text (first ~10 + last few pages), regex + CHECKSUM-validate the
     ISBN. Scanned/image-only PDFs: OCR those pages first.
  3. ISBN → Open Library (free, no key) → Google Books fallback → canonical
     title/author/year/publisher. ONLY THE ISBN NUMBER leaves the machine.
  4. Filename parse ("Author - Title (Year).pdf") as cross-check / fallback.
  5. Raw filename stem as the last resort.

Precedence when composing the final record: ISBN lookup > embedded > filename >
stem. `title` reuses items.title; author/year/publisher/ISBN compose into the
caption (searchable in-chat AND via FTS); folder tags layer on exactly as for
photos.

DEPENDENCIES ARE ALL OPTIONAL AND GUARDED. pypdf/pdfminer.six (text),
pdf2image+pytesseract/tesseract (OCR) are imported lazily; a missing one disables
just that rung (logged once) rather than raising. Online lookup uses stdlib
urllib — no `requests` dependency. Every extractor is also injectable, so the
ladder is fully testable without any of these installed.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import isbn as isbn_mod
from ..tags import TagResolver, slugify_tag
from .photo import _merge_tags, description, segment_tags

log = logging.getLogger(__name__)

BOOK_EXTENSIONS = frozenset({".pdf", ".epub", ".mobi", ".azw3", ".djvu"})

# Pages to sample: ISBNs live on the copyright page (front) and back cover.
FIRST_PAGES = 10
LAST_PAGES = 3
_LOOKUP_TIMEOUT_S = 6.0

_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.info("book: %s", msg)


def is_book(path) -> bool:
    return Path(path).suffix.lower() in BOOK_EXTENSIONS


# ── the identified record ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class BookMetadata:
    title:     str | None = None
    author:    str | None = None
    year:      str | None = None
    publisher: str | None = None
    isbn:      str | None = None
    source:    str = "unknown"       # isbn | embedded | filename | stem

    def _fill(self, other: "BookMetadata") -> "BookMetadata":
        """Return self with any None field filled from `other` (self wins)."""
        return replace(
            self,
            title=self.title or other.title,
            author=self.author or other.author,
            year=self.year or other.year,
            publisher=self.publisher or other.publisher,
            isbn=self.isbn or other.isbn,
        )


# ── rung 1: embedded metadata ─────────────────────────────────────────────────
def embedded_metadata(path) -> BookMetadata:
    """PDF /Title + /Author via pypdf, if installed. Empty record otherwise."""
    try:
        import pypdf
    except ImportError:
        _warn_once("pypdf", "pypdf not installed — embedded metadata disabled")
        return BookMetadata()
    try:
        info = pypdf.PdfReader(str(path)).metadata or {}
        title = (info.get("/Title") or "").strip() or None
        author = (info.get("/Author") or "").strip() or None
    except Exception as e:                        # pypdf raises a broad set
        log.debug("book: embedded metadata failed on %s: %s", path, e)
        return BookMetadata()
    return BookMetadata(title=title, author=author,
                        source="embedded" if title else "unknown")


# ── rung 2: text / OCR extraction ─────────────────────────────────────────────
def extract_text(path, *, first: int = FIRST_PAGES, last: int = LAST_PAGES) -> str:
    """Text of the first `first` + last `last` pages. pypdf, then pdfminer.six as a
    fallback; "" if neither is installed or extraction fails (→ OCR may follow)."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        n = len(reader.pages)
        idxs = sorted(set(range(min(first, n))) | set(range(max(0, n - last), n)))
        return "\n".join((reader.pages[i].extract_text() or "") for i in idxs)
    except ImportError:
        pass
    except Exception as e:
        log.debug("book: pypdf text extract failed on %s: %s", path, e)
    try:
        from pdfminer.high_level import extract_text as _pm
        return _pm(str(path), maxpages=first) or ""
    except ImportError:
        _warn_once("text", "pypdf/pdfminer not installed — text extraction disabled")
    except Exception as e:
        log.debug("book: pdfminer text extract failed on %s: %s", path, e)
    return ""


def ocr_text(path, *, first: int = FIRST_PAGES, last: int = LAST_PAGES) -> str:
    """OCR the sampled pages for scanned/image-only PDFs (pdf2image + pytesseract).
    "" if either dependency (or the tesseract binary) is missing/failing."""
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        _warn_once("ocr", "pdf2image/pytesseract not installed — OCR disabled")
        return ""
    try:
        images = convert_from_path(str(path), first_page=1, last_page=first)
        return "\n".join(pytesseract.image_to_string(im) for im in images)
    except Exception as e:                        # tesseract-missing, poppler, etc.
        _warn_once("ocr-run", f"OCR unavailable ({e})")
        return ""


# ── rung 3: online lookup (only the ISBN leaves) ──────────────────────────────
def _http_get(url: str) -> str | None:
    """GET `url` → body text, or None on any error. stdlib only; fail-soft."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "librarian/0.0"})
        with urllib.request.urlopen(req, timeout=_LOOKUP_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.debug("book: lookup GET failed for %s: %s", url, e)
        return None


def _parse_open_library(body: str, isbn13: str) -> BookMetadata | None:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    rec = data.get(f"ISBN:{isbn13}") if isinstance(data, dict) else None
    if not rec:
        return None
    authors = ", ".join(a.get("name", "") for a in rec.get("authors", [])) or None
    year = None
    m = re.search(r"\d{4}", rec.get("publish_date", "") or "")
    if m:
        year = m.group(0)
    pubs = rec.get("publishers") or []
    publisher = (pubs[0].get("name") if pubs and isinstance(pubs[0], dict)
                 else None)
    title = (rec.get("title") or "").strip() or None
    if not title:
        return None
    return BookMetadata(title=title, author=authors, year=year,
                        publisher=publisher, isbn=isbn13, source="isbn")


def _parse_google_books(body: str, isbn13: str) -> BookMetadata | None:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not items:
        return None
    info = items[0].get("volumeInfo", {})
    title = (info.get("title") or "").strip() or None
    if not title:
        return None
    authors = ", ".join(info.get("authors", [])) or None
    year = None
    m = re.search(r"\d{4}", info.get("publishedDate", "") or "")
    if m:
        year = m.group(0)
    return BookMetadata(title=title, author=authors, year=year,
                        publisher=(info.get("publisher") or None),
                        isbn=isbn13, source="isbn")


def lookup_isbn(isbn13: str, *,
                fetcher: Callable[[str], str | None] | None = None
                ) -> BookMetadata | None:
    """Resolve a checksum-valid ISBN-13 to canonical metadata: Open Library first
    (free, no key), Google Books as fallback. `fetcher(url)->body|None` is
    injectable (tests pass a fake; default is stdlib HTTP). None if both miss."""
    get = fetcher or _http_get
    ol = get(f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn13}"
             f"&format=json&jscmd=data")
    if ol:
        meta = _parse_open_library(ol, isbn13)
        if meta:
            return meta
    gb = get("https://www.googleapis.com/books/v1/volumes?q=isbn:"
             + urllib.parse.quote(isbn13))
    if gb:
        return _parse_google_books(gb, isbn13)
    return None


# ── rung 4: filename parse ────────────────────────────────────────────────────
_FILENAME_RE = re.compile(
    r"^\s*(?P<author>.+?)\s+-\s+(?P<title>.+?)\s*(?:\((?P<year>\d{4})\))?\s*$")


def parse_filename(path) -> BookMetadata:
    """"Author - Title (Year).pdf" → metadata. Falls back to the raw stem as the
    title when it doesn't match the pattern."""
    stem = Path(path).stem
    m = _FILENAME_RE.match(stem)
    if m and m.group("title"):
        return BookMetadata(title=m.group("title").strip(),
                            author=m.group("author").strip() or None,
                            year=m.group("year"),
                            source="filename")
    return BookMetadata(title=stem.strip() or None, source="stem")


# ── the ladder ────────────────────────────────────────────────────────────────
def enrich(
    path,
    *,
    online: bool = True,
    ocr: bool = True,
    fetcher: Callable[[str], str | None] | None = None,
    text_extractor: Callable[..., str] | None = None,
    ocr_extractor: Callable[..., str] | None = None,
    embedded_reader: Callable[..., BookMetadata] | None = None,
) -> BookMetadata:
    """Run the full identification ladder for one book, fail-soft. Every extractor
    is injectable for testing; defaults use the guarded lazy-import rungs above.
    Always returns a record — worst case, title = the filename stem."""
    path = Path(path)
    read_embedded = embedded_reader or embedded_metadata
    read_text = text_extractor or extract_text
    read_ocr = ocr_extractor or ocr_text

    emb = read_embedded(path)
    fname = parse_filename(path)

    # rung 2: born-digital text → ISBN; scanned → OCR the same pages.
    text = read_text(path) or ""
    isbns = isbn_mod.find_isbns(text)
    if not isbns and ocr:
        isbns = isbn_mod.find_isbns(read_ocr(path) or "")

    # rung 3: authoritative lookup on the first ISBN that resolves.
    online_meta: BookMetadata | None = None
    if online and isbns:
        for code in isbns:
            online_meta = lookup_isbn(code, fetcher=fetcher)
            if online_meta:
                break

    # Compose by precedence: ISBN lookup > embedded > filename/stem. Whatever the
    # winner, still stamp a validated ISBN if we found one but the record lacks it.
    if online_meta is not None:
        meta = online_meta._fill(emb)._fill(fname)
    elif emb.title:
        meta = emb._fill(fname)
    else:
        meta = fname
    if meta.isbn is None and isbns:
        meta = replace(meta, isbn=isbns[0])
    if not meta.title:                            # absolute last resort
        meta = replace(meta, title=path.stem or None, source="stem")
    return meta


# ── caption composition ───────────────────────────────────────────────────────
def _meta_line(meta: BookMetadata) -> str:
    """`Author · Year · Publisher`, omitting the parts we don't have."""
    return " · ".join(p for p in (meta.author, meta.year, meta.publisher) if p)


def compose_book_caption(
    meta: BookMetadata,
    path,
    root_path,
    *,
    resolver: TagResolver | None = None,
    base_tags: "list[str] | tuple[str, ...]" = (),
) -> str:
    """Book caption: title, author/year/publisher, ISBN, folder description, and
    the same layered tag line photos get (root base tags + folder segments +
    `.tags` sidecars). Empty lines omitted."""
    lines: list[str] = []
    if meta.title:
        lines.append(meta.title)
    ml = _meta_line(meta)
    if ml:
        lines.append(ml)
    if meta.isbn:
        lines.append(f"ISBN {meta.isbn}")
    desc = description(path, root_path)
    if desc:
        lines.append(desc)

    tags = _merge_tags(
        (slugify_tag(b) for b in base_tags),
        segment_tags(path, root_path),
        resolver.tags_for(path) if resolver else (),
    )
    if tags:
        lines.append(" ".join(f"#{t}" for t in tags))
    return "\n".join(lines)
