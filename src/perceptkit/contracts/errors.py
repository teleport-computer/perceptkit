"""契约层的错误类型。

单独一个模块,免得 report / observation 互相 import 成环。
"""
from __future__ import annotations

from typing import Sequence


# Receipt error code: a legacy hash omitted semantic fields, so it cannot prove
# equality with a v2 report. Original envelopes can be explicitly migrated by
# adapters; Kit never guesses the missing original semantics from a retry.
LEGACY_REPORT_SEMANTICS_UNVERIFIABLE = "legacy_report_semantics_unverifiable"


class ContractError(ValueError):
    """契约校验失败。

    **一次报全部问题,不是遇到第一个就抛。** 一批上报里往往同时有好几个字段
    不对,逐个试错要往返很多次;adapter 拿到完整清单才能一次改完。
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class RetryableProjectionError(RuntimeError):
    """Projection contention exhausted; the whole transaction must roll back.

    The caller may retry the unchanged report. No accepted receipt is returned.
    """

    retryable = True

    def __init__(self, projection: str, signal: str, attempts: int) -> None:
        self.projection = projection
        self.signal = signal
        self.attempts = attempts
        super().__init__(f"{signal}: {projection} CAS exhausted after {attempts} attempts")


class RetryableMutationError(RuntimeError):
    """Ownership/fence failed. Roll back and retry the complete operation."""

    retryable = True


__all__ = ["ContractError", "RetryableProjectionError", "RetryableMutationError", "LEGACY_REPORT_SEMANTICS_UNVERIFIABLE"]
