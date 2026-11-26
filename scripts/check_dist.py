"""Check built sdist and wheel contents, metadata and an isolated install.

Usage: python -m scripts.check_dist DIST_DIR

DIST_DIR must hold exactly one sdist and one wheel of this project. The
distributed package files must match the tracked files under PACKAGE exactly,
so additions to the package extend the check without editing this script.
After an isolated install, every package data file is read back through
importlib.resources and compared with its source, and every console script
must be installed and run offline against the bundled simulator. The locked
``mcp`` extra is then installed from the uv cache, without network access,
and the documented quickstart runs through the installed scripts: direct
mode, a simulator daemon, the CLI as its client and ``xbloom-studio-mcp`` over
stdio with the README's tool groups.
"""

from __future__ import annotations

import configparser
import email.message
import email.parser
import hashlib
import importlib.metadata
import json
import site
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts import publication_policy

PROJECT = "xbloom-studio"
PACKAGE = "xbloom_studio"
LICENSE_EXPRESSION = "MIT"
REQUIRES_PYTHON = ">=3.14"
WHEEL_TAG = "py3-none-any"
COMPARED_FIELDS = (
    "Name",
    "Version",
    "Requires-Python",
    "License-Expression",
    "License-File",
    "Requires-Dist",
    "Provides-Extra",
)
WHEEL_METADATA_FILES = (
    "METADATA",
    "WHEEL",
    "RECORD",
    "entry_points.txt",
    "top_level.txt",
    "licenses/LICENSE",
)
ENTRY_POINTS = {
    "xbloom-studio": "xbloom_studio.cli:main",
    "xbloom-studio-mcp": "xbloom_studio.mcp_server:main",
}
SMOKE_KEY = "installed-smoke"
SDIST_ROOT_FILES = (
    "PKG-INFO",
    "README.md",
    "LICENSE",
    "MANIFEST.in",
    "pyproject.toml",
    "setup.cfg",
)
COPIED_ROOT_FILES = ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml")
SDIST_EGG_INFO_FILES = (
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "top_level.txt",
)

# Written by setuptools only when the project declares dependencies or extras.
SDIST_REQUIRES_FILE = "requires.txt"

Runner = Callable[[Sequence[str]], str]


def egg_info_files(expected: Mapping[str, list[str]]) -> tuple[str, ...]:
    if expected["Requires-Dist"] or expected["Provides-Extra"]:
        return (*SDIST_EGG_INFO_FILES, SDIST_REQUIRES_FILE)
    return SDIST_EGG_INFO_FILES


PROBE = """
import hashlib, importlib.metadata, importlib.resources, json, sys
import {package}
dist = importlib.metadata.distribution({project!r})

def resources(node, prefix):
    for child in node.iterdir():
        name = prefix + child.name
        if child.is_dir():
            if child.name != "__pycache__":
                yield from resources(child, name + "/")
        elif not child.name.endswith((".py", ".pyc")):
            yield name, hashlib.sha256(child.read_bytes()).hexdigest()

print(json.dumps({{
    "module": {package}.__file__,
    "prefix": sys.prefix,
    "version": dist.version,
    "files": sorted(str(f) for f in dist.files or ()),
    "scripts": {{
        e.name: e.value for e in dist.entry_points if e.group == "console_scripts"
    }},
    "resources": dict(resources(
        importlib.resources.files({package!r}), {package!r} + "/"
    )),
}}))
"""


