"""Every test process tree uses its own account home, data and runtime root."""

from __future__ import annotations

import ast
import json
import os
import pwd
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from scripts import testroot
from tests import isolation
from xbloom_studio import ownership, rpc, storage

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "xbloom_studio"
# Prints what a child resolves; imported by name so it carries no guard itself.
PROBE = (
    "import importlib, json, sys\n"
    f"ownership = importlib.import_module('{PACKAGE}.ownership')\n"
    "print(json.dumps({'leases': str(ownership.lease_directory()),"
    " 'guarded': 'tests' in sys.modules}))\n"
)
REPORT = (
    "import json, os, tempfile, tests\n"
    "from tests import isolation\n"
    "print(json.dumps({'root': str(isolation.root), 'owner': isolation.owner,"
    f" 'device': os.environ.get('{testroot.DEVICE}'),"
    " 'home': os.environ['HOME'], 'tmp': tempfile.gettempdir()}))\n"
)


def run(args, *, env=None, cwd=REPO, **kwargs):
    environ = {**os.environ, "PYTHONPATH": str(REPO), **(env or {})}
    environ = {key: value for key, value in environ.items() if value is not None}
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=environ,
        timeout=60,
        **kwargs,
    )


class IsolationTests(unittest.TestCase):
    def test_production_resolvers_land_in_the_private_root(self):
        self.assertEqual(
            Path(ownership.lease_directory.__code__.co_filename),
            REPO / PACKAGE / "ownership.py",
        )
        root = isolation.root
        self.assertIsNotNone(root)
        for path in (
            ownership.lease_directory(),
            storage.default_db(),
            rpc.default_socket(),
            Path.home(),
            Path(tempfile.gettempdir()),
        ):
            self.assertTrue(path.is_relative_to(root), path)
        self.assertEqual(ownership.lease_directory().parents[3], root / "home")

    def test_children_share_the_parents_lease_directory(self):
        expected = str(ownership.lease_directory())
        with tempfile.TemporaryDirectory() as modules:
            Path(modules, "isolation_probe.py").write_text(PROBE)
            path = os.pathsep.join([str(REPO), modules])
            for tmpdir in (None, modules):
                children = {
                    "child module": ["-m", "tests.child", "isolation_probe"],
                    "-c": ["-c", "import tests\n" + PROBE],
                }
                for name, args in children.items():
                    with self.subTest(child=name, tmpdir=tmpdir):
                        env = {"PYTHONPATH": path, "TMPDIR": tmpdir}
                        result = run(args, env=env, check=True)
                        self.assertEqual(
                            json.loads(result.stdout),
                            {"leases": expected, "guarded": True},
                        )

    def test_lease_directory_is_private(self):
        lease = ownership.AddressLease(f"isolation:{uuid.uuid4().hex}")
        lease.acquire()
        self.addCleanup(lease.release)
        mode = ownership.lease_directory().stat().st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o700)

    def test_fresh_process_owns_and_removes_its_root(self):
        env = {testroot.ROOT: None, testroot.DEVICE: "00:00:5E:00:53:99"}
        result = run(["-c", REPORT], env=env, check=True)
        report = json.loads(result.stdout)
        root = Path(report["root"])
        self.assertTrue(report["owner"])
        self.assertIsNone(report["device"])
        self.assertNotEqual(root, isolation.root)
        self.assertEqual(report["home"], str(root / "home"))
        self.assertEqual(report["tmp"], str(root / "tmp"))
        self.assertFalse(root.exists())

    def test_relative_base_root_is_removed_after_a_directory_change(self):
        # The child creates a same-named sentinel after changing directory;
        # a relative root would make cleanup delete it and leave the real root.
        code = (
            "import json, os, tests\n"
            "from tests import isolation\n"
            "os.chdir('elsewhere')\n"
            "os.mkdir(os.path.join('base', isolation.root.name))\n"
            "print(json.dumps(str(isolation.root)))\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "base").mkdir()
            (work / "elsewhere" / "base").mkdir(parents=True)
            env = {testroot.ROOT: None, testroot.TMPBASE: "base"}
            result = run(["-c", code], env=env, cwd=work, check=True)
            root = Path(json.loads(result.stdout))
            self.assertTrue((work / "elsewhere" / "base" / root.name).is_dir())
            self.assertEqual(list((work / "base").iterdir()), [])
            self.assertEqual(root.parent, work / "base")

    def test_inheriting_process_neither_owns_nor_removes_the_root(self):
        result = run(["-c", REPORT], check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["root"], str(isolation.root))
        self.assertFalse(report["owner"])
        self.assertTrue(isolation.root.is_dir())

    def test_inheriting_process_keeps_the_tmpdir_it_is_given(self):
        with tempfile.TemporaryDirectory() as other:
            result = run(["-c", REPORT], env={"TMPDIR": other}, check=True)
            report = json.loads(result.stdout)
        self.assertEqual(report["root"], str(isolation.root))
        self.assertEqual(report["tmp"], other)

    def test_socket_paths_under_the_temp_directory_stay_short(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "run", "daemon.sock")
        self.assertLess(len(os.fsencode(path)), 100, path)

    def test_child_without_a_module_prints_usage(self):
        result = run(["-m", "tests.child"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: python -m tests.child MODULE", result.stderr)

    def test_stale_root_variable_fails_loudly(self):
        result = run(["-c", "import tests"], env={testroot.ROOT: "/nonexistent"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(testroot.ROOT, result.stderr)

    def test_forked_child_exit_keeps_the_parents_root(self):
        code = (
            "import json, os, sys, tests\n"
            "from tests import isolation\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    sys.exit(0)\n"
            "os.waitpid(pid, 0)\n"
            "print(json.dumps([str(isolation.root), isolation.root.is_dir()]))\n"
        )
        result = run(["-c", code], env={testroot.ROOT: None}, check=True)
        root, kept = json.loads(result.stdout)
        self.assertTrue(kept)
        self.assertFalse(Path(root).exists())

    def test_sigusr1_dumps_every_threads_stack(self):
        code = (
            "import threading, time, tests\n"
            "threading.Thread(target=time.sleep, args=(60,), daemon=True).start()\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        environ = {**os.environ, "PYTHONPATH": str(REPO)}
        with subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=REPO,
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            watchdog = threading.Timer(30, child.kill)
            watchdog.start()
            try:
                self.assertEqual(child.stdout.readline(), "ready\n")
                child.send_signal(signal.SIGUSR1)
                # One header per thread; EOF means the signal killed it.
                dump = ""
                while dump.count("(most recent call first)") < 2:
                    text = child.stderr.readline()
                    if not text:
                        break
                    dump += text
            finally:
                watchdog.cancel()
                child.kill()
        self.assertEqual(dump.count("(most recent call first)"), 2, dump)
        self.assertIn("Current thread", dump)

    def test_guards_install_without_a_usable_stderr(self):
        for stderr in ("io.StringIO()", "None"):
            with self.subTest(stderr=stderr):
                code = (
                    f"import io, sys\nsys.stderr = {stderr}\nimport tests\nprint('ok')"
                )
                result = run(["-c", code])
                self.assertEqual((result.returncode, result.stdout), (0, "ok\n"))

    def test_only_this_account_moves_home(self):
        uid = os.getuid()
        mine = pwd.getpwuid(uid)
        self.assertEqual(mine.pw_dir, str(isolation.root / "home"))
        self.assertEqual(mine.pw_name, isolation.account_getpwuid(uid).pw_name)
        other = uid + 1
        entry = pwd.struct_passwd(("other", "x", other, other, "", "/srv", "/bin/sh"))
        with mock.patch.object(isolation, "account_getpwuid", return_value=entry):
            self.assertIs(pwd.getpwuid(other), entry)


class GuardCoverageTests(unittest.TestCase):
    """Static checks that every test module and child process is guarded."""

    def parsed(self, pattern):
        for path in sorted((REPO / "tests").glob(pattern)):
            yield path, ast.parse(path.read_text(), str(path))

    def test_every_test_module_imports_the_guards(self):
        for path, tree in self.parsed("test_*.py"):
            names = [
                alias.name
                for node in tree.body
                if isinstance(node, ast.Import)
                for alias in node.names
            ] + [
                node.module
                for node in tree.body
                if isinstance(node, ast.ImportFrom) and node.level == 0
            ]
            with self.subTest(module=path.name):
                self.assertTrue(
                    any(name == "tests" or name.startswith("tests.") for name in names),
                    f"{path.name} must import tests at module level",
                )

    def test_no_child_runs_the_package_unguarded(self):
        imports = (f"import {PACKAGE}", f"from {PACKAGE}")
        found = []
        for path, tree in self.parsed("*.py"):
            for node in ast.walk(tree):
                items = (
                    node.args
                    if isinstance(node, ast.Call)
                    else node.elts
                    if isinstance(node, (ast.List, ast.Tuple))
                    else []
                )
                values = [
                    item.value if isinstance(item, ast.Constant) else None
                    for item in items
                ]
                if any(
                    flag == "-m"
                    and isinstance(module, str)
                    and (module == PACKAGE or module.startswith(f"{PACKAGE}."))
                    for flag, module in zip(values, values[1:], strict=False)
                ):
                    found.append(f"{path.name}:{node.lineno}")
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and any(text in node.value for text in imports)
                    and "import tests" not in node.value
                ):
                    found.append(f"{path.name}:{node.lineno}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
