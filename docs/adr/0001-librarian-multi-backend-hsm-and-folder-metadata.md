# ADR-0001: Librarian — multi-backend backup tier, on-demand retrieval, and folder-driven metadata

**Status:** Accepted — implemented through Phase 11 (2026-07-18); as-built spec: `DESIGN.md`
**Date:** 2026-06-26
**Deciders:** Daniel (owner)
**Relates to:** the existing suite (`archiver`, `recorder`, `dispatcher`, `ops`, `core`) as a *source of copyable patterns only*; see `DESIGN.md`, `PROJECT_MAP.md`.

> **UPDATE 2026-06-26 — independence directive.** Librarian is now an
> **independent application destined for its own repo**. It **copies, never
> links**: no `import core`, no reaching into the dispatcher. It has its **own
> `librarian.db`**, its **own Telegram session**, and manages **only the user's
> own folders** (the suite keeps social-media downloads). The conceptual design
> below stands, but read these corrections: the `locations` table + `Status`
> live in **Librarian's own schema** (not `core.schema`); the bot is **Librarian's
> own worker/process with its own MTProto client** (not a "dispatcher mode"); the
> caption seam is **Librarian's own send path** (not the dispatcher's `drain.py`).
> Authoritative current spec: `librarian/DESIGN.md` + `librarian/PLAN.md`.

---

## Context

The Media Archiver Suite already does the hard half of a personal file
manager: producers write media to disk + a `pending` row, and the dispatcher
ships bytes to Telegram and (per policy) deletes the local copy. Coordination
is one SQLite file, no IPC. Established invariants we must not break:

- **Integrity > self-healing > seam robustness > efficiency** (standing order).
- One row per file (`UNIQUE file_path`), one row per post
  (`UNIQUE platform,identifier`); every row carries `content_hash` (global
  dedup key).
- Never delete unless `status='sent'` AND policy allows AND not safebraked;
  all deletes funnel through `core.deletion.DeletionGuard`.
- The dispatcher is the only Telegram talker; it owns the keepalive'd MTProto
  connection and the single reconnect authority.
- Folder names are already routing authority (`core.orphaned`: a top-level
  folder named like a chat_id routes there; a subfolder is an album).
- Captions are composed/mutated at a **send-time seam** (`core.sanitize`
  strips banned words there today).

We want to grow this into a **systemwide smart file manager** that:

1. **Backs up to multiple durable backends** (Google Drive, Box, etc.),
   routed **by filetype**, with **Telegram as a fast-access secondary tier**.
2. **Reclaims local disk** once a durable copy is verified (HSM / tiering),
   with **no placeholder files** — retrieval is on-demand via a Telegram bot.
3. Makes content **findable** without a local index, by writing rich,
   deterministic captions: **photos** described by their folder taxonomy +
   EXIF timestamp; **books (PDF)** identified by an ISBN-driven metadata ladder.

Forces at play: this is a **personal** archive (privacy-sensitive — we already
sanitize filenames and auto-retire banned accounts), the owner is decisive and
values deterministic-over-model, and the suite's discipline (single
chokepoints, fail-soft producers, additive schema) must be preserved.

### Decisions already taken during design (locked)

- Telegram is a **secondary, easy-access** tier; a durable cloud backend is the
  integrity-bearing copy. → safer offload gate, sidesteps the dead-account risk.
- Form factor: a **Telegram bot running on the Mac** ("librarian"), not a
  local SLM, not a GUI, not macFUSE.
- **No placeholder/alias files.** Retrieval is "ask the bot, file lands in
  `~/Downloads`."
- **Photos: no vision model.** The folder/subfolder path *is* the description;
  each path segment contributes a layered hashtag; EXIF supplies the timestamp.
- **Books: deterministic extraction ladder**, model only as an opt-in last
  resort. Default stance: OCR **on**, online ISBN lookup **on** (only the
  number leaves the machine), model fallback **off**.

---

## Decision

Introduce a **Librarian** capability layered on the existing suite, built from
three cohesive parts:

