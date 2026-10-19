from pathlib import Path

import tree_diff


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_compare_finds_only_left_only_right_and_differ(tmp_path):
    left, right = tmp_path / "server", tmp_path / "repo"
    _write(left, "src/a.py", b"same\n")
    _write(right, "src/a.py", b"same\n")
    _write(left, "src/splithub.py", b"x\n")
    _write(right, "docs/readme.md", b"y\n")
    _write(left, "src/b.py", b"server\n")
    _write(right, "src/b.py", b"repo\n")

    diff = tree_diff.compare(left, right)

    assert diff.only_left == ["src/splithub.py"]
    assert diff.only_right == ["docs/readme.md"]
    assert diff.differ == ["src/b.py"]


def test_crlf_and_lf_are_equal(tmp_path):
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left, "a.py", b"one\ntwo\n")
    _write(right, "a.py", b"one\r\ntwo\r\n")

    assert tree_diff.compare(left, right).differ == []


def test_ignored_dirs_and_pyc_are_skipped(tmp_path):
    left, right = tmp_path / "l", tmp_path / "r"
    right.mkdir()
    _write(left, "__pycache__/a.cpython-313.pyc", b"x")
    _write(left, ".venv/lib/x.py", b"x")
    _write(left, "state/content_factory.db", b"x")
    _write(left, "src/m.pyc", b"x")

    diff = tree_diff.compare(left, right)

    assert diff.only_left == []


def test_markdown_has_three_sections(tmp_path):
    diff = tree_diff.TreeDiff(only_left=["a"], only_right=["b"], differ=["c"])

    text = tree_diff.render_markdown(diff, "server", "repo")

    assert "## Только в server (1)" in text
    assert "## Только в repo (1)" in text
    assert "## Различаются (1)" in text
    assert "- `c`" in text


def test_main_list_mode_prints_paths(tmp_path, capsys):
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left, "x.py", b"1\n")
    right.mkdir()

    code = tree_diff.main([str(left), str(right), "--list", "only-left"])

    assert code == 0
    assert capsys.readouterr().out.strip() == "x.py"


def test_main_missing_dir_returns_2(tmp_path, capsys):
    code = tree_diff.main([str(tmp_path / "nope"), str(tmp_path)])

    assert code == 2
    assert "не найден" in capsys.readouterr().err
