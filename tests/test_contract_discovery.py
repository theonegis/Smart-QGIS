"""Compact discovery changes transport, never contract acceptance rules."""

import json
import sys

import pytest
from pydantic import ValidationError

from smart_qgis import server as mcp_server
from smart_qgis.contracts import TaskContract, verifier_catalog
from smart_qgis.coordinator import TaskCoordinator
from smart_qgis.task_store import TaskError
from smart_qgis.task_tools import ContractHelp, StepContractSubmit, TaskContractSubmit
from smart_qgis.tools import build_tools


def test_reliable_tool_surface_hides_direct_mutations(tmp_path):
    coordinator = TaskCoordinator(root=tmp_path)
    names = {tool.name for tool in build_tools(coordinator)}
    assert {"algorithm_info", "layer_info", "step_prepare", "workflow_run", "task_execute"} <= names
    assert not {
        "project_manage",
        "load_data",
        "processing_execute",
        "style_vector",
        "style_raster",
        "layout_manage",
        "export_map",
    }.intersection(names)


def test_compact_reliable_surface_has_processing_discovery_and_typed_preparation(tmp_path):
    coordinator = TaskCoordinator(root=tmp_path)
    names = {tool.name for tool in build_tools(coordinator, compact=True)}
    assert names == {
        "algorithm_info", "task_start", "task_execute_next", "task_answer", "task_record_guidance", "task_invalidate", "task_recover", "task_diagnose",
        "prepare_algorithm",
    }
    schemas = {
        tool.name: tool.args_schema.model_json_schema()
        for tool in build_tools(coordinator, compact=True)
    }
    assert schemas["task_execute_next"]["required"] == ["continuation_token"]
    assert schemas["task_execute_next"]["additionalProperties"] is False
    assert "repairs_step" in schemas["prepare_algorithm"]["properties"]


@pytest.mark.parametrize(("option", "expected"), [
    ([], (900, "reliable", 3, 10, 60)),
    (["--correction-limit", "5"], (900, "reliable", 5, 10, 60)),
    (["--timeout", "1200", "--retention-days", "20", "--failed-retention-days", "90"],
     (1200, "reliable", 3, 20, 90)),
])
def test_server_startup_defaults_and_overrides(monkeypatch, option, expected):
    received = []

    async def fake_serve(timeout, mode, correction_limit, retention_days, failed_retention_days):
        received.append((timeout, mode, correction_limit, retention_days, failed_retention_days))

    monkeypatch.setattr(mcp_server, "serve", fake_serve)
    monkeypatch.setattr(sys, "argv", ["smart-qgis", *option])
    mcp_server.main()
    assert received == [expected]


@pytest.mark.parametrize('case', ['missing_parameter', 'external_path', 'wrong_binding'])
async def test_processing_output_errors_explain_both_reference_and_binding(tmp_path, case):
    from unittest.mock import AsyncMock

    from smart_qgis.contracts import StepContract

    coordinator = TaskCoordinator(root=tmp_path)
    coordinator.bridge.call = AsyncMock(return_value={'parameters': [
        {'name': 'INPUT', 'default': None},
        {'name': 'OUTPUT', 'default': None, 'destination': True},
    ]})
    parameters = {'INPUT': 'asset:dem'}
    if case != 'missing_parameter':
        parameters['OUTPUT'] = 'private-test-marker' if case == 'external_path' else 'output:result'
    step = StepContract(operation='run_processing', inputs=['dem'], reason='Process input',
                        arguments={'algorithm': 'native:reprojectlayer', 'parameters': parameters},
                        outputs=[{'id': 'result', 'kind': 'vector', 'binding': 'result'}])
    try:
        with pytest.raises(TaskError) as failure:
            await coordinator.check_operation(step)
        payload = failure.value.payload
        assert 'arguments.parameters.OUTPUT' in payload['next_action']
        assert "binding:'OUTPUT'" in payload['next_action']
        assert 'not saved' in payload['next_action']
        assert 'private-test-marker' not in json.dumps(payload)
        assert coordinator.store is None
    finally:
        await coordinator.close()


