"""Process-level regression for serialization, timeout and cancellation."""

import asyncio
import os
import sys

import pytest

from smart_qgis.bridge import QgisBridge, WorkerError


@pytest.fixture
def fake_runtime(tmp_path, monkeypatch):
    worker = tmp_path / "worker-python"
    worker.write_text(
        f"#!{sys.executable}\n"
        + """import sys,json,time
for line in sys.stdin:
 request=json.loads(line)
 if request['operation']=='hang': time.sleep(60)
 print(json.dumps({'id':request['id'],'result':request['arguments']}),flush=True)
"""
    )
    worker.chmod(0o755)
    monkeypatch.setattr(
        "smart_qgis.bridge.worker_environment", lambda: (str(worker), os.environ.copy())
    )


async def test_serialized_replies_and_graceful_shutdown(fake_runtime):
    bridge = QgisBridge(2)
    results = await asyncio.gather(*(bridge.call("echo", {"value": i}) for i in range(8)))
    process = bridge.process
    assert results == [{"value": i} for i in range(8)]
    await bridge.close()
    assert process.returncode == 0


async def test_timeout_stops_worker_and_prevents_silent_state_reset(fake_runtime):
    bridge = QgisBridge(0.1)
    with pytest.raises(WorkerError, match="timed out"):
        await bridge.call("hang", {})
    assert bridge.process is None
    with pytest.raises(WorkerError, match="Restart the MCP server"):
        await bridge.call("echo", {})


async def test_cancellation_terminates_worker(fake_runtime):
    bridge = QgisBridge(10)
    task = asyncio.create_task(bridge.call("hang", {}))
    await asyncio.sleep(0.1)
    process = bridge.process
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    assert bridge.broken
