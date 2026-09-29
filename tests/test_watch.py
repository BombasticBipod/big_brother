"""The window watcher follows a stream file and exits when the main program is gone."""
import io
import subprocess
import sys
import time

from big_brother.watch import follow, pid_alive


def test_follow_prints_new_text_until_the_parent_is_gone(tmp_path):
    stream = tmp_path / "s.log"
    stream.write_text("first ")
    out = io.StringIO()
    checks = iter([True, True, False])

    def alive():
        with open(stream, "a") as f:
            f.write("more ")
        return next(checks)

    follow(stream, alive, out, poll=0)
    assert out.getvalue() == "first more more more "


def test_follow_waits_for_a_missing_file(tmp_path):
    stream = tmp_path / "later.log"
    out = io.StringIO()
    checks = iter([True, False])

    def alive():
        stream.write_text("now here")
        return next(checks)

    follow(stream, alive, out, poll=0)
    assert out.getvalue() == "now here"


def test_pid_alive():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not pid_alive(proc.pid)
    import os
    assert pid_alive(os.getpid())


def test_watcher_process_exits_when_its_parent_dies(tmp_path):
    stream = tmp_path / "s.log"
    stream.write_text("")
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watcher = subprocess.Popen([sys.executable, "-m", "big_brother.watch", str(stream),
                                "--parent", str(parent.pid), "--title", "builder"],
                               stdout=subprocess.PIPE, text=True)
    time.sleep(0.5)
    assert watcher.poll() is None
    parent.kill()
    parent.wait()
    assert watcher.wait(timeout=5) == 0
    assert "builder" in watcher.stdout.read()
