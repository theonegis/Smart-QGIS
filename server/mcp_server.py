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
            return None

_qgis_connection = None

def get_qgis_connection():
    """Get or create a persistent QGIS connection"""
    global _qgis_connection
    
    # If we have an existing connection, check if it's still valid
    if _qgis_connection is not None:
        # Test if the connection is still alive with a simple ping
        try:
            # Just try to send a small message to check if the socket is still connected
            _qgis_connection.sock.sendall(b'')
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
    """Execute a processing algorithm with the given parameters."""
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_processing", {"algorithm": algorithm, "parameters": parameters})
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
    """Execute arbitrary PyQGIS code provided as a string."""
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_code", {"code": code})
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


def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()
