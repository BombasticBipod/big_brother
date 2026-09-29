"""red_check runs new tests against generated stubs and says whether they fail for the right reason."""
import subprocess
from pathlib import Path

import pytest

from big_brother.red_check import red_check
from big_brother.suite_lock import SuiteLock

CALC_PYI = (
    "LIMIT = 100\n"
    "def add(a: int, b: int) -> int: ...\n"
    "def div(a: int, b: int) -> float: ...\n"
)
REAL_CALC = "LIMIT = 100\ndef add(a, b):\n    return a + b  # REAL-SOURCE\ndef div(a, b):\n    return a / b\n"


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A target project whose src/ fully implements the interface."""
    root = tmp_path / "proj"
    write(root, {
        "interface/calc.pyi": CALC_PYI,
        "src/calc.py": REAL_CALC,
        "src/secret.py": "def hidden():\n    return 'REAL-SOURCE'\n",
        "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
        "conftest.py": "import sys\nsys.path.insert(0, 'src')\n",
    })
    return root


def check(project: Path, files: dict[str, str], run: list[str] | None = None):
    write(project, files)
    return red_check(project, run or list(files))


# red

def test_test_for_missing_behavior_is_red(project):
    r = check(project, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    assert r.status == "red"
    assert [(t.nodeid, t.verdict, t.exc_type) for t in r.tests] == [
        ("tests/test_add.py::test_add", "red", "NotImplementedError")]
    assert r.summary == "red: 1 test fails correctly"


def test_assertion_failure_is_red(project):
    r = check(project, {"tests/test_limit.py": "from calc import LIMIT\ndef test_limit():\n    assert LIMIT == 200\n"})
    assert r.status == "red"
    assert r.tests[0].exc_type == "AssertionError"


def test_not_implemented_in_fixture_setup_is_red(project):
    r = check(project, {
        "tests/conftest.py": "import pytest\nfrom calc import add\n@pytest.fixture\ndef five():\n    return add(2, 3)\n",
        "tests/test_five.py": "def test_five(five):\n    assert five == 5\n",
    }, run=["tests/test_five.py"])
    assert r.status == "red"
    assert r.tests[0].when == "setup"


def test_expected_exception_test_is_red_against_stubs(project):
    r = check(project, {"tests/test_div.py": (
        "import pytest\nfrom calc import div\n"
        "def test_div_zero():\n    with pytest.raises(ZeroDivisionError):\n        div(1, 0)\n")})
    assert r.status == "red"


# passes on stubs

def test_test_that_passes_on_stubs_is_rejected(project):
    r = check(project, {"tests/test_x.py": "def test_nothing():\n    assert True\n"})
    assert r.status == "passes_on_stubs"
    assert r.summary == "passes_on_stubs: tests/test_x.py::test_nothing passed against stubs"


def test_mixed_reports_only_the_passing_test(project):
    r = check(project, {"tests/test_m.py": (
        "from calc import add\n"
        "def test_red():\n    assert add(1, 1) == 2\n"
        "def test_weak():\n    assert callable(add)\n")})
    assert r.status == "passes_on_stubs"
    assert "test_weak" in r.summary
    assert "test_red" not in r.summary


# broken

@pytest.mark.parametrize("body, needle", [
    ("def test_x(:\n    pass\n", "SyntaxError"),
    ("from calc import mul\ndef test_x():\n    assert mul(2, 3) == 6\n", "ImportError"),
    ("from calc import add\ndef test_x():\n    assert add(1) == 1\n", "TypeError"),
    ("import pytest\n@pytest.mark.skip\ndef test_x():\n    pass\n", "Skipped"),
    ("def helper():\n    pass\n", "no tests"),
])
def test_broken_tests_say_why(project, body, needle):
    r = check(project, {"tests/test_b.py": body})
    assert r.status == "broken"
    assert needle in r.summary


def test_bad_interface_is_broken(project):
    write(project, {"interface/bad.pyi": "def oops(:\n"})
    r = check(project, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    assert r.status == "broken"
    assert "bad.pyi" in r.summary


def test_paths_outside_tests_are_refused(project):
    with pytest.raises(ValueError):
        red_check(project, ["src/calc.py"])
    with pytest.raises(ValueError):
        red_check(project, ["tests/../src/calc.py"])


# real source never leaks in

def test_real_src_is_never_imported(project, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(project / "src"))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:big_brother_missing")
    r = check(project, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    assert r.status == "red"


def test_module_outside_interface_is_broken_and_source_not_shown(project):
    r = check(project, {"tests/test_s.py": "from secret import hidden\ndef test_s():\n    assert hidden() == 'x'\n"})
    assert r.status == "broken"
    assert "ModuleNotFoundError" in r.summary
    assert "REAL-SOURCE" not in repr(r)


# scope, budget and side effects

def test_only_named_paths_run_but_shared_conftest_is_available(project):
    r = check(project, {
        "tests/test_old.py": "def test_old():\n    assert True\n",
        "tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
    }, run=["tests/test_add.py"])
    assert r.status == "red"
    assert [t.nodeid for t in r.tests] == ["tests/test_add.py::test_add"]


def test_summary_stays_under_budget(project):
    body = "".join(f"def test_{i}():\n    assert True, '{'x' * 300}'\n" for i in range(40))
    r = check(project, {"tests/test_many.py": body})
    assert r.status == "passes_on_stubs"
    assert len(r.summary) <= 500
    assert "more" in r.summary


def test_works_on_a_locked_suite_and_leaves_repo_untouched(project):
    write(project, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A"],
                   cwd=project, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "x"],
                   cwd=project, check=True)
    lock = SuiteLock(project)
    lock.lock()
    try:
        r = red_check(project, ["tests/test_add.py"])
        assert r.status == "red"
        assert not (project / "tests" / "__pycache__").exists()
        assert lock.changes() == []
    finally:
        with lock.accept("unlock for cleanup"):
            pass
        for p in [project / "tests", *(project / "tests").rglob("*")]:
            p.chmod(0o755)


def test_missing_test_file_is_broken_with_pytest_reason(project):
    write(project, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    r = red_check(project, ["tests/test_missing.py"])
    assert r.status == "broken"
    assert "not found" in r.summary


# staged files and symlinks

def test_overlay_adds_staged_tests_and_interface(project, tmp_path):
    staged = tmp_path / "staged"
    write(staged, {
        "interface/shapes.pyi": "def area(w: int, h: int) -> int: ...\n",
        "tests/test_area.py": "from shapes import area\ndef test_area():\n    assert area(2, 3) == 6\n",
    })
    r = red_check(project, ["tests/test_area.py"], overlay=staged)
    assert r.status == "red", r.summary
    assert not (project / "tests").exists() and not (project / "interface" / "shapes.pyi").exists()


def test_overlay_replaces_a_committed_test(project, tmp_path):
    write(project, {"tests/test_add.py": "def test_add():\n    pass\n"})
    staged = tmp_path / "staged"
    write(staged, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(1, 1) == 2\n"})
    assert red_check(project, ["tests/test_add.py"], overlay=staged).status == "red"
    assert red_check(project, ["tests/test_add.py"]).status == "passes_on_stubs"


def test_a_symlink_in_tests_is_broken_and_never_followed(project):
    write(project, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    (project / "tests" / "leak.py").symlink_to(project / "src" / "secret.py")
    r = red_check(project, ["tests/test_add.py"])
    assert r.status == "broken"
    assert "symlink" in r.summary and "REAL-SOURCE" not in r.summary


def test_a_symlinked_tests_dir_is_broken(project, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere, {"test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    (project / "tests").symlink_to(elsewhere)
    r = red_check(project, ["tests/test_add.py"])
    assert r.status == "broken" and "symlink" in r.summary


def test_a_symlink_in_the_overlay_is_broken(project, tmp_path):
    staged = tmp_path / "staged"
    write(staged, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    (staged / "tests" / "leak.py").symlink_to(project / "src" / "secret.py")
    r = red_check(project, ["tests/test_add.py"], overlay=staged)
    assert r.status == "broken" and "symlink" in r.summary


# sandbox

def test_proposed_tests_cannot_read_src_by_absolute_path(project):
    secret = project / "src" / "secret.py"
    r = check(project, {"tests/test_peek.py": (
        "def test_peek():\n"
        "    try:\n"
        f"        open({str(secret)!r}).read()\n"
        "    except OSError:\n"
        "        raise NotImplementedError\n"
        "    raise AssertionError('read src')\n")})
    assert [(t.verdict, t.exc_type) for t in r.tests] == [("red", "NotImplementedError")]


def test_red_check_without_bubblewrap_refuses_to_run(project, monkeypatch):
    from big_brother import sandbox
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        check(project, {"tests/test_add.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
