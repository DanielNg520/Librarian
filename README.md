# Librarian

An independent, systemwide smart file manager. It backs up the folders you hand
it to multiple durable backends (routed by file type), keeps Telegram as a
fast-access tier, reclaims local disk once a durable copy is verified, and
retrieves files on demand via a Telegram bot. Captions are deterministic — built
from your folder taxonomy (photos) and ISBN metadata (books), no model required.

> **Independence.** Librarian shares design DNA with the Media Archiver Suite but
> is a standalone app: it **copies, never imports** suite code. Own database,
> own Telegram session, own folders. See [DESIGN.md](DESIGN.md) §0.

## Status

Core complete and usable via the CLI — see [PLAN.md](PLAN.md) for the build
history and per-phase verification.

| Phase | What | State |
|------:|------|-------|
| 0 | Folder hashtags (`.tags` sidecars, layered) | ✅ |
| 1 | Vendored spine + own `librarian.db` (`items`, `locations`, `roots`) | ✅ |
| 2 | Photo captions (folder taxonomy + dependency-free EXIF) | ✅ |
| 3 | Storage backends (Telegram + rclone clouds + local) + filetype routing | ✅ |
| 4 | Backup fan-out + offload (HSM) | ✅ |
| 5 | Librarian bot — find / serve / restore | ✅ |
| 6 | Book enrichment (ISBN ladder) | ✅ |
| 7 | Generalized captions (any filetype) + Telegram send seam | ✅ |
| 8 | Standalone dedup pass + protection policy + no-dup-upload | ✅ |
| 9 | iCloud-aware ingest (evicted-file policy + HSM guard) | ✅ |
| 10 | Full-cycle orchestration (`worker.full_cycle`, fail-soft stages) | ✅ |
| 11 | Bootstrap + `librarian` CLI (root/scan/cycle/status/find) | ✅ |

## Quick start

```sh
uv pip install -e ".[telegram,books]"      # extras optional; core: zero deps
librarian root add Ebooks ~/Documents/Ebooks --tags "#books"
librarian scan                             # discover (iCloud: report_only)
librarian cycle                            # heal → scan → enrich → backup
librarian cycle --offload                  # + reclaim disk (durable-verified)
librarian status && librarian find "query"
```

The deleting stages (`--dedup`, `--offload`) are opt-in everywhere, `dedup` is
dry-run unless `--live`, and iCloud-evicted files default to `report_only`
(surfaced in the scan report, never downloaded or deleted).

## Configuration — `~/.config/librarian/config.toml`

```toml
[backends.disk]                # durable: hash-verified local/NAS copy
type = "local"
path = "/Volumes/NAS/librarian"

[backends.gdrive]              # durable: any rclone remote (`rclone config`)
type = "rclone"
remote = "gdrive:"
base   = "librarian"

[backends.telegram]            # fast-access tier (presence-only; never gates offload)
type = "telegram"              # one-time auth: `librarian telegram-login`
api_id = 12345
api_hash = "…"
session = "~/.config/librarian/telegram.session"
destination = "me"

[backup.routing]               # filetype bucket → ordered backend names
default  = ["gdrive", "telegram"]
document = ["gdrive", "telegram"]

[protect]                      # the deletion safebrake
pause = false                  # true = block EVERY delete
roots = []                     # root names shielded from offload/dedup
paths = []                     # absolute path prefixes, likewise
```

A misconfigured or missing backend is skipped loudly — items routed to it stay
pending until it works; nothing crashes. Override paths for tests/automation
with `$LIBRARIAN_DB` and `$LIBRARIAN_CONFIG`.

## Design docs

- [DESIGN.md](DESIGN.md) — as-built spec (pipeline, modules, self-healing, invariants)
- [PLAN.md](PLAN.md) — phased build history + per-phase verification
- [docs/adr/0001-…](docs/adr/0001-librarian-multi-backend-hsm-and-folder-metadata.md) — the decision record

## Develop / test

Every test file runs standalone **and** under pytest (equivalent via the
`tests/conftest.py` bridge):

```sh
pytest tests/                                  # the whole suite
PYTHONPATH=. python3 tests/test_worker.py      # or any single file, no pytest
```

Requires Python ≥ 3.13. The core spine has **zero runtime dependencies**;
optional extras per feature: `[telegram]` (telethon), `[books]` (pypdf),
`[books-ocr]` (pdf2image + pytesseract). A missing extra disables just that
feature — never the process.
