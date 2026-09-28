import json
from unittest.mock import AsyncMock

import pytest

from smart_qgis.bridge import WorkerError
from smart_qgis.contracts import StepContract
from smart_qgis.coordinator import TaskCoordinator
from smart_qgis.task_store import TaskError, TaskStore
from smart_qgis.task_tools import TaskClarify


@pytest.fixture
def coordinator(tmp_path):
    co = TaskCoordinator(root=tmp_path)
    co.store = TaskStore.create(tmp_path, "Render DEM", {}, [{"id": "map"}], {})
    co.store.save_contract({}, "initial", 0)
    yield co
    co.store.close()
    co.traces.close()


def plan(classes=5, repairs_step=None, checks=None):
    return StepContract(
        operation="style_raster",
        arguments={"layer": "asset:dem", "classes": classes},
        inputs=["dem"],
        reason="Fix style",
        repairs_step=repairs_step,
        postconditions=checks or [],
    )


def fail(co, name, step):
    store = co.store
    store.save_step(name, step.model_dump(), [], store.task()["state_version"])
    attempt = store.begin_attempt(name, name, step.arguments, 1, store.task()["state_version"])
    store.fail(attempt["attempt_id"], {"code": "VALIDATION_FAILED"})


async def test_user_guidance_can_revise_map_layers_without_replacing_task(coordinator):
    with coordinator.store.transaction():
        coordinator.store.event("TASK_PRESENTATION", {
            "title": "Risk map", "raster_ramp": "Viridis", "dpi": 150,
            "layers": None,
        })
    task_id = coordinator.store.task_id
    await coordinator.call("task_record_guidance", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "question": "Which layers should the map show?",
        "user_response": "Show the selected areas above risk zones.",
        "map_layers": ["undeveloped_areas", "risk_zones"],
    })
    assert coordinator.store.task_id == task_id
    assert coordinator.presentation_options() == {
        "title": "Risk map", "raster_ramp": "Viridis", "dpi": 150,
        "layers": ["undeveloped_areas", "risk_zones"],
    }
    await coordinator.call("task_record_guidance", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "question": "Where should the legend and scale bar go?",
        "user_response": "Outside the map frame; the data needs the available area.",
        "map_element_placement": "outside",
    })
    assert coordinator.presentation_options()["map_element_placement"] == "outside"
    assert coordinator.presentation_options()["layers"] == ["undeveloped_areas", "risk_zones"]
    await coordinator.call("task_record_guidance", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "question": "How should the sparse area layer be shown?",
        "user_response": "Gray category over the continuous risk map.",
        "map_raster_styles": {
            "undeveloped_areas": {"mode": "mask", "color": "#666666", "opacity": 0.5},
            "risk_zones": {"mode": "continuous", "ramp": "Viridis", "opacity": 0.9},
        },
    })
    assert coordinator.presentation_options()["raster_styles"]["undeveloped_areas"]["mode"] == "mask"
    assert coordinator.presentation_options()["layers"] == ["undeveloped_areas", "risk_zones"]
    with pytest.raises(ValueError, match="duplicates"):
        TaskClarify.model_validate({
            "question": "Which layers?", "user_response": "Both",
            "map_layers": ["risk_zones", "risk_zones"],
        })


@pytest.mark.parametrize("limit", [2, 3, 5])
async def test_new_id_cannot_bypass_failed_request_or_repair_budget(coordinator, limit):
    coordinator.correction_limit = limit
    fail(coordinator, "original", plan())
    with pytest.raises(TaskError, match="repairs_step"):
        coordinator.check_repair(plan(6))
    with pytest.raises(TaskError, match="unchanged"):
        coordinator.check_repair(plan(5, "original"))
    # Branching all attempts from the root still consumes one shared budget.
    for number in range(limit):
        revised = plan(6 + number, "original")
        coordinator.check_repair(revised)
        fail(coordinator, f"repair{number}", revised)
    with pytest.raises(TaskError) as blocked:
        coordinator.check_repair(plan(20, "original"))
    assert blocked.value.payload["code"] == "CLARIFICATION_REQUIRED"
    assert blocked.value.payload["evidence"] == {"repairs": limit, "limit": limit}
    await coordinator.clarify({
        "task_id": coordinator.store.task_id,
        "expected_state_version": coordinator.store.task()["state_version"],
        "question": "How should the failed style step be corrected?",
        "user_response": "Use a different class count and continue the same task.",
    })
    coordinator.check_repair(plan(20, "original"))


