"""Private local RPC over a Unix socket. No network listener, no dynamic access.

Each connection carries one bounded JSON line each way. The server holds the
application's database lease for its whole life, accepts only a private
same-user socket directory, and never removes a socket it did not bind
except through an explicit, checked stale-socket recovery.
"""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._cleanup import wait_cleanup
from .diagnostics import (
    build_diagnostic,
    request_context,
    sanitize_remote_result,
)
from .errors import error_detail
from .rpc_types import RemoteError, SocketDirectoryError, SocketPathError
from .rpc_wire import MAX_REQUEST, MAX_RESPONSE, frame
from .rpc_wire import request as decode_request
from .rpc_wire import response as decode_response
from .serialization import dumps

if TYPE_CHECKING:
    from .facade import Application

__all__ = ["LocalServer", "RemoteError", "call", "default_socket"]

START_METHODS = ("operation_start", "recipe_start_uploaded")


def default_socket() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "xbloom-studio/daemon.sock"
    return Path.home() / ".local/state/xbloom-studio/run/daemon.sock"


@contextmanager
def _socket_path_errors() -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG or str(exc) == "AF_UNIX path too long":
            raise SocketPathError("Unix socket path is too long") from exc
        raise


def socket_path_exists(path: Path, *, follow_symlinks: bool = True) -> bool:
    """Check occupancy while retaining actionable filesystem path errors."""
    with _socket_path_errors():
        try:
            os.stat(path, follow_symlinks=follow_symlinks)
        except FileNotFoundError, NotADirectoryError:
            return False
    return True


async def call(
    socket_path: str | os.PathLike[str],
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = 30,
) -> Any:
    """Send one request to a local daemon. Never retried: delivery may be unknown."""
    payload = frame(
        {"method": method, "params": {} if params is None else params},
        MAX_REQUEST,
        "Request too large",
    )
    decode_request(payload, MAX_REQUEST)

    async def exchange() -> Any:
        with _socket_path_errors():
            reader, writer = await asyncio.open_unix_connection(
                str(Path(socket_path).expanduser()), limit=MAX_RESPONSE
            )
        try:
            writer.write(payload)
            await writer.drain()
            try:
                response = decode_response(await reader.readline(), MAX_RESPONSE)
            except ValueError as exc:
                raise ConnectionError(
                    "Invalid or incomplete local RPC response"
                ) from exc
            req_ctx = request_context(method, params)
            if not response["ok"]:
                raise RemoteError(
                    error_detail(
                        RemoteError(response["error"]),
                        context=req_ctx,
                    )
                )
            return sanitize_remote_result(method, response["data"], context=req_ctx)
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except TimeoutError as exc:
        raise ConnectionError(
            "Local RPC timed out; inspect operation_lookup with the original key "
            "before any new start"
        ) from exc


