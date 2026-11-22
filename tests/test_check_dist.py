import contextlib
import hashlib
import io
import json
import os
import site
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

import tests  # noqa: F401 - installs the test guards
from scripts import check_dist

VERSION = "1.2.3"
STEM = f"xbloom_studio-{VERSION}"
PACKAGE_FILES = {
    "xbloom_studio/__init__.py": b'"""Doc."""\n',
    "xbloom_studio/py.typed": b"",
}
ROOT_FILES = {
    "LICENSE": b"MIT\n",
    "MANIFEST.in": b"",
    "README.md": b"# Readme\n",
    "pyproject.toml": b"",
}
SOURCES = {**PACKAGE_FILES, **ROOT_FILES}
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
METADATA = (
    "Metadata-Version: 2.4\n"
    "Name: xbloom-studio\n"
    f"Version: {VERSION}\n"
    "License-Expression: MIT\n"
    "License-File: LICENSE\n"
    "Requires-Python: >=3.14\n"
)
ENTRY_POINTS = (
    b"[console_scripts]\n"
    b"xbloom-studio = xbloom_studio.cli:main\n"
    b"xbloom-studio-mcp = xbloom_studio.mcp_server:main\n"
)


def expected():
    return check_dist.expected_metadata(check_dist.parse_metadata(METADATA.encode()))


def wheel_files(**changes):
    info = f"{STEM}.dist-info"
    files = {
        **PACKAGE_FILES,
        f"{info}/METADATA": METADATA.encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\n",
        f"{info}/RECORD": b"",
        f"{info}/entry_points.txt": ENTRY_POINTS,
        f"{info}/top_level.txt": b"xbloom_studio\n",
        f"{info}/licenses/LICENSE": b"MIT\n",
    }
    files.update(changes)
    return {name: data for name, data in files.items() if data is not None}


def sdist_files(**changes):
    egg = "xbloom_studio.egg-info"
    files = {
        **SOURCES,
        "PKG-INFO": METADATA.encode(),
        "setup.cfg": b"",
        f"{egg}/PKG-INFO": METADATA.encode(),
        f"{egg}/SOURCES.txt": b"",
        f"{egg}/dependency_links.txt": b"\n",
        f"{egg}/entry_points.txt": ENTRY_POINTS,
        f"{egg}/top_level.txt": b"xbloom_studio\n",
    }
    files.update(changes)
    return {name: data for name, data in files.items() if data is not None}


def write_wheel(directory, files, name=f"{STEM}-py3-none-any.whl"):
    path = Path(directory) / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xbloom_studio/", b"")
        for member, data in files.items():
            archive.writestr(member, data)
    return path


