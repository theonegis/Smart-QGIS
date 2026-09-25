"""One serialized, main-thread QGIS worker per MCP process."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path

from .runtime import worker_environment


class WorkerError(RuntimeError):
    pass


class QgisBridge:
    def __init__(self, timeout: float = 600):
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.process = None
        self.sequence = 0
        self.broken = False

    async def start(self):
        if self.broken:
            raise WorkerError(
                "Worker stopped; unsaved state was lost. Restart the MCP server and reopen your saved project."
            )
        if self.process is None:
            executable, env = worker_environment()
            self.process = await asyncio.create_subprocess_exec(
                executable,
                str(Path(__file__).with_name("worker.py")),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
                limit=16 * 1024 * 1024,
                start_new_session=True,
            )

    async def call(self, operation: str, arguments: dict):
        async with self.lock:
            await self.start()
            self.sequence += 1
            request = {"id": self.sequence, "operation": operation, "arguments": arguments}
            try:
                self.process.stdin.write((json.dumps(request, allow_nan=False) + "\n").encode())
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), self.timeout)
                if not line:
                    raise WorkerError("QGIS worker exited unexpectedly; inspect server stderr")
                response = json.loads(line)
                if response.get("id") != self.sequence:
                    raise WorkerError("QGIS worker response sequence mismatch")
            except BaseException as exc:
                self.broken = True
                await self.close(abort=True)
                if isinstance(exc, TimeoutError):
                    raise WorkerError(
                        "QGIS operation timed out; worker stopped and unsaved state was lost"
                    ) from exc
                raise
            if "error" in response:
                raise WorkerError(response["error"])
            return response["result"]

    async def close(self, abort=False):
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            if not abort:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                    return
                except TimeoutError:
                    pass
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
