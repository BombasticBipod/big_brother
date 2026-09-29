"""Code under build runs in a bubblewrap sandbox that sees only the OS, Python and its work dir."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from big_brother import sandbox
from big_brother.sandbox import SandboxUnavailable, sandboxed


def run_in(work: Path, code: str) -> subprocess.CompletedProcess:
    cmd = sandboxed([sys.executable, "-c", code], writable=work, cwd=work)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(work)})


@pytest.fixture
def work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    w.mkdir()
    return w


def test_code_runs_and_writes_its_work_dir(work):
    r = run_in(work, "open('out.txt', 'w').write('hi')")
    assert r.returncode == 0, r.stderr
    assert (work / "out.txt").read_text() == "hi"


def test_big_brother_and_pytest_import_inside(work):
    r = run_in(work, "import pytest, big_brother.redcheck_plugin")
    assert r.returncode == 0, r.stderr


def test_real_files_outside_the_work_dir_are_not_visible(work, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    home_file = Path.home() / ".claude" / "CLAUDE.md"
    assert home_file.exists()
    r = run_in(work, f"import os; print(os.path.exists({str(secret)!r}), "
                     f"os.path.exists({str(home_file)!r}))")
    assert r.stdout.split() == ["False", "False"], r.stderr


def test_absolute_writes_outside_never_reach_the_real_file(work, tmp_path):
    # /tmp inside is a throwaway tmpfs, so the write may "succeed" there; the real file must not.
    target = tmp_path / "planted.txt"
    run_in(work, f"open({str(target)!r}, 'w').write('x')")
    assert not target.exists()


def test_writes_into_a_real_directory_outside_fail(work):
    root = Path(sandbox.__file__).resolve().parents[1]
    r = run_in(work, f"open({str(root / 'planted.txt')!r}, 'w').write('x')")
    assert r.returncode != 0
    assert not (root / "planted.txt").exists()


def test_the_repository_holding_big_brother_is_not_visible(work):
    root = Path(sandbox.__file__).resolve().parents[1]
    r = run_in(work, f"import os; print(os.path.exists({str(root / '.git')!r}), "
                     f"os.path.exists({str(root / 'tests')!r}))")
    assert r.stdout.split() == ["False", "False"], r.stderr


def test_there_is_no_network(work):
    r = run_in(work, "import socket\n"
                     "try:\n"
                     "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
                     "    print('connected')\n"
                     "except OSError:\n"
                     "    print('blocked')\n")
    assert r.stdout.strip() == "blocked", r.stderr


def test_other_processes_are_invisible(work):
    r = run_in(work, "import os; print(os.getppid() == 0 or len([p for p in os.listdir('/proc') "
                     "if p.isdigit()]) < 5)")
    assert r.stdout.strip() == "True", r.stderr


def test_missing_bubblewrap_is_an_error_not_a_silent_fallback(work, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(SandboxUnavailable):
        sandboxed([sys.executable, "-c", "pass"], writable=work, cwd=work)


def test_a_base_python_outside_usr_is_mounted_read_only(work, tmp_path, monkeypatch):
    base = tmp_path / "uv-python"
    base.mkdir()
    monkeypatch.setattr(sandbox.sys, "base_prefix", str(base))
    args = sandboxed(["true"], writable=work, cwd=work)
    assert any(args[i:i + 3] == ["--ro-bind", str(base), str(base)] for i in range(len(args)))


def test_a_base_python_under_usr_needs_no_extra_mount(work, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "base_prefix", "/usr")
    assert sandboxed(["true"], writable=work, cwd=work).count("/usr") == 2   # the /usr bind only
