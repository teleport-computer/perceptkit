"""一分钟跑通：一次"回到常去的地点"，串起 perceptkit 的六个判断。

    uv run python3 examples/quickstart.py

全程不联网、不需要 API key、不碰任何存储 —— 这正是这个包的承诺：
给它当下的信号，它只回答判断，剩下的（采集/存储/调模型）都是调用方的事。

场景：老王最近这几晚睡得不好，此刻他的手机检测到他到了"公司"——
一个他经常去的地方。走一遍从"一条原始测量"到"该怎么讲给模型听"的全程。
"""
from __future__ import annotations

from perceptkit.algorithms import attribution, history, observation, streaks, trend_models, wake
from perceptkit.algorithms.glance import build_perception_glance
from perceptkit.prompts import V1_BOARD_HOWTO, V1_GLANCE_HOWTO


def main() -> None:
    print("① 一条测量：先分清「有值」「测到是零」「没测到」「不可用」四态")
    # 昨晚的手表数据正常同步——有观测值，用来判断好坏。
    last_night = observation.classify(180.0)  # 180 分钟，睡得不好，但确实测到了
    print(f"     昨晚(有数据) -> {last_night}")
    # 前晚没戴表：查询成功但没有样本——这不是"没睡"，是"没测到"。
    no_watch = observation.classify(None)
    print(f"     前晚(没戴表) -> {no_watch}  （不能当成 0 分钟，也不能当成没发生过）")
    # 上周健康权限被关掉那几天：压根拿不到数据，跟"没测到"是两回事。
    permission_off = observation.classify(None, available=False)
    print(f"     权限关闭那几天 -> {permission_off}  （不可采信，不参与任何判断）")

    print("\n② 一条测量该记在哪一天：睡眠区间整体归「醒来」那天")
    # 23:10 睡下、次日 06:15 醒来——横跨了午夜，但睡眠不能拆成两天，
    # 直觉上"昨晚的觉"应该算在醒来那天。
    sleep_day = attribution.attribute_episode(
        "2026-08-22T23:10:00-07:00", "2026-08-23T06:15:00-07:00"
    )
    print(f"     23:10 睡下、06:15 醒来 -> 记在 {sleep_day}")

    print("\n③ 到了常去的地方：这次变化值不值得戳一下 agent")
    # 第一次到达：没有上次戳醒的记录，直接放行。
    ok1, reason1 = wake.should_wake(
        "arrival", enabled_sources=("arrival",), last_wake_ts=None, now=1000.0, debounce_sec=60.0
    )
    print(f"     第一次到达 -> 戳醒={ok1}  原因={reason1}")
    # 30 秒后 GPS 又抖了一下、判定成"又到达了一次"——同一个人还站在同一个地方，
    # 这不该再叫醒一次。debounce 就是拦这种事，不是拦"真的离开又回来"。
    ok2, reason2 = wake.should_wake(
        "arrival", enabled_sources=("arrival",), last_wake_ts=1000.0, now=1030.0, debounce_sec=60.0
    )
    print(f"     30 秒后同一次到达(GPS 抖动) -> 戳醒={ok2}  原因={reason2}  （被防抖拦下）")

    print("\n④ 连续偏离：只在「从正常跨入异常」那一刻叫一次，不许反复叫")
    # 8/20-8/22 连续三晚睡得不好（跟①里的"是否有观测"是同一套四态）。
    bad_three_nights = [
        {"date": "2026-08-19", "state": "observed", "abnormal": False},
        {"date": "2026-08-20", "state": "observed", "abnormal": True},
        {"date": "2026-08-21", "state": "observed", "abnormal": True},
        {"date": "2026-08-22", "state": "observed", "abnormal": True},
    ]
    print(f"     当前连续偏离天数 -> {streaks.current_streak(bad_three_nights)}")
    trigger = streaks.should_trigger(bad_three_nights, min_days=3, already_firing=False)
    print(f"     达到 3 天阈值 -> fire={trigger.fire}  reason={trigger.reason}")

    # 第四晚还是没睡好——已经叫过了，这段异常还没结束，不该再叫第二次。
    still_bad = bad_three_nights + [{"date": "2026-08-23", "state": "observed", "abnormal": True}]
    trigger2 = streaks.should_trigger(still_bad, min_days=3, already_firing=trigger.next_firing)
    print(f"     第 4 晚仍不好(已经叫过) -> fire={trigger2.fire}  reason={trigger2.reason}")

    # 第五晚终于睡好了——异常段结束，「锁」自动打开，为下一段新的异常让路。
    recovered = still_bad + [{"date": "2026-08-24", "state": "observed", "abnormal": False}]
    trigger3 = streaks.should_trigger(recovered, min_days=3, already_firing=trigger2.next_firing)
    print(f"     第 5 晚睡好了 -> fire={trigger3.fire}  reason={trigger3.reason}  "
          "（锁已打开，下次再连续 3 天会重新叫）")

    print("\n⑤ 此刻的状况压成一份摘要 —— 只有 True/False，没有一个具体数字")
    signals = {
        "location": {"place_label": "公司", "country": "CN", "locality": "上海"},
        # 注意：这里的 180 分钟不会泄露到 glance 里，glance 只回答
        # 「有没有睡眠数据」和「跟上次比是不是显著变了」。
        "sleep": {"asleep_minutes": 180.0, "core_minutes": 100.0, "deep_minutes": 15.0, "rem_minutes": 20.0},
    }
    glance = build_perception_glance(
        signals,
        notable_changes=[{"signal": "location_signal"}, {"signal": "health_sleep"}],
    )
    print(f"     {glance}")
    print("     （模型读到这份摘要，只知道「地点变了」「健康数据有显著变化」——")
    print("      想知道具体睡了多久、变化多大，得自己去查工具，而不是被喂进来。）")

    print("\n⑥ 同一件事，两种指标要用两种读法")
    # 睡眠是「波动型」——有个平时水平，偏离才是信号。
    sleep_rows = [
        {"date": "2026-08-19", "doc": {"asleep_minutes": 420.0}},
        {"date": "2026-08-20", "doc": {"asleep_minutes": 400.0}},
        {"date": "2026-08-21", "doc": {"asleep_minutes": 190.0}},
        {"date": "2026-08-22", "doc": {"asleep_minutes": 175.0}},
        {"date": "2026-08-23", "doc": {"asleep_minutes": 180.0}},
    ]
    print(f"     health_sleep 的模型 -> {trend_models.model_for('health_sleep')}")
    sleep_trend = history.read_trend(sleep_rows, "health_sleep", "asleep_minutes")
    print(f"     平时(中位数) {sleep_trend['baseline']['median']} 分钟，"
          f"今天 {sleep_trend['current']} 分钟，"
          f"偏离 {sleep_trend['delta']}（{sleep_trend['direction']}）")

    # 体重是「漂移型」——没有"平时水平"这回事，一年从 80kg 掉到 60kg，
    # 拿"跟最近几次的中位数比"去读，会得出荒谬的结论：见下面的对照。
    weight_rows = [
        {"date": "2025-08-23", "doc": {"weight_kg": 80.0}},
        {"date": "2025-12-23", "doc": {"weight_kg": 74.0}},
        {"date": "2026-04-23", "doc": {"weight_kg": 68.0}},
        {"date": "2026-08-23", "doc": {"weight_kg": 60.0}},
    ]
    print(f"\n     health_body 的模型 -> {trend_models.model_for('health_body')}")
    wrong_way = history.read_trend(weight_rows, "health_body", "weight_kg")
    print(f"     ✗ 用波动型的读法(跟中位数比) -> 「比平时轻了 {abs(wrong_way['delta'])} 公斤」"
          "  —— 这个人根本没有一个叫「平时」的体重")
    right_way = trend_models.read_drift(weight_rows, "health_body", "weight_kg")
    print(f"     ✓ 用漂移型的读法(看方向和速率) -> 一年内共 {right_way['total_delta']} 公斤，"
          f"平均每月 {right_way['per_month']} 公斤，还在加速={right_way['accelerating']}")

    print("\n⑦ 最后：这份摘要该怎么讲给模型听（说明书是库给的，话不是库替它说的）")
    print(f"     {V1_GLANCE_HOWTO}")
    print()
    print(f"     {V1_BOARD_HOWTO}")

    print("\n跑通了：没有网络、没有 API key、没有数据库——库只回答判断，")
    print("戳醒之后 agent 继续睡 / 只看一眼 / 开口说话，这三选一从来不是它管的事。")


if __name__ == "__main__":
    main()
