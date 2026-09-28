import threading

import pytest

from smart_qgis.observability import TraceExporter


def test_local_timing_write_failure_preserves_execution_outcome(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    from smart_qgis.coordinator import TaskCoordinator

    monkeypatch.delenv("SMART_QGIS_LANGSMITH_TRACING", raising=False)
    coordinator = TaskCoordinator(root=tmp_path)

    def disk_full(kind, body):
        assert kind == "PHASE_TIMING"
        raise sqlite3.OperationalError("database or disk is full")

    coordinator.store = SimpleNamespace(event=disk_full, task_id="0" * 32)
    with coordinator.trace_attempt("commit", "step", "1" * 32):
        pass
    with pytest.raises(ValueError, match="original execution error"):
        with coordinator.trace_attempt("validation", "step", "1" * 32):
            raise ValueError("original execution error")


def test_ambient_langsmith_tracing_does_not_enable_export(monkeypatch):
    monkeypatch.delenv("SMART_QGIS_LANGSMITH_TRACING", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    exporter = TraceExporter(lambda: (_ for _ in ()).throw(AssertionError("must not construct")))
    exporter.emit("task_begin", success=True, duration_ms=1)
    assert exporter.thread is None
    assert exporter.pending.empty()


def test_allowlisted_payload_and_network_errors_do_not_affect_caller(monkeypatch):
    monkeypatch.setenv("SMART_QGIS_LANGSMITH_TRACING", "true")
    received, completed = [], threading.Event()

    class FailingClient:
        def create_run(self, **kwargs):
            received.append(kwargs)
            completed.set()
            raise ConnectionError("sensitive remote response must not be logged")

    exporter = TraceExporter(FailingClient)
    try:
        exporter.emit("/private/file.tif", success=True, duration_ms=1)
        exporter.emit("task_begin", task_id="secret-token-or-path", success=False, duration_ms=2)
        assert completed.wait(2)
        assert len(received) == 1
        assert received[0]["inputs"] == {}
        assert received[0]["outputs"] == {
            "event": "task_begin",
            "success": False,
            "duration_ms": 2.0,
            "task_id": None,
        }
        assert "secret" not in str(received)
    finally:
        exporter.close()


def test_phase_correlation_does_not_export_step_names(monkeypatch):
    import uuid

    monkeypatch.setenv('SMART_QGIS_LANGSMITH_TRACING', 'true')
    received, completed = [], threading.Event()

    class Client:
        def create_run(self, **kwargs):
            received.append(kwargs)
            if len(received) == 3:
                completed.set()

    exporter = TraceExporter(Client)
    task, attempt = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        for event in ('attempt_execution', 'attempt_validation', 'attempt_recovery'):
            exporter.emit(event, task_id=task, step_id='sensitive-test-marker',
                          attempt_id=attempt, success=True, duration_ms=123)
        assert completed.wait(2)
        outputs = [run['outputs'] for run in received]
        assert len({output['step_key'] for output in outputs}) == 1
        assert all(output['attempt_id'] == attempt for output in outputs)
        assert all(output['task_id'] == task for output in outputs)
        assert 'sensitive-test-marker' not in str(received)
        assert all((run['end_time'] - run['start_time']).total_seconds() == .123
                   for run in received)
    finally:
        exporter.close()
