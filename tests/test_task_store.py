"""Persistence tests use real SQLite, filesystem state and a second process."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from smart_qgis.coordinator import TaskCoordinator, explicitly_authorizes_output_overwrite
from smart_qgis.task_store import TaskError, TaskStore, fingerprint, sync_tree


@pytest.fixture
def store(tmp_path):
    instance = TaskStore.create(tmp_path, "Make a map", {}, [{"id": "map"}], {"qgis": "test"})
    yield instance
    instance.close()


def plan(store, step="load", dependencies=()):
    if store.task()["contract_version"] == 0:
        store.save_contract({"requirement": "map"}, "initial", store.task()["state_version"])
    store.save_step(
        step, {"operation": "load_data"}, list(dependencies), store.task()["state_version"]
    )


def start(store, step="load", key="request1"):
    return store.begin_attempt(
        step,
        key,
        {"operation": "load_data", "step": step},
        store.task()["contract_version"],
        store.task()["state_version"],
    )


def commit(store, step, dependencies=()):
    plan(store, step, dependencies)
    attempt = start(store, step, step)
    store.prepare_commit(attempt["attempt_id"], {"step": step}, {"project": step + ".qgz"})
    store.commit(attempt["attempt_id"])


def aged_task(root, status, age_days, *, now):
    item = TaskStore.create(root, status, {}, [{"id": "result"}], {})
    with item.transaction():
        item.db.execute("UPDATE task SET status=?", (status,))
        item.db.execute("UPDATE events SET created=?", (now - age_days * 86400,))
    task_id = item.task_id
    directory = item.directory
    item.close()
    return task_id, directory


def test_retention_removes_only_unlocked_expired_terminal_tasks(tmp_path):
    now = time.time()
    completed_id, completed = aged_task(tmp_path, "COMPLETED", 31, now=now)
    cancelled_id, cancelled = aged_task(tmp_path, "CANCELLED", 31, now=now)
    blocked_id, blocked = aged_task(tmp_path, "BLOCKED", 61, now=now)
    _, recent_blocked = aged_task(tmp_path, "BLOCKED", 59, now=now)
    _, active = aged_task(tmp_path, "READY", 31, now=now)
    _, recent = aged_task(tmp_path, "COMPLETED", 29, now=now)
    busy_id, busy_path = aged_task(tmp_path, "COMPLETED", 31, now=now)
    busy = TaskStore(tmp_path, busy_id)
    try:
        report = TaskStore.cleanup_expired(
            tmp_path, terminal_retention_days=30, failed_retention_days=60, now=now,
        )
        assert report["removed"] == sorted([completed_id, cancelled_id, blocked_id])
        assert report["skipped_busy"] == [busy_id]
        assert not completed.exists() and not cancelled.exists() and not blocked.exists()
        assert active.exists() and recent.exists() and recent_blocked.exists() and busy_path.exists()
    finally:
        busy.close()


def test_retention_zero_disables_cleanup_and_invalid_values_fail(tmp_path):
    now = time.time()
    _, directory = aged_task(tmp_path, "COMPLETED", 365, now=now)
    _, blocked = aged_task(tmp_path, "BLOCKED", 365, now=now)
    assert TaskStore.cleanup_expired(
        tmp_path, terminal_retention_days=0, failed_retention_days=0, now=now,
    )["removed"] == []
    assert directory.exists()
    assert blocked.exists()
    with pytest.raises(TaskError) as failure:
        TaskStore.cleanup_expired(tmp_path, terminal_retention_days=-1, now=now)
    assert failure.value.payload["code"] == "INVALID_CONFIG"
    with pytest.raises(TaskError) as failure:
        TaskStore.cleanup_expired(tmp_path, failed_retention_days=-1, now=now)
    assert failure.value.payload["code"] == "INVALID_CONFIG"


def test_declared_output_destination_is_preserved_and_existing_file_is_not_overwritten(tmp_path):
    destination = tmp_path / "desktop" / "result.tif"
    store = TaskStore.create(
        tmp_path / "state", "Output", {},
        [{"id": "result", "description": "Raster", "kind": "raster", "path": str(destination)}], {},
    )
    coordinator = TaskCoordinator.__new__(TaskCoordinator)
    coordinator.store = store
    output = SimpleNamespace(id="result", kind="raster", filename=None)
    try:
        assert coordinator.output_path(output, tmp_path / "temporary") == str(destination)
        assert destination.parent.is_dir()
        destination.write_bytes(b"existing")
        with pytest.raises(TaskError) as failure:
            coordinator.output_path(output, tmp_path / "temporary")
        assert failure.value.payload["code"] == "OUTPUT_EXISTS"
        assert failure.value.payload["evidence"]["question"]["kind"] == "output_conflict"
        assert failure.value.payload["evidence"]["decision_calls"]["overwrite"] == {
            "tool": "task_update",
            "arguments": {
                "task_id": store.task_id,
                "instruction": "The user explicitly authorized replacement of this exact file",
                "output_conflict": {"path": str(destination), "action": "overwrite"},
            },
        }
        assert "task_record_guidance" not in str(failure.value.payload)
        assert coordinator.output_conflict_resolution(destination) is None
        store.event("OUTPUT_CONFLICT_RESOLUTION", {"path": str(destination), "action": "overwrite"})
        assert coordinator.output_path(output, tmp_path / "temporary") == str(destination)
        assert not destination.exists()
    finally:
        store.close()


def test_task_start_overwrite_authorizes_only_declared_final_paths(tmp_path):
    exact = tmp_path / "desktop" / "result.tif"
    directory = tmp_path / "exports"
    store = TaskStore.create(
        tmp_path / "state", "Output", {},
        [
            {"id": "result", "description": "Raster", "kind": "raster", "path": str(exact)},
            {"id": "map", "description": "Map", "kind": "image", "directory": str(directory)},
            {"id": "temporary", "description": "Temporary", "kind": "pdf"},
        ], {},
    )
    coordinator = TaskCoordinator.__new__(TaskCoordinator)
    coordinator.store = store
    try:
        coordinator.authorize_declared_output_overwrites(
            store.task()["deliverables"], source="test"
        )
        assert coordinator.output_conflict_resolution(exact) == "overwrite"
        assert coordinator.output_conflict_resolution(directory / "map.png") == "overwrite"
        assert coordinator.output_conflict_resolution(tmp_path / "other.pdf") is None
    finally:
        store.close()


@pytest.mark.parametrize(
    ("instruction", "authorized"),
    [
        ("若有同名文件请直接覆盖", True),
        ("目标输出文件已存在时自动覆盖", True),
        ("目标文件有重复直接覆盖", True),
        ("overwrite existing output files", True),
        ("不要覆盖同名文件", False),
        ("ask me before overwrite", False),
        ("请生成新的输出文件", False),
    ],
)
def test_only_unambiguous_output_overwrite_instructions_are_authorized(instruction, authorized):
    assert explicitly_authorizes_output_overwrite(instruction) is authorized


def test_default_map_title_uses_first_sentence():
    assert TaskCoordinator.default_map_title(
        "Compute terrain ruggedness from a DEM. Then export the result map."
    ) == "Compute terrain ruggedness from a DEM."
    assert TaskCoordinator.default_map_title("基于 DEM 的崎岖度结果图") == "基于 DEM 的崎岖度结果图"


def test_undeclared_output_destination_uses_the_ephemeral_execution_directory(tmp_path):
    store = TaskStore.create(
        tmp_path / "state", "Output", {},
        [{"id": "result", "description": "Raster", "kind": "raster"}], {},
    )
    coordinator = TaskCoordinator.__new__(TaskCoordinator)
    coordinator.store = store
    temporary = tmp_path / "system-temporary" / "execution"
    try:
        output = SimpleNamespace(id="result", kind="raster", filename=None)
        assert coordinator.output_path(output, temporary) == str(temporary / "result.tif")
    finally:
        store.close()


def test_exclusive_task_ownership_across_processes(store):
    code = """
