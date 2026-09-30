"""Public typed task lifecycle tools; reasoning stays in the MCP host."""

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from .contracts import (
    CoordinateAnnotations,
    Deliverable,
    MapElements,
    MapFrame,
    Model,
    Nonempty,
    SafeId,
)

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

    mode: Literal["continuous", "mask", "gray", "rgb", "hillshade", "qml"] = Field(
        "continuous",
        description=(
            "continuous or mask for a pseudocolor legend; gray for one-band grayscale; "
            "rgb for a three-band composite; hillshade for relief display"
        ),
    )
    ramp: str | None = Field(None, description="Color-ramp name for continuous mode")
    color: str | None = Field(None, description="Single color for mask mode, e.g. #666666")
    label: str | None = Field(None, description="Reader-facing category label for mask mode")
    band: int = Field(1, ge=1, description="One-based band for continuous, mask, gray or hillshade")
    red: int | None = Field(None, ge=1, description="One-based red band for rgb mode")
    green: int | None = Field(None, ge=1, description="One-based green band for rgb mode")
    blue: int | None = Field(None, ge=1, description="One-based blue band for rgb mode")
    minimum: float | None = Field(None, description="Optional fixed display minimum")
    maximum: float | None = Field(None, description="Optional fixed display maximum")
    classes: int = Field(8, ge=2, le=256, description="Pseudocolor class count")
    azimuth: float = Field(315, ge=0, le=360, description="Hillshade illumination azimuth")
    altitude: float = Field(45, ge=0, le=90, description="Hillshade illumination altitude")
    z_factor: float = Field(1, gt=0, description="Hillshade vertical exaggeration")
    qml_asset: SafeId | None = Field(None, description="Declared style input ID for qml mode")
    opacity: float = Field(1, ge=0, le=1, description="Layer opacity from 0 to 1")

    @model_validator(mode="after")
    def renderer_parameters(self):
        if self.minimum is not None and self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("raster style maximum must be at least minimum")
        if self.mode == "rgb" and None in (self.red, self.green, self.blue):
            raise ValueError("rgb mode requires red, green and blue band numbers")
        if self.mode == "qml" and not self.qml_asset:
            raise ValueError("qml mode requires qml_asset")
        return self


class VectorRule(Model):
    expression: Nonempty = Field(description="Verified QGIS filter expression")
    label: Nonempty
    color: str = "#4c78a8"
    outline: str = "#202020"
    width: float = Field(0.4, ge=0, le=20)
    size: float = Field(2, gt=0, le=100)


class VectorPresentationStyle(Model):
    """Point, line and polygon presentation requested by the user."""

    renderer: Literal["single", "categorized", "graduated", "rule_based", "qml"] = Field(
        "single", description="Renderer family; the required field must be supplied for categorized or graduated"
    )
    color: str = "#4c78a8"
    outline: str = "#202020"
    width: float = Field(0.4, ge=0, le=20)
    size: float = Field(2, gt=0, le=100)
    opacity: float = Field(1, ge=0, le=1)
    category_field: str | None = None
    categories: list[dict[str, Any]] | None = None
    graduated_field: str | None = None
    ramp: str = "Viridis"
    classes: int = Field(5, ge=2, le=100)
    method: Literal["equal_interval", "quantile", "jenks"] = "equal_interval"
    rules: list[VectorRule] | None = Field(None, min_length=1)
    marker: Literal["circle", "square", "triangle", "diamond", "cross", "cross2"] = "circle"
    line_style: Literal["solid", "dash", "dot", "dash_dot", "dash_dot_dot"] = "solid"
    qml_asset: SafeId | None = Field(None, description="Declared style input ID for qml renderer")
    label_field: str | None = None

    @model_validator(mode="after")
    def renderer_parameters(self):
        if self.renderer == "categorized" and (
            not self.category_field or not self.categories
        ):
            raise ValueError("categorized vector style requires category_field and categories")
        if self.renderer == "graduated" and not self.graduated_field:
            raise ValueError("graduated vector style requires graduated_field")
        if self.renderer == "rule_based" and not self.rules:
            raise ValueError("rule_based vector style requires rules")
        if self.renderer == "qml" and not self.qml_asset:
            raise ValueError("qml vector style requires qml_asset")
        return self


