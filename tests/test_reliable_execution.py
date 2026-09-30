"""Real worker integration for the default contract-gated execution boundary."""

import asyncio
import errno
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from smart_qgis.bridge import QgisBridge, WorkerError
from smart_qgis.contracts import StepContract
from smart_qgis.coordinator import TaskCoordinator
from smart_qgis.task_store import TaskError, TaskStore
from smart_qgis.tools import build_tools

pytestmark = pytest.mark.integration


def require_qgis():
    if not Path("/Applications/QGIS.app").exists() and not os.getenv("SMART_QGIS_PYTHON"):
        pytest.skip("Requires an installed QGIS runtime")


def readable(target="map", kind="pdf"):
    return {
        "id": "read_" + target,
        "kind": "readable",
        "target": target,
        "data_kind": kind,
        "source": "user_requirement",
        "basis": "Requested output must be readable",
        "evidence": ["goal"],
    }


async def initialize(coordinator, inputs=None):
    tools = {tool.name: tool for tool in build_tools(coordinator)}
    status = await tools["task_begin"].ainvoke(
        {
            "goal": "Make a PDF map of synthetic points",
            "inputs": inputs or {},
            "deliverables": [{"id": "map", "description": "PDF map", "kind": "pdf"}],
        }
    )
    status = await tools["task_contract_submit"].ainvoke(
        {
            "task_id": status["task_id"],
            "continuation_token": status["continuation_token"],
            "contract": {
                "requirements": {"pdf": "A readable PDF map"},
                "checks": [readable()],
                "coverage": {"pdf": ["read_map"]},
            },
        }
    )
    return tools, status


async def step(
    tools,
    status,
    step_id,
    operation,
    arguments,
    outputs=(),
    inputs=(),
    postconditions=(),
    repairs_step=None,
    inherit_required_checks=False,
):
    status = await tools["step_contract_submit"].ainvoke(
        {
            "task_id": status["task_id"],
            "continuation_token": status["continuation_token"],
            "step_id": step_id,
            "inherit_required_checks": inherit_required_checks,
            "contract": {
                "operation": operation,
                "arguments": arguments,
                "outputs": list(outputs),
                "inputs": list(inputs),
                "postconditions": list(postconditions),
                "reason": "Map workflow",
                "repairs_step": repairs_step,
            },
        }
    )
    execute_request = status["next_call"]["arguments"]
    request = execute_request
    result = await tools["step_execute"].ainvoke(execute_request)
    status = await tools["task_diagnose"].ainvoke({"task_id": status["task_id"]})
    return status, result, request


def points():
    return {
        "action": "create",
        "name": "Points",
        "geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"value": n},
                    "geometry": {"type": "Point", "coordinates": [100 + n, 30 + n]},
                }
                for n in (1, 2, 3)
            ],
        },
    }


async def test_processing_discovery_filters_pages_and_reports_reliable_support(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / 'state')
    try:
        compact = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        started = await compact['task_start'].ainvoke({
            'goal': 'Inspect installed Processing algorithms',
            'inputs': {},
            'deliverables': [
                {'id': 'result', 'kind': 'raster', 'description': 'Processing result'},
            ],
            'raster_styles': {
                'result': {'mode': 'mask', 'color': '#666666', 'opacity': 0.5},
            },
        })
        assert started['route'] == 'processing_required'
        assert coordinator.presentation_options()['raster_styles']['result']['mode'] == 'mask'
        first = await coordinator.call('algorithms', {'action': 'list', 'provider': 'native', 'limit': 2})
        assert first['next_offset'] == 2 and len(first['algorithms']) == 2
        second = await coordinator.call('algorithms', {
            'action': 'list', 'provider': 'native', 'limit': 2, 'offset': first['next_offset'],
        })
        assert {a['id'] for a in first['algorithms']}.isdisjoint(a['id'] for a in second['algorithms'])
        assert all(a['provider'] == 'native' for a in first['algorithms'] + second['algorithms'])
        ranked = await coordinator.call('algorithms', {
            'action': 'list', 'query': 'native:buffer',
        })
        assert ranked['algorithms'][0]['id'] == 'native:buffer'
        assert len(ranked['algorithms']) <= 12
        assert await coordinator.call('algorithms', {
            'action': 'list', 'query': 'native:buffer',
        }) == ranked
        typo = await coordinator.call('algorithms', {
            'action': 'list', 'query': 'bufffer', 'provider': 'native',
        })
        assert typo['total'] == 0 and typo['algorithms'] == []
        assert 'native:buffer' in {item['id'] for item in typo['suggestions']}
        assert all(item['reliable_supported'] for item in typo['suggestions'])
        assert 'bufffer' in typo['suggested_terms']
        assert 'do not execute' in typo['next_action']
        buffer = await coordinator.call('algorithms', {'action': 'help', 'algorithm': 'native:buffer'})
        assert buffer['reliable_supported'] and buffer['execution_mode'] == 'reliable'
        parameters = {p['name']: p for p in buffer['parameters']}
        assert parameters['INPUT']['required'] and not parameters['INPUT']['has_default']
        assert parameters['DISTANCE']['required'] and parameters['DISTANCE']['has_default']
        assert parameters['DISSOLVE']['required'] and parameters['DISSOLVE']['has_default']
        clip = await coordinator.call('algorithms', {
            'action': 'help', 'algorithm': 'gdal:cliprasterbymasklayer',
        })
        clip_parameters = {p['name']: p for p in clip['parameters']}
        assert not clip_parameters['SOURCE_CRS']['required']
        assert not clip_parameters['SOURCE_CRS']['has_default']
        assert all(a['reliable_supported'] for a in first['algorithms'])
        help_result = await coordinator.call(
            'algorithms', {'action': 'help', 'algorithm': first['algorithms'][0]['id']}
        )
        assert help_result['reliable_supported'] and help_result['parameters']
        grouped = await coordinator.call('algorithms', {
            'action': 'list', 'provider': 'native',
            'group': first['algorithms'][0]['group'], 'limit': 200,
        })
        assert all(
            a['group'] == first['algorithms'][0]['group'] for a in grouped['algorithms']
        )
        empty = await coordinator.call('algorithms', {'action': 'list', 'provider': 'missing-provider'})
        assert empty['algorithms'] == [] and empty['next_offset'] is None
        for query in ('clip raster mask', ' MASK   clip RASTER '):
            found = await coordinator.call('algorithms', {
                'action': 'list', 'query': query, 'provider': 'gdal', 'limit': 200,
            })
            assert 'gdal:cliprasterbymasklayer' in {a['id'] for a in found['algorithms']}
            assert all(all(term in (a['id'] + ' ' + a['name'] + ' ' + a['group_name']).casefold()
                           for term in query.casefold().split()) for a in found['algorithms'])
        unmatched = await coordinator.call('algorithms', {
            'action': 'list', 'query': 'clip nonexistent-keyword',
        })
        assert unmatched['total'] == 0
        assert coordinator.store is not None
    finally:
        await coordinator.close()


async def test_compact_task_start_rejects_contract_before_creating_task(tmp_path):
    require_qgis()
    root = tmp_path / "state"
    coordinator = TaskCoordinator(QgisBridge(120), root)
    request = {
        "goal": "Create a processing result",
        "inputs": {},
        "deliverables": [{"id": "result", "kind": "raster", "description": "Result"}],
        "raster_ramp": "Viridis",
        "dpi": 300,
    }
    try:
        with pytest.raises(TaskError) as failure:
            await coordinator.run_task({**request, "contract": {"notes": "extra field"}})
        assert failure.value.payload["code"] == "INVALID_CONTRACT"
        assert coordinator.store is None
        assert not root.exists() or not list(root.iterdir())

        with pytest.raises(TaskError) as failure:
            await coordinator.run_task({
                **request,
                "contract": {"checks": [readable("missing", "raster")]},
            })
        assert failure.value.payload["code"] == "UNKNOWN_ASSET"
        assert coordinator.store is None
        assert not root.exists() or not list(root.iterdir())

        with pytest.raises(TaskError) as failure:
            await coordinator.run_task({**request, "contract": {}, "coordinate_crs": "not-a-crs"})
        assert failure.value.payload["code"] == "INVALID_CRS"
        assert coordinator.store is None
        assert not root.exists() or not list(root.iterdir())

        started = await coordinator.run_task({
            **request, "contract": {}, "coordinate_crs": "geographic",
        })
        assert started["route"] == "processing_required"
        assert coordinator.store.task_id == started["task_id"]
        assert coordinator.presentation_options()["coordinate_crs"] == "EPSG:4326"

        guidance = {
            "task_id": started["task_id"],
            "continuation_token": started["continuation_token"],
            "question": "Which coordinate annotation CRS should the map use?",
            "user_response": "Use the source projected CRS",
        }
        with pytest.raises(TaskError) as failure:
            await coordinator.call("task_update", {
                **guidance, "map_coordinate_crs": "not-a-crs",
            })
        assert failure.value.payload["code"] == "INVALID_CRS"
        assert coordinator.presentation_options()["coordinate_crs"] == "EPSG:4326"
        clarified = await coordinator.call("task_update", {
            **guidance, "map_coordinate_crs": "EPSG:32126",
        })
        assert clarified["task_id"] == started["task_id"]
        assert coordinator.presentation_options()["coordinate_crs"] == "EPSG:32126"
    finally:
        await coordinator.close()


async def test_processing_discovery_requires_a_task_first(tmp_path):
    coordinator = TaskCoordinator(root=tmp_path / 'state')
    try:
        with pytest.raises(TaskError) as failure:
            await coordinator.call('algorithms', {'action': 'list', 'query': 'buffer'})
        assert failure.value.payload['code'] == 'TASK_REQUIRED'
        assert failure.value.payload['next_action'].startswith('For a new request call task_start')
    finally:
        await coordinator.close()


