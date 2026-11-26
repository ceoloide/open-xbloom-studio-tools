"""Authenticated SSH carrier and restricted stdio relay for the local Unix RPC.

The operator owns SSH configuration and keys. :func:`ssh_call` runs the
system ``ssh`` client non-interactively with strict host-key checking and no
forwarding, and sends one request to ``xbloom-studio relay`` on the remote
host, typically pinned as a forced command. :func:`relay` forwards one request
to a fixed local socket and exposes neither a shell, the link nor the database.

The remote policy is the fixed :data:`REMOTE_METHODS` allowlist, maintained
independently of the methods an application offers locally: a new local
method is never remote by default, and filesystem backup and provider
publishing stay local.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import signal
import sys
from collections.abc import Sequence
from typing import Any

from .diagnostics import request_context, sanitize_remote_result
from .errors import error_detail
from .rpc import call
from .rpc_types import RemoteError
from .rpc_wire import (
    MAX_REQUEST,
    MAX_RESPONSE,
    default_timeout,
    frame,
    request,
    response,
)

DEFAULT_RELAY_SOCKET = "/run/xbloom-studio/daemon.sock"
REMOTE_COMMAND = ("xbloom-studio", "relay")
REMOTE_METHODS = frozenset(
    {
        "service_info",
        "recipe_create",
        "recipe_get",
        "recipe_validate",
        "recipe_constraints",
        "recipe_templates",
        "recipe_list",
        "recipe_page",
        "recipe_update",
        "recipe_archive",
        "recipe_alias",
        "recipe_compile",
        "recipe_compare",
        "recipe_match",
        "recipe_import",
        "recipe_import_export",
        "recipe_examples",
        "recipe_tea_references",
        "recipe_tea_validate",
        "recipe_preview",
        "recipe_import_get",
        "recipe_refresh",
        "provider_search",
        "provider_capabilities",
        "history_list",
        "history_get",
        "history_page",
        "history_event_page",
        "history_events",
        "history_feedback",
        "history_save_recipe",
        "machine_capabilities",
        "machine_command",
        "recipe_upload",
        "recipe_start_uploaded",
        "recipe_cancel_uploaded",
        "machine_state",
        "machine_preset_writes",
        "machine_prepare",
        "machine_connect",
        "machine_disconnect",
        "operation_lookup",
        "operation_start",
        "operation_stop",
        "operation_pause_resume",
        "operation_acknowledge",
    }
)
UNCERTAIN = (
    "the request may have been submitted. Inspect operation_lookup/history; do not "
    "automatically retry a start"
)


def validate_host(host: object) -> str:
    if not isinstance(host, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.@:%-]{0,254}", host
    ):
        raise ValueError(
            "SSH target must be a host or SSH configuration alias, never options or "
            "a shell command"
        )
    return host


async def _bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks = bytearray()
    while True:
        part = await stream.read(min(65536, limit - len(chunks) + 1))
        if not part:
            return bytes(chunks)
        chunks.extend(part)
        if len(chunks) > limit:
            raise ConnectionError("SSH RPC output exceeds its limit")


async def _reap(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        pass
    # A leader can exit while descendants keep its pipes; always retire its group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await asyncio.wait_for(process.wait(), 2)


async def ssh_call(
    host: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    config_file: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
) -> Any:
    """Send one request through SSH. Never retried: delivery may be unknown."""
    validate_host(host)
    if timeout is None:
        timeout = default_timeout(method, params)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("SSH timeout must be a positive finite number")
    raw = frame(
        {"method": method, "params": {} if params is None else params}, MAX_REQUEST
    )
    request(raw)  # Fail before process creation for malformed requests.
    argv = [
        "ssh",
        "-T",
        "-oBatchMode=yes",
        "-oStrictHostKeyChecking=yes",
        "-oConnectTimeout=10",
        "-oClearAllForwardings=yes",
        "-oForwardAgent=no",
        "-oControlMaster=no",
        "-oControlPath=none",
    ]
    if config_file is not None:
        argv += ["-F", os.fspath(config_file)]
    argv += ["--", host, *REMOTE_COMMAND]
    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task[bytes]] = []

    async def exchange() -> Any:
        nonlocal process
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ConnectionError(
                "Unable to launch SSH; no RPC request was submitted"
            ) from exc
        assert process.stdin and process.stdout and process.stderr
        stdout = asyncio.create_task(_bounded(process.stdout, MAX_RESPONSE))
        stderr = asyncio.create_task(_bounded(process.stderr, 65536))
        tasks.extend((stdout, stderr))
        try:
            process.stdin.write(raw)
            await process.stdin.drain()
            process.stdin.close()
            output, _ = await asyncio.gather(stdout, stderr)
            code = await process.wait()
            if code != 0:
                raise ConnectionError("SSH RPC failed; " + UNCERTAIN)
            try:
                result = response(output)
            except ValueError as exc:
                raise ConnectionError(
                    "Invalid or incomplete SSH RPC response; inspect "
                    "operation_lookup/history before any new start"
                ) from exc
            req_ctx = request_context(method, params)
            if not result["ok"]:
                raise RemoteError(
                    error_detail(
                        RemoteError(result["error"]),
                        context=req_ctx,
                    )
                )
            return sanitize_remote_result(method, result["data"], context=req_ctx)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ConnectionError(
                "SSH RPC connection closed; inspect operation_lookup/history before "
                "any new start"
            ) from exc

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except TimeoutError as exc:
        raise ConnectionError("SSH RPC timed out; " + UNCERTAIN) from exc
    finally:
        try:
            if process is not None:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                finally:
                    await _reap(process)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


async def relay(socket_path: str | os.PathLike[str]) -> int:
    """Relay one request/response on stdio; the forced command fixes the socket."""
    transport = None
    method: str | None = None
    req_params: dict[str, Any] | None = None
    try:
        reader = asyncio.StreamReader(limit=MAX_REQUEST)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol, sys.stdin.buffer
        )
        raw = await asyncio.wait_for(_bounded(reader, MAX_REQUEST), 10)
        envelope = request(raw)
        method = envelope["method"]
        raw_params = envelope.get("params")
        req_params = raw_params if isinstance(raw_params, dict) else None
        if method not in REMOTE_METHODS:
            raise ValueError(
                "Method is not available through the SSH endpoint; backups require a "
                "local operator"
            )
        result = await call(socket_path, method, req_params)
        reply = {"ok": True, "data": result}
    except Exception as exc:
        context = request_context(method, req_params) if method is not None else None
        reply = {"ok": False, "error": error_detail(exc, context=context)}
    finally:
        if transport is not None:
            transport.close()
    try:
        encoded = frame(reply, MAX_RESPONSE)
    except ValueError:
        context = request_context(method, req_params) if method is not None else None
        encoded = frame(
            {
                "ok": False,
                "error": error_detail(
                    ConnectionError(
                        "RPC response exceeds its limit; inspect history with "
                        "pagination"
                    ),
                    context=context,
                ),
            },
            MAX_RESPONSE,
        )
    try:
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``xbloom-studio relay``: one restricted request on stdio, then exit."""
    parser = argparse.ArgumentParser(
        prog="xbloom-studio relay",
        description="Restricted one-shot xbloom-studio RPC stdio relay",
    )
    parser.add_argument("--socket", default=DEFAULT_RELAY_SOCKET)
    args = parser.parse_args(argv)
    return asyncio.run(relay(args.socket))