def test_help_schema_enumerates_registered_validators_for_both_selectors():
    schema = ContractHelp.model_json_schema()['properties']
    single = schema['kind']['anyOf'][0]['enum']
    batch = schema['kinds']['anyOf'][0]['items']['enum']
    assert set(single) == set(batch) == set(verifier_catalog()['validators'])
    # File types are arguments to readable, not independent validators.
    for payload in ({'kind': 'pdf'}, {'kinds': ['readable', 'project']}):
        with pytest.raises(ValidationError):
            ContractHelp.model_validate(payload)


async def test_layer_mutation_is_rejected_before_execution_when_asset_is_only_a_file(tmp_path):
    from smart_qgis.contracts import StepContract

    coordinator = TaskCoordinator(root=tmp_path)
    step = StepContract(
        operation="style_vector",
        arguments={"layer": "asset:boundary", "color": "transparent"},
        inputs=["boundary"],
        reason="Style the boundary",
    )
    try:
        with pytest.raises(TaskError) as failure:
            await coordinator.check_operation(
                step, {"boundary": {"kind": "vector", "path": "/data/boundary.shp"}}
            )
        assert failure.value.payload["code"] == "LAYER_NOT_LOADED"
        assert "binding='layer'" in failure.value.payload["next_action"]
        assert await coordinator.check_operation(
            step,
            {"boundary": {"kind": "vector", "path": "/data/boundary.shp", "layer_id": "id"}},
        ) == {}
    finally:
        await coordinator.close()


def test_execution_result_compaction_drops_logs_and_bounds_field_lists():
    result = TaskCoordinator.compact_worker_result(
        "load_data",
        {
            "id": "layer-id",
            "name": "Boundary",
            "kind": "vector",
            "fields": [{"name": f"field_{index}", "type": "Real"} for index in range(30)],
            "log": "very long provider log",
            "provider": "ogr",
        },
    )
    assert result["id"] == "layer-id"
    assert result["field_count"] == 30
    assert len(result["field_names"]) == 20 and result["fields_truncated"]
    assert "fields" not in result and "log" not in result and "provider" not in result


def test_server_injects_only_basic_crs_checks_not_algorithm_quality_requirements():
    """Algorithm choices must not silently create stricter acceptance criteria."""
    from smart_qgis.algorithm_rules import family_checks
    from smart_qgis.contracts import StepContract

    step = StepContract(
        operation="run_processing",
        reason="Clip the requested raster",
        inputs=["dem", "boundary"],
        arguments={
            "algorithm": "gdal:cliprasterbymasklayer",
            "parameters": {
                "INPUT": "asset:dem",
                "MASK": "asset:boundary",
                "KEEP_RESOLUTION": True,
                "NODATA": "nan",
                "OUTPUT": "output:clipped",
            },
        },
        outputs=[{"id": "clipped", "kind": "raster", "binding": "OUTPUT"}],
    )

    preconditions, postconditions = family_checks(
        step,
        defaults={"TARGET_CRS": None, "SET_RESOLUTION": False, "EXTRA": None},
    )

    assert preconditions == []
    assert [(check.kind, check.target) for check in postconditions] == [
        ("crs_valid", "clipped")
    ]
    assert not {
        "geometry_valid",
        "layout_layers",
        "legend_consistent",
        "provenance",
        "spatial_overlap",
        "raster_mask",
        "nodata",
        "raster_grid",
        "raster_values",
        "coordinate_units",
        "raster_range",
    }.intersection(check.kind for check in postconditions)


def test_maps_default_to_title_legend_scale_and_coordinate_annotations():
    from smart_qgis.algorithm_rules import family_checks
    from smart_qgis.contracts import StepContract

    step = StepContract(
        operation="layout",
        reason="Create the requested map",
        inputs=["layer"],
        arguments={"action": "create", "name": "Map", "layers": ["asset:layer"]},
        outputs=[{"id": "layout", "kind": "layout", "binding": "layout"}],
    )
    _, checks = family_checks(step)
    assert len(checks) == 1
    check = checks[0]
    assert check.kind == "layout_content"
    assert check.require_title
    assert check.require_legend
    assert check.require_scalebar
    assert check.require_grid
    assert check.grid_crs is None

    _, omitted = family_checks(
        step, map_omissions={"legend", "coordinates"}
    )
    assert omitted[0].require_title
    assert omitted[0].require_scalebar
    assert not omitted[0].require_legend
    assert not omitted[0].require_grid
    _, none = family_checks(
        step,
        map_omissions={"title", "legend", "scalebar", "coordinates"},
    )
    assert none == []


