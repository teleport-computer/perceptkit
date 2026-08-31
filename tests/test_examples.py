"""examples/quickstart.py 是活文档——它必须一直能跑，不能悄悄烂掉。

不 import 它的代码去断言细节（那是上面各个 test_*.py 的事），只用子进程
跑一遍整个脚本，钉住两件事：
  · 它能在当前解释器下干净跑完（exit code 0），不炸
  · 它确实走完了全部七步、打印了结尾那句话——不是半路 return 或空转

用 ``sys.executable`` 而不是裸 ``python3``：pytest 本身就是在装好 perceptkit
（editable install）的解释器下跑的，这样测试环境和 README 里教用户跑的方式
（``uv run python3 examples/quickstart.py``）用的是同一个解释器，不会出现
"测试绿、用户手上跑不动"的分裂。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

QUICKSTART = pathlib.Path(__file__).resolve().parent.parent / "examples" / "quickstart.py"


def test_quickstart_runs_cleanly_end_to_end():
    result = subprocess.run(
        [sys.executable, str(QUICKSTART)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"quickstart.py 退出码非 0：\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "跑通了" in result.stdout
    # 七个步骤都得真的跑到，不是提前 return 或者只跑了一半。
    for marker in ("①", "②", "③", "④", "⑤", "⑥", "⑦"):
        assert marker in result.stdout, f"quickstart.py 没有打印步骤 {marker}"


# ---------------------------------------------------------------------------
# examples/end_to_end.py —— 接入模板,同样是活文档
# ---------------------------------------------------------------------------

END_TO_END = pathlib.Path(__file__).resolve().parent.parent / "examples" / "end_to_end.py"


def test_the_end_to_end_example_still_runs():
    """产品规范点名说过"只串联纯函数的 quickstart 不能代表插件可接入"。
    这个例子走的是真链路,它烂掉就等于接入模板烂掉。"""
    proc = subprocess.run([sys.executable, str(END_TO_END)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # 钉住几个关键行为,不是钉措辞
    assert "戳醒 agent" in out                     # 事件真的投出去了
    assert "duplicate" in out                      # 重传被挡住了
    assert "stale" in out                          # 过期值没冒充当前
    assert "全部通过" in out                        # 一致性套件跑了