class MapService(Model):
    """A named remote map layer; the server owns provider-specific QGIS URIs."""

    provider: Literal["openstreetmap", "xyz", "wms", "wmts", "wfs"] = Field(
        "openstreetmap",
        description="openstreetmap needs no URL; other services require the user-supplied service URL",
    )
    url: str | None = Field(
        None,
        description="Exact XYZ template or service endpoint; never guess it",
    )
    uri: str | None = Field(
        None,
        description="Optional complete QGIS provider URI supplied by the user; mutually exclusive with url",
    )
    name: str | None = Field(None, description="Optional reader-facing layer name")
    attribution: str | None = Field(None, description="Optional attribution supplied by the service provider")
    role: Literal["basemap", "overlay"] | None = Field(
        None, description="basemap stays below thematic data and never controls map extent; WFS defaults to overlay"
    )
    layer_name: str | None = Field(None, description="Exact WMS/WMTS layer name")
    type_name: str | None = Field(None, description="Exact WFS feature type name")
    style_name: str | None = Field(None, description="Optional exact server style name")
    crs: str | None = Field(None, description="Optional service CRS requested by the user")
    image_format: str = Field("image/png", description="WMS/WMTS image MIME type")
    version: str | None = Field(None, description="Optional service protocol version")
    authcfg: str | None = Field(None, description="Existing QGIS authentication configuration ID")
    zmin: int = Field(0, ge=0, le=30)
    zmax: int = Field(19, ge=0, le=30)

    @model_validator(mode="after")
    def service_url(self):
        if self.url and self.uri:
            raise ValueError("Use either a service url or a complete provider uri, not both")
        if self.provider != "openstreetmap" and not (self.url or self.uri):
            raise ValueError("A non-OSM map service requires its exact URL or provider URI")
        if self.provider in {"wms", "wmts"} and self.url and not self.layer_name:
            raise ValueError(f"{self.provider} requires the exact layer_name when url is used")
        if self.provider == "wfs" and self.url and not self.type_name:
            raise ValueError("wfs requires the exact type_name when url is used")
        if self.zmax < self.zmin:
            raise ValueError("zmax must be at least zmin")
        if self.role is None:
            self.role = "overlay" if self.provider == "wfs" else "basemap"
        return self


class LayerOperation(Model):
    """One explicit operation on task-owned or exactly identified project layers."""

    action: Literal["rename", "remove", "visibility", "opacity", "order", "group"]
    layer: str | None = Field(None, description="Logical layer ID or exact ID/name returned by project_info")
    name: str | None = Field(None, description="New layer name or group name")
    visible: bool | None = None
    opacity: float | None = Field(None, ge=0, le=1)
    order: list[Nonempty] | None = Field(None, min_length=1, description="Requested topmost-first subset")
    layers: list[Nonempty] | None = Field(None, min_length=1, description="Layers moved into a group")

    @model_validator(mode="after")
    def required_fields(self):
        if self.action in {"rename", "remove", "visibility", "opacity"} and not self.layer:
            raise ValueError(f"{self.action} requires layer")
        if self.action == "rename" and not self.name:
            raise ValueError("rename requires name")
        if self.action == "visibility" and self.visible is None:
            raise ValueError("visibility requires visible")
        if self.action == "opacity" and self.opacity is None:
            raise ValueError("opacity requires opacity")
        if self.action == "order" and not self.order:
            raise ValueError("order requires a nonempty order")
        if self.action == "group" and (not self.name or not self.layers):
            raise ValueError("group requires name and layers")
        return self


class FeatureUpdate(Model):
    feature_id: int = Field(ge=0, description="Exact feature ID obtained from data_info")
    attributes: dict[Nonempty, Any] = Field(min_length=1)


