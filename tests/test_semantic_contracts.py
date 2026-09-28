import copy

import pytest
from pydantic import ValidationError

from smart_qgis.contracts import (
    Deliverable,
    StepContract,
    TaskContract,
    validate_task_contract,
    verifier_catalog,
)
from smart_qgis.task_store import TaskError


def body():
    return {
        "requirements": {"pdf": "Export a readable PDF map"},
        "coverage": {"pdf": ["read_pdf"]},
        "checks": [
            {
                "id": "read_pdf",
                "kind": "readable",
                "target": "map",
                "data_kind": "pdf",
                "source": "user_requirement",
                "basis": "User requests PDF",
                "evidence": ["goal"],
            }
        ],
    }


def deliverables():
    return [Deliverable(id="map", description="PDF map", kind="pdf")]


def test_deliverable_rejects_removed_display_name_field():
    with pytest.raises(ValidationError):
        Deliverable.model_validate(
            {"id": "map", "name": "Map output", "description": "PDF map", "kind": "pdf"}
        )


def test_agent_contract_is_structured_and_validates_optional_coverage():
    contract = TaskContract.model_validate(body())
    validate_task_contract(contract, deliverables(), {})
    assert "discriminator" in str(TaskContract.model_json_schema())
    assert "readable" in verifier_catalog()["validators"]
    bad = body()
    bad["coverage"] = {}
    assert TaskContract.model_validate(bad).coverage == {}
    bad = body()
    bad["coverage"] = {"wrong_requirement": ["read_pdf"]}
    with pytest.raises(ValidationError, match="Coverage"):
        TaskContract.model_validate(bad)
    bad = body()
    bad["checks"][0]["required"] = False
    with pytest.raises(ValidationError, match="required checks"):
        TaskContract.model_validate(bad)

    # Basic output existence, readability/type and spatial CRS validity are
    # server checks, so a task with no additional user constraint needs no
    # synthetic requirement/check/coverage boilerplate.
    empty = TaskContract.model_validate({})
    validate_task_contract(empty, deliverables(), {})
    assert empty.requirements == empty.coverage == {}
    assert empty.checks == []


def test_unknown_validator_and_executable_code_are_rejected():
    bad = body()
    bad["checks"][0]["kind"] = "python"
    with pytest.raises(ValidationError):
        TaskContract.model_validate(bad)
    bad = body()
    bad["checks"][0]["code"] = "print('not executed')"
    with pytest.raises(ValidationError, match="Extra inputs"):
        TaskContract.model_validate(bad)


def test_basic_deliverables_need_no_duplicate_contract_check_but_unknown_assets_fail():
    contract = TaskContract.model_validate(body())
    validate_task_contract(
        contract,
        [*deliverables(), Deliverable(id="dem", description="Clipped DEM", kind="raster")],
        {},
    )
    changed = body()
    changed["checks"][0]["target"] = "imagined"
    with pytest.raises(TaskError, match="undeclared"):
        validate_task_contract(TaskContract.model_validate(changed), deliverables(), {})


def test_ai_cannot_weaken_required_checks_or_rewrite_user_requirements():
    original = TaskContract.model_validate(body())
    revised = copy.deepcopy(body())
    revised["checks"][0]["data_kind"] = "image"
    with pytest.raises(TaskError, match="cannot be changed"):
        validate_task_contract(TaskContract.model_validate(revised), deliverables(), {}, original)
    revised = body()
    revised["requirements"]["pdf"] = "Any image will do"
    with pytest.raises(TaskError, match="cannot be rewritten"):
        validate_task_contract(TaskContract.model_validate(revised), deliverables(), {}, original)


def test_material_questions_block_execution_but_external_review_is_explicit():
    changed = body()
    changed["unresolved_questions"] = ["Which area does the user mean?"]
    with pytest.raises(TaskError, match="Resolve"):
        validate_task_contract(TaskContract.model_validate(changed), deliverables(), {})
    changed = body()
    changed["checks"].append(
        {
            "id": "visual",
            "kind": "external_review",
            "target": "map",
            "source": "method_assumption",
            "basis": "Layout clarity",
            "evidence": ["goal"],
            "question": "Are labels legible?",
        }
    )
    contract = TaskContract.model_validate(changed)
    assert contract.checks[-1].kind == "external_review"
    assert contract.checks[-1].required