import sys
from smart_qgis.task_store import TaskStore, TaskError
try:
    store = TaskStore(sys.argv[1], sys.argv[2])
except TaskError as exc:
    print(exc.payload['code'])
else:
    store.close()
    raise SystemExit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.directory.parent), store.task_id],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "TASK_BUSY"
    task_id, root = store.task_id, store.directory.parent
    store.close()
    reopened = TaskStore(root, task_id)
    assert reopened.task()["goal"] == "Make a map"
    reopened.close()


def test_response_lost_committed_request_is_returned_before_stale_version_check(store):
    plan(store)
    old_version = store.task()["state_version"]
    attempt = start(store)
    store.prepare_commit(attempt["attempt_id"], {"asset": "dem"}, {"path": "project.qgz"})
    store.commit(attempt["attempt_id"])
    root, task_id = store.directory.parent, store.task_id
    store.close()
    reopened = TaskStore(root, task_id)
    try:
        cached = reopened.begin_attempt(
            "load", "request1", {"operation": "load_data", "step": "load"}, 1, old_version
        )
        assert cached == {"cached": True, "result": {"asset": "dem"}}
        assert reopened.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        with pytest.raises(TaskError, match="different request"):
            reopened.begin_attempt("load", "request1", {"different": True}, 1, old_version)
    finally:
        reopened.close()