### D1 — Storage is a `StorageBackend` Strategy; one item → many `locations`

Telegram stops being special. Define a backend Protocol (mirrors the Strategy
pattern already used for archiver platforms and dispatcher send paths):

```python
class StorageBackend(Protocol):
    name: str                                       # 'telegram' | 'gdrive' | 'box' | …
    def store(self, path, content_hash) -> Locator
    def fetch(self, locator, dest_path) -> Path
    def verify(self, locator, content_hash) -> bool
    def exists(self, locator) -> bool
```

- The existing dispatcher send + `tg_message_id` **becomes the `telegram`
  backend** behind this interface (including 3.9 GiB part-splitting, which
  stays *inside* that backend; cloud backends store one object).
- Cloud backends are implemented by **wrapping `rclone`** (one CLI speaks
  Drive/Box/Dropbox/S3, resumable, checksum-verified) — not per-provider APIs.

New `locations` table generalizes the single `tg_message_id` column:

```sql
CREATE TABLE locations (
    item_id     INTEGER NOT NULL REFERENCES items(id),
    backend     TEXT    NOT NULL,     -- 'telegram' | 'gdrive' | …
    locator     TEXT    NOT NULL,     -- msg_id, drive file_id, rclone path …
    verified_at TEXT,                 -- last verify() that confirmed bytes
    PRIMARY KEY (item_id, backend)
);
```

Migrate `tg_message_id` → a `backend='telegram'` row using the additive ALTER
pattern in `core.schema`. The `items` row stays the **restore manifest**:
`file_path` (where to restore), `content_hash` (how to verify), `locations`
(every place the bytes live).

**Filetype → backend routing** via `PolicyStore` (same scoped-config machinery
as banned words / protection policy), keyed on the existing media-bucket:

```toml
[backup.routing]
video    = ["gdrive", "telegram"]
photo    = ["box", "telegram"]
document = ["gdrive", "telegram"]
default  = ["gdrive", "telegram"]
```

**Offload (HSM) gate** — the integrity-bearing rule. Add a `Status.OFFLOADED`.
A separate, later pass (age- or disk-pressure-triggered, reusing the archiver's
existing emergency-purge logic):

> Reclaim the local file only once **a durable (non-Telegram) backend
> `verify()`s the copy present**. Telegram presence never authorizes deletion.
> Route the unlink through `DeletionGuard` (safebrake still applies). Keep the
> row; flip `status → offloaded`. No marker left on disk — the row + `locations`
> *is* the record.