async def test_layout_cannot_drop_default_map_elements_without_explicit_omission(coordinator):
    step = StepContract(
        operation="layout",
        reason="Create a map",
        inputs=["dem"],
        arguments={
            "action": "create",
            "name": "Map",
            "layers": ["asset:dem"],
            "legend": False,
        },
        outputs=[{"id": "layout", "kind": "layout", "binding": "layout"}],
    )
    with pytest.raises(TaskError) as failure:
        await coordinator.check_operation(step)
    assert failure.value.payload["code"] == "MAP_ELEMENTS_REQUIRED"
    assert failure.value.payload["evidence"] == {"elements": ["legend"]}


async def test_explicit_map_omission_must_be_applied(tmp_path):
    coordinator = TaskCoordinator(root=tmp_path)
    coordinator.store = TaskStore.create(
        tmp_path, "Map without legend", {}, [{"id": "map"}], {}
    )
    coordinator.store.save_contract(
        {"map_omissions": ["legend"]}, "initial", 0
    )
    try:
        step = StepContract(
            operation="layout",
            reason="Create a map without the omitted legend",
            inputs=["dem"],
            arguments={"action": "create", "name": "Map", "layers": ["asset:dem"]},
            outputs=[{"id": "layout", "kind": "layout", "binding": "layout"}],
        )
        with pytest.raises(TaskError) as failure:
            await coordinator.check_operation(step)
        assert failure.value.payload["code"] == "MAP_OMISSION_REQUIRED"
        step.arguments["legend"] = False
        assert await coordinator.check_operation(step) == {}
    finally:
        coordinator.store.close()
        coordinator.traces.close()


def test_repair_cannot_drop_required_step_checks(coordinator):
    check = {
        "id": "crs",
        "kind": "crs",
        "target": "dem",
        "expected": "EPSG:3857",
        "source": "user_requirement",
        "basis": "metric CRS",
        "evidence": ["goal"],
    }
    fail(coordinator, "original", plan(checks=[check]))
    with pytest.raises(TaskError, match="retain required") as missing:
        coordinator.check_repair(plan(6, "original"))
    assert missing.value.payload["evidence"] == {
        "original_step": "original", "checks": [{"id": "crs", "phase": "postconditions"}],
    }
    coordinator.check_repair(plan(6, "original", [check]))


def test_failed_style_can_be_retried_unchanged_after_its_input_is_rebuilt(coordinator):
    fail(coordinator, "original", plan())
    store = coordinator.store
    store.save_step("rebuilt_dem", plan(6).model_dump(), [], store.task()["state_version"])
    checkpoint = {"assets": {"dem": {"kind": "raster", "step_id": "rebuilt_dem"}}}
    store.db.execute("UPDATE task SET checkpoint=?", (json.dumps(checkpoint),))
    coordinator.check_repair(plan(5, "original"))


def test_processing_raster_summary_is_persisted_with_the_logical_asset(coordinator, tmp_path):
    output = str(tmp_path / "result.tif")
    step = StepContract(
        operation="run_processing",
        arguments={"algorithm": "native:createconstantrasterlayer", "parameters": {}},
        reason="Create a test raster",
        outputs=[{"id": "result", "kind": "raster", "binding": "OUTPUT"}],
    )
    summary = {
        "bands": [{"band": 1, "nodata": None, "valid_percent": 100.0,
                   "minimum": 1.0, "maximum": 1.0}],
        "all_nodata": False,
        "statistics_approximate": True,
    }
    assets = {}
    coordinator.register_outputs(
        step,
        "create_raster",
        {"outputs": {"OUTPUT": output}, "loaded_layers": [{
            "id": "layer-id", "source": output, "raster_summary": summary,
        }]},
        {"result": output},
        assets,
    )
    assert assets["result"]["raster_summary"] == summary


