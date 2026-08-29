"""参考实现：把 iOS 的一份快照转成 ``ReportEnvelope``。

**这是模板，不是包的一部分。** 上报适配器天然属于宿主 —— 它要懂某个具体
producer 的形状，而 kit 只认标准信封。把它放在 examples/ 里，是因为
「第一个宿主怎么写这一层」本身就是最难照抄的一段。

## 这一层唯一真正重要的事：不要把没有变成零

iOS 把三种状态编码在 ``data`` 上：

    data 是对象   有值              → observed
    data 是 ""    有权限但这轮没读到 → no_data
    data 是 null  没权限            → unavailable

**后两种写成 0，管线下游每一层都会忠实地处理一份编造的事实**：
规则照常触发、趋势照常计算、agent 照常开口说「你今天一步都没走」。
不会崩，不会报错，只有用户知道那句话不对。
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

#: iOS 的快照 key → kit 的 signal。名字不一样的地方在这里对上，
#: 不要改 kit 那边 —— 每个 producer 的叫法都不一样，改 kit 就是替所有
#: 宿主做决定。
KEY_TO_SIGNAL: dict[str, str] = {
    "time": "time_context",
    "battery": "battery",
    "broadcast": "broadcast",
    "motion_state": "motion_state",
    "focus": "focus_state",
    "audio_route": "audio_route",
    "weather": "weather",
    "playback": "music_playback",
    "health_vitals": "health_vitals",
    "health_sleep": "health_sleep",
    "health_workout": "health_workout",
    "health_activity": "health_activity",
    "health_body": "health_body",
    "health_metabolic": "health_metabolic",
    "health_cycle": "health_cycle",
    "health_mood": "health_mood",
}

#: 这些 key 不进标准管线。写下来免得以后有人当成漏项。
IGNORED_KEYS = {
    # 第三方 app 拿不到的东西，iOS 用它显式声明"这些是 null"。
    "unsupported",
    # 日历和提醒走来源镜像那条路，不是 signal —— 产品规范 §7.13 也是这么定的。
    "calendar_next_event", "reminders",
    # 位置要先在端上解析成城市 / 锚点，坐标不出设备，所以它不是一条直通的观测。
    "location_signal",
}

#: 各信号的字段改名。iOS 的叫法和 manifest 对不上的地方在这里对。
FIELD_ALIASES: dict[str, dict[str, str]] = {
    "battery": {"level": "level_ratio", "charging": "is_charging",
                "low_power_mode": "is_low_power_mode_enabled"},
    "focus_state": {"focused": "is_active"},
    "time_context": {"timezone": "time_zone_id", "local_time": None},
    "weather": {"temperature": "temperature_c",
                "apparent_temperature": "apparent_temperature_c",
                "humidity": "humidity_ratio",
                "precipitation_chance": "precipitation_probability"},
}

#: manifest 里没有、但 iOS 会发的字段。**显式丢掉而不是让它悄悄被过滤** ——
#: 写在这里，下次有人问「我发的 authorization_status 怎么查不到」时有答案。
DROPPED_FIELDS: dict[str, set[str]] = {
    # 授权状态由 availability 表达（unavailable），不再单独当一个字段。
    "focus_state": {"authorization_status"},
    # 本地时间可以从 time_zone_id + occurred_at 推出来，不必存两份。
    "time_context": {"local_time"},
}


#: 有些信号**把授权状态放在 data 里的一个字段上**，而不是用 `data: null`。
#: focus 就是这样：`{"authorization_status": "denied", "focused": null}`。
#: 不认这一条的话，它会被当成 observed，然后因为必填字段是 null 被管线拒收 ——
#: 报出来是"数据格式不对"，实际是"用户没给权限"，排查方向整个跑偏。
AUTH_STATUS_FIELDS: dict[str, str] = {
    "focus_state": "authorization_status",
}

#: 上面那个字段取什么值算"有授权"。
AUTHORIZED_VALUES = {"authorized", "granted", "allowed"}


def _availability(data: Any, signal: str | None = None) -> str:
    """三态判定 —— 这一整个适配器里最要紧的一段。"""
    if data is None:
        return "unavailable"        # 没权限
    if data == "":
        return "no_data"            # 有权限，这轮没读到
    field = AUTH_STATUS_FIELDS.get(signal or "")
    if field and isinstance(data, Mapping):
        status = data.get(field)
        if status is not None and str(status).lower() not in AUTHORIZED_VALUES:
            return "unavailable"    # 授权状态写在字段里的那一类
    return "observed"


def _rename(signal: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """iOS 的字段名 → manifest 的字段名。

    **这一步不对上，整条观测会被管线拒收**（必填字段缺失）。那是好事 ——
    拒收是响的，比默默存下一份字段名对不上、查不出来的数据强。
    """
    alias = FIELD_ALIASES.get(signal, {})
    dropped = DROPPED_FIELDS.get(signal, set())
    out: dict[str, Any] = {}
    for k, v in value.items():
        if v is None or k in dropped:
            continue
        out[alias.get(k, k)] = v
    return out


def report_id_for(payload: Mapping[str, Any]) -> str:
    """**由载荷本身决定**，不掺当前时间、不掺随机数。

    掺进去的话，同一份上报重传两次就变成两份"新"上报，幂等彻底失效 ——
    而重传是常态：网络抖一下、app 被挂起，客户端就会重来一次。
    """
    canonical = repr(sorted(
        (i.get("key"), repr(i.get("data")))
        for i in payload.get("context_snapshot", [])
    )) + str(payload.get("client_ts", ""))
    return "ios-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def to_envelope(payload: Mapping[str, Any], *, occurred_at: str) -> dict[str, Any]:
    """把一份 iOS 快照转成标准上报信封。

    ``occurred_at`` 由调用方给：整份快照是同一时刻采的，而 iOS 的 ``time``
    项里就带着设备本地时间 —— 宿主应该用那个，而不是自己 ``now()``。
    """
    observations: list[dict[str, Any]] = []
    for item in payload.get("context_snapshot", []):
        key = item.get("key")
        if key in IGNORED_KEYS:
            continue
        signal = KEY_TO_SIGNAL.get(key)
        if signal is None:
            # 不认识的 key **跳过而不是报错** —— iOS 可以先发新字段，
            # 后端晚一个版本再认。反过来（报错）会让整批上报失败。
            continue

        data = item.get("data")
        availability = _availability(data, signal)
        obs: dict[str, Any] = {
            "signal": signal,
            "signal_schema_version": 1,
            "occurred_at": occurred_at,
            "availability": availability,
        }
        if availability == "observed" and isinstance(data, Mapping):
            obs["value"] = _rename(signal, data)
        observations.append(obs)

    return {
        "schema_version": 1,
        "report_id": report_id_for(payload),
        "producer": "ios",
        "observations": observations,
    }


__all__ = ["KEY_TO_SIGNAL", "IGNORED_KEYS", "FIELD_ALIASES", "DROPPED_FIELDS",
           "AUTH_STATUS_FIELDS", "AUTHORIZED_VALUES", "report_id_for", "to_envelope"]