async def test_unknown_output_is_rejected_before_step_or_attempt_is_saved(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    try:
        tools, status = await initialize(coordinator)
        before_version = coordinator.store.task()["state_version"]
        request = {
            "task_id": status["task_id"], "continuation_token": status["continuation_token"],
            "step_id": "save_project", "contract": {
                "operation": "project", "arguments": {"action": "save", "path": "output:project"},
                "outputs": [{"id": "project_qgis", "kind": "project", "binding": "project"}],
                "reason": "Persist the editable project",
            },
        }
        with pytest.raises(TaskError) as failure:
            await tools["step_contract_submit"].ainvoke(request)
        error = failure.value.payload
        assert error["code"] == "UNKNOWN_OUTPUT" and error["phase"] == "contract"
        assert error["evidence"]["declared_output_ids"] == ["project_qgis"]
        assert "outputs[].id" in error["next_action"]
        assert coordinator.store.task()["state_version"] == before_version
        assert coordinator.store.db.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        # Only correct the reference; the same continuation and step ID remain usable.
        request["contract"]["arguments"]["path"] = "output:project_qgis"
        accepted = await tools["step_contract_submit"].ainvoke(request)
        assert coordinator.store.task()["state_version"] == before_version + 1
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        next_call = accepted['next_call']
        assert next_call['tool'] == 'step_execute'
        assert next_call['arguments']['continuation_token']
        executed = await tools[next_call['tool']].ainvoke(next_call['arguments'])
        for _ in range(3):
            coordinator.store.event('CORRECTION_REJECTED', {'code': 'INVALID_CONTRACT'})
        replayed = await tools[next_call['tool']].ainvoke(next_call['arguments'])
        assert replayed == executed
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
    finally:
        await coordinator.close()


async def test_cancellation_restores_committed_state_and_requires_explicit_resume(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    try:
        tools, status = await initialize(coordinator)
        status, _, _ = await step(
            tools,
            status,
            "upstream",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        entered = asyncio.Event()
        original = coordinator.bridge.call

        async def interrupted_call(operation, arguments):
            result = await original(operation, arguments)
            if operation == "vector_data":
                entered.set()
                await asyncio.Future()
            return result

        coordinator.bridge.call = interrupted_call
        pending = asyncio.create_task(
            step(
                tools,
                status,
                "cancelled",
                "vector_data",
                {**points(), "name": "Uncommitted"},
                [{"id": "uncommitted", "kind": "vector", "binding": "layer"}],
            )
        )
        await asyncio.wait_for(entered.wait(), 30)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert coordinator.store.task()["status"] == "CANCELLED"
        layers = await coordinator.bridge.call("layers", {"action": "list"})
        assert len(layers["layers"]) == 1
        assert "uncommitted" not in coordinator.assets()
        with pytest.raises(TaskError, match="resume_cancelled"):
            await coordinator.call("task_recover", {"task_id": status["task_id"]})
        restored = await coordinator.call(
            "task_recover",
            {
                "task_id": status["task_id"],
                "resume_cancelled": True,
            },
        )
        assert restored["status"] == "READY"
        assert (
            coordinator.store.db.execute("SELECT status FROM steps WHERE id='upstream'").fetchone()[
                0
            ]
            == "COMMITTED"
        )
        assert (
            coordinator.store.db.execute(
                "SELECT count(*) FROM attempts WHERE step_id='cancelled'"
            ).fetchone()[0]
            == 1
        )
    finally:
        await coordinator.close()


@pytest.mark.parametrize("offline_tracing", [False, True])
async def test_contract_gated_map_completion_idempotence_and_server_resume(tmp_path, monkeypatch, offline_tracing):
    require_qgis()
    if offline_tracing:
        import threading

        import smart_qgis.coordinator as module
        from smart_qgis.observability import TraceExporter

        received = threading.Event()

        class OfflineClient:
            def create_run(self, **kwargs):
                received.set()
                raise ConnectionError("test-only-sensitive-marker")

        monkeypatch.setenv("SMART_QGIS_LANGSMITH_TRACING", "true")
        monkeypatch.setattr(module, "TraceExporter", lambda: TraceExporter(OfflineClient))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    try:
        tools, status = await initialize(coordinator)
        contract = coordinator.current_contract().model_dump()
        contract["intermediates"] = {"figure": "layout"}
        contract["checks"].append({
            "id": "final_legend", "kind": "legend_consistent", "target": "figure",
            "source": "user_requirement", "basis": "Map legend matches rendered layers",
            "evidence": ["goal"],
        })
        contract["checks"].append({
            "id": "final_content", "kind": "layout_content", "target": "figure",
            "texts": ["Required map title"], "require_scalebar": True, "grid_crs": "EPSG:4326",
            "source": "user_requirement", "basis": "Title, scale and graticule required",
            "evidence": ["Synthetic task requirements"],
        })
        status = await tools["task_contract_submit"].ainvoke({
            "task_id": status["task_id"], "continuation_token": status["continuation_token"],
            "contract": contract, "reason": "Declare intermediate layout for final map checks",
        })
        with pytest.raises(TaskError) as wrong_kind:
            await step(tools, status, "wrong_kind", "vector_data", points(),
                       [{"id": "figure", "kind": "vector", "binding": "layer"}])
        assert wrong_kind.value.payload["code"] == "ASSET_KIND_MISMATCH"
        compact = await tools["task_diagnose"].ainvoke({"task_id": status["task_id"]})
        detailed = await tools["task_diagnose"].ainvoke(
            {
                "task_id": status["task_id"],
                "include_details": True,
            }
        )
        assert "checkpoint" not in compact
        assert detailed["checkpoint"] == coordinator.store.task()["checkpoint"]
        assert detailed["diagnostics"]["state_version"] == coordinator.store.task()["state_version"]
        assert compact["assets"] == {
            key: {name: value[name] for name in ("kind", "input", "path", "layout") if name in value}
            for key, value in detailed["assets"].items()
        }
        assert all(isinstance(item["dependencies"], list) for item in compact["steps"])
        assert "vector_data" not in tools
        with pytest.raises(TaskError) as missing_authorization:
            await coordinator.call("vector_data", points())
        assert missing_authorization.value.payload["code"] == "CONTRACT_REQUIRED"
        assert set(missing_authorization.value.payload["evidence"]["missing_fields"]) == {
            "task_id", "step_id", "continuation_token"
        }
        with pytest.raises(TaskError, match="not passed"):
            await tools["task_finish"].ainvoke(
                {"task_id": status["task_id"], "continuation_token": status["continuation_token"]}
            )
        status, result, request = await step(
            tools,
            status,
            "points",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        assert result["validation"][0]["status"] == "passed"
        assert all(set(report) == {"id", "status"} for report in result["validation"])
        assert Path(result["assets"]["points"]["path"]).is_file()
        cached = await tools["step_execute"].ainvoke(request)
        assert cached == result
        assert len((await tools["layer_info"].ainvoke({}))["layers"]) == 1
        status, _, _ = await step(
            tools,
            status,
            "select",
            "vector_data",
            {"action": "select", "layer": "asset:points", "expression": '"value" > 1'},
            inputs=["points"],
        )
        status, _, _ = await step(
            tools,
            status,
            "layout",
            "layout",
            {
                "name": "Figure",
                "title": "Required map title",
                "layers": ["asset:points"],
                "extent_layer": "asset:points",
            },
            [{"id": "figure", "kind": "layout", "binding": "layout"}],
            ["points"],
        )
        task_id = status["task_id"]
        await coordinator.close()
        coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
        tools = {tool.name: tool for tool in build_tools(coordinator)}
        status = await tools["task_recover"].ainvoke({"task_id": task_id})
        status, result, _ = await step(
            tools,
            status,
            "export",
            "export_map",
            {
                "layout": "asset:figure",
                "path": "output:map",
            },
            [{"id": "map", "kind": "pdf", "binding": "path"}],
            ["figure"],
        )
        assert Path(result["assets"]["map"]["path"]).is_file()
        status = await tools["task_finish"].ainvoke(
            {"task_id": task_id, "continuation_token": status["continuation_token"]}
        )
        assert status["status"] == "COMPLETED"
        recorded = json.loads(coordinator.store.db.execute(
            "SELECT body FROM events WHERE kind='TASK_VALIDATED' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0])
        assert recorded["passed"]
        assert all(report["duration_ms"] >= 0 for report in recorded["checks"])
        timings = [json.loads(row[0]) for row in coordinator.store.db.execute(
            "SELECT body FROM events WHERE kind='PHASE_TIMING'"
        )]
        point_timings = [item for item in timings if item["step_id"] == "points"]
        assert {item["phase"] for item in point_timings} == {
            "preconditions", "execution", "checkpoint", "validation", "persistence", "commit"
        }
        assert len(point_timings) == 6  # Cached requests do not repeat phase measurements.
        assert all(item["duration_ms"] >= 0 for item in timings)
        assert any(item["phase"] == "final_validation" for item in timings)
        if offline_tracing:
            assert received.wait(2)
    finally:
        await coordinator.close()


async def test_failed_validation_restores_previous_project_and_blocks_commit(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        check = {
            "id": "bad_crs",
            "kind": "crs",
            "target": "points",
            "expected": "EPSG:3857",
            "source": "user_requirement",
            "basis": "test mismatch",
            "evidence": ["goal"],
        }
        with pytest.raises(TaskError, match="Required checks") as failure:
            await step(
                tools,
                status,
                "bad",
                "vector_data",
                points(),
                [{"id": "points", "kind": "vector", "binding": "layer"}],
                postconditions=[check],
            )
        assert failure.value.payload["phase"] == "validation"
        assert all(report["duration_ms"] >= 0
                   for report in failure.value.payload["evidence"]["details"]["evidence"]["checks"])
        assert failure.value.payload["task_id"] == status["task_id"]
        assert failure.value.payload["checkpoint"]["project"]
        current = await tools["task_diagnose"].ainvoke({"task_id": status["task_id"]})
        assert current["status"] == "BLOCKED"
        assert "points" not in current["assets"]
        assert (await tools["layer_info"].ainvoke({}))["layers"] == []
        assert current["attempts"][0]["status"] == "FAILED"
        with pytest.raises(TaskError) as retry:
            await tools["task_recover"].ainvoke({
                "task_id": status["task_id"], "continuation_token": current["continuation_token"],
                "retry_step": "bad", "reason": "Attempt unchanged validation again",
            })
        assert retry.value.payload["code"] == "SEMANTIC_REPAIR_REQUIRED"
        repaired, result, _ = await step(
            tools, current, "corrected", "vector_data", {**points(), "crs": "EPSG:3857"},
            [{"id": "points", "kind": "vector", "binding": "layer"}],
            repairs_step="bad", inherit_required_checks=True,
        )
        saved = await tools["contract_get"].ainvoke({"task_id": current["task_id"], "step_id": "corrected"})
        retained = next(item for item in saved["contract"]["postconditions"] if item["id"] == "bad_crs")
        assert retained["expected"] == "EPSG:3857" and retained["required"]
        assert any(item["id"] == "bad_crs" and item["status"] == "passed" for item in result["validation"])
        assert coordinator.store.task()["contract_version"] == 1
        assert len(repaired["attempts"]) == 2 and repaired["attempts"][0]["status"] == "FAILED"
    finally:
        await coordinator.close()


async def test_default_mcp_exposes_tasks_and_returns_structured_contract_error(tmp_path):
    require_qgis()
    env = {**os.environ, "SMART_QGIS_STATE_DIR": str(tmp_path)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "smart_qgis.server"], env=env
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=120)) as session:
            await session.initialize()
            advertised = (await session.list_tools()).tools
            schemas = {tool.name: tool.inputSchema for tool in advertised}
            assert set(schemas) == {
                "project_info", "data_info", "algorithm_info", "task_start", "task_execute",
                "task_answer", "task_update", "task_resume", "task_diagnose", "task_restart",
                "task_stop", "prepare_algorithm",
            }
            for name in {
                "task_execute", "task_answer", "task_update", "task_resume", "prepare_algorithm",
            }:
                assert "continuation_token" not in schemas[name].get("properties", {})
                assert "continuation_token" not in schemas[name].get("required", [])
            assert schemas["task_execute"].get("properties", {}) == {}


async def test_step_prepare_load_layout_and_save_recipes(tmp_path):
    require_qgis()
    source = tmp_path / "input.geojson"
    source.write_text(json.dumps(points()["geojson"]))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator)}
        state = await tools["task_begin"].ainvoke({
            "goal": "Create an editable point map",
            "inputs": {"points_source": {"path": str(source), "kind": "vector"}},
            "deliverables": [
                {"id": "project", "kind": "project", "description": "Editable project"}
            ],
        })
        assert "task_contract_schema" not in state
        assert "validator_kinds" not in state
        assert state["next_call"]["arguments"]["contract"] == {}
        state = await tools["task_contract_submit"].ainvoke({
            "task_id": state["task_id"],
            "continuation_token": state["continuation_token"],
            "contract": {},
        })
        assert state["next_call"]["tool"] == "workflow_run"
        assert "step_contract_schema" not in state
        with pytest.raises(TaskError) as ambiguity:
            await tools["step_prepare"].ainvoke({
                "task_id": state["task_id"],
                "continuation_token": state["continuation_token"],
                "step_id": "needs_crs",
                "action": "reproject_vector",
                "source": "asset:points_source",
                "output": "projected_points",
            })
        assert ambiguity.value.payload["code"] == "TASK_AMBIGUOUS"
        assert ambiguity.value.payload["next_action"] == "ask_user"
        assert (await tools["task_diagnose"].ainvoke({
            "task_id": state["task_id"]
        }))["correction_budget"]["rejected_submissions"] == 0

        async def prepare_and_execute(step_id, action, **recipe):
            nonlocal state
            state = await tools["step_prepare"].ainvoke({
                "task_id": state["task_id"],
                "continuation_token": state["continuation_token"],
                "step_id": step_id,
                "action": action,
                **recipe,
            })
            assert state["prepared_recipe"] == action
            assert "assets" not in state and "steps" not in state and "attempts" not in state
            result = await tools["step_execute"].ainvoke(state["next_call"]["arguments"])
            assert all(set(report) == {"id", "status"} for report in result["validation"])
            state = await tools["task_diagnose"].ainvoke({"task_id": state["task_id"]})
            return result

        loaded = await prepare_and_execute(
            "load_points", "load", source="asset:points_source", output="points"
        )
        assert loaded["assets"]["points"]["layer_id"]
        layout = await prepare_and_execute("make_layout", "create_layout", output="figure")
        assert layout["assets"]["figure"]["layout"] == "figure"
        saved = await prepare_and_execute("save_project", "save_project", output="project")
        assert Path(saved["assets"]["project"]["path"]).is_file()
        final = await tools["task_finish"].ainvoke({
            "task_id": state["task_id"],
            "continuation_token": state["continuation_token"],
        })
        assert final["status"] == "COMPLETED"
        assert final["correction_budget"]["rejected_submissions"] == 0
    finally:
        await coordinator.close()


async def test_standard_map_workflow_runs_with_per_step_checkpoints(tmp_path):
    require_qgis()
    source = tmp_path / "input.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "Boundary"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[100, 30], [102, 30], [102, 32], [100, 32], [100, 30]]],
            },
        }],
    }))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator)}
        state = await tools["task_begin"].ainvoke({
            "goal": "Create an editable boundary map",
            "inputs": {"boundary_source": {"path": str(source), "kind": "vector"}},
            "deliverables": [
                {"id": "project", "kind": "project", "description": "Editable project"}
            ],
        })
        state = await tools["task_contract_submit"].ainvoke({
            **state["next_call"]["arguments"],
        })
        assert state["next_call"]["tool"] == "workflow_run"
        result = await tools["workflow_run"].ainvoke(state["next_call"]["arguments"])
        assert result["workflow"] == "standard_map_project"
        assert [item["action"] for item in result["completed_steps"]] == [
            "load", "style_vector", "create_layout", "save_project",
        ]
        assert all(item["status"] == "COMMITTED" for item in result["completed_steps"])
        assert Path(result["assets"]["project"]["path"]).is_file()
        assert result["next_call"]["tool"] == "task_finish"
        assert coordinator.store.db.execute(
            "SELECT count(*) FROM attempts WHERE status='COMMITTED'"
        ).fetchone()[0] == 4
        style_contract = json.loads(coordinator.store.db.execute(
            "SELECT body FROM steps WHERE id='workflow_style_boundary_source'"
        ).fetchone()[0])
        assert style_contract["arguments"]["color"] == "transparent"
        layout_result = json.loads(coordinator.store.db.execute(
            "SELECT result FROM attempts WHERE step_id='workflow_layout'"
        ).fetchone()[0])
        layout_checks = {
            item["id"]: item for item in layout_result["validation"]
        }
        content = next(
            item for key, item in layout_checks.items() if key.endswith("layout_content")
        )
        assert content["status"] == "passed"
        assert content["evidence"]["title_present"]
        assert content["evidence"]["linked_visible_legends"]
        assert content["evidence"]["linked_visible_scalebars"]
        assert content["evidence"]["annotated_coordinate_grids"]
        final = await tools["task_finish"].ainvoke(result["next_call"]["arguments"])
        assert final["status"] == "COMPLETED"
        assert Path(final["assets"]["project"]["path"]).is_file()
        assert final["correction_budget"]["rejected_submissions"] == 0
    finally:
        await coordinator.close()


