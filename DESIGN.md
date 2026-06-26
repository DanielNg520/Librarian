# Librarian — design note

> Component-level working spec. Decision record (why/options/trade-offs):
> `docs/adr/0001-librarian-multi-backend-hsm-and-folder-metadata.md`.
> Status: **design + Phase 0 only.**

## 0. Independence contract (read this first)

Librarian is an **independent application**, built to be lifted into its **own
repo**. The relationship to the Media Archiver Suite is **copy, never link**:

- **No `import core`**, no importing `archiver`/`recorder`/`dispatcher`. The suite
  is a *source of copyable patterns*, not a dependency.
- Proven suite code is **vendored** (copied in and adapted) under `librarian/`:
  the `ingest` template (stabilize→hash→dedup→insert), the `Sanitizer`/slug
  idea, the SQLite-WAL `ItemStore`, `DeletionGuard`, `hashing`/`dedup`,
  `paths`/`heartbeat`. Each copied file notes its origin so drift is auditable.
- **Own database** — `librarian.db` (SQLite/WAL), schema copied-and-adapted. No
  read or write of `suite.db`.
- **Own Telegram identity** — its own Telethon session/account. It is its own
  (and only) Telegram talker; the suite's dispatcher is unaffected. If the same
  account is reused, the two share the account's rate limits — Librarian must be
  a polite, single-flight talker.
- **Own domain** — Librarian manages **the user's own folders** (Photos/, Books/,
  documents). The archiver suite keeps owning social-media downloads. No overlap.

Everything below lives **inside Librarian**; nothing reaches back into the suite.

| | |
|---|---|
| **Purpose** | Systemwide file manager: back up user folders to multiple durable backends (routed by filetype), keep Telegram as a fast-access tier, reclaim local disk once a durable copy is verified, retrieve on demand via a Telegram bot. Deterministic, folder/ISBN-derived captions for findability. |
| **Shape** | Standalone package + binary. A long-running worker (backup + offload passes), a Telegram bot, and a CLI, coordinating through `librarian.db` — no IPC. |
| **Priorities** | Inherited stance: integrity > self-healing > seam robustness > efficiency. |

## 1. Pipeline

```
user folders (Photos/, Books/, …)
        │  watch / scan
        ▼
   ingest (stabilize → hash → dedup → insert)              [vendored pattern]
        ▼
   librarian.db  items + locations                          [own DB]
        │
   backup pass ── StorageBackend fan-out ──► gdrive/box (durable) + telegram (fast)
        │              records one `locations` row per copy
        ▼
   offload pass ── durable verify() ──► DeletionGuard ──► Status.OFFLOADED  (no placeholder)
        ▲
   bot: find (FTS5) · serve (inline TG) · restore (fetch → ~/Downloads, re-verify hash)
```

Captions are composed at Librarian's **own send seam** (the only caption seam
here — there is no dispatcher to plug into): folder tags + EXIF date (photos) /
ISBN metadata (books).

## 2. Module map (`librarian/librarian/`)

**Phase 0 (now):**
- `tags.py` — folder-authored hashtags (`.tags` sidecars), inheritance down the
  tree, Telegram-safe slug rule, append-to-caption. **Pure, no DB/Telegram deps.**

**Later (vendored + new):**
- `store.py` / `schema.py` — `librarian.db`: `items` + `locations`, `Status`
  incl. `OFFLOADED`. Copied-and-adapted from the suite's `core.store`/`schema`.
- `ingest.py` — the template-method enqueue (copied), enforcing stabilize-first +
  universal `content_hash`.
- `backends/` — `StorageBackend` Protocol; `telegram.py` (own Telethon session,
  send + `download_media` + part-split internal), `rclone_backend.py` (clouds),
  `registry.py`.
- `captioning/photo.py` — EXIF timestamp + folder-taxonomy description + layered
  tags (uses `tags.py`).
- `captioning/book.py` + an async enrichment pass — ISBN ladder.
- `deletion.py` — vendored `DeletionGuard` (safebrake) for the offload gate.
- `roots.py` — register a human-named folder → destination + base tags.
- `worker.py` — backup + offload passes. `bot.py` — find/serve/restore + FTS5.
- `cli.py` — `librarian` binary.

## 3. Data model — `librarian.db` (own)
`items` (one row per file): `id, path UNIQUE, content_hash, status, root,
caption, upload_date (EXIF/derived), title, group_key, size_bytes, …`.
`locations` (one row per stored copy): `item_id, backend, locator, verified_at`,
`PRIMARY KEY(item_id, backend)`. `status`: `pending → backed_up → offloaded`,
`offloaded → restored/backed_up`.

## 4. Phase 0 — folder hashtags (`tags.py`)

The only piece built now. Self-contained, no DB or network — pure caption logic,
so it's fully testable in isolation and drops straight into the send seam later.

- **Source:** a `.tags` sidecar in any folder. One or more whitespace-separated
  tags per line; leading `#` optional; blank lines and `//` comments ignored.
- **Inheritance ("layers"):** a file inherits the **union** of `.tags` from its
  own folder and every ancestor up to a registered **root boundary**, outermost
  first. Parents apply to everything beneath; nearer folders *add*, never override.
- **Slug rule (Telegram-correct):** lowercase; every run of non-alphanumeric →
  single `_`; **never `-`** (Telegram ends a hashtag at `-`); trim `_`; drop
  empty/all-digit. So `bathroom selfie` → `#bathroom_selfie`.
- **Apply:** append a final `#a #b #c` line to the caption, skipping tags the
  caption already carries (word-boundary, case-insensitive). No tags / no root →
  caption returned unchanged (costs nothing unconfigured).
- **Safety + cache:** the walk **stops at the registered root** and never reads a
  `.tags` outside it. `.tags` re-read only when mtime moves (stat-per-lookup),
  mirroring the suite's `ReloadingSanitizer`.

## 5. Invariants Librarian keeps (its own copies)
- One row per file; every row carries `content_hash`; caption is **not** hashed,
  so enrichment never disturbs dedup.
- All deletes funnel through Librarian's `DeletionGuard`; never delete unless a
  **durable, non-Telegram** backend verified the copy.
- Single-flight Telegram talker; additive schema only; every pass fail-soft and
  never blocks delivery.

## 6. Deferred
macFUSE on-demand FS; NL/SLM query layer over FTS; GPS reverse-geocoding; books
model fallback. None are on the critical path.
