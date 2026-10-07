"""Scope validation and contextual affinity."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Memory, MemoryCreate, Scope, SearchContext


class ScopeValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    eligible: bool
    score: float
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


def scope_affinity(memory: Memory, context: SearchContext) -> ScopeDecision:
    """Return eligibility plus a bounded scope-affinity score.

    A known contextual mismatch excludes a scoped memory. If the caller did not
    provide that context dimension, the memory remains searchable with a modest
    score so explicit global searches can still find it.
    """

    try:
        scope = Scope(memory.scope)
    except ValueError:
        return ScopeDecision(False, 0.0, "unknown memory scope")

    if scope is Scope.GLOBAL:
        return ScopeDecision(True, 0.40, "global memory")

    if scope is Scope.WORKSPACE:
        return _match_one(
            expected=memory.workspace,
            actual=context.workspace,
            matched=0.60,
            label="workspace",
        )

    if scope is Scope.PROJECT:
        return _match_one(
            expected=memory.project,
            actual=context.project,
            matched=0.75,
            label="project",
        )

    if scope is Scope.REPO:
        return _match_one(
            expected=memory.repo,
            actual=context.repo,
            matched=0.90,
            label="repo",
        )

    # branch scope requires both repo and branch.
    if context.repo is not None and context.repo != memory.repo:
        return ScopeDecision(False, 0.0, "repo mismatch")
    if context.branch is not None and context.branch != memory.branch:
        return ScopeDecision(False, 0.0, "branch mismatch")
    if context.repo == memory.repo and context.branch == memory.branch:
        return ScopeDecision(True, 1.00, "repo and branch match")
    return ScopeDecision(True, 0.45, "branch context not fully specified")


def _match_one(
    *,
    expected: str | None,
    actual: str | None,
    matched: float,
    label: str,
) -> ScopeDecision:
    if actual is None:
        return ScopeDecision(True, 0.45, f"{label} context not specified")
    if expected == actual:
        return ScopeDecision(True, matched, f"{label} match")
    return ScopeDecision(False, 0.0, f"{label} mismatch")
