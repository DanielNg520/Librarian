"""
librarian.captioning.isbn
─────────────────────────
Pure ISBN logic — extraction from free text and checksum validation. No network,
no file I/O, no dependencies, so it's the trustworthy core the book ladder builds
on: only a checksum-VALID number is ever handed to an online lookup, so OCR noise
and stray digit runs on a copyright page can't send us chasing a bogus record.

ISBN-10:  9 digits + check (0-9 or X), weights 10..1, sum % 11 == 0.
ISBN-13:  12 digits + check, alternating weights 1/3, sum % 10 == 0 (EAN-13).

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import re

# Candidate runs on a page: "ISBN 978-0-13-468599-1", "ISBN-10 0134685997", or a
# bare grouped number. We grab generous digit/sep/x runs and validate downstream;
# the checksum — not the regex — is the real gate.
_CANDIDATE = re.compile(
    r"(?<![0-9Xx])"                       # not mid-number
    r"(97[89][\-\s]?(?:[0-9][\-\s]?){9}[0-9]"   # ISBN-13 shape
    r"|(?:[0-9][\-\s]?){9}[0-9Xx])"       # ISBN-10 shape
    r"(?![0-9Xx])",
)


def normalize(raw: str) -> str:
    """Strip ISBN separators (hyphens, spaces) and upper-case the X check digit."""
    return re.sub(r"[\s\-]", "", raw).upper()


def is_valid_isbn10(s: str) -> bool:
    s = normalize(s)
    if len(s) != 10 or not re.fullmatch(r"[0-9]{9}[0-9X]", s):
        return False
    total = 0
    for i, ch in enumerate(s):
        val = 10 if ch == "X" else int(ch)
        total += val * (10 - i)
    return total % 11 == 0


def is_valid_isbn13(s: str) -> bool:
    s = normalize(s)
    if len(s) != 13 or not s.isdigit():
        return False
    total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(s))
    return total % 10 == 0


def is_valid(s: str) -> bool:
    return is_valid_isbn13(s) or is_valid_isbn10(s)


def isbn10_to_13(s: str) -> str | None:
    """Convert a valid ISBN-10 to its ISBN-13 (978 prefix). None if invalid."""
    s = normalize(s)
    if not is_valid_isbn10(s):
        return None
    core = "978" + s[:9]
    check = (10 - sum((1 if i % 2 == 0 else 3) * int(c)
                      for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def find_isbns(text: str) -> list[str]:
    """All checksum-VALID ISBNs in `text`, normalized to ISBN-13, de-duplicated in
    first-seen order. ISBN-10s are up-converted so callers key on one form. Invalid
    candidates (OCR noise, random digit runs) are dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _CANDIDATE.finditer(text or ""):
        cand = normalize(m.group(1))
        if len(cand) == 13 and is_valid_isbn13(cand):
            code = cand
        elif len(cand) == 10 and is_valid_isbn10(cand):
            code = isbn10_to_13(cand) or cand
        else:
            continue
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out
