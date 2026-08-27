"""读取侧 —— agent 主动来查的八个函数。

和写入侧方向相反、共用同一份存储。**不是转发器**:里面装着 TTL 判定、
趋势模型选择、缺数据显式化、隐私投影四样每个宿主都必须一样的逻辑。

MCP 工具那层(工具名、描述、给谁开、返回怎么写)留在宿主 —— 全是产品决策。
"""
from __future__ import annotations

from .api import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    CurrentView,
    DailyView,
    get_current,
    get_daily_aggregates,
    get_last_known,
    get_trend,
    list_calendar_events,
    list_events,
    list_reminders,
    list_timeline,
    project,
    visible_fields,
)

__all__ = [
    "get_current", "get_last_known", "list_timeline", "get_daily_aggregates",
    "get_trend", "list_calendar_events", "list_reminders", "list_events",
    "CurrentView", "DailyView", "visible_fields", "project",
    "DEFAULT_LIMIT", "MAX_LIMIT",
]
