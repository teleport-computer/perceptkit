"""文档里那些**声称一个数字**的地方，必须和代码对得上。

不是洁癖。2026-09-02 的外部审查逐条列出来的漂移里，最贵的一条是
manifest 开头写着"五个信号"而实际有 23 个 —— 下一个接入的人（和下一个
工程 AI）会照着"五个"去设计。文档说错话和代码说错话一样贵，区别只是
没人给文档写测试。

**只测"能自动核对的断言"**，不测措辞。写死的测试条数那种每次提交都变的
数字，正确做法是从文档里删掉，不是给它加个测试。
"""
from __future__ import annotations

import re
from pathlib import Path

from perceptkit.manifest import MINIMAL_SIGNALS

ROOT = Path(__file__).resolve().parents[1]


def test_the_manifest_says_how_many_signals_it_actually_has():
    text = (ROOT / "src/perceptkit/manifest/minimal.py").read_text()
    claimed = re.search(r"默认 manifest —— (\d+) 个信号", text)
    assert claimed, "manifest 开头那句声明被改没了 —— 它是给接入方的第一印象"
    assert int(claimed.group(1)) == len(MINIMAL_SIGNALS)


def test_the_kit_docstring_says_how_many_signals_the_default_has():
    text = (ROOT / "src/perceptkit/kit.py").read_text()
    claimed = re.search(r"``MINIMAL_SIGNALS``（(\d+) 个", text)
    assert claimed, "kit 上那条默认信号集的注释被改没了"
    assert int(claimed.group(1)) == len(MINIMAL_SIGNALS)


def test_the_changelog_top_entry_matches_the_packaged_version():
    """打了 tag 还写着"未发布"，是审查里被点名的那一条。"""
    # 不用 tomllib —— 它是 3.11 才进标准库的，而这个包声明支持 3.10。
    # （这一行本身就是新加的 3.10 矩阵在第一次跑的时候抓出来的。）
    text = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
    head = (ROOT / "CHANGELOG.md").read_text().split("\n## ", 1)[1].split("\n", 1)[0]
    assert head.startswith(version), (
        f"CHANGELOG 顶上是 {head!r}，但打出去的包是 {version} —— 两个必须一致"
    )
    assert "未发布" not in head or version.endswith("dev"), (
        f"{version} 已经是要发的版本了，CHANGELOG 还写着未发布"
    )


def test_the_package_says_where_it_comes_from():
    """PyPI 页面上要能看出这个包属于谁、仓库在哪。

    2026-09-03 真的有人据此判断成"私有仓、还没发布"——而它一直是公开的、
    八个版本都在 PyPI 上。接入方（或工程 AI）从包页面得不到源头就只能猜，
    猜出来的结论会被当成事实往下传。
    """
    text = (ROOT / "pyproject.toml").read_text()
    for key in ("Homepage", "Source", "Changelog", "Issues"):
        assert f"{key} = " in text, f"project.urls 缺 {key}"
    assert "authors = " in text


def test_the_package_still_declares_zero_dependencies():
    """`[project.urls]` 这类表头插错位置会把后面的键吸进去。

    写 project.urls 时真踩到：表头插在 `dependencies = []` 之前，
    零依赖声明就静默变成了 urls 的一个键 —— TOML 合法、构建不报错，
    只是这个包从此不再声明"零依赖"，而 test_purity.py 那条 AST 扫描
    盯的是源码不是元信息，它抓不到。
    """
    import re
    text = (ROOT / "pyproject.toml").read_text()
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    assert re.search(r"^dependencies = \[\]", project, re.M), (
        "dependencies 不在 [project] 里了 —— 多半被某个表头吸走了")
