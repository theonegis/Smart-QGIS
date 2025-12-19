"""
QGIS MCP Server - Simple server to connect to the QGIS socket server
Thanks to QGISMCP
https://github.com/jjsantos01/qgis_mcp
"""

import logging
from contextlib import asynccontextmanager
import socket
import json
from typing import AsyncIterator, Dict, Any, Optional
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("MCPServer")

class QgisMcpServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.socket = None
    
    def connect(self):
        """Connect to the QGIS socket server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
        if not self.socket:
            logger.error("Not connected to server")
            return None
        
        # Create command
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        try:
            # Send the command
            self.socket.sendall(json.dumps(command).encode('utf-8'))
            
            # Receive the response
            response_data = b''
            while True:
                chunk = self.socket.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                
                # Try to decode as JSON to see if it's complete
                try:
                    json.loads(response_data.decode('utf-8'))
                    break  # Valid JSON, we have the full message
                except json.JSONDecodeError:
                    continue  # Keep receiving
            
            # Parse and return the response
            return json.loads(response_data.decode('utf-8'))
            
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
            _qgis_connection.socket.sendall(b'')
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
        _qgis_connection = QgisMcpServer(host="localhost", port=9876)
        if not _qgis_connection.connect():
            logger.error("Failed to connect to QGIS")
            _qgis_connection = None
            raise Exception("Could not connect to QGIS. Make sure the QGIS plugin is running.")
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
            logger.warning("Make sure the QGIS addon is running before using QGIS resources or tools")
        
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
    "QgisMcp",
    instructions="QGIS integration through the MCP",
    lifespan=server_lifespan
)

@mcp.tool()
def ping(ctx: Context) -> str:
    """Simple ping command to check server connectivity"""
    qgis = get_qgis_connection()
    result = qgis.send_command("ping")
    return json.dumps(result, indent=2)

@mcp.tool()
def get_qgis_info(ctx: Context) -> str:
    """Get QGIS information"""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_qgis_info")
    return json.dumps(result, indent=2)

@mcp.tool()
def load_project(ctx: Context, path: str) -> str:
    """Load a QGIS project from the specified path."""
    qgis = get_qgis_connection()
    result = qgis.send_command("load_project", {"path": path})
    return json.dumps(result, indent=2)

@mcp.tool()
def create_new_project(ctx: Context, path: str) -> str:
    """Create a new project a save it"""
    qgis = get_qgis_connection()
    result = qgis.send_command("create_new_project", {"path": path})
    return json.dumps(result, indent=2)

@mcp.tool()
def get_project_info(ctx: Context) -> str:
    """Get current project information"""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_project_info")
    return json.dumps(result, indent=2)

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
        "temp": tempfile.gettempdir()
    }
    
    return json.dumps({"status": "success", "paths": paths}, indent=2)

@mcp.tool()
def add_vector_layer(ctx: Context, path: str, provider: str = "ogr", name: str = None) -> str:
    """Add a vector layer to the project."""
    qgis = get_qgis_connection()
    params = {"path": path, "provider": provider}
    if name:
        params["name"] = name
    result = qgis.send_command("add_vector_layer", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def add_raster_layer(ctx: Context, path: str, provider: str = "gdal", name: Optional[str] = None) -> str:
    """Add a raster layer to the project."""
    qgis = get_qgis_connection()
    params = {"path": path, "provider": provider}
    if name:
        params["name"] = name
    result = qgis.send_command("add_raster_layer", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def add_xyz_tile_layer(ctx: Context, url: Optional[str] = None, name: str = "XYZ Layer") -> str:
    """
    Add an XYZ tile layer to the project as a base map.
    
    Args:
        url: The URL of the XYZ tile service. If not provided, 'name' is checked against built-in sources.
             Built-in names: "Google Roadmap", "Google Terrain", "Google Satellite", "OpenStreetMap"
        name: The name of the layer (and key for built-in lookup if url is missing)
    """
    qgis = get_qgis_connection()
    params = {"name": name}
    if url:
        params["url"] = url
    result = qgis.send_command("add_xyz_tile_layer", params)
    return json.dumps(result, indent=2)

def _get_layer_id_smart(qgis, layer_id: Optional[str], layer_name: Optional[str], geometry_type: str) -> str:
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
        raise Exception(f"Multiple {geometry_type.lower()} layers found: {', '.join(names)}. Please specify a layer name or select the desired layer in QGIS")
    
    # Case 2: Layer name specified - fuzzy match
    name_matches = [l for l in candidates if layer_name.lower() in l["name"].lower()]
    if not name_matches:
        raise Exception(f"No {geometry_type.lower()} layer found matching name '{layer_name}'")
    
    # If multiple matches, prefer active
    active_matches = [l for l in name_matches if l.get("active")]
    if active_matches:
        return active_matches[0]["id"]
    
    # If only one match, use it
    if len(name_matches) == 1:
        return name_matches[0]["id"]
    
    # Multiple matches, none active
    names = [l["name"] for l in name_matches]
    raise Exception(f"Multiple {geometry_type.lower()} layers match '{layer_name}': {', '.join(names)}. Please be more specific or select the desired layer in QGIS")


@mcp.tool()
def set_point_layer_style(ctx: Context, layer_id: Optional[str] = None, layer_name: Optional[str] = None, color: Optional[str] = None, size: Optional[float] = None, shape: Optional[str] = None) -> str:
    """
    Set the style of a point layer.
    
    If no layer_id or layer_name is provided, this function will automatically:
    1. Use the currently active/focused point layer in QGIS, OR
    2. Use the only point layer if there's exactly one in the project
    
    Args:
        layer_id: Optional. The ID of the layer to style. Leave empty to use active layer.
        layer_name: Optional. The name (or partial name) of the layer. Leave empty to use active layer.
        color: The color of the marker (e.g., "red", "blue", "#FF0000").
        size: The size of the marker.
        shape: The shape of the marker. Supported: "circle", "square", "diamond", "cross", "star", "triangle".
    """
    qgis = get_qgis_connection()
    try:
        actual_layer_id = _get_layer_id_smart(qgis, layer_id, layer_name, "Point")
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)

    params = {"layer_id": actual_layer_id}
    if color: params["color"] = color
    if size is not None: params["size"] = size
    if shape: params["shape"] = shape
    
    result = qgis.send_command("set_point_layer_style", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def set_line_layer_style(ctx: Context, layer_id: Optional[str] = None, layer_name: Optional[str] = None, color: Optional[str] = None, width: Optional[float] = None, line_style: Optional[str] = None) -> str:
    """
    Set the style of a line layer.
    
    IMPORTANT: You can call this function WITHOUT specifying layer_id or layer_name!
    The function will automatically find the right layer:
    1. Use the currently active/focused line layer in QGIS, OR
    2. Use the only line layer if there's exactly one in the project
    
    Example usage:
    - set_line_layer_style(color="blue", width=2) - styles the active line layer
    - set_line_layer_style(layer_name="roads", color="red") - styles a specific layer
    
    Args:
        layer_id: Optional. The ID of the layer to style. Leave empty to use active layer.
        layer_name: Optional. The name (or partial name) of the layer. Leave empty to use active layer.
        color: The color of the line (e.g., "red", "blue", "#FF0000").
        width: The width of the line in map units.
        line_style: The style of the line. Supported: "solid", "dash", "dot", "dashdot", "dashdotdot".
    
    Returns:
        JSON string with status and message
    """
    qgis = get_qgis_connection()
    
    # Log the input parameters
    import sys
    print(f"DEBUG: set_line_layer_style called with layer_id={layer_id}, layer_name={layer_name}, color={color}, width={width}, line_style={line_style}", file=sys.stderr)
    
    try:
        actual_layer_id = _get_layer_id_smart(qgis, layer_id, layer_name, "Line")
        print(f"DEBUG: Found layer_id={actual_layer_id}", file=sys.stderr)
    except Exception as e:
        error_msg = f"Failed to find layer: {str(e)}"
        print(f"DEBUG: {error_msg}", file=sys.stderr)
        return json.dumps({"status": "error", "message": error_msg}, indent=2)

    params = {"layer_id": actual_layer_id}
    if color: params["color"] = color
    if width is not None: params["width"] = width
    if line_style: params["line_style"] = line_style
    
    print(f"DEBUG: Sending command with params={params}", file=sys.stderr)
    result = qgis.send_command("set_line_layer_style", params)
    print(f"DEBUG: Received result={result}", file=sys.stderr)
    
    return json.dumps(result, indent=2)

@mcp.tool()
def set_polygon_layer_style(
    ctx: Context, 
    layer_id: Optional[str] = None, 
    layer_name: Optional[str] = None, 
    fill_color: Optional[str] = None, 
    fill_style: Optional[str] = None, 
    stroke_color: Optional[str] = None,  # Primary - matches PyQGIS setStrokeColor()
    stroke_width: Optional[float] = None,  # Primary - matches PyQGIS setStrokeWidth()
    # Aliases for those who prefer "outline" terminology
    outline_color: Optional[str] = None, 
    outline_width: Optional[float] = None
) -> str:
    """
    Set the style of a polygon layer.
    
    **PREFERRED TOOL FOR POLYGON STYLING** - Use this instead of execute_code!
    
    IMPORTANT: You can call this function WITHOUT specifying layer_id or layer_name!
    The function will automatically find the right layer:
    1. Use the currently active/focused polygon layer in QGIS, OR
    2. Use the only polygon layer if there's exactly one in the project
    
    Common use cases:
    - **Hollow/No Fill**: set_polygon_layer_style(fill_color="transparent", stroke_color="black", stroke_width=1.5)
    - **Solid Color**: set_polygon_layer_style(fill_color="green")
    - **With Outline**: set_polygon_layer_style(fill_color="blue", stroke_color="red", stroke_width=2)
    - **Specific Layer**: set_polygon_layer_style(layer_name="countries", fill_color="yellow")
    
    PARAMETER NAMES (matching PyQGIS API):
        layer_id: Optional. The ID of the layer to style. Leave empty to use active layer.
        layer_name: Optional. The name (or partial name) of the layer. Leave empty to use active layer.
        fill_color: The fill color of the polygon (e.g., "red", "blue", "#FF0000").
                    **Set to "transparent", "hollow", "none", or "no_fill" for transparent fill (show only outline).**
        fill_style: The fill style. Supported: "solid", "horizontal", "vertical", "cross", "b_diagonal", "f_diagonal", "diagonal_cross", "no_brush".
        stroke_color OR outline_color: The color of the polygon boundary/edge (matches PyQGIS setStrokeColor).
        stroke_width OR outline_width: The width of the polygon boundary/edge in map units (matches PyQGIS setStrokeWidth).
    
    Returns:
        JSON string with status and message
    """
    qgis = get_qgis_connection()
    
    # Handle parameter aliases (outline_* is an alias for stroke_*)
    if outline_color and not stroke_color:
        stroke_color = outline_color
    if outline_width is not None and stroke_width is None:
        stroke_width = outline_width
    
    # Log the input parameters
    import sys
    print(f"DEBUG: set_polygon_layer_style called with layer_id={layer_id}, layer_name={layer_name}, fill_color={fill_color}, fill_style={fill_style}, stroke_color={stroke_color}, stroke_width={stroke_width}", file=sys.stderr)
    
    try:
        actual_layer_id = _get_layer_id_smart(qgis, layer_id, layer_name, "Polygon")
        print(f"DEBUG: Found layer_id={actual_layer_id}", file=sys.stderr)
    except Exception as e:
        error_msg = f"Failed to find layer: {str(e)}"
        print(f"DEBUG: {error_msg}", file=sys.stderr)
        return json.dumps({"status": "error", "message": error_msg}, indent=2)

    params = {"layer_id": actual_layer_id}
    if fill_color: params["fill_color"] = fill_color
    if fill_style: params["fill_style"] = fill_style
    # Send as outline_* to maintain compatibility with socket_server backend
    if stroke_color: params["outline_color"] = stroke_color
    if stroke_width is not None: params["outline_width"] = stroke_width
    
    print(f"DEBUG: Sending command with params={params}", file=sys.stderr)
    result = qgis.send_command("set_polygon_layer_style", params)
    print(f"DEBUG: Received result={result}", file=sys.stderr)
    
    return json.dumps(result, indent=2)

@mcp.tool()
def set_categorized_polygon_style(ctx: Context, layer_id: Optional[str] = None, layer_name: Optional[str] = None, field_name: Optional[str] = None, color_scheme: str = "random") -> str:
    """
    Set categorized styling for a polygon layer - each polygon gets a different color.
    
    TWO MODES:
    1. **Without field_name**: Each individual feature (polygon) gets a unique color
    2. **With field_name**: Features are categorized by field values (same value = same color)
    
    IMPORTANT: You can call this function WITHOUT specifying layer_id or layer_name!
    The function will automatically find the right layer.
    
    Example usage:
    - set_categorized_polygon_style() - each polygon gets a unique random color
    - set_categorized_polygon_style(color_scheme="rainbow") - each polygon gets a unique rainbow color
    - set_categorized_polygon_style(field_name="NAME") - categorize by NAME field with random colors
    - set_categorized_polygon_style(field_name="TYPE", color_scheme="gradient") - categorize by TYPE with gradient
    
    Args:
        layer_id: Optional. The ID of the layer to style. Leave empty to use active layer.
        layer_name: Optional. The name (or partial name) of the layer. Leave empty to use active layer.
        field_name: Optional. If provided, categorize by this field. If not provided, each feature gets unique color.
        color_scheme: Color scheme to use. Options: "random" (default), "rainbow", "gradient".
    
    Returns:
        JSON string with status and message
    """
    qgis = get_qgis_connection()
    
    # Log the input parameters
    import sys
    print(f"DEBUG: set_categorized_polygon_style called with layer_id={layer_id}, layer_name={layer_name}, field_name={field_name}, color_scheme={color_scheme}", file=sys.stderr)
    
    try:
        actual_layer_id = _get_layer_id_smart(qgis, layer_id, layer_name, "Polygon")
        print(f"DEBUG: Found layer_id={actual_layer_id}", file=sys.stderr)
    except Exception as e:
        error_msg = f"Failed to find layer: {str(e)}"
        print(f"DEBUG: {error_msg}", file=sys.stderr)
        return json.dumps({"status": "error", "message": error_msg}, indent=2)

    params = {
        "layer_id": actual_layer_id,
        "field_name": field_name,
        "color_scheme": color_scheme
    }
    
    print(f"DEBUG: Sending command with params={params}", file=sys.stderr)
    result = qgis.send_command("set_categorized_polygon_style", params)
    print(f"DEBUG: Received result={result}", file=sys.stderr)
    
    return json.dumps(result, indent=2)

@mcp.tool()
def set_raster_transparency(ctx: Context, layer_id: Optional[str] = None, layer_name: Optional[str] = None, transparency: Optional[float] = None, nodata_value: Optional[float] = None, band: int = 1) -> str:
    """
    Set transparency and/or NODATA values for a raster layer.
    
    IMPORTANT: You can call this function WITHOUT specifying layer_id or layer_name!
    The function will automatically find the right layer:
    1. Use the currently active/focused raster layer in QGIS, OR
    2. Use the only raster layer if there's exactly one in the project
    
    IMPORTANT: These are TWO INDEPENDENT settings:
    1. **Overall Layer Transparency** (transparency parameter): Makes the ENTIRE layer semi-transparent
    2. **NODATA Value** (nodata_value parameter): Marks specific pixel values as NODATA, which renders as FULLY TRANSPARENT
    
    DO NOT set transparency=100 when you only want to make NODATA values transparent!
    Setting transparency=100 makes the ENTIRE layer invisible, not just NODATA pixels.
    
    Args:
        layer_id: Optional. The exact layer ID. Leave empty to use active layer or layer_name.
        layer_name: Optional. The name (or partial name) of the layer. Leave empty to use active layer.
                   When user mentions a layer name like "Elevation", use this parameter, NOT layer_id.
        transparency: Optional. Overall layer transparency percentage (0-100). 
                     0 = fully opaque (default), 100 = fully transparent (entire layer invisible)
                     Use this ONLY when you want to make the whole layer semi-transparent.
        nodata_value: Optional. Pixel value to treat as NODATA (will render as fully transparent).
                     Use this when you want specific pixel values (like 0, -9999, etc.) to be transparent.
                     NODATA pixels are ALWAYS fully transparent regardless of the transparency parameter.
        band: Optional. Band number to apply NODATA value to (default: 1).
              Only relevant when setting nodata_value.
    
    Returns:
        JSON string with status and message
    """
    qgis = get_qgis_connection()
    
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
    
    result = qgis.send_command("set_raster_transparency", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def list_color_ramps(ctx: Context) -> str:
    """
    List all available color ramps (colormaps) in QGIS.
    
    Use this tool to discover what color ramps are available for styling raster layers.
    QGIS includes many built-in color ramps for different visualization needs.
    
    Returns:
        JSON with list of color ramp names and count
    
    Common color ramps include:
    - Sequential: Viridis, Plasma, Inferno, Magma, Blues, Greens, Reds, Greys
    - Diverging: Spectral, RdYlGn, RdYlBu, RdBu, BrBG, PiYG
    - Qualitative: Set1, Set2, Set3, Paired, Accent, Pastel1
    
    Example:
        list_color_ramps() → Returns all available color ramp names
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("list_color_ramps")
    return json.dumps(result, indent=2)

