"""规则改了、删了之后，旧事件仍然解释得通（外部审查 F11 的核心那半）。

事件只记 ``(规则 id, 版本)``。用户问「这条为什么叫醒我」，答案要靠回看
**当时那一版**的规则。所以规则被改版或删掉之后，旧版本必须还查得到。

现在的缺口：宿主直接 ``kit.definitions = [...]`` 换一份规则（热更新最朴素
的形态）时，整份规则表被换掉，**上一版连同它的历史一起消失** ——
上周那条"体重超 71kg 提醒"改成 75 之后，上周那条提醒就再也解释不清了。
"""
from __future__ import annotations

from perceptkit import PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.manifest import MINIMAL_SIGNALS
from perceptkit.rules import EventDefinition


def _rule(value: int, version: int) -> EventDefinition:
    return EventDefinition.parse({
        "id": "weight_over", "version": version,
        "source": {"signal": "health_weight", "field": "weight_kg"},
        "condition": {"type": "threshold_crossing", "operator": "gte",
                      "value": value},
        "event": {"type": "health.weight_over"},
    })


def test_an_old_version_is_still_explainable_after_the_rules_are_swapped():
    kit = PerceptionKit(storage=InMemoryStorage(), signals=MINIMAL_SIGNALS,
                        definitions=[_rule(71, version=1)])
    assert kit.definition_at("weight_over", 1) is not None

    kit.definitions = [_rule(75, version=2)]        # 用户把阈值改了

    assert kit.definition_at("weight_over", 2) is not None, "新版本查不到"
    old = kit.definition_at("weight_over", 1)
    assert old is not None, \
        "换了规则之后，上一版查不到了 —— 上周那条提醒再也解释不清为什么发"
    assert old.value == 71, "查到的不是当时那一版"


def test_a_deleted_rule_is_still_explainable():
    """规则被整条删掉（而不是改版）同样要留得住。"""
    kit = PerceptionKit(storage=InMemoryStorage(), signals=MINIMAL_SIGNALS,
                        definitions=[_rule(71, version=1)])
    assert kit.definition_at("weight_over", 1) is not None

    kit.definitions = []                            # 用户把这条规则删了

    assert kit.definitions_for("u") == (), "删掉之后还在生效"
    assert kit.definition_at("weight_over", 1) is not None, \
        "规则删了，此前产出的事件就变成一串无从追溯的 id"


def test_the_host_provider_stays_the_authority_for_what_is_in_effect():
    """反向守卫：留住历史**不等于**让旧规则继续生效。

    两件事混了的话，用户删掉的规则会继续叫醒他 —— 比查不到历史严重得多。
    """
    kit = PerceptionKit(storage=InMemoryStorage(), signals=MINIMAL_SIGNALS,
                        definitions=[_rule(71, version=1)])
    kit.definitions = [_rule(75, version=2)]

    live = kit.definitions_for("u")
    assert [d.version for d in live] == [2], f"旧版本还在生效：{live}"
