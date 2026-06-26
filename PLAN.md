# Librarian — implementation plan

> Build sequence for `librarian/DESIGN.md` (decisions in `docs/adr/0001`).
> Librarian is an **independent app** (own DB, own Telegram session, own folders)
> that **vendors copied code** from the suite — never imports it. Ordered so each
> phase is independently shippable and verifiable, lowest-risk first.
> Per-phase rules: additive schema only, fail-soft, never block delivery, route
> deletes through the (vendored) `DeletionGuard`, stay a single-flight TG talker.

Legend: ⬚ not started · ✅ done · ⚠ risk to watch.

---

## Phase 0 — project skeleton + folder hashtags  ✅ *(done 2026-06-26)*
Pure caption logic, zero DB/network — fully testable in isolation, drops into the
send seam later. Establishes the standalone package.
- ✅ `librarian/pyproject.toml`, `librarian/librarian/__init__.py` — standalone
  package, no dependency on `core` (coupling grep is clean; imports from a
  neutral cwd with only `librarian` on the path).
- ✅ `librarian/librarian/tags.py` — `.tags` sidecar resolver: slug rule
  (lowercase, non-alnum→`_`, never `-`, drop empty/all-digit), inheritance up to
  a registered root boundary, append-to-caption with word-boundary de-dup, mtime
  hot-reload cache.
- ✅ `librarian/tests/test_tags.py` — 31 checks: slug edges, multi-tag lines +
  comments, inheritance order, root boundary (no read outside), apply de-dup
  (incl. `#beaches`≠`#beach`), mtime reload. **All passing.**
- ✅ **Verify:** `PYTHONPATH=librarian python3 librarian/tests/test_tags.py`.
- ✅ Hyphen-in-tag becomes `_`; the walk never reads a `.tags` above the root.

## Phase 1 — vendored core + own DB  ✅ *(done 2026-06-26)*
Copy-and-adapt the proven spine into Librarian; stand up `librarian.db`.
- ✅ Vendored `hashing`, `stability`, `dedup` (simplified winner — no
  canonical/sidecar signals), `paths` (own `$LIBRARIAN_DB` namespace). Each file
  names its suite origin. (`heartbeat` deferred to when workers exist — Phase 4/5.)
- ✅ `schema.py` — `items` (path-keyed) + `locations` (item,backend) + `roots` +
  `metadata`; WAL pragmas + the forward-only versioned migration runner
  (`SCHEMA_VERSION=0`, scaffold ready). `models.py` — `Status`
  (`pending/backed_up/offloaded/failed`) + `Item` + `Location`. `store.py` —
  items/locations/roots accessors. `ingest.py` — template
  (stabilize→hash→dedup→insert), universal `content_hash`, dedup-collapse + adopt.
- ✅ `roots.py` — register a human-named folder + idempotent recursive `scan`
  that ingests stable files (hidden `.tags` skipped).
- ✅ **Verify:** `PYTHONPATH=librarian python3 librarian/tests/test_ingest.py` —
  31 checks: fresh+reopen schema, insert + universal hash, byte-dup collapse
  (one row, dup file removed), already-known, unstable/missing skipped, locations
  upsert + CASCADE, root register + idempotent scan + bad-name/dup rejection.
  **All passing.** Standalone import + coupling grep both clean.
- ⚠ Fresh DB — no migration from `suite.db`; the two stay fully separate.

## Phase 2 — captions (folder taxonomy + Phase 0 tags)  ✅ *(done 2026-06-26)*
- ✅ `exif.py` — dependency-free JPEG/Exif `DateTimeOriginal` reader (no Pillow);
  malformed/non-JPEG/missing → None, never raises.
- ✅ `captioning/photo.py` — `timestamp()` (EXIF for photos → mtime fallback);
  `description()` = path segments below the root joined with ` · `;
  `segment_tags()` = each segment slugified (shared slug rule with `tags.py`);
  `compose_caption()` = date · description · layered tags, unioning root base
  tags + segment tags + `.tags` sidecars (Phase 0), de-duplicated in layer order.
