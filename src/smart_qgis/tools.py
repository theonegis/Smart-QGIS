"""Single typed contract shared by LangChain and the MCP protocol."""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Project(Arguments):
    action: Literal["info", "create", "open", "save"] = "info"
    path: str | None = None
    crs: str = "EPSG:4326"
    title: str = "Smart-QGIS"
    overwrite: bool = False


class Load(Arguments):
    path: str = Field(
        description="Absolute file path or provider URI (e.g. GeoPackage path|layername=name)"
    )
    name: str | None = None
    kind: Literal["vector", "raster"] = "vector"
    provider: str | None = None


class Basemap(Arguments):
    service: Literal["osm", "google", "xyz", "wms", "wmts"] = "osm"
    url: str | None = Field(
        None,
        description="XYZ template or full QGIS WMS/WMTS provider URI. Optional override for Google presets.",
    )
    name: str | None = None
    attribution: str | None = None
    google_style: Literal["roadmap", "terrain", "satellite"] = "roadmap"
    zmin: int = Field(0, ge=0, le=30)
    zmax: int = Field(19, ge=0, le=30)


class Layers(Arguments):
    action: Literal["list", "rename", "remove", "visibility", "order"] = "list"
    layer: str | None = Field(None, description="Exact layer ID or unique name")
    name: str | None = None
    visible: bool = True
    order: list[str] | None = Field(None, description="All layer IDs/names, topmost first")


class Features(Arguments):
    layer: str
    limit: int = Field(10, ge=1, le=100)
    expression: str | None = Field(None, description="Optional QGIS filter expression")


class VectorStyle(Arguments):
    layer: str
    color: str = "#4c78a8"
    outline: str = "#202020"
    width: float = Field(0.4, ge=0, le=20, description="Stroke width in millimetres")
    size: float = Field(2, gt=0, le=100, description="Point size in millimetres")
    opacity: float = Field(1, ge=0, le=1)
    category_field: str | None = None
    categories: list[dict[str, Any]] | None = Field(
        None, description="Objects with value, color, optional label"
    )
    label_field: str | None = None


class RasterStyle(Arguments):
    layer: str
    mode: Literal["continuous", "mask"] = "continuous"
    ramp: str = "Viridis"
    color: str = "#666666"
    label: str | None = None
    band: int = Field(1, ge=1)
    minimum: float | None = None
    maximum: float | None = None
    classes: int = Field(8, ge=2, le=256)
    opacity: float = Field(1, ge=0, le=1)


class Algorithms(Arguments):
    action: Literal["list", "help", "ramps"] = Field(
        "list",
        description="list searches installed algorithms; help returns one exact algorithm schema; ramps lists color ramps",
    )
    query: str = Field(
        "",
        description="For action=list only: one or two space-separated keywords; every word must match",
    )
    algorithm: str | None = Field(
        None,
        description="Required for action=help: exact installed provider:algorithm ID copied from list results",
    )
    provider: str | None = Field(None, description="Exact provider ID filter for list, e.g. native or gdal")
    group: str | None = Field(None, description="Exact group ID filter for list; returned in list entries")
    offset: int = Field(0, ge=0, description="For action=list only: zero-based result offset")
    limit: int = Field(30, ge=1, le=200, description="For action=list only: maximum results; keep small")
    include_details: bool = Field(
        False,
        description=(
            "Use only when compact help lacks a specific parameter detail; includes raw "
            "provider definitions and long algorithm help"
        ),
    )


class LayerList(Arguments):
    """Reliable-mode read-only project layer listing."""


class Processing(Arguments):
    algorithm: str = Field(description="Exact QGIS processing ID; discover with algorithm_info first")
    parameters: dict[str, Any] = Field(
        description="QGIS algorithm parameters. Use layer IDs or absolute file paths. Use durable output paths to persist results."
    )
    load_outputs: bool = True


class Layout(Arguments):
    action: Literal["create", "list", "remove", "template"] = "create"
    name: str = "Map"
    title: str = ""
    show_title: bool = True
    layers: list[str] | None = Field(None, description="Topmost first. Defaults to visible layers.")
    extent_layer: str | None = Field(
        None, description="Use this layer's transformed extent; excludes global basemap extents"
    )
    extent: list[float] | None = Field(
        None, min_length=4, max_length=4, description="xmin,ymin,xmax,ymax in map CRS"
    )
    crs: str | None = None
    width_mm: float = Field(210, ge=100, le=1000)
    height_mm: float = Field(297, ge=100, le=1000)
    legend: bool = True
    scalebar: bool = True
    grid: bool = True
    map_element_placement: Literal["auto", "inside", "outside"] = Field(
        "auto", description="Place legend and scale bar inside available map-frame space or outside when they do not fit"
    )
    grid_crs: str | None = Field(
        None,
        description=(
            "Coordinate annotation CRS; defaults to EPSG:4326. May be geographic or projected."
        ),
    )
    path: str | None = Field(None, description="QPT template output path for action=template")
    overwrite: bool = False


class Export(Arguments):
    layout: str = "Map"
    path: str = Field(description="Absolute output .png, .jpg, .tif, or .pdf path")
    dpi: int = Field(150, ge=72, le=600)
    overwrite: bool = False


