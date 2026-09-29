"""Terminal windows that show the streams and close with the main program."""
import os
import sys

import pytest

from big_brother.windows import WindowError, Windows, find_terminal, watch_command, window_argv


def test_watch_command_runs_the_watcher_for_this_process():
    cmd = watch_command("builder", "/t/.git/big_brother/streams/builder.log")
    assert cmd == [sys.executable, "-m", "big_brother.watch",
                   "/t/.git/big_brother/streams/builder.log",
                   "--parent", str(os.getpid()), "--title", "builder"]


@pytest.mark.parametrize("terminal,head", [
    ("konsole", ["konsole", "--separate", "--hide-menubar", "-p", "tabtitle=bb builder", "-e"]),
    ("gnome-terminal", ["gnome-terminal", "--wait", "--title=bb builder", "--"]),
    ("kitty", ["kitty", "--title", "bb builder"]),
    ("xterm", ["xterm", "-T", "bb builder", "-e"]),
])
def test_window_argv_per_terminal(terminal, head):
    assert window_argv(terminal, "bb builder", ["cmd", "arg"]) == head + ["cmd", "arg"]


def test_find_terminal_prefers_konsole_then_the_others():
    have = {"xterm", "kitty"}
    assert find_terminal(which=lambda n: n in have and f"/usr/bin/{n}") == "kitty"
    assert find_terminal(which=lambda n: f"/usr/bin/{n}") == "konsole"


def test_find_terminal_honours_the_choice_and_refuses_what_is_missing():
    assert find_terminal("xterm", which=lambda n: "/usr/bin/xterm") == "xterm"
    with pytest.raises(WindowError, match="xterm"):
        find_terminal("xterm", which=lambda n: None)
    with pytest.raises(WindowError, match="no-windows"):
        find_terminal(which=lambda n: None)
    with pytest.raises(WindowError, match="unknown"):
        find_terminal("tilix", which=lambda n: "/usr/bin/tilix")


class FakeProc:
    def __init__(self, stubborn=False):
        self.terminated = self.killed = False
        self.stubborn = stubborn

    def poll(self):
        return 0 if (self.terminated and not self.stubborn) or self.killed else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if self.stubborn and not self.killed:
            import subprocess
            raise subprocess.TimeoutExpired("x", timeout)
        return 0

    def kill(self):
        self.killed = True


def test_windows_open_and_close_on_normal_exit():
    procs, launched = [], []

    def launch(argv):
        launched.append(argv)
        procs.append(FakeProc())
        return procs[-1]

    with Windows("konsole", launch=launch) as windows:
        windows.open("builder", "/s/builder.log")
        windows.open("writer", "/s/writer.log")
    assert len(launched) == 2 and launched[0][0] == "konsole"
    assert "big_brother.watch" in launched[0]
    assert all(p.terminated for p in procs)


def test_windows_close_when_the_block_raises_and_kill_the_stubborn():
    procs = [FakeProc(), FakeProc(stubborn=True)]
    it = iter(procs)
    with pytest.raises(RuntimeError):
        with Windows("konsole", launch=lambda argv: next(it), grace=0.01) as windows:
            windows.open("builder", "/s/b.log")
            windows.open("writer", "/s/w.log")
            raise RuntimeError("boom")
    assert procs[0].terminated and procs[1].killed


def test_disabled_windows_open_nothing():
    with Windows(None, launch=lambda argv: pytest.fail("launched")) as windows:
        windows.open("builder", "/s/b.log")
