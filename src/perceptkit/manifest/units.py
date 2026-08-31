"""单位换算 —— 把 producer 发来的单位统一成 manifest 声明的那个。

**为什么必须换算而不是拒收**：同一个数据可能来自不同来源，各自习惯的单位不一样
（美区设备报磅、国区报公斤）。拒收会让一整类用户的数据进不来。

**但换算会放大错误，所以从来不能单独用**：

    用户体重 70 kg，设备把单位标成了 lb（标错了）
      → 我们老老实实换算：70 lb = 31.8 kg
      → 用户的体重记录突然掉了一半，而且没有任何报错
      → 值域校验也拦不住（31.8 kg 完全合法）

所以三道一起：

    ① 换算      按声明的单位转成标准单位，原始单位留作 metadata
    ② 值域校验  换算【之后】再查一遍范围
    ③ 跳变阈值  和上一次比，变化超过阈值就报 conflict —— 不拒收，交给宿主决定

第 ③ 道是唯一能挡住"单位标错"的：体重一次掉一半，不管单位对不对都不正常。
它也会误伤（用户真换了体重计），所以只报 conflict 不拒收。
"""
from __future__ import annotations

from typing import Callable

#: 换算表：``(来的单位, 目标单位) -> 换算函数``。
#: 刻意只放真实会遇到的，不做通用单位库 —— 那是另一个包的活。
_CONVERSIONS: dict[tuple[str, str], Callable[[float], float]] = {
    # 质量
    ("lb", "kg"): lambda v: v * 0.45359237,
    ("g", "kg"): lambda v: v / 1000.0,
    # 温度
    ("fahrenheit", "celsius"): lambda v: (v - 32.0) * 5.0 / 9.0,
    # 长度
    ("in", "cm"): lambda v: v * 2.54,
    ("m", "cm"): lambda v: v * 100.0,
    ("ft", "cm"): lambda v: v * 30.48,
    # 距离
    ("km", "m"): lambda v: v * 1000.0,
    ("mi", "m"): lambda v: v * 1609.344,
    # 能量
    ("kj", "kcal"): lambda v: v / 4.184,
    # 血糖 —— 换算系数取决于摩尔质量，葡萄糖是 18.0182
    ("mg_dl", "mmol_l"): lambda v: v / 18.0182,
}


class UnitError(ValueError):
    """来的单位换算不到目标单位。"""


def can_convert(source: str, target: str) -> bool:
    return source == target or (source, target) in _CONVERSIONS


def convert(value: float, *, source: str, target: str) -> float:
    """把 ``value`` 从 ``source`` 换算成 ``target``。

    换不了就抛 —— **不要静默按原值放行**：一个 lb 的数字当 kg 存进去，
    比拒收难查得多（没有报错，只有一个悄悄错掉一半的体重）。
    """
    if source == target:
        return float(value)
    fn = _CONVERSIONS.get((source, target))
    if fn is None:
        raise UnitError(f"没有 {source!r} -> {target!r} 的换算")
    return fn(float(value))


def relative_jump(new: float, old: float) -> float | None:
    """两次测量的相对变化幅度。``old`` 为 0 或缺失时返回 ``None``（比不了）。

    用相对值不用绝对值：体重差 5 公斤和血糖差 5 mmol/L 是完全不同量级的事，
    每个字段各写一个绝对阈值既啰嗦又容易写错。
    """
    if old in (None, 0) or new is None:
        return None
    try:
        return abs((float(new) - float(old)) / float(old))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


__all__ = ["UnitError", "can_convert", "convert", "relative_jump"]
