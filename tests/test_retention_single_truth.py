"""公开的 retention 查询必须和 manifest 是同一个答案。

外部审查（2026-09-03 §9）复现的：``perceptkit.retention_days()`` 读的是
``retention.RETENTION_DAYS``（旧目录名、旧数字），和 manifest **七条全对不上**：

    retention_days("focus_state")    → KeyError      manifest 说 365
    retention_days("motion_state")   → 90            manifest 说 365
    retention_days("music_playback") → KeyError      manifest 说 365
    retention_days("audio_route")    → 90            manifest 说 7
    retention_days("weather")        → 90            manifest 说 7

接入方和工程 AI 调公开 API 会拿到错的结论，**而且没有任何地方报错** ——
两个真相各自自洽，只是不一样。这个文件的作用是让它们不可能再分开。
"""
from __future__ import annotations

import pytest

import perceptkit
from perceptkit.manifest import MINIMAL_SIGNALS
from perceptkit.manifest.types import PERMANENT
from perceptkit.retention import _LEGACY_NAMES


@pytest.mark.parametrize("signal", sorted(MINIMAL_SIGNALS))
def test_every_signal_answers_the_same_as_the_manifest(signal):
    sig = MINIMAL_SIGNALS[signal]
    assert perceptkit.stores_history(signal) is bool(sig.stores_history)
    if not sig.stores_history:
        # 抛错而不是返回 None —— 见 retention_days 的 docstring。
        with pytest.raises(KeyError):
            perceptkit.retention_days(signal)
        return
    assert perceptkit.retention_days(signal) == sig.history_retention_days


@pytest.mark.parametrize("old,new", sorted(_LEGACY_NAMES.items()))
def test_a_legacy_name_answers_the_same_as_the_name_it_maps_to(old, new):
    """旧名只能是别名，不能是第二套目录。"""
    assert new in MINIMAL_SIGNALS, f"{old} 指向了一个 manifest 里没有的 {new}"
    assert perceptkit.retention_days(old) == perceptkit.retention_days(new)
    assert perceptkit.stores_history(old) is perceptkit.stores_history(new)


def test_an_unknown_signal_is_not_history():
    with pytest.raises(KeyError):
        perceptkit.retention_days("从来没有过的信号")
    assert perceptkit.stores_history("从来没有过的信号") is False


def test_the_old_table_is_no_longer_consulted():
    """反过来钉一次：旧表里那些和 manifest 不一致的数字，一个都不许再冒出来。

    ``audio_route`` 在旧表里是 90，在 manifest 里是 7。查出 90 就说明
    公开 API 又走回旧表了。
    """
    from perceptkit.retention import RETENTION_DAYS
    disagreeing = []
    for old_name, old_days in RETENTION_DAYS.items():
        try:
            got = perceptkit.retention_days(old_name)
        except KeyError:
            continue
        if old_days is not None and got != old_days:
            disagreeing.append((old_name, old_days, got))
    assert disagreeing, (
        "旧表和 manifest 现在处处一致 —— 那这条测试什么也没验到。"
        "要么旧表被清空了（那就删掉这条），要么公开 API 又走回旧表了"
    )
    for name, old_days, got in disagreeing:
        assert got == perceptkit.retention_days(name), name


def test_permanent_is_reported_as_permanent_not_as_a_day_count():
    """-1 是「永久」不是「负一天」。当成天数用会把永久数据立刻扫掉。"""
    permanent = [k for k, s in MINIMAL_SIGNALS.items()
                 if s.stores_history and s.history_retention_days == PERMANENT]
    assert permanent, "一个明细永久的信号都没有 —— 这条测试什么也没验到"
    for k in permanent:
        assert perceptkit.retention_days(k) == PERMANENT