async def test_resume_issues_compact_route_for_pending_processing_map(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator, "verify_inputs", AsyncMock())
    monkeypatch.setattr(coordinator, "restore_committed", AsyncMock())
    monkeypatch.setattr(coordinator, "presentation_pending", lambda: True)
    present = AsyncMock(return_value={"status": "READY", "task_id": coordinator.store.task_id})
    monkeypatch.setattr(coordinator, "auto_present_deliverables", present)
    result = await coordinator.resume({"task_id": coordinator.store.task_id})
    handle = result["next_call"]["arguments"]["continuation_token"]
    assert len(handle) == 32
    assert coordinator.route_record(handle)["next_tool"] == "presentation_continue"
    continued = await coordinator.continue_task(result["next_call"]["arguments"])
    assert continued["status"] == "READY"
    present.assert_awaited_once()


@pytest.mark.parametrize(
    ("map_deliverable", "physical_layout", "expected_name", "expected_overwrite"),
    [
        ({"id": "map", "kind": "image", "description": "Map image"},
         "map_layout", "map_layout_2", False),
        ({"id": "final_layout", "kind": "layout", "description": "Editable layout"},
         "final_layout", "final_layout", True),
    ],
)
async def test_auto_presentation_rebuild_handles_layout_retained_in_checkpoint(
    tmp_path, monkeypatch, map_deliverable, physical_layout, expected_name,
    expected_overwrite,
):
    co = TaskCoordinator(root=tmp_path)
    co.store = TaskStore.create(
        tmp_path,
        "Build a repaired risk map",
        {},
        [
            {"id": "result", "kind": "raster", "description": "Final raster"},
            map_deliverable,
        ],
        {},
    )
    co.store.save_contract({}, "initial", 0)
    checkpoint = {
        "assets": {
            "result": {
                "kind": "raster", "step_id": "analysis", "layer_id": "layer-1",
                "path": str(tmp_path / "result.tif"),
            },
        },
        "info": {"layouts": [physical_layout]},
    }
    co.store.db.execute("UPDATE task SET checkpoint=?", (json.dumps(checkpoint),))
    co.store.event("TASK_PRESENTATION", {
        "title": "Risk map", "raster_ramp": "Viridis", "dpi": 150,
        "coordinate_crs": "EPSG:4326", "layers": None,
    })
    style = StepContract(
        operation="style_raster",
        arguments={"layer": "asset:result", "ramp": "Viridis", "band": 1,
                   "classes": 8, "opacity": 1},
        inputs=["result"],
        reason="Already styled",
    )
    co.store.save_step(
        "presentation_style_result", style.model_dump(), [],
        co.store.task()["state_version"],
    )
    co.store.db.execute(
        "UPDATE steps SET status='COMMITTED' WHERE id='presentation_style_result'"
    )

    class CapturedLayout(Exception):
        pass

    captured = {}

    async def capture_layout(payload):
        captured.update(payload["contract"]["arguments"])
        raise CapturedLayout

    monkeypatch.setattr(co, "submit_step_contract", capture_layout)
    try:
        with pytest.raises(CapturedLayout):
            await co.auto_present_deliverables(co.issue_continuation())
        assert captured["name"] == expected_name
        assert captured["overwrite"] is expected_overwrite
    finally:
        co.store.close()
        co.traces.close()


