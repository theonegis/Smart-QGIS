"""
QGIS MCP Server - Simple server to connect to the QGIS socket server
Thanks to QGISMCP
https://github.com/jjsantos01/qgis_mcp
"""

import logging
import threading
import argparse
from contextlib import asynccontextmanager
import socket
import json
import uvicorn
from typing import AsyncIterator, Dict, Any, Optional
from mcp.server.fastmcp import FastMCP, Context
import sys
from pathlib import Path

# Add parent directory to path to import prompts module
sys.path.insert(0, str(Path(__file__).parent.parent))
from prompts import QGIS_AGENT_SYSTEM_PROMPT

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("MCPServer")


class QgisMcpServer:
    def __init__(self, host="127.0.0.1", port=9876):
        self.host = host
        self.port = port
        self.socket = None
        self.lock = threading.Lock()

    def connect(self):
        """Connect to the QGIS socket server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(
                600.0
            )  # 60 minute timeout for long-running GIS tasks
            self.socket.connect((self.host, self.port))
            return True
        except Exception as e:
            logger.error(f"Error connecting to server: {str(e)}")
            return False

    def disconnect(self):
        """Disconnect from the server"""
        if self.socket:
            self.socket.close()
            self.socket = None

    def send_command(self, command_type, params=None):
        """Send a command to the server and get the response"""
        with self.lock:  # Ensure only one command at a time over this socket
            if not self.socket:
                logger.error("Not connected to server")
                return None

            # Create command
            command = {"type": command_type, "params": params or {}}
            import time

            start_time = time.time()
            logger.info(f"Sending command to QGIS: {command_type}")

            try:
                # Send the command
                self.socket.sendall(json.dumps(command).encode("utf-8"))

                # Receive the response
                response_data = b""
                while True:
                    chunk = self.socket.recv(4096)
                    if not chunk:
                        logger.warning(
                            "Socket closed by server while waiting for response"
                        )
                        break
                    response_data += chunk

                    # Try to decode as JSON to see if it's complete
                    try:
                        decoded = response_data.decode("utf-8")
                        result = json.loads(decoded)
                        duration = time.time() - start_time
                        logger.info(
                            f"Received response from QGIS for {command_type} in {duration:.3f}s"
                        )
                        return result
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue  # Keep receiving

                return {
                    "status": "error",
                    "message": "Connection closed without response",
                }

            except Exception as e:
                logger.error(f"Error sending command: {str(e)}")
                return {"status": "error", "message": f"Communication error: {str(e)}"}


_qgis_connection = None


def get_qgis_connection():
    """Get or create a persistent QGIS connection"""
    global _qgis_connection

    # If we have an existing connection, check if it's still valid
    if _qgis_connection is not None:
        # Test if the connection is still alive with a simple ping
        try:
            # Just try to send a small message to check if the socket is still connected
            _qgis_connection.socket.sendall(b"")
            return _qgis_connection
        except Exception as e:
            # Connection is dead, close it and create a new one
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _qgis_connection.disconnect()
            except Exception:
                pass
            _qgis_connection = None

    # Create a new connection if needed
    if _qgis_connection is None:
        _qgis_connection = QgisMcpServer(host="127.0.0.1", port=9876)
        if not _qgis_connection.connect():
            logger.error("Failed to connect to QGIS")
            _qgis_connection = None
            raise Exception(
                "Could not connect to QGIS. Make sure the QGIS plugin is running."
            )
        logger.info("Created new persistent connection to QGIS")

    return _qgis_connection


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    # We don't need to create a connection here since we're using the global connection
    # for resources and tools
    global _qgis_connection
    try:
        # Just log that we're starting up
        logger.info("MCP server starting up")

        # Try to connect to QGIS on startup to verify it's available
        try:
            # This will initialize the global connection if needed
            _qgis_connection = get_qgis_connection()
            logger.info("Successfully connected to QGIS on startup")
        except Exception as e:
            logger.warning(f"Could not connect to QGIS on startup: {str(e)}")
            logger.warning(
                "Make sure the QGIS addon is running before using QGIS resources or tools"
            )

        # Return an empty context - we're using the global connection
        yield {}
    finally:
        # Clean up the global connection on shutdown
        if _qgis_connection:
            logger.info("Disconnecting from QGIS on shutdown")
            _qgis_connection.disconnect()
            _qgis_connection = None
        logger.info("MCP server shut down")


mcp = FastMCP(
    "qgis",
    instructions=QGIS_AGENT_SYSTEM_PROMPT,
    lifespan=server_lifespan,
)


@mcp.resource("qgis://current/layers")
def get_active_layers() -> str:
    """Get the list of currently loaded layers in the QGIS project."""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_project_info")
    layers = result.get("layers", [])
    if not layers:
        return "The QGIS project is currently empty (no layers loaded)."

    summary = ["Current QGIS Layers:"]
    for l in layers:
        summary.append(f"- {l['name']} (Type: {l['type']}, ID: {l['id']})")
    return "\n".join(summary)


@mcp.prompt()
def quick_start() -> str:
    """Prompt to help the user get started with QGIS analysis."""
    return "I am connected to your QGIS instance. Tell me what you'd like to do - load data, analyze layers, create maps, or anything else. I will help you with exactly what you ask for."


@mcp.tool()
def manage_project(ctx: Context, action: str, path: Optional[str] = None) -> str:
    """
    Manage the QGIS project: load, save, create, or get info.

    Args:
        action: (REQUIRED) One of "load", "save", "create_new", "get_info", "get_qgis_info".
        path: (REQUIRED for "load" and "create_new", OPTIONAL for "save") Path to the project file.

    Examples:
        Get current project info:
            manage_project(action="get_info")

        Load a project:
            manage_project(action="load", path="/path/to/project.qgz")

        Save current project:
            manage_project(action="save")

        Create new project:
            manage_project(action="create_new")
    """
    qgis = get_qgis_connection()
    match action:
        case "load":
            if not path:
                return json.dumps(
                    {"status": "error", "message": "Path is required for load"},
                    indent=2,
                )
            return json.dumps(
                qgis.send_command("load_project", {"path": path}), indent=2
            )
        case "save":
            return json.dumps(
                qgis.send_command("save_project", {"path": path} if path else {}),
                indent=2,
            )
        case "create_new":
            return json.dumps(
                qgis.send_command("create_new_project", {"path": path} if path else {}),
                indent=2,
            )
        case "get_info":
            return json.dumps(qgis.send_command("get_project_info"), indent=2)
        case "get_qgis_info":
            return json.dumps(qgis.send_command("get_qgis_info"), indent=2)
        case _:
            return json.dumps(
                {"status": "error", "message": f"Unknown action: {action}"}, indent=2
            )


@mcp.tool()
def get_system_paths(ctx: Context) -> str:
    """
    Get standard system paths for the current user (Home, Desktop, Documents, Downloads, Temp).
    Use this tool to find the correct path to save files, instead of guessing.
    """
    import os
    import tempfile

    home = os.path.expanduser("~")
    paths = {
        "home": home,
        "desktop": os.path.join(home, "Desktop"),
        "documents": os.path.join(home, "Documents"),
        "downloads": os.path.join(home, "Downloads"),
        "temp": tempfile.gettempdir(),
    }

    return json.dumps({"status": "success", "paths": paths}, indent=2)


@mcp.tool()
def load_data(
    ctx: Context,
    data_type: Optional[str] = None,
    path: Optional[str] = None,
    provider: Optional[str] = None,
    name: Optional[str] = None,
    url: Optional[str] = None,
) -> str:
    """
    Load data into QGIS: vector files, raster files, or base map tiles.

    **IMPORTANT: This tool ONLY loads data. Do NOT call style_layer or style_raster after this unless the user explicitly asks for styling.**

    **CRITICAL:**
    - For vector/raster: Use 'path' to specify the file location.
    - For 'data_type': Use 'vector', 'raster', or 'basemap'. (REQUIRED)

    Args:
        data_type: (REQUIRED) Type of data ("vector", "raster", "basemap").
        path: (REQUIRED for vector/raster) Full file path or location.
        provider: (OPTIONAL) Data provider (e.g., "ogr", "gdal").
        name: (OPTIONAL, if it is not given, guess from the file name without extension) Display name for the layer.
        url: (OPTIONAL if `name` is given for basemap, otherwise it is REQUIRED) Custom tile URL only for basemap type.

    Examples:
        Load a Shapefile:
            load_data(data_type="vector", path="/path/to/data.shp", name="Rivers", provider="ogr")

        Load a GeoTIFF:
            load_data(data_type="raster", path="/path/to/elevation.tif", name="DEM", provider="gdal")

        Add OpenStreetMap:
            load_data(data_type="basemap", name="OSM", url="https://tile.openstreetmap.org/{z}/{x}/{y}.png")

        Add Google Satellite:
            load_data(data_type="basemap", name="Google Satellite")
    """
    # Check for truncated path
    if path and "..." in path:
        return json.dumps(
            {
                "status": "error",
                "message": "The file path appears to be truncated with '...'. NEVER truncate paths. Please provide the FULL absolute file path from the user's request.",
            },
            indent=2,
        )

    # Inference for missing data_type
    if not data_type and path:
        ext = path.lower().split(".")[-1]
        if ext in ["shp", "geojson", "gpkg", "kml", "tab"]:
            data_type = "vector"
        elif ext in ["tif", "tiff", "jpg", "jpeg", "png", "img", "asc"]:
            data_type = "raster"

    # DEBUG: Log what we received after inference
    logger.info(
        f"load_data called with: data_type={data_type}, path={path}, name={name}"
    )

    if not data_type:
        return json.dumps(
            {
                "status": "error",
                "message": "data_type is required (vector, raster, or basemap)",
            },
            indent=2,
        )

    qgis = get_qgis_connection()

    match data_type:
        case "vector":
            if not path:
                return json.dumps(
                    {"status": "error", "message": "path is required for vector"},
                    indent=2,
                )
            params = {"path": path, "provider": provider or "ogr"}
            if name:
                params["name"] = name
            return json.dumps(qgis.send_command("add_vector_layer", params), indent=2)
        case "raster":
            if not path:
                return json.dumps(
                    {"status": "error", "message": "path is required for raster"},
                    indent=2,
                )
            params = {"path": path, "provider": provider or "gdal"}
            if name:
                params["name"] = name
            return json.dumps(qgis.send_command("add_raster_layer", params), indent=2)
        case "basemap":
            params = {"name": name or "XYZ Layer"}
            if url:
                params["url"] = url
            return json.dumps(qgis.send_command("add_xyz_tile_layer", params), indent=2)
        case _:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Unknown data_type: {data_type}. Use 'vector', 'raster', or 'basemap'",
                },
                indent=2,
            )


def _get_layer_id_smart(
    qgis, layer_id: Optional[str], layer_name: Optional[str], geometry_type: str
) -> str:
    """
    Helper to find layer ID based on name and geometry type.

    Priority:
    1. If layer_id provided, use it directly
    2. Filter all layers by geometry type
    3. If no layer_name specified:
       - Use active layer if it matches geometry type
       - Use the only layer if there's exactly one matching geometry type
    4. If layer_name specified:
       - Fuzzy match by name among layers of correct geometry type
       - Prefer active layer if multiple matches
    """
    if layer_id:
        return layer_id

    # Get all layers
    layers_resp = qgis.send_command("get_layers")
    if layers_resp.get("status") != "success":
        raise Exception("Failed to retrieve layers for selection")

    layers = layers_resp.get("layers", [])
    if not layers:
        raise Exception("No layers found in the project")

    # Filter by geometry type first (strict requirement)
    candidates = [l for l in layers if l.get("geometry_type") == geometry_type]
    if not candidates:
        raise Exception(f"No {geometry_type.lower()} layers found in the project")

    # Case 1: No layer name specified - use active or single layer
    if not layer_name:
        # Try active layer first
        active_candidates = [l for l in candidates if l.get("active")]
        if active_candidates:
            return active_candidates[0]["id"]

        # If only one layer of this type, use it
        if len(candidates) == 1:
            return candidates[0]["id"]

        # Multiple layers and none active
        names = [l["name"] for l in candidates]
        raise Exception(
            f"Multiple {geometry_type.lower()} layers found: {', '.join(names)}. Please specify a layer name or select the desired layer in QGIS"
        )

    # Case 2: Layer name specified - fuzzy match
    name_matches = [l for l in candidates if layer_name.lower() in l["name"].lower()]
    if not name_matches:
        raise Exception(
            f"No {geometry_type.lower()} layer found matching name '{layer_name}'"
        )

    # If multiple matches, prefer active
    active_matches = [l for l in name_matches if l.get("active")]
    if active_matches:
        return active_matches[0]["id"]

    # If only one match, use it
    if len(name_matches) == 1:
        return name_matches[0]["id"]

    # Multiple matches, none active
    names = [l["name"] for l in name_matches]
    raise Exception(
        f"Multiple {geometry_type.lower()} layers match '{layer_name}': {', '.join(names)}. Please be more specific or select the desired layer in QGIS"
    )


@mcp.tool()
def style_layer(
    ctx: Context,
    style_type: str,
    layer_id: Optional[str] = None,
    layer_name: Optional[str] = None,
    color: Optional[str] = None,
    size: Optional[float] = None,
    width: Optional[float] = None,
    shape: Optional[str] = None,
    line_style: Optional[str] = None,
    fill_color: Optional[str] = None,
    fill_style: Optional[str] = None,
    stroke_color: Optional[str] = None,
    stroke_width: Optional[float] = None,
    field_name: Optional[str] = None,
    color_scheme: str = "random",
) -> str:
    """
    Set the style of a vector layer (DO NOT call this tool when loading data except user explicitly asks for it).

    Args:
        style_type: (REQUIRED) One of "point", "line", "polygon", "categorized_polygon".
        layer_id: (REQUIRED if layer_name not provided) ID of the layer.
        layer_name: (REQUIRED if layer_id not provided) Name of the layer.
        color: (OPTIONAL) Color for point/line (e.g., "red", "#FF0000").
        size: (OPTIONAL) Size for point marker.
        width: (OPTIONAL) Width for line.
        shape: (OPTIONAL) Shape for point marker ("circle", "square", etc.).
        line_style: (OPTIONAL) Style for line ("solid", "dash", etc.).
        fill_color: (OPTIONAL) Fill color for polygon.
                    Set to "transparent" or "no_fill" for hollow polygons.
        fill_style: (OPTIONAL) Fill style for polygon ("solid", "no_brush", etc.).
        stroke_color: (OPTIONAL) Border color for polygon.
        stroke_width: (OPTIONAL) Border width for polygon.
        field_name: (REQUIRED for "categorized_polygon") Field for categorized styling.
        color_scheme: (OPTIONAL) Color scheme for categorized styling ("random", "rainbow", "gradient").

    Examples:
        Style a point layer with red circles:
            style_layer(style_type="point", layer_name="cities", color="red", size=5, shape="circle")

        Style a line layer with blue dashed lines:
            style_layer(style_type="line", layer_name="roads", color="blue", width=2, line_style="dash")

        Style a polygon layer with green fill and black border:
            style_layer(style_type="polygon", layer_name="countries", fill_color="green", stroke_color="black", stroke_width=1)

        Create hollow polygons (no fill):
            style_layer(style_type="polygon", layer_name="boundaries", fill_color="transparent", stroke_color="red", stroke_width=2)

        Categorize polygons by a field:
            style_layer(style_type="categorized_polygon", layer_name="regions", field_name="category", color_scheme="rainbow")
    """
    qgis = get_qgis_connection()

    base_params = {}
    if layer_id:
        base_params["layer_id"] = layer_id
    if layer_name:
        base_params["layer_name"] = layer_name

    match style_type:
        case "point":
            params = {**base_params}
            if color:
                params["color"] = color
            if size is not None:
                params["size"] = size
            if shape:
                params["shape"] = shape
            return json.dumps(
                qgis.send_command("set_point_layer_style", params), indent=2
            )
        case "line":
            params = {**base_params}
            if color:
                params["color"] = color
            if width is not None:
                params["width"] = width
            if line_style:
                params["line_style"] = line_style
            return json.dumps(
                qgis.send_command("set_line_layer_style", params), indent=2
            )
        case "polygon":
            params = {**base_params}
            if fill_color:
                params["fill_color"] = fill_color
            if fill_style:
                params["fill_style"] = fill_style
            if stroke_color:
                params["outline_color"] = stroke_color
            if stroke_width is not None:
                params["outline_width"] = stroke_width
            return json.dumps(
                qgis.send_command("set_polygon_layer_style", params), indent=2
            )
        case "categorized_polygon":
            params = {**base_params}
            if field_name:
                params["field_name"] = field_name
            params["color_scheme"] = color_scheme
            return json.dumps(
                qgis.send_command("set_categorized_polygon_style", params), indent=2
            )
        case _:
            return json.dumps(
                {"status": "error", "message": f"Unknown style_type: {style_type}"},
                indent=2,
            )


@mcp.tool()
def style_raster(
    ctx: Context,
    action: str,
    layer_id: Optional[str] = None,
    layer_name: Optional[str] = None,
    transparency: Optional[float] = None,
    nodata_value: Optional[float] = None,
    band: int = 1,
    color_ramp_name: str = "Spectral",
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    interpolation: str = "interpolated",
    classes: int = 5,
) -> str:
    """
    Style raster layers: set transparency, colormap, or list available color ramps (DO NOT call this tool when loading data except user explicitly asks for it).

    Args:
        action: (REQUIRED) One of "set_transparency", "set_colormap", "list_color_ramps".
        layer_id: (REQUIRED if layer_name not provided for styling) ID of the layer.
        layer_name: (REQUIRED if layer_id not provided for styling) Name of the layer.
        transparency: (REQUIRED for "set_transparency") Overall layer transparency (0-100).
        nodata_value: (OPTIONAL) Pixel value to treat as NODATA.
        band: (OPTIONAL) Band number (default 1).
        color_ramp_name: (OPTIONAL for "set_colormap") Color ramp name (default "Spectral").
        min_value: (OPTIONAL) Min value for color mapping.
        max_value: (OPTIONAL) Max value for color mapping.
        interpolation: (OPTIONAL) "interpolated", "discrete", or "exact".
        classes: (OPTIONAL) Number of color classes for discrete mode.

    Examples:
        List available color ramps:
            style_raster(action="list_color_ramps")

        Set raster transparency to 50%:
            style_raster(action="set_transparency", layer_name="DEM", transparency=50)

        Set NODATA value and make it transparent:
            style_raster(action="set_transparency", layer_name="DEM", nodata_value=-9999)

        Apply a color ramp to a raster:
            style_raster(action="set_colormap", layer_name="elevation", color_ramp_name="Spectral", min_value=0, max_value=3000)

        Apply discrete color classes:
            style_raster(action="set_colormap", layer_name="temperature", color_ramp_name="RdYlBu", interpolation="discrete", classes=10)
    """
    qgis = get_qgis_connection()

    match action:
        case "list_color_ramps":
            return json.dumps(qgis.send_command("list_color_ramps"), indent=2)
        case "set_transparency":
            params = {}
            if layer_id:
                params["layer_id"] = layer_id
            if layer_name:
                params["layer_name"] = layer_name
            if transparency is not None:
                params["transparency"] = transparency
            if nodata_value is not None:
                params["nodata_value"] = nodata_value
                params["band"] = band
            return json.dumps(
                qgis.send_command("set_raster_transparency", params), indent=2
            )
        case "set_colormap":
            params = {
                "color_ramp_name": color_ramp_name,
                "interpolation": interpolation,
                "band": band,
                "classes": classes,
            }
            if layer_id:
                params["layer_id"] = layer_id
            if layer_name:
                params["layer_name"] = layer_name
            if min_value is not None:
                params["min_value"] = min_value
            if max_value is not None:
                params["max_value"] = max_value
            return json.dumps(
                qgis.send_command("set_raster_colormap", params), indent=2
            )
        case _:
            return json.dumps(
                {"status": "error", "message": f"Unknown action: {action}"}, indent=2
            )


@mcp.tool()
def manage_layer(
    ctx: Context,
    action: str,
    layer_id: Optional[str] = None,
    layer_name: Optional[str] = None,
    new_name: Optional[str] = None,
    limit: int = 10,
    filter_expression: Optional[str] = None,
    output_path: Optional[str] = None,
    target_crs: Optional[str] = None,
    driver_name: str = "ESRI Shapefile",
) -> str:
    """
    Manage layers: list, remove, rename, zoom, get features, or save to file.

    **CRITICAL - WHEN TO USE THIS TOOL:**
    - User wants to see all layers → action="list"
    - User wants to delete/remove a layer → action="remove"
    - User wants to change layer name → action="rename"
    - User wants to zoom to a layer → action="zoom"
    - User wants to see layer data/attributes → action="get_features"
    - User wants to export/save layer to file → action="save"

    **Examples:**
    - "Show me all layers" → manage_layer(action="list")
    - "Remove the roads layer" → manage_layer(action="remove", layer_name="roads")
    - "Rename layer to 'Cities'" → manage_layer(action="rename", layer_name="old_name", new_name="Cities")
    - "Zoom to the 'Boundary' layer" → manage_layer(action="zoom", layer_name="Boundary")
    - "Show me the first 20 features" → manage_layer(action="get_features", layer_name="rivers", limit=20)

    Args:
        action: (REQUIRED) One of "list", "remove", "rename", "zoom", "get_features", "save".
        layer_id: (OPTIONAL) ID of the layer.
        layer_name: (OPTIONAL) Name of the layer (use this if ID is unknown).
        new_name: (REQUIRED for "rename") New name for the layer.
        limit: (OPTIONAL) Max features to return (for "get_features", default 10).
        filter_expression: (OPTIONAL) QGIS expression to filter features (for "get_features").
        output_path: (REQUIRED for "save") Full file path for saving.
        target_crs: (OPTIONAL) CRS for reprojection when saving (e.g., "EPSG:4326").
        driver_name: (OPTIONAL) Output format for saving (default "ESRI Shapefile").
                    Options: "GeoJSON", "GPKG", "KML", "ESRI Shapefile"
    """
    qgis = get_qgis_connection()

    base_params = {}
    if layer_id:
        base_params["layer_id"] = layer_id
    if layer_name:
        base_params["layer_name"] = layer_name

    match action:
        case "list":
            return json.dumps(qgis.send_command("get_layers"), indent=2)
        case "remove":
            if not layer_id and not layer_name:
                return json.dumps(
                    {"status": "error", "message": "layer_id or layer_name required"},
                    indent=2,
                )
            return json.dumps(qgis.send_command("remove_layer", base_params), indent=2)
        case "rename":
            if (not layer_id and not layer_name) or not new_name:
                return json.dumps(
                    {
                        "status": "error",
                        "message": "layer identification and new_name required",
                    },
                    indent=2,
                )
            params = {**base_params, "new_name": new_name}
            return json.dumps(qgis.send_command("rename_layer", params), indent=2)
        case "zoom":
            if not layer_id and not layer_name:
                return json.dumps(
                    {"status": "error", "message": "layer identification required"},
                    indent=2,
                )
            return json.dumps(qgis.send_command("zoom_to_layer", base_params), indent=2)
        case "get_features":
            if not layer_id and not layer_name:
                return json.dumps(
                    {"status": "error", "message": "layer identification required"},
                    indent=2,
                )
            params = {**base_params, "limit": limit}
            if filter_expression:
                params["filter_expression"] = filter_expression
            return json.dumps(qgis.send_command("get_layer_features", params), indent=2)
        case "save":
            if (not layer_id and not layer_name) or not output_path:
                return json.dumps(
                    {
                        "status": "error",
                        "message": "layer identification and output_path required",
                    },
                    indent=2,
                )
            params = {
                **base_params,
                "output_path": output_path,
                "driver_name": driver_name,
            }
            if target_crs:
                params["target_crs"] = target_crs
            return json.dumps(qgis.send_command("save_layer", params), indent=2)
        case _:
            return json.dumps(
                {"status": "error", "message": f"Unknown action: {action}"}, indent=2
            )


@mcp.tool()
def execute_processing(ctx: Context, algorithm: str, parameters: dict) -> str:
    """
    Execute a processing algorithm with the given parameters.

    CRITICAL: Parameter names are CASE-SENSITIVE and algorithm-specific.
    You MUST use get_algorithm_help(algorithm_id) FIRST to discover the exact
    parameter names required by the algorithm.

    For example, gdal:cliprasterbymasklayer uses:
    - INPUT (not input_raster or input)
    - MASK (not mask_layer or mask)
    - OUTPUT (not output_file or output)

    Args:
        algorithm: (REQUIRED) Algorithm ID (e.g., "gdal:cliprasterbymasklayer")
        parameters: (REQUIRED) Dictionary of parameters with EXACT names from get_algorithm_help.
                   Layer parameters can be either layer IDs (strings) or layer objects.

    Workflow:
        1. Use list_processing_algorithms(search="clip") to find the algorithm
        2. Use get_algorithm_help(algorithm_id="gdal:cliprasterbymasklayer") to get parameter names
        3. Use execute_processing with the EXACT parameter names from step 2

    Examples:
        Buffer a vector layer:
            # First find the algorithm
            list_processing_algorithms(search="buffer")
            # Then get help to see parameter names
            get_algorithm_help(algorithm_id="native:buffer")
            # Finally execute with exact parameter names
            execute_processing(
                algorithm="native:buffer",
                parameters={
                    "INPUT": "layer_id_here",
                    "DISTANCE": 1000,
                    "OUTPUT": "/path/to/output.shp"
                }
            )

        Clip raster by mask layer:
            execute_processing(
                algorithm="gdal:cliprasterbymasklayer",
                parameters={
                    "INPUT": "raster_layer_id",
                    "MASK": "polygon_layer_id",
                    "OUTPUT": "/path/to/clipped.tif"
                }
            )

        Reproject a layer:
            execute_processing(
                algorithm="native:reprojectlayer",
                parameters={
                    "INPUT": "layer_id",
                    "TARGET_CRS": "EPSG:4326",
                    "OUTPUT": "/path/to/reprojected.shp"
                }
            )
    """
    qgis = get_qgis_connection()
    result = qgis.send_command(
        "execute_processing", {"algorithm": algorithm, "parameters": parameters}
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def list_processing_algorithms(
    ctx: Context, search: Optional[str] = None, limit: int = 50
) -> str:
    """
    List available QGIS processing algorithms.

    Use this tool to discover what processing algorithms are available in QGIS.
    This is essential when you need to perform operations like clipping, buffering,
    reprojecting, or any other spatial analysis.

    Args:
        search: (OPTIONAL) Search term to filter algorithms by name, ID, or group.
                For example: "clip", "buffer", "raster", "vector", etc.
        limit: (OPTIONAL) Maximum number of results to return (default 50)

    Returns:
        JSON with list of algorithms including their ID, name, and group.
        Use the algorithm ID with execute_processing or get_algorithm_help.

    Examples:
        list_processing_algorithms(search="buffer")
        list_processing_algorithms(search="clip", limit=10)

    Typical Workflow:
        1. Search for algorithms: list_processing_algorithms(search="clip raster")
        2. Get details: get_algorithm_help(algorithm_id="gdal:cliprasterbymasklayer")
        3. Execute: execute_processing(algorithm="gdal:cliprasterbymasklayer", parameters={...})
    """
    qgis = get_qgis_connection()
    result = qgis.send_command(
        "list_processing_algorithms", {"search": search, "limit": limit}
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def get_algorithm_help(ctx: Context, algorithm_id: str) -> str:
    """
    Get detailed help for a specific processing algorithm.

    Use this tool after finding an algorithm with list_processing_algorithms to understand
    what parameters it requires and what outputs it produces.

    Args:
        algorithm_id: (REQUIRED) The ID of the algorithm (e.g., "native:buffer", "gdal:cliprasterbymasklayer")

    Returns:
        JSON with detailed information including:
        - Algorithm name and description
        - List of parameters with their names, types, descriptions, and whether they're optional
        - List of outputs
        - Default values for parameters

    Example:
        get_algorithm_help(algorithm_id="native:buffer")
        get_algorithm_help(algorithm_id="gdal:cliprasterbymasklayer")
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("get_algorithm_help", {"algorithm_id": algorithm_id})
    return json.dumps(result, indent=2)