async def test_batch_validator_help_matches_single_schemas_without_task(tmp_path):
    coordinator = TaskCoordinator(root=tmp_path)
    try:
        kinds = ['readable', 'layout_content', 'legend_consistent']
        result = await coordinator.call('contract_help', ContractHelp(kinds=kinds).model_dump())
        assert result['validators'] == {kind: verifier_catalog(kind) for kind in kinds}
        assert coordinator.store is None
        assert '"$ref"' not in json.dumps(result)
        with pytest.raises(TaskError) as error:
            await coordinator.call('contract_help', {'kinds': ['readable', 'invented']})
        assert error.value.payload['code'] == 'UNKNOWN_VALIDATOR'
    finally:
        await coordinator.close()


async def test_advanced_step_structure_is_loaded_only_on_request(tmp_path):
    coordinator = TaskCoordinator(root=tmp_path)
    try:
        structure = await coordinator.call(
            "contract_help", ContractHelp(structure="step").model_dump()
        )
        assert "operation" in structure["properties"]
        assert "outputs" in structure["properties"]
    finally:
        await coordinator.close()


async def test_task_structure_is_loaded_only_on_request(tmp_path):
    coordinator = TaskCoordinator(root=tmp_path)
    try:
        structure = await coordinator.call(
            "contract_help", ContractHelp(structure="task").model_dump()
        )
        assert "requirements" in structure["properties"]
        assert "map_omissions" in structure["properties"]
        assert "checks" in structure["properties"]
    finally:
        await coordinator.close()


async def test_repeated_unselected_contract_help_is_compact(tmp_path):
    coordinator = TaskCoordinator(root=tmp_path)
    try:
        catalog = await coordinator.call("contract_help", {})
        repeated = await coordinator.call("contract_help", {})
        assert "validator_kinds" in catalog
        assert "task" not in catalog and "step" not in catalog
        assert repeated["already_provided"] is True
        assert "task" not in repeated and "step" not in repeated
        assert len(json.dumps(repeated)) < 300
    finally:
        await coordinator.close()


@pytest.mark.parametrize('payload', [
    {'kind': 'readable', 'kinds': ['readable']},
    {'structure': 'step', 'kind': 'readable'},
    {'kinds': ['readable', 'readable']}, {'kinds': []},
    {'kinds': ['readable'] * 9},
])
def test_batch_validator_help_rejects_ambiguous_or_unbounded_selection(payload):
    with pytest.raises(ValidationError):
        ContractHelp.model_validate(payload)


def test_transport_schema_is_small_and_selected_validator_is_complete():
    assert len(json.dumps(TaskContractSubmit.model_json_schema())) < 1200
    assert len(json.dumps(StepContractSubmit.model_json_schema())) < 1600
    catalog = verifier_catalog()
    assert "raster_mask" in catalog["validators"]
    assert len(json.dumps(catalog)) < 5000
    mask = verifier_catalog("raster_mask")
    assert "reference" in mask["required"]
    assert mask["additionalProperties"] is False
    # Every published helper is self-contained, without dangling definitions.
    assert '"$ref"' not in json.dumps(catalog)
    for kind in catalog["validators"]:
        assert '"$ref"' not in json.dumps(verifier_catalog(kind))


def test_invalid_payload_passes_transport_but_is_rejected_before_persistence():
    request = TaskContractSubmit(
        task_id="task",
        continuation_token="token",
        contract={
            "requirements": {"map": "Map"},
            "coverage": {"map": ["clip"]},
            "checks": [
                {
                    "id": "clip",
                    "target": "dem",
                    "kind": "raster_mask",
                    "source": "user_requirement",
                    "basis": "Clip",
                    "evidence": ["goal"],
                }
            ],
        },
    )
    with pytest.raises(TaskError) as failure:
        TaskCoordinator.parse_contract(TaskContract, request.contract)
    assert failure.value.payload["code"] == "INVALID_CONTRACT"
    assert any(
        error["field"][-1] == "reference" for error in failure.value.payload["evidence"]["errors"]
    )
    assert len(json.dumps(failure.value.payload)) < 2000


