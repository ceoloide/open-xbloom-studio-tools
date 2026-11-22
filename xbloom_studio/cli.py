"""The ``xbloom-studio`` command: local SQLite, a private daemon, or SSH.

Every command routes to one :meth:`~xbloom_studio.facade.Application.call`
method, including the same registry and binding for database-free recipe calls.
Machine work runs in the daemon (``xbloom-studio serve``) when its socket
exists, through ``--ssh`` on a remote daemon, or attached in this process with
``--direct`` when no daemon is running. ``xbloom-studio relay`` is the
restricted stdio endpoint that SSH forced commands run.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from .command_schema import CommandValidationError
from .connector import Connector, normalize_identity
from .diagnostics import (
    DiagnosticContext,
    InputFileReadError,
    LocalJsonSyntaxError,
    build_diagnostic,
    is_safe_human_note,
    preset_write_diagnostic,
    render_human_diagnostic,
    request_context,
    safe_identifier,
)
from .errors import error_detail
from .facade import PURE_METHODS, Application, bind_parameters
from .grind_profiles import grind_profile
from .provider_exports import load_export
from .recipe_validation import RecipeValidationError
from .rpc import LocalServer, call, default_socket, socket_path_exists
from .rpc_types import RemoteError
from .serialization import dumps
from .storage import ConflictError, Store
from .tea_guidance import FAMILIES

DEVICE_ENVIRONMENT = "XBLOOM_STUDIO_DEVICE"
TARGETS = ("host", "coffee", "tea")
ACTIVE = ("accepted", "running", "waiting_for_user")
RETAINED_CONNECTION_METHODS = (
    "machine_connect",
    "machine_disconnect",
    "machine_command",
    "operation_stop",
    "operation_pause_resume",
    "operation_acknowledge",
    "recipe_upload",
    "recipe_start_uploaded",
    "recipe_cancel_uploaded",
)
PURE_ACTIONS = (
    "grind-profile",
    "tea-references",
    "validate",
    "tea-validate",
    "preview",
    "constraints",
    "templates",
    "examples",
)
# The simulator has no physical timing, so sessions skip settle delays.
SIMULATOR_OPTIONS = {"settle": 0, "subscription_settle": 0, "transition_delay": 0}

Invoke = Callable[..., Awaitable[Any]]


_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "--db",
        "--socket",
        "--ssh",
        "--ssh-config",
        "--device",
        "--connector",
        "--error-format",
    }
)
_GLOBAL_FLAG_OPTIONS = frozenset({"--direct"})
_GLOBAL_OPTIONS = _GLOBAL_VALUE_OPTIONS | _GLOBAL_FLAG_OPTIONS
_ERROR_FORMATS = frozenset({"auto", "json", "human"})
_KNOWN_CLEANUP_STATUSES = frozenset(
    {"pending", "completed", "failed", "skipped_uncertain"}
)

_ARGPARSE_REQUIRED = re.compile(r"^the following arguments are required: .+$")
_ARGPARSE_EXPECTED = re.compile(
    r"^argument (--?[A-Za-z0-9_-]+(?:/--?[A-Za-z0-9_-]+)*): expected .+$"
)
_ARGPARSE_INVALID_VALUE = re.compile(
    r"^argument (--?[A-Za-z0-9_-]+(?:/--?[A-Za-z0-9_-]+)*): "
    r"invalid ([A-Za-z0-9_]+) value:"
)
_ARGPARSE_CHOICE = re.compile(
    r"^argument ("
    r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*"
    r"|--?[A-Za-z0-9_-]+(?:/--?[A-Za-z0-9_-]+)*"
    r"): invalid choice:"
)
_ARGPARSE_POS_CHOICE = re.compile(r"^invalid choice:")
_ARGPARSE_MUTEX = re.compile(
    r"^argument (--?[A-Za-z0-9_-]+(?:/--?[A-Za-z0-9_-]+)*): "
    r"not allowed with argument (--?[A-Za-z0-9_-]+(?:/--?[A-Za-z0-9_-]+)*)$"
)


class _CliParseError(ValueError):
    def __init__(self, message: str, *, prog: str) -> None:
        super().__init__(message)
        self.prog = prog


class _DiagnosticArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _CliParseError(message, prog=self.prog)


def parser(*, raise_on_error: bool = False) -> argparse.ArgumentParser:
    parser_cls: type[argparse.ArgumentParser] = (
        _DiagnosticArgumentParser if raise_on_error else argparse.ArgumentParser
    )
    p = parser_cls(
        prog="xbloom-studio",
        description=(
            "xbloom-studio recipes, complete history and owned machine operations"
        ),
    )
    p.add_argument("--db", type=Path)
    p.add_argument("--socket", type=Path, help="Local private daemon socket")
    p.add_argument(
        "--ssh",
        help="SSH host/config alias for the fixed remote RPC endpoint; never falls "
        "back to local execution",
    )
    p.add_argument(
        "--ssh-config",
        type=Path,
        help="Optional local OpenSSH configuration file; requires --ssh",
    )
    p.add_argument("--device", default=os.environ.get(DEVICE_ENVIRONMENT))
    p.add_argument(
        "--connector",
        help="Byte link for serve or --direct: 'simulator', or module:factory "
        "called with the device; defaults to local Bluetooth",
    )
    p.add_argument(
        "--direct",
        action="store_true",
        help="Use attached local execution; never compete with a daemon",
    )
    p.add_argument(
        "--error-format",
        choices=("auto", "json", "human"),
        default="auto",
        help="Error output format on stderr: auto (human on TTY, json otherwise), "
        "json, or human",
    )
    sub = p.add_subparsers(dest="command", required=True)
    daemon = sub.add_parser("serve", help="Run the private Unix socket daemon")
    daemon.add_argument(
        "--recover-stale-socket",
        action="store_true",
        help="Lease-checked recovery of an unchanged refused same-user Unix socket",
    )
    daemon.add_argument(
        "--capture-idle-telemetry",
        action="store_true",
        help="Persist raw notifications while connected without an active operation",
    )
    relay = sub.add_parser(
        "relay", help="Restricted one-shot stdio relay for SSH forced commands"
    )
    relay.add_argument("--socket", dest="relay_socket")
    sub.add_parser("mcp", help="Use the separate xbloom-studio-mcp command")
    c = sub.add_parser("call", help="Call one application method by name")
    c.add_argument("method")
    c.add_argument("--params", type=Path)
    for group in ("recipe", "history", "machine", "operation"):
        q = sub.add_parser(group)
        actions = q.add_subparsers(dest="action", required=True)
        if group == "recipe":
            _recipe_actions(actions)
        elif group == "history":
            _history_actions(actions)
        elif group == "machine":
            _machine_actions(actions)
        else:
            lookup = actions.add_parser("lookup")
            lookup.add_argument("idempotency_key")
            for name in ("get", "watch", "stop", "pause", "resume", "acknowledge"):
                a = actions.add_parser(name)
                a.add_argument("operation_id")
                if name == "acknowledge":
                    a.add_argument("token")
    for name in ("water", "grind", "brew"):
        a = sub.add_parser(name)
        control_flags(a)
        if name == "water":
            a.add_argument("--volume-ml", type=float, required=True)
            a.add_argument("--temperature-c", type=float, default=20)
            a.add_argument("--flow-ml-s", type=float, default=3)
            a.add_argument(
                "--pattern", choices=["center", "ring", "spiral"], default="center"
            )
        elif name == "grind":
            a.add_argument("--size", type=int, required=True)
            a.add_argument("--rpm", type=int, required=True)
            a.add_argument("--duration-s", type=float, required=True)
        else:
            a.add_argument("reference", nargs="?")
            a.add_argument("--file", type=Path)
            a.add_argument("--revision", type=int)
    return p


def _recipe_actions(actions: Any) -> None:
    a = actions.add_parser("validate")
    a.add_argument("file", type=Path)
    a = actions.add_parser("tea-references")
    a.add_argument("--family", choices=FAMILIES)
    a = actions.add_parser("tea-validate")
    a.add_argument("file", type=Path)
    a.add_argument("--expected-output-ml", type=int)
    a = actions.add_parser("grind-profile")
    a.add_argument("name")
    a.add_argument("--size", type=int, required=True)
    a.add_argument("--rpm", type=int, required=True)
    a.add_argument("--duration-s", type=float, required=True)
    a = actions.add_parser("preview")
    a.add_argument("file", type=Path)
    actions.add_parser("constraints")
    actions.add_parser("templates")
    a = actions.add_parser("create")
    a.add_argument("file", type=Path)
    a.add_argument("--temporary", action="store_true")
    for name in ("get", "compile", "archive", "alias", "update", "refresh"):
        a = actions.add_parser(name)
        a.add_argument("reference")
        if name in ("get", "compile"):
            a.add_argument("--revision", type=int)
        if name in ("archive", "update"):
            a.add_argument("--expected-revision", type=int, required=True)
        if name == "update":
            a.add_argument("file", type=Path)
        if name == "alias":
            a.add_argument("alias")
        if name == "refresh":
            a.add_argument("--flow-ml-s", type=float)
            a.add_argument("--target", choices=TARGETS)
    a = actions.add_parser("list")
    a.add_argument("--query", default="")
    a.add_argument("--include-temporary", action="store_true")
    list_flags(a)
    a = actions.add_parser("import")
    a.add_argument("url")
    a.add_argument("--flow-ml-s", type=float)
    a.add_argument("--temporary", action="store_true")
    a.add_argument("--target", choices=TARGETS)
    a = actions.add_parser("import-export")
    a.add_argument("file", type=Path)
    a.add_argument("--target", choices=TARGETS, default="host")
    a.add_argument("--flow-ml-s", type=float)
    a.add_argument("--temporary", action="store_true")
    a = actions.add_parser("search-online")
    a.add_argument("query")
    a.add_argument("--page", type=int, default=1)
    a = actions.add_parser("match")
    a.add_argument("file", type=Path)
    a.add_argument(
        "--limit",
        type=int,
        help="Return the best 1..1000 matches; omitted returns the complete ranking",
    )
    a = actions.add_parser("examples")
    a.add_argument("file", type=Path)
    a.add_argument("--water-ml", type=float)
    a.add_argument("--limit", type=int, default=5)


def _history_actions(actions: Any) -> None:
    a = actions.add_parser("list")
    a.add_argument("--query", default="")
    a.add_argument("--liked", action="store_true")
    list_flags(a)
    a.add_argument("--status", help="Filter recorded operation status; implies --page")
    a.add_argument(
        "--min-rating", type=int, help="Minimum explicit rating 1..5; implies --page"
    )
    for name in ("get", "events", "export", "feedback", "save-recipe", "repeat"):
        a = actions.add_parser(name)
        a.add_argument("operation_id")
        if name in ("events", "export"):
            a.add_argument("--after", type=int, default=0)
        if name == "feedback":
            a.add_argument("note")
            a.add_argument("--rating", type=int)
        if name == "save-recipe":
            a.add_argument("name")
        if name == "repeat":
            control_flags(a)


def _machine_actions(actions: Any) -> None:
    for name in ("state", "capabilities", "connect", "disconnect"):
        a = actions.add_parser(name)
        if name == "connect":
            a.add_argument("--allow-untested", action="store_true")
    a = actions.add_parser("preset-writes")
    a.add_argument("--slot", choices=("A", "B", "C"))
    a = actions.add_parser("command")
    a.add_argument("machine_command")
    a.add_argument("--params", type=Path)
    a.add_argument("--allow-control", action="store_true")
    a.add_argument("--allow-untested", action="store_true")
    a = actions.add_parser("upload")
    a.add_argument("reference", nargs="?")
    a.add_argument("--file", type=Path)
    a.add_argument("--revision", type=int)
    a.add_argument("--allow-control", action="store_true")
    a.add_argument("--allow-untested", action="store_true")
    a.add_argument("--expected-recipe-sha256")
    a = actions.add_parser("start-uploaded")
    a.add_argument("token")
    control_flags(a)
    a = actions.add_parser("cancel-uploaded")
    a.add_argument("token")
    a.add_argument("--allow-control", action="store_true")


def list_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--limit",
        type=int,
        help="Maximum items: legacy list default 100, page default 25; range 1..1000",
    )
    p.add_argument(
        "--page",
        action="store_true",
        help="Return a live-collection page envelope with items, has_more and "
        "next_cursor",
    )
    p.add_argument(
        "--cursor",
        help="Continue a page with identical query/filters, even after empty items; "
        "implies --page",
    )
    p.add_argument(
        "--detail",
        choices=("compact", "full"),
        help="Page projection: full includes documents/plans; defaults compact and "
        "implies --page",
    )


def list_route(
    a: argparse.Namespace, history: bool = False
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"query": a.query}
    params.update(
        {"liked": a.liked} if history else {"include_temporary": a.include_temporary}
    )
    page = a.page or a.cursor is not None or a.detail is not None
    if history:
        page = page or a.status is not None or a.min_rating is not None
    params["limit"] = a.limit if a.limit is not None else 25 if page else 100
    if page:
        params.update(
            cursor=a.cursor, detail=a.detail if a.detail is not None else "compact"
        )
        if history:
            params.update(status=a.status, min_rating=a.min_rating)
    return ("history_" if history else "recipe_") + ("page" if page else "list"), params


def control_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--allow-control", action="store_true")
    p.add_argument("--allow-untested", action="store_true")
    p.add_argument(
        "--key",
        required=True,
        help="Idempotency key; reuse only to retrieve the same attempt",
    )
    p.add_argument(
        "--wait", action="store_true", help="Follow daemon operation to completion"
    )


class _SourceTracker:
    def __init__(self) -> None:
        self.method: str | None = None
        self.params: Mapping[str, Any] | None = None
        self.path: str | None = None
        self.text: str | None = None
        self.parameter: str | None = None
        self.source_kind: str | None = None
        self.allow_excerpt: bool = False

    def record(
        self,
        path: str,
        text: str,
        *,
        parameter: str = "file",
        source_kind: str | None = None,
        allow_excerpt: bool = True,
    ) -> None:
        self.path = path
        self.text = text
        self.parameter = parameter
        self.source_kind = source_kind
        self.allow_excerpt = allow_excerpt


def _effective_error_format(choice: str) -> str:
    if choice in ("json", "human"):
        return choice
    return "human" if bool(getattr(sys.stderr, "isatty", lambda: False)()) else "json"


def _subcommand_tree(p: argparse.ArgumentParser) -> dict[str, set[str]]:
    tree: dict[str, set[str]] = {}
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            for cmd_name, subparser in action.choices.items():
                nested: set[str] = set()
                for sub_action in subparser._actions:
                    if isinstance(sub_action, argparse._SubParsersAction):
                        nested.update(sub_action.choices.keys())
                tree[cmd_name] = nested
    return tree


def _match_global_option(name: str) -> str | None:
    if not name.startswith("--") or len(name) < 3:
        return None
    if name in _GLOBAL_OPTIONS:
        return name
    matches = [opt for opt in _GLOBAL_OPTIONS if opt.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def _scan_cli_args(
    args: Sequence[str], p: argparse.ArgumentParser
) -> tuple[str, str | None, bool]:
    tree = _subcommand_tree(p)
    error_format = "auto"
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return error_format, None, False
        opt_name, has_eq, opt_val = token.partition("=")
        matched = _match_global_option(opt_name)
        if matched == "--error-format":
            if has_eq:
                if opt_val in _ERROR_FORMATS:
                    error_format = opt_val
                index += 1
            elif index + 1 < len(args) and not args[index + 1].startswith("-"):
                if args[index + 1] in _ERROR_FORMATS:
                    error_format = args[index + 1]
                index += 2
            else:
                index += 1
            continue
        if matched in _GLOBAL_VALUE_OPTIONS:
            index += 1 if has_eq else (2 if index + 1 < len(args) else 1)
            continue
        if matched in _GLOBAL_FLAG_OPTIONS and not has_eq:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break

    known_cmd: str | None = None
    if index < len(args) and args[index] in tree:
        top = args[index]
        known_cmd = top
        index += 1
        if index < len(args) and args[index] in tree[top]:
            known_cmd = f"{top} {args[index]}"
            index += 1

    trailing_global = False
    for token in args[index:]:
        if token == "--":
            break
        opt_name, has_eq, _ = token.partition("=")
        matched = _match_global_option(opt_name)
        if matched is not None and (not has_eq or matched in _GLOBAL_VALUE_OPTIONS):
            trailing_global = True
            break
    return error_format, known_cmd, trailing_global


def _target_parsers(
    p: argparse.ArgumentParser | None, known_cmd: str | None
) -> tuple[argparse.ArgumentParser, ...]:
    if p is None:
        return ()
    target: argparse.ArgumentParser = p
    if known_cmd:
        for part in known_cmd.split():
            found_sub: argparse.ArgumentParser | None = None
            for action in target._actions:
                if (
                    isinstance(action, argparse._SubParsersAction)
                    and part in action.choices
                ):
                    found_sub = action.choices[part]
                    break
            if found_sub is None:
                break
            target = found_sub
    return (target,) if target is p else (target, p)


def _parser_choices(
    p: argparse.ArgumentParser | None, known_cmd: str | None, arg_name: str | None
) -> tuple[str, ...] | None:
    for candidate in _target_parsers(p, known_cmd):
        for action in candidate._actions:
            if isinstance(action, argparse._SubParsersAction):
                if arg_name is None or action.dest == arg_name:
                    return tuple(str(choice) for choice in action.choices.keys())
            elif action.choices is not None and arg_name is not None:
                names = set(action.option_strings)
                if action.dest:
                    names.add(action.dest)
                if arg_name in names or any(
                    part in names for part in arg_name.split("/")
                ):
                    return tuple(str(choice) for choice in action.choices)
    return None


def _is_parser_mutex_conflict(
    p: argparse.ArgumentParser | None,
    known_cmd: str | None,
    first: str,
    second: str,
) -> bool:
    if first == second:
        return False
    for candidate in _target_parsers(p, known_cmd):
        for group in candidate._mutually_exclusive_groups:
            labels = {
                "/".join(action.option_strings)
                for action in group._group_actions
                if action.option_strings
            }
            if first in labels and second in labels:
                return True
    return False


def _sanitize_argparse_message(
    message: str,
    *,
    trailing_global: bool,
    p: argparse.ArgumentParser | None = None,
    known_cmd: str | None = None,
) -> str:
    text = message.strip()
    if _ARGPARSE_REQUIRED.fullmatch(text):
        return text
    if _ARGPARSE_EXPECTED.fullmatch(text):
        return text
    invalid_val = _ARGPARSE_INVALID_VALUE.match(text)
    if invalid_val:
        return f"argument {invalid_val.group(1)}: invalid {invalid_val.group(2)} value"
    choice_match = _ARGPARSE_CHOICE.match(text)
    if choice_match:
        arg_name = choice_match.group(1)
        choices = _parser_choices(p, known_cmd, arg_name)
        if choices:
            formatted = ", ".join(repr(choice) for choice in choices)
            return f"argument {arg_name}: invalid choice (choose from {formatted})"
        return f"argument {arg_name}: invalid choice"
    if _ARGPARSE_POS_CHOICE.match(text):
        choices = _parser_choices(p, known_cmd, None)
        if choices:
            formatted = ", ".join(repr(choice) for choice in choices)
            return f"invalid choice (choose from {formatted})"
        return "invalid choice"
    if text.startswith("unrecognized arguments:"):
        if trailing_global:
            return (
                "Unrecognized command-line arguments; global options such as "
                "--error-format must precede the subcommand."
            )
        return "Unrecognized command-line arguments."
    mutex_match = _ARGPARSE_MUTEX.fullmatch(text)
    if mutex_match and _is_parser_mutex_conflict(
        p, known_cmd, mutex_match.group(1), mutex_match.group(2)
    ):
        return (
            f"argument {mutex_match.group(1)}: not allowed with argument "
            f"{mutex_match.group(2)}"
        )
    return "Invalid command-line arguments."


def _parse_error_detail(
    exc: _CliParseError, args: Sequence[str], p: argparse.ArgumentParser
) -> tuple[str, dict[str, Any], DiagnosticContext | None]:
    error_format, known_cmd, trailing_global = _scan_cli_args(args, p)
    if exc.prog.startswith("xbloom-studio "):
        prog_cmd = exc.prog.removeprefix("xbloom-studio ").strip()
        if prog_cmd:
            known_cmd = prog_cmd
    cause = _sanitize_argparse_message(
        str(exc), trailing_global=trailing_global, p=p, known_cmd=known_cmd
    )
    if known_cmd:
        fix = (
            f"Run `xbloom-studio {known_cmd} --help` to check required arguments "
            "and options."
        )
    else:
        fix = (
            "Run `xbloom-studio --help` to check available subcommands and "
            "global options."
        )
    if trailing_global:
        fix += " Pass global options such as `--error-format` before the subcommand."
    ctx = DiagnosticContext(command=known_cmd) if known_cmd else None
    loc = {"kind": "command", "command": known_cmd} if known_cmd else None
    detail: dict[str, Any] = {
        "type": "ArgumentError",
        "code": "invalid_argument",
        "message": cause,
        "details": {},
        "next_step": fix,
        "retryable": False,
        "diagnostic": build_diagnostic(
            "XB_INVALID_ARGUMENT", cause=cause, fix=fix, location=loc
        ),
    }
    return error_format, detail, ctx


def _command_label(a: argparse.Namespace) -> str:
    command = str(a.command)
    if command == "call":
        return f"call {a.method}"
    action = getattr(a, "action", None)
    if command == "machine" and action == "command":
        return f"machine command {a.machine_command}"
    if isinstance(action, str):
        return f"{command} {action}"
    return command


def _command_parameter(a: argparse.Namespace) -> str | None:
    command = a.command
    action = getattr(a, "action", None)
    if command == "call":
        return "params" if a.params else None
    if command == "recipe":
        if action in ("get", "compile", "archive", "alias", "update", "refresh"):
            return "reference"
        if action in (
            "validate",
            "tea-validate",
            "preview",
            "create",
            "examples",
            "match",
            "import-export",
        ):
            return "file"
        if action == "import":
            return "url"
    if command == "history" and action in (
        "get",
        "events",
        "export",
        "feedback",
        "save-recipe",
        "repeat",
    ):
        return "operation_id"
    if command == "operation":
        return "idempotency_key" if action == "lookup" else "operation_id"
    if command == "machine":
        if action == "command" and a.params:
            return "params"
        if action == "preset-writes" and a.slot:
            return "slot"
        if action in ("start-uploaded", "cancel-uploaded"):
            return "token"
        if action == "upload":
            return "file" if a.file else "reference" if a.reference else None
    if command == "brew":
        return "file" if a.file else "reference" if a.reference else None
    return None


def _is_file_error(exc: BaseException | None, result_code: str | None = None) -> bool:
    if result_code == "validation_error":
        return True
    if isinstance(
        exc,
        (
            LocalJsonSyntaxError,
            InputFileReadError,
            RecipeValidationError,
            CommandValidationError,
        ),
    ):
        return True
    if (
        isinstance(exc, RemoteError)
        and isinstance(exc.detail, dict)
        and exc.detail.get("code") == "validation_error"
    ):
        return True
    return False


def _command_context(
    a: argparse.Namespace,
    tracker: _SourceTracker | None = None,
    *,
    exc: BaseException | None = None,
    result_code: str | None = None,
) -> DiagnosticContext:
    use_file = (
        tracker is not None
        and tracker.path is not None
        and _is_file_error(exc, result_code)
    )
    parameter = (
        tracker.parameter
        if use_file and tracker is not None and tracker.parameter is not None
        else _command_parameter(a)
    )
    return request_context(
        tracker.method if tracker is not None else None,
        tracker.params if tracker is not None else None,
        command=_command_label(a),
        parameter=parameter,
        source_path=tracker.path if use_file and tracker is not None else None,
        source_text=tracker.text if use_file and tracker is not None else None,
        source_kind=tracker.source_kind if use_file and tracker is not None else None,
        allow_excerpt=tracker.allow_excerpt
        if use_file and tracker is not None
        else False,
    )


def _emit_human_result_diagnostic(
    a: argparse.Namespace,
    result: dict[str, Any],
    code: str,
    fallback_message: str,
    tracker: _SourceTracker | None,
    *,
    remote: bool = False,
) -> None:
    if _effective_error_format(getattr(a, "error_format", "auto")) != "human":
        return
    context = _command_context(a, tracker, result_code=code)
    if code == "preset_write_incomplete":
        raw_details = result.get("details")
        payload = raw_details if isinstance(raw_details, Mapping) else None
        raw_attempt = result.get("attempt")
        attempt = raw_attempt if isinstance(raw_attempt, Mapping) else None
        rec_req = (
            result.get("recovery_required")
            if type(result.get("recovery_required")) is bool
            else None
        )
        cause = (
            result["error"]
            if isinstance(result.get("error"), str) and result["error"].strip()
            else fallback_message
        )
        envelope: dict[str, Any] = {
            "code": code,
            "message": cause,
            "details": {"preset_write": payload or {}},
            "diagnostic": preset_write_diagnostic(
                refused=False,
                cause=cause,
                payload=payload,
                attempt=attempt,
                enclosing=result,
                recovery_required=rec_req,
                context=context,
                trusted=not remote,
            ),
        }
        print(
            render_human_diagnostic(envelope, context=context, trusted_local=True),
            file=sys.stderr,
        )
        return
    envelope = {
        "code": code,
        "message": str(result.get("error") or fallback_message),
        "details": {"validation": result},
    }
    if not remote and "diagnostic" in result:
        envelope["diagnostic"] = result["diagnostic"]
    print(
        render_human_diagnostic(envelope, context=context, trusted_local=not remote),
        file=sys.stderr,
    )


def _emit_human_operation_diagnostic(
    a: argparse.Namespace,
    result: Mapping[str, Any],
    tracker: _SourceTracker | None,
    *,
    expected_id: str | None = None,
) -> None:
    if _effective_error_format(getattr(a, "error_format", "auto")) != "human":
        return
    raw_status = result.get("status")
    status = raw_status if raw_status in ("failed", "unknown") else "unknown"
    raw_op_id = expected_id if expected_id is not None else result.get("id")
    safe_op_id = safe_identifier(raw_op_id)
    unsafe_raw_id = (
        raw_op_id.strip()
        if isinstance(raw_op_id, str) and safe_op_id is None and raw_op_id.strip()
        else None
    )
    context = _command_context(a, tracker)
    location: dict[str, Any] = {"kind": "command", "command": _command_label(a)}
    if tracker is not None and tracker.method:
        location["method"] = tracker.method
    if safe_op_id is not None:
        location["operation_id"] = safe_op_id

    summary_bits = [f"status={status}"]
    reason: str | None = None
    outcome = result.get("result")
    if isinstance(outcome, Mapping):
        for key in ("reason", "error"):
            val = outcome.get(key)
            if isinstance(val, str) and val.strip():
                reason = val.strip()
                break
        cleanup = outcome.get("cleanup_status")
        if type(cleanup) is str and cleanup in _KNOWN_CLEANUP_STATUSES:
            summary_bits.append(f"cleanup_status={cleanup}")
        steps = outcome.get("steps")
        if reason is None and isinstance(steps, list):
            for step in steps:
                if isinstance(step, Mapping):
                    step_err = step.get("error")
                    if isinstance(step_err, str) and step_err.strip():
                        reason = step_err.strip()
                        break

    if status == "unknown":
        cause = (
            "Watched operation ended with status 'unknown'; physical effects "
            "may be uncertain."
        )
    else:
        cause = f"Watched operation ended with status '{status}'."
    if (
        reason is not None
        and is_safe_human_note(reason)
        and (unsafe_raw_id is None or unsafe_raw_id not in reason)
    ):
        cause = f"{cause} {reason}"

    if safe_op_id is not None:
        fix = (
            f"Inspect read-only `xbloom-studio history get {safe_op_id}`, "
            f"`xbloom-studio history events {safe_op_id}`, `xbloom-studio machine "
            "state`, and the physical machine. Never automatically replay a "
            "start with a new idempotency key."
        )
    else:
        fix = None
    diag = build_diagnostic(
        "XB_OPERATION_FAILED",
        cause=cause,
        fix=fix,
        location=location,
        context={"summary": "; ".join(summary_bits)},
    )
    envelope = {"code": "operation_failed", "message": cause, "diagnostic": diag}
    print(
        render_human_diagnostic(envelope, context=context, trusted_local=True),
        file=sys.stderr,
    )


def _emit_watch_mismatch(
    a: argparse.Namespace,
    expected_id: str | None,
    tracker: _SourceTracker | None,
) -> None:
    safe_op_id = safe_identifier(expected_id)
    context = _command_context(a, tracker)
    location: dict[str, Any] = {"kind": "command", "command": _command_label(a)}
    if tracker is not None and tracker.method:
        location["method"] = tracker.method
    if safe_op_id is not None:
        location["operation_id"] = safe_op_id
    cause = (
        "Daemon returned a mismatched operation record while watching the "
        "requested operation; its status is unverified."
    )
    fix = (
        f"Inspect read-only `xbloom-studio history get {safe_op_id}`, "
        f"`xbloom-studio history events {safe_op_id}`, `xbloom-studio machine "
        "state`, and the physical machine. Never automatically replay a "
        "start with a new idempotency key."
        if safe_op_id is not None
        else None
    )
    diag = build_diagnostic(
        "XB_OPERATION_FAILED",
        cause=cause,
        fix=fix,
        location=location,
    )
    envelope: dict[str, Any] = {
        "type": "OperationWatchError",
        "code": "operation_failed",
        "message": cause,
        "details": {"operation_id": safe_op_id} if safe_op_id is not None else {},
        "next_step": diag["fix"],
        "retryable": False,
        "diagnostic": diag,
    }
    _emit_cli_error(
        getattr(a, "error_format", "auto"),
        envelope,
        context,
        trusted_local=True,
    )


def load(
    path: Path,
    tracker: _SourceTracker | None = None,
    *,
    parameter: str = "file",
    source_kind: str | None = None,
    allow_excerpt: bool = True,
) -> Any:
    if tracker is not None:
        tracker.record(
            str(path),
            "",
            parameter=parameter,
            source_kind=source_kind,
            allow_excerpt=allow_excerpt,
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InputFileReadError(
            f"Unable to read JSON input file: {path}", display_path=str(path)
        ) from exc
    if tracker is not None:
        tracker.record(
            str(path),
            text,
            parameter=parameter,
            source_kind=source_kind,
            allow_excerpt=allow_excerpt,
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LocalJsonSyntaxError(
            exc.msg, exc.doc, exc.pos, display_path=str(path)
        ) from exc


def _recipe_source(
    a: argparse.Namespace, tracker: _SourceTracker | None = None
) -> dict[str, Any]:
    if bool(a.reference) == bool(a.file):
        raise ValueError("Provide exactly one recipe reference or --file")
    if a.reference:
        return {"kind": "recipe", "reference": a.reference, "revision": a.revision}
    if a.revision is not None:
        raise ValueError("--revision requires a saved recipe reference")
    return {
        "kind": "recipe",
        "document": load(
            a.file, tracker, parameter="file", source_kind="recipe_document"
        ),
    }


def _recipe_route(
    a: argparse.Namespace, tracker: _SourceTracker | None = None
) -> tuple[str, dict[str, Any]]:
    action = a.action
    if action == "tea-references":
        return "recipe_tea_references", {"family": a.family}
    if action == "tea-validate":
        return "recipe_tea_validate", {
            "document": load(a.file, tracker, source_kind="recipe_document"),
            "expected_output_ml": a.expected_output_ml,
        }
    if action in ("preview", "validate"):
        return "recipe_" + action, {
            "document": load(a.file, tracker, source_kind="recipe_document")
        }
    if action in ("constraints", "templates"):
        return "recipe_" + action, {}
    if action == "examples":
        return "recipe_examples", {
            "coffee": load(a.file, tracker, source_kind="coffee_input"),
            "water_ml": a.water_ml,
            "limit": a.limit,
        }
    if action == "grind-profile":
        document = grind_profile(a.name, a.size, a.rpm, a.duration_s)
        return "recipe_validate", {"document": document}
    if action == "create":
        return "recipe_create", {
            "document": load(a.file, tracker, source_kind="recipe_document"),
            "temporary": a.temporary,
        }
    if action == "update":
        return "recipe_update", {
            "reference": a.reference,
            "document": load(a.file, tracker, source_kind="recipe_document"),
            "expected_revision": a.expected_revision,
        }
    if action in ("get", "compile"):
        return "recipe_" + action, {"reference": a.reference, "revision": a.revision}
    if action == "archive":
        return "recipe_archive", {
            "reference": a.reference,
            "expected_revision": a.expected_revision,
        }
    if action == "alias":
        return "recipe_alias", {"reference": a.reference, "alias": a.alias}
    target = {"target": a.target} if getattr(a, "target", None) is not None else {}
    if action == "refresh":
        return "recipe_refresh", {
            "reference": a.reference,
            "flow_ml_s": a.flow_ml_s,
            **target,
        }
    if action == "list":
        return list_route(a)
    if action == "import-export":
        return "recipe_import_export", {
            "document": load_export(a.file),
            "flow_ml_s": a.flow_ml_s,
            "temporary": a.temporary,
            **target,
        }
    if action == "import":
        return "recipe_import", {
            "url": a.url,
            "flow_ml_s": a.flow_ml_s,
            "temporary": a.temporary,
            **target,
        }
    if action == "search-online":
        return "provider_search", {"query": a.query, "page": a.page}
    limit = {"limit": a.limit} if a.limit is not None else {}
    return "recipe_match", {
        "coffee": load(a.file, tracker, source_kind="coffee_input"),
        **limit,
    }


def _machine_route(
    a: argparse.Namespace, tracker: _SourceTracker | None = None
) -> tuple[str, dict[str, Any]]:
    if a.action == "command":
        return "machine_command", {
            "command": a.machine_command,
            "params": load(
                a.params, tracker, parameter="params", source_kind="command_params"
            )
            if a.params
            else {},
            "allow_control": a.allow_control,
            "allow_untested": a.allow_untested,
        }
    if a.action == "upload":
        return "recipe_upload", {
            "request": _recipe_source(a, tracker),
            "allow_control": a.allow_control,
            "allow_untested": a.allow_untested,
            "expected_recipe_sha256": a.expected_recipe_sha256,
        }
    if a.action == "start-uploaded":
        return "recipe_start_uploaded", {
            "token": a.token,
            "idempotency_key": a.key,
            "allow_control": a.allow_control,
        }
    if a.action == "cancel-uploaded":
        return "recipe_cancel_uploaded", {
            "token": a.token,
            "allow_control": a.allow_control,
        }
    if a.action == "preset-writes":
        return "machine_preset_writes", {"slot": a.slot}
    connect = {"allow_untested": a.allow_untested} if a.action == "connect" else {}
    return "machine_" + a.action, connect


def _operation_route(a: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if a.action == "lookup":
        return "operation_lookup", {"idempotency_key": a.idempotency_key}
    if a.action in ("get", "watch"):
        return "history_get", {"ident": a.operation_id}
    if a.action in ("pause", "resume"):
        return "operation_pause_resume", {
            "operation_id": a.operation_id,
            "resume": a.action == "resume",
        }
    token = {"token": a.token} if a.action == "acknowledge" else {}
    return "operation_" + a.action, {"operation_id": a.operation_id, **token}


def route(
    a: argparse.Namespace, tracker: _SourceTracker | None = None
) -> tuple[str, dict[str, Any]]:
    """Map parsed arguments to one application method and its parameters."""
    if a.command == "call":
        return (
            a.method,
            load(a.params, tracker, parameter="params", source_kind="method_params")
            if a.params
            else {},
        )
    if a.command == "recipe":
        return _recipe_route(a, tracker)
    if a.command == "machine":
        return _machine_route(a, tracker)
    if a.command == "operation":
        return _operation_route(a)
    if a.command == "history":
        if a.action == "list":
            return list_route(a, history=True)
        if a.action == "get":
            return "history_get", {"ident": a.operation_id}
        if a.action in ("events", "export"):
            return "history_events", {"operation_id": a.operation_id, "after": a.after}
        if a.action == "feedback":
            return "history_feedback", {
                "ident": a.operation_id,
                "note": a.note,
                "rating": a.rating,
            }
        if a.action == "save-recipe":
            return "history_save_recipe", {
                "operation_id": a.operation_id,
                "name": a.name,
            }
        request: dict[str, Any] = {"kind": "repeat", "operation_id": a.operation_id}
    elif a.command == "brew":
        request = _recipe_source(a, tracker)
    else:
        fields = (
            ("volume_ml", "temperature_c", "flow_ml_s", "pattern")
            if a.command == "water"
            else ("size", "rpm", "duration_s")
        )
        request = {"kind": a.command, **{field: getattr(a, field) for field in fields}}
    return "operation_start", {
        "request": request,
        "idempotency_key": a.key,
        "allow_control": a.allow_control,
        "allow_untested": a.allow_untested,
    }


def load_connector(spec: str, device: str | None) -> tuple[Connector, dict[str, Any]]:
    """Build a connector from ``simulator`` or an importable ``module:factory``.

    A factory is called with the configured device (or ``None``) and must
    return a :class:`~xbloom_studio.Connector` for that machine.
    """
    if spec == "simulator":
        from .simulator import ScriptedConnector

        return ScriptedConnector(device or "SIMULATED-STUDIO"), dict(SIMULATOR_OPTIONS)
    module, _, name = spec.partition(":")
    if not module or not name.isidentifier():
        raise ValueError("--connector must be 'simulator' or module:factory")
    connector = getattr(importlib.import_module(module), name)(device)
    if not isinstance(connector, Connector):
        raise TypeError("The connector factory must return a Connector")
    return connector, {}


def output(value: Any) -> None:
    print(dumps({"ok": True, "data": value}), flush=True)


def _check_options(a: argparse.Namespace) -> None:
    if a.command == "relay":
        raise ValueError("xbloom-studio relay takes no global options")
    if a.command == "mcp":
        raise ValueError("Use xbloom-studio-mcp for the optional MCP adapter")
    if a.ssh_config is not None and a.ssh is None:
        raise ValueError("--ssh-config requires --ssh")


def _same_device(configured: str | None, served: object) -> bool:
    if not configured:
        return True
    try:
        return normalize_identity(configured) == normalize_identity(served)
    except ValueError:
        return False


async def run(a: argparse.Namespace, *, _tracker: _SourceTracker | None = None) -> int:
    _check_options(a)
    app: Application | None = None
    invoke: Invoke
    if a.ssh is not None:
        from .ssh_rpc import ssh_call, validate_host

        validate_host(a.ssh)
        if (
            a.command == "serve"
            or a.direct
            or a.db is not None
            or a.socket is not None
            or a.connector is not None
        ):
            raise ValueError(
                "--ssh cannot be combined with serve, --direct, --db, --socket or "
                "--connector; the remote endpoint fixes its database and link"
            )
        remote = True

        async def invoke(method: str, params: dict[str, Any] | None = None) -> Any:
            return await ssh_call(a.ssh, method, params, config_file=a.ssh_config)

    else:
        a.socket = (a.socket or default_socket()).expanduser()
        remote = socket_path_exists(a.socket)
        if a.direct and remote:
            raise ValueError("Daemon socket exists; do not bypass its owner")

        async def invoke(method: str, params: dict[str, Any] | None = None) -> Any:
            if remote:
                return await call(a.socket, method, params)
            assert app is not None
            return await app.call(method, params)

    if (
        not remote
        and a.connector is not None
        and not (a.direct or a.command == "serve")
    ):
        raise ValueError("--connector requires xbloom-studio serve or --direct")
    if not remote and (
        (a.command == "recipe" and a.action in PURE_ACTIONS)
        or (a.command == "call" and a.method in PURE_METHODS)
    ):
        method, params = route(a, _tracker)
        if _tracker is not None:
            _tracker.method = method
            _tracker.params = params
        fn, _ = PURE_METHODS[method]
        result = fn(**bind_parameters(fn, params))
        output(result)
        validation = method in ("recipe_validate", "recipe_tea_validate")
        if validation and not result["valid"]:
            _emit_human_result_diagnostic(
                a, result, "validation_error", "Recipe validation failed.", _tracker
            )
            return 2
        return 0
    if remote and a.command != "serve":
        if a.connector is not None:
            raise ValueError(
                "A running daemon owns the link; --connector applies to serve and "
                "--direct"
            )
        identity = await invoke("service_info")
        if not _same_device(a.device, identity["device"]):
            raise ValueError("Configured daemon targets a different device")
        if a.db and a.db.expanduser().resolve() != Path(identity["database"]):
            raise ValueError("Configured daemon uses a different database")
    if a.command != "serve":
        method, params = route(a, _tracker)
        if _tracker is not None:
            _tracker.method = method
            _tracker.params = params
        if not remote and method in RETAINED_CONNECTION_METHODS:
            raise ValueError(
                "Retained connections require a daemon; start xbloom-studio serve first"
            )
        if not remote and method == "operation_start" and not a.direct:
            raise ValueError(
                "Start xbloom-studio serve or pass --direct for machine operations"
            )
    store = None
    try:
        if not remote or a.command == "serve":
            store = Store(a.db)
            connector, options = (
                load_connector(a.connector, a.device)
                if a.connector is not None
                else (None, {})
            )
            app = Application(
                store,
                a.device,
                connector=connector,
                interaction_options=options,
                capture_idle_telemetry=getattr(a, "capture_idle_telemetry", False),
            )
        if a.command == "serve":
            assert app is not None
            return await serve(
                app, a.socket or default_socket(), a.recover_stale_socket
            )
        return await _execute(a, method, params, invoke, remote, store, app, _tracker)
    finally:
        try:
            if app is not None:
                await app.close()
        finally:
            if store is not None:
                store.close()


async def serve(app: Application, socket_path: Path, recover_stale: bool) -> int:
    server = LocalServer(app, socket_path, recover_stale=recover_stale)
    await server.start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    output({"serving": str(server.path), "connected": False})
    try:
        await stopped.wait()
    finally:
        await server.close()
    return 0


async def _execute(
    a: argparse.Namespace,
    method: str,
    params: dict[str, Any],
    invoke: Invoke,
    remote: bool,
    store: Store | None,
    app: Application | None,
    tracker: _SourceTracker | None = None,
) -> int:
    if not remote and method == "operation_start":
        assert store is not None and app is not None
        existing = await asyncio.to_thread(
            store.operation_by_key, params["idempotency_key"]
        )
        if existing is None:
            preview = await app.call("machine_prepare", {"request": params["request"]})
            if preview["requires_user"]:
                raise ValueError(
                    "Host preparation gates require a running daemon; start "
                    "xbloom-studio serve first"
                )
            supplied_digest = params.get("expected_recipe_sha256")
            digest = preview["recipe_sha256"]
            if supplied_digest is not None and supplied_digest != digest:
                raise ConflictError(
                    "Prepared recipe changed; preview it again before starting"
                )
            params["expected_recipe_sha256"] = digest
    result = await invoke(method, params)
    if a.command == "history" and a.action == "export":
        while result:
            for event in result:
                print(dumps(event))
            params["after"] = result[-1]["id"]
            result = await invoke(method, params)
        return 0
    started = method in ("operation_start", "recipe_start_uploaded")
    watching_cmd = a.command == "operation" and a.action == "watch"
    watch = (started and (not remote or getattr(a, "wait", False))) or watching_cmd
    if watching_cmd:
        expected_id = params["ident"]
        if not isinstance(result, Mapping) or result.get("id") != expected_id:
            _emit_watch_mismatch(a, expected_id, tracker)
            return 2
    output(result)
    if method in ("recipe_validate", "recipe_tea_validate"):
        if not result["valid"]:
            _emit_human_result_diagnostic(
                a,
                result,
                "validation_error",
                "Recipe validation failed.",
                tracker,
                remote=remote,
            )
            return 2
        return 0
    if method == "machine_command" and result.get("status") == "failed":
        _emit_human_result_diagnostic(
            a,
            result,
            "preset_write_incomplete",
            "Machine command reported failure.",
            tracker,
            remote=remote,
        )
        return 2
    if not watch:
        return 0
    if watching_cmd:
        ident = params["ident"]
    else:
        raw_ident = result.get("id") if isinstance(result, Mapping) else None
        if not isinstance(raw_ident, str) or not raw_ident:
            _emit_watch_mismatch(a, None, tracker)
            return 2
        ident = raw_ident
    last = None
    while result.get("status") in ACTIVE:
        if result["status"] == "waiting_for_user" and not remote:
            raise ValueError(
                "Host preparation gates require a daemon caller to acknowledge "
                "their exact tokens"
            )
        await asyncio.sleep(1 if a.ssh is not None else 0.1)
        polled = await invoke("history_get", {"ident": ident})
        if not isinstance(polled, Mapping) or polled.get("id") != ident:
            _emit_watch_mismatch(a, ident, tracker)
            return 2
        result = polled
        text = dumps(result)
        if text != last:
            output(result)
            last = text
    if result.get("status") in ("failed", "unknown"):
        _emit_human_operation_diagnostic(a, result, tracker, expected_id=ident)
    return 0 if result.get("status") == "completed" else 2


def _emit_cli_error(
    error_format: str,
    detail: Mapping[str, Any],
    context: DiagnosticContext | None,
    *,
    trusted_local: bool,
) -> None:
    if _effective_error_format(error_format) == "human":
        print(
            render_human_diagnostic(
                detail, context=context, trusted_local=trusted_local
            ),
            file=sys.stderr,
        )
    else:
        print(dumps({"ok": False, "error": detail}), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["relay"]:
        from .ssh_rpc import main as relay_main

        return relay_main(args[1:])
    p = parser(raise_on_error=True)
    try:
        parsed = p.parse_args(args)
    except _CliParseError as exc:
        error_format, detail, context = _parse_error_detail(exc, args, p)
        _emit_cli_error(error_format, detail, context, trusted_local=True)
        return 2
    tracker = _SourceTracker()
    try:
        return asyncio.run(run(parsed, _tracker=tracker))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        context = _command_context(parsed, tracker, exc=exc)
        detail = error_detail(exc, log_unexpected=False, context=context)
        _emit_cli_error(
            parsed.error_format,
            detail,
            context,
            trusted_local=not isinstance(exc, RemoteError),
        )
        return 2
