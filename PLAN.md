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

## Phase 3 — storage backends + filetype routing  ✅ *(done 2026-06-26)*
- ✅ `backends/base.py` — `StorageBackend` Protocol (`store/fetch/verify/exists`)
  + `Locator` + `BackendError`/`BackendUnavailable`. The verify-semantics split
  (hash-verifying `durable` backends vs Telegram presence-only) is the contract
  that gates offload.
- ✅ `backends/local.py` — content-addressed `LocalBackend` (durable; the
  external-disk tier and the test reference). `backends/rclone.py` — guarded
  `rclone` CLI wrapper (durable; any cloud). `backends/telegram.py` — own
  Telethon session, sync-over-async, presence-only verify, lazy `telethon`
  import + guard (oversize → BackendError; splitting a documented TODO).
- ✅ `backends/registry.py` — name→backend, `available()` filter, `is_durable()`.
  `routing.py` — `bucket()` + `RoutingPolicy` from `[backup.routing]` in
  config.toml (stdlib `tomllib`, no dep); default `["gdrive","telegram"]`.
- ✅ **Verify:** `PYTHONPATH=librarian python3 librarian/tests/test_backends.py`
  — 28 checks: LocalBackend store→exists→verify→fetch with hash integrity +
  tamper detection + idempotence, routing bucket/policy/config-load, registry
  get/has/available/durability, Telegram guards + flag. `rclone` round-trip runs
  for real via a local-path remote when the binary is present (skipped here).
  **All passing**; backends import without eagerly loading telethon.
- ⤳ *Deferred to Phase 4 (the consumer):* the fan-out backup PASS that iterates
  a file's routed backends, records a `locations` row each, transitions
  `pending → backed_up`, and parallelizes so the Telegram fast tier isn't gated
  on slow cloud completion. Telegram part-splitting + `group_key` reassembly.

## Phase 4 — backup fan-out + offload (HSM)  ✅ *(done 2026-06-26)*  *the dangerous delete*
- ✅ `backup.py` — `backup_item`/`backup_pass`: stores a PENDING item to every
  routed+available backend CONCURRENTLY (fast tier not gated on slow cloud), DB
  writes back on the main thread; durable copies verified right after store;
  records a `locations` row each; `pending → backed_up` only when all available
  backends hold it. Partial failure → mark_failed (attempts++, retry, then
  FAILED at cap); successes kept so retries resume without double-storing.
- ✅ `deletion.py` — vendored `DeletionGuard` (single unlink chokepoint, optional
  `protect` predicate, never raises). `offload.py` — `offload_item`/`offload_pass`
  with the integrity gate: reclaim local file ONLY after a **durable,
  non-Telegram** backend verifies the bytes LIVE (re-verified immediately before
  unlink — never a stale `verified_at`); set `OFFLOADED`, no placeholder.
  Crash-safe/idempotent (file already gone + durable verified → converges).
  Age- and disk-pressure-based selection (oldest first). `worker.run_once`
  orchestrates backup-then-offload.
- ✅ **Verify:** `PYTHONPATH=librarian python3 librarian/tests/test_backup.py` —
  33 checks: fan-out (durable verified_at vs presence-only), partial-fail
  retry+resume, max-retries→FAILED, **offload happy path (file gone, OFFLOADED,
  no marker), Telegram-only NEVER offloaded, corruption of the durable copy
  blocks the delete, crash-safe convergence, guard refusal, dry-run, age
  filter.** **All passing.** Suite total: 137 checks.

## Phase 5 — librarian bot + retrieval  ✅ *(done 2026-07-08)*
- ✅ `schema.py` migration **v1**: external-content FTS5 `items_fts` over
  `title/caption/path/upload_date` (tags travel inside the caption), kept in
  lock-step with `items` by AFTER INSERT/UPDATE/DELETE triggers; `'rebuild'`
  back-fills rows that predate the migration. `SCHEMA_VERSION` 0→1; the
  forward-only runner applies it once and the newer-DB guard still fires.
- ✅ `store.search()` + `_fts_match()` — user text is never passed to `MATCH`
  verbatim: each token → a quoted prefix phrase (`"tok"*`), internal quotes
  doubled, operator-only queries → no rows; fail-soft (any FTS error → `[]`).
- ✅ `bot.py` — retrieval logic as plain, fakeable functions: `find` (FTS5),
  `telegram_location`/`serve` (forward the presence-only TG copy inline, no
  download), `restore` (durable backends first then TG fallback; fetch →
  `~/Downloads`, **re-hash the written file against `content_hash`** — a corrupt
  copy is discarded and the next backend tried; `OFFLOADED → backed_up` only on a
  verified restore). `LibrarianBot` is the thin Telethon wiring (own MTProto
  session, single-flight, lazy `telethon` import) mapping `/find /serve /restore`.
- ✅ **Verify:** `python3 tests/test_bot.py` — 33 checks: FTS sanitizer, find by
  filename/folder/caption + trigger re-index on edit/delete + prefix, serve
  location pick, **restore happy path (bytes in Downloads, OFFLOADED→BACKED_UP),
  corrupt durable copy skipped→TG fallback, all-corrupt→verify_failed (no file
  left, status untouched), no-location, fetch-failed.** Plus a manual check that a
  hand-built v0 DB back-fills its FTS index on upgrade. **All passing.** Suite
  total: 170 checks.

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
