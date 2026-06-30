"""Async entrypoint: launches scanner + REPL tasks, handles shutdown."""

import asyncio
import signal
import logging

from typing import Any

from .state import ctx
from .ble.scanner import scanner_loop
from .cli.repl import repl


def _install_quiet_loop_handler(loop: asyncio.AbstractEventLoop) -> None:
    """
        Replace the default asyncio exception handler so spurious
        callback errors don't break the REPL with 'Press ENTER' prompts.
    """

    def handler(_loop, context) -> Any:
        msg = context.get("exception") or context.get("message")
        logging.warning(f"[asyncio] {msg}")

    loop.set_exception_handler(handler)


async def main() -> Any:
    """
        Run the scanner and REPL concurrently until shutdown.
    """

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    _install_quiet_loop_handler(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    scan_task = asyncio.create_task(scanner_loop(stop))
    repl_task = asyncio.create_task(repl(stop))

    await stop.wait()

    if ctx.client and ctx.client.is_connected:
        try:
            await ctx.client.disconnect()

        # pylint: disable=broad-exception-caught
        except Exception:
            pass

    if ctx.scanner:
        try:
            await ctx.scanner.stop()

        # pylint: disable=broad-exception-caught
        except Exception:
            pass

    await asyncio.gather(scan_task, repl_task, return_exceptions=True)
    ctx.save()
