"""Command-line interface for MemBus."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .core import MemBus
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

    get = sub.add_parser("get", help="Get one memory by ID.")
    get.add_argument("memory_id")

    update = sub.add_parser("update", help="Update a memory.")
    update.add_argument("memory_id")
    _add_memory_fields(update, require_core=False)

    delete = sub.add_parser("delete", help="Delete a memory.")
    delete.add_argument("memory_id")
    restore = sub.add_parser("restore", help="Restore a deleted memory.")
    restore.add_argument("memory_id")

    alias = sub.add_parser("alias", help="Add or replace an alias.")
    alias.add_argument("alias")
    alias.add_argument("canonical")

    search = sub.add_parser("search", help="Search memories.")
    search.add_argument("query")
    _add_context_fields(search)
    search.add_argument("--type", dest="types", action="append", default=[])
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--strict", action="store_true")

    backup = sub.add_parser("backup", help="Create a SQLite backup.")
    backup.add_argument("destination")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bus = MemBus(args.db)

    if args.command == "doctor":
        report = run_doctor(bus)
        _print_json(report)
        return 0 if report["ok"] else 1

    if args.command == "put":
        _print_json(asdict(bus.put(**_memory_kwargs(args, require_core=True))))
        return 0

    if args.command == "get":
        memory = bus.get(args.memory_id)
        if memory is None:
            _print_json({"error": "not_found", "id": args.memory_id})
            return 1
        _print_json(asdict(memory))
        return 0

    if args.command == "update":
        _print_json(asdict(bus.update(args.memory_id, **_memory_kwargs(args))))
        return 0

    if args.command == "delete":
        deleted = bus.delete(args.memory_id)
        _print_json({"id": args.memory_id, "deleted": deleted})
        return 0 if deleted else 1

    if args.command == "restore":
        restored = bus.restore(args.memory_id)
        _print_json({"id": args.memory_id, "restored": restored})
        return 0 if restored else 1

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
        _print_json({"query": args.query, "results": [asdict(m) for m in results]})
        return 0

    if args.command == "backup":
        destination = bus.backup(args.destination)
        _print_json({"source": str(bus.path), "backup": str(destination)})
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


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


def _memory_kwargs(
    args: argparse.Namespace,
    *,
    require_core: bool = False,
) -> dict[str, Any]:
    values = {
        name: getattr(args, name)
        for name in _MEMORY_FIELDS
        if getattr(args, name) is not None
    }
    if require_core:
        values["title"] = values.get("title", "")
    if not values:
        raise ValueError("update requires at least one mutable field")
    return values


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    entrypoint()
