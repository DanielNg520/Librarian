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

## Phase 6 — book enrichment (async, fail-soft)  ✅ *(done 2026-07-08)*
- ✅ `captioning/isbn.py` — pure, dependency-free ISBN core: ISBN-10/13 checksum
  validation, ISBN-10→13 up-convert, and `find_isbns()` (regex candidates gated by
  CHECKSUM, normalized to 13, de-duped) so OCR noise never triggers a bogus lookup.
- ✅ `captioning/book.py` — the ISBN ladder, every rung guarded/injectable:
  embedded metadata (`pypdf`), text extract (`pypdf`→`pdfminer.six`), OCR
  (`pdf2image`+`pytesseract`), online lookup (Open Library → Google Books, **stdlib
  urllib — no `requests`**, only the ISBN leaves), filename parse
  (`Author - Title (Year)`), raw-stem last resort. Precedence ISBN > embedded >
  filename > stem. `compose_book_caption()` layers folder tags on identically to
  photos. A missing optional dep disables just that rung (logged once), never raises.
- ✅ `enrich.py` — the async, fail-soft pass (`enrich_item`/`enrich_pass`): iterates
  document-bucket books lacking a caption, writes `items.title` + `items.caption`
  (re-indexed by the FTS trigger from Phase 5), swallows any per-book failure. Kept
  OUT of ingest/backup so slow network+OCR never gate discovery or delivery.
- ✅ Default: OCR on, online ISBN lookup on, model fallback off (not implemented —
  the deferred opt-in). Optional deps declared as `[project.optional-dependencies]`
  extras (`telegram`, `books`, `books-ocr`).
- ✅ **Verify:** `python3 tests/test_book.py` — 43 checks (all optional deps ABSENT,
  via injection): checksum edges incl. X, extraction dedup + noise rejection,
  filename ladder, Open Library hit + Google Books fallback + both-miss/garbage,
  **born-digital → ISBN, scanned → OCR → ISBN, ocr/online toggles, no-ISBN →
  filename, embedded > stem, raw-stem last resort**, caption composition with
  layered tags, and the pass (only document-books enriched, back-write + FTS
  findable, idempotent skip, ladder-crash swallowed). **All passing.** Suite
  total: 213 checks.

## Phase 7 — generalize captions + close the send seam  ✅ *(done 2026-07-18)*
The composers existed but were unwired: `compose_caption` (photos) was only
tested, never called, and `TelegramBackend.store()` sent `send_file(dest, path)`
with NO caption. This phase makes the folder taxonomy caption ANY file type and
makes that caption actually ride the Telegram upload. Pure-logic first, then one
back-compatible backend seam.
- ✅ `captioning/_compose.py` — the shared caption spine: `path_segments`,
  `description`, `segment_tags`, `merge_tags`, `display_date` (mtime), and
  `folder_lines()` (the `description` + `#hashtags` pair). photo/book/generic all
  delegate here, so the taxonomy→caption + slug rule can't drift. `photo.py` keeps
  its EXIF date and re-exports the neutral names (public API preserved); `book.py`
  now builds its lead lines then calls `folder_lines`.
- ✅ `captioning/generic.py` — `compose_generic_caption(path, root_path, *,
  resolver, base_tags)`: mtime date line + shared folder lines. Identical to a
  photo caption minus EXIF, so nothing ever ships caption-less.
- ✅ `captioning.compose(path, root_path, *, resolver, base_tags, book_caption)` —
  the ONE dispatcher the send path calls: photo → `compose_caption`; book →
  `book_caption` (the enriched `items.caption`) if present, else generic fallback;
  other → generic. Pure/testable — caller supplies root_path/tags/resolver.
- ✅ `backends/base.py` protocol + local/rclone/telegram — `store(path,
  content_hash, *, caption=None)`. Telegram threads `caption=` into `send_file`;
  durable backends accept-and-ignore (bytes are the payload). Default None → fully
  back-compatible.
- ✅ `backup.py` — `_caption_for(store, item)` composes ONCE at send (so a later
  `.tags`/folder-move edit is reflected), fail-soft to None (never blocks
  delivery), passed through `_safe_store` to every backend's `store()`.
- ✅ **Verify:** `PYTHONPATH=. python3 tests/test_send_caption.py` — 14 checks:
  generic 3-line shape + all-digit-segment slug drop, layered base/segment/sidecar
  tags, dispatcher routing (photo == compose_caption, book-enriched verbatim,
  book-unenriched → generic, other → generic), send seam (composed caption reaches
  the fake Telegram store exactly once + durable copy still stored), and fail-soft
  (unknown root → None, no raise). Existing test fakes updated to the new `store`
  signature. **All passing.** Suite total: 251 checks.

