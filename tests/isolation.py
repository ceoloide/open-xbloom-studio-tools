"""Give every test process tree a private home, data, runtime and temp root.

A process that starts without ``XBLOOM_STUDIO_TEST_ROOT`` owns a new root: it
points ``HOME``, the XDG directories, ``TMPDIR`` and ``tempfile`` at it, drops
``XBLOOM_STUDIO_DEVICE`` and deletes the root when it exits. A process that
inherits the variable changes no environment, keeps the ``TMPDIR`` it was given
and deletes nothing. Both replace ``pwd.getpwuid`` so
the production lease resolver, which reads the account home rather than
``HOME``, also lands inside the root, and both drop asyncio's load-dependent
slow-callback warnings so a passing run prints only unittest's own lines.
"""

from __future__ import annotations

import atexit
import logging
import os
import pwd
import shutil
import tempfile
from pathlib import Path

from scripts import testroot

root: Path | None = None
owner = False
account_getpwuid = pwd.getpwuid


def _getpwuid(uid: int) -> pwd.struct_passwd:
    entry = account_getpwuid(uid)
    if root is None or uid != os.getuid():
        return entry
    fields = list(entry)
    fields[5] = str(root / "home")
    return pwd.struct_passwd(fields)


def _fast_enough(record: logging.LogRecord) -> bool:
    return record.msg != "Executing %s took %.3f seconds"


def _remove(path: Path, pid: int) -> None:
    # A forked child inherits this handler; only the creator deletes the root.
    if os.getpid() == pid:
        shutil.rmtree(path, ignore_errors=True)


def install() -> None:
    global root, owner
    if root is not None:
        return
    inherited = os.environ.get(testroot.ROOT)
    if inherited:
        candidate = Path(os.path.abspath(inherited))
        if not (candidate / "home").is_dir():
            raise RuntimeError(
                f"{testroot.ROOT}={inherited!r} is not a test root; unset it"
            )
        root = candidate
    else:
        root = testroot.create(os.environ)
        testroot.apply(os.environ, root)
        tempfile.tempdir = os.environ["TMPDIR"]
        owner = True
        atexit.register(_remove, root, os.getpid())
    pwd.getpwuid = _getpwuid
    logging.getLogger("asyncio").addFilter(_fast_enough)
