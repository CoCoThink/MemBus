"""Command-line interface for MemBus."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .doctor import run_doctor
from .evaluation import evaluate_dataset
from .models import MemoryCreate, SearchContext, SearchRequest
from .services.memory_service import MemoryService
from .store import SQLiteStore


_MEMORY_FIELDS = (
    "title",
    "content",
    "memory_type",
    "workspace",
    "project",
    "repo",
    "branch",
    "source_type",
    "source_ref",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="membus",
        description="User-sovereign local memory bus for AI agents.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "MEMBUS_DB",
            str(Path.home() / ".membus" / "memory.db"),
        ),
        help="SQLite canonical database path.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check MemBus application and data health.")

    put = sub.add_parser("put", help="Store an explicit durable memory.")
    _add_memory_fields(put, require_core=True)

    get = sub.add_parser("get", help="Get one memory by ID.")
    get.add_argument("memory_id")

    update = sub.add_parser("update", help="Update mutable fields of a memory.")
    update.add_argument("memory_id")
    _add_memory_fields(update, require_core=False)

    delete = sub.add_parser("delete", help="Delete a memory.")
    delete.add_argument("memory_id")

    alias = sub.add_parser("alias", help="Add or replace a deterministic alias.")
    alias.add_argument("alias")
    alias.add_argument("canonical")

    search = sub.add_parser("search", help="Search the memory store.")
    search.add_argument("query")
    _add_context_fields(search)
    search.add_argument("--type", dest="types", action="append", default=[])
    search.add_argument("--limit", type=int, default=10)

    backup = sub.add_parser("backup", help="Create a consistent SQLite backup.")
    backup.add_argument("destination")

    evaluate = sub.add_parser("eval", help="Run the isolated golden evaluation.")
    evaluate.add_argument("dataset")
    evaluate.add_argument("--min-recall-5", type=float)
    evaluate.add_argument("--min-recall-10", type=float)
    evaluate.add_argument("--max-wrong-scope-rate", type=float)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "eval":
        report = evaluate_dataset(args.dataset)
        _print_json(report.as_dict())

        failures: list[str] = []
        if args.min_recall_5 is not None and report.recall_at_5 < args.min_recall_5:
            failures.append(
                f"recall@5 {report.recall_at_5:.4f} < {args.min_recall_5:.4f}"
            )
        if args.min_recall_10 is not None and report.recall_at_10 < args.min_recall_10:
            failures.append(
                f"recall@10 {report.recall_at_10:.4f} < {args.min_recall_10:.4f}"
            )
        if (
            args.max_wrong_scope_rate is not None
            and report.wrong_scope_hit_rate > args.max_wrong_scope_rate
        ):
            failures.append(
                "wrong-scope rate "
                f"{report.wrong_scope_hit_rate:.4f} > "
                f"{args.max_wrong_scope_rate:.4f}"
            )

        for failure in failures:
            print(f"evaluation gate failed: {failure}", file=sys.stderr)
        return 2 if failures else 0

    store = SQLiteStore(args.db)

    if args.command == "doctor":
        report = run_doctor(store)
        _print_json(report)
        return 0 if report["ok"] else 1

    service = MemoryService(store)

    if args.command == "put":
        _print_json(asdict(service.put(MemoryCreate(**_memory_create_kwargs(args)))))
        return 0

    if args.command == "get":
        memory = service.get(args.memory_id)
        if memory is None:
            _print_json({"error": "not_found", "id": args.memory_id})
            return 1
        _print_json(asdict(memory))
        return 0

    if args.command == "update":
        _print_json(asdict(service.update(args.memory_id, **_memory_update_kwargs(args))))
        return 0

    if args.command == "delete":
        deleted = service.delete(args.memory_id)
        _print_json({"id": args.memory_id, "deleted": deleted})
        return 0 if deleted else 1

    if args.command == "alias":
        service.put_alias(args.alias, args.canonical)
        _print_json({"alias": args.alias, "canonical": args.canonical})
        return 0

    if args.command == "search":
        results = service.search(
            SearchRequest(
                query=args.query,
                context=_context_from_args(args),
                types=tuple(args.types),
                limit=args.limit,
            )
        )
        _print_json(
            {
                "query": args.query,
                "results": [
                    {"memory": asdict(result.memory), "match": result.match}
                    for result in results
                ],
            }
        )
        return 0

    if args.command == "backup":
        destination = store.backup(args.destination)
        _print_json({"source": str(store.path), "backup": str(destination)})
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
    _add_context_fields(parser)
    parser.add_argument("--source-type")
    parser.add_argument("--source-ref")


def _memory_create_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    values = {name: getattr(args, name) for name in _MEMORY_FIELDS}
    values["title"] = values["title"] or ""
    return values


def _memory_update_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    changes = {
        name: getattr(args, name)
        for name in _MEMORY_FIELDS
        if getattr(args, name) is not None
    }
    if not changes:
        raise ValueError("update requires at least one mutable field")
    return changes


def _context_from_args(args: argparse.Namespace) -> SearchContext:
    return SearchContext(
        workspace=args.workspace,
        project=args.project,
        repo=args.repo,
        branch=args.branch,
    )


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    entrypoint()
