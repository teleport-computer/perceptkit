"""内核的纯度：只依赖标准库,不依赖任何第三方包。

结构判据(AST 里有没有这个 import),不判语义、不判风格：误伤为零。
照抄 memgarden 的 ``tests/test_purity.py`` 的做法——唯一的差别是这个包
**没有任何允许的第三方依赖**（memgarden 允许同源发布的 agent-protocol-core，
这个包没有等价的同源伙伴包，允许名单是空集）。

这条测试是本包的核心承诺：任何宿主都能直接嵌入这个包，不需要额外装依赖、
不需要跟宿主自己的依赖版本打架。一旦这条破了，"零依赖、可被任意宿主嵌入"
这句话就是假的。
"""
from __future__ import annotations

import ast
import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "perceptkit"

#: 唯一允许的非标准库依赖——空集。这是本包和 memgarden 最大的区别：
#: memgarden 允许同源的 agent-protocol-core，这个包没有等价的同源伙伴包。
ALLOWED_THIRD_PARTY: frozenset[str] = frozenset()

#: 内核不许碰的东西。纯函数意味着：给同样的输入，永远得到同样的输出。
FORBIDDEN = frozenset({
    "socket", "http", "urllib", "requests", "httpx", "asyncio",
    "sqlite3", "psycopg", "psycopg2", "sqlalchemy",
    "subprocess", "threading", "multiprocessing",
})


def _modules(path: pathlib.Path):
    for p in sorted(path.rglob("*.py")):
        yield p


def _top_level_imports(src: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
    return out


def test_source_tree_is_not_empty():
    """守卫本身要有牙 —— 路径写错就变成空扫，永远绿。"""
    assert len(list(_modules(SRC))) > 10


def test_no_third_party_imports():
    """本包只依赖标准库。任何 import 都必须是 stdlib 或包自身。"""
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for f in _modules(SRC):
        rel = str(f.relative_to(SRC))
        for mod in _top_level_imports(f.read_text(encoding="utf-8")):
            if mod in stdlib or mod == "perceptkit" or mod in ALLOWED_THIRD_PARTY:
                continue
            offenders.append(f"{rel}: {mod}")
    assert not offenders, (
        "perceptkit import 了标准库之外的东西——这个包唯一的承诺就是"
        "「任何宿主都能直接嵌入，不用装额外依赖」，破了这条就是假的：\n"
        + "\n".join(offenders)
    )


def test_no_io_in_the_judgment_modules():
    """判断内核不许碰网络/数据库/进程/线程——纯函数意味着可测、可预测。"""
    offenders = []
    for f in _modules(SRC):
        rel = str(f.relative_to(SRC))
        hit = _top_level_imports(f.read_text(encoding="utf-8")) & FORBIDDEN
        if hit:
            offenders.append(f"{rel}: {sorted(hit)}")
    assert not offenders, "内核里出现了 I/O：\n" + "\n".join(offenders)
