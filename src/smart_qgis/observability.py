"""Opt-in, bounded, best-effort telemetry. Never export raw tool inputs or outputs."""

import os
import queue
import threading
import uuid
from datetime import datetime, timedelta, timezone

ALLOWED_EVENTS = {
    "attempt_preconditions", "attempt_execution", "attempt_checkpoint",
    "attempt_validation", "attempt_persistence", "attempt_commit", "attempt_recovery",
    "attempt_final_validation",
    "task_begin",
    "inspect_data",
    "task_contract_submit",
    "step_contract_submit",
    "contract_help",
    "contract_get",
    "task_execute",
    "task_diagnose",
    "task_checkpoint",
    "task_validate",
    "task_recover",
    "task_invalidate",
    "task_revise_inputs",
    "task_finish",
    "project",
    "load_data",
    "add_basemap",
    "layers",
    "features",
    "style_vector",
    "style_raster",
    "algorithms",
    "run_processing",
    "layout",
    "export_map",
    "style_file",
    "vector_data",
    "render_raster",
    "style_graduated",
}


class TraceExporter:
    def __init__(self, client_factory=None):
        self.enabled = os.getenv("SMART_QGIS_LANGSMITH_TRACING", "").lower() == "true"
        self.pending = queue.Queue(maxsize=256)
        self.thread = None
        self.client_factory = client_factory
        if self.enabled:
            self.thread = threading.Thread(target=self._run, daemon=True, name="smart-qgis-traces")
            self.thread.start()

    def emit(self, event, *, task_id=None, step_id=None, attempt_id=None, success, duration_ms):
        if not self.enabled or event not in ALLOWED_EVENTS:
            return
        # Task IDs are generated UUIDs, not user-controlled step names or file paths.
        try:
            safe_task = uuid.UUID(task_id).hex if task_id else None
        except (ValueError, TypeError, AttributeError):
            safe_task = None
        record = {
            "event": event,
            "success": bool(success),
            "duration_ms": max(0, round(float(duration_ms), 3)),
            "task_id": safe_task,
        }
        if step_id is not None or attempt_id is not None:
            # Do not export user-selected step names. Correlate within a task using
            # a deterministic opaque identifier; raw names remain in local SQLite.
            record["step_key"] = (
                uuid.uuid5(uuid.UUID(safe_task), str(step_id)).hex
                if safe_task and step_id is not None else None
            )
            try:
                record["attempt_id"] = uuid.UUID(attempt_id).hex if attempt_id else None
            except (ValueError, TypeError, AttributeError):
                record["attempt_id"] = None
        try:
            self.pending.put_nowait(record)
        except queue.Full:
            pass

    def _run(self):
        try:
            if self.client_factory:
                client = self.client_factory()
            else:
                from langsmith import Client

                client = Client(auto_batch_tracing=False, timeout_ms=1000)
            while True:
                record = self.pending.get()
                try:
                    if record is None:
                        return
                    now = datetime.now(timezone.utc)
                    client.create_run(
                        name=record["event"],
                        inputs={},
                        outputs=record,
                        run_type="tool",
                        id=uuid.uuid4(),
                        start_time=now - timedelta(milliseconds=record["duration_ms"]),
                        end_time=now,
                        project_name=os.getenv("SMART_QGIS_LANGSMITH_PROJECT", "smart-qgis"),
                    )
                except Exception:
                    # Telemetry must never make task execution fail or disclose exception payloads.
                    pass
                finally:
                    self.pending.task_done()
        except Exception:
            return

    def close(self):
        if self.thread:
            try:
                self.pending.put_nowait(None)
            except queue.Full:
                pass