SPECS = [
    (
        "project_manage",
        Project,
        "Create, open, save or inspect a headless QGIS project (.qgz/.qgs). Create/open replace in-memory state; save first. No QGIS window is needed.",
    ),
    (
        "load_data",
        Load,
        "Mutation that loads vector/raster data into the QGIS project. In reliable mode approve a step with exactly one output using binding='layer'; use inspect_data for read-only inspection.",
    ),
    (
        "add_basemap",
        Basemap,
        "Add OSM, authorized Google XYZ tiles, custom XYZ, WMS or WMTS. Remote imagery requires network access. Google supports roadmap, terrain and satellite presets or a custom URL.",
    ),
    (
        "layer_manage",
        Layers,
        "List, rename, remove, reorder or show/hide layers. Order is topmost first; names must be unique, IDs are preferred.",
    ),
    (
        "feature_info",
        Features,
        "Inspect a bounded GeoJSON feature sample and attributes, optionally filtered by a QGIS expression.",
    ),
    (
        "style_vector",
        VectorStyle,
        "Style point, line or polygon data, optionally categorized with labels. For transparent polygons set color='transparent'. Width and size are millimetres.",
    ),
    (
        "style_raster",
        RasterStyle,
        "Apply a QGIS color ramp to a raster band using valid-pixel statistics. NoData stays transparent. Default ramp is Viridis.",
    ),
    (
        "algorithm_info",
        Algorithms,
        "Read-only Processing registry lookup after task_start (or task_recover). Use list once with 1-2 keywords, then help once for the chosen exact provider:algorithm ID. Stop searching after help identifies a suitable algorithm; prepare_algorithm performs the live preflight.",
    ),
    (
        "processing_execute",
        Processing,
        "Execute an installed QGIS/GDAL processing algorithm. Read help first. Output files may be overwritten by the algorithm: choose fresh paths. TEMPORARY_OUTPUT lives only for this server session.",
    ),
    (
        "layout_manage",
        Layout,
        "Create/manage a printable map document with title, legend, scale bar and WGS84 graticule, or save it as QPT. Saved projects retain layouts. extent_layer avoids global basemap extents.",
    ),
    (
        "export_map",
        Export,
        "Export a named print layout to PNG/JPEG/TIFF/PDF. Returns verified absolute path and byte count. Save the project too to retain an editable map document.",
    ),
]

WORKER_OPERATIONS = {
    "project_manage": "project", "layer_manage": "layers", "layer_info": "layers",
    "feature_info": "features", "algorithm_info": "algorithms",
    "processing_execute": "run_processing", "layout_manage": "layout",
    "qml_style_manage": "style_file", "vector_data_manage": "vector_data",
    "style_vector_graduated": "style_graduated",
}


def build_tools(bridge, *, compact=False):
    tools = []
    specs = list(SPECS)
    if getattr(bridge, "reliable", False):
        from .task_tools import COMPACT_TASK_SPECS, TASK_SPECS

        if compact:
            algorithm_spec = next(spec for spec in SPECS if spec[0] == "algorithm_info")
            specs = [algorithm_spec, *COMPACT_TASK_SPECS]
        else:
            algorithm_spec = next(spec for spec in SPECS if spec[0] == "algorithm_info")
            specs = [
                algorithm_spec,
                ("layer_info", LayerList, "List current project layers without changing them."),
                *TASK_SPECS,
            ]
    for name, schema, description in specs:

        def bind(operation, model):
            async def invoke(**kwargs):
                arguments = model.model_validate(kwargs).model_dump()
                return await bridge.call(operation, arguments)

            return invoke

        tools.append(
            StructuredTool.from_function(
                coroutine=bind(WORKER_OPERATIONS.get(name, name), schema),
                name=name,
                description=description,
                args_schema=schema,
            )
        )
    return tools


class StyleFile(Arguments):
    action: Literal["load", "save"]
    layer: str
    path: str = Field(description="Absolute .qml path")
    overwrite: bool = False


class VectorData(Arguments):
    action: Literal["select", "clear_selection", "export", "create", "statistics"]
    layer: str | None = None
    expression: str | None = None
    field: str | None = None
    path: str | None = None
    name: str = "Features"
    crs: str = "EPSG:4326"
    geojson: dict[str, Any] | None = Field(None, description="GeoJSON FeatureCollection for create")
    selected_only: bool = False
    overwrite: bool = False


class RasterRender(Arguments):
    layer: str
    mode: Literal["gray", "rgb", "hillshade"] = "gray"
    band: int = Field(1, ge=1)
    red: int = Field(1, ge=1)
    green: int = Field(2, ge=1)
    blue: int = Field(3, ge=1)
    azimuth: float = Field(315, ge=0, le=360)
    altitude: float = Field(45, gt=0, le=90)
    z_factor: float = Field(1, gt=0)
    opacity: float = Field(1, ge=0, le=1)


class GraduatedStyle(Arguments):
    layer: str
    field: str
    ramp: str = "Viridis"
    classes: int = Field(5, ge=2, le=20)
    method: Literal["equal_interval", "quantile", "jenks"] = "quantile"


SPECS.extend(
    [
        (
            "qml_style_manage",
            StyleFile,
            "Save/load QGIS QML symbology including detailed renderer and labeling settings.",
        ),
        (
            "vector_data_manage",
            VectorData,
            "Select features by QGIS expression, clear selection, compute field statistics, create a memory layer from GeoJSON, or export vector data to GPKG/GeoJSON/SHP. Export supports selected features and target CRS.",
        ),
        (
            "render_raster",
            RasterRender,
            "Render rasters as contrast-stretched grayscale, RGB composite or live hillshade. For pseudocolor use style_raster. Hillshade z_factor must match horizontal/vertical units; use projected elevation data for physical slopes.",
        ),
        (
            "style_vector_graduated",
            GraduatedStyle,
            "Classify a numeric vector field using equal interval, quantiles or Jenks with a QGIS color ramp.",
        ),
    ]
)