# Runs with the installed environment's interpreter after the locked mcp extra
# is added. Arguments: the installed scripts directory and an empty work
# directory and a unique simulator identity. Default paths are redirected
# there, so the probe follows the README without sharing another test's lease.
DAEMON_PROBE = """
import asyncio, json, os, select, signal, subprocess, sys
from pathlib import Path
import xbloom_studio

scripts, root = Path(sys.argv[1]), Path(sys.argv[2])
(root / "home").mkdir(parents=True)
ENV = {**os.environ, "HOME": str(root / "home"), "XDG_DATA_HOME": str(root / "data"),
       "XDG_RUNTIME_DIR": str(root / "run"), "XBLOOM_STUDIO_DEVICE": sys.argv[3]}
WATER = {"kind": "water", "volume_ml": 50, "temperature_c": 20}
ACTIVE = ("accepted", "running", "waiting_for_user")


def cli(*args):
    done = subprocess.run([str(scripts / "xbloom-studio"), *args], env=ENV, cwd=root,
                          check=True, capture_output=True, text=True, timeout=120)
    lines = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else {}


def status(reply):
    data = reply.get("data") or {}
    return data.get("status") or (data.get("operation") or {}).get("status")


async def tools(group, calls):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(command=str(scripts / "xbloom-studio-mcp"),
                                   args=["--group", group], env=ENV, cwd=str(root))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = (await session.list_tools()).tools
            return len(listed), await calls(session)


async def call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    return result.structured_content


async def recipes(session):
    return len((await call(session, "recipe_templates", {}))["result"])


async def machine(session):
    start = {"request": WATER, "idempotency_key": "mcp", "allow_control": True}
    first = await call(session, "operation_start", start)
    state = first["status"]
    for _ in range(600):
        found = await call(session, "operation_lookup", {"idempotency_key": "mcp"})
        state = found["operation"]["status"]
        if state not in ACTIVE:
            break
        await asyncio.sleep(0.1)
    again = await call(session, "operation_start", start)
    return {"id": first["id"], "status": state, "replayed": again["id"] == first["id"]}


async def mcp():
    result = {"tools": {}}
    result["tools"]["recipes"], result["templates"] = await tools("recipes", recipes)
    result["tools"]["machine"], result["operation"] = await tools("machine", machine)

    async def history(session):
        ident = result["operation"]["id"]
        return (await call(session, "history_get", {"ident": ident}))["status"]

    result["tools"]["history"], result["history"] = await tools("history", history)
    return result


result = {"module": xbloom_studio.__file__}
result["direct"] = status(cli("--connector", "simulator", "--direct", "water",
                              "--volume-ml", "100", "--key", "first",
                              "--allow-control"))
result["lookup"] = status(cli("operation", "lookup", "first"))
serve = [str(scripts / "xbloom-studio"), "--connector", "simulator", "serve"]
daemon = subprocess.Popen(serve, env=ENV, cwd=root, stdout=subprocess.PIPE, text=True)
try:
    ready = select.select([daemon.stdout], [], [], 60)[0]
    line = daemon.stdout.readline() if ready else ""
    result["serving"] = "serving" in (json.loads(line or "{}").get("data") or {})
    if result["serving"]:
        result["connect"] = cli("machine", "connect").get("ok")
        result["daemon"] = status(cli("water", "--volume-ml", "100", "--key",
                                      "second", "--allow-control", "--wait"))
        result["mcp"] = asyncio.run(asyncio.wait_for(mcp(), 300))
finally:
    daemon.send_signal(signal.SIGTERM)
    try:
        result["exit"] = daemon.wait(60)
    except subprocess.TimeoutExpired:
        daemon.kill()
        result["exit"] = daemon.wait()
print(json.dumps(result))
"""
EXPECTED_PROBE = {
    "direct": "completed",
    "lookup": "completed",
    "serving": True,
    "connect": True,
    "daemon": "completed",
    "exit": 0,
}
GROUPS = ("recipes", "machine", "history")


class DistError(Exception):
    pass


def distribution_stem(version: str) -> str:
    return f"{PROJECT.replace('-', '_')}-{version}"


