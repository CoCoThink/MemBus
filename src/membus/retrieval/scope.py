"""Scope validation and eligibility."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Memory, MemoryCreate, Scope, SearchContext


class ScopeValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    eligible: bool
    reason: str


def validate_scope(memory: MemoryCreate) -> Scope:
    try:
        scope = Scope(str(memory.scope))
    except ValueError as exc:
        raise ScopeValidationError(f"Unsupported memory scope: {memory.scope!r}") from exc

    if scope is Scope.WORKSPACE and not memory.workspace:
        raise ScopeValidationError("workspace scope requires workspace")
    if scope is Scope.PROJECT and not memory.project:
        raise ScopeValidationError("project scope requires project")
    if scope is Scope.REPO and not memory.repo:
        raise ScopeValidationError("repo scope requires repo")
    if scope is Scope.BRANCH and (not memory.repo or not memory.branch):
        raise ScopeValidationError("branch scope requires repo and branch")

    return scope


def scope_eligibility(memory: Memory, context: SearchContext) -> ScopeDecision:
    """Exclude only known context mismatches; do not score scope."""

    try:
        scope = Scope(memory.scope)
    except ValueError:
        return ScopeDecision(False, "unknown memory scope")

    if (
        memory.workspace is not None
        and context.workspace is not None
        and memory.workspace != context.workspace
    ):
        return ScopeDecision(False, "workspace mismatch")

    if (
        scope in {Scope.PROJECT, Scope.REPO, Scope.BRANCH}
        and memory.project is not None
        and context.project is not None
        and memory.project != context.project
    ):
        return ScopeDecision(False, "project mismatch")

    if (
        scope in {Scope.REPO, Scope.BRANCH}
        and memory.repo is not None
        and context.repo is not None
        and memory.repo != context.repo
    ):
        return ScopeDecision(False, "repo mismatch")

    if (
        scope is Scope.BRANCH
        and context.branch is not None
        and memory.branch != context.branch
    ):
        return ScopeDecision(False, "branch mismatch")

    return ScopeDecision(True, f"{scope.value} applicable")