def test_crash_after_prepare_preserves_unknown_result_for_revalidation(store):
    plan(store)
    attempt = start(store)
    store.prepare_commit(attempt["attempt_id"], {"output": "candidate"}, {"path": "candidate.qgz"})
    root, task_id = store.directory.parent, store.task_id
    store.close()
    reopened = TaskStore(root, task_id)
    try:
        assert reopened.task()["checkpoint"] is None
        assert reopened.task()["status"] == "RUNNING"
        pending = reopened.unresolved()
        assert pending[0]["status"] == "VALIDATING"
        assert json.loads(pending[0]["result"]) == {"output": "candidate"}
        with pytest.raises(TaskError, match="recover"):
            start(reopened)
        reopened.fail(attempt["attempt_id"], {"code": "ARTIFACT_MISSING"})
        assert reopened.task()["status"] == "BLOCKED"
        assert reopened.task()["checkpoint"] is None
    finally:
        reopened.close()


def test_real_process_exit_leaves_attempt_running_not_success(tmp_path):
    code = """
import os,sys
from smart_qgis.task_store import TaskStore
s=TaskStore.create(sys.argv[1], 'goal', {}, [{'id':'map'}], {})
s.save_contract({}, 'initial', 0)
s.save_step('load', {}, [], 1)
s.begin_attempt('load', 'request', {}, 1, 2)
print(s.task_id, flush=True)
os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 17, result.stderr
    reopened = TaskStore(tmp_path, result.stdout.strip())
    try:
        assert reopened.task()["status"] == "RUNNING"
        assert reopened.unresolved()[0]["status"] == "RUNNING"
        assert reopened.task()["checkpoint"] is None
    finally:
        reopened.close()


def test_invalidation_closure_preserves_independent_work_and_history(store):
    commit(store, "source")
    commit(store, "clip", ["source"])
    commit(store, "map", ["clip"])
    commit(store, "independent")
    assert store.invalidate(["clip"], "input changed") == ["clip", "map"]
    statuses = dict(store.db.execute("SELECT id,status FROM steps"))
    assert statuses == {
        "source": "COMMITTED",
        "clip": "INVALIDATED",
        "map": "INVALIDATED",
        "independent": "COMMITTED",
    }
    with pytest.raises(TaskError, match="no longer valid"):
        store.begin_attempt("map", "map", {"operation": "load_data", "step": "map"}, 1, 0)
    assert store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 4


def test_contract_revision_invalidates_results_and_requires_reason(store):
    commit(store, "load")
    current = store.task()["state_version"]
    with pytest.raises(TaskError, match="Explain"):
        store.save_contract({"new": True}, "", current)
    assert store.task()["state_version"] == current
    assert store.save_contract({"new": True}, "Add PDF requirement", current) == 2
    assert store.db.execute("SELECT status FROM steps").fetchone()[0] == "INVALIDATED"
    assert store.db.execute("SELECT count(*) FROM contracts").fetchone()[0] == 2


def test_stale_requests_and_cancellation_cannot_continue(store):
    plan(store)
    with pytest.raises(TaskError, match="changed"):
        store.begin_attempt("load", "request", {}, 1, 0)
    attempt = start(store)
    store.fail(attempt["attempt_id"], {"code": "CANCELLED"}, cancelled=True)
    assert store.task()["status"] == "CANCELLED"
    with pytest.raises(TaskError, match="not ready"):
        start(store, key="new")


def test_fingerprint_detects_same_size_same_timestamp_and_shapefile_sidecar(tmp_path):
    shp = tmp_path / "boundary.shp"
    dbf = tmp_path / "boundary.dbf"
    shp.write_bytes(b"geometry")
    dbf.write_bytes(b"old")
    first = fingerprint(shp)
    old_stat = dbf.stat()
    dbf.write_bytes(b"new")
    os.utime(dbf, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    second = fingerprint(shp)
    assert first["digest"] != second["digest"]
    assert len(second["files"]) == 2
    (tmp_path / "boundary.prj").write_text("new CRS")
    assert len(fingerprint(shp)["files"]) == 3


def test_symlink_artifacts_and_task_traversal_are_rejected(store, tmp_path):
    artifacts = tmp_path / "outputs"
    artifacts.mkdir()
    (artifacts / "escape").symlink_to(store.directory / "task.sqlite3")
    with pytest.raises(TaskError, match="symbolic"):
        sync_tree(artifacts)
    with pytest.raises(TaskError, match="Identifiers"):
        TaskStore(tmp_path, "../escape")


def test_raster_auxiliary_mask_and_georeference_changes_are_fingerprinted(tmp_path):
    raster = tmp_path / "dem.tif"
    raster.write_bytes(b"synthetic-primary-file")
    original = fingerprint(raster)
    mask = tmp_path / "dem.tif.msk"
    mask.write_bytes(b"mask-v1")
    with_mask = fingerprint(raster)
    assert original["digest"] != with_mask["digest"]
    before = mask.stat()
    mask.write_bytes(b"mask-v2")
    os.utime(mask, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert fingerprint(raster)["digest"] != with_mask["digest"]
    (tmp_path / "dem.tif.aux.xml").write_text("<PAMDataset/>")
    (tmp_path / "dem.tfw").write_text("1\n0\n0\n-1\n0\n0")
    assert len(fingerprint(raster)["files"]) == 4


def test_transaction_rolls_back_partial_changes(store):
    before = store.task()
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.db.execute("UPDATE task SET status='COMPLETED'")
            store.event("SHOULD_NOT_COMMIT", {})
            raise RuntimeError("simulated failure")
    assert store.task() == before
    assert (
        store.db.execute("SELECT count(*) FROM events WHERE kind='SHOULD_NOT_COMMIT'").fetchone()[0]
        == 0
    )


def test_vrt_recursively_fingerprints_sources_and_companions(tmp_path):
    raster = tmp_path / 'data.tif'
    raster.write_bytes(b'original')
    (tmp_path / 'data.tif.msk').write_bytes(b'mask')
    nested = tmp_path / 'nested.vrt'
    nested.write_text('<VRTDataset><SourceFilename relativeToVRT="1">data.tif</SourceFilename></VRTDataset>')
    root = tmp_path / 'root.vrt'
    root.write_text('<VRTDataset><SourceDataset relativeToVRT="1">nested.vrt</SourceDataset></VRTDataset>')
    before = fingerprint(root)
    assert len(before['files']) == 4
    timestamp = raster.stat()
    raster.write_bytes(b'modified')
    os.utime(raster, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
    assert fingerprint(root)['digest'] != before['digest']
    raster.unlink()
    with pytest.raises(TaskError, match='existing absolute'):
        fingerprint(root)


def test_vrt_cycles_and_nonlocal_dependencies_are_rejected(tmp_path):
    vrt = tmp_path / 'cycle.vrt'
    vrt.write_text('<VRTDataset><SourceFilename relativeToVRT="1">cycle.vrt</SourceFilename></VRTDataset>')
    with pytest.raises(TaskError) as error:
        fingerprint(vrt)
    assert error.value.payload['code'] == 'INPUT_DEPENDENCY_CYCLE'
    for source in ('https://example.invalid/data.tif', '/vsicurl/example', 'relative.tif'):
        vrt.write_text(f'<VRTDataset><SourceFilename>{source}</SourceFilename></VRTDataset>')
        with pytest.raises(TaskError) as error:
            fingerprint(vrt)
        assert error.value.payload['code'] == 'UNSUPPORTED_SOURCE'


@pytest.mark.parametrize("pending", [False, True])
def test_input_revision_rolls_back_with_checkpoint_on_journal_failure(store, monkeypatch, pending):
    commit(store, 'load')
    if pending:
        plan(store, 'pending', ['load'])
        start(store, 'pending', 'pending-key')
    before = store.task()
    original_event = store.event

    def unavailable(kind, body):
        if kind == 'INPUTS_REVISED':
            raise OSError('simulated journal failure')
        return original_event(kind, body)

    monkeypatch.setattr(store, 'event', unavailable)
    with pytest.raises(OSError, match='journal'):
        store.invalidate(['load'], 'Updated source', checkpoint={'project': 'new.qgz'},
                         revised_inputs={'source': {'fingerprint': {'digest': 'new'}}},
                         discard_uncommitted=pending)
    assert store.task() == before
    assert store.db.execute("SELECT status FROM steps WHERE id='load'").fetchone()[0] == 'COMMITTED'
    assert store.db.execute("SELECT count(*) FROM events WHERE kind='REPAIR_STARTED'").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM events WHERE kind='ATTEMPT_DISCARDED'").fetchone()[0] == 0
    if pending:
        assert store.unresolved()[0]['status'] == 'RUNNING'
        assert store.db.execute("SELECT status FROM steps WHERE id='pending'").fetchone()[0] == 'RUNNING'
