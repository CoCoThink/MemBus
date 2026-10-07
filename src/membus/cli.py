"""Command-line interface for MemBus."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .db import Database
from .evaluation import evaluate_dataset
from .models import MemoryCreate, SearchContext, SearchManyRequest, SearchRequest
from .services.memory_service import MemoryService


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
    parser.add_argument(
        "--retrieval-log-mode",
        choices=("off", "metadata-only", "full"),
        default=os.environ.get("MEMBUS_RETRIEVAL_LOG_MODE", "off"),
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of pretty JSON.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize or verify the database.")

    put = sub.add_parser("put", help="Store an explicit durable memory.")
    _add_memory_fields(put, require_core=True)
    put.add_argument("--actor")

    get = sub.add_parser("get", help="Get one memory by ID.")
    get.add_argument("memory_id")
    get.add_argument("--include-deleted", action="store_true")

    update = sub.add_parser("update", help="Update mutable fields of a memory.")
    update.add_argument("memory_id")
    _add_memory_fields(update, require_core=False)
    update.add_argument("--actor")

    delete = sub.add_parser("delete", help="Logically delete a memory.")
    delete.add_argument("memory_id")
    delete.add_argument("--actor")

    alias = sub.add_parser("alias", help="Add or replace a deterministic alias.")
    alias.add_argument("alias")
    alias.add_argument("canonical")

    search = sub.add_parser("search", help="Search the canonical memory store.")
    search.add_argument("query")
    _add_context_fields(search)
    search.add_argument("--type", dest="types", action="append", default=[])
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--explain", action="store_true")
    search.add_argument("--include-deleted", action="store_true")

    many = sub.add_parser("search-many", help="Search several query formulations.")
    many.add_argument("queries", nargs="+")
    _add_context_fields(many)
    many.add_argument("--type", dest="types", action="append", default=[])
    many.add_argument("--limit", type=int, default=10)
    many.add_argument("--explain", action="store_true")
    many.add_argument("--include-deleted", action="store_true")

    backup = sub.add_parser("backup", help="Create a consistent SQLite backup.")
    backup.add_argument("destination")

    evaluate = sub.add_parser(
        "eval",
        help="Run a golden retrieval dataset in an isolated temporary database.",
    )
    evaluate.add_argument("dataset")
    evaluate.add_argument("--min-recall-5", type=float)
    evaluate.add_argument("--min-recall-10", type=float)
    evaluate.add_argument("--max-wrong-scope-rate", type=float)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "eval":
        report = evaluate_dataset(args.dataset)
        _print_json(report.as_dict(), compact=args.compact)
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
        if failures:
            for failure in failures:
                print(f"evaluation gate failed: {failure}", file=sys.stderr)
            return 2
        return 0

    db = Database(args.db)
    service = MemoryService(db, retrieval_log_mode=args.retrieval_log_mode)

    if args.command == "init":
        _print_json(
            {"database": str(db.path), "status": "ready"},
            compact=args.compact,
        )
        return 0

    if args.command == "put":
        memory = service.put(
            MemoryCreate(**_memory_create_kwargs(args)),
            actor=args.actor,
        )
        _print_json(asdict(memory), compact=args.compact)
        return 0

    if args.command == "get":
        memory = service.get(
            args.memory_id,
            include_deleted=args.include_deleted,
        )
        if memory is None:
            _print_json({"error": "not_found", "id": args.memory_id}, compact=args.compact)
            return 1
        _print_json(asdict(memory), compact=args.compact)
        return 0

    if args.command == "update":
        changes = _memory_update_kwargs(args)
        memory = service.update(
            args.memory_id,
            actor=args.actor,
            **changes,
        )
        _print_json(asdict(memory), compact=args.compact)
        return 0

    if args.command == "delete":
        deleted = service.delete(args.memory_id, actor=args.actor)
        _print_json(
            {"id": args.memory_id, "deleted": deleted},
            compact=args.compact,
        )
        return 0 if deleted else 1

    if args.command == "alias":
        service.put_alias(args.alias, args.canonical)
        _print_json(
            {"alias": args.alias, "canonical": args.canonical},
            compact=args.compact,
        )
        return 0

    if args.command == "search":
        results = service.search(
            SearchRequest.with_types(
                query=args.query,
                context=_context_from_args(args),
                types=args.types,
                limit=args.limit,
                explain=args.explain,
                include_deleted=args.include_deleted,
            )
        )
        _print_json(
            {
                "query": args.query,
                "results": [_search_result_payload(result) for result in results],
            },
            compact=args.compact,
        )
        return 0

    if args.command == "search-many":
        results = service.search_many(
            SearchManyRequest.from_queries(
                args.queries,
                context=_context_from_args(args),
                types=args.types,
                limit=args.limit,
                explain=args.explain,
                include_deleted=args.include_deleted,
            )
        )
        _print_json(
            {
                "queries": args.queries,
                "results": [_search_result_payload(result) for result in results],
            },
            compact=args.compact,
        )
        return 0

    if args.command == "backup":
        destination = db.backup(args.destination)
        _print_json(
            {"source": str(db.path), "backup": str(destination)},
            compact=args.compact,
        )
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


def entrypoint() -> None:
    raise SystemExit(main())


def _add_context_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace")
    parser.add_argument("--project")
    parser.add_argument("--repo")
    parser.add_argument("--branch")


def _add_memory_fields(
    parser: argparse.ArgumentParser,
    *,
    require_core: bool,
) -> None:
    parser.add_argument("--title")
    parser.add_argument("--content", required=require_core)
    parser.add_argument("--memory-type", required=require_core)
    parser.add_argument("--scope", required=require_core)
    _add_context_fields(parser)
    parser.add_argument("--importance", type=float)
    parser.add_argument("--confidence", type=float)
    parser.add_argument("--trust", type=float)
    parser.add_argument("--source-type")
    parser.add_argument("--source-ref")
    parser.add_argument("--valid-from")
    parser.add_argument("--valid-until")
    parser.add_argument("--supersedes-id")


def _memory_create_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "title": args.title or "",
        "content": args.content,
        "memory_type": args.memory_type,
        "scope": args.scope,
        "workspace": args.workspace,
        "project": args.project,
        "repo": args.repo,
        "branch": args.branch,
        "importance": 0.5 if args.importance is None else args.importance,
        "confidence": 1.0 if args.confidence is None else args.confidence,
        "trust": 0.5 if args.trust is None else args.trust,
        "source_type": args.source_type,
        "source_ref": args.source_ref,
        "valid_from": args.valid_from,
        "valid_until": args.valid_until,
        "supersedes_id": args.supersedes_id,
    }


def _memory_update_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    field_map = {
        "title": "title",
        "content": "content",
        "memory_type": "memory_type",
        "scope": "scope",
        "workspace": "workspace",
        "project": "project",
        "repo": "repo",
        "branch": "branch",
        "importance": "importance",
        "confidence": "confidence",
        "trust": "trust",
        "source_type": "source_type",
        "source_ref": "source_ref",
        "valid_from": "valid_from",
        "valid_until": "valid_until",
        "supersedes_id": "supersedes_id",
    }
    changes = {
        target: getattr(args, source)
        for source, target in field_map.items()
        if getattr(args, source) is not None
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


def _search_result_payload(result: Any) -> dict[str, Any]:
    return {
        "memory": asdict(result.memory),
        "score": result.score,
        "signals": result.signals,
    }


def _print_json(payload: Any, *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    entrypoint()
