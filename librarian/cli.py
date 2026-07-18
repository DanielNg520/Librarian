"""
librarian.cli
─────────────
The `librarian` command — a THIN argparse layer over the library. Every
subcommand is: open the store, bootstrap from config, call ONE existing
function, print its report. No logic lives here; if a command needs a behavior
the library doesn't have, the library grows it first (and gets the tests).

    librarian root add Ebooks ~/Documents/Ebooks --tags "#books"
    librarian root list / remove
    librarian scan [ROOT] [--icloud report_only|materialize|skip]
    librarian cycle [--dedup] [--offload] [--dry-run] [--no-heal|scan|enrich]
    librarian dedup [--live]              # dry-run by default (deleting stage)
    librarian status
    librarian find "query"
    librarian telegram-login              # one-time interactive session auth

Safety posture mirrors the library defaults: the deleting stages (dedup,
offload) are opt-in flags, dedup is dry-run unless --live, iCloud defaults to
report_only. DB/config paths honor $LIBRARIAN_DB / $LIBRARIAN_CONFIG.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import bootstrap, roots, worker
from .dedup import dedup_pass
from .models import Status
from .paths import config_path, db_path
from .store import ItemStore


def _store() -> ItemStore:
    return ItemStore.open()


def cmd_root(args) -> int:
    with _store() as s:
        if args.action == "add":
            rec = roots.register(s, args.name, args.path,
                                 destination=args.destination, tags=args.tags)
            print(f"registered {rec['name']!r} → {rec['path']}")
        elif args.action == "remove":
            ok = roots.remove(s, args.name)
            print(f"removed {args.name!r}" if ok else f"no such root: {args.name!r}")
            return 0 if ok else 1
        else:                                    # list
            rows = roots.list_roots(s)
            if not rows:
                print("no roots registered — `librarian root add NAME PATH`")
            for r in rows:
                extra = " ".join(x for x in (r.get("tags"), r.get("destination"))
                                 if x)
                print(f"  {r['name']:<16} {r['path']}"
                      + (f"  [{extra}]" if extra else ""))
    return 0


def cmd_scan(args) -> int:
    with _store() as s:
        names = [args.root] if args.root else [r["name"] for r in s.list_roots()]
        if not names:
            print("no roots registered — `librarian root add NAME PATH`")
            return 1
        for name in names:
            print(roots.scan(s, name, icloud_policy=args.icloud))
    return 0


def cmd_cycle(args) -> int:
    with _store() as s:
        registry, policy, guard = bootstrap.assemble(s)
        rep = worker.full_cycle(
            s, registry, policy, guard,
            heal=not args.no_heal, scan=not args.no_scan,
            enrich=not args.no_enrich,
            dedup=args.dedup, offload=args.offload,
            icloud_policy=args.icloud, dry_run=args.dry_run,
        )
        print(rep)
        return 0 if rep.ok else 1


def cmd_dedup(args) -> int:
    with _store() as s:
        _, _, guard = bootstrap.assemble(s)
        for rep in dedup_pass(s, guard, dry_run=not args.live):
            print(rep)
        if not args.live:
            print("(dry run — pass --live to actually remove duplicates)")
    return 0


def cmd_status(args) -> int:
    with _store() as s:
        total = s.count_by_status()
        print(f"db: {db_path()}   config: {config_path()}")
        print(f"items: {total}")
        for st in Status:
            n = s.count_by_status(st)
            if n:
                print(f"  {st.value:<10} {n}")
        rows = roots.list_roots(s)
        print(f"roots: {len(rows)}" + (" — " + ", ".join(r["name"] for r in rows)
                                       if rows else ""))
        registry, policy, _ = bootstrap.assemble(s)
        print(f"backends: {', '.join(registry.names()) or 'NONE configured'}")
        print(f"routing default: {policy.default}")
    return 0


def cmd_find(args) -> int:
    with _store() as s:
        hits = s.search(" ".join(args.query))
        if not hits:
            print("no matches")
            return 1
        for it in hits:
            print(f"  #{it.id:<5} [{it.status:<9}] {it.title or ''}  ({it.path})")
    return 0


def cmd_telegram_login(args) -> int:
    """One-time interactive Telethon sign-in so `[backends.*] type=telegram`
    can run non-interactively afterwards."""
    import tomllib
    from pathlib import Path
    try:
        import telethon
    except ImportError:
        print("telethon is not installed — `pip install 'librarian[telegram]'`")
        return 1
    cfg = {}
    p = config_path()
    if p.exists():
        with p.open("rb") as f:
            cfg = tomllib.load(f)
    sect = next((v for v in (cfg.get("backends") or {}).values()
                 if isinstance(v, dict) and v.get("type") == "telegram"), None)
    if not sect or not {"api_id", "api_hash", "session"} <= sect.keys():
        print(f"add a [backends.<name>] section with type='telegram', api_id, "
              f"api_hash and session to {p} first")
        return 1
    session = str(Path(sect["session"]).expanduser())
    client = telethon.TelegramClient(session, int(sect["api_id"]),
                                     str(sect["api_hash"]))
    with client:                                 # interactive: phone + code
        me = client.loop.run_until_complete(client.get_me())
    print(f"authorized as {getattr(me, 'username', None) or me.id}; "
          f"session saved to {session}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="librarian",
        description="Systemwide smart file manager: folder-tagged multi-backend "
                    "backup, HSM offload, Telegram retrieval.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="INFO-level logging to stderr")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("root", help="manage registered roots")
    ps = p.add_subparsers(dest="action", required=True)
    pa = ps.add_parser("add");    pa.add_argument("name"); pa.add_argument("path")
    pa.add_argument("--tags"); pa.add_argument("--destination")
    ps.add_parser("list")
    pr = ps.add_parser("remove"); pr.add_argument("name")
    p.set_defaults(fn=cmd_root)

    p = sub.add_parser("scan", help="discover files under a root (or all)")
    p.add_argument("root", nargs="?")
    p.add_argument("--icloud", choices=roots.ICLOUD_POLICIES,
                   default="report_only")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("cycle", help="one full maintenance cycle "
                                     "(heal→scan→enrich[→dedup]→backup[→offload])")
    p.add_argument("--dedup", action="store_true",
                   help="collapse duplicate local copies (deleting stage)")
    p.add_argument("--offload", action="store_true",
                   help="reclaim disk for durable-verified items (deleting stage)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-heal", action="store_true")
    p.add_argument("--no-scan", action="store_true")
    p.add_argument("--no-enrich", action="store_true")
    p.add_argument("--icloud", choices=roots.ICLOUD_POLICIES,
                   default="report_only")
    p.set_defaults(fn=cmd_cycle)

    p = sub.add_parser("dedup", help="standalone duplicate collapse (dry-run "
                                     "unless --live)")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_dedup)

    p = sub.add_parser("status", help="counts, roots, backends, routing")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("find", help="full-text search (title/caption/path/date)")
    p.add_argument("query", nargs="+")
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("telegram-login",
                       help="one-time interactive Telegram sign-in")
    p.set_defaults(fn=cmd_telegram_login)

    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
