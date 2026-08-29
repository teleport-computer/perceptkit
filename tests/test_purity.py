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


# ---------------------------------------------------------------------------
# algorithms/ 这一层要名副其实
# ---------------------------------------------------------------------------

ALGORITHMS = SRC / "algorithms"   # SRC 已经指到 src/perceptkit

#: algorithms/ 允许依赖的包内模块。**存储、端口、管线、装配一个都不许** ——
#: 一旦依赖了它们，这一层就不再是"给定输入算出结果"，
#: 而分出这一层的全部意义就是它可以被放心大改：没有副作用、没有顺序依赖，
#: 改错了测试当场红，而不是在某个宿主的生产环境里变成一条静默错掉的记录。
ALGORITHM_MAY_IMPORT = {"catalog", "observation", "manifest", "contracts"}


def test_the_algorithms_layer_does_not_reach_into_the_rest_of_the_package():
    """产品规范 §18：不能再把 contract、算法、存储、宿主 runtime 接线混成一层。"""
    import re
    offenders = []
    for f in ALGORITHMS.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for m in re.findall(r"^from \.\.?([a-z_]+)", text, flags=re.M):
            if m and m not in ALGORITHM_MAY_IMPORT:
                offenders.append(f"{f.name}: 依赖了 {m}")
    assert not offenders, (
        "algorithms/ 依赖了这一层不该知道的东西：\n" + "\n".join(offenders))


def test_nothing_that_stayed_at_the_top_level_is_actually_an_algorithm():
    """留在顶层的四个是声明表和待定项，不是算法。

    写成测试是为了防"顺手也搬进去" —— 把声明表塞进 algorithms/，
    这个词就不再有意义，下一个人也就不知道该往哪放东西了。
    """
    top = {p.stem for p in SRC.glob("*.py")}
    assert top == {"__init__", "kit", "catalog", "fields", "retention", "prompts"}


def test_every_algorithm_module_is_reachable_from_the_package():
    """搬完之后漏导出一个，宿主就 import 不到它，而 import 错误比静默漂移好，
    但最好一个都不漏。"""
    import perceptkit.algorithms as alg
    on_disk = {p.stem for p in ALGORITHMS.glob("*.py")} - {"__init__"}
    assert set(alg.__all__) == on_disk
