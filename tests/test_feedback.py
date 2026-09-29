"""Feedback runs coverage and mutation testing and reports gaps by interface name only."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from big_brother.builder import DirtySrc
from big_brother.feedback import (SUMMARY_BUDGET, FeedbackResult, feedback, feedback_log_path,
                                  interface_names, main, mutant_name, run_killable)
from big_brother.runlock import BuildBusy, run_lock
from big_brother.suite_lock import SuiteLock, TestsTampered

INTERFACE = {
    "calc.pyi": '''def add(a: int, b: int) -> int: ...
def sign(x: int) -> int: ...

class Counter:
    def bump(self, n: int) -> int: ...
''',
    "pkg/__init__.pyi": "",
    "pkg/shapes.pyi": "def area(w: int, h: int) -> int: ...\n",
}
SRC = {
    "calc.py": '''def _secret_helper_marker(x):
    return x * 2


def add(a, b):
    return a + b


def sign(x):
    if x > 0:
        return 1
    return 0


class Counter:
    def __init__(self):
        self.n = 0

    def bump(self, n):
        self.n += n
        return self.n
''',
    "pkg/__init__.py": "",
    "pkg/shapes.py": "def area(w, h):\n    return w * h\n",
}
WEAK_TESTS = '''from calc import Counter, add, sign
from pkg.shapes import area


def test_add():
    assert add(2, 3) == 5


def test_sign():
    assert sign(5) == 1


def test_bump():
    Counter().bump(1)


def test_area():
    assert area(2, 3) == 6
'''
STRONG_TESTS = '''from calc import add


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
'''


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def make_target(root: Path, interface: dict, src: dict, tests: str) -> Path:
    for folder, files in (("interface", interface), ("src", src)):
        for rel, text in files.items():
            (root / folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / folder / rel).write_text(text)
    (root / "tests").mkdir()
    (root / "tests" / "test_it.py").write_text(tests)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "target")
    SuiteLock(root).lock()
    return root


@pytest.fixture(scope="module")
def weak(tmp_path_factory) -> tuple[Path, FeedbackResult, list[str]]:
    repo = make_target(tmp_path_factory.mktemp("weak"), INTERFACE, SRC, WEAK_TESTS)
    lines: list[str] = []
    return repo, feedback(repo, progress=lines.append), lines


@pytest.fixture(scope="module")
def strong(tmp_path_factory) -> FeedbackResult:
    repo = make_target(tmp_path_factory.mktemp("strong"), {"calc.pyi": "def add(a, b): ...\n"},
                       {"calc.py": "def add(a, b):\n    return a + b\n"}, STRONG_TESTS)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PYTHONPATH", str(elsewhere))    # must not shadow the mutated copy
        return feedback(repo, progress=lambda line: None)


# names

def test_interface_names_include_functions_and_methods(tmp_path):
    for rel, text in INTERFACE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert interface_names(tmp_path) == {"calc": {"add", "sign", "Counter.bump"},
                                         "pkg": set(), "pkg.shapes": {"area"}}


def test_mutant_names_map_back_to_interface_names(weak):
    _, result, _ = weak
    assert result.status == "ok"
    names = set(result.survivors) | set(result.killed_in)
    assert {"calc.add", "calc.sign", "calc.Counter.bump", "pkg.shapes.area"} <= names
    assert "calc (other code)" in names


def test_unknown_mutant_key_goes_to_other_code():
    declared = {"calc": {"add"}}
    assert mutant_name("calc.x_add__mutmut_3", declared) == "calc.add"
    assert mutant_name("calc.x_helper__mutmut_1", declared) == "calc (other code)"
    assert mutant_name("calc.something_new__mutmut_1", declared) == "calc (other code)"


def test_undeclared_module_names_never_reach_the_writer(tmp_path):
    src = {"calc.py": "from _private_util_marker import double\n\n\ndef add(a, b):\n"
                      "    return double(a) // 2 + b\n",
           "_private_util_marker.py": "def double(x):\n    if x:\n        return x * 2\n"
                                     "    return 0\n"}
    assert mutant_name("_private_util_marker.x_double__mutmut_1", {"calc": {"add"}}) == \
        "other modules"
    lines: list[str] = []
    result = feedback(make_target(tmp_path, {"calc.pyi": "def add(a, b): ...\n"}, src,
                                  STRONG_TESTS), progress=lines.append)
    assert result.status == "ok"
    assert "other modules" in set(result.coverage) | set(result.survivors) | set(result.killed_in)
    for text in [result.summary, *lines, *result.coverage, *result.survivors, *result.killed_in]:
        assert "_private_util_marker" not in text


# results

def test_weak_suite_reports_coverage_and_mutation_gaps(weak):
    _, result, _ = weak
    assert result.coverage["calc.sign"] < 100
    assert result.coverage["calc.add"] == 100
    assert result.coverage["calc (other code)"] < 100
    assert result.survivors["calc.sign"] >= 1
    assert result.survivors["calc.Counter.bump"] >= 1
    assert result.survivors.get("pkg.shapes.area", 0) == 0
    assert result.survivors.get("calc.add", 0) == 0


def test_summary_names_gaps_within_budget_and_leaks_nothing(weak):
    _, result, lines = weak
    s = result.summary
    assert s.startswith("feedback: ") and len(s) <= SUMMARY_BUDGET
    assert "calc.sign" in s and "calc.Counter.bump" in s and "calc (other code)" in s
    assert "pkg.shapes.area" not in s and "calc.add" not in s
    for text in [s, *lines]:
        assert "_secret_helper_marker" not in text
        assert "return" not in text and "self.n" not in text


def test_strong_suite_kills_every_mutant_in_the_copy_it_tested(strong):
    assert strong.status == "ok"
    assert strong.killed > 0 and strong.survivors == {}
    assert strong.coverage == {"calc.add": 100.0}
    assert "no surviving mutants" in strong.summary


def test_target_is_left_untouched(weak):
    repo, _, _ = weak
    assert git(repo, "status", "--porcelain", "--ignored") == ""
    assert not (repo / "mutants").exists() and not (repo / ".coverage").exists()


def test_progress_reports_mutation_counts(weak):
    _, result, lines = weak
    assert any(line.startswith("coverage:") for line in lines)
    assert any(line.startswith("mutation: ") and "/" in line for line in lines)
    assert f"mutation: {result.total}/{result.total}" in lines


def test_log_keeps_detail_outside_the_summary(weak):
    repo, _, _ = weak
    log = feedback_log_path(repo).read_text()
    assert "survived" in log


def test_not_green_suite_skips_mutation(tmp_path):
    repo = make_target(tmp_path, {"calc.pyi": "def add(a, b): ...\n"},
                       {"calc.py": "def add(a, b):\n    return a - b\n"}, STRONG_TESTS)
    result = feedback(repo, progress=lambda line: None)
    assert result.status == "not_green"
    assert result.summary == "feedback: suite is not green; build first"
    assert not (repo / "mutants").exists()


def test_budget_holds_with_many_gaps(tmp_path):
    names = [f"function_with_a_long_name_{i:02}" for i in range(40)]
    interface = {"calc.pyi": "".join(f"def {n}(x): ...\n" for n in names)}
    src = {"calc.py": "".join(f"def {n}(x):\n    return x + {i}\n\n" for i, n in enumerate(names))}
    tests = "import calc\n\n\ndef test_one():\n    calc.function_with_a_long_name_00(1)\n"
    result = feedback(make_target(tmp_path, interface, src, tests), progress=lambda line: None)
    assert len(result.summary) <= SUMMARY_BUDGET and "more)" in result.summary


def test_coverage_timeout_is_a_failure_not_a_red_suite(tmp_path):
    tests = "import time\n\n\ndef test_slow():\n    time.sleep(30)\n"
    repo = make_target(tmp_path, {"calc.pyi": "def add(a, b): ...\n"},
                       {"calc.py": "def add(a, b):\n    return a + b\n"}, tests)
    result = feedback(repo, coverage_timeout=2, progress=lambda line: None)
    assert result.status == "failed"
    assert result.summary == "feedback: coverage run timed out after 2s"


def test_missing_coverage_report_is_a_failure(tmp_path, monkeypatch):
    import big_brother.feedback as fb
    real = fb.run_killable

    def no_json(cmd, *args, **kwargs):
        return (1, "", False) if "json" in cmd else real(cmd, *args, **kwargs)

    monkeypatch.setattr(fb, "run_killable", no_json)
    repo = make_target(tmp_path, {"calc.pyi": "def add(a, b): ...\n"},
                       {"calc.py": "def add(a, b):\n    return a + b\n"}, STRONG_TESTS)
    result = feedback(repo, progress=lambda line: None)
    assert result.status == "failed"
    assert result.summary == "feedback: coverage report failed (exit 1)"


# preconditions

def test_uncommitted_src_is_refused(tmp_path):
    repo = make_target(tmp_path, {"calc.pyi": "def add(a, b): ...\n"},
                       {"calc.py": "def add(a, b):\n    return a + b\n"}, STRONG_TESTS)
    (repo / "src" / "calc.py").write_text("# edited\n")
    with pytest.raises(DirtySrc):
        feedback(repo, progress=lambda line: None)


def test_interface_changed_outside_accept_is_refused(tmp_path):
    repo = make_target(tmp_path, {"calc.pyi": "def add(a, b): ...\n"},
                       {"calc.py": "def add(a, b):\n    return a + b\n"}, STRONG_TESTS)
    pyi = repo / "interface" / "calc.pyi"
    os.chmod(pyi, 0o644)
    pyi.write_text("def add(a, b): ...\ndef _secret_helper_marker(x): ...\n")
    with pytest.raises(TestsTampered):
        feedback(repo, progress=lambda line: None)
    assert "_secret_helper_marker" not in pyi.read_text()


def test_a_custom_interface_dir_is_guarded_too(tmp_path):
    repo = make_target(tmp_path, {"calc.pyi": "def add(a, b): ...\n"},
                       {"calc.py": "def add(a, b):\n    return a + b\n"}, STRONG_TESTS)
    (repo / "api").mkdir()
    (repo / "api" / "calc.pyi").write_text("def add(a, b): ...\n")
    git(repo, "add", "api")
    git(repo, "commit", "-qm", "api")
    SuiteLock(repo, interface_dir="api").lock()
    pyi = repo / "api" / "calc.pyi"
    os.chmod(pyi.parent, 0o755)
    os.chmod(pyi, 0o644)
    pyi.write_text("def add(a, b): ...\ndef _secret_helper_marker(x): ...\n")
    with pytest.raises(TestsTampered):
        feedback(repo, interface_dir="api", progress=lambda line: None)


def test_refused_while_a_build_runs(tmp_path):
    repo = make_target(tmp_path, {"calc.pyi": "def add(a, b): ...\n"},
                       {"calc.py": "def add(a, b):\n    return a + b\n"}, STRONG_TESTS)
    with run_lock(repo):
        with pytest.raises(BuildBusy):
            feedback(repo, progress=lambda line: None)


# process control

def test_timeout_kills_the_whole_process_tree(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = ("import subprocess, sys, time\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
              "time.sleep(60)\n")
    code, output, timed_out = run_killable([sys.executable, "-c", script], cwd=tmp_path,
                                           env=dict(os.environ), timeout=1)
    assert timed_out
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and alive(child):
        time.sleep(0.05)
    assert not alive(child)


def alive(pid: int) -> bool:
    """Running, not a zombie waiting for init to reap it. Reaping can land mid-read."""
    try:
        return "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]
    except OSError:
        return False


def test_run_killable_streams_output_to_a_callback(tmp_path):
    seen: list[str] = []
    code, output, timed_out = run_killable([sys.executable, "-c", "print('1/2'); print('2/2')"],
                                           cwd=tmp_path, env=dict(os.environ), timeout=10,
                                           on_output=seen.append)
    assert (code, timed_out) == (0, False)
    assert "2/2" in output and "2/2" in "".join(seen)


# command line

def test_main_prints_the_summary(weak, capsys):
    repo, result, _ = weak
    assert main([str(repo)]) == 0
    assert capsys.readouterr().out.strip().endswith(result.summary)
