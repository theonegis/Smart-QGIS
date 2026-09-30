"""Agent-authored task/step contracts; no executable code or arbitrary validators."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contract_policy import MINIMUM_ACCEPTANCE_POLICY
from .task_store import TaskError

SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
Nonempty = Annotated[str, Field(min_length=1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


MapElementFrame = Literal["inside", "outside"]
MapElementAnchor = Literal[
    "top_left", "top", "top_right", "left", "center", "right",
    "bottom_left", "bottom", "bottom_right",
]


class MapElementPlacement(Model):
    """One optional, user-directed placement constraint for a map item."""

    frame: MapElementFrame | None = Field(
        None, description="inside the map frame, outside it on the page, or null for server choice"
    )
    anchor: MapElementAnchor | None = Field(
        None, description="Nine-position anchor or null for server choice"
    )

    @model_validator(mode="after")
    def meaningful_outside_anchor(self):
        if self.frame == "outside" and self.anchor == "center":
            raise ValueError("outside map elements cannot use the center anchor")
        return self


class LegendPlacement(MapElementPlacement):
    flow: Literal["auto", "horizontal", "vertical"] = Field(
        "auto", description="Legend item flow; auto follows available map/page space"
    )
    border: bool | None = Field(
        None, description="Show or hide the legend border; null keeps the server default"
    )


class ScaleBarPlacement(MapElementPlacement):
    units: Literal["auto", "meters", "kilometers", "miles"] = Field(
        "auto", description="Scale-bar units; auto chooses readable metric units"
    )
    style: Literal["single_box", "double_box", "line_ticks_middle"] = Field(
        "single_box", description="Common QGIS scale-bar style"
    )


class MapElements(Model):
    """Independent placement preferences. Omitted fields are server-resolved."""

    legend: LegendPlacement | None = None
    scalebar: ScaleBarPlacement | None = None
    north_arrow: MapElementPlacement | None = None
    title: MapElementPlacement | None = None

    @model_validator(mode="after")
    def distinct_explicit_positions(self):
        occupied = {}
        for name in ("legend", "scalebar", "north_arrow", "title"):
            item = getattr(self, name)
            if item and item.frame and item.anchor:
                key = (item.frame, item.anchor)
                if key in occupied:
                    raise ValueError(
                        f"{name} and {occupied[key]} cannot use the same explicit map position"
                    )
                occupied[key] = name
        return self


class MapFrame(Model):
    """How the server should size the thematic map frame and its page."""

    mode: Literal["auto", "maximize"] = Field(
        "maximize",
        description="auto chooses a compact page from the data footprint; maximize (default) makes the map frame use the available page width or height",
    )
    min_page_coverage: float | None = Field(
        None, ge=0.2, le=0.9,
        description="Optional user-required minimum fraction of page area occupied by the map frame",
    )


class CoordinateAnnotations(Model):
    """Coordinate labels belong to map-frame sides rather than free page anchors."""

    sides: list[Literal["top", "bottom", "left", "right"]] | None = Field(
        None, min_length=1,
        description="Annotated map-frame sides, or null for server choice",
    )
    format: Literal["auto", "decimal", "degree_minute", "degree_minute_second"] = Field(
        "auto",
        description="Coordinate label format; degree formats require a geographic annotation CRS",
    )
    precision: int | None = Field(
        None, ge=0, le=6,
        description="Optional label precision; omit for the server default",
    )
    cardinal_directions: bool | None = Field(
        None,
        description="For geographic labels, show or hide E/W/N/S; null keeps the server default",
    )
    density: Literal["auto", "dense", "sparse"] = Field(
        "auto", description="Common coordinate tick/label density preset"
    )
    grid_lines: bool = Field(
        False, description="Draw interior grid lines in addition to frame ticks and labels"
    )

    @model_validator(mode="after")
    def distinct_sides(self):
        if self.sides and len(set(self.sides)) != len(self.sides):
            raise ValueError("coordinate annotation sides must not contain duplicates")
        return self


class CheckBase(Model):
    id: SafeId
    target: SafeId = Field(description="Declared input, output or layout logical ID")
    source: Literal["user_requirement", "algorithm_rule", "method_assumption"]
    basis: Nonempty = Field(
        description="Reason this check applies; quote user requirements accurately"
    )
    evidence: list[Nonempty] = Field(min_length=1)
    required: bool = True


class Readable(CheckBase):
    kind: Literal["readable"]
    data_kind: Literal["vector", "raster", "project", "image", "pdf", "style", "template"]


class CRS(CheckBase):
    kind: Literal["crs"]
    expected: Nonempty


class ValidCRS(CheckBase):
    kind: Literal["crs_valid"]


class Units(CheckBase):
    kind: Literal["coordinate_units"]
    expected: Literal["meters", "degrees", "projected"]


class RasterRange(CheckBase):
    kind: Literal["raster_range"]
    minimum: float
    maximum: float | None = None
    minimum_valid_pixels: int = Field(1, ge=0)
    scope: Literal["full", "sample"] = "full"
    sample_size: int = Field(1225, ge=1, le=100000)
    seed: int = Field(0, ge=0)

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("raster_range maximum must be at least minimum")
        return self


class Fields(CheckBase):
    kind: Literal["fields"]
    names: list[Nonempty] = Field(min_length=1)


class Overlap(CheckBase):
    kind: Literal["spatial_overlap"]
    reference: SafeId


class RasterGrid(CheckBase):
    kind: Literal["raster_grid"]
    reference: SafeId
    tolerance: float = Field(ge=0, description="Explicit coordinate-unit absolute tolerance")
    match_extent: bool = False


class NoData(CheckBase):
    kind: Literal["nodata"]
    band: int = Field(1, ge=1)
    value: float | Literal["nan"]


class Geometry(CheckBase):
    kind: Literal["geometry_valid"]


class Provenance(CheckBase):
    kind: Literal["provenance"]
    inputs: list[SafeId] = Field(min_length=1)


class LayoutLayers(CheckBase):
    kind: Literal["layout_layers"]
    layers: list[SafeId] = Field(
        min_length=1,
        description="Exact ordered layout layer IDs, topmost first; not an unordered membership check. The server injects this check from layout arguments. Do not duplicate it unless the user explicitly requires an exact order.",
    )


class Legend(CheckBase):
    kind: Literal["legend_consistent"]


class LayoutContent(CheckBase):
    """Explicit structural requirements; exported appearance still needs review."""

    kind: Literal["layout_content"]
    map_item: Nonempty = "main-map"
    texts: list[Nonempty] = Field(default_factory=list, description="Exact visible plain label texts")
    require_title: bool = False
    require_legend: bool = False
    require_scalebar: bool = False
    require_north_arrow: bool = False
    require_grid: bool = False
    grid_crs: Nonempty | None = Field(None, description="Require an enabled annotated grid in this CRS")
    element_placements: dict[Literal["legend", "scalebar", "north_arrow", "title"], dict[str, str | None]] = Field(
        default_factory=dict,
        description="Explicit per-element frame/anchor placements to verify",
    )
    min_page_coverage: float | None = Field(
        None, ge=0.2, le=0.9,
        description="Optional minimum fraction of page area occupied by the main map frame",
    )

    @model_validator(mode="after")
    def meaningful(self):
        if not (
            self.texts
            or self.require_title
            or self.require_legend
            or self.require_scalebar
            or self.require_north_arrow
            or self.require_grid
            or self.grid_crs is not None
            or self.element_placements
            or self.min_page_coverage is not None
        ):
            raise ValueError(
                "layout_content requires a title, legend, text, scale bar or coordinate grid"
            )
        return self


class RasterMask(CheckBase):
    kind: Literal["raster_mask"]
    reference: SafeId
    source_raster: SafeId | None = Field(
        None, description="Aligned original raster for checking missing valid pixels inside mask"
    )
    boundary_rule: Literal["pixel_center", "all_touched"]
    geometry_model: Literal["transformed_vertices", "original_crs"] = Field(
        "transformed_vertices",
        description="transformed_vertices: transform polygon vertices to raster CRS (GDAL rasterization); original_crs: inverse-transform pixel centers and use strict interior containment in original boundary CRS",
    )

    @model_validator(mode="after")
    def compatible_geometry_model(self):
        if self.geometry_model == "original_crs" and self.boundary_rule != "pixel_center":
            raise ValueError("original_crs requires pixel_center; exact boundary points are outside")
        return self

    scope: Literal["full", "sample"] = "sample"
    sample_size: int = Field(1225, ge=1, le=100000)
    seed: int = Field(0, ge=0)


class RasterValues(CheckBase):
    kind: Literal["raster_values"]
    reference: SafeId
    absolute_tolerance: float = Field(ge=0)
    relative_tolerance: float = Field(ge=0)
    scope: Literal["full", "sample"] = "sample"
    sample_size: int = Field(1225, ge=1, le=100000)
    seed: int = Field(0, ge=0)


class ExternalReview(CheckBase):
    kind: Literal["external_review"]
    question: Nonempty


Check = Annotated[
    Readable
    | CRS
    | ValidCRS
    | Units
    | RasterRange
    | Fields
    | Overlap
    | RasterGrid
    | NoData
    | Geometry
    | Provenance
    | LayoutLayers
    | Legend
    | LayoutContent
    | RasterMask
    | RasterValues
    | ExternalReview,
    Field(discriminator="kind"),
]


class Deliverable(Model):
    id: SafeId
    description: Nonempty
    kind: Literal["vector", "raster", "project", "image", "pdf", "style", "template", "layout"]
    path: str | None = Field(
        None,
        description=(
            "Optional absolute final file path. Omit to keep this file in the system temporary "
            "Smart-QGIS output directory. Mutually exclusive with directory."
        ),
    )
    directory: str | None = Field(
        None,
        description=(
            "Optional absolute final output directory. The service derives the filename from id and "
            "kind. Mutually exclusive with path."
        ),
    )

    @model_validator(mode="after")
    def normalize_destination(self):
        if self.path and self.directory:
            raise ValueError("Use either path or directory for a deliverable, not both")
        if self.kind == "layout" and (self.path or self.directory):
            raise ValueError("A layout is not a file deliverable and cannot have a path or directory")
        for field in ("path", "directory"):
            raw = getattr(self, field)
            if raw is None:
                continue
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                raise ValueError(f"deliverable {field} must be an absolute path")
            candidate = candidate.resolve()
            if field == "path" and candidate.name in {"", ".", ".."}:
                raise ValueError("deliverable path must name a file")
            setattr(self, field, str(candidate))
        return self


def check_static_conflicts(checks):
    """Reject definite same-state contradictions, without guessing CRS equivalence."""
    nodata, units = {}, {}
    for check in checks:
        if not check.required:
            continue
        if check.kind == "nodata":
            key = (check.target, check.band)
            previous = nodata.get(key)
            if previous is not None and previous.value != check.value:
                raise ValueError(f"Conflicting required NoData checks: {previous.id}, {check.id}")
            nodata[key] = check
        elif check.kind == "coordinate_units":
            prior = units.setdefault(check.target, {})
            opposite = {"meters": "degrees", "degrees": "meters"}.get(check.expected)
            if opposite in prior:
                raise ValueError(f"Conflicting required coordinate units: {prior[opposite]}, {check.id}")
            prior[check.expected] = check.id


class TaskContract(Model):
    intermediates: dict[SafeId, Literal["vector", "raster", "project", "image", "pdf", "style", "template", "layout"]] = Field(
        default_factory=dict,
        description="Logical intermediate assets referenced by final checks, e.g. map_layout: layout; produced by later step contracts",
    )
    requirements: dict[SafeId, Nonempty] = Field(
        default_factory=dict,
        description="Explicit user requirements beyond automatic basics",
    )
    checks: list[Check] = Field(
        default_factory=list,
        description="Checks for requirements above",
    )
    coverage: dict[SafeId, list[SafeId]] = Field(
        default_factory=dict,
        description="Optional audit map from requirement IDs to required check IDs; may be omitted"
    )
    map_omissions: list[Literal["title", "legend", "scalebar", "north_arrow", "coordinates"]] = Field(
        default_factory=list,
        description="Map elements the user explicitly asked to remove",
    )
    assumptions: list[Nonempty] = Field(default_factory=list)
    unresolved_questions: list[Nonempty] = Field(
        default_factory=list,
        description="Safety backstop; ask before submission. Nonempty blocks approval",
    )

    @model_validator(mode="after")
    def check_coverage(self):
        self.map_omissions = sorted(set(self.map_omissions))
        check_static_conflicts(self.checks)
        checks = {check.id: check for check in self.checks}
        if len(checks) != len(self.checks):
            raise ValueError("Check IDs must be unique")
        if self.coverage:
            if set(self.coverage) != set(self.requirements):
                raise ValueError("Coverage must address exactly the declared requirements")
            for references in self.coverage.values():
                if not references or any(
                    ref not in checks or not checks[ref].required for ref in references
                ):
                    raise ValueError("Each requirement must reference existing required checks")
        return self


class Output(Model):
    id: SafeId
    kind: Literal["vector", "raster", "project", "image", "pdf", "style", "template", "layout"]
    binding: Nonempty = Field(
        description="layer for layer creators; layout for layouts; OUTPUT for processing; path for exports"
    )
    filename: str | None = Field(
        None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        description="Basename only; service assigns the actual attempt directory",
    )


class StepContract(Model):
    operation: Nonempty = Field(description="MCP GIS tool name, e.g. run_processing. A QGIS algorithm ID belongs in arguments.algorithm, with parameters in arguments.parameters.")
    arguments: dict[str, Any]
    inputs: list[SafeId] = Field(default_factory=list)
    dependencies: list[SafeId] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    preconditions: list[Check] = Field(default_factory=list)
    postconditions: list[Check] = Field(default_factory=list)
    resampling: bool = False
    reason: Nonempty
    repairs_step: SafeId | None = None
    unresolved_questions: list[Nonempty] = Field(
        default_factory=list,
        description="Safety backstop; ask before submission. Nonempty blocks approval",
    )

    @model_validator(mode="after")
    def check_consistency(self):
        output_ids = [output.id for output in self.outputs]
        if len(set(output_ids)) != len(output_ids):
            raise ValueError("Output IDs must be unique")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Dependencies must be unique")
        if set(self.inputs).intersection(output_ids):
            raise ValueError("Outputs must not replace inputs")
        checks = [*self.preconditions, *self.postconditions]
        if len({check.id for check in checks}) != len(checks):
            raise ValueError("Step check IDs must be unique")
        if self.resampling and any(check.kind == "raster_values" for check in checks):
            raise ValueError("Source pixel equality is inappropriate when resampling")
        check_static_conflicts(self.preconditions)
        check_static_conflicts(self.postconditions)
        return self


def validate_task_contract(contract, deliverables, inputs, previous=None):
    """Deterministic coverage and anti-weakening checks; not an LLM intent oracle."""
    if contract.unresolved_questions:
        raise TaskError(
            "TASK_AMBIGUOUS",
            "Resolve material questions before executing",
            evidence={"questions": contract.unresolved_questions},
            next_action="ask_user",
        )
    known = set(inputs) | {item.id for item in deliverables}
    collisions = known.intersection(contract.intermediates)
    if collisions:
        raise TaskError("ASSET_COLLISION", "Intermediate IDs must not shadow inputs or deliverables",
                        evidence={"assets": sorted(collisions)})
    known.update(contract.intermediates)
    for check in contract.checks:
        references = {check.target}
        reference = getattr(check, "reference", None)
        if reference:
            references.add(reference)
        if getattr(check, "source_raster", None):
            references.add(check.source_raster)
        references.update(getattr(check, "inputs", []))
        references.update(getattr(check, "layers", []))
        if not references <= known:
            raise TaskError(
                "UNKNOWN_ASSET",
                "Check references undeclared logical assets",
                evidence={"assets": sorted(references - known)},
                next_action="Declare intermediate IDs and kinds in task_contract.intermediates, or correct the reference to an existing input/deliverable",
            )
    if previous:
        if contract.map_omissions != previous.map_omissions:
            raise TaskError(
                "REQUIREMENT_IMMUTABLE",
                "Explicit map-element omissions cannot be added, removed or rewritten",
            )
        for key, kind in previous.intermediates.items():
            if contract.intermediates.get(key) != kind:
                raise TaskError("CONTRACT_WEAKENING", "Existing intermediate declarations cannot be changed")
        # Keep all earlier requirements/checks. Revisions can add obligations, not remove them.
        for key, requirement in previous.requirements.items():
            if contract.requirements.get(key) != requirement:
                raise TaskError(
                    "REQUIREMENT_IMMUTABLE", "Existing requirements cannot be rewritten"
                )
        current_checks = {check.id: check.model_dump() for check in contract.checks}
        for check in previous.checks:
            if check.required and current_checks.get(check.id) != check.model_dump():
                raise TaskError("CONTRACT_WEAKENING", "Existing required checks cannot be changed")


def verifier_catalog(kind=None):
    """On-demand schemas keep all validators out of every model tool declaration."""
    schema = TaskContract.model_json_schema()
    validators = {
        definition["properties"]["kind"]["const"]: definition
        for definition in schema.get("$defs", {}).values()
        if "const" in definition.get("properties", {}).get("kind", {})
    }
    if kind is not None:
        if kind not in validators:
            raise TaskError(
                "UNKNOWN_VALIDATOR",
                "Choose a registered validator kind",
                evidence={"available": sorted(validators)},
            )
        return validators[kind]

    def structure(model):
        result = model.model_json_schema()
        result.pop("$defs", None)
        for field in ("checks", "preconditions", "postconditions"):
            if field in result["properties"]:
                result["properties"][field]["items"] = {
                    "type": "object",
                    "description": "Use contract_help(kind=...) for exact check schema",
                }
        if "outputs" in result["properties"]:
            result["properties"]["outputs"]["items"] = Output.model_json_schema()
        return result

    return {
        "task": structure(TaskContract),
        "step": structure(StepContract),
        "validators": sorted(validators),
        "instructions": "Reuse this structure; fetch only needed validators with kind/kinds. Empty task contract is valid for automatic basics. " + MINIMUM_ACCEPTANCE_POLICY,
    }