@pytest.mark.parametrize("kind,values", [("nodata", [-9999, "nan"]),
                                        ("coordinate_units", ["meters", "degrees"])])
def test_conflicting_required_checks_rejected_but_transformation_phases_allowed(kind, values):
    checks = [{"id": f"check{n}", "kind": kind, "target": "dem",
               "source": "method_assumption", "basis": "Test constraint", "evidence": ["fixture"],
               ("value" if kind == "nodata" else "expected"): value}
              for n, value in enumerate(values)]
    contract = {"requirements": {"dem": "Required result"}, "checks": checks,
                "coverage": {"dem": ["check0", "check1"]}}
    with pytest.raises(ValidationError, match="Conflicting required"):
        TaskContract.model_validate(contract)
    step = {"operation": "run_processing", "arguments": {}, "reason": "Transform data",
            "inputs": ["dem"], "preconditions": checks}
    with pytest.raises(ValidationError, match="Conflicting required"):
        StepContract.model_validate(step)
    step["preconditions"], step["postconditions"] = checks[:1], checks[1:]
    StepContract.model_validate(step)  # A transformation may intentionally change this property.
    optional = copy.deepcopy(contract)
    optional["checks"][1]["required"] = False
    optional["coverage"] = {"dem": ["check0"]}
    TaskContract.model_validate(optional)
    if kind == "nodata":
        different_band = copy.deepcopy(contract)
        different_band["checks"][1]["band"] = 2
        TaskContract.model_validate(different_band)


def test_reversed_raster_bounds_rejected_without_inventing_a_tolerance():
    check = {"id": "range", "kind": "raster_range", "target": "dem", "minimum": 10,
             "maximum": 9, "source": "method_assumption", "basis": "Value range", "evidence": ["fixture"]}
    contract = {"requirements": {"dem": "Elevation bounds"}, "checks": [check],
                "coverage": {"dem": ["range"]}}
    with pytest.raises(ValidationError, match="maximum must be at least minimum"):
        TaskContract.model_validate(contract)
    check["maximum"] = 10
    TaskContract.model_validate(contract)


def test_resampling_conflicts_with_source_pixel_equality():
    step = {
        "operation": "run_processing",
        "arguments": {},
        "reason": "reproject",
        "resampling": True,
        "postconditions": [
            {
                "kind": "raster_values",
                "id": "values",
                "target": "out",
                "reference": "in",
                "source": "algorithm_rule",
                "basis": "pixels unchanged",
                "evidence": ["algorithm"],
                "absolute_tolerance": 0,
                "relative_tolerance": 0,
            }
        ],
    }
    with pytest.raises(ValidationError, match="resampling"):
        StepContract.model_validate(step)


def test_output_paths_cannot_escape_managed_directory():
    step = {
        "operation": "export_map",
        "arguments": {},
        "reason": "Export",
        "outputs": [{"id": "map", "kind": "pdf", "binding": "path", "filename": "../map.pdf"}],
    }
    with pytest.raises(ValidationError):
        StepContract.model_validate(step)


def test_final_checks_can_reference_declared_intermediate_layout():
    original = body()
    original['intermediates'] = {'map_layout': 'layout'}
    original['checks'].append({
        'id': 'legend', 'kind': 'legend_consistent', 'target': 'map_layout',
        'source': 'user_requirement', 'basis': 'Map legend must match rendered layers',
        'evidence': ['goal'],
    })
    contract = TaskContract.model_validate(original)
    validate_task_contract(contract, deliverables(), {})
    changed = copy.deepcopy(original)
    changed['intermediates']['map_layout'] = 'raster'
    with pytest.raises(TaskError, match='intermediate'):
        validate_task_contract(TaskContract.model_validate(changed), deliverables(), {}, contract)
    changed = copy.deepcopy(original)
    changed['intermediates']['map'] = 'layout'
    with pytest.raises(TaskError, match='shadow'):
        validate_task_contract(TaskContract.model_validate(changed), deliverables(), {})
    del original['intermediates']
    with pytest.raises(TaskError) as failure:
        validate_task_contract(TaskContract.model_validate(original), deliverables(), {})
    assert 'intermediates' in failure.value.payload['next_action']