async def test_auto_presentation_rebuild_uses_fresh_layout_after_checkpoint_restore(tmp_path):
    """A stale physical layout must not trap the compact recovery route."""
    require_qgis()
    source = tmp_path / "result.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "Result"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[100, 30], [102, 30], [102, 32], [100, 32], [100, 30]]],
            },
        }],
    }))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    coordinator.store = TaskStore.create(
        tmp_path / "state",
        "Create a repaired result map",
        {},
        [
            {"id": "result", "kind": "vector", "description": "Final result"},
            {"id": "map", "kind": "image", "description": "Result map"},
        ],
        {},
    )
    coordinator.store.save_contract({}, "initial", 0)
    try:
        await coordinator.bridge.call("project", {"action": "create"})
        loaded = await coordinator.bridge.call(
            "load_data", {"path": str(source), "kind": "vector", "name": "Result"}
        )
        await coordinator.bridge.call("layout", {
            "action": "create", "name": "map_layout", "title": "Old map",
            "layers": [loaded["id"]], "extent_layer": loaded["id"],
            "legend": True, "scalebar": True, "grid": True,
        })
        analysis = StepContract(
            operation="load_data",
            arguments={"path": str(source), "kind": "vector"},
            outputs=[{"id": "result", "kind": "vector", "binding": "layer"}],
            reason="Fixture analysis output",
        )
        coordinator.store.save_step(
            "analysis", analysis.model_dump(), [], coordinator.store.task()["state_version"]
        )
        coordinator.store.db.execute("UPDATE steps SET status='COMMITTED' WHERE id='analysis'")
        style = StepContract(
            operation="style_vector",
            arguments={"layer": "asset:result"},
            inputs=["result"],
            reason="Fixture presentation style",
        )
        coordinator.store.save_step(
            "presentation_style_result", style.model_dump(), ["analysis"],
            coordinator.store.task()["state_version"],
        )
        coordinator.store.db.execute(
            "UPDATE steps SET status='COMMITTED' WHERE id='presentation_style_result'"
        )
        coordinator.store.event("TASK_PRESENTATION", {
            "title": "Repaired map", "raster_ramp": "Viridis", "dpi": 150,
            "coordinate_crs": "EPSG:4326", "layers": None,
        })
        checkpoint = await coordinator.make_checkpoint(
            coordinator.store.directory / "fixture-checkpoint",
            {"result": {
                "kind": "vector", "step_id": "analysis", "layer_id": loaded["id"],
                "path": str(source),
            }},
        )
        coordinator.store.db.execute(
            "UPDATE task SET checkpoint=?", (json.dumps(checkpoint),)
        )

        result = await coordinator.auto_present_deliverables(coordinator.issue_continuation())

        assert result["next_call"]["tool"] == "task_finish"
        assert Path(result["assets"]["map"]["path"]).is_file()
        assert (await coordinator.bridge.call("layout", {"action": "list"}))["layouts"] == [
            "map_layout", "map_layout_2",
        ]
        layout_contract = json.loads(coordinator.store.db.execute(
            "SELECT body FROM steps WHERE id='presentation_layout'"
        ).fetchone()[0])
        assert layout_contract["arguments"]["name"] == "map_layout_2"
        assert layout_contract["arguments"]["overwrite"] is False
    finally:
        await coordinator.close()


async def test_compact_task_run_inspects_routes_and_finishes_standard_map(tmp_path):
    require_qgis()
    source = tmp_path / "compact-input.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "Boundary"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[100, 30], [102, 30], [102, 32], [100, 32], [100, 30]]],
            },
        }],
    }))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "compact-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        routed = await tools["task_start"].ainvoke({
            "goal": "Create an editable boundary map",
            "inputs": {"boundary": {"path": str(source)}},
            "deliverables": [
                {"id": "project", "kind": "project", "description": "Editable project"}
            ],
        })
        assert routed["route"] == "standard_map_project"
        assert "continuation_token" not in routed
        assert routed["inspections"]["boundary"]["kind"] == "vector"
        assert routed["inspections"]["boundary"]["geometry_type"] == "Polygon"
        assert routed["next_call"]["tool"] == "task_execute"
        assert routed["next_call"]["arguments"] == {}

        final = await tools["task_execute"].ainvoke({})
        assert final["status"] == "COMPLETED"
        assert "continuation_token" not in final
        assert final["workflow"] == "standard_map_project"
        assert [item["action"] for item in final["completed_steps"]] == [
            "load", "style_vector", "create_layout", "save_project",
        ]
        assert Path(final["assets"]["project"]["path"]).is_file()
        assert final["correction_budget"]["rejected_submissions"] == 0
        assert coordinator.store.db.execute(
            "SELECT count(*) FROM attempts WHERE status='COMMITTED'"
        ).fetchone()[0] == 4
        replay = await tools["task_execute"].ainvoke({})
        assert replay == final
        assert coordinator.store.db.execute(
            "SELECT count(*) FROM attempts WHERE status='COMMITTED'"
        ).fetchone()[0] == 4
        revised = await tools["task_update"].ainvoke({
            "task_id": final["task_id"],
            "instruction": "Use the supplied Chinese title.",
            "map_title": "基于 DEM 的崎岖度计算结果制图",
            "legend_title": "地形崎岖度",
            "map_crs": "EPSG:3857",
            "map_coordinate_crs": "EPSG:4326",
            "map_coordinate_annotations": {
                "format": "degree_minute", "cardinal_directions": False,
                "density": "sparse",
            },
            "map_elements": {"legend": {"border": True}},
        })
        assert revised["presentation_rebuild"] is True
        assert revised["completion_state"] == "NOT_COMPLETED"
        assert revised["next_call"] == {"tool": "task_execute", "arguments": {}}
        assert "workflow_load_boundary" not in revised["invalidated_steps"]
        rebuilt = await tools["task_execute"].ainvoke({})
        assert rebuilt["status"] == "COMPLETED"
        layout_contract = json.loads(coordinator.store.db.execute(
            "SELECT body FROM steps WHERE id='presentation_layout'"
        ).fetchone()[0])
        assert layout_contract["arguments"]["title"] == "基于 DEM 的崎岖度计算结果制图"
        assert layout_contract["arguments"]["legend_title"] == "地形崎岖度"
        assert layout_contract["arguments"]["crs"] == "EPSG:3857"
        assert layout_contract["arguments"]["coordinate_annotations"]["density"] == "sparse"
        assert layout_contract["arguments"]["map_elements"]["legend"]["border"] is True
    finally:
        await coordinator.close()


async def test_compact_task_adds_osm_basemap_without_changing_thematic_extent(tmp_path):
    require_qgis()
    source = tmp_path / "area.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[100, 30], [102, 30], [102, 32], [100, 32], [100, 30]]],
        }}],
    }))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "osm-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        await tools["task_start"].ainvoke({
            "goal": "Create a boundary map with OSM context",
            "inputs": {"area": {"path": str(source)}},
            "deliverables": [{"id": "project", "kind": "project", "description": "Editable map"}],
            "basemap": {"provider": "openstreetmap"},
            "map_crs": "EPSG:3857",
            "coordinate_crs": "EPSG:4326",
            "coordinate_annotations": {
                "format": "decimal", "cardinal_directions": True, "density": "dense",
            },
            "map_elements": {
                "legend": {"flow": "horizontal", "border": False},
                "scalebar": {"units": "kilometers", "style": "line_ticks_middle"},
            },
        })
        completed = await tools["task_execute"].ainvoke({})
        assert completed["status"] == "COMPLETED"
        assert "add_basemap" in [step["action"] for step in completed["completed_steps"]]
        layout = next(step for step in completed["completed_steps"] if step["action"] == "create_layout")
        assert layout["status"] == "COMMITTED"
        layout_contract = json.loads(coordinator.store.db.execute(
            "SELECT body FROM steps WHERE id='workflow_layout'"
        ).fetchone()[0])
        assert layout_contract["arguments"]["crs"] == "EPSG:3857"
        assert layout_contract["arguments"]["grid_crs"] == "EPSG:4326"
        assert layout_contract["arguments"]["coordinate_annotations"]["density"] == "dense"
    finally:
        await coordinator.close()


async def test_compact_task_can_add_osm_without_a_layout_or_output(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "osm-only-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        await tools["task_start"].ainvoke({
            "goal": "Add OpenStreetMap to the current project",
            "basemap": {"provider": "openstreetmap"},
        })
        completed = await tools["task_execute"].ainvoke({})
        assert completed["status"] == "COMPLETED"
        assert [step["action"] for step in completed["completed_steps"]] == ["add_basemap"]
    finally:
        await coordinator.close()


async def test_compact_task_can_load_and_style_a_vector_without_map_output(tmp_path):
    require_qgis()
    source = tmp_path / "points.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
        "geometry": {"type": "Point", "coordinates": [100, 30]}}],
    }))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "layers-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        await tools["task_start"].ainvoke({
            "goal": "Load and style the point layer",
            "inputs": {"points": {"path": str(source)}},
            "vector_styles": {"points": {"color": "#ff0000", "size": 4}},
        })
        completed = await tools["task_execute"].ainvoke({})
        assert completed["status"] == "COMPLETED"
        assert [step["action"] for step in completed["completed_steps"]] == ["load", "style_vector"]
    finally:
        await coordinator.close()


