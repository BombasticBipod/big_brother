"""Terminal windows that show a run's streams and close when the run ends.

Each window runs `big_brother.watch` on one stream file with this process as
its parent. Closing is layered: `Windows` terminates every window it opened
when its block exits, for any reason, and kills one that does not stop within
the grace period; and the watcher inside exits by itself once this process is
gone, which closes the window even after `kill -9`.

Terminals are tried in `TERMINALS` order unless one is named. `Windows(None)`
opens nothing, for `--no-windows` and headless runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable

TERMINALS = {
    "konsole": ["konsole", "--separate", "--hide-menubar", "-p", "tabtitle={title}", "-e"],
    "gnome-terminal": ["gnome-terminal", "--wait", "--title={title}", "--"],
    "kitty": ["kitty", "--title", "{title}"],
    "xterm": ["xterm", "-T", "{title}", "-e"],
}


class WindowError(Exception):
    """No terminal to open the stream windows in."""


def watch_command(title: str, path: str) -> list[str]:
    return [sys.executable, "-m", "big_brother.watch", str(path),
            "--parent", str(os.getpid()), "--title", title]


def window_argv(terminal: str, title: str, command: list[str]) -> list[str]:
    return [part.replace("{title}", title) for part in TERMINALS[terminal]] + command


def find_terminal(preferred: str | None = None,
                  which: Callable[[str], str | None] = shutil.which) -> str:
    if preferred:
        if preferred not in TERMINALS:
            raise WindowError(f"unknown terminal {preferred}; use one of {', '.join(TERMINALS)}")
        if not which(preferred):
            raise WindowError(f"{preferred} is not installed")
        return preferred
    for name in TERMINALS:
        if which(name):
            return name
    raise WindowError(f"no terminal found ({', '.join(TERMINALS)}); run with --no-windows")


def _launch(argv: list[str]):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


class Windows:
    def __init__(self, terminal: str | None, launch: Callable = _launch, grace: float = 3):
        self.terminal = terminal
        self.launch = launch
        self.grace = grace
        self.procs: list = []

    def open(self, title: str, path: str) -> None:
        if self.terminal is None:
            return
        argv = window_argv(self.terminal, f"big_brother {title}", watch_command(title, path))
        self.procs.append(self.launch(argv))

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=self.grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.procs = []

    def __enter__(self) -> "Windows":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
