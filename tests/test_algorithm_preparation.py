import json

import pytest
from pydantic import ValidationError

from smart_qgis.algorithm_parameters import (
    AlgorithmParameter,
    _normalize_matrix,
    normalize_supplied_value,
    validate_processing_expressions,
)
from smart_qgis.contracts import TaskContract
from smart_qgis.coordinator import TaskCoordinator
from smart_qgis.task_store import TaskError, TaskStore
from smart_qgis.task_tools import PrepareAlgorithm, TaskRun


def test_matrix_rows_are_flattened_without_changing_values():
    assert _normalize_matrix([[1, 3], [2, 1], [0, 10, 5]]) == [1, 1, 3, 2, 2, 1, 0, 10, 5]
    assert _normalize_matrix([
        {"minimum": 0, "maximum": 10, "value": 5},
    ]) == [0, 10, 5]


def test_live_help_normalizes_representation_without_inventing_values():
    enum = AlgorithmParameter(
        name="METHOD", description="Method", type="enum",
        choices=[{"value": 0, "label": "Nearest"}, {"value": 1, "label": "Bilinear"}],
    )
    integer = AlgorithmParameter(
        name="COUNT", description="Count", type="number",
        definition={"data_type": 0},
    )
    boolean = AlgorithmParameter(name="KEEP", description="Keep", type="boolean")
    crs = AlgorithmParameter(name="CRS", description="CRS", type="crs")

    assert normalize_supplied_value(enum, "Bilinear") == 1
    assert normalize_supplied_value(enum, "1") == 1
    assert normalize_supplied_value(integer, "3") == 3
    assert normalize_supplied_value(boolean, "false") is False
    assert normalize_supplied_value(crs, "4326") == "4326"


def test_gdal_formula_preflight_accepts_elementwise_supported_syntax():
    validate_processing_expressions(
        "gdal:rastercalculator",
        {"FORMULA": "where((A >= 1) & (A <= 5), A * 2, 0)"},
    )


@pytest.mark.parametrize(
    ("formula", "code"),
    [
        ("1 + (A - np.nanmin(A))", "INVALID_EXPRESSION_DIALECT"),
        ("1 + (A - nanmin(A))", "NONLOCAL_RASTER_EXPRESSION"),
        ("where(A > 0, 1)", "INVALID_EXPRESSION"),
        ("A > 0 and B > 0", "INVALID_EXPRESSION_DIALECT"),
        ("where(", "INVALID_EXPRESSION"),
    ],
)
def test_gdal_formula_preflight_rejects_known_dialect_failures(formula, code):
    with pytest.raises(TaskError) as error:
        validate_processing_expressions(
            "gdal:rastercalculator", {"FORMULA": formula}
        )
    assert error.value.payload["code"] == code


def test_prepare_algorithm_schema_accepts_multilayer_asset_bindings():
    parsed = PrepareAlgorithm.model_validate({
        "task_id": "task",
        "continuation_token": "token",
        "step_id": "stack",
        "algorithm": "native:cellstatistics",
        "inputs": {"INPUT": ["north", "center", "south"]},
    })
    assert parsed.inputs["INPUT"] == ["north", "center", "south"]

    with pytest.raises(ValidationError):
        PrepareAlgorithm.model_validate({
            "task_id": "task",
            "continuation_token": "token",
            "step_id": "stack",
            "algorithm": "native:cellstatistics",
            "inputs": {"INPUT": []},
        })


class ParameterBridge:
    reliable = True
    timeout = 30

    def __init__(self, parameter, *, preflight_failures=0):
        self.parameter = parameter
        self.preflight_failures = preflight_failures

    async def close(self, abort=False):
        return None

    async def call(self, operation, arguments):
        if operation == "algorithms":
            return {
                "id": "test:parameter",
                "parameters": [
                    {
                        "name": "INPUT", "description": "Input", "type": "source",
                        "destination": False, "required": True, "has_default": False,
                        "default": None, "definition": {},
                    },
                    self.parameter,
                    {
                        "name": "OUTPUT", "description": "Output", "type": "rasterDestination",
                        "destination": True, "required": True, "has_default": False,
                        "default": None, "definition": {},
                    },
                ],
            }
        if operation == "_inspect":
            return {
                "kind": "vector", "valid": True,
                "fields": [{"name": "class"}, {"name": "value"}],
            }
        if operation == "_processing_preflight":
            if self.preflight_failures:
                self.preflight_failures -= 1
                from smart_qgis.bridge import WorkerError
                raise WorkerError("temporary preflight failure")
            return {"valid": True, "executed": False}
        raise AssertionError((operation, arguments))