async def test_compact_common_layer_style_query_and_project_updates(tmp_path):
    require_qgis()
    source = tmp_path / "managed-points.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"name": "A", "value": 1},
             "geometry": {"type": "Point", "coordinates": [100, 30]}},
            {"type": "Feature", "properties": {"name": "B", "value": 2},
             "geometry": {"type": "Point", "coordinates": [101, 31]}},
        ],
    }))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "managed-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        await tools["task_start"].ainvoke({
            "goal": "Load, classify and organize points",
            "inputs": {"points": {"path": str(source)}},
            "vector_styles": {"points": {
                "renderer": "graduated", "graduated_field": "value",
                "classes": 2, "method": "equal_interval",
            }},
            "layer_operations": [
                {"action": "rename", "layer": "points", "name": "Styled points"},
                {"action": "opacity", "layer": "points", "opacity": 0.7},
                {"action": "group", "name": "Analysis", "layers": ["points"]},
            ],
            "project_update": {"title": "Managed project", "crs": "EPSG:3857"},
        })
        completed = await tools["task_execute"].ainvoke({})
        assert completed["status"] == "COMPLETED"
        assert [step["action"] for step in completed["completed_steps"]] == [
            "load", "style_vector", "layer_manage", "layer_manage", "layer_manage",
            "project_update",
        ]
        project = await tools["project_info"].ainvoke({})
        assert project["title"] == "Managed project"
        assert project["crs"] == "EPSG:3857"
        assert project["layers"][0]["name"] == "Styled points"
        ruled = await coordinator.bridge.call("style_vector", {
            "layer": project["layers"][0]["id"], "renderer": "rule_based",
            "marker": "triangle", "line_style": "dash",
            "rules": [{
                "expression": '"value" = 1', "label": "First",
                "color": "#ff0000", "outline": "#000000", "size": 3, "width": 0.5,
            }],
        })
        assert ruled["renderer"] == "RuleRenderer"

        sample = await tools["data_info"].ainvoke({
            "source": str(source), "query": {"action": "sample", "limit": 1},
        })
        assert len(sample["features"]) == 1
        statistics = await tools["data_info"].ainvoke({
            "source": str(source), "query": {"action": "statistics", "field": "value"},
        })
        assert statistics["count"] == 2
        assert statistics["mean"] == 1.5

        updated = await tools["task_update"].ainvoke({
            "task_id": completed["task_id"], "instruction": "Hide the point layer",
            "layer_operations": [{"action": "visibility", "layer": "map_points", "visible": False}],
        })
        assert updated["next_call"] == {"tool": "task_execute", "arguments": {}}
        assert (await tools["task_execute"].ainvoke({}))["status"] == "COMPLETED"
        assert (await tools["project_info"].ainvoke({}))["layers"][0]["visible"] is False
    finally:
        await coordinator.close()


async def test_compact_task_loads_a_declared_qml_style(tmp_path):
    require_qgis()
    source = tmp_path / "qml-points.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {"name": "A"},
            "geometry": {"type": "Point", "coordinates": [100, 30]},
        }],
    }))
    qml = tmp_path / "points.qml"
    seed = TaskCoordinator(QgisBridge(120), tmp_path / "qml-seed-state")
    try:
        layer = await seed.bridge.call("load_data", {
            "path": str(source), "name": "Seed", "kind": "vector",
        })
        await seed.bridge.call("style_vector", {
            "layer": layer["id"], "renderer": "single", "color": "#ff0000",
        })
        await seed.bridge.call("style_file", {
            "action": "save", "layer": layer["id"], "path": str(qml),
        })
    finally:
        await seed.close()

    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "qml-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        await tools["task_start"].ainvoke({
            "goal": "Load points with the supplied QML style",
            "inputs": {
                "points": {"path": str(source)},
                "point_style": {"path": str(qml), "kind": "style"},
            },
            "vector_styles": {
                "points": {"renderer": "qml", "qml_asset": "point_style"},
            },
        })
        completed = await tools["task_execute"].ainvoke({})
        assert completed["status"] == "COMPLETED"
        assert [step["action"] for step in completed["completed_steps"]] == [
            "load", "style_vector",
        ]
        project = await tools["project_info"].ainvoke({})
        assert project["layers"][0]["name"] == "map_points"
    finally:
        await coordinator.close()


async def test_compact_project_create_save_and_open(tmp_path):
    require_qgis()
    project_path = tmp_path / "managed.qgz"
    creator = TaskCoordinator(QgisBridge(120), tmp_path / "project-create-state")
    try:
        tools = {tool.name: tool for tool in build_tools(creator, compact=True)}
        await tools["task_start"].ainvoke({
            "goal": "Create an empty managed project",
            "project": {"action": "create", "crs": "EPSG:3857", "title": "Managed"},
            "deliverables": [{
                "id": "project", "kind": "project", "description": "Editable project",
                "path": str(project_path),
            }],
        })
        completed = await tools["task_execute"].ainvoke({})
        assert completed["status"] == "COMPLETED"
        assert project_path.is_file()
        assert [step["action"] for step in completed["completed_steps"]] == [
            "project_setup", "save_project",
        ]
    finally:
        await creator.close()

    opener = TaskCoordinator(QgisBridge(120), tmp_path / "project-open-state")
    try:
        tools = {tool.name: tool for tool in build_tools(opener, compact=True)}
        await tools["task_start"].ainvoke({
            "goal": "Open the managed project",
            "project": {"action": "open", "path": str(project_path)},
        })
        assert (await tools["task_execute"].ainvoke({}))["status"] == "COMPLETED"
        project = await tools["project_info"].ainvoke({})
        assert project["title"] == "Managed"
        assert project["crs"] == "EPSG:3857"
    finally:
        await opener.close()


async def test_single_frame_map_common_projection_and_element_options(tmp_path):
    require_qgis()
    source = tmp_path / "raw-source-name.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"name": "A"},
             "geometry": {"type": "Point", "coordinates": [100, 30]}},
            {"type": "Feature", "properties": {"name": "B"},
             "geometry": {"type": "Point", "coordinates": [101, 31]}},
        ],
    }))
    bridge = QgisBridge(120)
    try:
        loaded = await bridge.call("load_data", {
            "path": str(source), "kind": "vector", "name": "调查点",
        })
        result = await bridge.call("layout", {
            "name": "个性化单框图", "title": "调查点分布图",
            "layers": [loaded["id"]], "extent_layer": loaded["id"],
            "crs": "EPSG:3857", "grid_crs": "EPSG:4326",
            "coordinate_annotations": {
                "sides": ["bottom", "left"],
                "format": "degree_minute_second", "precision": 1,
                "cardinal_directions": True, "density": "sparse",
                "grid_lines": True,
            },
            "map_elements": {
                "legend": {"flow": "vertical", "border": True},
                "scalebar": {"units": "miles", "style": "double_box"},
            },
        })
        assert result["crs"] == "EPSG:3857"
        assert result["legend_layer_names"] == ["调查点"]
        assert result["legend_flow"] == "vertical"
        assert result["legend_border"] is True
        assert result["scalebar"] == {"units": "miles", "style": "double_box"}
        assert result["coordinate_annotations"] == {
            "crs": "EPSG:4326", "format": "degree_minute_second",
            "precision": 1, "cardinal_directions": True, "density": "sparse",
            "interval": result["coordinate_annotations"]["interval"],
            "grid_lines": True, "sides": ["bottom", "left"],
        }
    finally:
        await bridge.close()


async def test_coordinate_suffixes_reject_a_projected_annotation_crs_before_task_creation(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "invalid-coordinate-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        with pytest.raises(TaskError) as failure:
            await tools["task_start"].ainvoke({
                "goal": "Create a map with projected coordinate labels and compass suffixes",
                "coordinate_crs": "EPSG:3857",
                "coordinate_annotations": {"cardinal_directions": True},
            })
        assert failure.value.payload["code"] == "INVALID_COORDINATE_FORMAT"
        assert coordinator.store is None
    finally:
        await coordinator.close()


async def test_wfs_is_compiled_as_a_vector_overlay_and_advanced_raster_schema_is_explicit(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "wfs-compile-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        started = await tools["task_start"].ainvoke({
            "goal": "Add a WFS road layer",
            "services": {"roads": {
                "provider": "wfs", "url": "https://example.test/wfs",
                "type_name": "roads",
            }},
        })
        contract = coordinator.compile_recipe({
            "task_id": started["task_id"], "action": "add_basemap", "output": "roads",
            "service": {
                "provider": "wfs", "url": "https://example.test/wfs",
                "type_name": "roads", "role": "overlay",
            },
        })
        assert contract["arguments"]["service"] == "wfs"
        assert contract["outputs"] == [{"id": "roads", "kind": "vector", "binding": "layer"}]

        from smart_qgis.task_tools import TaskRun
        request = TaskRun.model_validate({
            "goal": "Render a multispectral raster",
            "raster_styles": {"image": {
                "mode": "rgb", "red": 4, "green": 3, "blue": 2,
            }},
        })
        assert request.raster_styles["image"].model_dump()["red"] == 4

        raster = await coordinator.bridge.call("run_processing", {
            "algorithm": "native:createconstantrasterlayer",
            "parameters": {
                "EXTENT": "0,10,0,10 [EPSG:3857]", "TARGET_CRS": "EPSG:3857",
                "PIXEL_SIZE": 1, "NUMBER": 5, "OUTPUT": str(tmp_path / "display.tif"),
            },
            "load_outputs": True,
        })
        rendered = await coordinator.bridge.call("render_raster", {
            "layer": raster["loaded_layers"][0]["id"], "mode": "gray", "band": 1,
            "red": 1, "green": 2, "blue": 3, "azimuth": 315, "altitude": 45,
            "z_factor": 1, "opacity": 1,
        })
        assert rendered["renderer"] == "singlebandgray"
    finally:
        await coordinator.close()


async def test_compact_vector_edit_exports_a_copy_and_preserves_source(tmp_path):
    require_qgis()
    source = tmp_path / "source.geojson"
    original = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"name": "A", "value": 1},
             "geometry": {"type": "Point", "coordinates": [100, 30]}},
            {"type": "Feature", "properties": {"name": "B", "value": 2},
             "geometry": {"type": "Point", "coordinates": [101, 31]}},
        ],
    }
    source.write_text(json.dumps(original))
    original_bytes = source.read_bytes()
    output = tmp_path / "edited.gpkg"
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "edit-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        sample = await tools["data_info"].ainvoke({
            "source": str(source), "query": {"action": "sample", "limit": 2},
        })
        feature_id = sample["features"][0]["id"]
        await tools["task_start"].ainvoke({
            "goal": "Edit a copy of the source layer",
            "inputs": {"source": {"path": str(source)}},
            "deliverables": [{
                "id": "edited", "kind": "vector", "description": "Edited copy",
                "path": str(output),
            }],
            "data_operations": [{
                "action": "edit", "layer": "source", "output": "edited",
                "updates": [{"feature_id": feature_id, "attributes": {"name": "Updated"}}],
                "expression": "\"value\" = 2",
            }],
        })
        completed = await tools["task_execute"].ainvoke({})
        assert completed["status"] == "COMPLETED"
        assert output.is_file()
        assert source.read_bytes() == original_bytes
        edited = await tools["data_info"].ainvoke({
            "source": str(output), "query": {"action": "sample", "limit": 10},
        })
        assert len(edited["features"]) == 1
        assert edited["features"][0]["properties"]["name"] == "Updated"
    finally:
        await coordinator.close()


async def test_compact_processing_rebuilds_current_action_after_guidance_and_recovery(tmp_path):
    require_qgis()
    source = tmp_path / "polygon.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "Area"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[100, 30], [101, 30], [101, 31], [100, 31], [100, 30]]],
            },
        }],
    }))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "compact-processing-state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        started = await tools["task_start"].ainvoke({
            "goal": "Create polygon centroids",
            "inputs": {"area": {"path": str(source)}},
            "deliverables": [
                {"id": "centroids", "kind": "vector", "description": "Centroid points"}
            ],
        })
        assert started["route"] == "processing_required"
        prepared = await tools["prepare_algorithm"].ainvoke({
            "task_id": started["task_id"],
            "algorithm": "native:centroids",
            "inputs": {"INPUT": "area"},
            "outputs": {"OUTPUT": "centroids"},
        })
        assert "continuation_token" not in prepared
        assert prepared["next_call"] == {"tool": "task_execute", "arguments": {}}

        version = coordinator.store.task()["state_version"]
        await coordinator.clarify({
            "task_id": started["task_id"],
            "expected_state_version": version,
            "question": "Continue the prepared centroid step?",
            "user_response": "Yes, continue the same task.",
        })
        recovered = await tools["task_resume"].ainvoke({"task_id": started["task_id"]})
        assert "continuation_token" not in recovered

        final = await tools["task_execute"].ainvoke({})
        assert final["status"] == "COMPLETED"
        assert "continuation_token" not in final
        assert Path(final["assets"]["centroids"]["path"]).is_file()
    finally:
        await coordinator.close()


