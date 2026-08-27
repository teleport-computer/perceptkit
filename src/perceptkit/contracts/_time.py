"""时间戳解析 —— 只用标准库,兼容到 Python 3.10。

为什么不直接用 ``datetime.fromisoformat``:3.10 的版本只认
``datetime.isoformat()`` 自己吐出来的那个窄格式 —— ``Z`` 后缀不认,
少写秒不认。真实 producer(iOS / Android / 各种 SDK)发过来的东西比那宽。
3.11 起放宽了,但这个包声明支持 3.10,所以自己兜一层。

**返回的一律是 aware datetime。** naive 的时间戳在这套系统里没有意义 ——
归属到哪一天完全取决于时区,丢了偏移就只能猜,而猜错会静默写进历史。
"""
from __future__ import annotations

from datetime import datetime, timezone

#: ``fromisoformat`` 兜不住时挨个试的格式。按常见程度排。
_FALLBACK_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M%z",
    "%Y-%m-%d %H:%M:%S%z",
)


class TimestampError(ValueError):
    """时间戳解析不了,或者解析出来是 naive 的。"""


def parse_timestamp(raw: object, *, field: str = "timestamp") -> datetime:
    """把 ISO 8601 字符串解析成带时区的 :class:`datetime`。

    也接受 :class:`datetime` 本身(必须 aware)。naive 一律拒绝 —— 见模块开头。
    """
    if isinstance(raw, datetime):
        if raw.tzinfo is None or raw.tzinfo.utcoffset(raw) is None:
            raise TimestampError(
                f"{field}: naive datetime; 时间戳必须带 UTC 偏移,"
                f"否则归属到哪一天只能靠猜"
            )
        return raw

    if not isinstance(raw, str) or not raw.strip():
        raise TimestampError(f"{field}: expected an ISO 8601 string, got {raw!r}")

    text = raw.strip()
    # 3.10 的 fromisoformat 不认 Z;统一换成显式偏移。
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _FALLBACK_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise TimestampError(f"{field}: cannot parse {raw!r} as ISO 8601")
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise TimestampError(
            f"{field}: {raw!r} 没有 UTC 偏移;时间戳必须带偏移,"
            f"否则归属到哪一天只能靠猜"
        )
    return parsed


def to_iso(value: datetime) -> str:
    """序列化回 ISO 8601。UTC 用 ``+00:00`` 而不是 ``Z``,保持一种写法。"""
    if value.tzinfo is None:
        raise TimestampError("naive datetime cannot be serialised")
    return value.isoformat()


def utc_now_is_not_available() -> None:
    """占位:这个包不读时钟。

    "现在几点"是宿主的事 —— 内核读时钟就没法测试、没法重放、没法在
    conformance test 里固定输入。需要"现在"的地方一律由调用方把时间传进来。
    """
    raise NotImplementedError(
        "perceptkit 不读时钟:需要 now 的地方由宿主传入,"
        "这样才能重放和测试"
    )


__all__ = ["TimestampError", "parse_timestamp", "to_iso", "timezone"]
