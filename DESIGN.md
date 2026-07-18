# Librarian — design note (as built)

> Component-level working spec, kept in lockstep with the code. Decision record
> (why/options/trade-offs): `docs/adr/0001-librarian-multi-backend-hsm-and-folder-metadata.md`.
> Build history + per-phase verification: `PLAN.md`.
> Status: **Phases 0–11 built and tested** (339 script checks / 89 pytest tests).

## 0. Independence contract (read this first)

Librarian is an **independent application**, built to be lifted into its **own
repo**. The relationship to the Media Archiver Suite is **copy, never link**:

- **No `import core`**, no importing `archiver`/`recorder`/`dispatcher`. The suite
  is a *source of copyable patterns*, not a dependency. (Enforced: the coupling
  grep is part of release checks.)
- Proven suite code is **vendored** (copied in and adapted) under `librarian/`:
  the `ingest` template (stabilize→hash→dedup→insert), the `Sanitizer`/slug
  idea, the SQLite-WAL `ItemStore`, `DeletionGuard`, `hashing`/`dedup`,
  `paths`. Each copied file notes its origin so drift is auditable.
- **Own database** — `librarian.db` (SQLite/WAL, `$LIBRARIAN_DB`). No read or
  write of `suite.db`.
- **Own Telegram identity** — its own Telethon session/account, started by
  `bootstrap`. It is its own (and only) Telegram talker; single-flight, polite.
- **Own domain** — Librarian manages **the user's own folders** (Photos/, Books/,
  documents). The archiver suite keeps owning social-media downloads. No overlap.

## 1. Shape

| | |
|---|---|
| **Purpose** | Systemwide file manager: back up user folders to multiple durable backends (routed by filetype), keep Telegram as a fast-access tier, reclaim local disk once a durable copy is verified, retrieve on demand via a Telegram bot. Deterministic, folder/ISBN-derived captions for findability. |
| **Shape** | Standalone package + `librarian` CLI. One full-cycle worker function (`worker.full_cycle`) a scheduler calls, a Telegram bot, all coordinating through `librarian.db` — no IPC. |
| **Priorities** | Inherited stance: integrity > self-healing > seam robustness > efficiency. |

## 2. Pipeline (one `full_cycle`)

```
                    ┌── heal ── re-verify every durable claim LIVE; drop stale,
                    │           re-arm (→ PENDING) what can re-ship this cycle
user folders        ▼
(Photos/, Books/) ── scan ── iCloud classify (report_only|materialize|skip)
                    │        → ingest (stabilize → hash → dedup-collapse → insert)
                    ▼
                  enrich ── ISBN ladder writes items.title/caption (books)
                    │       BEFORE backup, so the first upload carries it
                    ▼
                  dedup* ── collapse redundant LOCAL byte-twins (DB-driven;
                    │       deterministic winner; via DeletionGuard)
                    ▼
                  backup ── routing (filetype → backends) → concurrent fan-out
                    │       caption composed AT SEND (photo/book/generic)
                    │       dup-upload skip: byte-identical content reuses the
                    │       existing locator; durable copies verify-after-store
                    ▼
                  offload* ── durable verify() RIGHT NOW → DeletionGuard →
                              Status.OFFLOADED (no placeholder; iCloud-evicted
                              files are CLOUD_MANAGED, never touched)

  bot: find (FTS5) · serve (inline TG) · restore (fetch → disk, re-verify hash)
  * = deleting stages, opt-in; every stage fail-soft (a crash is recorded in
      CycleReport.errors and the later stages still run)
```

## 3. Module map (`librarian/`) — as built

**Spine**
- `schema.py` / `models.py` / `store.py` — `librarian.db`: `items`, `locations`,
  `roots`, FTS5 index + triggers; WAL; forward-only migrations. `Status`:
  `pending → backed_up → offloaded` (+ `failed`, retry-capped).
- `hashing.py` — partial/full SHA-256 primitives (one definition, used everywhere).
- `stability.py` — hidden/incomplete-suffix/size-floor filters + stat-probe gate.
- `ingest.py` — the template method: stabilize → hash → dedup-collapse → insert.
  Every row carries `content_hash`; writing the row IS the enqueue.
- `roots.py` — named-folder registration + idempotent `scan` with the iCloud
  policy seam (`ScanReport.cloud_only` surfaces evicted files).
- `icloud.py` — placeholder/dataless classification (`st_blocks` signal), stub
  name mapping, `brctl`-driven `materialize`; every OS probe injectable.

**Metadata / captions**
- `tags.py` — `.tags` sidecars: Telegram-safe slug rule, root-bounded
  inheritance, mtime hot-reload (see §5).
- `captioning/_compose.py` — the ONE folder→caption spine (description +
  hashtag lines) shared by all types, so the rule can't drift.
- `captioning/photo.py` (EXIF date), `captioning/book.py` + `isbn.py` (the ISBN
  ladder; every rung optional + injectable), `captioning/generic.py` (mtime
  date; any other filetype). `captioning.compose()` dispatches; books prefer
  the enriched `items.caption`, falling back to generic — nothing ships
  caption-less.
- `enrich.py` — the async, fail-soft book-identification pass.

