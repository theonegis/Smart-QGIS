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
    ramp: str = "Viridis"
    band: int = Field(1, ge=1)
    minimum: float | None = None
    maximum: float | None = None
    classes: int = Field(8, ge=2, le=256)
    opacity: float = Field(1, ge=0, le=1)


class Algorithms(Arguments):
    action: Literal["list", "help", "ramps"] = "list"
    query: str = ""
    algorithm: str | None = None
    offset: int = Field(0, ge=0)
    limit: int = Field(30, ge=1, le=200)


class Processing(Arguments):
    algorithm: str = Field(description="Exact QGIS processing ID; discover with algorithms first")
    parameters: dict[str, Any] = Field(
        description="QGIS algorithm parameters. Use layer IDs or absolute file paths. Use durable output paths to persist results."
    )
    load_outputs: bool = True


class Layout(Arguments):
    action: Literal["create", "list", "remove", "template"] = "create"
    name: str = "Map"
    title: str = ""
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
    path: str | None = Field(None, description="QPT template output path for action=template")
    overwrite: bool = False


class Export(Arguments):
    layout: str = "Map"
    path: str = Field(description="Absolute output .png, .jpg, .tif, or .pdf path")
    dpi: int = Field(150, ge=72, le=600)
    overwrite: bool = False


SPECS = [
    (
        "project",
        Project,
        "Create, open, save or inspect a headless QGIS project (.qgz/.qgs). Create/open replace in-memory state; save first. No QGIS window is needed.",
    ),
    (
        "load_data",
        Load,
        "Load vector (SHP, GeoJSON, GPKG etc.) or raster (GeoTIFF etc.) data. Returns ID, CRS, extent and fields/bands.",
    ),
    (
        "add_basemap",
        Basemap,
        "Add OSM, authorized Google XYZ tiles, custom XYZ, WMS or WMTS. Remote imagery requires network access. Google supports roadmap, terrain and satellite presets or a custom URL.",
    ),
    (
        "layers",
        Layers,
        "List, rename, remove, reorder or show/hide layers. Order is topmost first; names must be unique, IDs are preferred.",
    ),
    (
        "features",
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
        "algorithms",
        Algorithms,
        "Search installed QGIS algorithms, inspect parameters/outputs/help, or list color ramps. Always inspect algorithm help before processing.",
    ),
    (
        "run_processing",
        Processing,
        "Execute an installed QGIS/GDAL processing algorithm. Read help first. Output files may be overwritten by the algorithm: choose fresh paths. TEMPORARY_OUTPUT lives only for this server session.",
    ),
    (
        "layout",
        Layout,
        "Create/manage a printable map document with title, legend, scale bar and WGS84 graticule, or save it as QPT. Saved projects retain layouts. extent_layer avoids global basemap extents.",
    ),
    (
        "export_map",
        Export,
        "Export a named print layout to PNG/JPEG/TIFF/PDF. Returns verified absolute path and byte count. Save the project too to retain an editable map document.",
    ),
]


def build_tools(bridge):
    tools = []
    for name, schema, description in SPECS:

        def bind(operation, model):
            async def invoke(**kwargs):
                arguments = model.model_validate(kwargs).model_dump()
                return await bridge.call(operation, arguments)

            return invoke

        tools.append(
            StructuredTool.from_function(
                coroutine=bind(name, schema),
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
            "style_file",
            StyleFile,
            "Save/load QGIS QML symbology including detailed renderer and labeling settings.",
        ),
        (
            "vector_data",
            VectorData,
            "Select features by QGIS expression, clear selection, compute field statistics, create a memory layer from GeoJSON, or export vector data to GPKG/GeoJSON/SHP. Export supports selected features and target CRS.",
        ),
        (
            "render_raster",
            RasterRender,
            "Render rasters as contrast-stretched grayscale, RGB composite or live hillshade. For pseudocolor use style_raster. Hillshade z_factor must match horizontal/vertical units; use projected elevation data for physical slopes.",
        ),
        (
            "style_graduated",
            GraduatedStyle,
            "Classify a numeric vector field using equal interval, quantiles or Jenks with a QGIS color ramp.",
        ),
    ]
)
