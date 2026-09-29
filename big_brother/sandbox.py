"""Run code big_brother did not write inside a bubblewrap sandbox.

The builder's test runs and the feedback runs execute code the local model
wrote, with the user's permissions. A temporary copy with no `.git` stops
relative writes only; an absolute path still reaches anything the user can.
`sandboxed(cmd, writable, cwd)` wraps a command in `bwrap` so it sees:

- `/usr` (and the `/bin`, `/lib`, `/lib64`, `/sbin` links into it), read-only;
- the virtualenv big_brother runs in, the Python it was built from (when that
  lives outside `/usr`, as a uv-managed Python does) and big_brother's own
  package, read-only, at their real paths, so the same interpreter, pytest,
  coverage and mutmut work unchanged;
- fresh `/proc`, `/dev` and an empty `/tmp`;
- the one writable directory, at its real path.

The root is remounted read-only afterwards, so the empty parent directories
bwrap creates for those mounts cannot be written either; only `/tmp` (a
throwaway tmpfs) and the writable directory can.

Nothing else exists inside: no home directory, no target repository, no
`.git`, no other project. Every namespace is unshared, so there is no network
and no view of other processes; all capabilities are dropped, the sandbox gets
a new terminal session, and it dies with its parent.

A missing `bwrap` is an error, never a silent fallback to running unsandboxed.
The kernel is still shared: escaping needs a kernel exploit.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import big_brother

USR_LINKS = {"/bin": "usr/bin", "/lib": "usr/lib", "/lib64": "usr/lib64", "/sbin": "usr/sbin"}


class SandboxUnavailable(Exception):
    """bubblewrap is not installed, so code under build cannot be isolated."""


def require_bwrap() -> str:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise SandboxUnavailable("install bubblewrap (dnf install bubblewrap) to run builds")
    return bwrap


def sandboxed(cmd: list[str], writable: Path | str, cwd: Path | str) -> list[str]:
    bwrap = require_bwrap()
    venv = Path(sys.prefix).resolve()
    base = Path(sys.base_prefix).resolve()
    package = Path(big_brother.__file__).resolve().parent
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr"]
    for link, dest in USR_LINKS.items():
        if Path(link).is_symlink():
            args += ["--symlink", dest, link]
        elif Path(link).is_dir():
            args += ["--ro-bind", link, link]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm", "--tmpfs", "/tmp",
             "--ro-bind", str(venv), str(venv), "--ro-bind", str(package), str(package)]
    if not base.is_relative_to("/usr"):
        args += ["--ro-bind", str(base), str(base)]
    writable = str(Path(writable).resolve())
    args += ["--bind", writable, writable,
             "--remount-ro", "/", "--chdir", str(Path(cwd).resolve()), "--"]
    return args + cmd
