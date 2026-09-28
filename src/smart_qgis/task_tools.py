"""Public typed task lifecycle tools; reasoning stays in the MCP host."""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from .contracts import Deliverable, Model, Nonempty, SafeId

AssetBinding = SafeId | Annotated[list[SafeId], Field(min_length=1)]

ARGUMENT_RULE_MESSAGES = {
    "task_asset_ids": "Input and deliverable IDs must be unique and disjoint",
    "repair_inheritance": "inherit_required_checks requires a step_contract with repairs_step",
    "retry_context": "retry_step requires continuation_token and a nonempty reason",
    "revalidation_version": "continuation_token is required when revalidating steps",
    "revalidation_unique": "revalidate_steps must not contain duplicates",
    "validator_help_selection": "Use one of structure, kind or kinds; kinds must contain distinct validator names",
}


def argument_rule_error(rule):
    return PydanticCustomError(rule, ARGUMENT_RULE_MESSAGES[rule])


class Input(Model):
    path: Nonempty
    kind: Literal["vector", "raster", "style"]


class RunInput(Model):
    path: Nonempty
    kind: Literal["vector", "raster", "style"] | None = Field(
        None,
        description="Optional hint. The service inspects vector/raster inputs and determines the actual kind",
    )


class RasterPresentationStyle(Model):
    """Optional, user-directed display of one logical raster layer."""

    mode: Literal["continuous", "mask"] = Field(
        "continuous", description="continuous for measured values; mask for one valid-cell category"
    )
    ramp: str | None = Field(None, description="Color-ramp name for continuous mode")
    color: str | None = Field(None, description="Single color for mask mode, e.g. #666666")
    label: str | None = Field(None, description="Reader-facing category label for mask mode")
    opacity: float = Field(1, ge=0, le=1, description="Layer opacity from 0 to 1")


class TaskBegin(Model):
    goal: Nonempty
    inputs: dict[SafeId, Input] = Field(
        default_factory=dict,
        description='Map logical IDs to objects, not path strings: {"dem":{"path":"/path/to/dem.tif","kind":"raster"}}',
    )
    deliverables: list[Deliverable] = Field(
        min_length=1,
        description='Use only id, kind, description: [{"id":"map","kind":"pdf","description":"Requested map"}]. Do not use name, type or path. Use kind="image" for PNG.',
    )

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.deliverables]
        if len(set(ids)) != len(ids) or set(ids).intersection(self.inputs):
            raise argument_rule_error("task_asset_ids")
        return self


class TaskRun(Model):
    """Compact task bootstrap; the service owns inspection and route selection."""

    goal: Nonempty = Field(description="The user's requested GIS outcome; preserve its scientific meaning")
    inputs: dict[SafeId, RunInput] = Field(
        default_factory=dict,
        description='Logical input IDs mapped to {"path":"/absolute/path"}; kind is optional and inspected by the service',
    )
    deliverables: list[Deliverable] = Field(
        min_length=1,
        description='Exact requested outputs. Each item uses only id, kind, description. id is a logical SafeId without a filename extension; PNG uses kind="image"',
    )
    contract: dict[str, Any] = Field(
        default_factory=dict,
        description="Only explicit user requirements beyond automatic basic checks; {} is normal",
    )
    layers: list[str] | None = Field(
        None,
        description="Optional logical map layer IDs, topmost first. They may be planned Processing outputs or context inputs; omit to map final outputs",
    )
    title: str | None = Field(None, description="Map title; defaults to the user goal")
    coordinate_crs: str | None = Field(
        None, description="Optional annotation CRS ID, e.g. EPSG:4326; omit for default EPSG:4326 longitude/latitude labels"
    )
    map_element_placement: Literal["auto", "inside", "outside"] = Field(
        "auto", description="Presentation choice for legend and scale bar; outside keeps a full thematic map frame"
    )
    style_layers: bool = Field(True, description="Apply standard styles before creating the final map")
    raster_ramp: str = Field("Viridis", description="Default color ramp for continuous rasters without a per-layer style")
    raster_styles: dict[SafeId, RasterPresentationStyle] = Field(
        default_factory=dict,
        description="Optional per-layer display keyed by logical raster ID; mode=mask draws all valid cells as one category",
    )
    dpi: int = Field(150, ge=72, le=600, description="Map export resolution in dots per inch")
    plan: list["PlannedAlgorithm"] | None = Field(
        None,
        description=(
            "Optional frozen Processing plan for controlled execution. Each entry uses "
            "an exact QGIS algorithm ID plus logical input/output bindings. The service "
            "validates every live parameter schema before execution."
        ),
    )

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.deliverables]
        if len(set(ids)) != len(ids) or set(ids).intersection(self.inputs):
            raise argument_rule_error("task_asset_ids")
        if self.plan and len({item.step_id for item in self.plan}) != len(self.plan):
            raise ValueError("Frozen plan step IDs must be unique")
        return self