async def test_worker_lost_after_mutation_replays_from_checkpoint_once(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        old_bridge = coordinator.bridge
        old_call = old_bridge.call
        failed = False

        async def lost_response(operation, arguments):
            nonlocal failed
            result = await old_call(operation, arguments)
            if operation == "vector_data" and not failed:
                failed = True
                await old_bridge.close(abort=True)
                old_bridge.broken = True
                raise WorkerError("Worker exited after writing its output")
            return result

        old_bridge.call = lost_response
        status, _, _ = await step(
            tools,
            status,
            "points",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        assert status["status"] == "READY"
        assert len((await tools["layer_info"].ainvoke({}))["layers"]) == 1
        assert (
            coordinator.store.db.execute(
                "SELECT count(*) FROM events WHERE kind='WORKER_REPLAY'"
            ).fetchone()[0]
            == 1
        )
    finally:
        await coordinator.close()


@pytest.mark.parametrize("crash_stage", ["execution", "validation", "prepared", "committed"])
async def test_process_death_reconciles_commit_windows(tmp_path, crash_stage):
    require_qgis()
    code = r"""
import asyncio, os, sys
from smart_qgis.coordinator import TaskCoordinator
from smart_qgis.bridge import QgisBridge
from smart_qgis.tools import build_tools
async def run():
    co = TaskCoordinator(QgisBridge(120), sys.argv[1])
    tools = {t.name: t for t in build_tools(co)}
    state = await tools['task_begin'].ainvoke({'goal':'Create points','deliverables':[{'id':'points','description':'points','kind':'vector'}]})
    print(state['task_id'], flush=True)
    state = await tools['task_contract_submit'].ainvoke({'task_id':state['task_id'],'continuation_token':state['continuation_token'],'contract':{
        'requirements':{'points':'Readable points'},'coverage':{'points':['read']},'checks':[{
            'id':'read','kind':'readable','data_kind':'vector','target':'points','source':'user_requirement','basis':'goal','evidence':['goal']}]}})
    args = {'action':'create','geojson':{'type':'FeatureCollection','features':[{'type':'Feature','properties':{'n':1},'geometry':{'type':'Point','coordinates':[100,30]}}]}}
    state = await tools['step_contract_submit'].ainvoke({'task_id':state['task_id'],'continuation_token':state['continuation_token'],'step_id':'create','contract':{
        'operation':'vector_data','arguments':args,'reason':'goal','outputs':[{'id':'points','kind':'vector','binding':'layer'}]}})
    stage = sys.argv[2]
    if stage == 'execution':
        original_call = co.bridge.call
        async def crash_after_mutation(operation, arguments):
            result = await original_call(operation, arguments)
            if operation == 'vector_data':
                os._exit(81)
            return result
        co.bridge.call = crash_after_mutation
    elif stage == 'validation':
        original_checks = co.require_checks
        async def crash_during_validation(checks, assets):
            if co.store.unresolved() and 'points' in assets:
                os._exit(81)
            return await original_checks(checks, assets)
        co.require_checks = crash_during_validation
    else:
        original_commit = co.store.commit
        def crash_at_commit(attempt_id):
            if stage == 'committed':
                original_commit(attempt_id)
            os._exit(81)
        co.store.commit = crash_at_commit
    await tools['step_execute'].ainvoke(state['next_call']['arguments'])
asyncio.run(run())
"""
    process = await __import__("asyncio").to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(tmp_path), crash_stage],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert process.returncode == 81, process.stderr
    task_id = process.stdout.strip()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator)}
        state = await tools["task_recover"].ainvoke({"task_id": task_id})
        assert len(state["attempts"]) == 1
        if crash_stage in {"execution", "validation"}:
            assert state["attempts"][0]["status"] == "FAILED"
            assert "points" not in state["assets"]
            assert (await tools["layer_info"].ainvoke({}))["layers"] == []
            with pytest.raises(TaskError, match="not passed"):
                await tools["task_finish"].ainvoke(
                    {
                        "task_id": task_id,
                        "continuation_token": state["continuation_token"],
                    }
                )
            return
        assert state["attempts"][0]["status"] == "COMMITTED"
        assert len((await tools["layer_info"].ainvoke({}))["layers"]) == 1
        final = await tools["task_finish"].ainvoke(
            {"task_id": task_id, "continuation_token": state["continuation_token"]}
        )
        assert final["status"] == "COMPLETED"
    finally:
        await coordinator.close()


async def test_disk_full_during_checkpoint_preserves_previous_project(tmp_path, monkeypatch):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        initial = coordinator.store.task()["checkpoint"]
        import smart_qgis.coordinator as module

        original = module.sync_tree

        def disk_full(path):
            if "attempts" in Path(path).parts:
                raise OSError(errno.ENOSPC, "simulated disk full")
            return original(path)

        monkeypatch.setattr(module, "sync_tree", disk_full)
        with pytest.raises(TaskError, match="disk full") as failure:
            await step(
                tools,
                status,
                "points",
                "vector_data",
                points(),
                [{"id": "points", "kind": "vector", "binding": "layer"}],
            )
        assert failure.value.payload["phase"] == "checkpoint"
        stored = json.loads(coordinator.store.db.execute("SELECT failure FROM attempts").fetchone()[0])
        assert stored["phase"] == "checkpoint"
        assert coordinator.store.task()["checkpoint"] == initial
        assert coordinator.store.task()["status"] == "BLOCKED"
        assert not coordinator.store.unresolved()
        assert (await tools["layer_info"].ainvoke({}))["layers"] == []
        monkeypatch.setattr(module, "sync_tree", original)
        with pytest.raises(TaskError) as stale:
            await tools["task_recover"].ainvoke({
                "task_id": status["task_id"], "continuation_token": status["continuation_token"],
                "retry_step": "points", "reason": "Disk restored",
            })
        assert stale.value.payload["code"] == "STATE_CONFLICT"
        current = await tools["task_diagnose"].ainvoke({"task_id": status["task_id"]})
        recovered = await tools["task_recover"].ainvoke({
            "task_id": status["task_id"],
            "continuation_token": current["continuation_token"],
            "retry_step": "points", "reason": "Simulated disk capacity restored",
        })
        result = await tools["step_execute"].ainvoke(recovered["next_call"]["arguments"])
        assert Path(result["assets"]["points"]["path"]).is_file()
        attempts = list(coordinator.store.db.execute("SELECT status FROM attempts ORDER BY created"))
        assert [row[0] for row in attempts] == ["FAILED", "COMMITTED"]
        from smart_qgis.contracts import StepContract

        completed_plan = StepContract.model_validate_json(coordinator.store.db.execute(
            "SELECT body FROM steps WHERE id='points'"
        ).fetchone()[0])
        # An old infrastructure failure must not label the now-committed step as failed.
        coordinator.check_repair(completed_plan)
    finally:
        await coordinator.close()


