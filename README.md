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

Early build — see [PLAN.md](PLAN.md) for the phased roadmap and what's done.

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

Backends live in `~/.config/librarian/config.toml` (`[backends.<name>]` with
`type = "local" | "rclone" | "telegram"`); see `librarian/bootstrap.py` for the
exact keys. Telegram needs a one-time `librarian telegram-login`.

## Design docs

- [DESIGN.md](DESIGN.md) — working spec (modules, seams, invariants)
- [PLAN.md](PLAN.md) — phased build sequence
- [docs/adr/0001-…](docs/adr/0001-librarian-multi-backend-hsm-and-folder-metadata.md) — the decision record

## Develop / test

```sh
PYTHONPATH=. python3 tests/test_tags.py
PYTHONPATH=. python3 tests/test_ingest.py
PYTHONPATH=. python3 tests/test_captioning.py
```

Requires Python ≥ 3.13. Runtime dependencies (telethon, rclone, …) are added per
phase as they land; the core spine has none.
