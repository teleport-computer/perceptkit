"""Transaction-owned mutation resources, shared by every Kit write path.

Keys are tuples, never delimiter-concatenated strings. Kit sorts/deduplicates
each acquisition phase: Fact -> Current signal -> Aggregate -> RuleState.
An adapter must preserve that order (do not re-sort hashes used by a DB lock).
"""
from __future__ import annotations

from datetime import date
from typing import Protocol, Sequence

from .errors import RetryableMutationError

MutationKey = tuple[str, ...]


def fact_key(subject: str, signal: str, source: str, source_event_id: str | None,
             *, fallback: str | None = None) -> MutationKey:
    if source_event_id is None and fallback is None:
        raise ValueError("Fact requires source_event_id or canonical fallback")
    return ("10_fact", subject, signal, source,
            "source_id" if source_event_id is not None else "fallback",
            source_event_id if source_event_id is not None else fallback)


def current_key(subject: str, signal: str) -> MutationKey:
    # Reselection may discover multiple dimensions. The signal-wide projection
    # guard protects that discovery without serializing unrelated signals/users.
    return ("20_current", subject, signal)


def aggregate_key(subject: str, signal: str, day: date, kind: str,
                  aggregation_version: int) -> MutationKey:
    return ("30_aggregate", subject, signal, day.isoformat(), kind, str(aggregation_version))


def rule_key(subject: str, definition_id: str, scope_key: str) -> MutationKey:
    return ("40_rule", subject, definition_id, scope_key)


def canonical_keys(keys: Sequence[MutationKey]) -> tuple[MutationKey, ...]:
    return tuple(sorted(set(keys)))


class MutationOwner(Protocol):
    def acquire(self, keys: Sequence[MutationKey]) -> None:
        """Acquire sorted unique keys until the enclosing transaction ENDS.

        New keys must sort after all previously held keys. Reacquiring a subset
        with THIS owner is idempotent. A new/nested mutation_transaction creates
        a distinct owner and must contend; thread identity is not ownership.
        On contention, ordering violation or expired fence, raise
        RetryableMutationError and mark the transaction rollback-only, even if
        a caller catches the exception. An owner cannot be used after exit.
        """
        ...


__all__ = ["MutationKey", "MutationOwner", "RetryableMutationError", "fact_key",
           "current_key", "aggregate_key", "rule_key", "canonical_keys"]
