"""一瞥（glance）必须仍然只出 bool——这是设计里「坚决不给配」的第一条。

原测试文件另有一条 ``test_io_shells_reexport_kernel_objects``,断言宿主的
re-export 壳和内核对象是同一批对象——那是宿主集成测试,不属于这个包,
迁移时未带过来（宿主侧应在自己仓库里保留一份等价断言）。
"""
from __future__ import annotations

import sensegate.glance as glance


def test_glance_emits_only_booleans():
    out = glance.build_perception_glance(
        {
            "location": {"place_label": {"v": "office"}},
            "sleep": {"asleep_minutes": {"v": 312}},
        },
        notable_changes=[{"signal": "health_sleep", "field": "asleep_minutes"}],
    )
    for group in out.values():
        for value in group.values():
            assert isinstance(value, bool), out


def test_glance_of_empty_input_is_still_a_dict():
    assert isinstance(glance.build_perception_glance({}), dict)