**Storage / movement**
- `backends/` — `StorageBackend` Protocol (`store(path, hash, *, caption=None)`,
  `fetch`, `verify`, `exists`); `local.py` + `rclone.py` (durable,
  hash-verifying), `telegram.py` (fast tier, presence-only verify, carries the
  caption), `registry.py` (name → instance; `is_durable` gates offload).
- `routing.py` — filetype bucket → ordered backend names (`[backup.routing]`).
- `backup.py` — concurrent per-item fan-out, verify-after-store for durable
  copies, dup-upload skip via `store.location_ref_for_hash`, retry ladder.
- `offload.py` — the HSM gate: re-verify a durable copy LIVE immediately before
  the unlink; `CLOUD_MANAGED` guard for iCloud-evicted files.
- `dedup.py` — ingest-time winner rule + the Phase 8 standalone `dedup_root`
  pass (DB-driven grouping; disk-hash only untracked strays).
- `deletion.py` — `DeletionGuard`, the ONE delete chokepoint, backed by the
  config-driven `ProtectionPolicy` (`[protect]`: pause, roots, paths).
- `heal.py` — the self-healing pass (see §6).

**Orchestration / interface**
- `worker.py` — `run_once` (heal→backup→offload core) and `full_cycle` (the
  facade over every pass, §2), each stage fail-soft into `CycleReport`.
- `bootstrap.py` — config → wired objects: `registry_from_config`
  (`[backends.*]`, fail-soft per backend), `assemble` → (Registry,
  RoutingPolicy, guarded DeletionGuard).
- `cli.py` / `__main__.py` — the `librarian` command; thin by rule (one library
  call per subcommand). `bot.py` — find/serve/restore over FTS5.

## 4. Data model — `librarian.db` (own)

`items` (one row per file): `id, path UNIQUE, content_hash, root, size_bytes,
title, caption, upload_date, group_key, status, discovered_at, last_error,
attempts`. `locations` (one row per stored copy): `item_id, backend, locator,
verified_at`, `PRIMARY KEY(item_id, backend)`. `roots`: `name UNIQUE, path,
destination, tags`. `items_fts` (FTS5 over title/caption/path/upload_date) kept
by triggers. `status`: `pending → backed_up → offloaded`; `failed` after the
retry cap; heal/ingest can re-arm `→ pending`.

## 5. Folder hashtags (`tags.py`)

- **Source:** a `.tags` sidecar in any folder. Whitespace-separated tags per
  line; leading `#` optional; blank lines and `//` comments ignored.
- **Inheritance ("layers"):** a file inherits the **union** of `.tags` from its
  folder and every ancestor up to the registered **root boundary**, outermost
  first. Nearer folders *add*, never override.
- **Slug rule (Telegram-correct):** lowercase; non-alphanumeric runs → single
  `_`; **never `-`** (Telegram ends a hashtag at `-`); trim `_`; drop
  empty/all-digit. `bathroom selfie` → `#bathroom_selfie`. Shared with folder
  segment names via `_compose.py`, so a folder and a sidecar tag identically.
- **Safety + cache:** the walk **stops at the registered root** and never reads
  a `.tags` outside it; `.tags` re-read only when mtime moves.

## 6. Self-healing (the automation stance)

- **heal pass** — every durable claim re-verified LIVE each cycle: verifying
  claims get `verified_at` refreshed; a POSITIVE failure drops the claim (the
  DB stops lying); a transient error NEVER drops (an outage must not amplify
  into record loss); a re-armed item re-ships the SAME cycle; an offloaded item
  with no verifying durable copy is reported LOST, loudly.
- **fail-soft everywhere** — a missing optional dep disables one rung/backend,
  never the process; a crashing cycle stage is recorded and the rest still run;
  a missing caption never blocks delivery; bootstrap skips a broken backend and
  its items just stay PENDING.
- **safety gates on deletion** — offload re-verifies a durable, hash-verifying
  (never Telegram) copy immediately before the unlink; every unlink goes
  through `DeletionGuard`/`ProtectionPolicy`; dedup only ever removes a
  redundant local copy while an identical one remains; iCloud-evicted files are
  never offloaded (`CLOUD_MANAGED`).

## 7. Invariants Librarian keeps

- One row per file; every row carries `content_hash`; caption is **not** hashed,
  so enrichment never disturbs dedup.
- All deletes funnel through the `DeletionGuard`; never delete unless a
  **durable, non-Telegram** backend verified the copy live.
- Dedup winner order puts a tracked row above any untracked file — a backed-up
  row can never be orphaned by dedup; survivor choice is deterministic across
  re-scans (path tiebreak).
- Single-flight Telegram talker; additive schema only; every pass idempotent
  and fail-soft; deleting stages are opt-in at every interface (library, cycle,
  CLI).
- Captions are composed at **send time** (a `.tags`/folder edit is reflected on
  the next ship); only the capture date is frozen at ingest.
- Only an ISBN ever leaves the machine during enrichment.

## 8. Deferred

Long-running daemon wrapper (launchd/cron around `librarian cycle`) and a
`librarian bot` CLI entry; Telegram part-splitting for >2 GiB files (cloud
backends already hold them); real-world `brctl` smoke test before enabling
`--icloud materialize` by default; macFUSE on-demand FS; NL/SLM query layer
over FTS; GPS reverse-geocoding; books model fallback. None on the critical
path.
