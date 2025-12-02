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
        path = params.get("path")
        name = params.get("name", "Layer")
        provider = params.get("provider", "ogr")

        layer = self.iface.addVectorLayer(path, name, provider)
        if layer and layer.isValid():
            return {"status": "success", "layer_id": layer.id(), "name": layer.name()}
        else:
            return {"status": "error", "message": "Failed to load layer"}

    def action_add_raster_layer(self, params):
        path = params.get("path")
        name = params.get("name", "Layer")
        provider = params.get("provider", "gdal")

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
        if not layer or layer.type() != QgsVectorLayer.VectorLayer:
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
        if not layer or layer.type() != QgsVectorLayer.VectorLayer:
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
            if not layer or layer.type() != QgsVectorLayer.VectorLayer:
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
        if not layer or layer.type() != QgsVectorLayer.VectorLayer:
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
            if layer.type() == QgsMapLayer.VectorLayer:
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
        
        if not layer or layer.type() != QgsVectorLayer.VectorLayer:
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
        if not layer or layer.type() != QgsVectorLayer.VectorLayer:
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
        if not source_layer or source_layer.type() != QgsVectorLayer.VectorLayer:
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
