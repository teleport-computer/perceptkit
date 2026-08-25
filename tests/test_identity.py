"""幂等键 —— 认出「这条我已经收过了」。

设计文档修订 B：不能用测量时间当去重键。同一时刻可以有不同设备、不同指标、
多条样本；同一条样本还可能被修订或删除。键必须是
(来源命名空间, 指标, 来源侧稳定样本 id)。
"""
from __future__ import annotations


import pytest


import sensegate.identity as identity


def test_same_three_parts_give_the_same_key():
    a = identity.measurement_key(source="healthkit", metric="body_mass", sample_id="ABC-1")
    b = identity.measurement_key(source="healthkit", metric="body_mass", sample_id="ABC-1")
    assert a == b


def test_different_metric_at_the_same_instant_is_a_different_key():
    # 同一次体检同时记了体重和体脂 —— 两条，不是一条
    a = identity.measurement_key(source="healthkit", metric="body_mass", sample_id="S1")
    b = identity.measurement_key(source="healthkit", metric="body_fat", sample_id="S1")
    assert a != b


def test_different_source_is_a_different_key():
    # 体重秤和手环都报了体重，是两条独立观测
    a = identity.measurement_key(source="healthkit", metric="body_mass", sample_id="S1")
    b = identity.measurement_key(source="oura", metric="body_mass", sample_id="S1")
    assert a != b


def test_key_is_stable_across_processes():
    # 不能用 hash()（每进程加盐会变），也不能带随机数
    key = identity.measurement_key(source="healthkit", metric="body_mass", sample_id="ABC-1")
    assert key == identity.measurement_key(source="healthkit", metric="body_mass", sample_id="ABC-1")
    assert isinstance(key, str) and len(key) >= 16


def test_separator_cannot_be_smuggled_in():
    # 「a|b + c」和「a + b|c」必须是两个键，否则伪造 id 能撞掉别人的记录
    a = identity.measurement_key(source="s", metric="a|b", sample_id="c")
    b = identity.measurement_key(source="s", metric="a", sample_id="b|c")
    assert a != b


def test_internal_separator_byte_cannot_be_smuggled_in():
    # 独立代码评审抓到的真实碰撞：旧实现用 \x1f 拼接三段，这两个不同的三元组
    # 拼出来的原始字节完全相同 —— 不管截多少位哈希都会撞键：
    #   ("s", "a\x1fb", "c") -> "s" + \x1f + "a\x1fb" + \x1f + "c"
    #   ("s\x1fa", "b", "c") -> "s\x1fa" + \x1f + "b" + \x1f + "c"
    # 两串字节完全一样。修复后必须是两个不同的键。
    a = identity.measurement_key(source="s", metric="a\x1fb", sample_id="c")
    b = identity.measurement_key(source="s\x1fa", metric="b", sample_id="c")
    assert a != b


@pytest.mark.parametrize("kwargs", [
    {"source": "", "metric": "body_mass", "sample_id": "S1"},
    {"source": "healthkit", "metric": "", "sample_id": "S1"},
    {"source": "healthkit", "metric": "body_mass", "sample_id": ""},
    {"source": "healthkit", "metric": "body_mass", "sample_id": "   "},
])
def test_missing_any_part_is_refused(kwargs):
    # 缺任何一段就不许造键 —— 造一个假键出来会静默产生重复计数
    with pytest.raises(identity.MissingIdentity):
        identity.measurement_key(**kwargs)
