"""Private test roots that keep the suite off the account's real state.

A root holds the account home, data, runtime and temporary directories for one
test process tree. ``XBLOOM_STUDIO_TEST_ROOT`` tells child processes that their
tree already has one. New roots go under ``XBLOOM_STUDIO_TEST_TMPBASE`` when it
is set, else on the first usable of ``/dev/shm`` and ``/tmp``, so test files
avoid slow fsync on disk. These are pure functions: importing this module has
no side effect and never imports the tests or the package under test.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

ROOT = "XBLOOM_STUDIO_TEST_ROOT"
TMPBASE = "XBLOOM_STUDIO_TEST_TMPBASE"
DEVICE = "XBLOOM_STUDIO_DEVICE"
PREFIX = "xbs-test-"
LAYOUT = {
    "HOME": "home",
    "XDG_DATA_HOME": "data",
    "XDG_RUNTIME_DIR": "run",
    "TMPDIR": "tmp",
}
BASES = ("/dev/shm", "/tmp")
MIN_FREE = 64 * 1024 * 1024

StatVFS = Callable[[str], os.statvfs_result]


def usable(path: str, statvfs: StatVFS = os.statvfs) -> bool:
    """Whether ``path`` can hold roots: writable, exec allowed, 64 MiB free."""
    try:
        info = statvfs(path)
    except OSError:
        return False
    return (
        not info.f_flag & getattr(os, "ST_NOEXEC", 0)
        and info.f_bavail * info.f_frsize >= MIN_FREE
        and os.access(path, os.W_OK | os.X_OK)
    )


def bases(
    environ: Mapping[str, str], statvfs: StatVFS = os.statvfs
) -> list[str | None]:
    """Directories to try for new roots, in order; ``None`` is the system default."""
    chosen = environ.get(TMPBASE)
    if chosen:
        if not os.path.isdir(chosen):
            raise RuntimeError(f"{TMPBASE}={chosen!r} is not a directory")
        # Resolve symlinks before "..", as the kernel does.
        return [os.path.realpath(chosen)]
    return [*(path for path in BASES if usable(path, statvfs)), None]


def create(environ: Mapping[str, str], prefix: str = PREFIX) -> Path:
    """Make a new, empty, private root under the first base that accepts it."""
    *fallbacks, last = bases(environ)
    for directory in fallbacks:
        try:
            return Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
        except OSError:
            pass  # os.access misses sandbox rules, such as Landlock; try the next.
    return Path(tempfile.mkdtemp(prefix=prefix, dir=last))


def environment(root: Path) -> dict[str, str]:
    """Make the root's directories and return the variables that select them."""
    variables = {ROOT: str(root)}
    for name, child in LAYOUT.items():
        path = root / child
        path.mkdir(mode=0o700, exist_ok=True)
        variables[name] = str(path)
    return variables


def apply(environ: MutableMapping[str, str], root: Path) -> None:
    """Point ``environ`` at ``root`` and drop the user's default device."""
    environ.update(environment(root))
    environ.pop(DEVICE, None)