class VectorDataOperation(Model):
    """Copy-on-write vector creation, editing or export."""

    action: Literal["export", "create", "edit"]
    layer: str | None = Field(None, description="Logical layer ID or exact loaded layer ID/name")
    output: SafeId = Field(description="Declared vector deliverable ID")
    crs: str | None = Field(None, description="Output CRS; omit to preserve the source CRS")
    selected_only: bool = False
    expression: str | None = Field(None, description="Optional QGIS filter or delete expression")
    geojson: dict[str, Any] | None = Field(None, description="FeatureCollection for create or additions")
    geojson_crs: str = Field("EPSG:4326", description="CRS of supplied GeoJSON coordinates")
    updates: list[FeatureUpdate] = Field(default_factory=list)
    name: str | None = Field(None, description="Optional reader-facing output layer name")

    @model_validator(mode="after")
    def operation_parameters(self):
        if self.action in {"export", "edit"} and not self.layer:
            raise ValueError(f"{self.action} requires layer")
        if self.action == "create" and (
            not self.geojson or self.geojson.get("type") != "FeatureCollection"
        ):
            raise ValueError("create requires a GeoJSON FeatureCollection")
        if self.action == "edit" and not (self.expression or self.geojson or self.updates):
            raise ValueError("edit requires a delete expression, additions or attribute updates")
        return self


class ProjectUpdate(Model):
    """Non-destructive changes to the current managed project."""

    title: str | None = None
    crs: str | None = Field(None, description="Project display CRS; this does not reproject source data")

    @model_validator(mode="after")
    def meaningful(self):
        if self.title is None and self.crs is None:
            raise ValueError("project_update requires title or crs")
        return self


class FeatureQuery(Model):
    action: Literal["sample", "statistics"] = "sample"
    expression: str | None = Field(None, description="Optional QGIS filter expression")
    limit: int = Field(10, ge=1, le=100)
    field: str | None = Field(None, description="Existing numeric field for statistics")

    @model_validator(mode="after")
    def statistics_field(self):
        if self.action == "statistics" and not self.field:
            raise ValueError("statistics requires field")
        return self


