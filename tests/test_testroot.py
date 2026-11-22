"""Test roots: base directory, layout, variables and the dropped device default."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401 - installs the test guards
from scripts import testroot

MIB = 1024 * 1024

# A known flag, so the tests do not depend on this platform's os.ST_NOEXEC.
NOEXEC = 1 << 20


def statvfs(*, flag=0, free=testroot.MIN_FREE, missing=()):
    """A fake ``os.statvfs``: ``free`` bytes in 4 KiB blocks, given mount flags."""

    def probe(path):
        if path in missing:
            raise FileNotFoundError(path)
        return mock.Mock(f_flag=flag, f_bavail=free // 4096, f_frsize=4096)

    return probe


class BaseTests(unittest.TestCase):
    def setUp(self):
        # Only the injected statvfs decides; the host's /dev/shm and /tmp do not.
        access = mock.patch.object(testroot.os, "access", return_value=True)
        self.access = access.start()
        self.addCleanup(access.stop)
        noexec = mock.patch.object(testroot.os, "ST_NOEXEC", NOEXEC, create=True)
        noexec.start()
        self.addCleanup(noexec.stop)

    def test_usable_needs_exec_space_and_access(self):
        self.assertTrue(testroot.usable("/dev/shm", statvfs()))
        cases = {
            "no-exec": statvfs(flag=NOEXEC),
            "under 64 MiB": statvfs(free=64 * MIB - 4096),
            "missing": statvfs(missing=("/dev/shm",)),
        }
        for name, probe in cases.items():
            with self.subTest(name):
                self.assertFalse(testroot.usable("/dev/shm", probe))
        self.access.reset_mock()
        self.access.return_value = False
        self.assertFalse(testroot.usable("/dev/shm", statvfs()))
        self.access.assert_called_once_with("/dev/shm", os.W_OK | os.X_OK)

    def test_usable_without_a_no_exec_constant_ignores_mount_flags(self):
        del testroot.os.ST_NOEXEC
        # Runs before the patch stops, which expects the attribute back.
        self.addCleanup(setattr, testroot.os, "ST_NOEXEC", NOEXEC)
        self.assertTrue(testroot.usable("/dev/shm", statvfs(flag=NOEXEC)))

    def test_bases_lists_usable_candidates_then_the_default(self):
        self.assertEqual(testroot.bases({}, statvfs()), ["/dev/shm", "/tmp", None])
        shm_missing = statvfs(missing=("/dev/shm",))
        self.assertEqual(testroot.bases({}, shm_missing), ["/tmp", None])
        self.assertEqual(testroot.bases({}, statvfs(flag=NOEXEC)), [None])

    def test_set_base_variable_wins_and_must_be_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            environ = {testroot.TMPBASE: directory}
            rejected = statvfs(missing=(directory,))
            self.assertEqual(testroot.bases(environ, rejected), [directory])
            with self.assertRaisesRegex(RuntimeError, testroot.TMPBASE):
                testroot.bases({testroot.TMPBASE: f"{directory}/missing"})
        empty = {testroot.TMPBASE: ""}
        self.assertEqual(testroot.bases(empty, statvfs()), ["/dev/shm", "/tmp", None])

    def test_base_variable_resolves_symlinks_before_parent_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "target")
            (target / "child").mkdir(parents=True)
            link = Path(directory, "link")
            link.symlink_to(target / "child")
            root = testroot.create({testroot.TMPBASE: f"{link}/.."})
            self.assertTrue(root.parent.samefile(target))

    def test_relative_base_variable_becomes_absolute(self):
        with tempfile.TemporaryDirectory() as directory:
            relative = os.path.relpath(directory)
            self.assertFalse(os.path.isabs(relative))
            found = testroot.bases({testroot.TMPBASE: relative})
        self.assertEqual(found, [directory])


class TestRootTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base)
        self.environ = {testroot.TMPBASE: self.base}
        self.root = testroot.create(self.environ, prefix="xbs-unit-")

    def test_create_makes_an_empty_private_directory(self):
        self.assertEqual(self.root.parent, Path(self.base))
        self.assertTrue(self.root.name.startswith("xbs-unit-"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        default = testroot.create(self.environ)
        self.assertTrue(default.name.startswith(testroot.PREFIX))

    def test_create_falls_through_bases_that_refuse_writes(self):
        refused = Path(self.base, "refused")
        accepted = Path(self.base, "accepted")
        accepted.mkdir()
        original = tempfile.mkdtemp
        made = []

        def mkdtemp(prefix, dir):
            made.append(dir)
            if dir == str(refused):
                raise PermissionError(dir)
            return original(prefix=prefix, dir=dir or str(accepted))

        cases = {
            "next candidate": (
                [str(refused), str(accepted), None],
                [str(refused), str(accepted)],
            ),
            "system default": ([str(refused), None], [str(refused), None]),
        }
        for name, (bases, tried) in cases.items():
            made.clear()
            with (
                self.subTest(name),
                mock.patch.object(testroot, "bases", return_value=bases),
                mock.patch.object(testroot.tempfile, "mkdtemp", side_effect=mkdtemp),
            ):
                self.assertEqual(testroot.create({}).parent, accepted)
                self.assertEqual(made, tried)

    def test_explicit_base_that_refuses_writes_raises(self):
        with (
            mock.patch.object(
                testroot.tempfile, "mkdtemp", side_effect=PermissionError("denied")
            ) as mkdtemp,
            self.assertRaises(PermissionError),
        ):
            testroot.create(self.environ)
        mkdtemp.assert_called_once_with(prefix=testroot.PREFIX, dir=self.base)

    def test_mkdtemp_result_is_absolute_even_for_relative_default(self):
        with (
            mock.patch.object(testroot, "bases", return_value=[None]),
            mock.patch.object(testroot.tempfile, "tempdir", "."),
        ):
            root = testroot.create({})
        try:
            self.assertTrue(root.is_absolute())
            self.assertEqual(root.parent, Path.cwd())
        finally:
            root.rmdir()

    def test_environment_makes_and_names_the_layout(self):
        variables = testroot.environment(self.root)
        self.assertEqual(
            variables,
            {
                testroot.ROOT: str(self.root),
                "HOME": str(self.root / "home"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_RUNTIME_DIR": str(self.root / "run"),
                "TMPDIR": str(self.root / "tmp"),
            },
        )
        for child in ("home", "data", "run", "tmp"):
            self.assertTrue((self.root / child).is_dir())
        self.assertEqual(testroot.environment(self.root), variables)

    def test_apply_replaces_the_variables_and_drops_the_device(self):
        environ = {
            "HOME": "/elsewhere",
            "TMPDIR": "/elsewhere/tmp",
            testroot.ROOT: "/stale",
            testroot.DEVICE: "00:00:5E:00:53:99",
            "PATH": "/bin",
        }
        testroot.apply(environ, self.root)
        self.assertEqual(environ, {"PATH": "/bin", **testroot.environment(self.root)})
        testroot.apply(environ, self.root)
        self.assertNotIn(testroot.DEVICE, environ)


if __name__ == "__main__":
    unittest.main()