async def test_tampered_artifact_blocks_cached_success_and_resume(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        status, result, request = await step(
            tools,
            status,
            "points",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        artifact = Path(result["assets"]["points"]["path"])
        with artifact.open("ab") as stream:
            stream.write(b"changed content")
        with pytest.raises(TaskError, match="modified"):
            await tools["step_execute"].ainvoke(request)
        with pytest.raises(TaskError, match="modified"):
            await tools["task_recover"].ainvoke({"task_id": status["task_id"]})
    finally:
        await coordinator.close()


@pytest.mark.parametrize("scenario", ["retained", "replaced", "tampered"])
async def test_state_only_revalidation_requires_exact_retained_checkpoint(tmp_path, scenario):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        status, _, _ = await step(tools, status, "points", "vector_data", points(),
                                  [{"id": "points", "kind": "vector", "binding": "layer"}])
        status, _, _ = await step(
            tools, status, "rename", "layers",
            {"action": "rename", "layer": "asset:points", "name": "Reviewed points"},
            inputs=["points"], postconditions=[readable("points", "vector")],
        )
        if scenario == "replaced":
            status, _, _ = await step(
                tools, status, "select", "vector_data",
                {"action": "select", "layer": "asset:points", "expression": '"value" > 1'},
                inputs=["points"],
            )
        contract = coordinator.current_contract().model_dump()
        contract["intermediates"] = {"points": "vector"}
        contract["checks"].append({
            "id": "fields", "kind": "fields", "target": "points", "names": ["value"],
            "source": "method_assumption", "basis": "Attribute is needed for review",
            "evidence": ["Synthetic fixture"],
        })
        status = await tools["task_contract_submit"].ainvoke({
            "task_id": status["task_id"], "continuation_token": status["continuation_token"],
            "contract": contract, "reason": "Add an attribute acceptance check",
        })
        await tools["task_validate"].ainvoke({
            "task_id": status["task_id"], "continuation_token": status["continuation_token"],
            "revalidate_steps": ["points"],
        })
        before = coordinator.status(include_details=True)
        if scenario == "tampered":
            supplemental = Path(coordinator.store.task()["checkpoint"]["supplemental"])
            supplemental.write_text(supplemental.read_text() + "\n", encoding="utf-8")
        request = {"task_id": status["task_id"], "continuation_token": before["continuation_token"],
                   "revalidate_steps": ["rename"]}
        if scenario != "retained":
            with pytest.raises(TaskError) as error:
                await tools["task_validate"].ainvoke(request)
            assert error.value.payload["code"] == (
                "ARTIFACT_CHANGED" if scenario == "tampered" else "RESULT_NOT_REVALIDATABLE"
            )
            assert coordinator.status(include_details=True) == before
        else:
            await tools["task_validate"].ainvoke(request)
            assert coordinator.store.db.execute(
                "SELECT status FROM steps WHERE id='rename'"
            ).fetchone()[0] == "COMMITTED"
            assert (await tools["layer_info"].ainvoke({}))["layers"][0]["name"] == "Reviewed points"
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == (
            3 if scenario == "replaced" else 2
        )
    finally:
        await coordinator.close()


@pytest.mark.parametrize("component", ["qgis", "numpy", "shapely", "pyproj", "missing_dependencies"])
async def test_runtime_mismatch_blocks_restore(tmp_path, component):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        checkpoint = coordinator.store.task()["checkpoint"]
        dependencies = checkpoint["environment"]["validation_dependencies"]
        assert set(dependencies) == {"numpy", "shapely", "pyproj"}
        assert all(value is None or isinstance(value, str) for value in dependencies.values())
        changed_component = "qgis" if component == "qgis" else "validation_dependencies"
        if component == "qgis":
            checkpoint["environment"][component] = "different-runtime"
        elif component == "missing_dependencies":
            del checkpoint["environment"]["validation_dependencies"]
        else:
            dependencies[component] = "different-runtime"
        coordinator.store.db.execute("UPDATE task SET checkpoint=?", (json.dumps(checkpoint),))
        before = coordinator.status(include_details=True)
        with pytest.raises(TaskError, match="versions differ") as mismatch:
            await tools["task_recover"].ainvoke({"task_id": status["task_id"]})
        assert mismatch.value.payload["code"] == "ENVIRONMENT_CHANGED"
        assert mismatch.value.payload["evidence"]["changed_components"] == [changed_component]
        assert mismatch.value.payload["evidence"]["recorded"] == {
            changed_component: checkpoint["environment"].get(changed_component)
        }
        assert "newly approved contract" in mismatch.value.payload["next_action"]
        assert coordinator.status(include_details=True) == before
    finally:
        await coordinator.close()


async def test_removed_layer_retains_durable_asset_for_processing_after_resume(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        status, _, _ = await step(
            tools,
            status,
            "points",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        saved_path = status["assets"]["points"]["path"]
        status, _, _ = await step(
            tools,
            status,
            "remove",
            "layers",
            {"action": "remove", "layer": "asset:points"},
            inputs=["points"],
        )
        assert "layer_id" not in status["assets"]["points"]
        assert status["assets"]["points"]["path"] == saved_path
        status = await tools["task_recover"].ainvoke({"task_id": status["task_id"]})
        assert (await tools["layer_info"].ainvoke({}))["layers"] == []
        status, _, _ = await step(
            tools,
            status,
            "reproject",
            "run_processing",
            {
                "algorithm": "native:reprojectlayer",
                "parameters": {
                    "INPUT": "asset:points",
                    "TARGET_CRS": "EPSG:3857",
                    "OUTPUT": "output:projected",
                },
            },
            [{"id": "projected", "kind": "vector", "binding": "OUTPUT"}],
            inputs=["points"],
        )
        assert Path(status["assets"]["projected"]["path"]).is_file()
    finally:
        await coordinator.close()


async def test_processing_load_preserves_project_dependency_chain(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        status, _, _ = await step(
            tools,
            status,
            "points",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        status, _, _ = await step(
            tools,
            status,
            "background",
            "vector_data",
            {**points(), "name": "Background"},
            [{"id": "background", "kind": "vector", "binding": "layer"}],
        )
        status, _, _ = await step(
            tools,
            status,
            "reproject",
            "run_processing",
            {
                "algorithm": "native:reprojectlayer",
                "parameters": {
                    "INPUT": "asset:points",
                    "TARGET_CRS": "EPSG:3857",
                    "OUTPUT": "output:projected",
                },
            },
            [{"id": "projected", "kind": "vector", "binding": "OUTPUT"}],
            inputs=["points"],
        )
        dependencies = json.loads(
            coordinator.store.db.execute(
                "SELECT dependencies FROM steps WHERE id='reproject'"
            ).fetchone()[0]
        )
        assert set(dependencies) == {"points", "background"}
        assert set(coordinator.store.invalidate(["background"], "test dependency closure")) == {
            "background",
            "reproject",
        }
        assert (
            coordinator.store.db.execute("SELECT status FROM steps WHERE id='points'").fetchone()[0]
            == "COMMITTED"
        )
    finally:
        await coordinator.close()


@pytest.mark.parametrize("repair_target", ["bad", "independent"])
async def test_repair_preserves_independent_results_and_project_state(tmp_path, repair_target):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        status, _, _ = await step(
            tools,
            status,
            "source",
            "vector_data",
            points(),
            [{"id": "source", "kind": "vector", "binding": "layer"}],
        )
        status, _, _ = await step(
            tools,
            status,
            "detach",
            "layers",
            {"action": "remove", "layer": "asset:source"},
            inputs=["source"],
        )
        status, _, _ = await step(
            tools,
            status,
            "bad",
            "vector_data",
            points(),
            [{"id": "bad", "kind": "vector", "binding": "layer"}],
        )
        status, _, _ = await step(
            tools,
            status,
            "independent",
            "run_processing",
            {
                "algorithm": "native:reprojectlayer",
                "load_outputs": False,
                "parameters": {
                    "INPUT": "asset:source",
                    "TARGET_CRS": "EPSG:3857",
                    "OUTPUT": "output:projected",
                },
            },
            [{"id": "projected", "kind": "vector", "binding": "OUTPUT"}],
            inputs=["source"],
        )
        original_path = status["assets"]["projected"]["path"]
        if repair_target == "independent":
            status, _, _ = await step(
                tools,
                status,
                "later_layer",
                "vector_data",
                {**points(), "name": "Later layer"},
                [{"id": "later_layer", "kind": "vector", "binding": "layer"}],
            )
        direct_tools = {tool.name: tool for tool in build_tools(coordinator)}
        status = await direct_tools["task_invalidate"].ainvoke(
            {
                "task_id": status["task_id"],
                "continuation_token": status["continuation_token"],
                "steps": [repair_target],
                "reason": "Replace incorrect candidate",
            }
        )
        assert status["invalidated_steps"] == [repair_target]
        assert Path(original_path).is_file()
        status = await tools["task_recover"].ainvoke({"task_id": status["task_id"]})
        assert "source" in status["assets"]
        layers = (await tools["layer_info"].ainvoke({}))["layers"]
        if repair_target == "bad":
            assert status["assets"]["projected"]["path"] == original_path
            assert layers == []
        else:
            assert "projected" not in status["assets"]
            assert len(layers) == 2
            assert {layer["id"] for layer in layers} == {
                status["assets"][name]["layer_id"] for name in ("bad", "later_layer")
            }
        assert (
            coordinator.store.db.execute(
                "SELECT count(*) FROM attempts WHERE step_id='independent'"
            ).fetchone()[0]
            == 1
        )
    finally:
        await coordinator.close()


async def test_final_failure_can_replace_same_logical_deliverable_and_preserve_upstream(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator)}
        status = await tools["task_begin"].ainvoke(
            {
                "goal": "Create projected synthetic points",
                "deliverables": [
                    {"id": "points", "description": "Projected points", "kind": "vector"}
                ],
            }
        )
        check = {
            "id": "crs",
            "kind": "crs",
            "target": "points",
            "expected": "EPSG:3857",
            "source": "user_requirement",
            "basis": "Projected output",
            "evidence": ["goal"],
        }
        status = await tools["task_contract_submit"].ainvoke(
            {
                "task_id": status["task_id"],
                "continuation_token": status["continuation_token"],
                "contract": {
                    "requirements": {"projected": "EPSG:3857 output"},
                    "coverage": {"projected": ["crs"]},
                    "checks": [check],
                },
            }
        )
        status, _, _ = await step(
            tools,
            status,
            "upstream",
            "vector_data",
            {**points(), "name": "Upstream"},
            [{"id": "upstream", "kind": "vector", "binding": "layer"}],
        )
        status, _, _ = await step(
            tools,
            status,
            "bad",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        old_path = status["assets"]["points"]["path"]
        status, _, _ = await step(
            tools,
            status,
            "downstream",
            "layers",
            {"action": "rename", "layer": "asset:points", "name": "Wrong output"},
            inputs=["points"],
        )
        failed = await tools["task_validate"].ainvoke({"task_id": status["task_id"]})
        assert not failed["passed"]
        status = await tools["task_invalidate"].ainvoke(
            {
                "task_id": status["task_id"],
                "continuation_token": status["continuation_token"],
                "steps": ["bad"],
                "reason": "Final CRS check failed",
            }
        )
        assert set(status["invalidated_steps"]) == {"bad", "downstream"}
        before_repeat = coordinator.status(include_details=True)
        with pytest.raises(TaskError) as repeated_repair:
            await tools["task_invalidate"].ainvoke({
                "task_id": status["task_id"],
                "continuation_token": status["continuation_token"],
                "steps": ["bad"], "reason": "Repeat rollback after it already invalidated the step",
            })
        assert repeated_repair.value.payload["code"] == "INVALID_REPAIR"
        hint = repeated_repair.value.payload["evidence"]["steps"][0]
        assert hint["step_id"] == "bad" and hint["status"] == "INVALIDATED"
        assert "step_contract_submit" in hint["next_action"] and "repairs_step" in hint["next_action"]
        before_repeat['correction_budget']['rejected_submissions'] = 1
        assert coordinator.status(include_details=True) == before_repeat
        assert "points" not in status["assets"] and "upstream" in status["assets"]
        assert Path(old_path).is_file()
        assert len((await tools["layer_info"].ainvoke({}))["layers"]) == 1
        status = await tools["task_recover"].ainvoke({"task_id": status["task_id"]})
        status, _, _ = await step(
            tools,
            status,
            "corrected",
            "vector_data",
            {**points(), "crs": "EPSG:3857"},
            [{"id": "points", "kind": "vector", "binding": "layer"}],
            repairs_step="bad",
        )
        assert status["assets"]["points"]["path"] != old_path
        status, _, _ = await step(
            tools, status, 'verified_label', 'layers',
            {'action': 'rename', 'layer': 'asset:points', 'name': 'Projected points'},
            inputs=['points'], repairs_step='downstream',
        )
        result = await tools["task_finish"].ainvoke(
            {"task_id": status["task_id"], "continuation_token": status["continuation_token"]}
        )
        assert result["status"] == "COMPLETED"
        assert coordinator.store.task()["contract_version"] == 1
        # A later independent review may identify a defect after task_finish.
        # Explicit repair reopens completion, preserves the contract and upstream,
        # and requires a new successful validation before completion again.
        reopened = await tools['task_invalidate'].ainvoke({
            'task_id': result['task_id'], 'continuation_token': result['continuation_token'],
            'steps': ['corrected'], 'reason': 'Rebuild after independent review',
        })
        assert reopened['status'] == 'READY'
        assert coordinator.store.task()['contract_version'] == 1
        assert set(reopened['invalidated_steps']) == {'corrected', 'verified_label'}
        assert 'points' not in reopened['assets'] and 'upstream' in reopened['assets']
        with pytest.raises(TaskError):
            await tools['task_finish'].ainvoke({
                'task_id': reopened['task_id'], 'continuation_token': reopened['continuation_token'],
            })
        status, _, _ = await step(
            tools, reopened, 'rebuilt', 'vector_data', {**points(), 'crs': 'EPSG:3857'},
            [{'id': 'points', 'kind': 'vector', 'binding': 'layer'}], repairs_step='corrected',
        )
        status, _, _ = await step(
            tools, status, 'rebuilt_label', 'layers',
            {'action': 'rename', 'layer': 'asset:points', 'name': 'Projected points'},
            inputs=['points'], repairs_step='verified_label',
        )
        result = await tools['task_finish'].ainvoke({
            'task_id': status['task_id'], 'continuation_token': status['continuation_token'],
        })
        assert result['status'] == 'COMPLETED'
        assert coordinator.store.task()['contract_version'] == 1
        assert (
            coordinator.store.db.execute(
                "SELECT count(*) FROM attempts WHERE step_id='upstream'"
            ).fetchone()[0]
            == 1
        )
    finally:
        await coordinator.close()


async def test_error_after_database_commit_keeps_success_and_does_not_repeat(tmp_path):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools, status = await initialize(coordinator)
        original = coordinator.store.commit

        def response_lost(attempt_id):
            original(attempt_id)
            raise OSError("Injected failure after durable commit")

        coordinator.store.commit = response_lost
        status, result, request = await step(
            tools,
            status,
            "points",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        assert status["status"] == "READY"
        assert status["attempts"][0]["status"] == "COMMITTED"
        assert await tools["step_execute"].ainvoke(request) == result
        assert len((await tools["layer_info"].ainvoke({}))["layers"]) == 1
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
    finally:
        await coordinator.close()


@pytest.mark.parametrize("required_field", ["value", "missing"])
async def test_contract_revision_revalidates_durable_result_without_reexecution(
    tmp_path, required_field
):
    require_qgis()
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path)
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator)}
        status = await tools["task_begin"].ainvoke(
            {
                "goal": "Create points",
                "deliverables": [{"id": "points", "kind": "vector", "description": "Points"}],
            }
        )
        contract = {
            "requirements": {"points": "Readable points"},
            "checks": [readable("points", "vector")],
            "coverage": {"points": ["read_points"]},
        }
        status = await tools["task_contract_submit"].ainvoke(
            {
                "task_id": status["task_id"],
                "continuation_token": status["continuation_token"],
                "contract": contract,
            }
        )
        status, _, _ = await step(
            tools,
            status,
            "points",
            "vector_data",
            points(),
            [{"id": "points", "kind": "vector", "binding": "layer"}],
        )
        contract["checks"].append(
            {
                "id": "field",
                "kind": "fields",
                "target": "points",
                "names": [required_field],
                "source": "method_assumption",
                "basis": "Verify attribute availability",
                "evidence": ["data inspection"],
            }
        )
        status = await tools["task_contract_submit"].ainvoke(
            {
                "task_id": status["task_id"],
                "continuation_token": status["continuation_token"],
                "contract": contract,
                "reason": "Add explicit field validation",
            }
        )
        status = await tools["task_diagnose"].ainvoke({"task_id": status["task_id"]})
        assert "points" not in status["assets"]
        persisted = await tools["contract_get"].ainvoke({
            "task_id": status["task_id"], "step_id": "points",
        })
        assert persisted["status"] == "INVALIDATED"
        assert coordinator.store.task()["contract_version"] == 2
        current = await tools["contract_get"].ainvoke({"task_id": status["task_id"]})
        assert any(item["id"] == "field" for item in current["contract"]["checks"])
        # Direct mutation tools stay hidden; reading an old step does not re-authorize task_execute.
        assert "vector_data" not in tools
        with pytest.raises(TaskError) as invalidated_saved:
            await tools["step_execute"].ainvoke({
                "task_id": status["task_id"], "step_id": "points",
                "continuation_token": coordinator.issue_continuation("execute", "points"),
            })
        assert invalidated_saved.value.payload["code"] == "STEP_CONTRACT_REQUIRED"
        request = {
            "task_id": status["task_id"],
            "continuation_token": status["continuation_token"],
            "revalidate_steps": ["points"],
        }
        if required_field == "missing":
            with pytest.raises(TaskError, match="Required checks"):
                await tools["task_validate"].ainvoke(request)
            assert "points" not in coordinator.assets()
        else:
            result = await tools["task_validate"].ainvoke(request)
            assert result["passed"]
            assert "points" in coordinator.assets()
            assert coordinator.store.task()["contract_version"] == 2
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
    finally:
        await coordinator.close()


@pytest.mark.parametrize("pending_phase", [None, "RUNNING", "VALIDATING"])
async def test_explicit_input_revision_preserves_independent_project_and_history(tmp_path, pending_phase):
    require_qgis()
    source = tmp_path / "input.geojson"
    source.write_text(json.dumps(points()["geojson"]))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    try:
        tools, status = await initialize(coordinator, {"source": {"path": str(source), "kind": "vector"}})
        original_inputs = coordinator.store.task()["inputs"]
        status, _, _ = await step(tools, status, "independent", "vector_data", points(),
                                  [{"id": "stable", "kind": "vector", "binding": "layer"}])
        stable = status["assets"]["stable"]["path"]
        status, _, _ = await step(tools, status, "load", "load_data", {"path": "asset:source"},
                                  [{"id": "loaded", "kind": "vector", "binding": "layer"}], ["source"])
        loaded_layer_id = status["assets"]["loaded"]["layer_id"]
        pending_id = None
        if pending_phase:
            status = await tools["step_contract_submit"].ainvoke({
                "task_id": status["task_id"], "continuation_token": status["continuation_token"],
                "step_id": "pending", "contract": {
                    "operation": "layers", "arguments": {
                        "action": "rename", "layer": "asset:loaded", "name": "Uncommitted name"},
                    "inputs": ["loaded"], "reason": "Uncommitted mutation fixture",
                },
            })
            task = coordinator.store.task()
            attempt = coordinator.store.begin_attempt("pending", "pending-key", {"fixture": True},
                                                       task["contract_version"], task["state_version"])
            pending_id = attempt["attempt_id"]
            await coordinator.bridge.call("layers", {
                "action": "rename", "layer": loaded_layer_id,
                "name": "Uncommitted name",
            })
            if pending_phase == "VALIDATING":
                cp = await coordinator.make_checkpoint(Path(attempt["directory"]) / "checkpoint", coordinator.assets())
                coordinator.store.prepare_commit(pending_id, {"fixture": True}, cp)
            await coordinator.close()
            coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
            tools = {tool.name: tool for tool in build_tools(coordinator)}
        changed = points()["geojson"]
        changed["features"].append({"type": "Feature", "properties": {"value": 4},
                                    "geometry": {"type": "Point", "coordinates": [104, 34]}})
        source.write_text(json.dumps(changed))
        with pytest.raises(TaskError) as failure:
            await tools["task_recover"].ainvoke({"task_id": status["task_id"]})
        assert failure.value.payload["code"] == "INPUT_CHANGED"
        inspected = await tools["inspect_data"].ainvoke({"source": "asset:source", "include_fingerprint": True})
        current = await tools["task_diagnose"].ainvoke({"task_id": status["task_id"]})
        request = {"task_id": status["task_id"], "continuation_token": current["continuation_token"],
                   "expected_digests": {"source": original_inputs["source"]["fingerprint"]["digest"]},
                   "reason": "Source dataset now includes an additional observation"}
        with pytest.raises(TaskError) as stale:
            await tools["task_revise_inputs"].ainvoke(request)
        assert stale.value.payload["code"] == "INPUT_VERSION_CONFLICT"
        request["expected_digests"]["source"] = inspected["fingerprint"]["digest"]
        if pending_phase:
            before = coordinator.status(include_details=True)
            with pytest.raises(TaskError) as unresolved:
                await tools["task_revise_inputs"].ainvoke(request)
            assert unresolved.value.payload["code"] == "ATTEMPT_UNRESOLVED"
            assert coordinator.status(include_details=True) == before
            request["discard_uncommitted"] = True
        status = await tools["task_revise_inputs"].ainvoke(request)
        if pending_phase:
            assert coordinator.store.unresolved() == []
            discarded = coordinator.store.db.execute(
                "SELECT status,failure FROM attempts WHERE id=?", (pending_id,)
            ).fetchone()
            assert discarded["status"] == "FAILED"
            assert json.loads(discarded["failure"])["code"] == "INPUT_REVISION_INTERRUPTED"
            assert (coordinator.store.directory / "attempts" / pending_id).is_dir()
        assert coordinator.store.task()["contract_version"] == 1
        assert status["assets"]["stable"]["path"] == stable
        assert "loaded" not in status["assets"]
        assert len((await tools["layer_info"].ainvoke({}))["layers"]) == 1
        revised_event = json.loads(coordinator.store.db.execute(
            "SELECT body FROM events WHERE kind='INPUTS_REVISED'"
        ).fetchone()[0])
        assert revised_event["before"] == original_inputs
        assert revised_event["after"]["source"]["fingerprint"] == inspected["fingerprint"]
        status = await tools["task_recover"].ainvoke({"task_id": status["task_id"]})
        status, _, _ = await step(tools, status, "reload", "load_data", {"path": "asset:source"},
                                  [{"id": "loaded", "kind": "vector", "binding": "layer"}],
                                  ["source"], repairs_step="load")
        layers = (await tools["layer_info"].ainvoke({}))["layers"]
        assert len(layers) == 2
        assert sorted(layer["feature_count"] for layer in layers) == [3, 4]
        assert coordinator.store.db.execute(
            "SELECT count(*) FROM attempts WHERE step_id='independent'"
        ).fetchone()[0] == 1
    finally:
        await coordinator.close()


async def test_input_revision_before_contract_keeps_preparing_state(tmp_path):
    require_qgis()
    source = tmp_path / 'input.geojson'
    source.write_text(json.dumps(points()['geojson']))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / 'state')
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator)}
        status = await tools['task_begin'].ainvoke({
            'goal': 'Map updated observations', 'inputs': {'source': {'path': str(source), 'kind': 'vector'}},
            'deliverables': [{'id': 'map', 'kind': 'pdf', 'description': 'Map'}],
        })
        updated = points()['geojson']
        updated['features'][0]['properties']['value'] = 100
        source.write_text(json.dumps(updated))
        inspected = await tools['inspect_data'].ainvoke({'source': 'asset:source', 'include_fingerprint': True})
        revised = await tools['task_revise_inputs'].ainvoke({
            'task_id': status['task_id'], 'continuation_token': status['continuation_token'],
            'expected_digests': {'source': inspected['fingerprint']['digest']}, 'reason': 'Corrected observation',
        })
        assert revised['status'] == 'PREPARING'
        assert coordinator.store.task()['contract_version'] == 0
        assert coordinator.store.task()['goal'] == 'Map updated observations'
    finally:
        await coordinator.close()


async def test_unrelated_discarded_attempt_can_explicitly_retry_unchanged(tmp_path):
    require_qgis()
    changed_source, stable_source = tmp_path / "changed.geojson", tmp_path / "stable.geojson"
    for source in (changed_source, stable_source):
        source.write_text(json.dumps(points()["geojson"]))
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    try:
        tools, status = await initialize(coordinator, {
            "changed": {"path": str(changed_source), "kind": "vector"},
            "stable": {"path": str(stable_source), "kind": "vector"},
        })
        status = await tools["step_contract_submit"].ainvoke({
            "task_id": status["task_id"], "continuation_token": status["continuation_token"],
            "step_id": "pending", "contract": {
                "operation": "run_processing", "arguments": {
                    "algorithm": "native:reprojectlayer", "load_outputs": False,
                    "parameters": {"INPUT": "asset:stable", "TARGET_CRS": "EPSG:3857",
                                   "OUTPUT": "output:projected"},
                },
                "inputs": ["stable"], "outputs": [{"id": "projected", "kind": "vector", "binding": "OUTPUT"}],
                "reason": "Transform the unchanged independent input",
            },
        })
        task = coordinator.store.task()
        coordinator.store.begin_attempt("pending", "interrupted", {"fixture": True},
                                        task["contract_version"], task["state_version"])
        changed_source.write_text(json.dumps(points()["geojson"]) + "\n")
        inspected = await tools["inspect_data"].ainvoke({"source": "asset:changed", "include_fingerprint": True})
        status = await tools["task_diagnose"].ainvoke({"task_id": status["task_id"]})
        status = await tools["task_revise_inputs"].ainvoke({
            "task_id": status["task_id"], "continuation_token": status["continuation_token"],
            "expected_digests": {"changed": inspected["fingerprint"]["digest"]},
            "discard_uncommitted": True, "reason": "Accept input revision and quarantine pending work",
        })
        assert status["invalidated_steps"] == []
        assert coordinator.store.db.execute("SELECT status FROM steps WHERE id='pending'").fetchone()[0] == "FAILED"
        status = await tools["task_recover"].ainvoke({
            "task_id": status["task_id"], "continuation_token": status["continuation_token"],
            "retry_step": "pending", "reason": "Input revision is committed; this input and contract did not change",
        })
        result = await tools["step_execute"].ainvoke(status["next_call"]["arguments"])
        assert Path(result["assets"]["projected"]["path"]).is_file()
        assert coordinator.store.db.execute("SELECT status FROM steps WHERE id='pending'").fetchone()[0] == "COMMITTED"
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 2
    finally:
        await coordinator.close()


async def test_generic_mcp_client_discovers_corrects_executes_and_resumes(tmp_path):
    """Protocol-only client: no host-specific tools, prompts or reasoning runtime."""
    from contextlib import asynccontextmanager

    require_qgis()
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("internal_mcp_server.py"))],
        env={**os.environ, 'SMART_QGIS_STATE_DIR': str(tmp_path)},
    )

    @asynccontextmanager
    async def connect():
        async with stdio_client(params) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=120)) as session:
                await session.initialize()
                yield session

    async def call(session, name, arguments, error=None):
        response = await session.call_tool(name, arguments)
        body = json.loads(response.content[0].text)
        assert bool(response.isError) == (error is not None), body
        if error:
            assert body['code'] == error, body
        return body

    async with connect() as session:
        schemas = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
        assert 'vector_data' not in schemas
        assert 'step_prepare' in schemas
        assert set(schemas['step_execute']['required']) == {
            'task_id', 'step_id', 'continuation_token'
        }
        assert schemas['step_execute']['additionalProperties'] is False
        invalid_cases = [
            ('step_execute', {'task_id': 'private-test-marker'}),
            ('step_execute', {'task_id': 'task', 'step_id': 'step',
                              'expected_state_version': 'private-test-marker'}),
            ('task_contract_submit', {'task_id': 'task', 'continuation_token': 'token',
                                      'contract': {}, 'step_id': 'step'}),
            ('step_contract_submit', {'task_id': 'task', 'continuation_token': 'token',
                                      'step_id': 'step'}),
        ]
        for name, arguments in invalid_cases:
            invalid = await call(session, name, arguments, error='INVALID_ARGUMENTS')
            assert invalid['phase'] == 'arguments' and invalid['retryable'] is False
            assert 'private-test-marker' not in json.dumps(invalid)
            assert invalid['next_action']
            if invalid.get('correction_budget', {}).get('remaining') == 0:
                await call(session, 'task_update', {'question': 'Continue independent argument fixtures?', 'user_response': 'Yes.'})
        await call(session, 'task_update', {'question': 'Continue the independent schema fixtures?', 'user_response': 'Yes, test the next fixture.'})
        # Give schema-derived corrections for common local-model mistakes,
        # without repeating submitted paths, goals or invalid values.
        for fields, expected in [
            ({'inputs': {'dem': 'private-test-marker'}},
             {'expected_type': 'object', 'required_fields': ['path', 'kind'],
              'allowed_fields': ['kind', 'path']}),
            ({'deliverables': [{'id': 'map', 'kind': 'private-test-marker',
                                'description': 'Map'}]},
             {'allowed_values': ['vector', 'raster', 'project', 'image', 'pdf', 'style', 'template', 'layout']}),
        ]:
            invalid = await call(session, 'task_begin', {
                'goal': 'private-test-marker',
                'deliverables': [{'id': 'map', 'kind': 'pdf', 'description': 'Map'}],
                **fields,
            }, error='INVALID_ARGUMENTS')
            for key, value in expected.items():
                assert invalid['evidence'][key] == value
            assert 'private-test-marker' not in json.dumps(invalid)
        # Cross-field failures must explain the exact correction without echoing
        # values or arbitrary exception text (the old response was just value_error).
        rule_cases = [
            ('step_contract_submit', {
                'continuation_token': 'token', 'contract': {}, 'step_id': 'step',
                'inherit_required_checks': True,
             },
             'repair_inheritance', 'repairs_step'),
            ('task_recover', {'retry_step': 'step', 'reason': ''},
             'retry_context', 'nonempty reason'),
            ('task_validate', {'revalidate_steps': ['step']},
             'revalidation_version', 'continuation_token'),
            ('task_validate', {'revalidate_steps': ['step', 'step']},
             'revalidation_unique', 'duplicates'),
        ]
        await call(session, 'task_update', {'question': 'Continue the cross-field fixtures?', 'user_response': 'Yes.'})
        for name, fields, rule, hint in rule_cases:
            invalid = await call(session, name, {
                'task_id': 'private-test-marker', **fields,
            }, error='INVALID_ARGUMENTS')
            detail = invalid['evidence']['errors'][0]
            assert detail['rule'] == rule and hint in detail['message']
            if invalid.get('correction_budget', {}).get('remaining') == 0:
                await call(session, 'task_update', {'question': 'Continue the remaining fixtures?', 'user_response': 'Yes.'})
            assert 'private-test-marker' not in json.dumps(invalid)
        await call(session, 'task_update', {'question': 'Start the independent creation-budget fixture?', 'user_response': 'Yes.'})
        invalid = await call(session, 'task_begin', {
            'goal': 'private-test-marker',
            'deliverables': [{'id': 'same', 'kind': 'vector', 'description': 'private-test-marker'}] * 2,
        }, error='INVALID_ARGUMENTS')
        assert invalid['evidence']['errors'][0]['rule'] == 'task_asset_ids'
        assert 'private-test-marker' not in json.dumps(invalid)
        for _ in range(2):
            await call(session, 'task_begin', {'goal': 'Fixture', 'deliverables': []}, error='INVALID_ARGUMENTS')
        created = await call(session, 'task_begin', {
            'goal': 'Create points', 'deliverables': [{'id': 'points', 'kind': 'vector', 'description': 'Points'}],
        })
        assert created['status'] == 'PREPARING'
        # Malformed task declarations do not spend the algorithm-argument
        # correction budget or force a clarification loop.
        assert created['correction_budget']['rejected_submissions'] == 0
        await call(session, 'not_a_real_tool', {}, error='UNKNOWN_TOOL')
        catalog = await call(session, 'contract_help', {})
        assert 'validator_kinds' in catalog and 'task' not in catalog
        check_schema = await call(session, 'contract_help', {'kind': 'readable'})
        assert check_schema['additionalProperties'] is False
        # Continue the attached task. Reliable mode intentionally forbids replacing
        # an unfinished task merely to change wording around the same deliverable.
        status = created
        task_id = status['task_id']
        status = await call(session, 'task_contract_submit', {
            'task_id': task_id, 'continuation_token': status['continuation_token'],
            'contract': {'requirements': {'points': 'Readable points'},
                         'coverage': {'points': ['read_points']},
                         'checks': [readable('points', 'vector')]},
        })
        wrong = await call(session, 'step_contract_submit', {
            'task_id': task_id, 'continuation_token': status['continuation_token'],
            'step_id': 'wrong', 'contract': {
                'operation': 'native:buffer', 'arguments': {'INPUT': 'private-test-marker'},
                'reason': 'Exercise operation validation',
            },
        }, error='UNKNOWN_OPERATION')
        assert wrong['evidence']['processing_structure']['operation'] == 'run_processing'
        assert 'private-test-marker' not in json.dumps(wrong)
        status = await call(session, 'step_contract_submit', {
            'task_id': task_id, 'continuation_token': status['continuation_token'],
            'step_id': 'points', 'contract': {
                'operation': 'vector_data', 'arguments': points(),
                'reason': 'Create requested points',
                'outputs': [{'id': 'points', 'kind': 'vector', 'binding': 'layer'}],
            },
        })
        rejected = await call(session, 'vector_data', {**points(), 'task_id': task_id},
                              error='UNKNOWN_TOOL')
        assert rejected['next_action'] == 'tools/list'
        execution_context = status['next_call']['arguments']
        result = await call(session, 'step_execute', execution_context)
        assert Path(result['assets']['points']['path']).is_file()
        persisted_task = await call(session, 'contract_get', {'task_id': task_id})
        persisted_step = await call(session, 'contract_get', {'task_id': task_id, 'step_id': 'points'})
        assert persisted_task['contract']['requirements'] == {'points': 'Readable points'}
        assert persisted_step['contract']['operation'] == 'vector_data'
        assert persisted_step['contract']['arguments']['geojson'] == points()['geojson']
        before_inheritance = await call(session, 'task_diagnose', {'task_id': task_id})
        await call(session, 'step_contract_submit', {
            'task_id': task_id, 'continuation_token': before_inheritance['continuation_token'],
            'inherit_required_checks': True, 'step_id': 'bad_inheritance',
            'contract': {'operation': 'vector_data', 'arguments': points(),
                         'reason': 'Must not inherit from an active committed result',
                         'repairs_step': 'points'},
        }, error='INVALID_REPAIR')
        before_inheritance['correction_budget']['rejected_submissions'] = 1
        assert await call(session, 'task_diagnose', {'task_id': task_id}) == before_inheritance
        await call(session, 'contract_get', {'task_id': task_id, 'step_id': 'missing'}, error='UNKNOWN_STEP')
    # New MCP process reads committed state and returns the original idempotent result.
    async with connect() as session:
        status = await call(session, 'task_recover', {'task_id': task_id})
        restored_task = await call(session, 'contract_get', {'task_id': task_id})
        restored_step = await call(session, 'contract_get', {'task_id': task_id, 'step_id': 'points'})
        assert restored_task['contract'] == persisted_task['contract']
        assert restored_step['contract'] == persisted_step['contract']
        assert restored_step['status'] == 'COMMITTED'
        assert await call(session, 'step_execute', execution_context) == result
        assert len((await call(session, 'layer_info', {}))['layers']) == 1
        status = await call(session, 'task_finish', {
            'task_id': task_id, 'continuation_token': status['continuation_token']})
        assert status['status'] == 'COMPLETED'


