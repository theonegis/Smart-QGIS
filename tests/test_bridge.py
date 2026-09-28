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
 if request['operation']=='malformed':
  print('not-json',flush=True)
  continue
 if request['operation']=='empty':
  print(json.dumps({'id':request['id']}),flush=True)
  continue
 print(json.dumps({'id':request['id'],'result':request['arguments']}),flush=True)
"""
    )
    worker.chmod(0o755)
    monkeypatch.setattr(
        "smart_qgis.bridge.worker_environment", lambda: (str(worker), os.environ.copy())
    )


async def test_serialized_replies_and_graceful_shutdown(fake_runtime):
    # This tests ordering, not cold process startup latency under host load.
    # The separate timeout regression retains its short deadline.
    bridge = QgisBridge(10)
    results = await asyncio.gather(*(bridge.call("echo", {"value": i}) for i in range(8)))
    process = bridge.process
    assert results == [{"value": i} for i in range(8)]
    await bridge.close()
    assert process.returncode == 0


async def test_timeout_stops_worker_and_prevents_silent_state_reset(fake_runtime):
    bridge = QgisBridge(0.1)
    with pytest.raises(WorkerError, match="timed out") as timed_out:
        await bridge.call("hang", {})
    assert timed_out.value.code == "WORKER_TIMEOUT"
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


@pytest.mark.parametrize("operation", ["malformed", "empty"])
async def test_invalid_protocol_is_a_worker_failure_and_stops_process(fake_runtime, operation):
    bridge = QgisBridge(10)
    with pytest.raises(WorkerError) as failure:
        await bridge.call(operation, {})
    assert failure.value.code is None
    assert bridge.broken and bridge.process is None


async def test_broken_pipe_is_not_reported_as_a_filesystem_failure(fake_runtime, monkeypatch):
    bridge = QgisBridge(10)
    await bridge.call("echo", {})
    process = bridge.process

    def broken_write(payload):
        raise BrokenPipeError("simulated closed worker pipe")

    monkeypatch.setattr(process.stdin, "write", broken_write)
    with pytest.raises(WorkerError, match="communication failed") as failure:
        await bridge.call("echo", {})
    assert failure.value.code is None
    assert process.returncode is not None
    assert bridge.broken and bridge.process is None


async def test_invalid_json_arguments_do_not_destroy_running_worker(fake_runtime):
    bridge = QgisBridge(10)
    try:
        await bridge.call("echo", {})
        process = bridge.process
        with pytest.raises(WorkerError) as failure:
            await bridge.call("echo", {"value": float("nan")})
        assert failure.value.code == "INVALID_PARAMETERS"
        assert bridge.process is process and not bridge.broken
        assert await bridge.call("echo", {"value": 1}) == {"value": 1}
    finally:
        await bridge.close()
