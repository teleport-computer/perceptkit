# iOS 上报样本

**这些是按 iOS 端的真实结构手工构造的，不是从真机抓下来的。**
写清楚这一点很重要 —— 它们能证明「结构对得上」，证明不了「真机会发出这些值」。

结构依据（`feedling-mcp-ios`）：

    App/FeedlingTest/Pages/Settings/Perception/PerceptionContextSnapshot.swift
    App/FeedlingTest/API/FeedlingAPI.swift  uploadPerceptionSnapshot

整体形状：

    { "context_snapshot": [ {key, data, message}, ... ], "client_ts": "..." }

**三态就编码在 `data` 上**，这是适配器最容易做错的一处：

    data 是对象   有值        → observed
    data 是 ""    有权限但这轮没读到 → no_data
    data 是 null  没权限      → unavailable

把后两种写成 `0`，管线下游每一层都会忠实地处理一份编造的事实。