async def test_processing_preflight_rejects_bad_parameters_and_questions_before_attempt(tmp_path):
    require_qgis()
    source = tmp_path/'points.geojson'
    source.write_text(json.dumps({'type':'FeatureCollection','features':[
        {'type':'Feature','properties':{},'geometry':{'type':'Point','coordinates':[0,0]}}
    ]}))
    co = TaskCoordinator(root=tmp_path/'state')
    try:
        tools, status = await initialize(co, {'points': {'path':str(source),'kind':'vector'}})
        proposal = {'operation':'run_processing','reason':'Buffer input points', 'inputs':['points'],
                    'arguments':{'algorithm':'native:buffer','parameters':{'INPUT':'asset:points','DISTANCE':'invalid-distance','OUTPUT':'output:buffer'}},
                    'outputs':[{'id':'buffer','kind':'vector','binding':'OUTPUT'}]}
        request = {'task_id':status['task_id'],'continuation_token':status['continuation_token'],
                   'step_id':'buffer','contract':proposal}
        with pytest.raises(TaskError) as caught:
            await tools['step_contract_submit'].ainvoke(request)
        assert caught.value.payload['code'] == 'INVALID_PARAMETERS'
        assert caught.value.payload['phase'] == 'preflight'
        assert co.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        assert co.store.db.execute('SELECT count(*) FROM steps').fetchone()[0] == 0
        proposal['arguments']['parameters']['DISTANCE'] = 1
        proposal['unresolved_questions'] = ['Does distance mean degrees or meters?']
        with pytest.raises(TaskError) as caught:
            await tools['step_contract_submit'].ainvoke(request)
        assert caught.value.payload['code'] == 'TASK_AMBIGUOUS'
        assert caught.value.payload['next_action'] == 'ask_user'
        assert co.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        proposal['unresolved_questions'] = []
        approved = await tools['step_contract_submit'].ainvoke(request)
        assert approved['next_call']['tool'] == 'step_execute'
        assert co.correction_failures() == 1
        assert co.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
    finally:
        await co.close()