def ready_coordinator(tmp_path, parameter, *, correction_limit=3):
    source = tmp_path / "input.gpkg"
    source.write_bytes(b"test")
    store = TaskStore.create(
        tmp_path / "state", "Test parameter preparation",
        {
            "source": {
                "path": str(source), "kind": "vector",
                "fingerprint": {"digest": "test", "files": []},
            }
        },
        [{"id": "result", "kind": "vector", "description": "Result"}],
        {"test": True},
    )
    store.save_contract(TaskContract().model_dump(), "", 0)
    coordinator = TaskCoordinator(
        ParameterBridge(parameter), tmp_path / "state", correction_limit=correction_limit
    )
    coordinator.store = store
    return coordinator


@pytest.mark.parametrize(
    ("parameter", "answer_type", "answer", "choices"),
    [
        (
            {
                "name": "TARGET_CRS", "description": "Target CRS", "type": "crs",
                "destination": False, "required": True, "has_default": False,
                "default": None, "definition": {},
            },
            "crs", "EPSG:3857", [],
        ),
        (
            {
                "name": "FIELD", "description": "Field", "type": "field",
                "destination": False, "required": True, "has_default": False,
                "default": None,
                "definition": {"parent_layer_parameter_name": "INPUT"},
            },
            "field", "class", ["class", "value"],
        ),
        (
            {
                "name": "METHOD", "description": "Method", "type": "enum",
                "destination": False, "required": True, "has_default": False,
                "default": None, "choices": [
                    {"value": 0, "label": "Nearest"},
                    {"value": 1, "label": "Bilinear"},
                ],
                "definition": {},
            },
            "enum", 1, [0, 1],
        ),
    ],
)
async def test_prepare_algorithm_persists_and_resumes_typed_questions(
    tmp_path, parameter, answer_type, answer, choices
):
    coordinator = ready_coordinator(tmp_path, parameter)
    task_id = coordinator.store.task_id
    prepared = await coordinator.call("prepare_algorithm", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "generic_step",
        "algorithm": "test:parameter",
        "inputs": {"INPUT": "source"},
        "outputs": {"OUTPUT": "result"},
        "parameters": {},
        "load_outputs": True,
    })
    assert prepared["status"] == "WAITING_FOR_USER"
    assert len(prepared["questions"]) == 1
    question = prepared["questions"][0]
    assert question["answer_type"] == answer_type
    assert [item["value"] for item in question["choices"]] == choices
    plan_id = prepared["plan_id"]
    assert coordinator.store.algorithm_plan(plan_id)["status"] == "WAITING_FOR_USER"
    token = prepared["continuation_token"]
    await coordinator.close()
    coordinator = TaskCoordinator(ParameterBridge(parameter), tmp_path / "state")
    recovered = await coordinator.call("task_diagnose", {
        "task_id": task_id, "include_details": False,
    })
    assert recovered["questions"][0]["id"] == question["id"]
    answered = await coordinator.call("task_answer", {
        "task_id": task_id,
        "continuation_token": token,
        "question_id": question["id"],
        "answer": answer,
    })
    assert answered["prepared_algorithm"] == "test:parameter"
    assert answered["next_call"]["tool"] == "task_execute_next"
    row = coordinator.store.db.execute(
        "SELECT body FROM steps WHERE id='generic_step'"
    ).fetchone()
    contract = json.loads(row["body"])
    assert contract["arguments"]["parameters"][parameter["name"]] == answer
    assert coordinator.store.algorithm_plan(plan_id)["status"] == "PLANNED"
    await coordinator.close()


async def test_task_answer_replays_saved_answer_after_preflight_interruption(tmp_path):
    parameter = {
        "name": "BAND_A", "description": "Band", "type": "band",
        "destination": False, "required": True, "has_default": False,
        "default": None, "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    coordinator.bridge.preflight_failures = 1
    task_id = coordinator.store.task_id
    prepared = await coordinator.call("prepare_algorithm", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "calculator",
        "algorithm": "test:parameter",
        "inputs": {"INPUT": "source"},
        "outputs": {"OUTPUT": "result"},
        "parameters": {},
    })
    question = prepared["questions"][0]
    with pytest.raises(TaskError) as failure:
        await coordinator.call("task_answer", {
            "task_id": task_id,
            "continuation_token": prepared["continuation_token"],
            "question_id": question["id"],
            "answer": 1,
        })
    assert failure.value.payload["code"] == "WORKER_UNAVAILABLE"
    row = coordinator.store.db.execute(
        "SELECT status,answer FROM questions WHERE id=?", (question["id"],)
    ).fetchone()
    assert (row["status"], row["answer"]) == ("ANSWERED", "1")
    assert coordinator.store.db.execute(
        "SELECT 1 FROM steps WHERE id='calculator'"
    ).fetchone() is None

    resumed = await coordinator.call("task_answer", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "question_id": question["id"],
        "answer": 1,
    })
    assert resumed["prepared_algorithm"] == "test:parameter"
    assert resumed["next_call"]["tool"] == "task_execute_next"
    assert coordinator.store.db.execute(
        "SELECT status FROM steps WHERE id='calculator'"
    ).fetchone()[0] == "PLANNED"

    with pytest.raises(TaskError) as failure:
        await coordinator.call("task_answer", {
            "task_id": task_id,
            "continuation_token": coordinator.issue_continuation(),
            "question_id": question["id"],
            "answer": 2,
        })
    assert failure.value.payload["code"] == "ANSWER_IMMUTABLE"
    await coordinator.close()


