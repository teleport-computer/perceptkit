"""契约层的错误类型。

单独一个模块,免得 report / observation 互相 import 成环。
"""
from __future__ import annotations

from typing import Sequence


class ContractError(ValueError):
    """契约校验失败。

    **一次报全部问题,不是遇到第一个就抛。** 一批上报里往往同时有好几个字段
    不对,逐个试错要往返很多次;adapter 拿到完整清单才能一次改完。
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


__all__ = ["ContractError"]