- ✅ EXIF → `upload_date` stamped at ingest (`register_file(upload_date=…)`,
  `roots.scan` computes it per file).
- ⬚ Generalize grouping to the full subpath (album per leaf folder) — deferred to
  the send path (Phase 3), where `group_key` is actually consumed.
- ✅ **Verify:** `PYTHONPATH=librarian python3 librarian/tests/test_captioning.py`
  — 14 checks incl. a hand-built JPEG+EXIF blob, the exact
  `Photos/selfie/bathroom selfie/outdoor/IMG.jpg` →
  `2024-08-14 18:32 / selfie · bathroom selfie · outdoor / #selfie
  #bathroom_selfie #outdoor` case, sidecar+base-tag merge, EXIF-sentinel→mtime
  fallback, and upload_date stamping. **All passing.**

## Phase 3 — storage backends + filetype routing
- ⬚ `backends/` Protocol; `telegram.py` (own Telethon session: send +
  `download_media`; part-split internal, reassemble via `group_key`);
  `rclone_backend.py` (Drive/Box/… via the `rclone` CLI); `registry.py`.
- ⬚ Filetype → backend list config (media-bucket keyed); default
  `["gdrive","telegram"]`. Startup guards for `rclone` + any missing dep.
- **Verify:** a file lands in a cloud remote *and* Telegram with two `locations`
  rows; `rclone check` passes; Telegram send is its own single-flight talker.
- ⚠ Don't gate the Telegram fast tier on slow cloud completion.

## Phase 4 — backup fan-out + offload (HSM)  *the dangerous delete*
- ⬚ `worker.py` backup pass: for each routed backend `store()` + record a
  `locations` row; partial failure retryable, never loses the item.
- ⬚ Offload pass (age/disk-pressure): reclaim local file ONLY after a **durable,
  non-Telegram** backend `verify()`s present; unlink via `DeletionGuard`; set
  `OFFLOADED`; no placeholder. Idempotent + crash-safe (re-verify before unlink).
- **Verify:** force offload on a cloud-verified file → gone + `OFFLOADED`, no
  marker; a Telegram-only file is **never** offloaded.
- ⚠ Re-verify immediately before unlink; never trust a stale `verified_at`.

## Phase 5 — librarian bot + retrieval
- ⬚ `bot.py` (own MTProto session): `find` (FTS5 over
  `title/caption/path/upload_date/tags`), `serve` (forward stored TG message
  inline), `restore` (best backend `fetch()` → `~/Downloads`, re-verify
  `content_hash`, `OFFLOADED → backed_up`).
- **Verify:** offload → `find` → `restore`; bytes match by `content_hash`, lands
  in Downloads.

## Phase 6 — book enrichment (async, fail-soft)
- ⬚ `captioning/book.py` + async pass (document bucket): embedded metadata →
  text/OCR ISBN regex+checksum → Open Library / Google Books → filename ladder.
  Startup guards for `pypdf`/`pdfminer.six`, `tesseract`+`pdf2image`, `requests`.
- ⬚ Default: OCR on, online ISBN lookup on (only the number leaves), model
  fallback off.
- **Verify:** born-digital + scanned PDFs both resolve title/author/ISBN; no-ISBN
  falls back to filename, no crash.

## Cross-cutting
- ⬚ Keep `librarian/DESIGN.md` + this plan current as phases land; maintain a
  `librarian/README.md` once the binary exists.
- ⬚ Each vendored file names its suite origin so future drift is auditable.

## Order rationale
0 first (zero-risk pure logic, proves the package). 1 (own spine) before anything
touching files. 3 (backends) before 4 (offload) so the delete only happens once
fan-out + verify are trustworthy. 5 is the payoff; 6 is independent.