@pytest.mark.parametrize('repair_target', ['original', 'dependent'])
def test_invalidated_success_can_recompute_unchanged_but_failed_replay_cannot(coordinator, repair_target):
    check = {"id": "crs", "kind": "crs", "target": "dem", "expected": "EPSG:3857",
             "source": "user_requirement", "basis": "Required CRS", "evidence": ["goal"]}
    original = plan(checks=[check])
    store = coordinator.store
    store.save_step('original', original.model_dump(), [], store.task()['state_version'])
    attempt = store.begin_attempt('original', 'first', original.arguments, 1,
                                  store.task()['state_version'])
    store.prepare_commit(attempt['attempt_id'], {}, {'project': 'verified.qgz'})
    store.commit(attempt['attempt_id'])
    store.save_step('dependent', original.model_dump(), ['original'], store.task()['state_version'])
    dependent = store.begin_attempt('dependent', 'second', original.arguments, 1,
                                    store.task()['state_version'])
    store.prepare_commit(dependent['attempt_id'], {}, {'project': 'dependent.qgz'})
    store.commit(dependent['attempt_id'])
    store.invalidate(['original'], 'Recompute after display correction',
                     checkpoint={'project': 'earlier.qgz'})
    assert store.db.execute("SELECT status FROM steps WHERE id='dependent'").fetchone()[0] == 'INVALIDATED'
    with pytest.raises(TaskError, match='repairs_step'):
        coordinator.check_repair(plan(checks=[check]))
    with pytest.raises(TaskError, match='retain required'):
        coordinator.check_repair(plan(5, repair_target))
    revised = plan(5, repair_target, [check])
    coordinator.check_repair(revised)
    fail(coordinator, 'recomputed', revised)
    with pytest.raises(TaskError) as rejected:
        coordinator.check_repair(plan(5, 'recomputed', [check]))
    assert rejected.value.payload['code'] == 'UNCHANGED_REPAIR'


@pytest.mark.parametrize("original_phase", ["preconditions", "postconditions"])
def test_repair_cannot_move_required_checks_to_another_phase(coordinator, original_phase):
    check = {"id": "crs", "kind": "crs", "target": "dem", "expected": "EPSG:3857",
             "source": "user_requirement", "basis": "Required CRS", "evidence": ["goal"]}
    original = plan().model_dump()
    original[original_phase] = [check]
    fail(coordinator, "original", StepContract.model_validate(original))
    revised = plan(6, "original").model_dump()
    revised["postconditions" if original_phase == "preconditions" else "preconditions"] = [check]
    with pytest.raises(TaskError) as failure:
        coordinator.check_repair(StepContract.model_validate(revised))
    assert failure.value.payload["code"] == "CONTRACT_WEAKENING"
    assert failure.value.payload["evidence"]["checks"] == [{"id": "crs", "phase": original_phase}]


async def test_infrastructure_retry_budget_is_persistent(coordinator):
    fail(coordinator, 'original', plan())
    coordinator.store.db.execute(
        "UPDATE attempts SET failure=?", ('{"code":"WORKER_UNAVAILABLE"}',)
    )
    for _ in range(2):
        coordinator.store.event('INFRASTRUCTURE_RETRY', {'step_id': 'original', 'reason': 'restored'})
    with pytest.raises(TaskError) as failure:
        await coordinator.call('task_recover', {
            'task_id': coordinator.store.task_id,
            'continuation_token': coordinator.issue_continuation(),
            'retry_step': 'original', 'reason': 'try again',
        })
    assert failure.value.payload['code'] == 'RETRY_BUDGET_EXHAUSTED'


@pytest.mark.parametrize("case", ["omitted", "changed", "moved"])
def test_explicit_check_inheritance_preserves_obligations_without_masking_changes(coordinator, case):
    check = {"id": "crs", "kind": "crs", "target": "dem", "expected": "EPSG:3857",
             "source": "user_requirement", "basis": "Metric output", "evidence": ["goal"]}
    fail(coordinator, "original", plan(checks=[check, {**check, "id": "system_crs"},
                                              {**check, "id": "advisory", "required": False}]))
    revised = plan(6, "original")
    if case == "changed":
        revised = plan(6, "original", [{**check, "expected": "EPSG:4326"}])
    elif case == "moved":
        revised.preconditions = plan(checks=[check]).postconditions
    coordinator.inherit_repair_checks(revised)
    if case == "omitted":
        assert [item.id for item in revised.postconditions] == ["crs"]
        assert revised.postconditions[0].model_dump() == plan(checks=[check]).postconditions[0].model_dump()
        coordinator.check_repair(revised)
    else:
        with pytest.raises(TaskError) as failure:
            coordinator.check_repair(revised)
        assert failure.value.payload["code"] == "CONTRACT_WEAKENING"