async def test_worker_preflights_qgis_raster_expression_and_reports_raster_health(tmp_path):
    require_qgis()
    bridge = QgisBridge(120)
    try:
        created = await bridge.call("run_processing", {
            "algorithm": "native:createconstantrasterlayer",
            "parameters": {
                "EXTENT": "0,10,0,10 [EPSG:3857]",
                "TARGET_CRS": "EPSG:3857",
                "PIXEL_SIZE": 1,
                "NUMBER": 5,
                "OUTPUT": str(tmp_path / "constant.tif"),
            },
            "load_outputs": True,
        })
        summary = created["loaded_layers"][0]["raster_summary"]
        assert summary == {
            "bands": [{
                "band": 1,
                "nodata": None,
                "valid_percent": 100.0,
                "minimum": 5.0,
                "maximum": 5.0,
            }],
            "all_nodata": False,
            "statistics_approximate": False,
        }
        assert created["warnings"] == []
        from PIL import Image

        outlier_source = tmp_path / "outlier-source.tif"
        image = Image.new("L", (1024, 1024), 14)
        image.putpixel((1023, 1023), 37)
        image.save(outlier_source)
        outlier = await bridge.call("run_processing", {
            "algorithm": "gdal:translate",
            "parameters": {
                "INPUT": str(outlier_source),
                "OUTPUT": str(tmp_path / "outlier-copy.tif"),
            },
            "load_outputs": True,
        })
        outlier_summary = outlier["loaded_layers"][0]["raster_summary"]
        assert outlier_summary["statistics_approximate"] is False
        assert outlier_summary["bands"][0]["minimum"] == 14.0
        assert outlier_summary["bands"][0]["maximum"] == 37.0
        nodata = await bridge.call("run_processing", {
            "algorithm": "gdal:translate",
            "parameters": {
                "INPUT": created["loaded_layers"][0]["id"],
                "NODATA": 5,
                "OUTPUT": str(tmp_path / "all_nodata.tif"),
            },
            "load_outputs": True,
        })
        assert nodata["loaded_layers"][0]["raster_summary"]["all_nodata"] is True
        assert nodata["loaded_layers"][0]["raster_summary"]["bands"][0]["valid_percent"] == 0.0
        assert [item["code"] for item in nodata["warnings"]] == ["RASTER_ALL_NODATA"]
        with pytest.raises(WorkerError) as failure:
            await bridge.call("run_processing", {
                "algorithm": "qgis:rastercalculator",
                "parameters": {
                    "EXPRESSION": "'constant'@1 + 1",
                    "LAYERS": [created["loaded_layers"][0]["id"]],
                    "OUTPUT": str(tmp_path / "invalid.tif"),
                },
                "load_outputs": True,
            })
        assert failure.value.code == "INVALID_PARAMETERS"
        assert "double-quoted layer@band" in str(failure.value)
    finally:
        await bridge.close()


async def test_grass_processing_materializes_managed_raster_source(tmp_path):
    """GRASS must receive a QgsRasterLayer, not a bare managed file path."""
    require_qgis()
    bridge = QgisBridge(120)
    try:
        available = await bridge.call("algorithms", {
            "action": "list", "provider": "grass", "query": "r.neighbors",
        })
        if not any(item["id"] == "grass:r.neighbors" for item in available["algorithms"]):
            pytest.skip("Requires the QGIS GRASS Processing Provider")
        source = tmp_path / "constant.tif"
        await bridge.call("run_processing", {
            "algorithm": "native:createconstantrasterlayer",
            "parameters": {
                "EXTENT": "0,10,0,10 [EPSG:3857]",
                "TARGET_CRS": "EPSG:3857",
                "PIXEL_SIZE": 1,
                "NUMBER": 5,
                "OUTPUT": str(source),
            },
            "load_outputs": False,
        })
        result = await bridge.call("run_processing", {
            "algorithm": "grass:r.neighbors",
            "parameters": {
                "input": str(source), "method": 5, "size": 3,
                "output": str(tmp_path / "range.tif"),
            },
            "load_outputs": True,
        })
        assert result["loaded_layers"][0]["crs"] == "EPSG:3857"
        summary = result["loaded_layers"][0]["raster_summary"]
        assert not summary["all_nodata"]
        assert summary["bands"][0]["maximum"] == 0.0
    finally:
        await bridge.close()


async def test_compact_task_writes_declared_final_output_path(tmp_path):
    """A user-selected deliverable path is the actual artifact, not a post-task copy."""
    require_qgis()
    target = tmp_path / "desktop" / "constant.tif"
    coordinator = TaskCoordinator(QgisBridge(120), tmp_path / "state")
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator, compact=True)}
        started = await tools["task_start"].ainvoke({
            "goal": "Create one constant raster",
            "inputs": {},
            "deliverables": [{
                "id": "result", "kind": "raster", "description": "Constant raster",
                "path": str(target),
            }],
        })
        prepared = await tools["prepare_algorithm"].ainvoke({
            "task_id": started["task_id"],
            "algorithm": "native:createconstantrasterlayer",
            "outputs": {"OUTPUT": "result"},
            "parameters": {
                "EXTENT": "0,10,0,10 [EPSG:3857]",
                "TARGET_CRS": "EPSG:3857",
                "PIXEL_SIZE": 1,
                "NUMBER": 5,
            },
        })
        assert prepared["next_call"]["tool"] == "task_execute"
        completed = await tools["task_execute"].ainvoke({})
        assert completed["status"] == "COMPLETED"
        assert completed["assets"]["result"]["path"] == str(target)
        assert target.is_file() and target.stat().st_size > 0
    finally:
        await coordinator.close()