async def test_prepare_algorithm_infers_raster_working_output(tmp_path):
    parameter = {
        "name": "METHOD", "description": "Method", "type": "enum",
        "destination": False, "required": True, "has_default": True,
        "default": 0, "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    prepared = await coordinator.call("prepare_algorithm", {
        "task_id": coordinator.store.task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "raster_working_step",
        "algorithm": "test:parameter",
        "inputs": {"INPUT": "source"},
        "outputs": {"OUTPUT": "scratch_raster"},
        "parameters": {},
        "load_outputs": True,
    })
    assert prepared["prepared_algorithm"] == "test:parameter"
    row = coordinator.store.db.execute(
        "SELECT body FROM steps WHERE id='raster_working_step'"
    ).fetchone()
    output = json.loads(row["body"])["outputs"]
    assert output == [{"id": "scratch_raster", "kind": "raster", "binding": "OUTPUT", "filename": None}]
    await coordinator.close()


async def test_prepare_algorithm_compiles_multilayer_bindings_to_asset_list(tmp_path):
    parameter = {
        "name": "STACK", "description": "Input layers", "type": "multilayer",
        "destination": False, "required": True, "has_default": False,
        "default": None, "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    prepared = await coordinator.call("prepare_algorithm", {
        "task_id": coordinator.store.task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "stack_step",
        "algorithm": "test:parameter",
        "inputs": {"INPUT": "source", "STACK": ["source"]},
        "outputs": {"OUTPUT": "scratch_raster"},
        "parameters": {},
    })
    assert prepared["step_status"] == "PLANNED"
    row = coordinator.store.db.execute(
        "SELECT body FROM steps WHERE id='stack_step'"
    ).fetchone()
    contract = json.loads(row["body"])
    assert contract["inputs"] == ["source"]
    assert contract["arguments"]["parameters"]["STACK"] == ["asset:source"]
    await coordinator.close()


async def test_prepare_algorithm_corrects_names_and_enum_from_live_help(tmp_path):
    parameter = {
        "name": "METHOD", "description": "Method", "type": "enum",
        "destination": False, "required": True, "has_default": False,
        "default": None, "choices": [
            {"value": 0, "label": "Nearest"},
            {"value": 1, "label": "Bilinear"},
        ],
        "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    prepared = await coordinator.call("prepare_algorithm", {
        "task_id": coordinator.store.task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "normalized_step",
        "algorithm": "test:parameter",
        "inputs": {"input": "source"},
        "outputs": {"output": "result"},
        "parameters": {"method": "Bilinear"},
    })
    assert prepared["step_status"] == "PLANNED"
    row = coordinator.store.db.execute(
        "SELECT body FROM steps WHERE id='normalized_step'"
    ).fetchone()
    contract = json.loads(row["body"])
    assert contract["arguments"]["parameters"]["METHOD"] == 1
    plan = coordinator.store.algorithm_plan(prepared["plan_id"])
    assert {item["reason"] for item in plan["normalizations"]} == {
        "parameter_name_case", "live_help_type_or_choice",
    }
    await coordinator.close()


