"""Show one stream file in a terminal window for as long as the main program runs.

`python -m big_brother.watch FILE --parent PID` prints FILE as it grows, like
`tail -f`, and exits once process PID is gone. The terminal window closes when
its command exits, so a window never outlives the run, even when the main
program is killed without a chance to close it.

Usage: python -m big_brother.watch FILE --parent PID [--title TITLE]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def follow(path: Path, alive: Callable[[], bool], out: TextIO, poll: float = 0.1) -> None:
    """Copy what is appended to `path` onto `out` until `alive()` is false, then drain."""
    handle = None
    try:
        while True:
            if handle is None and path.exists():
                handle = open(path, errors="replace")
            if handle is not None:
                out.write(handle.read())
                out.flush()
            if not alive():
                if handle is None and path.exists():
                    handle = open(path, errors="replace")
                if handle is not None:
                    out.write(handle.read())
                    out.flush()
                return
            time.sleep(poll)
    finally:
        if handle is not None:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Follow a stream file while a process lives.")
    parser.add_argument("file")
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--title", default="")
    args = parser.parse_args(argv)
    if args.title:
        print(f"==== big_brother {args.title} ====", flush=True)
    try:
        follow(Path(args.file), lambda: pid_alive(args.parent), sys.stdout)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