def test_input_revision_changes_retry_identity_without_dropping_obligations(coordinator):
    check = {
        'id': 'crs', 'kind': 'crs', 'target': 'dem', 'expected': 'EPSG:3857',
        'source': 'user_requirement', 'basis': 'Metric CRS', 'evidence': ['goal'],
    }
    fail(coordinator, 'original', plan(checks=[check]))
    coordinator.store.event('INPUTS_REVISED', {'steps': ['original']})
    with pytest.raises(TaskError, match='retain required'):
        coordinator.check_repair(plan(5, 'original'))
    revised = plan(5, 'original', [check])
    coordinator.check_repair(revised)
    fail(coordinator, 'new_version_attempt', revised)
    with pytest.raises(TaskError, match='unchanged'):
        coordinator.check_repair(plan(5, 'new_version_attempt', [check]))


@pytest.mark.parametrize("case,calls,retries", [
    ("parameters", 1, 0), ("worker", 2, 1),
    ("network_exhausted", 3, 2), ("network_success", 3, 2),
    ("input_changed", 1, 1),
])
async def test_recovery_classification_and_bounded_backoff(coordinator, tmp_path, monkeypatch, case, calls, retries):
    co = coordinator
    failure = WorkerError("connection timed out", code=(
        "INVALID_PARAMETERS" if case == "parameters" else "OPERATION_FAILED"
    ))
    if case == "worker":
        failure = WorkerError("connection timed out")
    co.bridge.broken = case == "worker"
    co.bridge.call = AsyncMock(side_effect=(
        [failure, failure, {"ok": True}] if case == "network_success" else failure
    ))
    co.verify_inputs = AsyncMock(side_effect=(
        TaskError("INPUT_CHANGED", "Source changed") if case == "input_changed" else None
    ))
    co.restore_committed = AsyncMock()
    co.require_checks = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr("smart_qgis.coordinator.asyncio.sleep", sleep)
    monkeypatch.setattr("smart_qgis.coordinator.random.uniform", lambda a, b: 0.125)
    operation = StepContract(operation="project", arguments={"action": "new"}, reason="Test recovery")
    if case == "network_success":
        assert (await co.run_with_recovery(operation, {}, tmp_path))[0] == {"ok": True}
    else:
        with pytest.raises(TaskError if case == "input_changed" else WorkerError):
            await co.run_with_recovery(operation, {}, tmp_path)
    assert co.bridge.call.await_count == calls
    assert len(list(tmp_path.glob("execution-*"))) == calls
    events = [json.loads(row[0]) for row in co.store.db.execute(
        "SELECT body FROM events WHERE kind IN ('NETWORK_RETRY','WORKER_REPLAY') ORDER BY sequence"
    )]
    assert len(events) == retries
    if case in {"parameters", "worker"}:
        sleep.assert_not_awaited()
    else:
        assert [call.args[0] for call in sleep.await_args_list] == [1.125, 4.125][:retries]
    assert co.verify_inputs.await_count == retries
    assert co.restore_committed.await_count == (0 if case == "input_changed" else retries)


