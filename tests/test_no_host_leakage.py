"""同步守卫：这个包不该带着宿主的内部痕迹发出去。

**这是照抄 memgarden 的 ``tests/test_no_host_leakage.py`` 改的**——同一个团队
从同一个宿主(io)先后抽出过两个内核，第一次(memgarden)已经踩过"同步覆盖式
迁移会把清理过一次的宿主私有内容再带回来"这个坑。这个包是第二次抽取，同样的
风险原样存在，所以同样需要一条测试盯着。

选词标准：**只挑结构性的、不含糊的标识符**——宿主的内部模块名、内部工具名、
内部数据库表名、内部运行时 lane 名、生态里的第三方插件名、真实用户 id 的
形状、凭据名。**刻意不禁裸的 "io" 这个词**——它只有两个字母，是 Python 标准库
模块名(``import io``)的合法用法，也是很多英文缩写/变量名的子串，禁了假阳性
远大于真阳性；产品名这一类改用更具体的旗标(比如生态里的插件名)来兜底，
或者在真正说明性的宿主提及处交给人工判断，而不是这条自动测试。

结构判据优先：能从 import/标识符判断的，就不用扫自由文本；扫文本的几条
（内部模块名、内部工具名、DB 表名、凭据名）挑的都是不常见到几乎不会误判
的具体词，不是会在正常英文/代码里出现的常见词。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 宿主私有、不该出现在通用库里的东西，以及选它的理由。
BANNED: dict[str, str] = {
    # 迁移前的宿主内部包名——留着就是没洗干净的直接证据。
    r"\bperception_kernel\b": "宿主迁移前的内部包名",
    # 宿主的内部 CLI 工具名 / VPS 自托管常驻进程模块名。
    r"\bio_cli\b": "宿主内部工具名",
    r"\bchat_resident_consumer\b": "宿主内部模块名(自托管 consumer)",
    # 宿主运行时内部模块名——这些名字只有宿主自己的代码库里才有意义。
    r"\bdiffer_v2\b": "宿主内部模块名",
    r"\bsignal_state_v2\b": "宿主内部模块名",
    r"\beffect_outbox\b": "宿主内部模块名",
    r"\btool_executor_v2\b": "宿主内部模块名",
    # 宿主的运行时 lane 名——这个包要同时兼容多条宿主 runtime，写死其中一条
    # 的名字就等于假设了宿主的内部架构。
    r"\bmodel_api_runtime\b": "宿主内部运行时 lane 名",
    # 宿主生态里的第三方插件名——只有知道这个宿主生态的人才认得。
    r"\bOpenClaw\b": "宿主生态里的插件名",
    # 宿主仓库的目录布局——这个包不该假设自己活在宿主仓库的哪个子目录下。
    r"\bbackend/[A-Za-z_./]+\.py\b": "宿主内部文件路径",
    r"\btools/chat_resident_consumer\.py\b": "宿主内部文件路径",
    # 宿主数据库里的实际表名——这个包不碰存储，不该知道表叫什么。
    r"\bperception_daily\b": "宿主数据库表名",
    # 真实用户 id 的形状 + 凭据名，和 memgarden 那条一致。
    r"\busr_[0-9a-f]{8,}\b": "真实用户 id",
    r"\bADMIN_KEY\b": "凭据名",
}


def _sources():
    # 只扫会真的发出去的东西：包源码 + 面向读者的文档 + 打包元数据。
    # 刻意不扫 tests/ ——这个文件自己就活在 tests/ 里，它的 BANNED 模式
    # 字面量本身就含有这些词，扫自己会自证失败（误报，不是真的泄漏）。
    for p in (ROOT / "src").rglob("*.py"):
        yield p
    for name in ("README.md", "pyproject.toml"):
        f = ROOT / name
        if f.exists():
            yield f


def test_no_host_private_content_in_the_package():
    hits = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for pattern, what in BANNED.items():
            for m in re.finditer(pattern, text):
                line = text[: m.start()].count("\n") + 1
                hits.append(f"{path.relative_to(ROOT)}:{line} {what} -> {m.group(0)}")
    assert not hits, "宿主私有内容漏进包里了：\n" + "\n".join(hits)