class PlannedAlgorithm(Model):
    """One pre-decomposed Processing operation for a controlled execution task."""

    step_id: SafeId = Field(description="Unique logical step ID")
    algorithm: Nonempty = Field(description="Exact installed provider:algorithm ID")
    inputs: dict[Nonempty, AssetBinding] = Field(
        default_factory=dict,
        description="Layer/source parameter names mapped to existing logical asset IDs",
    )
    outputs: dict[Nonempty, SafeId] = Field(
        default_factory=dict,
        description="Destination parameter names mapped to new logical output IDs, never file paths",
    )
    parameters: dict[Nonempty, Any] = Field(
        default_factory=dict,
        description="Known non-layer values such as bands, numbers, enums, CRS and expressions",
    )
    load_outputs: bool = Field(True, description="Load produced spatial outputs into the QGIS project")


class TaskContinue(Model):
    continuation_token: Nonempty = Field(
        description="Opaque action token returned by task_start or the preceding task_execute_next; copy unchanged"
    )


class TaskReference(Model):
    task_id: SafeId = Field(description="Existing durable task ID returned by task_start; never invent or replace it")


class TaskContinuation(TaskReference):
    continuation_token: Nonempty = Field(
        description="Opaque token returned by the preceding task mutation; copy it unchanged",
    )


class TaskAnswer(TaskContinuation):
    question_id: SafeId = Field(description="Exact pending question ID returned by prepare_algorithm")
    answer: Any = Field(description="The user's actual value; never infer it")


class PrepareAlgorithm(TaskContinuation):
    """Prepare any installed QGIS Processing algorithm from registry metadata."""

    step_id: SafeId = Field(description="New unique logical step ID; reuse only when the service explicitly requests repair semantics")
    algorithm: Nonempty = Field(
        description="Exact installed provider:algorithm ID; never a display name or search phrase"
    )
    inputs: dict[Nonempty, AssetBinding] = Field(
        default_factory=dict,
        description=(
            "Only layer/source parameter names mapped to existing logical asset IDs; "
            "use a nonempty ID list only for multilayer parameters. Put bands such as BAND_A, "
            "numbers, enums, CRS and expressions in parameters, not inputs"
        ),
    )
    outputs: dict[Nonempty, SafeId] = Field(
        default_factory=dict,
        description=(
            "Algorithm destination parameter names mapped to logical output IDs. "
            "Values are SafeIds, never paths or TEMPORARY_OUTPUT. "
            "Raster/vector destinations become automatic working assets when they "
            "are not declared deliverables; ambiguous destination types must be "
            "declared in contract.intermediates."
        ),
    )
    parameters: dict[Nonempty, Any] = Field(
        default_factory=dict,
        description="Known non-layer values from the request, inspected data or exact live help (including band numbers); omit unknown required values so the service asks",
    )
    load_outputs: bool = Field(
        True,
        description="Keep true when a later step or final map needs the produced raster/vector in QGIS",
    )
    repairs_step: SafeId | None = Field(
        None, description="Failed or invalidated step ID to replace. Use a NEW step_id, retain required checks, and reuse its logical output ID",
    )


class TaskClarify(Model):
    task_id: SafeId | None = Field(
        None, description="Existing durable task ID; omit only when guidance was requested before a task existed"
    )
    continuation_token: str | None = Field(
        None, description="Current continuation token when a task already exists",
    )
    question: Nonempty = Field(description="The specific unresolved question presented to the user")
    user_response: Nonempty = Field(description="The user's actual answer; never invent an answer or use an autonomous retry explanation")
    map_layers: list[SafeId] | None = Field(
        None,
        min_length=1,
        description="Optional ordered logical map layer IDs from the user's actual guidance, topmost first. Rebuild an existing layout after recording this change.",
    )
    map_element_placement: Literal["inside", "outside"] | None = Field(
        None, description="Optional actual user guidance for moving legend and scale bar inside or outside the map frame"
    )
    map_coordinate_crs: str | None = Field(
        None, description="Optional actual user choice of coordinate annotation CRS; geographic means EPSG:4326"
    )
    map_raster_styles: dict[SafeId, RasterPresentationStyle] | None = Field(
        None, description="Optional actual user guidance for per-layer raster display; does not change analysis results",
    )

    @model_validator(mode="after")
    def unique_map_layers(self):
        if self.map_layers and len(set(self.map_layers)) != len(self.map_layers):
            raise ValueError("map_layers must not contain duplicates")
        return self


