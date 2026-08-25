"""测量的幂等键。

★ 为什么不能用测量时间（设计文档修订 B）：同一时刻可以有不同设备、不同指标、
  多条样本；同一条样本还会被修订或删除。按时间去重会同时造成误删和重复累计，
  而 min/max/sum/count 这类累积形状一旦被重复上传污染就无法回滚 —— 后端不存
  原始点，扣不回去。

★ 三段都必须由来源提供。缺任何一段就拒绝造键：造一个假键出来，
  等于让这条记录每次上传都被当成新的。

★ 零 I/O、无随机、跨进程稳定（不用内置 hash()，它每进程加盐）。
"""
from __future__ import annotations

import hashlib


class MissingIdentity(ValueError):
    """来源没给齐 (source, metric, sample_id)，无法安全去重。"""


def _encode_part(value: str) -> str:
    """长度前缀编码单个字段：``f"{len(v)}:{v}"``。

    ★ 为什么不能用分隔符拼接（Codex code_review 2026-08-23 抓到）：任何固定
      分隔符都可能出现在字段内容里，造成两个不同的三元组拼出同一段字节：
      ``("s", "a\\x1fb", "c")`` 与 ``("s\\x1fa", "b", "c")`` 用 ``\\x1f`` 拼接
      结果完全相同，无论截多少位哈希都会撞键。长度前缀是自描述的：每一段
      自带自己的字节长度，解析在读到冒号后精确消费 N 个字节，不依赖内容
      里不出现某个字符这条无法保证的假设——第三方适配器迟早会传入任意字节。
    """
    v = value.strip()
    return f"{len(v.encode('utf-8'))}:{v}"


def measurement_key(*, source: str, metric: str, sample_id: str) -> str:
    """`(来源命名空间, 指标, 来源侧稳定样本 id)` -> 稳定的十六进制键。"""
    parts = {"source": source, "metric": metric, "sample_id": sample_id}
    missing = sorted(k for k, v in parts.items() if not str(v or "").strip())
    if missing:
        raise MissingIdentity(f"缺少 {missing}，无法构造幂等键")
    raw = "".join(_encode_part(str(parts[k])) for k in ("source", "metric", "sample_id"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
