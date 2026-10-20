"""Command-line interface for MemBus."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .core import (
    DuplicateMemory,
    MemBus,
    MemoryRef,
    NotFound,
    RevisionConflict,
    SyncResetRequired,
)
from .doctor import run_doctor


_MEMORY_FIELDS = (
    "title",
    "content",
    "memory_type",
    "workspace",
    "project",
    "repo",
    "branch",
    "source",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="membus",
        description="User-owned local memory bus for AI agents.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "MEMBUS_DB",
            str(Path.home() / ".membus" / "memory.db"),
        ),
        help="SQLite database path.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check database health.")

    put = sub.add_parser("put", help="Store a memory.")
    _add_memory_fields(put, require_core=True)
    put.add_argument(
        "--supersedes", action="append", default=[], metavar="MEMORY_ID:REVISION",
        type=_memory_ref,
        help="Replace an active memory at its last-read revision (repeatable).",
    )

    get = sub.add_parser("get", help="Get one memory by ID (any state).")
    get.add_argument("memory_id")

    update = sub.add_parser(
        "update",
        help="Correct a memory. Empty string clears a context field/source.",
    )
    update.add_argument("memory_id")
    update.add_argument("--expected-revision", type=int, required=True)
    _add_memory_fields(update, require_core=False)

    delete = sub.add_parser("delete", help="Soft-delete a memory.")
    delete.add_argument("memory_id")
    delete.add_argument("--expected-revision", type=int, required=True)
    restore = sub.add_parser("restore", help="Restore a deleted memory.")
    restore.add_argument("memory_id")
    restore.add_argument("--expected-revision", type=int, required=True)
    purge = sub.add_parser(
        "purge", help="Irreversibly remove a memory (e.g. a stored secret).",
    )
    purge.add_argument("memory_id")
    purge.add_argument("--yes", action="store_true", required=True)

    alias = sub.add_parser("alias", help="Add or replace an alias.")
    alias.add_argument("alias")
    alias.add_argument("canonical")

    search = sub.add_parser("search", help="Search memories.")
    search.add_argument("query")
    _add_context_fields(search)
    search.add_argument("--type", dest="types", action="append", default=[])
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--strict", action="store_true")

    list_ = sub.add_parser("list", help="Browse memories; omitted context fields match any scope.")
    _add_context_fields(list_)
    list_.add_argument("--type", dest="types", action="append", default=[])
    list_.add_argument("--updated-since")
    list_.add_argument("--include-inactive", action="store_true")
    list_.add_argument("--limit", type=int, default=50)
    list_.add_argument("--cursor")

    sync = sub.add_parser("sync", help="Sync latest states globally, including inactive memories.")
    sync.add_argument("--after-revision", type=int, default=0)
    sync.add_argument("--generation")
    sync.add_argument("--limit", type=int, default=100)

    sub.add_parser("contexts", help="List workspace/project/repo in use.")

    backup = sub.add_parser("backup", help="Create a SQLite backup.")
    backup.add_argument("destination")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except NotFound as exc:
        _print_json({"error": "not_found", "id": exc.memory_id})
        return 1
    except RevisionConflict as exc:
        _print_json({
            "error": "revision_conflict",
            "current": exc.current.to_dict(),
        })
        return 1
    except SyncResetRequired as exc:
        _print_json({"error": "sync_reset_required", "generation": exc.generation,
                     "hint": str(exc)})
        return 1
    except DuplicateMemory as exc:
        _print_json({
            "error": "duplicate_memory",
            "existing": exc.existing.to_dict(),
        })
        return 1
    except ValueError as exc:
        _print_json({"error": "invalid", "detail": str(exc)})
        return 2


def _run(args: argparse.Namespace) -> int:
    bus = MemBus(args.db)

    if args.command == "doctor":
        report = run_doctor(bus)
        _print_json(report)
        return 0 if report["ok"] else 1

    if args.command == "put":
        kwargs = _memory_kwargs(args)
        kwargs["title"] = kwargs.get("title") or ""
        _print_json(bus.put(**kwargs, supersedes=args.supersedes).to_dict())
        return 0

    if args.command == "get":
        memory = bus.get(args.memory_id)
        if memory is None:
            _print_json({"error": "not_found", "id": args.memory_id})
            return 1
        _print_json(memory.to_dict())
        return 0

    if args.command == "update":
        changes = _memory_kwargs(args)
        # Empty string clears; core treats None as "clear", missing as unchanged.
        changes = {k: (v if v != "" or k in ("content", "memory_type") else None)
                   for k, v in changes.items()}
        _print_json(bus.update(
            args.memory_id, expected_revision=args.expected_revision, **changes,
        ).to_dict())
        return 0

    if args.command == "delete":
        deleted = bus.delete(args.memory_id, expected_revision=args.expected_revision)
        _print_json({"id": args.memory_id, "deleted": deleted})
        return 0 if deleted else 1

    if args.command == "restore":
        restored = bus.restore(args.memory_id, expected_revision=args.expected_revision)
        similar = bus.similar(args.memory_id) if restored else []
        _print_json({"id": args.memory_id, "restored": restored,
                     "similar": [memory.to_dict() for memory in similar]})
        return 0 if restored else 1

    if args.command == "purge":
        purged = bus.purge(args.memory_id)
        _print_json({"id": args.memory_id, "purged": purged})
        return 0 if purged else 1

    if args.command == "alias":
        bus.put_alias(args.alias, args.canonical)
        _print_json({"alias": args.alias, "canonical": args.canonical})
        return 0

    if args.command == "search":
        results = bus.search(
            args.query,
            workspace=args.workspace,
            project=args.project,
            repo=args.repo,
            branch=args.branch,
            types=args.types,
            limit=args.limit,
            strict=args.strict,
        )
        _print_json({
            "query": args.query,
            "results": [memory.to_dict() for memory in results],
        })
        return 0

    if args.command == "list":
        page = bus.list(
            workspace=args.workspace,
            project=args.project,
            repo=args.repo,
            branch=args.branch,
            types=args.types,
            updated_since=args.updated_since,
            include_inactive=args.include_inactive,
            limit=args.limit,
            cursor=args.cursor,
        )
        _print_json(page.to_dict())
        return 0

    if args.command == "sync":
        _print_json(bus.sync(after_revision=args.after_revision,
                             generation=args.generation, limit=args.limit).to_dict())
        return 0

    if args.command == "contexts":
        _print_json({"contexts": [asdict(c) for c in bus.contexts()]})
        return 0

    if args.command == "backup":
        destination = bus.backup(args.destination)
        _print_json({"source": str(bus.path), "backup": str(destination)})
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


def _memory_ref(value: str) -> MemoryRef:
    try:
        memory_id, revision = value.rsplit(":", 1)
        expected_revision = int(revision)
        if not memory_id or expected_revision < 1:
            raise ValueError
        return MemoryRef(memory_id, expected_revision)
    except ValueError:
        raise argparse.ArgumentTypeError("expected MEMORY_ID:REVISION with revision >= 1") from None


def entrypoint() -> None:
    raise SystemExit(main())


def _add_context_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace")
    parser.add_argument("--project")
    parser.add_argument("--repo")
    parser.add_argument("--branch")


def _add_memory_fields(parser: argparse.ArgumentParser, *, require_core: bool) -> None:
    parser.add_argument("--title")
    parser.add_argument("--content", required=require_core)
    parser.add_argument("--memory-type", required=require_core)
    parser.add_argument("--source")
    _add_context_fields(parser)


def _memory_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        name: getattr(args, name)
        for name in _MEMORY_FIELDS
        if getattr(args, name) is not None
    }


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    entrypoint()
