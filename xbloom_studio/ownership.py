"""Cooperative, per-user machine leases shared by library processes on a host."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import TextIO


class LeaseBusyError(ConnectionError):
    """A cooperative lease is already held by another owner."""


class LeaseDirectoryError(PermissionError):
    """The shared per-user lease directory cannot be used safely."""


def lease_directory() -> Path:
    import pwd

    # The account's home is shared by the CLI and service even when either
    # overrides HOME or runs in a private temporary directory.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "state" / "xbloom-studio" / "leases"


class AddressLease:
    """Exclusive advisory lock for one machine identity or other owned resource.

    The lock file name is a hash of the key, so any spelling that normalizes to
    the same identity shares one lease. Release unlinks the file while it still
    holds the lock, so acquire keeps a lock only when the path still names the
    file it locked, and otherwise retries. Requires POSIX ``fcntl``.
    """

    def __init__(self, address: str, *, resource: str = "Bluetooth address") -> None:
        self.address = address
        self.resource = resource
        self._lock: tuple[Path, TextIO] | None = None

    @property
    def held(self) -> bool:
        return self._lock is not None

    def acquire(self) -> None:
        import fcntl

        directory = lease_directory()
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.lstat()
        except FileExistsError as exc:
            raise LeaseDirectoryError("Lease directory path is occupied") from exc
        except OSError as exc:
            if not isinstance(exc, PermissionError) and exc.errno != errno.EROFS:
                raise
            raise LeaseDirectoryError(
                "Cannot access the per-user lease directory"
            ) from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise LeaseDirectoryError(
                "Lease directory must be private and owned by this user"
            )
        digest = hashlib.sha256(self.address.encode()).hexdigest()
        path = directory / f"{digest}.lock"
        while True:
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            except OSError as exc:
                if not isinstance(exc, PermissionError) and exc.errno != errno.EROFS:
                    raise
                raise LeaseDirectoryError(
                    "Cannot open a file in the per-user lease directory"
                ) from exc
            handle = os.fdopen(fd, "r+")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                linked = _links(path, handle)
            except BlockingIOError:
                handle.close()
                raise LeaseBusyError(
                    f"Another library process owns this {self.resource}"
                ) from None
            except BaseException:
                handle.close()
                raise
            if linked:
                break
            handle.close()
        self._lock = (path, handle)

    def release(self) -> None:
        if self._lock is not None:
            path, handle = self._lock
            self._lock = None
            with handle, suppress(OSError):
                if _links(path, handle):
                    os.unlink(path)


def _links(path: Path, handle: TextIO) -> bool:
    try:
        return os.path.samestat(path.lstat(), os.fstat(handle.fileno()))
    except FileNotFoundError:
        return False
