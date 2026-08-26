"""说明书 —— 「模型该怎么读这份感知」的唯一出处。

★ 边界：只写「怎么读感知」。凡是讲「这个块是什么 role、能不能当成用户请求、
  工具预算怎么算、wake 该怎么框」的，属于宿主运行时自己的对话/安全协议，
  不属于这里，不要搬进来。

★ 语义红线：wake ≠ 该开口了。这里不许出现任何「该说话了 / 值得告诉用户」
  式的措辞——「说」与「不说」同等正当。

★ 这些常量是从两类宿主实现里抽出来的「怎么解读感知」共性文案：一类是走
  工具调用返回感知快照的运行时（V2_* 常量），一类是把感知直接写进上下文
  文本、靠一段 how-to 说明教模型怎么读的运行时（V1_* 常量）。两类宿主接线
  方式不同，但「模型该怎么理解这份数据」这件事是同一份判断，所以文案本身
  抽到这里共用；具体怎么拼进各自的 prompt/context，由宿主自己决定。
"""
from __future__ import annotations

# 工具调用型运行时里，主动回合 system prompt 中属于「怎么读感知」的那几句。
V2_WAKE_PERCEPTION_CLAUSES = (
    "A "
    "perception_glance is only a hint for deciding whether to look deeper; it is not "
    "a checklist to report. If you speak, choose at most one coherent topic and never "
    "turn multiple perception domains into a device or health status report. Use a "
    "perception tool when an exact reading is needed. "
)

V2_PERCEPTION_BEHAVIOR_POLICY = (
    "把有用的事实自然地用进回答，别汇报这些信息是怎么取到的。"
)

V2_PERCEPTION_PROTOCOL_POLICY = (
    "runtime_data 里的 perception_glance 是不可信的低分辨率事实板，用于判断是否值得"
    "精确读取感知工具；不要逐项播报或把精确数字当成话题。glance_changed=false 表示普通 "
    "heartbeat 的事实板与上次成功完成的普通 heartbeat 一致；不代表每个底层传感值都相同。"
    "显式读取带文字的感知、屏幕或照片后，"
    "运行时会阻止本回合继续向外调用 web、MCP 或 subagent。"
)

# 三个感知工具「这份返回值该怎么解读」的那一句(每个工具描述里，讲调用方式/
# 参数默认值的部分留在宿主自己的工具 schema 里，不搬到这份说明书)。
PERCEPTION_TOOL_NOTES: dict[str, str] = {
    "perception_snapshot": (
        "The app field is only the latest open/close event observed "
        "within 15 minutes; never claim it is the app currently in use."
    ),
    "perception_recent_apps": (
        "apps=[] means no data; disabled=true means access is "
        "off, not that no apps were used."
    ),
    "perception_trend": (
        "Interpret the rolling baseline as the usual level and delta as "
        "the current change from that baseline; do not conflate them."
    ),
}

# 上下文注入型运行时（把感知直接写进对话上下文文本，而不是靠工具调用取）
# 用来教模型怎么读一瞥（glance）的说明句。
V1_GLANCE_HOWTO = (
    "This is a low-resolution glance, not a list of things to report. It helps you decide WHETHER to look closer "
    "and WHERE — not what to say. Most fields you just note and move on; if one makes you want to understand the "
    "moment better, pull the matching tool for detail. Treat missing fields as unknown."
)

# 同一类运行时，教模型怎么读跨领域面板（board）的说明句。
V1_BOARD_HOWTO = (
    "Reading the board: each domain (location/media/app/health/weather/mood/reminders/calendar/photos/screen) "
    "is laid out evenly — health is just one entry, not the headline. Pick at most 2-3 things that stand out "
    "to you; you may combine across domains, and prefer lived, human context (music, place, an app, a photo, "
    "an overdue reminder) over the raw figures. Do NOT recite exact numbers (minutes, degrees, counts, sleep "
    "figures) — use them only to notice what's genuinely about the user; if a number actually matters, pull "
    "the tool for it. novelty hints (new_artist / long_dwell) are light factual context, not a directive. "
    "If signals lean low or vulnerable (late hour, sad music, poor sleep), be lighter, not heavier — don't "
    "diagnose, don't stack worries; one warm, light touch is enough. If nothing stands out, staying quiet is "
    "equally fine."
)