async def test_prepare_algorithm_moves_declared_layer_out_of_scalar_parameters(tmp_path):
    coordinator = ready_coordinator(tmp_path, {
        "name": "REFERENCE_LAYER", "description": "Reference layer", "type": "source",
        "destination": False, "required": True, "has_default": False,
        "default": None, "definition": {},
    })
    prepared = await coordinator.call("prepare_algorithm", {
        "task_id": coordinator.store.task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "align_step", "algorithm": "test:parameter",
        "inputs": {"INPUT": "source"},
        "outputs": {"OUTPUT": "aligned"},
        "parameters": {"REFERENCE_LAYER": "source"},
    })
    assert prepared["step_status"] == "PLANNED"
    step = coordinator.store.db.execute(
        "SELECT body FROM steps WHERE id='align_step'"
    ).fetchone()
    assert json.loads(step["body"])["arguments"]["parameters"]["REFERENCE_LAYER"] == "asset:source"
    plan = coordinator.store.algorithm_plan(prepared["plan_id"])
    assert plan["request"]["inputs"]["REFERENCE_LAYER"] == "source"
    assert any(item["reason"] == "declared_layer_asset_binding" for item in plan["normalizations"])
    await coordinator.close()


async def test_prepare_algorithm_suggests_name_without_guessing_binding(tmp_path):
    coordinator = ready_coordinator(tmp_path, {
        "name": "REFERENCE_LAYER", "description": "Reference layer", "type": "source",
        "destination": False, "required": True, "has_default": False,
        "default": None, "definition": {},
    })
    with pytest.raises(TaskError) as error:
        await coordinator.call("prepare_algorithm", {
            "task_id": coordinator.store.task_id,
            "continuation_token": coordinator.issue_continuation(),
            "step_id": "align_step", "algorithm": "test:parameter",
            "inputs": {"INPUT": "source", "REF_LAYER": "source"},
            "outputs": {"OUTPUT": "aligned"},
        })
    assert error.value.payload["evidence"]["possible_names"]["REF_LAYER"] == ["REFERENCE_LAYER"]
    await coordinator.close()


async def test_prepare_algorithm_carries_repair_reference_and_required_checks(tmp_path, monkeypatch):
    parameter = {
        "name": "METHOD", "description": "Method", "type": "enum",
        "destination": False, "required": True, "has_default": True,
        "default": 0, "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    submitted = []

    async def capture_step(request):
        submitted.append(request)
        return {"next_call": {"tool": "task_execute_next", "arguments": {"continuation_token": "example"}}}

    monkeypatch.setattr(coordinator, "submit_step_contract", capture_step)
    prepared = await coordinator.call("prepare_algorithm", {
        "task_id": coordinator.store.task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "repaired_step",
        "algorithm": "test:parameter",
        "inputs": {"INPUT": "source"},
        "outputs": {"OUTPUT": "result"},
        "repairs_step": "failed_step",
    })
    assert prepared["prepared_algorithm"] == "test:parameter"
    assert submitted[0]["contract"]["repairs_step"] == "failed_step"
    assert submitted[0]["inherit_required_checks"] is True
    await coordinator.close()


async def test_prepare_algorithm_replaces_invalidated_step_on_compact_path(tmp_path):
    parameter = {
        "name": "METHOD", "description": "Method", "type": "enum",
        "destination": False, "required": True, "has_default": True,
        "default": 0, "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    task_id = coordinator.store.task_id
    first = await coordinator.call("prepare_algorithm", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "failed_step",
        "algorithm": "test:parameter",
        "inputs": {"INPUT": "source"},
        "outputs": {"OUTPUT": "result"},
    })
    assert first["step_status"] == "PLANNED"
    with coordinator.store.transaction():
        coordinator.store.db.execute(
            "UPDATE steps SET status='INVALIDATED' WHERE id='failed_step'"
        )
    repaired = await coordinator.call("prepare_algorithm", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "repaired_step",
        "algorithm": "test:parameter",
        "inputs": {"INPUT": "source"},
        "outputs": {"OUTPUT": "result"},
        "parameters": {"METHOD": 1},
        "repairs_step": "failed_step",
    })
    assert repaired["step_status"] == "PLANNED"
    row = coordinator.store.db.execute(
        "SELECT body FROM steps WHERE id='repaired_step'"
    ).fetchone()
    assert json.loads(row["body"])["repairs_step"] == "failed_step"
    await coordinator.close()