## Phase 8 — standalone dedup pass + protection policy + no-dup-upload  ✅ *(done 2026-07-18)*
Ported the suite's dedup + safebrake, OPTIMIZED for Librarian: every row already
carries `content_hash`, so grouping is DB-driven and disk hashing runs only on
untracked stragglers. Winner stays Librarian's simplified rule.
- ✅ `dedup.py` — `dedup_root(store, guard, root, *, dry_run=True)` + `dedup_pass`
  (all roots). A cheap SIZE prefilter isolates candidates; each candidate's full
  hash is REUSED from its `items` row when tracked, computed only for untracked
  strays (the suite's separate partial stage is redundant once the DB carries full
  hashes). Groups by hash, `_pick_winner` (tracked → earliest-discovery → path)
  makes the survivor deterministic across re-scans. Emits `DedupReport` (dry-run =
  PLANNED counts + `bytes_freed`). **INVARIANT proven in code+tests:** a tracked
  row always outranks an untracked file, so a backed-up row is never orphaned —
  there is no adopt-the-orphan case (the suite needed one; Librarian doesn't).
- ✅ `deletion.py` — `ProtectionPolicy` (a plain callable: `pause` blocks every
  delete; else protect a path under any registered-root folder / explicit prefix),
  `ProtectionPolicy.load(store, path)` reads `[protect]` from config.toml the same
  reload-on-load way `RoutingPolicy.load` does — NO separate PolicyStore process.
  `DeletionGuard(policy=…)` (or legacy `protect=…`) stays the one chokepoint;
  offload AND `dedup_root` route through it. Default = nothing protected.
- ✅ Avoid dup upload — `store.location_ref_for_hash(hash, backend, exclude_item)`;
  `backup_item` reuses an existing backend copy's locator instead of re-uploading
  byte-identical content (content-addressed durable refs + identical Telegram
  bytes are interchangeable). A reused DURABLE copy is still re-verified by the
  existing integrity gate, so a corrupt share can't be silently trusted.
- ✅ **Verify:** `PYTHONPATH=. python3 tests/test_dedup_pass.py` — 29 checks:
  tracked-twin collapse (dry-run predicts, live deletes loser row), untracked
  straggler removal + winner determinism across a re-run (no re-created dup),
  tracked-beats-untracked invariant (backup row + locations preserved), protection
  (prefix + pause + config `roots` resolution + absent config), dedup respects the
  guard on a protected root, dup-upload skip (no second `store()`, both locations
  still recorded), and `dedup_pass` over all roots. ⚠ `dedup_root` only ever
  removes a REDUNDANT local copy while an identical one remains — distinct from
  offload, so it needs no durable-backup gate. **All passing.** Suite total: 280.

## Phase 9 — iCloud-aware ingest  ✅ *(done 2026-07-18)*
Before this, an evicted `.name.icloud` stub was hidden→invisible (never backed
up), and a dataless file got materialized ACCIDENTALLY by the hash read (blocking,
metered, re-filling the disk offload just freed). The download/skip decision is
now explicit, and HSM no longer fights iCloud. Every OS probe is injected, so the
whole phase tests off macOS.
- ✅ `icloud.py` (pure, guarded) — `placeholder_state(path, *, stat_fn)` →
  `MATERIALIZED | DATALESS | EVICTED_STUB` (stub = `.…​.icloud` by name; dataless
  via `st_blocks==0 && st_size>0`, the reliable no-pyobjc signal; unstattable →
  fail-open to MATERIALIZED so a real file is never wrongly hidden). Plus
  `is_stub`, `original_name`/`original_path`, `is_evicted`, and
  `materialize(path, …)` (`brctl download` + bounded poll; runner/state/clock/sleep
  all injectable; never raises — fails soft to False on a missing `brctl` or a
  timeout).
- ✅ `roots.scan(…, icloud_policy="report_only")` — classifies each path (STUBS
  included, before the hidden-file filter that used to lose them) BEFORE any
  content read. `report_only` (default, safe) surfaces evicted files in the new
  `ScanReport.cloud_only` and never downloads; `materialize` downloads then
  ingests the real file; `skip` ignores them silently. An invalid policy raises.
- ✅ `offload.py` — `offload_item` returns the new `CLOUD_MANAGED` outcome and
  leaves an already-evicted file in place: there's no local disk to reclaim, and
  unlinking the placeholder could delete the file from iCloud Drive across every
  device — so HSM can't loop download→hash→offload→evict→download.
- ✅ **Verify:** `PYTHONPATH=. python3 tests/test_icloud.py` — 26 checks: stub name
  parsing, dataless/materialized/stub/fail-open classification (injected stat),
  `materialize` success/fail-soft/timeout (injected runner + clock, no real sleep),
  the three scan policies (report_only surfaces + never reads, skip silent,
  materialize→ingest), stub no-longer-invisible, and offload refusing to evict a
  cloud-managed file (status + file preserved). **All passing.** Suite total: 306.

## Phase 10 — full-cycle orchestration (facade)  ✅ *(done 2026-07-18)*
Phases 7–9 left the passes hand-composed; this wires every pass into ONE facade
so a daemon/cron line gets the whole machine, each stage fail-soft.
- ✅ `worker.full_cycle(store, registry, policy, guard, …)` → `CycleReport` —
  stages in dependency order **heal → scan → enrich → dedup → backup → offload**
  (heal re-arms for the SAME cycle's backup; enrich runs BEFORE backup so the
  first upload already carries the ISBN caption; dedup collapses before ship;
  offload last). Each stage runs through `_stage` (fail-soft: a crash is logged
  + recorded in `CycleReport.errors`, later stages still run — a scan hiccup
  must never stop backup from shipping). Defaults = the safe automation set
  (heal/scan/enrich/backup on; the DELETING stages dedup/offload opt-in;
  iCloud `report_only`). `run_once` unchanged (back-compat).
- ✅ pytest bridge parity — `tests/conftest.py` gains `patch_attr` +
  `monkeypatch_state` fixtures mirroring the script harness, so every test file
  passes under BOTH `python3 tests/test_X.py` and `pytest`.
- ✅ **Verify:** `PYTHONPATH=. python3 tests/test_worker.py` — full_cycle
  end-to-end (discover → enrich-before-send → twin collapse → fan-out →
  durable-verified offload, then an idempotent second cycle) and stage-crash
  containment (heal explodes → recorded, backup still ships). **All passing.**
  Suite total: 316 script checks / 84 pytest tests.

## Phase 11 — bootstrap + CLI (`librarian` command)  ✅ *(done 2026-07-18)*
The library becomes a tool: config → wired objects, and a thin argparse layer.
- ✅ `bootstrap.py` — the `from_config` factory the registry docstring promised:
  `registry_from_config` builds `[backends.*]` (local / rclone / telegram — the
  Telegram backend starts its OWN Telethon session and refuses unauthorized ones
  with a pointer to `telegram-login`); each backend constructs FAIL-SOFT (missing
  dep / bad section / typo'd type → loud skip; routed items stay PENDING).
  `assemble(store)` → (Registry, RoutingPolicy, DeletionGuard-with-Protection)
  from one config read — CLI, daemon, and scripts wire identically.
- ✅ `cli.py` + `__main__.py` + `[project.scripts] librarian` — THIN by rule (open
  store → bootstrap → call ONE library function → print its report): `root
  add/list/remove`, `scan [ROOT] [--icloud …]`, `cycle [--dedup] [--offload]
  [--dry-run] [--no-heal/scan/enrich]`, `dedup` (dry-run unless `--live`),
  `status`, `find`, `telegram-login` (one-time interactive auth). Safety posture
  = library defaults: deleting stages opt-in, iCloud report_only. Paths honor
  `$LIBRARIAN_DB` / `$LIBRARIAN_CONFIG`.
- ✅ **Verify:** `PYTHONPATH=. python3 tests/test_cli.py` — 23 checks: fail-soft
  backend construction (well-formed survives; unknown type / missing key /
  wrong-typed value are skips; missing config → empty registry), assemble wiring
  (routing + protection-guard actually refuse), CLI end-to-end via `main(argv)`
  (root add/list/remove exit codes, scan ingests, status counts + honest
  no-backends warning, find hit/miss, cycle collapses a byte-twin + ships,
  `--offload` reclaims the file, dedup announces dry-run, malformed config never
  crashes a command). Plus a live `python -m librarian` smoke: ingest → enrich →
  backup → heal → offload → disk reclaimed. **All passing.** Suite total: 339
  script checks / 89 pytest tests.

## Cross-cutting
- ✅ `DESIGN.md` rewritten as the as-built spec (2026-07-18): full-cycle
  pipeline, as-built module map, self-healing stance, invariants incl. Phases
  7–11. `README.md` carries the phase table, quick start, and full config
  reference. ADR-0001 marked Accepted.
- ✅ Each vendored file names its suite origin so future drift is auditable.
- ⬚ Keep all three current as future phases land (daemon wrapper, `librarian
  bot` entry, Telegram part-splitting are the queued ones — DESIGN §8).

## Order rationale
0 first (zero-risk pure logic, proves the package). 1 (own spine) before anything
touching files. 3 (backends) before 4 (offload) so the delete only happens once
fan-out + verify are trustworthy. 5 is the payoff; 6 is independent.

7 before 8/9: it closes an already-open gap (composers wired to nothing) and is
the most self-contained (pure functions + one back-compatible backend signature).
8 builds on the dedup already present and hardens deletes before 9 leans on them.
9 last: most macOS-environment-dependent, so it ships with the most careful
injection-based testing once the delete/protection paths are trustworthy.