@mcp.tool()
def set_raster_colormap(
    ctx: Context,
    layer_id: Optional[str] = None,
    layer_name: Optional[str] = None,
    color_ramp_name: str = "Spectral",
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    interpolation: str = "interpolated",
    band: int = 1,
    classes: int = 5
) -> str:
    """
    Apply a color ramp (colormap) to a raster layer for visualization.
    
    IMPORTANT: You can call this function WITHOUT specifying layer_id or layer_name!
    The function will automatically use the active raster layer or the only raster layer.
    
    This function applies pseudocolor rendering to single-band raster data, making it
    easier to visualize elevation, temperature, or other continuous data.
    
    Args:
        layer_id: Optional. The exact layer ID. Leave empty to use active layer or layer_name.
        layer_name: Optional. The name (or partial name) of the layer. Leave empty to use active layer.
        color_ramp_name: Name of the color ramp to apply (default: "Spectral").
                        Use list_color_ramps() to see available options.
                        Common choices: "Spectral", "Viridis", "RdYlGn", "Plasma", "Inferno"
        min_value: Optional. Minimum value for color mapping. Auto-detected if not provided.
        max_value: Optional. Maximum value for color mapping. Auto-detected if not provided.
        interpolation: Interpolation mode (default: "interpolated")
                      - "interpolated": Smooth color transitions (best for continuous data like DEM)
                      - "discrete": Distinct color classes (good for categorized data)
                      - "exact": Only exact values get colors
        band: Band number to apply colormap to (default: 1)
        classes: Number of color classes for discrete mode (default: 5)
    
    Returns:
        JSON string with status, applied settings, and value range
    """
    qgis = get_qgis_connection()
    
    params = {
        "color_ramp_name": color_ramp_name,
        "interpolation": interpolation,
        "band": band,
        "classes": classes
    }
    
    if layer_id:
        params["layer_id"] = layer_id
    if layer_name:
        params["layer_name"] = layer_name
    
    if min_value is not None:
        params["min_value"] = min_value
    if max_value is not None:
        params["max_value"] = max_value
    
    result = qgis.send_command("set_raster_colormap", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def get_layers(ctx: Context) -> str:
    """Retrieve all layers in the current project."""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_layers")
    return json.dumps(result, indent=2)

@mcp.tool()
def remove_layer(ctx: Context, layer_id: str) -> str:
    """Remove a layer from the project by its ID."""
    qgis = get_qgis_connection()
    result = qgis.send_command("remove_layer", {"layer_id": layer_id})
    return json.dumps(result, indent=2)


@mcp.tool()
def rename_layer(ctx: Context, layer_id: str, new_name: str) -> str:
    """
    Rename a layer in the project.
    
    Args:
        layer_id: The ID of the layer to rename
        new_name: The new name for the layer
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("rename_layer", {"layer_id": layer_id, "new_name": new_name})
    return json.dumps(result, indent=2)

@mcp.tool()
def zoom_to_layer(ctx: Context, layer_id: str) -> str:
    """Zoom to the extent of a specified layer."""
    qgis = get_qgis_connection()
    result = qgis.send_command("zoom_to_layer", {"layer_id": layer_id})
    return json.dumps(result, indent=2)

@mcp.tool()
def get_layer_features(ctx: Context, layer_id: str, limit: int = 10, filter_expression: Optional[str] = None) -> str:
    """
    Retrieve features from a vector layer with an optional limit and filter expression.
    
    Args:
        layer_id: The ID of the layer
        limit: Maximum number of features to return (default 10)
        filter_expression: QGIS expression to filter features.
                           Examples:
                           - "\"name\" = 'Paris'" (Double quotes for field, single for string)
                           - "\"population\" > 1000000"
                           - "\"type\" IN ('City', 'Town')"
    """
    qgis = get_qgis_connection()
    params = {"layer_id": layer_id, "limit": limit}
    if filter_expression:
        params["filter_expression"] = filter_expression
    result = qgis.send_command("get_layer_features", params)
    return json.dumps(result, indent=2)

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
        algorithm: Algorithm ID (e.g., "gdal:cliprasterbymasklayer")
        parameters: Dictionary of parameters with EXACT names from get_algorithm_help.
                   Layer parameters can be either layer IDs (strings) or layer objects.
    
    Workflow:
        1. Use list_processing_algorithms(search="clip") to find the algorithm
        2. Use get_algorithm_help(algorithm_id="gdal:cliprasterbymasklayer") to get parameter names
        3. Use execute_processing with the EXACT parameter names from step 2
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_processing", {"algorithm": algorithm, "parameters": parameters})
    return json.dumps(result, indent=2)


@mcp.tool()
def list_processing_algorithms(ctx: Context, search: Optional[str] = None, limit: int = 50) -> str:
    """
    List available QGIS processing algorithms.
    
    Use this tool to discover what processing algorithms are available in QGIS.
    This is essential when you need to perform operations like clipping, buffering,
    reprojecting, or any other spatial analysis.
    
    Args:
        search: Optional search term to filter algorithms by name, ID, or group.
                For example: "clip", "buffer", "raster", "vector", etc.
        limit: Maximum number of results to return (default 50)
    
    Returns:
        JSON with list of algorithms including their ID, name, and group.
        Use the algorithm ID with execute_processing or get_algorithm_help.
    
    Example workflow:
        1. Search for algorithms: list_processing_algorithms(search="clip raster")
        2. Get details: get_algorithm_help(algorithm_id="gdal:cliprasterbymasklayer")
        3. Execute: execute_processing(algorithm="gdal:cliprasterbymasklayer", parameters={...})
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("list_processing_algorithms", {"search": search, "limit": limit})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_algorithm_help(ctx: Context, algorithm_id: str) -> str:
    """
    Get detailed help for a specific processing algorithm.
    
    Use this tool after finding an algorithm with list_processing_algorithms to understand
    what parameters it requires and what outputs it produces.
    
    Args:
        algorithm_id: The ID of the algorithm (e.g., "native:buffer", "gdal:cliprasterbymasklayer")
    
    Returns:
        JSON with detailed information including:
        - Algorithm name and description
        - List of parameters with their names, types, descriptions, and whether they're optional
        - List of outputs
        - Default values for parameters
    
    Example:
        get_algorithm_help(algorithm_id="gdal:cliprasterbymasklayer")
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("get_algorithm_help", {"algorithm_id": algorithm_id})
    return json.dumps(result, indent=2)



@mcp.tool()
def save_project(ctx: Context, path: str) -> str:
    """Save the current project to the given path, or to the current project path if not specified."""
    qgis = get_qgis_connection()
    params = {}
    if path:
        params["path"] = path
    result = qgis.send_command("save_project", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def save_layer(ctx: Context, layer_id: str, output_path: str, target_crs: Optional[str] = None, driver_name: str = "ESRI Shapefile") -> str:
    """
    Save a layer to a file with optional reprojection.
    
    Args:
        layer_id: The ID of the layer to save
        output_path: Full path where the file should be saved (e.g., "/Users/username/Desktop/layer.shp")
        target_crs: Optional CRS to reproject to (e.g., "EPSG:4610" for Gauss-Kruger). If not specified, uses the layer's CRS.
        driver_name: Output driver name (default: "ESRI Shapefile"). Other options: "GeoJSON", "GPKG", "KML", etc.
    """
    qgis = get_qgis_connection()
    params = {
        "layer_id": layer_id,
        "output_path": output_path,
        "driver_name": driver_name
    }
    if target_crs:
        params["target_crs"] = target_crs
    result = qgis.send_command("save_layer", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def render_map(ctx: Context, path: str, width: int = 800, height: int = 600) -> str:
    """Render the current map view to an image file with the specified dimensions."""
    qgis = get_qgis_connection()
    result = qgis.send_command("render_map", {"path": path, "width": width, "height": height})
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary PyQGIS code provided as a string.
    
    Available in scope: iface, QgsProject, QgsApplication, QColor, QgsWkbTypes,
    and various symbol layer classes (QgsSimpleFillSymbolLayer, etc.)
    
    CRITICAL: After modifying layer styles/renderers, you MUST refresh the UI:
    iface.layerTreeView().refreshLayerSymbology(layer.id())
    
    Also call layer.triggerRepaint() to refresh the map canvas.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_code", {"code": code})
    return json.dumps(result, indent=2)


@mcp.tool()
def add_layout_grid(ctx: Context, layout_name: Optional[str] = None, interval_x: float = 1.0, interval_y: float = 1.0, crs: Optional[str] = None) -> str:
    """
    Add a coordinate grid to the map in a print layout.
    
    Args:
        layout_name: Name of the layout. If not provided, uses the most recent one.
        interval_x: Grid interval in X direction (map units).
        interval_y: Grid interval in Y direction (map units).
        crs: Coordinate Reference System for the grid (e.g., "EPSG:4326").
    """
    qgis = get_qgis_connection()
    params = {
        "layout_name": layout_name,
        "interval_x": interval_x,
        "interval_y": interval_y
    }
    if crs:
        params["crs"] = crs
    result = qgis.send_command("add_layout_grid", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def add_layout_legend(ctx: Context, layout_name: Optional[str] = None) -> str:
    """
    Add a legend to a print layout.
    
    The legend will be automatically linked to the map and positioned at the bottom-right.
    
    Args:
        layout_name: Name of the layout. If not provided, uses the most recent one.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("add_layout_legend", {"layout_name": layout_name})
    return json.dumps(result, indent=2)

@mcp.tool()
def add_layout_scalebar(ctx: Context, layout_name: Optional[str] = None, style: str = "Single Box") -> str:
    """
    Add a scale bar to a print layout.
    
    The scale bar will be automatically linked to the map and positioned at the bottom-left.
    
    Args:
        layout_name: Name of the layout. If not provided, uses the most recent one.
        style: Scale bar style. Options: "Single Box", "Double Box", "Line Ticks Middle", "Line Ticks Down", "Line Ticks Up", "Numeric".
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("add_layout_scalebar", {"layout_name": layout_name, "style": style})
    return json.dumps(result, indent=2)

@mcp.tool()
def add_layout_title(ctx: Context, title: str, layout_name: Optional[str] = None, font_size: int = 24) -> str:
    """
    Add a title to a print layout.
    
    The title will be centered at the top of the page.
    
    Args:
        title: The text of the title.
        layout_name: Name of the layout. If not provided, uses the most recent one.
        font_size: Font size in points (default: 24).
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("add_layout_title", {"title": title, "layout_name": layout_name, "font_size": font_size})
    return json.dumps(result, indent=2)

@mcp.tool()
def create_memory_layer(ctx: Context, name: str, geometry_type: str = "Point", crs: str = "EPSG:4326", fields: list = []) -> str:
    """
    Create a new temporary (memory) vector layer.
    
    Args:
        name: Layer name
        geometry_type: "Point", "LineString", "Polygon", "MultiPoint", etc.
        crs: Coordinate Reference System authid (e.g., "EPSG:4326")
        fields: List of field definitions, e.g., [{"name": "id", "type": "int"}, {"name": "desc", "type": "string"}]
    """
    qgis = get_qgis_connection()
    params = {
        "name": name,
        "geometry_type": geometry_type,
        "crs": crs,
        "fields": fields
    }
    result = qgis.send_command("create_memory_layer", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def add_features(ctx: Context, layer_id: str, features: list) -> str:
    """
    Add features to a vector layer.
    
    Args:
        layer_id: The ID of the layer
        features: List of features to add. Each feature should be a dict with optional "geometry" (WKT string) and "attributes" (dict).
                  Example: [{"geometry": "POINT(0 0)", "attributes": {"id": 1, "desc": "Origin"}}]
    """
    qgis = get_qgis_connection()
    params = {
        "layer_id": layer_id,
        "features": features
    }
    result = qgis.send_command("add_features", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def extract_layer_to_memory(ctx: Context, source_layer_id: str, filter_expression: str, new_layer_name: str = "Extracted Layer") -> str:
    """
    Extract features from an existing layer to a new memory layer based on a filter expression.
    Use this for "extract", "filter", or "subset" operations.
    
    Args:
        source_layer_id: The ID of the source layer
        filter_expression: QGIS expression to filter features (e.g. "\"name\" = 'Paris'")
        new_layer_name: Name for the new temporary layer
    """
    qgis = get_qgis_connection()
    params = {
        "source_layer_id": source_layer_id,
        "filter_expression": filter_expression,
        "new_layer_name": new_layer_name
    }
    result = qgis.send_command("extract_layer_to_memory", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def create_print_layout(ctx: Context, layout_name: Optional[str] = None) -> str:
    """
    Create a new Print Layout in QGIS. Adds a map item showing current layers.
    If "layout_name" is not given, uses current time as layout name.
    """
    qgis = get_qgis_connection()
    params = {}
    if layout_name:
        params["layout_name"] = layout_name
    result = qgis.send_command("create_print_layout", params)
    return json.dumps(result, indent=2)


def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()
