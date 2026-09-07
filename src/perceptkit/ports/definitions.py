"""规则从哪来 —— 宿主实现的窄契约。

kit **不拥有宿主的配置存储**：规则存哪张表、怎么改版本、谁能改，都是宿主的事。
但「宿主自己看着办」到目前为止的实际后果是每家发明一套接法，而其中几件事
做错了不会报错：

    按 subject 加载    漏了就是把别人的规则用在这个人身上
    版本更新           规则改了不换版本，历史事件就再也解释不清是哪条规则产出的
    删除后仍可解释      规则删了，此前产出的事件变成一串无从追溯的 id
    热更新             用户刚改完规则要等进程重启才生效

所以这里定一个**窄接口**：只规定"按 subject 要规则"和"按 id+版本回看一条
已经不在用的规则"，其余（存哪、谁能改、怎么缓存）仍然完全由宿主决定。

静态规则的宿主什么都不用做 —— ``PerceptionKit(definitions=[...])`` 照旧，
内部会包成 :class:`StaticDefinitions`。
"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..rules.types import EventDefinition


@runtime_checkable
class DefinitionProviderPort(Protocol):
    """规则的来源。宿主实现，kit 只调这两个方法。"""

    def definitions_for(self, subject_id: str) -> Sequence[EventDefinition]:
        """这个人当前生效的规则。

        **每次求值都会调**，所以宿主要自己决定缓存策略 —— kit 不缓存，
        缓存了就等于替宿主决定"用户改完规则多久生效"。

        返回空序列 = 这个人没有规则，不是错误。
        """
        ...

    def definition_at(self, definition_id: str,
                      version: int) -> EventDefinition | None:
        """按 id + 版本回看一条规则，**包括已经停用或删除的**。

        为什么必须能回看：事件只记 ``definition_id`` + ``definition_version``。
        规则删掉之后，若这个方法答不出来，那些历史事件就变成一串无从追溯的
        id —— 用户问「这条为什么叫醒我」再也答不了。

        真的查不到就返回 None（比如宿主确实做了硬删除）。**返回 None 是
        一个诚实的答案，编一条出来不是。**
        """
        ...


class StaticDefinitions:
    """一份固定的规则表（宿主装配时传进来的那份）。

    这是 ``PerceptionKit(definitions=[...])`` 的内部形态 —— 让「静态规则」
    和「按用户加载」走同一条代码路径，而不是在求值处到处 if。
    """

    __slots__ = ("_definitions", "_by_key")

    def __init__(self, definitions: Sequence[EventDefinition] = ()):
        self._definitions = tuple(definitions)
        # (id, version) -> 规则。同一 id 的不同版本都留着，这样删掉/改版之后
        # 历史事件仍解释得出来。
        self._by_key = {(d.definition_id, d.version): d
                        for d in self._definitions}

    def definitions_for(self, subject_id: str) -> Sequence[EventDefinition]:
        # `subject_id is None` 的是宿主级规则，对所有人生效；带 subject_id 的
        # 只给那个人。**这个筛选必须在这里做**：漏了就是把别人的规则用在
        # 这个人身上，而它不会报错。
        return tuple(d for d in self._definitions
                     if d.subject_id is None or d.subject_id == subject_id)

    def definition_at(self, definition_id: str,
                      version: int) -> EventDefinition | None:
        return self._by_key.get((definition_id, version))

    def __len__(self) -> int:
        return len(self._definitions)

    def __iter__(self):
        return iter(self._definitions)


def as_provider(source) -> DefinitionProviderPort:
    """把宿主传进来的东西统一成 provider。

    传序列 → 包成 StaticDefinitions；传 provider → 原样用。
    这一层存在的意义是让求值处只认一种形状。
    """
    if source is None:
        return StaticDefinitions(())
    if isinstance(source, DefinitionProviderPort) and not isinstance(
            source, (list, tuple)):
        return source
    return StaticDefinitions(tuple(source))


__all__ = ["DefinitionProviderPort", "StaticDefinitions", "as_provider"]