def find_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    wheels = sorted(dist_dir.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise DistError(
            f"expected one sdist and one wheel, found {len(sdists)} and {len(wheels)}"
        )
    return sdists[0], wheels[0]


def parse_metadata(data: bytes) -> email.message.Message:
    return email.parser.BytesParser().parsebytes(data)


def metadata_fields(message: email.message.Message) -> dict[str, list[str]]:
    return {
        field: sorted(str(value) for value in message.get_all(field, []))
        for field in COMPARED_FIELDS
    }


def read_wheel(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.filename in files:
                raise DistError("duplicate wheel member")
            files[info.filename] = archive.read(info)
    return files


def read_sdist(path: Path, stem: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            prefix, _, name = member.name.partition("/")
            extracted = archive.extractfile(member) if member.isfile() else None
            if prefix != stem or not name or extracted is None:
                raise DistError(f"unexpected sdist member: {member.name}")
            if name in files:
                raise DistError("duplicate sdist member")
            files[name] = extracted.read()
    return files


def compare_names(kind: str, actual: set[str], expected: set[str]) -> list[str]:
    return [f"{kind} unexpected file: {name}" for name in sorted(actual - expected)] + [
        f"{kind} missing file: {name}" for name in sorted(expected - actual)
    ]


def check_artifacts(
    sdist: Path,
    wheel: Path,
    sources: Mapping[str, bytes],
    expected: Mapping[str, list[str]],
) -> list[str]:
    """Return problems with artifact names, contents and metadata.

    ``sources`` maps tracked package files and COPIED_ROOT_FILES to content.
    """
    package_files = package_only(sources)
    version = expected["Version"][0]
    stem = distribution_stem(version)
    problems = []
    if sdist.name != f"{stem}.tar.gz":
        problems.append(f"unexpected sdist name: {sdist.name}")
    if wheel.name != f"{stem}-{WHEEL_TAG}.whl":
        problems.append(f"unexpected wheel name: {wheel.name}")

    wheel_files = read_wheel(wheel)
    sdist_files = read_sdist(sdist, stem)
    dist_info = f"{stem}.dist-info"
    egg_info = f"{PROJECT.replace('-', '_')}.egg-info"
    problems += compare_names(
        "wheel",
        set(wheel_files),
        set(package_files) | {f"{dist_info}/{name}" for name in WHEEL_METADATA_FILES},
    )
    problems += compare_names(
        "sdist",
        set(sdist_files),
        set(package_files)
        | set(SDIST_ROOT_FILES)
        | {f"{egg_info}/{name}" for name in egg_info_files(expected)},
    )
    copies = [("sdist", sdist_files, name, name) for name in sorted(sources)]
    copies += [("wheel", wheel_files, name, name) for name in sorted(package_files)]
    copies.append(("wheel", wheel_files, f"{dist_info}/licenses/LICENSE", "LICENSE"))
    for kind, files, member, source in copies:
        if member in files and files[member] != sources.get(source):
            problems.append(f"{kind} file differs from source: {member}")

    for kind, raw in (
        ("wheel", wheel_files.get(f"{dist_info}/METADATA")),
        ("sdist", sdist_files.get("PKG-INFO")),
    ):
        if raw is None:
            continue
        fields = metadata_fields(parse_metadata(raw))
        for field, value in expected.items():
            if fields[field] != value:
                problems.append(f"{kind} metadata differs from source: {field}")

    for kind, raw in (
        ("wheel", wheel_files.get(f"{dist_info}/entry_points.txt")),
        ("sdist", sdist_files.get(f"{egg_info}/entry_points.txt")),
    ):
        if raw is not None and entry_points(raw) != {"console_scripts": ENTRY_POINTS}:
            problems.append(f"{kind} entry points differ from policy")

    for kind, files in (("wheel", wheel_files), ("sdist", sdist_files)):
        problems.extend(
            f"{kind}:{finding}"
            for finding in publication_policy.check_files(sorted(files.items()))
        )
    return problems


def entry_points(raw: bytes) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string(raw.decode("utf-8"))
    except configparser.Error, UnicodeDecodeError:
        return {}
    return {name: dict(parser[name]) for name in parser.sections()}


def expected_metadata(source: email.message.Message) -> dict[str, list[str]]:
    """Expected artifact metadata: the source checkout's, plus fixed policy."""
    expected = metadata_fields(source)
    problems = []
    if expected["Name"] != [PROJECT]:
        problems.append("source project name differs from policy")
    if expected["License-Expression"] != [LICENSE_EXPRESSION]:
        problems.append("source license differs from policy")
    if expected["Requires-Python"] != [REQUIRES_PYTHON]:
        problems.append("source Requires-Python differs from policy")
    if len(expected["Version"]) != 1:
        problems.append("source version is missing")
    if problems:
        raise DistError("; ".join(problems))
    return expected


def source_metadata() -> email.message.Message:
    """Metadata of the project as synced into this environment by uv.

    Only site-packages is searched: build tools leave stale metadata in the
    working tree, which is first on ``sys.path`` under ``python -m``.
    """
    for dist in importlib.metadata.distributions(
        name=PROJECT, path=site.getsitepackages()
    ):
        return parse_metadata((dist.read_text("METADATA") or "").encode())
    raise DistError(f"{PROJECT} is not installed in this environment")


def run_command(command: Sequence[str]) -> str:
    return subprocess.run(
        command, check=True, capture_output=True, text=True, cwd=tempfile.gettempdir()
    ).stdout


def check_install(
    wheel: Path,
    workdir: Path,
    package_files: Mapping[str, bytes],
    version: str,
    run: Runner = run_command,
) -> list[str]:
    """Install the wheel alone into a fresh environment and probe it."""
    venv = workdir / "venv"
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    run(["uv", "venv", "--quiet", "--python", sys.executable, str(venv)])
    run(
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(python),
            "--no-index",
            "--no-deps",
            str(wheel),
        ]
    )
    probe = PROBE.format(package=PACKAGE, project=PROJECT)
    result = json.loads(run([str(python), "-I", "-c", probe]))
    problems = []
    prefix = Path(result["prefix"]).resolve()
    if not Path(result["module"]).resolve().is_relative_to(prefix):
        problems.append("installed package was not imported from the new environment")
    if prefix != venv.resolve():
        problems.append("probe did not run in the new environment")
    if result["version"] != version:
        problems.append(f"installed version is {result['version']}")
    if result["scripts"] != ENTRY_POINTS:
        problems.append("installed entry points differ from policy")
    installed = {name for name in result["files"] if name.startswith(f"{PACKAGE}/")}
    problems += compare_names("installed", installed, set(package_files))
    problems += check_resources(result["resources"], package_files)
    if not problems:
        problems += check_entry_points(venv, workdir, run)
    return problems


def check_entry_points(
    venv: Path, workdir: Path, run: Runner = run_command, project: Path | None = None
) -> list[str]:
    """Run the installed console scripts against the simulator.

    The MCP SDK comes from the locked ``mcp`` extra of ``project`` (default:
    the working directory), synced offline from the uv cache.
    """
    scripts = venv / ("Scripts" if sys.platform == "win32" else "bin")
    missing = [name for name in ENTRY_POINTS if not (scripts / name).is_file()]
    if missing:
        return [f"installed entry point missing: {name}" for name in missing]
    problems = []
    device = f"sim-smoke-{uuid.uuid4().hex}"
    run([str(scripts / "xbloom-studio-mcp"), "--help"])
    output = run(
        [
            str(scripts / "xbloom-studio"),
            "--db",
            str(workdir / "smoke.sqlite3"),
            "--socket",
            str(workdir / "smoke.sock"),
            "--device",
            device,
            "--connector",
            "simulator",
            "--direct",
            "water",
            "--volume-ml",
            "50",
            "--key",
            SMOKE_KEY,
            "--allow-control",
        ]
    )
    lines = [json.loads(line) for line in output.splitlines() if line.strip()]
    if not lines or lines[-1].get("data", {}).get("status") != "completed":
        problems.append(
            "installed xbloom-studio did not complete a simulated operation"
        )
    # Lockfile URLs resolve from the cache the presubmit sync just filled;
    # --inexact keeps the installed wheel.
    run(
        [
            "env",
            f"UV_PROJECT_ENVIRONMENT={venv}",
            "uv",
            "sync",
            "--quiet",
            "--frozen",
            "--offline",
            "--inexact",
            "--no-install-project",
            "--no-dev",
            "--extra",
            "mcp",
            "--project",
            str(project or Path.cwd()),
        ]
    )
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    # Keep the daemon socket short even when TMPDIR is deeply nested.
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        probe = [
            str(python),
            "-I",
            "-c",
            DAEMON_PROBE,
            str(scripts),
            str(Path(directory) / "q"),
            device,
        ]
        return problems + check_probe(json.loads(run(probe)), venv)


def check_probe(result: Mapping[str, Any], venv: Path) -> list[str]:
    """Problems in the installed daemon, CLI client and MCP stdio flow."""
    problems = []
    if not Path(result["module"]).resolve().is_relative_to(venv.resolve()):
        problems.append("daemon probe did not import the installed package")
    problems += [
        f"installed quickstart step {step} returned {result.get(step)!r}"
        for step, value in EXPECTED_PROBE.items()
        if result.get(step) != value
    ]
    mcp = result.get("mcp") or {}
    operation = mcp.get("operation") or {}
    if (
        sorted(mcp.get("tools") or {}) != sorted(GROUPS)
        or not all(mcp["tools"].values())
        or not mcp.get("templates")
        or operation.get("status") != "completed"
        or operation.get("replayed") is not True
        or mcp.get("history") != "completed"
    ):
        problems.append("installed xbloom-studio-mcp did not complete the stdio flow")
    return problems


def check_resources(
    loaded: Mapping[str, str], package_files: Mapping[str, bytes]
) -> list[str]:
    """Package data read back through importlib.resources must match the source."""
    expected = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in package_files.items()
        if not name.endswith(".py")
    }
    problems = compare_names("installed resource", set(loaded), set(expected))
    problems += [
        f"installed resource differs from source: {name}"
        for name in sorted(loaded.keys() & expected.keys())
        if loaded[name] != expected[name]
    ]
    return problems


def package_only(sources: Mapping[str, bytes]) -> dict[str, bytes]:
    return {n: d for n, d in sources.items() if n.startswith(f"{PACKAGE}/")}


def source_files(root: Path) -> dict[str, bytes]:
    """Tracked files that artifacts must reproduce byte for byte."""
    files = {}
    for name, data in publication_policy.repository_files(root, tracked_only=True):
        if name.startswith(f"{PACKAGE}/") or name in COPIED_ROOT_FILES:
            if data is None:
                raise DistError(f"distributed file is a symbolic link: {name}")
            files[name] = data
    return files


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        sdist, wheel = find_artifacts(Path(args[0]).resolve())
        expected = expected_metadata(source_metadata())
        sources = source_files(Path.cwd())
        problems = check_artifacts(sdist, wheel, sources, expected)
        if not problems:
            with tempfile.TemporaryDirectory() as workdir:
                problems = check_install(
                    wheel, Path(workdir), package_only(sources), expected["Version"][0]
                )
    except DistError as error:
        problems = [str(error)]
    except subprocess.CalledProcessError as error:
        problems = [f"command failed: {error.cmd[0]} (exit {error.returncode})"]
        print(error.stderr or "", file=sys.stderr)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"distribution checks passed: {sdist.name}, {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