This mirrors the existing ship-and-delete discipline in reverse ("drop only
once the file is confirmed gone" → "stub only once the durable copy is
confirmed present"), and a dead Telegram account can never cost a file.

### D2 — Librarian bot = a mode of the dispatcher (the only Telegram talker)

Run the bot **inside the dispatcher process** so it reuses the keepalive'd
MTProto connection and single reconnect authority — do **not** stand up a
second client fighting the same account. Capabilities:

- **`find <query>`** → **SQLite FTS5** over
  `title / caption / platform / username / upload_date / tags`. Deterministic;
  covers ~90% of retrieval. (Any NL/SLM translator is a thin, optional layer on
  top, never in the integrity path.)
- **serve** → forward the stored Telegram message inline (instant, no disk
  round-trip) for a quick look.
- **restore** → choose the best backend's `fetch()`, write to `~/Downloads`
  (default; configurable to original `file_path`), **re-verify `content_hash`**,
  flip `status → sent`. Telegram-split files reassemble via `group_key`.

### D3 — Deterministic, content-type-specific caption enrichment

All captions are composed at the **existing send-time seam** (alongside
`sanitize` and the new folder-tags), gated on the media-bucket.

**Photos — folder taxonomy is the metadata (no model):**

For `…/selfie/bathroom selfie/outdoor/IMG_1234.jpg`:

```
2024-08-14 18:32                          ← EXIF DateTimeOriginal → items.upload_date
selfie · bathroom selfie · outdoor        ← path segments below the routing root = description
#selfie #bathroom_selfie #outdoor         ← each segment slugified = a layered hashtag
```

- EXIF timestamp read at ingest, stored in the existing `upload_date` column
  (no new column; feeds date-sort + FTS).
- Description = path segments below the root, joined.
- **Layered hashtags** = each segment slugified, **accumulated down the tree**
  (a deep photo inherits every ancestor's tag), plus an optional **`.tags`
  sidecar** in any folder for curated extra hashtags that also inherit downward.
- Generalizes the current single-level `group_key='<chat_id>/<sub>'` grouping to
  the **full subpath** — same album mechanics, every level walked.
- **Slug rule (correctness):** multiword segments join with **underscore, never
  hyphen** (Telegram terminates a hashtag at `-`, so `#bathroom_selfie`, not
  `#bathroom-selfie`); lowercase, strip punctuation, dedup the final tag list.

**Routing root (chosen):** extend `archiver local add` so a **human-named root**
(e.g. `Photos/`) binds to a destination chat_id + base hashtags in config —
friendlier than raw chat_id top folders, reusing the local-platform registry.

**Books (PDF) — ISBN-driven extraction ladder (deterministic-first):**

A small **async, fail-soft enrichment pass** gated on the `document` bucket
(network + possible OCR make it too slow for inline ingest or the send loop):

1. **Embedded PDF metadata** (`/Title`, `/Author`) — use, never trust alone.
2. **Text-extract first ~10 + last few pages**, regex **ISBN-10/13**, validate
   checksum (copyright page front, back cover last).
3. **ISBN → Open Library** (free, no key), fallback **Google Books** → canonical
   `title / author / year / publisher`. Only the **ISBN number** leaves the
   machine (low sensitivity).
4. **Filename parse** (`Author - Title (Year).pdf`) as cross-check / fallback.
5. **Scanned/image-only PDFs:** **OCR** (`tesseract` + `pdf2image`) on the first
   few + copyright pages before steps 2–3.
6. **Last resort (opt-in, default off):** vision/LLM read of the title page —
   the only step that sends book *content* to a cloud model.

Fail-soft down the ladder: ISBN-lookup → embedded → filename → raw stem.
`title` reuses the existing column; author/ISBN/year compose into `caption`
(searchable in Telegram in-chat AND in FTS), folder tags layer on identically.

---

## Options Considered

### D1 — Storage abstraction

#### Option A: `StorageBackend` Strategy + `locations` table *(chosen)*
| Dimension | Assessment |
|-----------|------------|
| Complexity | Med — one new table, refactor send into a backend |
| Cost | Low — `rclone` covers every cloud provider |
| Scalability | High — new backend = new strategy + config line |
| Team familiarity | High — same Strategy/Protocol pattern as platforms/send |

**Pros:** Telegram becomes one backend among equals; multi-copy fan-out and the
durable-verified offload gate fall out naturally; `rclone` removes per-provider
API work. **Cons:** schema migration of `tg_message_id`; an indirection layer
over code that currently works directly.

#### Option B: Keep `tg_message_id`, bolt cloud on as a parallel path
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low now, High later |
| Cost | Grows per provider |
| Scalability | Poor — every backend re-implements store/fetch/verify |
| Team familiarity | High |

**Pros:** no migration, smallest immediate diff. **Cons:** no single
multi-location truth; the offload gate ("a *durable* copy is verified") has
nowhere clean to live; violates the suite's one-chokepoint discipline.

### D3 (photos) — source of description

#### Option A: Folder taxonomy + EXIF *(chosen)* — deterministic, zero cost, human-curated, perfectly reliable; requires disciplined foldering.
#### Option B: Local vision model (moondream/Qwen-VL) — private but slow, a new heavy dep, lower precision than a human's own folders.
#### Option C: Cloud vision (Claude) — best captions, but personal photos leave the machine (rejected on privacy).

### D3 (books) — identification

#### Option A: ISBN ladder + online lookup *(chosen)* — authoritative metadata from a validated number; only the number leaves; OCR handles scans; model optional.
#### Option B: Embedded metadata only — free but frequently empty/garbage; no canonical source.
#### Option C: Model-first title-page read — works on anything but sends content to a cloud model and is non-deterministic; kept only as opt-in last resort.

---

## Trade-off Analysis

- **Integrity vs. space.** Offload reclaims disk only after a *durable
  non-Telegram* backend verifies the copy — Telegram alone never authorizes
  deletion. We trade maximum space savings for a guarantee that no single
  backend (especially a bannable Telegram account) can lose a file. Consistent
  with the standing order (integrity first).
- **Determinism vs. smartness.** Both enrichment paths reject models for the
  common case (folders for photos, ISBN for books). We trade "describes
  anything" for "never wrong, never private, free, offline." The model survives
  only as an opt-in books last resort.
- **Reuse vs. a second Telegram client.** Running the bot as a dispatcher mode
  trades a cleaner process boundary for not fighting the account's rate limits /
  reconnect authority — the suite's single-talker invariant wins.
- **`rclone` vs. native SDKs.** We trade fine-grained API control for one robust,
  checksum-verifying tool that already speaks every provider — a large shortcut
  with proven reliability.

---

## Consequences

**Easier**
- Adding a cloud provider = an `rclone` remote + a routing line.
- Retrieval everywhere (phone/desktop) via the bot; no placeholder subsystem,
  no macFUSE.
- Findability without a local index — the Telegram message text *is* the index
  (folder tags, EXIF date, book metadata), searchable in-app and via FTS.
- The `locations` table makes "where does this file live, and is it intact?" a
  single query.

**Harder / new burden**
- Schema migration (`locations`, `Status.OFFLOADED`) — must follow the additive
  ALTER pattern; verify against the editable-core install asymmetry.
- New runtime deps: `rclone`, `pypdf`/`pdfminer.six`, `tesseract`+`pdf2image`,
  `requests`. Each needs a startup guard (cf. the `hachoir` hard-dep lesson).
- A new async **book-enrichment** worker and a new **offload** pass — both must
  be fail-soft and must never block delivery.
- Folder discipline becomes load-bearing for photo metadata (garbage foldering
  → garbage captions).

**To revisit later**
- macFUSE on-demand "click greyed file → download" (deferred luxury).
- NL/SLM query translator on top of FTS (optional, never in integrity path).
- Books model fallback (opt-in) if the no-ISBN scanned case proves common.
- Reverse-geocoding GPS → place name for photos (offline vs. online lookup).

---

## Action Items

1. [ ] **Phase 1 — backend seam:** define `StorageBackend` Protocol; refactor
       existing send + `tg_message_id` into the `telegram` backend; add
       `locations` table + additive migration of `tg_message_id`. No new
       behavior — pure seam extraction proven against working code.
2. [ ] **Phase 2 — cloud + routing:** `rclone` backend; filetype→backend policy
       in `PolicyStore`; fan-out store on send. Add startup guards for new deps.
3. [ ] **Phase 3 — offload pass:** `Status.OFFLOADED`; age/disk-pressure trigger
       reusing archiver purge; durable-verified gate through `DeletionGuard`.
4. [ ] **Phase 4 — librarian bot:** dispatcher mode; FTS5 index; `find` /
       `serve` / `restore` (→ `~/Downloads`, re-verify `content_hash`).
5. [ ] **Phase 5 — photo captions:** folder-taxonomy description + layered
       hashtags (underscore slug rule) + `.tags` sidecar inheritance; EXIF →
       `upload_date`; extend `archiver local add` for human-named roots.
6. [ ] **Phase 6 — book enrichment:** async fail-soft pass; embedded → ISBN
       (OCR for scans) → Open Library/Google Books → filename ladder; compose
       into `title`/`caption`. Model fallback off by default.
7. [ ] Update `DESIGN.md`, `PROJECT_MAP.md`, and `AUTOMATION.md` (new workers,
       new manual/auto safeguards) as each phase lands.

> **Independent of all phases:** folder hashtags (`core.tags`) — the caption-seam
> injection — can ship anytime.
