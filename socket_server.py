import socket
import json
import threading
import traceback
from qgis.core import *
from qgis import processing
from qgis.PyQt import QtCore, QtGui
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *

"""
1. MCP Server sends JSON: {"type": "zoom_to_layer", ...}
2. SocketServer (Background Thread) receives it.
3. SocketServer calls handler.execute_sync().
4. RequestHandler emits signal -> Main Thread wakes up.
5. Main Thread runs `action_zoom_to_layer` (Safe QGIS API call).
6. Main Thread sends result back to Background Thread.
7. SocketServer sends JSON response back to MCP.
"""

LOG_TAG = "QGIS AI"


class RequestHandler(QObject):
    # Signal to trigger execution on the main thread
    sig_handle_request = pyqtSignal(object, object, object)

    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        # Connect signal to slot with QueuedConnection to ensure it runs on main thread
        self.sig_handle_request.connect(self.slot_handle_request, Qt.QueuedConnection)

    def execute_sync(self, command):
        """
        Called from the worker thread. Emits a signal to the main thread and waits for the result.
        """
        event = threading.Event()
        result_container = {}
        self.sig_handle_request.emit(command, event, result_container)
        event.wait()
        return result_container.get('result', {"status": "error", "message": "Execution failed or timed out"})

    @pyqtSlot(object, object, object)
    def slot_handle_request(self, command, event, result_container):
        """
        Slot running on the main thread.
        """
        try:
            result = self.handle_request(command)
            result_container['result'] = result
        except Exception as e:
            result_container['result'] = {"status": "error", "message": str(e)}
        finally:
            event.set()

    def _get_layer_by_id(self, layer_id):
        return QgsProject.instance().mapLayer(layer_id)

    def handle_request(self, command):
        """
        Dispatch method called from the slot (main thread).
        """
        cmd_type = command.get("type")
        params = command.get("params", {})

        try:
            method_name = f"action_{cmd_type}"
            if hasattr(self, method_name):
                return getattr(self, method_name)(params)
            else:
                return {"status": "error", "message": f"Unknown command: {cmd_type}"}
        except Exception as e:
            return {"status": "error", "message": str(e), "traceback": traceback.format_exc()}

    # --- Actions ---

    @staticmethod
    def action_ping(params):
        return {"status": "success", "message": "pong"}

    @staticmethod
    def action_get_qgis_info(params):
        return {
            "status": "success",
            "version": Qgis.version(),
            "release_name": Qgis.releaseName()
        }

    @staticmethod
    def action_load_project(params):
        path = params.get("path")
        if not path:
            return {"status": "error", "message": "Path is required"}

        success = QgsProject.instance().read(path)
        if success:
            return {"status": "success"}
        else:
            return {"status": "error", "message": "Failed to read project"}

    @staticmethod
    def action_create_new_project(params):
        QgsProject.instance().clear()
        return {"status": "success"}

    @staticmethod
    def action_get_project_info(params):
        project = QgsProject.instance()
        return {
            "status": "success",
            "file_name": project.fileName(),
            "crs": project.crs().authid(),
            "layers": [{"id": l.id(), "name": l.name(), "type": l.type().name} for l in project.mapLayers().values()]
        }

    def action_add_vector_layer(self, params):
        import os
        path = params.get("path")
        name = params.get("name")
        provider = params.get("provider", "ogr")
        
        # If name is not provided, extract filename without extension
        if not name:
            name = os.path.splitext(os.path.basename(path))[0]

        layer = self.iface.addVectorLayer(path, name, provider)
        if layer and layer.isValid():
            return {"status": "success", "layer_id": layer.id(), "name": layer.name()}
        else:
            return {"status": "error", "message": "Failed to load layer"}

    def action_add_raster_layer(self, params):
        import os
        path = params.get("path")
        name = params.get("name")
        provider = params.get("provider", "gdal")
        
        # If name is not provided, extract filename without extension
        if not name:
            name = os.path.splitext(os.path.basename(path))[0]

        layer = self.iface.addRasterLayer(path, name, provider)
        if layer and layer.isValid():
            return {"status": "success", "layer_id": layer.id(), "name": layer.name()}
        else:
            return {"status": "error", "message": "Failed to load layer"}

    def action_add_xyz_tile_layer(self, params):
        url = params.get("url")
        name = params.get("name", "XYZ Layer")
        
        # Built-in URLs
        builtin_urls = {
            "Google Roadmap": "http://mt0.google.com/vt/lyrs=m&hl=en&x={x}&y={y}&z={z}",
            "Google Terrain": "http://mt0.google.com/vt/lyrs=p&hl=en&x={x}&y={y}&z={z}",
            "Google Satellite": "http://mt0.google.com/vt/lyrs=s&hl=en&x={x}&y={y}&z={z}",
            "OpenStreetMap": "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        }
        
        if not url:
            # Try to find by name (case-insensitive lookup)
            for key, val in builtin_urls.items():
                if key.lower() == name.lower():
                    url = val
                    # Use the proper casing for the name if it was a match
                    if name.lower() == "google roadmap": name = "Google Roadmap"
                    elif name.lower() == "google terrain": name = "Google Terrain"
                    elif name.lower() == "google satellite": name = "Google Satellite"
                    elif name.lower() == "openstreetmap": name = "OpenStreetMap"
                    break
        
        if not url:
            return {"status": "error", "message": "URL not provided and name not found in built-in list"}
            
        # Construct XYZ layer URI
        # type=xyz&url=...&zmin=0&zmax=22
        uri = f"type=xyz&url={url}&zmin=0&zmax=22"
        
        layer = self.iface.addRasterLayer(uri, name, "wms")
        if layer and layer.isValid():
            return {"status": "success", "layer_id": layer.id(), "name": layer.name()}
        else:
            return {"status": "error", "message": "Failed to load XYZ layer"}

    def action_set_point_layer_style(self, params):
        layer_id = params.get("layer_id")
        color = params.get("color")
        size = params.get("size")
        shape = params.get("shape")
        
        layer = self._get_layer_by_id(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return {"status": "error", "message": "Invalid vector layer"}
            
        if layer.geometryType() != QgsWkbTypes.PointGeometry:
             return {"status": "error", "message": "Layer is not a point layer"}

        # Get current renderer or create a new single symbol renderer
        renderer = layer.renderer()
        if not isinstance(renderer, QgsSingleSymbolRenderer):
            symbol = QgsMarkerSymbol.createSimple({})
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)
        
        symbol = renderer.symbol()
        if not isinstance(symbol, QgsMarkerSymbol):
             return {"status": "error", "message": "Layer does not use a marker symbol"}
             
        if symbol.symbolLayerCount() > 0:
            sym_layer = symbol.symbolLayer(0)
            if isinstance(sym_layer, QgsSimpleMarkerSymbolLayer):
                if color:
                    sym_layer.setColor(QColor(color))
                    sym_layer.setStrokeColor(QColor("black"))
                if size is not None:
                    sym_layer.setSize(float(size))
                if shape:
                    shape_map = {
                        "circle": QgsSimpleMarkerSymbolLayer.Circle,
                        "square": QgsSimpleMarkerSymbolLayer.Square,
                        "rectangle": QgsSimpleMarkerSymbolLayer.Square,
                        "diamond": QgsSimpleMarkerSymbolLayer.Diamond,
                        "cross": QgsSimpleMarkerSymbolLayer.Cross,
                        "star": QgsSimpleMarkerSymbolLayer.Star,
                        "triangle": QgsSimpleMarkerSymbolLayer.Triangle
                    }
                    if shape.lower() in shape_map:
                        sym_layer.setShape(shape_map[shape.lower()])
        
        layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        return {"status": "success"}

    def action_set_line_layer_style(self, params):
        layer_id = params.get("layer_id")
        color = params.get("color")
        width = params.get("width")
        line_style = params.get("line_style")
        
        layer = self._get_layer_by_id(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return {"status": "error", "message": "Invalid vector layer"}
            
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
             return {"status": "error", "message": "Layer is not a line layer"}

        renderer = layer.renderer()
        if not isinstance(renderer, QgsSingleSymbolRenderer):
            symbol = QgsLineSymbol.createSimple({})
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)
        
        symbol = renderer.symbol()
        if not isinstance(symbol, QgsLineSymbol):
             return {"status": "error", "message": "Layer does not use a line symbol"}
            
        if symbol.symbolLayerCount() > 0:
            sym_layer = symbol.symbolLayer(0)
            if isinstance(sym_layer, QgsSimpleLineSymbolLayer):
                if color:
                    sym_layer.setColor(QColor(color))
                if width is not None:
                    sym_layer.setWidth(float(width))
                if line_style:
                    style_map = {
                        "solid": Qt.SolidLine,
                        "dash": Qt.DashLine,
                        "dashed": Qt.DashLine,
                        "dot": Qt.DotLine,
                        "dotted": Qt.DotLine,
                        "dashdot": Qt.DashDotLine,
                        "dashdotdot": Qt.DashDotDotLine
                    }
                    if line_style.lower() in style_map:
                        sym_layer.setPenStyle(style_map[line_style.lower()])
        
        layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        return {"status": "success"}

    def action_set_polygon_layer_style(self, params):
        try:
            QgsMessageLog.logMessage(f"action_set_polygon_layer_style called with params: {params}", LOG_TAG, Qgis.Info)
            
            layer_id = params.get("layer_id")
            fill_color = params.get("fill_color")
            fill_style = params.get("fill_style")
            outline_color = params.get("outline_color")
            outline_width = params.get("outline_width")
            
            layer = self._get_layer_by_id(layer_id)
            if not layer or not isinstance(layer, QgsVectorLayer):
                return {"status": "error", "message": "Invalid vector layer"}
                
            if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                 return {"status": "error", "message": "Layer is not a polygon layer"}

            renderer = layer.renderer()
            if not isinstance(renderer, QgsSingleSymbolRenderer):
                symbol = QgsFillSymbol.createSimple({})
                renderer = QgsSingleSymbolRenderer(symbol)
                layer.setRenderer(renderer)
            
            symbol = renderer.symbol()
            if not isinstance(symbol, QgsFillSymbol):
                 return {"status": "error", "message": "Layer does not use a fill symbol"}
                
            if symbol.symbolLayerCount() > 0:
                sym_layer = symbol.symbolLayer(0)
                if isinstance(sym_layer, QgsSimpleFillSymbolLayer):
                    if fill_color:
                        sym_layer.setColor(QColor(fill_color))
                    if fill_style:
                        style_map = {
                            "solid": Qt.SolidPattern,
                            "horizontal": Qt.HorPattern,
                            "vertical": Qt.VerPattern,
                            "cross": Qt.CrossPattern,
                            "b_diagonal": Qt.BDiagPattern,
                            "f_diagonal": Qt.FDiagPattern,
                            "diagonal_cross": Qt.DiagCrossPattern,
                            "no_brush": Qt.NoBrush
                        }
                        if fill_style.lower() in style_map:
                            sym_layer.setBrushStyle(style_map[fill_style.lower()])
                    if outline_color:
                        sym_layer.setStrokeColor(QColor(outline_color))
                    if outline_width is not None:
                        sym_layer.setStrokeWidth(float(outline_width))
            
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            
            QgsMessageLog.logMessage("action_set_polygon_layer_style completed successfully", LOG_TAG, Qgis.Info)
            return {"status": "success"}
        except Exception as e:
            error_msg = f"Error in action_set_polygon_layer_style: {str(e)}"
            QgsMessageLog.logMessage(error_msg, LOG_TAG, Qgis.Critical)
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.Critical)
            return {"status": "error", "message": error_msg}

    def action_set_categorized_polygon_style(self, params):
        layer_id = params.get("layer_id")
        field_name = params.get("field_name")
        color_scheme = params.get("color_scheme", "random")
        
        layer = self._get_layer_by_id(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return {"status": "error", "message": "Invalid vector layer"}
            
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
             return {"status": "error", "message": "Layer is not a polygon layer"}
        
        # Generate colors based on scheme
        import random
        import colorsys
        
        categories = []
        
        # MODE 1: No field_name - unique color per feature
        if not field_name:
            # Use feature ID as the categorization field
            # Create a temporary field "$id" that contains the feature ID
            num_features = layer.featureCount()
            if num_features == 0:
                return {"status": "error", "message": "Layer has no features"}
            
            # Get all feature IDs
            feature_ids = [f.id() for f in layer.getFeatures()]
            
            for i, fid in enumerate(feature_ids):
                # Generate color based on scheme
                if color_scheme == "rainbow":
                    hue = i / num_features
                    rgb = colorsys.hsv_to_rgb(hue, 0.8, 0.9)
                    color = QColor(int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))
                elif color_scheme == "gradient":
                    ratio = i / max(num_features - 1, 1)
                    color = QColor(int(ratio * 255), 0, int((1 - ratio) * 255))
                else:  # random
                    color = QColor(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                
                # Create symbol for this feature
                symbol = QgsFillSymbol.createSimple({
                    'color': color.name(),
                    'outline_color': 'black',
                    'outline_width': '0.26'
                })
                
                # Create category using feature ID
                category = QgsRendererCategory(fid, symbol, f"Feature {fid}")
                categories.append(category)
            
            # Create categorized renderer using $id field
            renderer = QgsCategorizedSymbolRenderer("$id", categories)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            
            return {"status": "success", "message": f"Applied {color_scheme} unique colors to {num_features} features"}
        
        # MODE 2: With field_name - categorize by field values
        else:
            # Check if field exists
            field_index = layer.fields().indexOf(field_name)
            if field_index == -1:
                available_fields = [f.name() for f in layer.fields()]
                return {"status": "error", "message": f"Field '{field_name}' not found. Available fields: {', '.join(available_fields)}"}
            
            # Get unique values from the field
            unique_values = layer.uniqueValues(field_index)
            if not unique_values:
                return {"status": "error", "message": f"No values found in field '{field_name}'"}
            
            num_categories = len(unique_values)
            
            for i, value in enumerate(sorted(unique_values, key=lambda x: str(x))):
                # Generate color based on scheme
                if color_scheme == "rainbow":
                    hue = i / num_categories
                    rgb = colorsys.hsv_to_rgb(hue, 0.8, 0.9)
                    color = QColor(int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))
                elif color_scheme == "gradient":
                    ratio = i / max(num_categories - 1, 1)
                    color = QColor(int(ratio * 255), 0, int((1 - ratio) * 255))
                else:  # random
                    color = QColor(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                
                # Create symbol for this category
                symbol = QgsFillSymbol.createSimple({
                    'color': color.name(),
                    'outline_color': 'black',
                    'outline_width': '0.26'
                })
                
                # Create category
                category = QgsRendererCategory(value, symbol, str(value))
                categories.append(category)
            
            # Create and apply categorized renderer
            renderer = QgsCategorizedSymbolRenderer(field_name, categories)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            
            return {"status": "success", "message": f"Applied {color_scheme} categorized styling with {num_categories} categories based on field '{field_name}'"}

    def _resolve_layer(self, layer_id, layer_name, layer_type_class=None):
        """
        Smart layer resolution helper.
        Priority:
        1. layer_id (exact match)
        2. layer_name (fuzzy match)
        3. Active layer (if matches type)
        4. Single layer of type (if only one exists)
        """
        # 1. Try by ID
        if layer_id:
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer:
                if layer_type_class and not isinstance(layer, layer_type_class):
                    return None, f"Layer with ID {layer_id} is not of type {layer_type_class.__name__}"
                return layer, None
            return None, f"Layer with ID {layer_id} not found"

        # Get all layers
        layers = list(QgsProject.instance().mapLayers().values())
        
        # Filter by type if specified
        if layer_type_class:
            layers = [l for l in layers if isinstance(l, layer_type_class)]
            if not layers:
                return None, f"No layers of type {layer_type_class.__name__} found in project"

        # 2. Try by Name (if provided)
        if layer_name:
            # Fuzzy match
            matches = [l for l in layers if layer_name.lower() in l.name().lower()]
            
            if not matches:
                # Fallback to active layer if it matches type
                active_layer = self.iface.activeLayer()
                if active_layer and (not layer_type_class or isinstance(active_layer, layer_type_class)):
                    return active_layer, None
                
                # Fallback to single layer if only one exists
                if len(layers) == 1:
                    return layers[0], None
                    
                return None, f"No layer found matching '{layer_name}'"
            
            if len(matches) == 1:
                return matches[0], None
            
            # Multiple matches - try to find active one
            active_matches = [l for l in matches if l == self.iface.activeLayer()]
            if active_matches:
                return active_matches[0], None
                
            names = [l.name() for l in matches]
            return None, f"Multiple layers match '{layer_name}': {', '.join(names)}"

        # 3. No ID or Name - Try Active Layer
        active_layer = self.iface.activeLayer()
        if active_layer:
            if not layer_type_class or isinstance(active_layer, layer_type_class):
                return active_layer, None

        # 4. Try Single Layer
        if len(layers) == 1:
            return layers[0], None

        return None, "No layer specified and could not determine active/single layer"

    def action_set_raster_transparency(self, params):
        """Set transparency and NODATA values for a raster layer."""
        try:
            layer_id = params.get("layer_id")
            layer_name = params.get("layer_name")
            transparency = params.get("transparency")  # 0-100 percentage
            nodata_value = params.get("nodata_value")
            band = params.get("band", 1)  # Default to band 1
            
            layer, error = self._resolve_layer(layer_id, layer_name, QgsRasterLayer)
            if not layer:
                return {"status": "error", "message": error}
            
            # Double check type just in case (though _resolve_layer handles it)
            if not isinstance(layer, QgsRasterLayer):
                return {"status": "error", "message": "Layer is not a raster layer"}
            
            # Set overall layer transparency if provided
            if transparency is not None:
                # Convert percentage (0-100) to opacity (0.0-1.0)
                # transparency=0 means fully opaque (opacity=1.0)
                # transparency=100 means fully transparent (opacity=0.0)
                opacity = 1.0 - (float(transparency) / 100.0)
                opacity = max(0.0, min(1.0, opacity))  # Clamp to valid range
                
                renderer = layer.renderer()
                if renderer:
                    renderer.setOpacity(opacity)
                    QgsMessageLog.logMessage(f"Set raster opacity to {opacity} (transparency {transparency}%)", LOG_TAG, Qgis.Info)
            
            # Set NODATA value if provided
            if nodata_value is not None:
                data_provider = layer.dataProvider()
                if not data_provider:
                    return {"status": "error", "message": "Could not access raster data provider"}
                
                # Validate band number
                if band < 1 or band > layer.bandCount():
                    return {"status": "error", "message": f"Invalid band number {band}. Layer has {layer.bandCount()} bands."}
                
                # Set user-defined NODATA value
                # Create a range for the exact value
                nodata_ranges = [QgsRasterRange(float(nodata_value), float(nodata_value))]
                success = data_provider.setUserNoDataValue(band, nodata_ranges)
                
                if success:
                    QgsMessageLog.logMessage(f"Set NODATA value {nodata_value} for band {band}", LOG_TAG, Qgis.Info)
                else:
                    QgsMessageLog.logMessage(f"Failed to set NODATA value for band {band}", LOG_TAG, Qgis.Warning)
                
                # Refresh the layer to apply changes
                layer.dataProvider().reloadData()
            
            # Trigger repaint to show changes
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            
            result_msg = "Raster transparency settings applied successfully"
            if transparency is not None and nodata_value is not None:
                result_msg = f"Set transparency to {transparency}% and NODATA value to {nodata_value}"
            elif transparency is not None:
                result_msg = f"Set transparency to {transparency}%"
            elif nodata_value is not None:
                result_msg = f"Set NODATA value to {nodata_value}"
            
            return {"status": "success", "message": result_msg}
            
        except Exception as e:
            error_msg = f"Error in action_set_raster_transparency: {str(e)}"
            QgsMessageLog.logMessage(error_msg, LOG_TAG, Qgis.Critical)
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.Critical)
            return {"status": "error", "message": error_msg}

    @staticmethod
    def action_list_color_ramps(params):
        """List all available color ramps in QGIS."""
        try:
            style = QgsStyle.defaultStyle()
            ramp_names = style.colorRampNames()
            return {
                "status": "success",
                "color_ramps": sorted(ramp_names),
                "count": len(ramp_names)
            }
        except Exception as e:
            error_msg = f"Error listing color ramps: {str(e)}"
            QgsMessageLog.logMessage(error_msg, LOG_TAG, Qgis.Critical)
            return {"status": "error", "message": error_msg}

    def action_set_raster_colormap(self, params):
        """Apply a color ramp to a raster layer."""
        try:
            layer_id = params.get("layer_id")
            layer_name = params.get("layer_name")
            color_ramp_name = params.get("color_ramp_name")
            min_value = params.get("min_value")
            max_value = params.get("max_value")
            interpolation = params.get("interpolation", "interpolated")
            band = params.get("band", 1)
            classes = params.get("classes", 5)  # Number of classes for discrete mode
            
            if not color_ramp_name:
                return {"status": "error", "message": "color_ramp_name is required"}
            
            layer, error = self._resolve_layer(layer_id, layer_name, QgsRasterLayer)
            if not layer:
                return {"status": "error", "message": error}
            
            # Double check type
            if not isinstance(layer, QgsRasterLayer):
                return {"status": "error", "message": "Layer is not a raster layer"}
            
            # Validate band number
            if band < 1 or band > layer.bandCount():
                return {"status": "error", "message": f"Invalid band number {band}. Layer has {layer.bandCount()} bands."}
            
            # Get the color ramp from QGIS style
            style = QgsStyle.defaultStyle()
            color_ramp = style.colorRamp(color_ramp_name)
            if not color_ramp:
                available_ramps = sorted(style.colorRampNames())[:10]
                return {
                    "status": "error",
                    "message": f"Color ramp '{color_ramp_name}' not found. Available ramps include: {', '.join(available_ramps)}..."
                }
            
            # Get min/max values if not provided
            data_provider = layer.dataProvider()
            if min_value is None or max_value is None:
                stats = data_provider.bandStatistics(band, QgsRasterBandStats.All)
                if min_value is None:
                    min_value = stats.minimumValue
                if max_value is None:
                    max_value = stats.maximumValue
            
            min_value = float(min_value)
            max_value = float(max_value)
            
            QgsMessageLog.logMessage(f"Applying colormap '{color_ramp_name}' with range [{min_value}, {max_value}]", LOG_TAG, Qgis.Info)
            
            # Create color ramp shader
            shader = QgsColorRampShader()
            
            # Set interpolation type
            if interpolation.lower() == "discrete":
                shader.setColorRampType(QgsColorRampShader.Discrete)
            elif interpolation.lower() == "exact":
                shader.setColorRampType(QgsColorRampShader.Exact)
            else:  # interpolated (default)
                shader.setColorRampType(QgsColorRampShader.Interpolated)
            
            # Create color ramp items
            color_ramp_items = []
            num_steps = classes if interpolation.lower() == "discrete" else 10
            
            for i in range(num_steps + 1):
                ratio = i / num_steps
                value = min_value + ratio * (max_value - min_value)
                color = color_ramp.color(ratio)
                label = f"{value:.2f}"
                color_ramp_items.append(QgsColorRampShader.ColorRampItem(value, color, label))
            
            shader.setColorRampItemList(color_ramp_items)
            
            # Create raster shader
            raster_shader = QgsRasterShader()
            raster_shader.setRasterShaderFunction(shader)
            
            # Create renderer
            renderer = QgsSingleBandPseudoColorRenderer(data_provider, band, raster_shader)
            
            # Apply renderer to layer
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            
            return {
                "status": "success",
                "message": f"Applied '{color_ramp_name}' colormap with {interpolation} interpolation",
                "min_value": min_value,
                "max_value": max_value,
                "interpolation": interpolation
            }
            
        except Exception as e:
            error_msg = f"Error setting raster colormap: {str(e)}"
            QgsMessageLog.logMessage(error_msg, LOG_TAG, Qgis.Critical)
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.Critical)
            return {"status": "error", "message": error_msg}

    @staticmethod
    def action_get_layers(params):
        layers = []
        # We need to access iface to get the active layer, but this method is static.
        # We should change it to an instance method or pass iface somehow.
        # However, RequestHandler has self.iface.
        # Let's change @staticmethod to instance method (remove @staticmethod)
        # But wait, the caller might be calling it as static?
        # The dispatcher `handle_request` calls `getattr(self, method_name)(params)`.
        # So it's fine to make it an instance method.
        pass 

    def action_get_layers(self, params):
        layers = []
        active_layer = self.iface.activeLayer()
        active_layer_id = active_layer.id() if active_layer else None
        
        for layer in QgsProject.instance().mapLayers().values():
            layer_info = {
                "id": layer.id(),
                "name": layer.name(),
                "type": layer.type().name,
                "crs": layer.crs().authid(),
                "active": (layer.id() == active_layer_id)
            }
            if isinstance(layer, QgsVectorLayer):
                # Map geometry type enum to string manually to ensure consistency and avoid TypeError
                g_type = layer.geometryType()
                if g_type == QgsWkbTypes.PointGeometry:
                    layer_info["geometry_type"] = "Point"
                elif g_type == QgsWkbTypes.LineGeometry:
                    layer_info["geometry_type"] = "Line"
                elif g_type == QgsWkbTypes.PolygonGeometry:
                    layer_info["geometry_type"] = "Polygon"
                else:
                    layer_info["geometry_type"] = "Unknown"
            
            layers.append(layer_info)
        return {"status": "success", "layers": layers}

    @staticmethod
    def action_remove_layer(params):
        layer_id = params.get("layer_id")
        QgsProject.instance().removeMapLayer(layer_id)
        return {"status": "success"}

    @staticmethod
    def action_rename_layer(params):
        layer_id = params.get("layer_id")
        new_name = params.get("new_name")
        
        if not new_name:
            return {"status": "error", "message": "new_name is required"}
        
        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer:
            return {"status": "error", "message": "Layer not found"}
        
        layer.setName(new_name)
        return {"status": "success", "layer_id": layer_id, "new_name": new_name}

    def action_zoom_to_layer(self, params):
        layer_id = params.get("layer_id")
        layer = self._get_layer_by_id(layer_id)
        if layer:
            self.iface.mapCanvas().setExtent(layer.extent())
            self.iface.mapCanvas().refresh()
            return {"status": "success"}
        return {"status": "error", "message": "Layer not found"}

    def action_get_layer_features(self, params):
        layer_id = params.get("layer_id")
        limit = params.get("limit", 10)
        filter_expression = params.get("filter_expression")
        layer = self._get_layer_by_id(layer_id)
        
        if not layer or not isinstance(layer, QgsVectorLayer):
            return {"status": "error", "message": "Invalid vector layer"}
        
        request = QgsFeatureRequest()
        if filter_expression:
            request.setFilterExpression(filter_expression)
        
        features = []
        for i, feat in enumerate(layer.getFeatures(request)):
            if i >= limit:
                break
            
            feat_dict = {
                "id": feat.id(),
                "attributes": feat.attributes()
            }
            if feat.hasGeometry():
                feat_dict["geometry"] = feat.geometry().asWkt()
                
            features.append(feat_dict)
        return {"status": "success", "features": features, "fields": [f.name() for f in layer.fields()]}

    @staticmethod
    def action_execute_processing(params):
        algorithm = params.get("algorithm")
        parameters = params.get("parameters", {})

        try:
            result = processing.run(algorithm, parameters)
            # Result might contain QgsMapLayer objects, which are not serializable.
            # We need to sanitize the result.
            sanitized_result = {}
            for k, v in result.items():
                if hasattr(v, 'id'):  # Layer object
                    sanitized_result[k] = v.id()
                else:
                    sanitized_result[k] = str(v)  # Fallback to string
            return {"status": "success", "result": sanitized_result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @staticmethod
    def action_list_processing_algorithms(params):
        """List all available processing algorithms with optional search filter."""
        search = params.get("search", "").lower()
        limit = params.get("limit", 50)
        
        try:
            from qgis.core import QgsApplication
            registry = QgsApplication.processingRegistry()
            all_algorithms = registry.algorithms()
            
            results = []
            for alg in all_algorithms:
                alg_id = alg.id()
                alg_name = alg.displayName()
                alg_group = alg.group()
                
                # Filter by search term if provided
                if search:
                    if (search not in alg_id.lower() and 
                        search not in alg_name.lower() and 
                        search not in alg_group.lower()):
                        continue
                
                results.append({
                    "id": alg_id,
                    "name": alg_name,
                    "group": alg_group
                })
                
                # Apply limit
                if len(results) >= limit:
                    break
            
            return {
                "status": "success",
                "algorithms": results,
                "count": len(results),
                "total_available": len(all_algorithms)
            }
        except Exception as e:
            return {"status": "error", "message": str(e), "traceback": traceback.format_exc()}

    @staticmethod
    def action_get_algorithm_help(params):
        """Get detailed help for a specific processing algorithm."""
        algorithm_id = params.get("algorithm_id")
        
        if not algorithm_id:
            return {"status": "error", "message": "algorithm_id is required"}
        
        try:
            from qgis.core import QgsApplication
            registry = QgsApplication.processingRegistry()
            alg = registry.algorithmById(algorithm_id)
            
            if not alg:
                return {"status": "error", "message": f"Algorithm '{algorithm_id}' not found"}
            
            # Get algorithm information
            info = {
                "id": alg.id(),
                "name": alg.displayName(),
                "group": alg.group(),
                "help": alg.shortDescription() if hasattr(alg, 'shortDescription') else "",
                "parameters": [],
                "outputs": []
            }
            
            # Get parameter definitions
            param_defs = alg.parameterDefinitions()
            for param in param_defs:
                param_info = {
                    "name": param.name(),
                    "description": param.description(),
                    "type": param.type(),
                    "optional": not param.flags() & param.FlagOptional == 0,
                    "default": str(param.defaultValue()) if param.defaultValue() is not None else None
                }
                
                # Add type-specific information
                if hasattr(param, 'dataType'):
                    param_info["data_type"] = param.dataType()
                
                info["parameters"].append(param_info)
            
            # Get output definitions
            output_defs = alg.outputDefinitions()
            for output in output_defs:
                output_info = {
                    "name": output.name(),
                    "description": output.description(),
                    "type": output.type()
                }
                info["outputs"].append(output_info)
            
            return {"status": "success", "algorithm": info}
        except Exception as e:
            return {"status": "error", "message": str(e), "traceback": traceback.format_exc()}

    @staticmethod
    def action_save_project(params):
        path = params.get("path")
        if path:
            QgsProject.instance().write(path)
        else:
            QgsProject.instance().write()
        return {"status": "success"}

    @staticmethod
    def action_save_layer(params):
        layer_id = params.get("layer_id")
        output_path = params.get("output_path")
        target_crs_authid = params.get("target_crs")  # Optional, e.g. "EPSG:4610"
        driver_name = params.get("driver_name", "ESRI Shapefile")

        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer or not layer.isValid():
            return {"status": "error", "message": "Invalid layer"}

        # Use target CRS if provided, otherwise use layer's CRS
        crs = QgsCoordinateReferenceSystem(target_crs_authid) if target_crs_authid else layer.crs()

        # writeAsVectorFormat returns (error_code, error_message)
        error = QgsVectorFileWriter.writeAsVectorFormat(
            layer,
            output_path,
            "UTF-8",
            crs,
            driver_name
        )

        if error[0] == QgsVectorFileWriter.NoError:
            return {"status": "success", "path": output_path}
        else:
            return {"status": "error", "message": f"Failed to save layer: {error[1]}"}

    def action_render_map(self, params):
        path = params.get("path")
        width = params.get("width", 800)
        height = params.get("height", 600)

        settings = self.iface.mapCanvas().mapSettings()
        settings.setOutputSize(QSize(width, height))

        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()

        image = job.renderedImage()
        image.save(path)
        return {"status": "success", "path": path}

    def action_execute_code(self, params):
        code = params.get("code")
        try:
            # Execute in a restricted scope, but with access to iface/qgis
            local_scope = {
                "iface": self.iface, 
                "QgsProject": QgsProject, 
                "QgsApplication": QgsApplication,
                "QColor": QColor,
                "QgsWkbTypes": QgsWkbTypes
            }
            exec(code, globals(), local_scope)
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def action_create_memory_layer(self, params):
        name = params.get("name", "Memory Layer")
        geometry_type = params.get("geometry_type", "Point") # Point, LineString, Polygon, etc.
        crs_authid = params.get("crs", "EPSG:4326")
        fields = params.get("fields", []) # List of {"name": "...", "type": "..."}
        
        # Construct URI
        # memory:?crs=EPSG:4326&index=yes&field=name:string(20)&field=age:integer
        uri = f"{geometry_type}?crs={crs_authid}&index=yes"
        
        layer = QgsVectorLayer(uri, name, "memory")
        if not layer.isValid():
            return {"status": "error", "message": "Failed to create memory layer"}
        
        qgs_fields = []
        for f in fields:
            f_name = f.get("name")
            f_type = f.get("type", "String")
            
            # Map simple type strings to QVariant types if needed, or let QGIS handle it
            # QgsField constructor takes name, type, typeName, len, prec
            # For simplicity, we'll assume standard type names or map common ones
            q_type = QVariant.String
            if f_type.lower() in ["int", "integer"]:
                q_type = QVariant.Int
            elif f_type.lower() in ["double", "float"]:
                q_type = QVariant.Double
                
            qgs_fields.append(QgsField(f_name, q_type))
            
        if qgs_fields:
            layer.dataProvider().addAttributes(qgs_fields)
            layer.updateFields()
            
        QgsProject.instance().addMapLayer(layer)
        return {"status": "success", "layer_id": layer.id(), "name": layer.name()}

    def action_add_features(self, params):
        layer_id = params.get("layer_id")
        features_data = params.get("features", []) # List of {"geometry": "WKT...", "attributes": {...}}
        
        layer = self._get_layer_by_id(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return {"status": "error", "message": "Invalid layer"}
        
        qgs_features = []
        fields = layer.fields()
        
        for f_data in features_data:
            feat = QgsFeature(fields)
            
            # Set Geometry
            wkt = f_data.get("geometry")
            if wkt:
                geom = QgsGeometry.fromWkt(wkt)
                feat.setGeometry(geom)
                
            # Set Attributes
            attrs = f_data.get("attributes", {})
            for k, v in attrs.items():
                idx = fields.indexFromName(k)
                if idx != -1:
                    feat.setAttribute(idx, v)
            
            qgs_features.append(feat)
            
        if qgs_features:
            layer.dataProvider().addFeatures(qgs_features)
            layer.triggerRepaint()
            
        return {"status": "success", "added_count": len(qgs_features)}

    def action_extract_layer_to_memory(self, params):
        source_layer_id = params.get("source_layer_id")
        filter_expression = params.get("filter_expression")
        new_layer_name = params.get("new_layer_name", "Extracted Layer")
        
        source_layer = self._get_layer_by_id(source_layer_id)
        if not source_layer or not isinstance(source_layer, QgsVectorLayer):
            return {"status": "error", "message": "Invalid source layer"}
            
        # 1. Create new memory layer with same properties
        crs = source_layer.crs().authid()
        wkb_type = source_layer.wkbType()
        geometry_type = QgsWkbTypes.displayString(wkb_type)
        
        # Construct URI for memory layer
        # We can use the geometry type string directly usually, or just "memory"
        # But QgsVectorLayer(uri, name, "memory") expects specific URI format for fields etc
        # Easier way: create empty memory layer and copy fields
        
        uri = f"{geometry_type}?crs={crs}&index=yes"
        new_layer = QgsVectorLayer(uri, new_layer_name, "memory")
        if not new_layer.isValid():
             return {"status": "error", "message": "Failed to create new memory layer"}
             
        # 2. Copy fields
        new_layer.dataProvider().addAttributes(source_layer.fields())
        new_layer.updateFields()
        
        # 3. Get features with filter
        request = QgsFeatureRequest()
        if filter_expression:
            QgsMessageLog.logMessage(f"Extracting with filter: {filter_expression}", LOG_TAG, Qgis.Info)
            request.setFilterExpression(filter_expression)
            
        features = []
        for feat in source_layer.getFeatures(request):
            new_feat = QgsFeature(feat)
            features.append(new_feat)
            
        # 4. Add features to new layer
        if features:
            new_layer.dataProvider().addFeatures(features)
            new_layer.triggerRepaint()
            
        QgsProject.instance().addMapLayer(new_layer)
        
        return {
            "status": "success", 
            "new_layer_id": new_layer.id(), 
            "new_layer_name": new_layer.name(),
            "feature_count": len(features)
        }


class QgisSocketServer(QtCore.QThread):
    def __init__(self, handler, host='localhost', port=9876):
        super().__init__()
        self.handler = handler
        self.host = host
        self.port = port
        self.running = True
        self.daemon = True
        self.socket = None

    def run(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            QgsMessageLog.logMessage(f"QGIS Socket Server listening on {self.host}:{self.port}", LOG_TAG, Qgis.Info)

            while self.running:
                try:
                    conn, addr = self.socket.accept()
                    with conn:
                        data = b''
                        while True:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                            try:
                                json_obj = json.loads(data.decode('utf-8'))
                                break
                            except json.JSONDecodeError:
                                continue

                        if not data:
                            continue
                        # Process request
                        response = self.process_request(json_obj)
                        # Send response
                        conn.sendall(json.dumps(response).encode('utf-8'))
                except OSError:
                    break
                except Exception as e:
                    QgsMessageLog.logMessage(f"Connection error: {e}", LOG_TAG, Qgis.Critical)
        except Exception as e:
            QgsMessageLog.logMessage(f"Server startup error: {e}", LOG_TAG, Qgis.Critical)

    def process_request(self, command):
        result_container = {}
        event = threading.Event()

        def worker():
            result_container['result'] = self.handler.handle_request(command)
            event.set()

        response = self.handler.execute_sync(command)
        return response

    def stop(self):
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