def test_unknown_validator_has_catalog_feedback():
    with pytest.raises(TaskError) as failure:
        verifier_catalog("invented")
    assert failure.value.payload["code"] == "UNKNOWN_VALIDATOR"


def test_flattened_processing_arguments_explain_the_missing_wrapper():
    coordinator = TaskCoordinator.__new__(TaskCoordinator)
    with pytest.raises(TaskError) as failure:
        coordinator.normalize("run_processing", {"INPUT": "asset:dem", "OUTPUT": "output:clip"})
    error = failure.value.payload
    assert error["code"] == "INVALID_STEP_ARGUMENTS"
    assert "parameters" in error["next_action"]
    assert any(item["field"] == ["algorithm"] for item in error["evidence"]["errors"])
    assert "asset:dem" not in json.dumps(error)


@pytest.mark.parametrize('worker_failure', [False, True])
async def test_inspection_failures_have_structured_recovery_context(tmp_path, worker_failure):
    from smart_qgis.bridge import QgisBridge, WorkerError

    bridge = QgisBridge()

    async def unavailable(operation, arguments):
        if worker_failure:
            raise WorkerError('worker unavailable')
        raise OSError('filesystem unavailable')

    bridge.call = unavailable
    coordinator = TaskCoordinator(bridge, tmp_path)
    try:
        with pytest.raises(TaskError) as failure:
            await coordinator.call('project', {'action': 'info'})
        error = failure.value.payload
        assert error['code'] == ('WORKER_UNAVAILABLE' if worker_failure else 'FILESYSTEM_ERROR')
        assert error['phase'] == 'inspection'
        assert error['retryable'] is False
        assert 'task_recover' in error['next_action']
    finally:
        await coordinator.close()


def test_algorithm_id_as_operation_returns_actionable_wrapper_without_parameter_echo():
    coordinator = TaskCoordinator.__new__(TaskCoordinator)
    with pytest.raises(TaskError) as failure:
        coordinator.normalize('gdal:cliprasterbymasklayer', {'INPUT': 'private-test-marker'})
    error = failure.value.payload
    assert error['code'] == 'UNKNOWN_OPERATION'
    assert 'run_processing' in error['evidence']['available_operations']
    assert error['evidence']['processing_structure']['operation'] == 'run_processing'
    assert 'arguments.parameters' in error['next_action']
    assert 'private-test-marker' not in json.dumps(error)
    assert len(json.dumps(error)) < 1800


@pytest.mark.parametrize('existing_task', [False, True])
async def test_missing_authorization_feedback_is_precise_and_never_executes(tmp_path, existing_task):
    from smart_qgis.bridge import QgisBridge
    from smart_qgis.task_store import TaskStore

    bridge = QgisBridge()
    calls = []

    async def forbidden(operation, arguments):
        calls.append(operation)
        raise AssertionError('Missing authorization must not reach the worker')

    bridge.call = forbidden
    coordinator = TaskCoordinator(bridge, tmp_path)
    if existing_task:
        coordinator.store = TaskStore.create(tmp_path, 'Private goal marker', {}, [{'id': 'map'}], {})
    try:
        with pytest.raises(TaskError) as failure:
            await coordinator.call('run_processing', {
                'algorithm': 'native:buffer', 'parameters': {'INPUT': 'private-input-marker'},
                'task_id': coordinator.store.task_id if existing_task else 'caller-task',
            })
        error = failure.value.payload
        assert error['code'] == 'CONTRACT_REQUIRED'
        assert error['evidence']['missing_fields'] == ['continuation_token', 'step_id']
        assert 'top-level' in error['message']
        assert 'step_contract_submit' in error['evidence']['context_sources']['continuation_token']
        assert not calls
        assert 'private-input-marker' not in json.dumps(error)
        assert 'Private goal marker' not in json.dumps(error)
        if existing_task:
            assert 'Do not create a replacement task' in error['next_action']
        else:
            assert 'task_begin' in error['next_action']
    finally:
        await coordinator.close()
