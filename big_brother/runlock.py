"""One run at a time per repository.

A build and a suite change must never overlap, or tests could change in the
middle of a build. `run_lock` takes an exclusive, non-blocking `flock` on
`.git/big_brother/build.lock`; a second holder, in this process or another,
gets `BuildBusy` at once. `flock` locks belong to the open file, so two
`open()` calls in one process conflict too, unlike POSIX `lockf` locks.
"""
from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class BuildBusy(Exception):
    """Another build or suite change holds the run lock."""


def lock_path(repo: Path | str) -> Path:
    git_dir = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
                             check=True, capture_output=True, text=True).stdout.strip()
    return Path(git_dir) / "big_brother" / "build.lock"


@contextmanager
def run_lock(repo: Path | str) -> Iterator[None]:
    path = lock_path(repo)
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BuildBusy(f"{repo}: another build or suite change is running") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
