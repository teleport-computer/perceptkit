"""Schema 版本与兼容规则。

三个互不相干的版本号,别混:

    REPORT_SCHEMA_VERSION   上报信封的解释版本。producer 写,kit 读。
    EVENT_SCHEMA_VERSION    事件信封的解释版本。kit 写,宿主 runtime 读。
    signal_schema_version   某个 signal 的 payload 版本。逐信号独立演进,
                            在 manifest 里声明,不在这里。

**跨版本客户端一定会同时在线。** 用户不会同时升级 App —— 老版本 iOS 还在
发 v1 的时候,后端已经是 v2 了。所以"版本不认识时怎么办"必须是协议的一部分,
不能留给每个宿主自己拍。

规则(**待 Seven 确认,见 OPEN-QUESTIONS B6**):

    主版本不认识   拒收,返回明确的错误码。宁可让 producer 看到失败并重试/升级,
                   也不要用猜出来的语义去解释一批数据 —— 猜错会静默污染历史。
    主版本认识     接收。payload 里多出来的字段忽略掉(向前兼容:新版 producer
                   发了新字段,老版 kit 读不懂但不该因此拒收整批)。
    字段缺失       按各自契约的必填规则判,和版本无关。

"忽略未知字段"是有意的:它让 producer 可以先发新字段、宿主后升级,
两边不用同步发版。代价是拼错的字段名不会报错 —— 由 manifest 校验去兜。
"""
from __future__ import annotations

#: 上报信封的当前版本。
REPORT_SCHEMA_VERSION = 1

#: 事件信封的当前版本。
EVENT_SCHEMA_VERSION = 1

#: 能解释的上报主版本。收到不在这里面的,拒收整批。
SUPPORTED_REPORT_VERSIONS: frozenset[int] = frozenset({1})


class UnsupportedSchemaVersion(ValueError):
    """收到了看不懂的 schema 版本。

    带上收到的版本和支持的版本,让 adapter 能把这个信息回给 producer ——
    producer 据此决定是升级还是降级重发,而不是盲目重试。
    """

    def __init__(self, got: object, supported: frozenset[int]) -> None:
        self.got = got
        self.supported = supported
        super().__init__(
            f"unsupported schema_version {got!r}; this build understands "
            f"{sorted(supported)}"
        )


def check_report_version(version: object) -> int:
    """校验上报信封的版本,返回归一后的整数版本号。

    不认识就抛 :class:`UnsupportedSchemaVersion` —— 见模块开头,这是有意的:
    用猜出来的语义解释一批数据,错了是静默污染历史,比拒收贵得多。
    """
    if isinstance(version, bool) or not isinstance(version, int):
        raise UnsupportedSchemaVersion(version, SUPPORTED_REPORT_VERSIONS)
    if version not in SUPPORTED_REPORT_VERSIONS:
        raise UnsupportedSchemaVersion(version, SUPPORTED_REPORT_VERSIONS)
    return version