def test_layout_content_requires_explicit_expectations():
    from smart_qgis.contracts import LayoutContent, verifier_catalog

    common = {'id': 'content', 'kind': 'layout_content', 'target': 'map_layout',
              'source': 'user_requirement', 'basis': 'Required title', 'evidence': ['goal']}
    with pytest.raises(ValidationError, match='requires a title'):
        LayoutContent.model_validate(common)
    assert LayoutContent.model_validate({**common, 'texts': ['Required title']}).texts == ['Required title']
    defaults = LayoutContent.model_validate(
        {
            **common,
            'require_title': True,
            'require_legend': True,
            'require_scalebar': True,
            'require_grid': True,
        }
    )
    assert defaults.require_title and defaults.require_legend and defaults.require_grid
    assert 'grid_crs' in verifier_catalog('layout_content')['properties']


def test_explicit_map_omissions_are_locked_requirements():
    original = TaskContract.model_validate({'map_omissions': ['legend']})
    validate_task_contract(original, deliverables(), {})
    revised = TaskContract.model_validate({'map_omissions': ['legend', 'scalebar']})
    with pytest.raises(TaskError, match='omissions'):
        validate_task_contract(revised, deliverables(), {}, original)


@pytest.mark.parametrize('extra', ['-wo CUTLINE_ALL_TOUCHED=TRUE -overwrite',
                                  '-wo CUTLINE_ALL_TOUCHED=TRUE; echo x',
                                  '-wo CUTLINE_ALL_TOUCHED=MAYBE',
                                  '-wo NUM_THREADS=ALL_CPUS', ['-wo'], True])
def test_clip_extra_arguments_remain_restricted(extra):
    from smart_qgis.algorithm_rules import clip_boundary_rule

    with pytest.raises(TaskError) as failure:
        clip_boundary_rule(extra)
    assert failure.value.payload['code'] == 'UNSUPPORTED_SIDE_EFFECT'
    assert clip_boundary_rule(None) == 'pixel_center'
    assert clip_boundary_rule('-wo CUTLINE_ALL_TOUCHED=FALSE') == 'pixel_center'
    assert clip_boundary_rule('-wo CUTLINE_ALL_TOUCHED=TRUE') == 'all_touched'


@pytest.mark.parametrize(
    'extra',
    [
        None,
        '',
        '-b 1',
        '-a_ullr 307049.5 3837890.25 398909.5 3763070.25',
        '-b 1 -a_ullr -120.5 45.25 -119.5 44.25',
        '-b 1 -b 3 -a_ullr 0 10 20 -10',
    ],
)
def test_translate_extra_accepts_only_band_and_numeric_georeferencing(extra):
    from smart_qgis.algorithm_rules import gdal_translate_extra

    assert gdal_translate_extra(extra) is None


@pytest.mark.parametrize(
    'extra',
    [
        '-b 0',
        '-b one',
        '-a_ullr 0 1 2',
        '-a_ullr 0 1 two 3',
        '-a_ullr 0 1 inf 3',
        '-a_ullr 0 1 2 3 -a_ullr 4 5 6 7',
        '-of VRT',
        '-co TILED=YES',
        '@/tmp/options.txt',
        '-b 1; touch /tmp/unmanaged',
        ['-b', '1'],
        True,
    ],
)
def test_translate_extra_rejects_unknown_or_non_scalar_arguments(extra):
    from smart_qgis.algorithm_rules import gdal_translate_extra

    with pytest.raises(TaskError):
        gdal_translate_extra(extra)


def test_original_crs_boundary_policy_cannot_use_all_touched_or_be_revised_away():
    contract = body()
    contract['checks'].append({
        'id': 'mask', 'kind': 'raster_mask', 'target': 'dem', 'reference': 'boundary',
        'boundary_rule': 'pixel_center', 'geometry_model': 'original_crs',
        'source': 'user_requirement', 'basis': 'Original boundary containment', 'evidence': ['goal'],
    })
    original = TaskContract.model_validate(contract)
    contract['checks'][-1]['boundary_rule'] = 'all_touched'
    with pytest.raises(ValidationError, match='requires pixel_center'):
        TaskContract.model_validate(contract)
    contract['checks'][-1]['boundary_rule'] = 'pixel_center'
    contract['checks'][-1]['geometry_model'] = 'transformed_vertices'
    with pytest.raises(TaskError, match='cannot be changed'):
        validate_task_contract(TaskContract.model_validate(contract), deliverables(),
                               {'dem': {}, 'boundary': {}}, original)