class ExecuteStep(TaskContinuation):
    step_id: SafeId
    include_details: bool = Field(
        False,
        description="Include full validation evidence; default returns only check IDs and status",
    )


class ContractGet(TaskReference):
    step_id: SafeId | None = Field(
        None, description="Omit for the locked task contract; provide a step ID to read its persisted contract"
    )


class TaskStatus(TaskReference):
    include_details: bool = Field(
        False, description="Include full checkpoint fingerprints and attempt failures for diagnosis"
    )


class InspectData(Model):
    source: Nonempty = Field(description="Existing absolute local file or asset:<logical_id>")
    kind: Literal["vector", "raster"] | None = None
    include_fingerprint: bool = False
    include_details: bool = Field(
        False,
        description=(
            "Use only when the compact result lacks a specific value needed for diagnosis; "
            "includes verbose provider, field and CRS-WKT metadata"
        ),
    )


class TaskContractSubmit(TaskContinuation):
    contract: dict[str, Any] = Field(
        default_factory=dict,
        description="Only explicit user requirements beyond automatic basic checks; {} is valid",
    )
    reason: str = ""


RecipeAction = Literal[
    "load",
    "clip_raster",
    "clip_vector",
    "reproject_vector",
    "style_raster",
    "style_vector",
    "create_layout",
    "export_map",
    "save_project",
]


class StepPrepare(TaskContinuation):
    """Small intent surface compiled into a full StepContract by the service."""

    step_id: SafeId
    action: RecipeAction = Field(
        description=(
            "Recipe: load(source,output); clip_raster(raster,mask,output); "
            "clip_vector(source,overlay,output); reproject_vector(source,target_crs,output); "
            "style_*(layer); create_layout(layers,output); "
            "export_map(layout,output); save_project(output)"
        )
    )
    source: str | None = Field(None, description="Existing input as asset:<logical_id>")
    raster: str | None = Field(None, description="Existing raster as asset:<logical_id>")
    mask: str | None = Field(None, description="Existing vector mask as asset:<logical_id>")
    overlay: str | None = Field(None, description="Existing vector overlay as asset:<logical_id>")
    layer: str | None = Field(None, description="Loaded project layer as asset:<logical_id>")
    layers: list[str] | None = Field(
        None, description="Loaded project layers as asset:<logical_id>, topmost first"
    )
    extent_layer: str | None = Field(
        None, description="Optional extent source as asset:<logical_id>"
    )
    layout: str | None = Field(None, description="Existing layout as asset:<logical_id>")
    output: SafeId | None = Field(
        None, description="New logical output ID without asset: or output: prefix"
    )
    target_crs: str | None = Field(None, description="User-selected target CRS")
    title: str | None = Field(None, description="Map title; defaults to the task goal")
    mode: Literal["continuous", "mask"] = "continuous"
    ramp: str = "Viridis"
    band: int = Field(1, ge=1)
    classes: int = Field(8, ge=2, le=256)
    color: str = "#4c78a8"
    label: str | None = None
    outline: str = "#202020"
    width: float = Field(0.4, ge=0, le=20)
    opacity: float = Field(1, ge=0, le=1)
    nodata: float | Literal["nan"] | None = None
    all_touched: bool = False
    coordinate_crs: str | None = None
    map_element_placement: Literal["auto", "inside", "outside"] = "auto"
    dpi: int = Field(150, ge=72, le=600)


class WorkflowRun(TaskContinuation):
    """Execute a common multi-step workflow with a checkpoint after every step."""

    workflow: Literal["standard_map_project"] = Field(
        description=(
            "Load selected task inputs, apply standard styles, create one complete map layout, "
            "export declared image/PDF deliverables and save declared project deliverables"
        )
    )
    layers: list[str] | None = Field(
        None,
        description=(
            "Optional task input assets as asset:<logical_id>, topmost first; "
            "defaults to every vector/raster task input with vectors above rasters"
        ),
    )
    title: str | None = Field(None, description="Map title; defaults to the task goal")
    coordinate_crs: str | None = Field(
        None, description="Optional annotation CRS ID, e.g. EPSG:4326; omit for default EPSG:4326 longitude/latitude labels"
    )
    map_element_placement: Literal["auto", "inside", "outside"] = "auto"
    style_layers: bool = Field(
        True, description="Apply the service's standard vector/raster styles before layout"
    )
    raster_ramp: str = "Viridis"
    raster_styles: dict[SafeId, RasterPresentationStyle] = Field(default_factory=dict)
    dpi: int = Field(150, ge=72, le=600)