async def test_contract_correction_budget_survives_reconnect_and_requires_user_answer(coordinator, monkeypatch):
    co = coordinator
    monkeypatch.setattr(co, 'dispatch_locked', AsyncMock(side_effect=TaskError('INVALID_CONTRACT', 'Invalid field')))
    for count in range(1, 4):
        with pytest.raises(TaskError) as caught:
            await co.dispatch('step_contract_submit', {'step_id': f'new_id_{count}'})
        assert caught.value.payload['correction_budget']['remaining'] == 3-count
    task_id = co.store.task_id
    co.store.close()
    co.store = TaskStore(co.root, task_id)
    assert co.correction_failures() == 3
    monkeypatch.setattr(co, 'dispatch_locked', AsyncMock(return_value={'ok': True}))
    for operation in ['step_contract_submit', 'task_execute', 'task_begin']:
        with pytest.raises(TaskError) as caught:
            await co.dispatch(operation, {})
        assert caught.value.payload['code'] == 'CLARIFICATION_REQUIRED'
    await co.dispatch('contract_help', {})
    assert co.correction_failures() == 3
    version = co.store.task()['state_version']
    await co.clarify({
        'task_id': task_id, 'expected_state_version': version,
        'question': 'Which distance unit is intended?', 'user_response': 'Use meters.',
    })
    assert co.correction_failures() == 0
    assert co.store.task()['state_version'] == version+1
    assert co.store.db.execute("SELECT count(*) FROM events WHERE kind='USER_CLARIFICATION'").fetchone()[0] == 1


async def test_accepted_preparation_does_not_reset_failures_before_execution_progress(
    coordinator, monkeypatch,
):
    co = coordinator
    co.store.event("CORRECTION_REJECTED", {"code": "OPERATION_FAILED"})
    monkeypatch.setattr(
        co,
        "dispatch_locked",
        AsyncMock(return_value={"status": "READY", "step_status": "PLANNED"}),
    )
    prepared = await co.dispatch("prepare_algorithm", {})
    assert prepared["correction_budget"]["rejected_submissions"] == 1
    assert co.correction_failures() == 1

    async def committed(_operation, _arguments):
        co.store.db.execute("UPDATE task SET state_version=state_version+1")
        return {"attempt_id": "committed-attempt", "status": "READY"}

    monkeypatch.setattr(co, "dispatch_locked", committed)
    executed = await co.dispatch("task_execute_next", {})
    assert executed["correction_budget"]["rejected_submissions"] == 0
    assert co.correction_failures() == 0


async def test_invalid_algorithm_help_calls_reach_the_shared_correction_limit(
    coordinator, monkeypatch,
):
    co = coordinator
    monkeypatch.setattr(
        co,
        "dispatch_locked",
        AsyncMock(side_effect=WorkerError("Unknown algorithm ID", code="OPERATION_FAILED")),
    )
    for expected in (1, 2, 3):
        with pytest.raises(TaskError) as failure:
            await co.dispatch("algorithm_info", {"action": "help", "algorithm": "missing:id"})
        assert failure.value.payload["correction_budget"]["rejected_submissions"] == expected
    status = co.status()
    assert status["user_intervention_required"] is True
    assert status["intervention_reason"] == "correction_limit"
    with pytest.raises(TaskError) as stopped:
        await co.dispatch("algorithm_info", {"action": "help", "algorithm": "another:id"})
    assert stopped.value.payload["code"] == "CLARIFICATION_REQUIRED"


async def test_unfinished_task_cannot_be_replaced(coordinator):
    with pytest.raises(TaskError) as failure:
        await coordinator.dispatch("task_start", {"goal": "replacement"})
    assert failure.value.payload["code"] == "ACTIVE_TASK_EXISTS"
    assert failure.value.payload["evidence"]["task_id"] == coordinator.store.task_id


async def test_argument_failures_share_budget_across_execution_tools(coordinator, monkeypatch):
    co = coordinator
    for operation, code in [('run_processing','CONTRACT_REQUIRED'), ('task_execute','STEP_CONTRACT_REQUIRED'), ('task_invalidate','INVALID_REPAIR')]:
        monkeypatch.setattr(co, 'dispatch_locked', AsyncMock(side_effect=TaskError(code, 'Fixture error')))
        with pytest.raises(TaskError):
            await co.dispatch(operation, {})
    assert co.correction_failures() == 3
    with pytest.raises(TaskError) as stopped:
        await co.dispatch('load_data', {'path':'asset:input'})
    assert stopped.value.payload['code'] == 'CLARIFICATION_REQUIRED'
    # Read-only mistakes and transient infrastructure faults are not argument repairs.
    co.record_correction_failure('layers', TaskError('INVALID_ARGUMENTS','Fixture'), {'action':'list'})
    co.record_correction_failure('task_execute', TaskError('WORKER_TIMEOUT','Fixture'), {})
    assert co.correction_failures() == 3