class ProjectRequest(Model):
    """Requested project lifecycle choice, kept inside the durable task."""

    action: Literal["current", "create", "open"] = Field(
        "current", description="Use current project, create a new project, or open an existing project"
    )
    path: str | None = Field(None, description="Absolute .qgz/.qgs path required to open a project")
    crs: str | None = Field(None, description="Project CRS for a newly created project; ask when required and unknown")
    title: str | None = Field(None, description="Optional project title for a newly created project")

    @model_validator(mode="after")
    def open_needs_path(self):
        if self.action == "open" and not self.path:
            raise ValueError("Opening a project requires its absolute path")
        if self.action == "open":
            candidate = Path(self.path).expanduser()
            if not candidate.is_absolute() or candidate.suffix.lower() not in {".qgz", ".qgs"}:
                raise ValueError("Project path must be an absolute .qgz or .qgs file")
            self.path = str(candidate.resolve())
        return self


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
        default_factory=list,
        description='Optional exact requested outputs. Omit for project/layer/service changes with no file output. id is a logical SafeId without an extension; PNG uses kind="image". Set optional absolute path for an exact final filename, or directory for a final folder; omit both for the system temporary output directory.',
    )
    overwrite_existing_outputs: bool = Field(
        False,
        description=(
            "Set true only when the user explicitly authorized replacing existing files at "
            "the exact declared deliverable paths. Do set it when the request says e.g. "
            "'overwrite if the same filename exists'; default false asks before replacement."
        ),
    )
    contract: dict[str, Any] = Field(
        default_factory=dict,
        description="Only explicit user requirements beyond automatic basic checks; {} is normal",
    )
    project: ProjectRequest = Field(
        default_factory=ProjectRequest,
        description="Optional project context. New and existing project actions remain journaled task operations.",
    )
    basemap: MapService | None = Field(
        None,
        description="Optional contextual map service. Use provider=openstreetmap for OSM; it never controls thematic extent.",
    )
    services: dict[SafeId, MapService] = Field(
        default_factory=dict,
        description=(
            "Optional named remote layers. OSM/XYZ/WMS/WMTS normally use role=basemap; "
            "WFS is a vector overlay. Exact endpoints, layer/type names and authcfg values must come from the user."
        ),
    )
    layer_operations: list[LayerOperation] = Field(
        default_factory=list,
        description="Optional ordered rename/remove/visibility/opacity/order/group changes",
    )
    data_operations: list[VectorDataOperation] = Field(
        default_factory=list,
        description="Optional copy-on-write vector create/edit/export operations",
    )
    project_update: ProjectUpdate | None = Field(
        None, description="Optional project title or display-CRS change after create/open"
    )
    layers: list[str] | None = Field(
        None,
        description="Optional logical map layer IDs, topmost first. They may be planned Processing outputs or context inputs; omit to map final outputs",
    )
    title: str | None = Field(None, description="Reader-facing map title; defaults to the user goal")
    legend_title: str | None = Field(
        None, description="Reader-facing legend heading; omit for a language-aware default"
    )
    map_language: Literal["auto", "zh", "en"] = Field(
        "auto",
        description=(
            "Language for server-generated map text such as a default legend heading. "
            "auto follows the original goal/title; zh or en forces the default language without translating user text."
        ),
    )
    show_legend_title: bool = Field(True, description="Whether to display the legend heading")
    north_arrow: bool = Field(False, description="Add a north arrow only when explicitly requested")
    page_orientation: Literal["auto", "portrait", "landscape"] = Field(
        "auto", description="Map page orientation; auto fits the thematic data footprint"
    )
    map_crs: str | None = Field(
        None,
        description="Optional map display CRS for on-the-fly projection; does not rewrite source data",
    )
    map_frame: MapFrame = Field(
        default_factory=MapFrame,
        description="Map-frame sizing; auto creates a compact page from the thematic data, maximize prioritizes map-frame occupancy",
    )
    coordinate_crs: str | None = Field(
        None, description="Optional annotation CRS ID, e.g. EPSG:4326; omit for default EPSG:4326 longitude/latitude labels"
    )
    map_elements: MapElements = Field(
        default_factory=MapElements,
        description="Optional independent legend, scale bar, north-arrow and title placements; omitted fields are server-resolved",
    )
    coordinate_annotations: CoordinateAnnotations = Field(
        default_factory=CoordinateAnnotations,
        description="Optional coordinate-label map-frame sides; omit for server choice",
    )
    style_layers: bool = Field(True, description="Apply standard styles before creating the final map")
    raster_ramp: str = Field("Viridis", description="Default color ramp for continuous rasters without a per-layer style")
    raster_styles: dict[SafeId, RasterPresentationStyle] = Field(
        default_factory=dict,
        description="Optional per-layer display keyed by logical raster ID; mode=mask draws all valid cells as one category",
    )
    vector_styles: dict[SafeId, VectorPresentationStyle] = Field(
        default_factory=dict,
        description="Optional point/line/polygon styles keyed by logical vector layer ID",
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
        collisions = set(self.services).intersection(self.inputs) | set(self.services).intersection(ids)
        if collisions:
            raise ValueError(f"service IDs collide with task assets: {sorted(collisions)}")
        declared = {item.id: item.kind for item in self.deliverables}
        for operation in self.data_operations:
            if declared.get(operation.output) != "vector":
                raise ValueError(
                    f"data operation output {operation.output!r} must be a declared vector deliverable"
                )
        for style in [*self.vector_styles.values(), *self.raster_styles.values()]:
            if style.qml_asset:
                declared_style = self.inputs.get(style.qml_asset)
                if declared_style is None or (
                    declared_style.kind not in {None, "style"}
                    or Path(declared_style.path).suffix.lower() != ".qml"
                ):
                    raise ValueError(
                        f"qml_asset {style.qml_asset!r} must name a declared QML style input"
                    )
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
    continuation_token: Nonempty = Field(description="Server-issued action handle")


class CompactTaskContinue(Model):
    """Token-free public shape; the compact service supplies the current action."""


class TaskReference(Model):
    task_id: SafeId = Field(description="Existing durable task ID returned by task_start; never invent or replace it")


class TaskContinuation(TaskReference):
    continuation_token: Nonempty = Field(description="Latest server-issued task state token")


class TaskAnswerFields(TaskReference):
    question_id: SafeId = Field(description="Exact pending question ID returned by prepare_algorithm")
    answer: Any = Field(description="The user's actual value; never infer it")


class TaskAnswer(TaskAnswerFields):
    continuation_token: Nonempty


class CompactTaskAnswer(TaskAnswerFields):
    """Public answer shape; the compact service supplies the task state token."""


class PrepareAlgorithmFields(TaskReference):
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


class PrepareAlgorithm(PrepareAlgorithmFields):
    continuation_token: Nonempty


class CompactPrepareAlgorithm(PrepareAlgorithmFields):
    """Public preparation shape; the compact service supplies task state."""


class SafePrepareAlgorithm(TaskReference):
    """Public Processing request without internal step or repair identifiers."""

    algorithm: Nonempty = Field(description="Exact installed provider:algorithm ID from algorithm_info")
    inputs: dict[Nonempty, AssetBinding] = Field(default_factory=dict)
    outputs: dict[Nonempty, SafeId] = Field(default_factory=dict)
    parameters: dict[Nonempty, Any] = Field(default_factory=dict)
    load_outputs: bool = True


class OutputConflictResolution(Model):
    path: Nonempty = Field(description="Exact absolute final-output path reported by OUTPUT_EXISTS")
    action: Literal["retry", "overwrite"] = Field(
        description="retry leaves files untouched; overwrite authorizes replacement of this exact existing file"
    )

    @model_validator(mode="after")
    def absolute_path(self):
        path = Path(self.path).expanduser()
        if not path.is_absolute():
            raise ValueError("output conflict path must be absolute")
        self.path = str(path.resolve())
        return self


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
    map_title: str | None = Field(
        None, description="Optional actual user revision of the reader-facing map title; rebuilds only presentation"
    )
    legend_title: str | None = Field(
        None, description="Optional actual user revision of the reader-facing legend heading; rebuilds only presentation"
    )
    map_language: Literal["auto", "zh", "en"] | None = Field(
        None, description="Optional actual user choice for server-generated map-text language; rebuilds only presentation"
    )
    show_legend_title: bool | None = Field(
        None, description="Optional actual user choice to show or hide the legend heading; rebuilds only presentation"
    )
    north_arrow: bool | None = Field(
        None, description="Optional actual user choice to add or remove the north arrow; rebuilds only presentation",
    )
    page_orientation: Literal["auto", "portrait", "landscape"] | None = Field(
        None, description="Optional actual user choice for the map page orientation; rebuilds only presentation"
    )
    map_crs: str | None = Field(
        None, description="Optional actual user choice of map display CRS; rebuilds only presentation"
    )
    map_elements: MapElements | None = Field(
        None, description="Optional actual user guidance for independent map-element positions; rebuild an existing layout after recording it",
    )
    map_frame: MapFrame | None = Field(
        None, description="Optional actual user guidance for map-frame sizing and page occupancy; rebuilds only presentation",
    )
    map_coordinate_annotations: CoordinateAnnotations | None = Field(
        None, description="Optional actual user guidance for coordinate-label map-frame sides",
    )
    map_coordinate_crs: str | None = Field(
        None, description="Optional actual user choice of coordinate annotation CRS; geographic means EPSG:4326"
    )
    map_raster_styles: dict[SafeId, RasterPresentationStyle] | None = Field(
        None, description="Optional actual user guidance for per-layer raster display; does not change analysis results",
    )
    map_vector_styles: dict[SafeId, VectorPresentationStyle] | None = Field(
        None, description="Optional actual user guidance for per-layer vector display"
    )
    services: dict[SafeId, MapService] | None = Field(None, min_length=1)
    layer_operations: list[LayerOperation] | None = Field(None, min_length=1)
    data_operations: list[VectorDataOperation] | None = Field(None, min_length=1)
    project_update: ProjectUpdate | None = None
    output_conflict: OutputConflictResolution | None = Field(
        None,
        description=(
            "Actual user decision for one existing final output. Use retry only after the user "
            "confirms the file was removed; use overwrite only after the user explicitly authorizes "
            "replacement of this exact path. Never infer either choice."
        ),
    )

    @model_validator(mode="after")
    def unique_map_layers(self):
        if self.map_layers and len(set(self.map_layers)) != len(self.map_layers):
            raise ValueError("map_layers must not contain duplicates")
        return self


class TaskUpdate(TaskReference):
    """User-requested change to a durable task, without internal step handles."""

    instruction: Nonempty = Field(
        description="The user's actual requested change or approval; do not invent it"
    )
    basemap: MapService | None = Field(
        None, description="Add or replace the contextual map service for the next map build"
    )
    services: dict[SafeId, MapService] | None = Field(
        None, min_length=1, description="Add named remote raster or WFS vector layers"
    )
    layer_operations: list[LayerOperation] | None = Field(
        None, min_length=1, description="Ordered layer rename/remove/visibility/opacity/order/group operations"
    )
    data_operations: list[VectorDataOperation] | None = Field(
        None, min_length=1, description="Copy-on-write vector create/edit/export operations"
    )
    project_update: ProjectUpdate | None = Field(
        None, description="Change the managed project title or display CRS"
    )
    map_layers: list[SafeId] | None = Field(None, min_length=1)
    map_title: str | None = None
    legend_title: str | None = None
    map_language: Literal["auto", "zh", "en"] | None = None
    show_legend_title: bool | None = None
    north_arrow: bool | None = None
    page_orientation: Literal["auto", "portrait", "landscape"] | None = None
    map_crs: str | None = None
    map_elements: MapElements | None = None
    map_frame: MapFrame | None = None
    map_coordinate_annotations: CoordinateAnnotations | None = None
    map_coordinate_crs: str | None = None
    map_raster_styles: dict[SafeId, RasterPresentationStyle] | None = None
    map_vector_styles: dict[SafeId, VectorPresentationStyle] | None = None
    output_conflict: OutputConflictResolution | None = Field(
        None,
        description=(
            "Required when the current task is blocked by OUTPUT_EXISTS. Copy the exact reported "
            "absolute path and set action=overwrite only after explicit approval, or action=retry "
            "only after the user removed that file. Prefer this structured decision; a clear actual "
            "user instruction such as '同名文件请直接覆盖' is also safely bound to the exact pending path."
        ),
    )

    @model_validator(mode="after")
    def meaningful_change(self):
        if self.map_layers and len(set(self.map_layers)) != len(self.map_layers):
            raise ValueError("map_layers must not contain duplicates")
        return self


class TaskRestart(TaskReference):
    scope: Literal["failed_operation", "analysis", "map"] = Field(
        "failed_operation",
        description="Restart the failed operation when safely possible, or rebuild analysis/map after explicit user instruction",
    )
    instruction: Nonempty = Field(description="The user's actual instruction authorizing this restart")


class TaskStop(TaskReference):
    reason: Nonempty = Field(description="The user's actual request to stop this task")


class ProjectInfo(Model):
    """No arguments: inspect the current QGIS project and loaded layers."""


class DataInfo(Model):
    source: Nonempty = Field(description="Absolute local data path or asset:<logical_id>")
    include_details: bool = Field(False, description="Include fields/bands only when needed")
    query: FeatureQuery | None = Field(
        None,
        description="Optional bounded vector feature sample or numeric-field statistics; never mutates selection",
    )


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
    "project_setup",
    "project_update",
    "load",
    "add_basemap",
    "layer_manage",
    "vector_data",
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
    service: MapService | None = Field(None, description="Map service for add_basemap")
    project: ProjectRequest | None = Field(None, description="Project choice for project_setup")
    project_update: ProjectUpdate | None = Field(None, description="Project title/CRS update")
    layer_operation: LayerOperation | None = Field(None, description="Layer-management operation")
    data_operation: VectorDataOperation | None = Field(None, description="Vector create/edit/export operation")
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
    legend_title: str | None = Field(None, description="Optional reader-facing legend heading")
    map_language: Literal["auto", "zh", "en"] = "auto"
    show_legend_title: bool = True
    north_arrow: bool = False
    page_orientation: Literal["auto", "portrait", "landscape"] = "auto"
    map_crs: str | None = Field(None, description="Map display CRS for on-the-fly projection")
    map_frame: MapFrame = Field(default_factory=MapFrame)
    mode: Literal["continuous", "mask", "gray", "rgb", "hillshade", "qml"] = "continuous"
    ramp: str = "Viridis"
    band: int = Field(1, ge=1)
    red: int | None = Field(None, ge=1)
    green: int | None = Field(None, ge=1)
    blue: int | None = Field(None, ge=1)
    minimum: float | None = None
    maximum: float | None = None
    classes: int = Field(8, ge=2, le=256)
    azimuth: float = Field(315, ge=0, le=360)
    altitude: float = Field(45, ge=0, le=90)
    z_factor: float = Field(1, gt=0)
    qml_asset: SafeId | None = None
    color: str = "#4c78a8"
    label: str | None = None
    outline: str = "#202020"
    width: float = Field(0.4, ge=0, le=20)
    size: float = Field(2, gt=0, le=100)
    category_field: str | None = None
    categories: list[dict[str, Any]] | None = None
    renderer: Literal["single", "categorized", "graduated", "rule_based", "qml"] = "single"
    graduated_field: str | None = None
    method: Literal["equal_interval", "quantile", "jenks"] = "equal_interval"
    rules: list[VectorRule] | None = None
    marker: Literal["circle", "square", "triangle", "diamond", "cross", "cross2"] = "circle"
    line_style: Literal["solid", "dash", "dot", "dash_dot", "dash_dot_dot"] = "solid"
    label_field: str | None = None
    opacity: float = Field(1, ge=0, le=1)
    nodata: float | Literal["nan"] | None = None
    all_touched: bool = False
    coordinate_crs: str | None = None
    map_elements: MapElements = Field(default_factory=MapElements)
    coordinate_annotations: CoordinateAnnotations = Field(default_factory=CoordinateAnnotations)
    dpi: int = Field(150, ge=72, le=600)


class WorkflowRun(TaskContinuation):
    """Execute a common multi-step workflow with a checkpoint after every step."""

    workflow: Literal["standard_map_project", "project_layers", "project_operations"] = Field(
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
    legend_title: str | None = Field(None, description="Optional reader-facing legend heading")
    map_language: Literal["auto", "zh", "en"] = "auto"
    show_legend_title: bool = True
    north_arrow: bool = False
    page_orientation: Literal["auto", "portrait", "landscape"] = "auto"
    map_crs: str | None = Field(None, description="Map display CRS for on-the-fly projection")
    map_frame: MapFrame = Field(default_factory=MapFrame)
    coordinate_crs: str | None = Field(
        None, description="Optional annotation CRS ID, e.g. EPSG:4326; omit for default EPSG:4326 longitude/latitude labels"
    )
    map_elements: MapElements = Field(default_factory=MapElements)
    coordinate_annotations: CoordinateAnnotations = Field(default_factory=CoordinateAnnotations)
    style_layers: bool = Field(
        True, description="Apply the service's standard vector/raster styles before layout"
    )
    raster_ramp: str = "Viridis"
    raster_styles: dict[SafeId, RasterPresentationStyle] = Field(default_factory=dict)
    vector_styles: dict[SafeId, VectorPresentationStyle] = Field(default_factory=dict)
    project: ProjectRequest = Field(default_factory=ProjectRequest)
    project_update: ProjectUpdate | None = None
    basemap: MapService | None = None
    services: dict[SafeId, MapService] = Field(default_factory=dict)
    layer_operations: list[LayerOperation] = Field(default_factory=list)
    data_operations: list[VectorDataOperation] = Field(default_factory=list)
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


class CompactResume(TaskReference):
    """Public recovery shape; failed-operation retries belong to task_restart."""

    resume_cancelled: bool = Field(
        False, description="Set true only when the user explicitly asks to reopen a cancelled task"
    )

class TaskRepairFields(TaskReference):
    steps: list[SafeId] = Field(
        min_length=1, description="Committed producer steps with incorrect or unusable results"
    )
    reason: Nonempty = Field(description="Concrete evidence that the committed result is incorrect or unusable")


class TaskRepair(TaskRepairFields):
    continuation_token: Nonempty


class CompactTaskRepair(TaskRepairFields):
    """Public invalidation shape; the compact service supplies task state."""


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
    ("task_update", TaskClarify, "Record an actual user answer and reopen the bounded correction and repair windows. Ask the user first. Does not change locked acceptance requirements."),
    ("step_execute", ExecuteStep, "Execute one approved internal step. Copy task_id, step_id and continuation_token from step_prepare or step_contract_submit; do not repeat GIS parameters."),
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
        "project_info",
        ProjectInfo,
        "Read the current QGIS project, its CRS, layouts and loaded layers. This never changes the project.",
    ),
    (
        "data_info",
        DataInfo,
        "Inspect an absolute input path or task asset. Returns metadata by default; query.sample returns at most 100 vector features and query.statistics computes one numeric field, optionally through a verified QGIS expression. It never changes selection.",
    ),
    (
        "task_start",
        TaskRun,
        "Start one durable project, data, processing, style or single-frame map task. It can load OSM/XYZ/WMS/WMTS/WFS context; set a display projection; configure title/legend/scale/north-arrow positions; choose common legend, scale and coordinate-label options; style layers; organize a project; and copy-edit/export vectors. When the user says same-named or existing declared outputs may be overwritten, set overwrite_existing_outputs=true; the server also recognizes that unambiguous original instruction. Otherwise it asks. Omit optional cartographic choices for server layout.",
    ),
    (
        "task_execute",
        CompactTaskContinue,
        "Execute the one operation currently prepared by the server. Takes no arguments: the service owns action handles, internal steps and approved parameters. Continue until completed or a structured question is returned.",
    ),
    (
        "task_answer",
        CompactTaskAnswer,
        "Record the user's actual answer to a required question. Repeating the same saved answer safely resumes interrupted preparation; a different answer is rejected. Never invent an answer.",
    ),
    (
        "task_update",
        TaskUpdate,
        "Apply an actual user-requested update: project or display CRS, services, layers, copy-edit/export vectors, single-frame map elements/coordinate labels/styles, or exact overwrite approval. During OUTPUT_EXISTS, prefer the structured output_conflict decision copied from the error; a clear actual user approval is otherwise bound only to the exact pending path. A valid decision automatically resumes only that failed operation and returns task_execute. An unsupported explicit map option must be reported instead of approximated. Project/data operations and presentation revisions are submitted separately.",
    ),
    (
        "task_resume",
        CompactResume,
        "Reconnect an interrupted task, verify its checkpoint and make the next safe action available. It does not silently retry a failed scientific or parameter choice.",
    ),
    (
        "task_diagnose",
        TaskStatus,
        "Read-only diagnosis for an existing task: returns durable state, assets and the last error. It never resumes, retries or mutates; use task_resume for continuation.",
    ),
    (
        "task_restart",
        TaskRestart,
        "Restart a user-selected failed operation, analysis or map build while retaining durable evidence. The server rejects scopes that would silently discard valid results.",
    ),
    (
        "task_stop",
        TaskStop,
        "Safely stop the task at its last committed checkpoint. It preserves the task record and existing outputs for later diagnosis or resume.",
    ),
    (
        "prepare_algorithm",
        SafePrepareAlgorithm,
        "Prepare one Processing operation by exact installed provider:algorithm ID. The service owns internal steps and repair links. Put only layer/source bindings in inputs, destinations in outputs, and known scalar/band/enum/CRS/expression values in parameters. Live help, normalization and native preflight run automatically; unknown required semantic values return structured questions.",
    ),
]