class StepContractSubmit(TaskContinuation):
    step_id: SafeId
    contract: dict[str, Any] = Field(
        description="One GIS operation with its inputs, managed outputs and explicit extra checks",
    )
    inherit_required_checks: bool = False

    @model_validator(mode="after")
    def repair_inheritance_requires_source(self):
        if self.inherit_required_checks and not self.contract.get("repairs_step"):
            raise argument_rule_error("repair_inheritance")
        return self


class Resume(TaskReference):
    resume_cancelled: bool = Field(
        False, description="Set true only when the user explicitly asks to reopen a cancelled task"
    )
    retry_step: SafeId | None = Field(None, description="Explicitly re-arm a failed infrastructure attempt while preserving its contract")
    continuation_token: str | None = Field(
        None, description="Required only when retry_step is supplied; obtain it from task_recover",
    )
    reason: str = Field("", description="Required explanation only when retry_step is supplied")

    @model_validator(mode="after")
    def retry_requires_context(self):
        if self.retry_step and (not self.continuation_token or not self.reason.strip()):
            raise argument_rule_error("retry_context")
        return self

class TaskRepair(TaskContinuation):
    steps: list[SafeId] = Field(
        min_length=1, description="Committed producer steps with incorrect or unusable results"
    )
    reason: Nonempty = Field(description="Concrete evidence that the committed result is incorrect or unusable")


class TaskValidate(TaskReference):
    revalidate_steps: list[SafeId] = Field(
        default_factory=list,
        description="Previously committed, now invalidated result steps to revalidate in dependency order",
    )
    continuation_token: str | None = Field(
        None, description="Required only when revalidating saved steps",
    )
    include_details: bool = Field(
        False,
        description="Include full validation evidence; default returns only check IDs and status",
    )

    @model_validator(mode="after")
    def require_version_for_reuse(self):
        if len(set(self.revalidate_steps)) != len(self.revalidate_steps):
            raise argument_rule_error("revalidation_unique")
        if self.revalidate_steps and not self.continuation_token:
            raise argument_rule_error("revalidation_version")
        return self

class ReviseInputs(TaskContinuation):
    discard_uncommitted: bool = Field(False, description="Explicitly quarantine unresolved attempts when accepting changed inputs; never commit their results")
    expected_digests: dict[SafeId, str] = Field(min_length=1, description="Changed input IDs mapped to digests returned by inspect_data(include_fingerprint=true)")
    reason: Nonempty


ValidatorKind = Literal[
    "coordinate_units", "crs", "crs_valid", "external_review", "fields",
    "geometry_valid", "layout_content", "layout_layers", "legend_consistent",
    "nodata", "provenance", "raster_grid", "raster_mask", "raster_range",
    "raster_values", "readable", "spatial_overlap",
]


class ContractHelp(Model):
    structure: Literal["task", "step"] | None = Field(
        None,
        description=(
            "Fetch a contract structure only when needed: task for explicit extra requirements, "
            "step only when no recipe fits"
        ),
    )
    kind: ValidatorKind | None = Field(
        None,
        description="Validator kind, e.g. raster_mask; omit for contract structure and catalog",
    )
    kinds: list[ValidatorKind] | None = Field(
        None, min_length=1, max_length=8,
        description="Fetch up to eight selected validator schemas in one call to avoid repeated model turns",
    )

    @model_validator(mode="after")
    def distinct_selection(self):
        selected = sum(value is not None for value in (self.structure, self.kind, self.kinds))
        if selected > 1 or (self.kinds and len(set(self.kinds)) != len(self.kinds)):
            raise argument_rule_error("validator_help_selection")
        return self