def write_sdist(directory, files, name=f"{STEM}.tar.gz", prefix=STEM, link=None):
    path = Path(directory) / name
    with tarfile.open(path, "w:gz") as archive:
        folder = tarfile.TarInfo(prefix)
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
        for member, data in files.items():
            info = tarfile.TarInfo(f"{prefix}/{member}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(f"{prefix}/{link}")
            info.type = tarfile.SYMTYPE
            info.linkname = "README.md"
            archive.addfile(info)
    return path


class ArtifactTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def check(self, wheel=None, sdist=None, **names):
        wheel_path = write_wheel(self.dir, wheel or wheel_files(), **names)
        sdist_path = write_sdist(self.dir, sdist or sdist_files())
        return check_dist.check_artifacts(sdist_path, wheel_path, SOURCES, expected())

    def test_matching_artifacts_pass(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(
            check_dist.find_artifacts(self.dir),
            (self.dir / f"{STEM}.tar.gz", self.dir / f"{STEM}-py3-none-any.whl"),
        )

    def test_artifact_count_and_names(self):
        with self.assertRaisesRegex(check_dist.DistError, "found 0 and 0"):
            check_dist.find_artifacts(self.dir)
        self.assertIn(
            "unexpected wheel name: x-1.0-py3-none-linux.whl",
            self.check(name="x-1.0-py3-none-linux.whl"),
        )
        sdist = write_sdist(self.dir, sdist_files(), name="x.tar.gz")
        wheel = write_wheel(self.dir, wheel_files())
        problems = check_dist.check_artifacts(sdist, wheel, SOURCES, expected())
        self.assertEqual(problems, ["unexpected sdist name: x.tar.gz"])

    def test_unexpected_and_missing_files(self):
        problems = self.check(
            wheel=wheel_files(**{"xbloom_studio/extra.json": b"{}"}),
            sdist=sdist_files(**{"xbloom_studio/py.typed": None}),
        )
        self.assertEqual(
            problems,
            [
                "wheel unexpected file: xbloom_studio/extra.json",
                "sdist missing file: xbloom_studio/py.typed",
            ],
        )

    def test_copied_files_must_match_source(self):
        license_file = f"{STEM}.dist-info/licenses/LICENSE"
        problems = self.check(
            wheel=wheel_files(**{license_file: b"other\n"}),
            sdist=sdist_files(**{"pyproject.toml": b"[project]\n"}),
        )
        self.assertEqual(
            problems,
            [
                "sdist file differs from source: pyproject.toml",
                f"wheel file differs from source: {license_file}",
            ],
        )

    def test_package_content_must_match_source(self):
        problems = self.check(wheel=wheel_files(**{"xbloom_studio/py.typed": b"x"}))
        self.assertEqual(
            problems, ["wheel file differs from source: xbloom_studio/py.typed"]
        )

    def test_metadata_must_match_source(self):
        changed = METADATA.replace("License-Expression: MIT", "License-Expression: X")
        problems = self.check(sdist=sdist_files(**{"PKG-INFO": changed.encode()}))
        self.assertEqual(
            problems, ["sdist metadata differs from source: License-Expression"]
        )
        missing = wheel_files(**{f"{STEM}.dist-info/METADATA": None})
        self.assertEqual(
            self.check(wheel=missing),
            [f"wheel missing file: {STEM}.dist-info/METADATA"],
        )

    def test_artifact_content_passes_publication_policy(self):
        secret = ("-----BEGIN " + "RSA PRIVATE KEY-----").encode()
        problems = self.check(sdist=sdist_files(**{"setup.cfg": secret}))
        self.assertEqual(problems, ["sdist:setup.cfg:1: private key"])

    def test_publication_checks_root_archive_names(self):
        problems = self.check(sdist=sdist_files(**{".env.local": b"example"}))
        self.assertIn("sdist:.env.local:0: forbidden file type", problems)

    def test_requires_file_follows_declared_dependencies(self):
        egg = "xbloom_studio.egg-info"
        requires = {f"{egg}/requires.txt": b"[ble]\nexample\n"}
        self.assertIn(
            f"sdist unexpected file: {egg}/requires.txt",
            self.check(sdist=sdist_files(**requires)),
        )
        declared = (
            METADATA + 'Requires-Dist: example; extra == "ble"\nProvides-Extra: ble\n'
        )
        metadata = {f"{STEM}.dist-info/METADATA": declared.encode()}
        sdist = sdist_files(
            **requires,
            **{"PKG-INFO": declared.encode(), f"{egg}/PKG-INFO": declared.encode()},
        )
        with tempfile.TemporaryDirectory() as directory:
            wheel_path = write_wheel(Path(directory), wheel_files(**metadata))
            sdist_path = write_sdist(Path(directory), sdist)
            want = check_dist.expected_metadata(
                check_dist.parse_metadata(declared.encode())
            )
            self.assertEqual(
                check_dist.check_artifacts(sdist_path, wheel_path, SOURCES, want), []
            )
            missing = write_sdist(
                Path(directory),
                sdist_files(
                    **{
                        "PKG-INFO": declared.encode(),
                        f"{egg}/PKG-INFO": declared.encode(),
                    }
                ),
                name="other.tar.gz",
            )
            problems = check_dist.check_artifacts(missing, wheel_path, SOURCES, want)
            self.assertIn(f"sdist missing file: {egg}/requires.txt", problems)

    def test_entry_points_must_match_policy(self):
        info = f"{STEM}.dist-info/entry_points.txt"
        egg = "xbloom_studio.egg-info/entry_points.txt"
        extra = ENTRY_POINTS + b"other = xbloom_studio.cli:main\n"
        for value in (extra, b"[gui_scripts]\nx = y:z\n", b"not ini"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.check(
                        wheel=wheel_files(**{info: value}),
                        sdist=sdist_files(**{egg: value}),
                    ),
                    [
                        "wheel entry points differ from policy",
                        "sdist entry points differ from policy",
                    ],
                )
        self.assertEqual(
            check_dist.entry_points(ENTRY_POINTS),
            {"console_scripts": check_dist.ENTRY_POINTS},
        )
        self.assertEqual(check_dist.entry_points(b"\xff"), {})

    def test_sdist_layout_is_strict(self):
        for kwargs in ({"prefix": "other"}, {"link": "alias"}):
            path = write_sdist(self.dir, sdist_files(), **kwargs)
            with self.subTest(**kwargs):
                with self.assertRaisesRegex(check_dist.DistError, "unexpected sdist"):
                    check_dist.read_sdist(path, STEM)

    def test_duplicate_archive_members_are_rejected(self):
        wheel = write_wheel(self.dir, wheel_files())
        with warnings.catch_warnings(), zipfile.ZipFile(wheel, "a") as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("xbloom_studio/py.typed", b"other")
        with self.assertRaisesRegex(check_dist.DistError, "duplicate wheel member"):
            check_dist.read_wheel(wheel)

        sdist = self.dir / "duplicate.tar.gz"
        with tarfile.open(sdist, "w:gz") as archive:
            for _ in range(2):
                archive.addfile(tarfile.TarInfo(f"{STEM}/README.md"))
        with self.assertRaisesRegex(check_dist.DistError, "duplicate sdist member"):
            check_dist.read_sdist(sdist, STEM)

    def test_source_metadata_policy(self):
        self.assertEqual(expected()["Version"], [VERSION])
        bad = "Name: other\nLicense-Expression: Apache-2.0\nRequires-Python: >=3.9\n"
        with self.assertRaises(check_dist.DistError) as caught:
            check_dist.expected_metadata(check_dist.parse_metadata(bad.encode()))
        for text in ("project", "license", "Requires-Python", "version is missing"):
            self.assertIn(text, str(caught.exception))


class InstallTest(unittest.TestCase):
    def probe(self, workdir, **changes):
        venv = (workdir / "venv").resolve()
        result = {
            "module": str(venv / "lib/site-packages/xbloom_studio/__init__.py"),
            "prefix": str(venv),
            "version": VERSION,
            "files": [*PACKAGE_FILES, f"{STEM}.dist-info/RECORD"],
            "scripts": dict(check_dist.ENTRY_POINTS),
            "resources": {"xbloom_studio/py.typed": EMPTY_SHA256},
        }
        result.update(changes)
        commands = []

        def run(command):
            commands.append(command)
            return json.dumps(result) if command[1] == "-I" else ""

        with mock.patch.object(
            check_dist, "check_entry_points", return_value=[]
        ) as entry_points:
            problems = check_dist.check_install(
                Path("x.whl"), workdir, PACKAGE_FILES, VERSION, run
            )
        if not problems:
            entry_points.assert_called_once_with(venv.parent / "venv", workdir, run)
        return problems, commands

    def test_install_is_isolated_and_complete(self):
        workdir = Path(tempfile.gettempdir())
        problems, commands = self.probe(workdir)
        self.assertEqual(problems, [])
        install = commands[1]
        self.assertIn("--no-index", install)
        self.assertIn("--no-deps", install)
        self.assertEqual(commands[2][1:3], ["-I", "-c"])

    def test_install_problems(self):
        workdir = Path(tempfile.gettempdir())
        problems, _ = self.probe(
            workdir,
            module=str(Path.cwd() / "xbloom_studio/__init__.py"),
            prefix=str(Path.cwd()),
            version="0",
            files=["xbloom_studio/__init__.py"],
        )
        self.assertEqual(
            problems,
            [
                "probe did not run in the new environment",
                "installed version is 0",
                "installed missing file: xbloom_studio/py.typed",
            ],
        )
        problems, _ = self.probe(workdir, module=str(Path.cwd() / "x.py"))
        self.assertEqual(
            problems, ["installed package was not imported from the new environment"]
        )
        for scripts in (
            {**check_dist.ENTRY_POINTS, "xbloom": "xbloom_studio.cli:main"},
            {"xbloom-studio": "xbloom_studio.cli:main"},
        ):
            problems, _ = self.probe(workdir, scripts=scripts)
            self.assertEqual(problems, ["installed entry points differ from policy"])
        self.assertEqual(
            check_dist.ENTRY_POINTS,
            {
                "xbloom-studio": "xbloom_studio.cli:main",
                "xbloom-studio-mcp": "xbloom_studio.mcp_server:main",
            },
        )

    def test_installed_resources_must_load_and_match(self):
        workdir = Path(tempfile.gettempdir())
        problems, _ = self.probe(
            workdir,
            resources={
                "xbloom_studio/py.typed": "0" * 64,
                "xbloom_studio/data/extra.json": EMPTY_SHA256,
            },
        )
        self.assertEqual(
            problems,
            [
                "installed resource unexpected file: xbloom_studio/data/extra.json",
                "installed resource differs from source: xbloom_studio/py.typed",
            ],
        )
        problems, _ = self.probe(workdir, resources={})
        self.assertEqual(
            problems, ["installed resource missing file: xbloom_studio/py.typed"]
        )

    def test_probe_reads_resources_through_importlib(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "xbloom_studio/data/__pycache__").mkdir(parents=True)
        (root / "xbloom_studio/__init__.py").write_text("")
        (root / "xbloom_studio/data/a.json").write_bytes(b"{}")
        (root / "xbloom_studio/data/__pycache__/a.pyc").write_bytes(b"")
        (root / f"{STEM}.dist-info").mkdir()
        (root / f"{STEM}.dist-info/METADATA").write_text(METADATA)
        probe = check_dist.PROBE.format(
            package="xbloom_studio", project="xbloom-studio"
        )
        output = subprocess.run(
            [sys.executable, "-c", probe],
            check=True,
            capture_output=True,
            text=True,
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root)},
        ).stdout
        self.assertEqual(
            json.loads(output)["resources"],
            {"xbloom_studio/data/a.json": hashlib.sha256(b"{}").hexdigest()},
        )

    def entry_points(self, scripts=("xbloom-studio", "xbloom-studio-mcp"), **outputs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        workdir = Path(directory.name)
        venv = workdir / "venv"
        (venv / "bin").mkdir(parents=True)
        for name in scripts:
            (venv / "bin" / name).write_text("")
        completed = json.dumps({"ok": True, "data": {"status": "completed"}})
        values = {"cli": completed + "\n", "probe": json.dumps(self.daemon(venv))}
        values.update(outputs)
        commands = []

        def run(command):
            commands.append(command)
            if command[0] == str(venv / "bin" / "python"):
                return values["probe"]
            if command[0].endswith("xbloom-studio"):
                return values["cli"]
            return ""

        problems = check_dist.check_entry_points(
            venv, workdir, run, project=Path("/project")
        )
        return problems, commands

    @staticmethod
    def daemon(venv, **changes):
        operation = {"id": "op", "status": "completed", "replayed": True}
        result = {
            **check_dist.EXPECTED_PROBE,
            "module": str(venv / "lib/xbloom_studio/__init__.py"),
            "mcp": {
                "tools": {"recipes": 2, "machine": 3, "history": 1},
                "templates": 3,
                "operation": operation,
                "history": "completed",
            },
        }
        result.update(changes)
        return result

    def test_installed_entry_points_run_the_simulator(self):
        problems, commands = self.entry_points()
        self.assertEqual(problems, [])
        self.assertEqual(commands[0][1:], ["--help"])
        self.assertIn("simulator", commands[1])
        self.assertIn("--direct", commands[1])
        sync, probe = commands[2:]
        self.assertEqual([sync[0], *sync[2:4]], ["env", "uv", "sync"])
        self.assertEqual(sync[1], f"UV_PROJECT_ENVIRONMENT={Path(probe[4]).parent}")
        for flag in ("--frozen", "--offline", "--inexact", "--no-install-project"):
            self.assertIn(flag, sync)
        self.assertEqual(sync[sync.index("--extra") + 1], "mcp")
        self.assertEqual(sync[sync.index("--project") + 1], "/project")
        self.assertEqual(probe[1:4], ["-I", "-c", check_dist.DAEMON_PROBE])
        self.assertTrue(probe[4].endswith("bin"))
        self.assertEqual(Path(probe[5]).parents[1], Path("/tmp"))
        device = commands[1][commands[1].index("--device") + 1]
        self.assertEqual(probe[6], device)
        self.assertNotEqual(self.entry_points()[1][-1][6], device)

    def test_installed_entry_point_problems(self):
        problems, commands = self.entry_points(scripts=("xbloom-studio",))
        self.assertEqual(problems, ["installed entry point missing: xbloom-studio-mcp"])
        self.assertEqual(commands, [])
        failed = json.dumps({"ok": True, "data": {"status": "failed"}})
        problems, _ = self.entry_points(cli=failed)
        self.assertEqual(
            problems, ["installed xbloom-studio did not complete a simulated operation"]
        )
        problems, _ = self.entry_points(cli="")
        self.assertIn(
            "installed xbloom-studio did not complete a simulated operation", problems
        )

    def test_daemon_probe_rejects_nonzero_cli_exit_with_completed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "xbloom-studio"
            cli.write_text(
                "#!/bin/sh\n"
                """echo '{"ok": true, "data": {"status": "completed"}}'\n"""
                "exit 2\n"
            )
            cli.chmod(0o700)
            output = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    check_dist.DAEMON_PROBE,
                    directory,
                    directory,
                    "00:00:5E:00:53:01",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertNotEqual(output.returncode, 0)
        self.assertIn("exit status 2", output.stderr)

    def test_daemon_probe_problems(self):
        venv = Path("/venv")
        self.assertEqual(check_dist.check_probe(self.daemon(venv), venv), [])
        elsewhere = self.daemon(venv, module="/elsewhere/x.py", daemon="unknown")
        del elsewhere["exit"]
        self.assertEqual(
            check_dist.check_probe(elsewhere, venv),
            [
                "daemon probe did not import the installed package",
                "installed quickstart step daemon returned 'unknown'",
                "installed quickstart step exit returned None",
            ],
        )
        stdio = "installed xbloom-studio-mcp did not complete the stdio flow"
        good = self.daemon(venv)["mcp"]
        for change in (
            {"tools": {"recipes": 2, "machine": 3}},
            {"tools": {"recipes": 2, "machine": 0, "history": 1}},
            {"templates": 0},
            {"operation": {**good["operation"], "status": "unknown"}},
            {"operation": {**good["operation"], "replayed": False}},
            {"history": "failed"},
        ):
            bad = self.daemon(venv, mcp={**good, **change})
            self.assertEqual(check_dist.check_probe(bad, venv), [stdio], change)
        self.assertIn(stdio, check_dist.check_probe(self.daemon(venv, mcp=None), venv))

    def test_run_command_returns_output(self):
        code = "print('ok')"
        self.assertEqual(check_dist.run_command([sys.executable, "-c", code]), "ok\n")


class MainTest(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = check_dist.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_usage(self):
        self.assertEqual(self.run_main([])[0], 2)

    def test_source_metadata_comes_from_site_packages(self):
        dist = mock.Mock()
        dist.read_text.return_value = METADATA
        with mock.patch("importlib.metadata.distributions") as distributions:
            distributions.return_value = iter([dist])
            self.assertEqual(check_dist.source_metadata()["Version"], VERSION)
            self.assertEqual(
                distributions.call_args.kwargs["path"], site.getsitepackages()
            )
            distributions.return_value = iter([])
            with self.assertRaisesRegex(check_dist.DistError, "not installed"):
                check_dist.source_metadata()

    def test_source_files_rejects_links(self):
        listed = [("README.md", b""), ("docs/a.md", b""), ("xbloom_studio/a.py", b"x")]
        with mock.patch.object(check_dist.publication_policy, "repository_files") as rf:
            rf.return_value = listed
            self.assertEqual(
                check_dist.source_files(Path(".")),
                {"README.md": b"", "xbloom_studio/a.py": b"x"},
            )
            rf.assert_called_with(Path("."), tracked_only=True)
            rf.return_value = [("xbloom_studio/b.py", None)]
            with self.assertRaisesRegex(check_dist.DistError, "symbolic link"):
                check_dist.source_files(Path("."))

    def test_main_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            write_wheel(directory, wheel_files())
            write_sdist(directory, sdist_files())
            source = check_dist.parse_metadata(METADATA.encode())
            patches = (
                mock.patch.object(check_dist, "source_metadata", return_value=source),
                mock.patch.object(check_dist, "source_files", return_value=SOURCES),
                mock.patch.object(check_dist, "check_install", return_value=[]),
            )
            with patches[0], patches[1], patches[2] as install:
                status, out, _ = self.run_main([directory])
                self.assertEqual(status, 0)
                self.assertIn("distribution checks passed", out)
                relative = os.path.relpath(directory)
                self.assertEqual(self.run_main([relative])[0], 0)
                self.assertTrue(install.call_args.args[0].is_absolute())

                install.reset_mock()
                write_sdist(directory, sdist_files(**{"setup.cfg": b"\x00"}))
                self.assertEqual(self.run_main([directory])[0], 1)
                install.assert_not_called()
                write_sdist(directory, sdist_files())

                install.return_value = ["installed version is 0"]
                self.assertEqual(self.run_main([directory])[0], 1)

                error = subprocess.CalledProcessError(1, ["uv"], stderr="boom")
                install.side_effect = error
                status, _, err = self.run_main([directory])
                self.assertEqual(status, 1)
                self.assertIn("command failed: uv (exit 1)", err)

            status, _, err = self.run_main([str(Path(directory) / "missing")])
            self.assertEqual(status, 1)
            self.assertIn("expected one sdist", err)


if __name__ == "__main__":
    unittest.main()
