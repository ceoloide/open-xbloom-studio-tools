"""Internal cancellation barrier for owned teardown tasks."""

from __future__ import annotations

import asyncio


async def wait_cleanup[T](cleanup: asyncio.Future[T]) -> T:
    """Wait for ``cleanup`` to finish even if the caller is cancelled meanwhile.

    Teardown owns its session until it finishes, even if its RPC caller
    leaves; in particular ``close()`` must not release a lease early. A
    cancellation that arrived while waiting is re-raised afterwards.
    """
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    result = cleanup.result()  # Also propagates a cancelled cleanup.
    if cancelled:
        raise asyncio.CancelledError()
    return result