TASK_SPECS = [
    ("task_record_guidance", TaskClarify, "Record an actual user answer and reopen the bounded correction and repair windows. Ask the user first. Does not change locked acceptance requirements."),
    ("task_execute", ExecuteStep, "Execute the approved step. Copy task_id, step_id and continuation_token from step_prepare or step_contract_submit; do not repeat GIS parameters."),
    ("contract_get", ContractGet, "Read the persisted task contract or one step contract without changes. After task_recover, use this to recover requirements and approved arguments without relying on prior chat history. Does not authorize execution."),
    ("task_revise_inputs", ReviseInputs, "Explicitly accept inspected new versions of existing local inputs; invalidate dependent results, preserve independent files and immutable task requirements. Paths and kinds cannot change."),
    (
        "task_invalidate",
        TaskRepair,
        "Invalidate committed producer steps and their downstream closure, restore a safe project checkpoint, and free their logical output IDs for corrected steps. Preserves files and history; requires a reason and the latest continuation token. Link corrected producer contracts using repairs_step.",
    ),
    (
        "contract_help",
        ContractHelp,
        "Read task/advanced-step contract structures or selected validator schemas only when needed. Empty task contracts and common recipes need no contract help. Reuse returned schemas instead of repeating calls.",
    ),
    (
        "task_begin",
        TaskBegin,
        "Begin a durable task. Returns compact state and a prefilled empty task-contract call; fetch the task structure only for explicit extra requirements.",
    ),
    (
        "inspect_data",
        InspectData,
        "Inspect an input before contracts without changing the project. Use this for data inspection; load_data is a later mutation that requires an approved step.",
    ),
    (
        "task_contract_submit",
        TaskContractSubmit,
        "Submit only the task contract. Keep it empty unless the user explicitly requested checks beyond automatic basics.",
    ),
    (
        "step_prepare",
        StepPrepare,
        "Prepare a common GIS step from a small recipe. The service binds assets, managed outputs, defaults and basic checks, then returns a prefilled task_execute call. Prefer this over authoring a step contract.",
    ),
    (
        "workflow_run",
        WorkflowRun,
        "Run a standard map workflow in one call while retaining strict per-step contracts, validation and checkpoints. Prefer this when the requested deliverables are a map layout/export or editable QGIS project.",
    ),
    (
        "step_contract_submit",
        StepContractSubmit,
        "Submit only one step contract. Use asset:<id> inputs and output:<id> managed destinations.",
    ),
    (
        "task_diagnose",
        TaskStatus,
        "Inspect durable status, assets, planned steps, failures and recovery evidence.",
    ),
    (
        "task_checkpoint",
        TaskContinuation,
        "Verify current committed artifacts and persist a fresh checkpoint using the current continuation token.",
    ),
    (
        "task_validate",
        TaskValidate,
        "Evaluate task acceptance checks against actual artifacts; unverified required checks prevent completion.",
    ),
    (
        "task_recover",
        Resume,
        "Attach/recover a durable task after worker or MCP restart. Does not silently change input data or acceptance conditions.",
    ),
    (
        "task_finish",
        TaskContinuation,
        "Finish only after all required task checks and deliverable checks pass; copy the current continuation token.",
    ),
]


COMPACT_TASK_SPECS = [
    (
        "task_start",
        TaskRun,
        "First mutation for a new request. Declare exact logical inputs and deliverables once; use contract={} unless the user added acceptance checks. The service inspects inputs and returns either a Processing route or an exact task_execute_next handle.",
    ),
    (
        "task_execute_next",
        TaskContinue,
        "Execute the exact server-bound next action. Pass only the latest opaque continuation_token unchanged; do not repeat task_id, step_id or GIS parameters. Continue until COMPLETED or a structured question/error is returned.",
    ),
    (
        "task_answer",
        TaskAnswer,
        "Record the user's actual answer to a required question. Repeating the same saved answer safely resumes interrupted preparation; a different answer is rejected. Never invent an answer.",
    ),
    (
        "task_record_guidance",
        TaskClarify,
        "Record an actual user reply after a timeout or correction limit, optionally including explicit map choices. This releases the same task for recovery; never invent the question or response.",
    ),
    (
        "task_invalidate",
        TaskRepair,
        "Invalidate an incorrect committed result and dependent steps without deleting evidence. Then use prepare_algorithm with repairs_step to replace the producer and reuse its output ID. Do not call this for an uncommitted failed step.",
    ),
    (
        "task_recover",
        Resume,
        "Reattach an existing task, verify inputs/checkpoint and reconcile interrupted attempts after restart. May change recovery state; use task_diagnose for read-only diagnosis. After a timeout, record user guidance first.",
    ),
    (
        "task_diagnose",
        TaskStatus,
        "Read-only status for an existing task: returns durable state, assets, planned steps, attempts and the last error. It never resumes, retries or mutates; use task_recover for continuation.",
    ),
    (
        "prepare_algorithm",
        PrepareAlgorithm,
        "Prepare one Processing step by exact installed provider:algorithm ID. Put only layer/source bindings in inputs, destinations in outputs, and known scalar/band/enum/CRS/expression values in parameters. Live help, normalization and native preflight run automatically; unknown required semantic values return structured questions.",
    ),
]