async def test_worker_timeout_waits_for_user_before_more_mutations(coordinator, monkeypatch):
    co = coordinator
    original = co.dispatch_locked
    monkeypatch.setattr(
        co, "dispatch_locked",
        AsyncMock(side_effect=WorkerError("QGIS operation timed out", code="WORKER_TIMEOUT")),
    )
    with pytest.raises(TaskError) as timed_out:
        await co.dispatch("task_execute", {"task_id": co.store.task_id})
    assert timed_out.value.payload["code"] == "WORKER_TIMEOUT"
    assert co.status()["user_intervention_required"] is True
    with pytest.raises(TaskError) as stopped:
        await co.dispatch("prepare_algorithm", {"task_id": co.store.task_id})
    assert stopped.value.payload["code"] == "USER_INTERVENTION_REQUIRED"
    with pytest.raises(TaskError) as stopped_recovery:
        await co.dispatch("task_recover", {"task_id": co.store.task_id})
    assert stopped_recovery.value.payload["code"] == "USER_INTERVENTION_REQUIRED"
    assert co.correction_failures() == 0
    monkeypatch.setattr(co, "dispatch_locked", original)
    diagnosed = await co.dispatch("task_diagnose", {"task_id": co.store.task_id})
    assert diagnosed["user_intervention_required"] is True
    await co.clarify({
        "task_id": co.store.task_id,
        "expected_state_version": co.store.task()["state_version"],
        "question": "Should I retry after the QGIS timeout?",
        "user_response": "Inspect the checkpoint, then repair the failed step.",
    })
    assert co.status()["user_intervention_required"] is False


async def test_worker_timeout_is_not_automatically_replayed(coordinator, tmp_path):
    co = coordinator
    co.bridge.call = AsyncMock(side_effect=WorkerError("QGIS operation timed out", code="WORKER_TIMEOUT"))
    co.bridge.broken = True
    with pytest.raises(WorkerError):
        await co.run_with_recovery(
            plan(), {"dem": {"kind": "raster", "path": "/tmp/dem.tif"}}, tmp_path,
        )
    co.bridge.call.assert_awaited_once()
    assert co.store.db.execute("SELECT count(*) FROM events WHERE kind='WORKER_REPLAY'").fetchone()[0] == 0


async def test_resume_at_correction_limit_returns_guidance_only(coordinator, monkeypatch):
    co = coordinator
    for _ in range(co.correction_limit):
        co.store.event("CORRECTION_REJECTED", {"code": "INVALID_PARAMETERS"})
    monkeypatch.setattr(co, "verify_inputs", AsyncMock())
    monkeypatch.setattr(co, "restore_committed", AsyncMock())
    monkeypatch.setattr(co, "presentation_pending", lambda: True)
    result = await co.resume({"task_id": co.store.task_id})
    assert result["user_intervention_required"] is True
    assert result["intervention_reason"] == "correction_limit"
    assert "next_call" not in result
    assert "task_record_guidance" in result["next_action"]


@pytest.mark.parametrize('invalid_token', [{}, [], 123])
def test_correction_gate_defers_malformed_tokens_to_schema_validation(coordinator, invalid_token):
    # The MCP guard runs before schema validation; malformed JSON types must not
    # reach SQLite parameter binding or produce an unstructured server error.
    coordinator.correction_gate('task_execute', {
        'task_id': coordinator.store.task_id, 'continuation_token': invalid_token,
    })
    assert coordinator.correction_failures() == 0