async def test_user_clarification_reopens_compact_preparation_after_three_rejections(tmp_path):
    parameter = {
        "name": "METHOD", "description": "Method", "type": "enum",
        "destination": False, "required": True, "has_default": True,
        "default": 0, "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    for _ in range(3):
        coordinator.store.event("CORRECTION_REJECTED", {"code": "INVALID_PARAMETERS"})
    task_id = coordinator.store.task_id
    request = {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "step_id": "after_clarification",
        "algorithm": "test:parameter",
        "inputs": {"INPUT": "source"},
        "outputs": {"OUTPUT": "result"},
    }
    with pytest.raises(TaskError) as error:
        await coordinator.call("prepare_algorithm", request)
    assert error.value.payload["code"] == "CLARIFICATION_REQUIRED"
    clarified = await coordinator.call("task_record_guidance", {
        "task_id": task_id,
        "continuation_token": coordinator.issue_continuation(),
        "question": "How should the operation continue?",
        "user_response": "Check the documented parameter names and retry this step.",
    })
    assert clarified["correction_budget"]["rejected_submissions"] == 0
    request["continuation_token"] = coordinator.issue_continuation()
    prepared = await coordinator.call("prepare_algorithm", request)
    assert prepared["step_status"] == "PLANNED"
    await coordinator.close()


async def test_custom_correction_limit_applies_until_user_clarifies(tmp_path):
    coordinator = ready_coordinator(tmp_path, {}, correction_limit=5)
    store = coordinator.store
    for _ in range(4):
        store.event("CORRECTION_REJECTED", {"code": "INVALID_PARAMETERS"})
    assert coordinator.status()["correction_budget"] == {
        "rejected_submissions": 4, "limit": 5, "requires_user_input": False,
    }
    coordinator.correction_gate("prepare_algorithm", {"task_id": store.task_id})
    store.event("CORRECTION_REJECTED", {"code": "INVALID_PARAMETERS"})
    with pytest.raises(TaskError) as error:
        coordinator.correction_gate("prepare_algorithm", {"task_id": store.task_id})
    assert error.value.payload["evidence"]["limit"] == 5
    clarified = await coordinator.call("task_record_guidance", {
        "task_id": store.task_id,
        "continuation_token": coordinator.issue_continuation(),
        "question": "How should I proceed?",
        "user_response": "Use the algorithm's documented parameter names.",
    })
    assert clarified["correction_budget"]["limit"] == 5
    assert clarified["correction_budget"]["rejected_submissions"] == 0
    await coordinator.close()


async def test_frozen_plan_returns_a_structured_question_instead_of_guessing(tmp_path):
    parameter = {
        "name": "TARGET_CRS", "description": "Target CRS", "type": "crs",
        "destination": False, "required": True, "has_default": False,
        "default": None, "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    with pytest.raises(TaskError, match="Frozen plan lacks") as error:
        await coordinator.execute_plan({
            "task_id": coordinator.store.task_id,
            "continuation_token": coordinator.issue_continuation(),
            "plan": [{
                "step_id": "planned_step", "algorithm": "test:parameter",
                "inputs": {"INPUT": "source"}, "outputs": {"OUTPUT": "result"},
                "parameters": {}, "load_outputs": True,
            }],
        })
    assert error.value.payload["code"] == "PLAN_PARAMETER_UNRESOLVED"
    assert error.value.payload["evidence"]["questions"][0]["answer_type"] == "crs"
    assert coordinator.store.pending_questions()[0]["parameter"] == "TARGET_CRS"
    await coordinator.close()


def test_frozen_plan_rejects_duplicate_step_ids():
    with pytest.raises(ValueError, match="step IDs"):
        TaskRun.model_validate({
            "goal": "Test", "inputs": {},
            "deliverables": [{"id": "result", "kind": "raster", "description": "Result"}],
            "plan": [
                {"step_id": "same", "algorithm": "native:a"},
                {"step_id": "same", "algorithm": "native:b"},
            ],
        })


def test_compact_route_accepts_a_frozen_plan(tmp_path):
    parameter = {
        "name": "METHOD", "description": "Method", "type": "enum",
        "destination": False, "required": True, "has_default": True,
        "default": 0, "definition": {},
    }
    coordinator = ready_coordinator(tmp_path, parameter)
    route = coordinator.compact_next_call({
        "tool": "plan_execute",
        "arguments": {
            "task_id": coordinator.store.task_id,
            "continuation_token": coordinator.issue_continuation(),
            "plan": [{"step_id": "one", "algorithm": "test:parameter"}],
        },
    })
    target, arguments = coordinator.routed_arguments(route["arguments"]["continuation_token"])
    assert target == "plan_execute"
    assert arguments["plan"][0]["step_id"] == "one"


def test_nested_algorithm_parameters_register_asset_dependencies():
    assert TaskCoordinator.parameter_asset_ids({
        "LAYERS": ["asset:first", {"reference": "asset:second"}],
        "literal": "not-an-asset",
    }) == ["first", "second"]


def test_feature_sink_is_an_automatic_vector_working_asset():
    from smart_qgis.algorithm_parameters import AlgorithmParameter, destination_asset_kind

    assert destination_asset_kind(AlgorithmParameter(
        name="OUTPUT", description="Features", type="sink", destination=True
    )) == "vector"