@mcp.tool()
def render_map(ctx: Context, path: str, width: int = 800, height: int = 600) -> str:
    """
    Render the current map view to an image file with the specified dimensions.

    Args:
        path: (REQUIRED) Full file path where the image will be saved (e.g., "/path/to/map.png").
        width: (OPTIONAL) Width of the output image in pixels (default 800).
        height: (OPTIONAL) Height of the output image in pixels (default 600).
    """
    qgis = get_qgis_connection()
    result = qgis.send_command(
        "render_map", {"path": path, "width": width, "height": height}
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary PyQGIS code provided as a string.

    **WARNING: This tool should ONLY be used as an ABSOLUTE LAST RESORT!**

    **BEFORE using this tool, you MUST:**
    1. Check if there's a dedicated tool for the task (load_data, manage_layer, style_layer, execute_processing, etc.)
    2. Explain to the user what you want to do and WHY execute_code is necessary
    3. Ask: "May I run this code?"
    4. Wait for explicit user approval

    **DO NOT use this tool for:**
    - Loading data (use load_data instead)
    - Styling layers (use style_layer or style_raster instead)
    - Processing operations (use execute_processing instead)
    - Layer management (use manage_layer instead)

    **Only use this for truly custom operations that have no dedicated tool.**

    Available in scope: iface, QgsProject, QgsApplication, QColor, QgsWkbTypes,
    and various symbol layer classes (QgsSimpleFillSymbolLayer, etc.)

    CRITICAL: After modifying layer styles/renderers, you MUST refresh the UI:
    iface.layerTreeView().refreshLayerSymbology(layer.id())

    Also call layer.triggerRepaint() to refresh the map canvas.

    Args:
        code: (REQUIRED) The PyQGIS code to execute.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_code", {"code": code})
    return json.dumps(result, indent=2)


@mcp.tool()
def manage_layout(
    ctx: Context,
    action: str,
    layout_name: Optional[str] = None,
    interval_x: float = 1.0,
    interval_y: float = 1.0,
    crs: Optional[str] = None,
    style: str = "Single Box",
    title: Optional[str] = None,
    font_size: int = 24,
    output_path: Optional[str] = None,
    format: str = "png",
    dpi: int = 300,
) -> str:
    """
    Manage print layouts: create, add elements, or export to file.

    Args:
        action: (REQUIRED) One of "create", "add_grid", "add_legend", "add_scalebar", "add_title", "export".
        layout_name: (REQUIRED for most actions) Name of the layout.
        interval_x: (OPTIONAL for "add_grid") Grid interval in X direction.
        interval_y: (OPTIONAL for "add_grid") Grid interval in Y direction.
        crs: (OPTIONAL for "add_grid") CRS for grid.
        style: (OPTIONAL for "add_scalebar") Scalebar style.
        title: (REQUIRED for "add_title") Title text.
        font_size: (OPTIONAL for "add_title") Font size for title (default 24).
        output_path: (OPTIONAL for "export") Path to save the exported file.
        format: (OPTIONAL for "export") "png", "pdf", "jpg", or "svg" (default "png").
        dpi: (OPTIONAL for "export") Export resolution (default 300).

    Examples:
        Create a new layout:
            manage_layout(action="create", layout_name="Main Map")

        Add a legend and title:
            manage_layout(action="add_legend", layout_name="Main Map")
            manage_layout(action="add_title", layout_name="Main Map", title="Project Area")

        Export to PDF:
            manage_layout(action="export", layout_name="Main Map", output_path="/path/to/map.pdf", format="pdf")
    """
    qgis = get_qgis_connection()

    match action:
        case "create":
            params = {}
            if layout_name:
                params["layout_name"] = layout_name
            return json.dumps(
                qgis.send_command("create_print_layout", params), indent=2
            )
        case "add_grid":
            params = {
                "layout_name": layout_name,
                "interval_x": interval_x,
                "interval_y": interval_y,
            }
            if crs:
                params["crs"] = crs
            return json.dumps(qgis.send_command("add_layout_grid", params), indent=2)
        case "add_legend":
            return json.dumps(
                qgis.send_command("add_layout_legend", {"layout_name": layout_name}),
                indent=2,
            )
        case "add_scalebar":
            return json.dumps(
                qgis.send_command(
                    "add_layout_scalebar", {"layout_name": layout_name, "style": style}
                ),
                indent=2,
            )
        case "add_title":
            if not title:
                return json.dumps(
                    {"status": "error", "message": "title required for add_title"},
                    indent=2,
                )
            return json.dumps(
                qgis.send_command(
                    "add_layout_title",
                    {
                        "title": title,
                        "layout_name": layout_name,
                        "font_size": font_size,
                    },
                ),
                indent=2,
            )
        case "export":
            params = {
                "layout_name": layout_name,
                "format": format,
                "dpi": dpi,
            }
            if output_path:
                params["output_path"] = output_path
            return json.dumps(qgis.send_command("export_layout", params), indent=2)
        case _:
            return json.dumps(
                {"status": "error", "message": f"Unknown action: {action}"}, indent=2
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the QGIS MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port for SSE (default: 8000)"
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the QGIS MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "both"],
        default="both",
        help="Transport type (stdio, sse, or both). 'both' runs sse in background and stdio in foreground.",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port for SSE (default: 8000)"
    )

    args = parser.parse_args()

    # Configure the SSE paths to match the user's request
    mcp.settings.sse_path = "/qgis"
    mcp.settings.message_path = "/qgis/messages"

    if args.transport == "sse":
        logger.info(f"Starting SSE server on port {args.port} at /qgis")
        app = mcp.sse_app()
        uvicorn.run(app, host="127.0.0.1", port=args.port)
    elif args.transport == "stdio":
        logger.info("Starting stdio server")
        mcp.run(transport="stdio")
    else:
        # 'both' mode
        logger.info(
            f"Starting dual transport: SSE on port {args.port} at /qgis and stdio"
        )

        # Start SSE in a background thread using uvicorn
        def run_sse():
            try:
                # Get the Starlette app from FastMCP
                app = mcp.sse_app()
                config = uvicorn.Config(
                    app, host="127.0.0.1", port=args.port, log_level="info"
                )
                server = uvicorn.Server(config)
                server.run()
            except Exception as e:
                logger.error(f"SSE server failed to start: {e}")

        sse_thread = threading.Thread(target=run_sse, daemon=True)
        sse_thread.start()

        # Start stdio in the foreground (main thread)
        mcp.run(transport="stdio")