class LocalServer:
    def __init__(
        self,
        app: Application,
        socket_path: str | os.PathLike[str] | None = None,
        *,
        recover_stale: bool = False,
    ) -> None:
        if type(recover_stale) is not bool:
            raise ValueError("recover_stale must be a boolean")
        self.app = app
        self.path = Path(socket_path or default_socket()).expanduser().absolute()
        self.server: asyncio.base_events.Server | None = None
        self.handlers: set[asyncio.Task[Any]] = set()
        self.recover_stale = recover_stale
        self.socket_identity: tuple[int, int] | None = None
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> LocalServer:
        with _socket_path_errors():
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            mode = self.path.parent.lstat()
        if (
            not stat.S_ISDIR(mode.st_mode)
            or mode.st_uid != os.getuid()
            or stat.S_IMODE(mode.st_mode) & 0o077
        ):
            raise SocketDirectoryError(
                "Socket directory must be owned by this user and mode 0700"
            )
        occupied = socket_path_exists(self.path, follow_symlinks=False)
        if occupied and not self.recover_stale:
            raise FileExistsError(
                "Socket already exists; use explicit stale recovery after "
                "inspecting its owner"
            )
        await self.app.machine._own()
        listener = None
        try:
            if occupied:
                await self._recover_stale()
            # asyncio's path-based binder may unlink an existing socket. Bind
            # directly so a competing listener is preserved with EADDRINUSE.
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.setblocking(False)
            with _socket_path_errors():
                listener.bind(str(self.path))
            bound = self.path.lstat()
            self.socket_identity = (bound.st_dev, bound.st_ino)
            self.server = await asyncio.start_unix_server(
                self.handle, sock=listener, limit=MAX_REQUEST
            )
            self.path.chmod(0o600)
        except BaseException:
            await wait_cleanup(asyncio.create_task(self._failed_start(listener)))
            raise
        return self

    async def _failed_start(self, listener: socket.socket | None) -> None:
        server = self.server
        try:
            if server:
                server.close()
        finally:
            try:
                if listener is not None:
                    listener.close()
            finally:
                await self._finish_close(server)

    async def _recover_stale(self) -> None:
        """Only remove an unchanged, same-user socket after a refused connection.

        Called while the application database lease is held. An active
        listener, timeout, permission error or replaced path is never assumed
        stale.
        """
        before = self.path.lstat()
        if (
            before.st_uid != os.getuid()
            or not stat.S_ISSOCK(before.st_mode)
            or before.st_nlink != 1
        ):
            raise FileExistsError(
                "Stale recovery requires a same-user plain socket, never a symlink "
                "or regular file"
            )
        try:
            with _socket_path_errors():
                _, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(self.path)), 1
                )
        except ConnectionRefusedError:
            pass
        else:
            writer.close()
            await writer.wait_closed()
            raise FileExistsError("Socket has an active listener; refusing recovery")
        after = self.path.lstat()
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or not stat.S_ISSOCK(after.st_mode)
            or after.st_uid != os.getuid()
            or after.st_nlink != 1
        ):
            raise FileExistsError("Socket changed during recovery; refusing removal")
        self.path.unlink()

    def _unlink_bound_socket(self) -> None:
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(current.st_mode)
            and current.st_uid == os.getuid()
            and (current.st_dev, current.st_ino) == self.socket_identity
        ):
            self.path.unlink()
        self.socket_identity = None

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        try:
            request: dict[str, Any] = {}
            result: Any = None
            try:
                raw = await asyncio.wait_for(reader.readline(), 10)
                if len(raw) > MAX_REQUEST:
                    raise ValueError("Request too large")
                request = decode_request(raw, MAX_REQUEST)
                result = await self.app.call(request["method"], request.get("params"))
                response = {"ok": True, "data": result}
            except Exception as exc:
                method = request.get("method")
                raw_params = request.get("params")
                context = (
                    request_context(
                        method, raw_params if isinstance(raw_params, dict) else None
                    )
                    if isinstance(method, str) and method.strip()
                    else None
                )
                response = {"ok": False, "error": error_detail(exc, context=context)}
            encoded = dumps(response).encode() + b"\n"
            if len(encoded) > MAX_RESPONSE:
                encoded = self._too_large(request, result, response["ok"])
            writer.write(encoded)
            await asyncio.wait_for(writer.drain(), 5)
        except ConnectionError, TimeoutError:
            pass
        finally:
            try:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
            finally:
                self.handlers.discard(task)

    @staticmethod
    def _too_large(request: dict[str, Any], result: Any, ok: bool) -> bytes:
        method = request.get("method")
        method_name = method if isinstance(method, str) and method.strip() else None
        location: dict[str, Any] = (
            {"kind": "method", "method": method_name} if method_name else {}
        )
        detail = error_detail(ValueError("Response too large; use cursor pagination"))
        detail["code"] = "response_too_large"
        if (
            ok
            and method_name in START_METHODS
            and isinstance(result, dict)
            and isinstance(result.get("id"), str)
        ):
            # An accepted start must stay identifiable: never invite a new key.
            detail["details"] = {"operation_id": result["id"]}
            location["operation_id"] = result["id"]
            key = (request.get("params") or {}).get("idempotency_key")
            if isinstance(key, str) and len(key) <= 512:
                detail["details"]["idempotency_key"] = key
            detail["next_step"] = (
                "The operation was accepted. Inspect history_get using operation_id "
                "or replay the same idempotency key to retrieve it. Never start "
                "again with a new key."
            )
        detail["diagnostic"] = build_diagnostic(
            "XB_RESPONSE_TOO_LARGE",
            cause=detail["message"],
            fix=detail["next_step"],
            location=location or None,
        )
        return dumps({"ok": False, "error": detail}).encode() + b"\n"

    async def close(self) -> None:
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close())
        await wait_cleanup(self._close_task)

    async def _close(self) -> None:
        if self.server:
            server = self.server
            try:
                server.close()
            finally:
                await self._finish_close(server)

    async def _finish_close(self, server: asyncio.base_events.Server | None) -> None:
        try:
            try:
                await self.app.close()
            finally:
                await self._drain_handlers()
        finally:
            try:
                if server:
                    await server.wait_closed()
            finally:
                try:
                    self._unlink_bound_socket()
                finally:
                    self.server = None

    async def _drain_handlers(self) -> None:
        if self.handlers:
            tasks = list(self.handlers)
            try:
                await asyncio.wait(tasks, timeout=5)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
