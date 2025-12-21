import socket
import json
import threading
import traceback
from qgis.core import *
from qgis import processing
from qgis.PyQt import QtCore, QtGui, QtWidgets
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import QProgressBar

"""
1. MCP Server sends JSON: {"type": "zoom_to_layer", ...}
2. SocketServer (Background Thread) receives it.
3. SocketServer calls handler.execute_sync().
4. RequestHandler emits signal -> Main Thread wakes up.
5. Main Thread runs `action_zoom_to_layer` (Safe QGIS API call).
6. Main Thread sends result back to Background Thread.
7. SocketServer sends JSON response back to MCP.
"""

LOG_TAG = "Smart QGIS"


class RequestHandler(QObject):
    # Signal to trigger execution on the main thread
    sig_handle_request = pyqtSignal(object, object, object)

    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self._active_tasks = []
        self._task_refs = {}
        # Connect signal to slot with QueuedConnection to ensure it runs on main thread
        self.sig_handle_request.connect(self.slot_handle_request, Qt.QueuedConnection)

    def execute_sync(self, command):
        """
        Called from the worker thread. Emits a signal to the main thread and waits for the result.
        """
        event = threading.Event()
        result_container = {}
        self.sig_handle_request.emit(command, event, result_container)

        # 600 second (10 minute) timeout for the QGIS main thread to process the request
        if not event.wait(600.0):
            return {
                "status": "error",
                "message": "QGIS main thread did not respond within 10 minutes. QGIS might be busy or processing a heavy task.",
            }

        return result_container.get(
            "result",
            {"status": "error", "message": "Execution failed or returned no result"},
        )

    @pyqtSlot(object, object, object)
    def slot_handle_request(self, command, event, result_container):
        """
        Slot running on the main thread.
        Usually blocks the main thread, except for specific background-supported commands.
        """
        try:
            cmd_type = command.get("type")

            # Special case for processing: run in background task to avoid UI freeze
            if cmd_type == "execute_processing":
                # For processing, we offload to a QTask and wait for it to finish asynchronously
                # The event.set() will be called by the task completion callback
                self.run_algorithm_background(command, event, result_container)
            else:
                result = self.handle_request(command)
                result_container["result"] = result
                event.set()
        except Exception as e:
            QgsMessageLog.logMessage(f"Error in slot: {str(e)}", LOG_TAG, Qgis.Critical)
            result_container["result"] = {
                "status": "error",
                "message": f"Slot error: {str(e)}",
            }
            event.set()

    def resolve_processing_params(self, parameters):
        """Helper to resolve layer names/paths to objects (must be called on main thread)."""
        import os

        resolved = {}

        def find_layer_by_name(name):
            layers = QgsProject.instance().mapLayers().values()
            for l in layers:
                if l.name().lower() == name.lower():
                    return l
            return None

        for key, value in parameters.items():
            if isinstance(value, str):
                # 1. Try as Layer ID
                layer_by_id = QgsProject.instance().mapLayer(value)
                if layer_by_id:
                    resolved[key] = layer_by_id.id()
                    continue
                # 2. Try as File Path
                if (
                    os.path.isabs(value) or "/" in value or "\\" in value
                ) and os.path.exists(value):
                    resolved[key] = value
                    continue
                # 3. Try as Layer Name
                layer_by_name = find_layer_by_name(value)
                if layer_by_name:
                    resolved[key] = layer_by_name.id()
                    continue

            resolved[key] = value
        return resolved

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
            return {
                "status": "error",
                "message": str(e),
                "traceback": traceback.format_exc(),
            }

    # --- Actions ---

    def run_algorithm_background(self, command, event, result_container):
        """
        Executes a QGIS processing algorithm in a background task (QgsTask).
        This ensures the UI remains responsive.
        """
        params = command.get("params", {})
        algorithm = params.get("algorithm")
        alg_params = params.get("parameters", {})

        if not algorithm:
            result_container["result"] = {
                "status": "error",
                "message": "Missing 'algorithm' parameter",
            }
            event.set()
            return

        QgsMessageLog.logMessage(
            f"Starting background task (QgsTask) for: {algorithm}",
            LOG_TAG,
            Qgis.Info,
        )

        try:
            from qgis.core import (
                QgsProcessingAlgRunnerTask,
                QgsApplication,
                QgsProcessingContext,
                QgsProcessingFeedback,
                QgsProcessingAlgorithm,
            )

            # Resolve parameters to IDs/Paths (Thread-safe)
            resolved_params = self.resolve_processing_params(alg_params)

            # Create Context and Feedback on Main Thread
            context = QgsProcessingContext()
            context.setProject(QgsProject.instance())
            feedback = QgsProcessingFeedback()

            # Get Algorithm
            alg_object = QgsApplication.processingRegistry().algorithmById(algorithm)
            if not alg_object:
                result_container["result"] = {
                    "status": "error",
                    "message": f"Algorithm not found: {algorithm}",
                }
                event.set()
                return

            if alg_object.flags() & QgsProcessingAlgorithm.FlagNoThreading:
                QgsMessageLog.logMessage(
                    f"Algorithm requires main thread (no threading): {algorithm}",
                    LOG_TAG,
                    Qgis.Warning,
                )
                try:
                    results = processing.run(
                        algorithm,
                        resolved_params,
                        context=context,
                        feedback=feedback,
                    )
                    result_container["result"] = {
                        "status": "success",
                        "result": self._sanitize_processing_results(results),
                    }
                except Exception as e:
                    result_container["result"] = {
                        "status": "error",
                        "message": f"Processing failed: {str(e)}",
                        "traceback": traceback.format_exc(),
                    }
                finally:
                    event.set()
                return

            # Create Task
            # QgsProcessingAlgRunnerTask(algorithm, parameters, context, feedback)
            task = QgsProcessingAlgRunnerTask(
                alg_object, resolved_params, context, feedback
            )
            task.setDescription(f"Smart QGIS: {algorithm}")
            # Keep references alive while task runs to avoid GC-related crashes
            self._active_tasks.append(task)
            self._task_refs[task] = (context, feedback)

            # Handlers for task completion
            def on_task_finished(status, result_bool):
                try:
                    # 'result_bool' is the success status
                    if result_bool:
                        # For QgsProcessingAlgRunnerTask, we can sometimes get results from the task
                        # but mostly we rely on the context or the fact it finished.
                        # Wait, QgsProcessingAlgRunnerTask.executed signal provides results.
                        pass
                    else:
                        pass
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Task finished error: {e}", LOG_TAG, Qgis.Critical
                    )

            # The 'executed' signal signature: void executed( bool successful, const QVariantMap& results )
            def on_task_executed(successful, results):
                try:
                    if successful:
                        result_container["result"] = {
                            "status": "success",
                            "result": self._sanitize_processing_results(results),
                        }
                    else:
                        result_container["result"] = {
                            "status": "error",
                            "message": "Processing task failed (returned False). Check QGIS logs.",
                        }
                except Exception as e:
                    result_container["result"] = {
                        "status": "error",
                        "message": f"Error handling task results: {str(e)}",
                    }
                finally:
                    if task in self._task_refs:
                        del self._task_refs[task]
                    if task in self._active_tasks:
                        self._active_tasks.remove(task)
                    # Wake up socket thread
                    event.set()

            # Connect executed signal (Primary result carrier)
            task.executed.connect(on_task_executed)

            # Start Task
            QgsApplication.taskManager().addTask(task)

        except Exception as e:
            error_msg = f"Failed to start background task: {str(e)}"
            QgsMessageLog.logMessage(error_msg, LOG_TAG, Qgis.Critical)
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.Critical)
            result_container["result"] = {
                "status": "error",
                "message": error_msg,
                "traceback": traceback.format_exc(),
            }
            event.set()

    @staticmethod
    def _sanitize_processing_results(results):
        sanitized = {}
        for k, v in (results or {}).items():
            if hasattr(v, "id"):
                sanitized[k] = v.id()
            else:
                sanitized[k] = str(v)
        return sanitized

    @staticmethod
    def action_ping(params):
        return {"status": "success", "message": "pong from QGIS"}

    @staticmethod
    def action_get_qgis_info(params):
        return {
            "status": "success",
            "version": Qgis.version(),
            "release_name": Qgis.releaseName(),
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
            "layers": [
                {"id": l.id(), "name": l.name(), "type": l.type().name}
                for l in project.mapLayers().values()
            ],
        }

    def action_add_vector_layer(self, params):
        import os

        path = params.get("path")
        name = params.get("name")
        provider = params.get("provider", "ogr")

        if not path:
            return {"status": "error", "message": "Missing 'path' parameter"}

        if not os.path.exists(path):
            return {"status": "error", "message": f"File does not exist: {path}"}

        if not os.access(path, os.R_OK):
            return {
                "status": "error",
                "message": f"File is not readable (permission issue): {path}",
            }

        # If name is not provided, extract filename without extension
        if not name:
            name = os.path.splitext(os.path.basename(path))[0]

        QgsMessageLog.logMessage(
            f"Attempting to add vector layer: {path} with provider {provider}",
            LOG_TAG,
            Qgis.Info,
        )

        layer = self.iface.addVectorLayer(path, name, provider)
        if layer and layer.isValid():
            return {"status": "success", "layer_id": layer.id(), "name": layer.name()}
        else:
            error_msg = "Failed to load vector layer"
            if layer:
                error_msg += f": {layer.error().summary()}"
            return {"status": "error", "message": error_msg}

    def action_add_raster_layer(self, params):
        import os

        path = params.get("path")
        name = params.get("name")
        provider = params.get("provider", "gdal")

        if not path:
            return {"status": "error", "message": "Missing 'path' parameter"}

        if not os.path.exists(path):
            return {"status": "error", "message": f"File does not exist: {path}"}

        QgsMessageLog.logMessage(
            f"Attempting to add raster layer: {path} with provider {provider}",
            LOG_TAG,
            Qgis.Info,
        )

        if not name:
            name = os.path.splitext(os.path.basename(path))[0]

        layer = self.iface.addRasterLayer(path, name, provider)
        if layer and layer.isValid():
            QgsMessageLog.logMessage(
                f"Successfully added raster layer: {name}", LOG_TAG, Qgis.Info
            )
            return {"status": "success", "layer_id": layer.id(), "name": layer.name()}
        else:
            error_msg = "Failed to load raster layer"
            if layer:
                error_summary = layer.error().summary()
                if error_summary:
                    error_msg += f": {error_summary}"
            return {"status": "error", "message": error_msg}

    def action_add_xyz_tile_layer(self, params):
        url = params.get("url")
        name = params.get("name", "XYZ Layer")

        # Built-in URLs
        builtin_urls = {
            "Google Roadmap": "http://mt0.google.com/vt/lyrs=m&hl=en&x={x}&y={y}&z={z}",
            "Google Terrain": "http://mt0.google.com/vt/lyrs=p&hl=en&x={x}&y={y}&z={z}",
            "Google Satellite": "http://mt0.google.com/vt/lyrs=s&hl=en&x={x}&y={y}&z={z}",
            "OpenStreetMap": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        }

        if not url:
            # Try to find by name (case-insensitive lookup)
            for key, val in builtin_urls.items():
                if key.lower() == name.lower():
                    url = val
                    # Use the proper casing for the name if it was a match
                    if name.lower() == "google roadmap":
                        name = "Google Roadmap"
                    elif name.lower() == "google terrain":
                        name = "Google Terrain"
                    elif name.lower() == "google satellite":
                        name = "Google Satellite"
                    elif name.lower() == "openstreetmap":
                        name = "OpenStreetMap"
                    break

        if not url:
            return {
                "status": "error",
                "message": "URL not provided and name not found in built-in list",
            }

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
        layer_name = params.get("layer_name")
        color = params.get("color")
        size = params.get("size")
        shape = params.get("shape")

        layer, error = self._resolve_layer(layer_id, layer_name, QgsVectorLayer)
        if not layer:
            return {"status": "error", "message": error}

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
                        "triangle": QgsSimpleMarkerSymbolLayer.Triangle,
                    }
                    if shape.lower() in shape_map:
                        sym_layer.setShape(shape_map[shape.lower()])

        layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        return {"status": "success"}

    def action_set_line_layer_style(self, params):
        layer_id = params.get("layer_id")
        layer_name = params.get("layer_name")
        color = params.get("color")
        width = params.get("width")
        line_style = params.get("line_style")

        layer, error = self._resolve_layer(layer_id, layer_name, QgsVectorLayer)
        if not layer:
            return {"status": "error", "message": error}

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
                        "dashdotdot": Qt.DashDotDotLine,
                    }
                    if line_style.lower() in style_map:
                        sym_layer.setPenStyle(style_map[line_style.lower()])

        layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        return {"status": "success"}

    def action_set_polygon_layer_style(self, params):
        try:
            QgsMessageLog.logMessage(
                f"action_set_polygon_layer_style called with params: {params}",
                LOG_TAG,
                Qgis.Info,
            )

            layer_id = params.get("layer_id")
            layer_name = params.get("layer_name")
            fill_color = params.get("fill_color")
            fill_style = params.get("fill_style")
            outline_color = params.get("outline_color")
            outline_width = params.get("outline_width")

            layer, error = self._resolve_layer(layer_id, layer_name, QgsVectorLayer)
            if not layer:
                return {"status": "error", "message": error}

            if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                return {"status": "error", "message": "Layer is not a polygon layer"}

            renderer = layer.renderer()
            if not isinstance(renderer, QgsSingleSymbolRenderer):
                symbol = QgsFillSymbol.createSimple({})
                renderer = QgsSingleSymbolRenderer(symbol)
                layer.setRenderer(renderer)

            symbol = renderer.symbol()
            if not isinstance(symbol, QgsFillSymbol):
                return {
                    "status": "error",
                    "message": "Layer does not use a fill symbol",
                }

            if symbol.symbolLayerCount() > 0:
                sym_layer = symbol.symbolLayer(0)
                if isinstance(sym_layer, QgsSimpleFillSymbolLayer):
                    if fill_color:
                        # Treat special keywords as a request for no fill, even if fill_style is not set
                        color_key = str(fill_color).lower()
                        if color_key in ("transparent", "none", "no_fill", "hollow"):
                            # Fully transparent fill + no brush = visually no fill
                            sym_layer.setColor(QColor(0, 0, 0, 0))
                            sym_layer.setBrushStyle(Qt.NoBrush)
                        else:
                            sym_layer.setColor(QColor(fill_color))
                    if fill_style:
                        # Support both API-style names and natural language synonyms for hollow fill
                        style_key = fill_style.lower()
                        style_map = {
                            "solid": Qt.SolidPattern,
                            "horizontal": Qt.HorPattern,
                            "vertical": Qt.VerPattern,
                            "cross": Qt.CrossPattern,
                            "b_diagonal": Qt.BDiagPattern,
                            "f_diagonal": Qt.FDiagPattern,
                            "diagonal_cross": Qt.DiagCrossPattern,
                            "no_brush": Qt.NoBrush,
                            # natural-language aliases
                            "hollow": Qt.NoBrush,
                            "none": Qt.NoBrush,
                            "no_fill": Qt.NoBrush,
                            "transparent": Qt.NoBrush,
                        }
                        if style_key in style_map:
                            sym_layer.setBrushStyle(style_map[style_key])
                    if outline_color:
                        sym_layer.setStrokeColor(QColor(outline_color))
                    if outline_width is not None:
                        sym_layer.setStrokeWidth(float(outline_width))

            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())

            QgsMessageLog.logMessage(
                "action_set_polygon_layer_style completed successfully",
                LOG_TAG,
                Qgis.Info,
            )
            return {"status": "success"}
        except Exception as e:
            error_msg = f"Error in action_set_polygon_layer_style: {str(e)}"
            QgsMessageLog.logMessage(error_msg, LOG_TAG, Qgis.Critical)
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.Critical)
            return {"status": "error", "message": error_msg}

    def action_set_categorized_polygon_style(self, params):
        layer_id = params.get("layer_id")
        layer_name = params.get("layer_name")
        field_name = params.get("field_name")
        color_scheme = params.get("color_scheme", "random")

        layer, error = self._resolve_layer(layer_id, layer_name, QgsVectorLayer)
        if not layer:
            return {"status": "error", "message": error}

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
                    color = QColor(
                        int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)
                    )
                elif color_scheme == "gradient":
                    ratio = i / max(num_features - 1, 1)
                    color = QColor(int(ratio * 255), 0, int((1 - ratio) * 255))
                else:  # random
                    color = QColor(
                        random.randint(0, 255),
                        random.randint(0, 255),
                        random.randint(0, 255),
                    )

                # Create symbol for this feature
                symbol = QgsFillSymbol.createSimple(
                    {
                        "color": color.name(),
                        "outline_color": "black",
                        "outline_width": "0.26",
                    }
                )

                # Create category using feature ID
                category = QgsRendererCategory(fid, symbol, f"Feature {fid}")
                categories.append(category)

            # Create categorized renderer using $id field
            renderer = QgsCategorizedSymbolRenderer("$id", categories)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())

            return {
                "status": "success",
                "message": f"Applied {color_scheme} unique colors to {num_features} features",
            }

        # MODE 2: With field_name - categorize by field values
        else:
            # Check if field exists
            field_index = layer.fields().indexOf(field_name)
            if field_index == -1:
                available_fields = [f.name() for f in layer.fields()]
                return {
                    "status": "error",
                    "message": f"Field '{field_name}' not found. Available fields: {', '.join(available_fields)}",
                }

            # Get unique values from the field
            unique_values = layer.uniqueValues(field_index)
            if not unique_values:
                return {
                    "status": "error",
                    "message": f"No values found in field '{field_name}'",
                }

            num_categories = len(unique_values)

            for i, value in enumerate(sorted(unique_values, key=lambda x: str(x))):
                # Generate color based on scheme
                if color_scheme == "rainbow":
                    hue = i / num_categories
                    rgb = colorsys.hsv_to_rgb(hue, 0.8, 0.9)
                    color = QColor(
                        int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)
                    )
                elif color_scheme == "gradient":
                    ratio = i / max(num_categories - 1, 1)
                    color = QColor(int(ratio * 255), 0, int((1 - ratio) * 255))
                else:  # random
                    color = QColor(
                        random.randint(0, 255),
                        random.randint(0, 255),
                        random.randint(0, 255),
                    )

                # Create symbol for this category
                symbol = QgsFillSymbol.createSimple(
                    {
                        "color": color.name(),
                        "outline_color": "black",
                        "outline_width": "0.26",
                    }
                )

                # Create category
                category = QgsRendererCategory(value, symbol, str(value))
                categories.append(category)

            # Create and apply categorized renderer
            renderer = QgsCategorizedSymbolRenderer(field_name, categories)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())

            return {
                "status": "success",
                "message": f"Applied {color_scheme} categorized styling with {num_categories} categories based on field '{field_name}'",
            }

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
                    return (
                        None,
                        f"Layer with ID {layer_id} is not of type {layer_type_class.__name__}",
                    )
                return layer, None
            return None, f"Layer with ID {layer_id} not found"

        # Get all layers
        layers = list(QgsProject.instance().mapLayers().values())

        # Filter by type if specified
        if layer_type_class:
            layers = [l for l in layers if isinstance(l, layer_type_class)]
            if not layers:
                return (
                    None,
                    f"No layers of type {layer_type_class.__name__} found in project",
                )

        # 2. Try by Name (if provided)
        if layer_name:
            # Fuzzy match
            matches = [l for l in layers if layer_name.lower() in l.name().lower()]

            if not matches:
                # Fallback to active layer if it matches type
                active_layer = self.iface.activeLayer()
                if active_layer and (
                    not layer_type_class or isinstance(active_layer, layer_type_class)
                ):
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
                    QgsMessageLog.logMessage(
                        f"Set raster opacity to {opacity} (transparency {transparency}%)",
                        LOG_TAG,
                        Qgis.Info,
                    )

            # Set NODATA value if provided
            if nodata_value is not None:
                data_provider = layer.dataProvider()
                if not data_provider:
                    return {
                        "status": "error",
                        "message": "Could not access raster data provider",
                    }

                # Validate band number
                if band < 1 or band > layer.bandCount():
                    return {
                        "status": "error",
                        "message": f"Invalid band number {band}. Layer has {layer.bandCount()} bands.",
                    }

                # Set user-defined NODATA value on the data provider
                # Create a range for the exact value
                nodata_ranges = [
                    QgsRasterRange(float(nodata_value), float(nodata_value))
                ]
                success = data_provider.setUserNoDataValue(band, nodata_ranges)

                if success:
                    QgsMessageLog.logMessage(
                        f"Set NODATA value {nodata_value} for band {band} on data provider",
                        LOG_TAG,
                        Qgis.Info,
                    )
                else:
                    QgsMessageLog.logMessage(
                        f"Failed to set NODATA value for band {band} on data provider",
                        LOG_TAG,
                        Qgis.Warning,
                    )

                # CRITICAL: Also configure the renderer's transparency to make nodata pixels transparent
                renderer = layer.renderer()
                if renderer:
                    # Create or get existing raster transparency
                    raster_transparency = QgsRasterTransparency()

                    # Add transparent pixel for the exact nodata value
                    transparent_pixels = []
                    transparent_pixel = (
                        QgsRasterTransparency.TransparentSingleValuePixel()
                    )
                    transparent_pixel.min = float(nodata_value)
                    transparent_pixel.max = float(nodata_value)
                    transparent_pixel.percentTransparent = 100.0  # Fully transparent
                    transparent_pixels.append(transparent_pixel)

                    raster_transparency.setTransparentSingleValuePixelList(
                        transparent_pixels
                    )
                    renderer.setRasterTransparency(raster_transparency)

                    QgsMessageLog.logMessage(
                        f"Configured renderer transparency for nodata value {nodata_value}",
                        LOG_TAG,
                        Qgis.Info,
                    )

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
                "count": len(ramp_names),
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
                return {
                    "status": "error",
                    "message": f"Invalid band number {band}. Layer has {layer.bandCount()} bands.",
                }

            # Get the color ramp from QGIS style
            style = QgsStyle.defaultStyle()
            color_ramp = style.colorRamp(color_ramp_name)
            if not color_ramp:
                available_ramps = sorted(style.colorRampNames())[:10]
                return {
                    "status": "error",
                    "message": f"Color ramp '{color_ramp_name}' not found. Available ramps include: {', '.join(available_ramps)}...",
                }

            # Get min/max values if not provided
            data_provider = layer.dataProvider()
            if min_value is None or max_value is None:
                try:
                    # Default: Use cumulative cut (1% - 98%) for better contrast
                    # This ignores extreme outliers which often skew the full range
                    lower_cut = 0.01
                    upper_cut = 0.98
                    sample_size = 250000  # Reasonable sample size for estimation

                    # QgsRasterDataProvider.cumulativeCut(band, lower, upper, extent, sampleSize)
                    range_limits = data_provider.cumulativeCut(
                        band, lower_cut, upper_cut, layer.extent(), sample_size
                    )

                    if min_value is None:
                        min_value = range_limits.min()
                    if max_value is None:
                        max_value = range_limits.max()

                    QgsMessageLog.logMessage(
                        f"Calculated 1-98% cumulative cut (sampled): [{min_value}, {max_value}]",
                        LOG_TAG,
                        Qgis.Info,
                    )

                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Cumulative cut failed: {e}. Falling back to full range.",
                        LOG_TAG,
                        Qgis.Warning,
                    )
                    stats = data_provider.bandStatistics(band, QgsRasterBandStats.All)
                    if min_value is None:
                        min_value = stats.minimumValue
                    if max_value is None:
                        max_value = stats.maximumValue

            min_value = float(min_value)
            max_value = float(max_value)

            # Fix for flat rasters (min == max)
            if min_value == max_value:
                min_value -= 1
                max_value += 1

            QgsMessageLog.logMessage(
                f"Applying colormap '{color_ramp_name}' with range [{min_value}, {max_value}]",
                LOG_TAG,
                Qgis.Info,
            )

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
                ratio = max(0.0, min(1.0, i / num_steps))
                value = min_value + ratio * (max_value - min_value)
                color = color_ramp.color(ratio)
                label = f"{value:.2f}"
                color_ramp_items.append(
                    QgsColorRampShader.ColorRampItem(value, color, label)
                )

            shader.setColorRampItemList(color_ramp_items)

            # Create raster shader
            raster_shader = QgsRasterShader()
            raster_shader.setRasterShaderFunction(shader)

            # Create renderer
            renderer = QgsSingleBandPseudoColorRenderer(
                data_provider, band, raster_shader
            )

            # Explicitly set classification bounds so Legend knows the exact range
            renderer.setClassificationMin(min_value)
            renderer.setClassificationMax(max_value)

            # Apply renderer to layer
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())

            return {
                "status": "success",
                "message": f"Applied '{color_ramp_name}' colormap with {interpolation} interpolation",
                "min_value": min_value,
                "max_value": max_value,
                "interpolation": interpolation,
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
                "active": (layer.id() == active_layer_id),
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

    def action_remove_layer(self, params):
        layer_id = params.get("layer_id")
        layer_name = params.get("layer_name")
        layer, error = self._resolve_layer(layer_id, layer_name)
        if not layer:
            return {"status": "error", "message": error}

        QgsProject.instance().removeMapLayer(layer.id())
        return {"status": "success"}

    def action_rename_layer(self, params):
        layer_id = params.get("layer_id")
        layer_name = params.get("layer_name")
        new_name = params.get("new_name")

        if not new_name:
            return {"status": "error", "message": "new_name is required"}

        layer, error = self._resolve_layer(layer_id, layer_name)
        if not layer:
            return {"status": "error", "message": error}

        layer.setName(new_name)
        return {"status": "success", "layer_id": layer.id(), "new_name": new_name}

    def action_zoom_to_layer(self, params):
        layer_id = params.get("layer_id")
        layer_name = params.get("layer_name")
        layer, error = self._resolve_layer(layer_id, layer_name)
        if layer:
            self.iface.mapCanvas().setExtent(layer.extent())
            self.iface.mapCanvas().refresh()
            return {"status": "success"}
        return {"status": "error", "message": error}

    def action_get_layer_features(self, params):
        layer_id = params.get("layer_id")
        layer_name = params.get("layer_name")
        limit = params.get("limit", 10)
        filter_expression = params.get("filter_expression")
        layer, error = self._resolve_layer(layer_id, layer_name, QgsVectorLayer)

        if not layer:
            return {"status": "error", "message": error}

        request = QgsFeatureRequest()
        if filter_expression:
            request.setFilterExpression(filter_expression)

        features = []
        for i, feat in enumerate(layer.getFeatures(request)):
            if i >= limit:
                break

            feat_dict = {"id": feat.id(), "attributes": feat.attributes()}
            if feat.hasGeometry():
                feat_dict["geometry"] = feat.geometry().asWkt()

            features.append(feat_dict)
        return {
            "status": "success",
            "features": features,
            "fields": [f.name() for f in layer.fields()],
        }

    @staticmethod
    def action_execute_processing(params):
        algorithm = params.get("algorithm")
        parameters = params.get("parameters", {})

        try:
            # Resolve layer IDs / Names / Paths to layer objects where appropriate
            # QGIS processing algorithms often require QgsMapLayer objects for layer inputs.
            # We implement a smart resolution strategy:
            # 1. Check if it's already a Layer Object (internal use) -> Use it.
            # 2. Check if string is a Layer ID -> Map to Layer Object.
            # 3. Check if string is a valid File Path -> Keep as String (QGIS handles paths).
            # 4. Check if string matches a Layer Name (fuzzy) -> Map to Layer Object.
            # 5. Fallback -> Keep as String (might be a number or option string).

            resolved_params = {}
            import os

            # Helper to find layer by name
            def find_layer_by_name(name):
                # Fuzzy match case-insensitive
                layers = QgsProject.instance().mapLayers().values()
                for l in layers:
                    if l.name().lower() == name.lower():
                        return l
                return None

            for key, value in parameters.items():
                QgsMessageLog.logMessage(
                    f"Resolving parameter '{key}': {value}", LOG_TAG, Qgis.Info
                )

                # Check if value is already a QgsMapLayer object
                if isinstance(value, QgsMapLayer):
                    resolved_params[key] = value
                    QgsMessageLog.logMessage(
                        f"  -> Kept as Layer Object: {value.name()}", LOG_TAG, Qgis.Info
                    )

                elif isinstance(value, str):
                    # 1. Try as Layer ID
                    layer_by_id = QgsProject.instance().mapLayer(value)
                    if layer_by_id:
                        resolved_params[key] = layer_by_id
                        QgsMessageLog.logMessage(
                            f"  -> Resolved ID to Layer: {layer_by_id.name()}",
                            LOG_TAG,
                            Qgis.Info,
                        )
                        continue

                    # 2. Try as File Path
                    # Check if it looks like a path and exists
                    if (
                        os.path.isabs(value) or "/" in value or "\\" in value
                    ) and os.path.exists(value):
                        resolved_params[key] = value
                        QgsMessageLog.logMessage(
                            f"  -> Identified as existing file path", LOG_TAG, Qgis.Info
                        )
                        continue

                    # 3. Try as Layer Name
                    layer_by_name = find_layer_by_name(value)
                    if layer_by_name:
                        resolved_params[key] = layer_by_name
                        QgsMessageLog.logMessage(
                            f"  -> Resolved Name to Layer: {layer_by_name.name()}",
                            LOG_TAG,
                            Qgis.Info,
                        )
                        continue

                    # 4. Fallback
                    resolved_params[key] = value
                    QgsMessageLog.logMessage(f"  -> Kept as string", LOG_TAG, Qgis.Info)

                else:
                    # Other types (int, float, bool, dict, list, etc.), keep as-is
                    resolved_params[key] = value

            result = processing.run(algorithm, resolved_params)

            # Result might contain QgsMapLayer objects, which are not serializable.
            # We need to sanitize the result.
            sanitized_result = {}
            for k, v in result.items():
                if hasattr(v, "id"):  # Layer object
                    sanitized_result[k] = v.id()
                else:
                    sanitized_result[k] = str(v)  # Fallback to string

            # Log the full result for debugging
            QgsMessageLog.logMessage(
                f"Algorithm '{algorithm}' finished. Result: {json.dumps(sanitized_result)}",
                LOG_TAG,
                Qgis.Info,
            )

            return {
                "status": "success",
                "result": sanitized_result,
                "message": f"Algorithm {algorithm} executed successfully. Outputs: {', '.join(sanitized_result.keys())}",
            }
        except Exception as e:
            error_msg = f"Algorithm execution failed: {str(e)}"
            QgsMessageLog.logMessage(error_msg, LOG_TAG, Qgis.Critical)
            return {"status": "error", "message": error_msg}

    @staticmethod
    def action_list_processing_algorithms(params):
        """List all available processing algorithms with optional search filter."""
        search = params.get("search", "").lower()
        limit = params.get("limit", 100)

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
                    if (
                        search not in alg_id.lower()
                        and search not in alg_name.lower()
                        and search not in alg_group.lower()
                    ):
                        continue

                results.append({"id": alg_id, "name": alg_name, "group": alg_group})

                # Apply limit
                if len(results) >= limit:
                    break

            return {
                "status": "success",
                "algorithms": results,
                "count": len(results),
                "total_available": len(all_algorithms),
            }
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "traceback": traceback.format_exc(),
            }

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
                return {
                    "status": "error",
                    "message": f"Algorithm '{algorithm_id}' not found",
                }

            # Get algorithm information
            info = {
                "id": alg.id(),
                "name": alg.displayName(),
                "group": alg.group(),
                "help": (
                    alg.shortDescription() if hasattr(alg, "shortDescription") else ""
                ),
                "parameters": [],
                "outputs": [],
            }

            # Get parameter definitions
            param_defs = alg.parameterDefinitions()
            for param in param_defs:
                param_info = {
                    "name": param.name(),
                    "description": param.description(),
                    "type": param.type(),
                    "optional": not param.flags() & param.FlagOptional == 0,
                    "default": (
                        str(param.defaultValue())
                        if param.defaultValue() is not None
                        else None
                    ),
                }

                # Add type-specific information
                if hasattr(param, "dataType"):
                    param_info["data_type"] = param.dataType()

                info["parameters"].append(param_info)

            # Get output definitions
            output_defs = alg.outputDefinitions()
            for output in output_defs:
                output_info = {
                    "name": output.name(),
                    "description": output.description(),
                    "type": output.type(),
                }
                info["outputs"].append(output_info)

            return {"status": "success", "algorithm": info}
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "traceback": traceback.format_exc(),
            }

    @staticmethod
    def action_save_project(params):
        path = params.get("path")
        if path:
            QgsProject.instance().write(path)
        else:
            QgsProject.instance().write()
        return {"status": "success"}

    def action_save_layer(self, params):
        layer_id = params.get("layer_id")
        layer_name = params.get("layer_name")
        output_path = params.get("output_path")
        target_crs_authid = params.get("target_crs")  # Optional, e.g. "EPSG:4610"
        driver_name = params.get("driver_name", "ESRI Shapefile")

        layer, error_msg = self._resolve_layer(layer_id, layer_name, QgsVectorLayer)
        if not layer or not layer.isValid():
            return {"status": "error", "message": error_msg or "Invalid layer"}

        # Use target CRS if provided, otherwise use layer's CRS
        crs = (
            QgsCoordinateReferenceSystem(target_crs_authid)
            if target_crs_authid
            else layer.crs()
        )

        # writeAsVectorFormat returns (error_code, error_message)
        error = QgsVectorFileWriter.writeAsVectorFormat(
            layer, output_path, "UTF-8", crs, driver_name
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
            # IMPORTANT: After modifying layer styles, you MUST call:
            # iface.layerTreeView().refreshLayerSymbology(layer_id)
            local_scope = {
                "iface": self.iface,
                "QgsProject": QgsProject,
                "QgsApplication": QgsApplication,
                "QColor": QColor,
                "QgsWkbTypes": QgsWkbTypes,
                "QgsSimpleFillSymbolLayer": QgsSimpleFillSymbolLayer,
                "QgsSimpleLineSymbolLayer": QgsSimpleLineSymbolLayer,
                "QgsSimpleMarkerSymbolLayer": QgsSimpleMarkerSymbolLayer,
                "QgsFillSymbol": QgsFillSymbol,
                "QgsLineSymbol": QgsLineSymbol,
                "QgsMarkerSymbol": QgsMarkerSymbol,
            }
            exec(code, globals(), local_scope)
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def action_create_memory_layer(self, params):
        name = params.get("name", "Memory Layer")
        geometry_type = params.get(
            "geometry_type", "Point"
        )  # Point, LineString, Polygon, etc.
        crs_authid = params.get("crs", "EPSG:4326")
        fields = params.get("fields", [])  # List of {"name": "...", "type": "..."}

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
        features_data = params.get(
            "features", []
        )  # List of {"geometry": "WKT...", "attributes": {...}}

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
            QgsMessageLog.logMessage(
                f"Extracting with filter: {filter_expression}", LOG_TAG, Qgis.Info
            )
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
            "feature_count": len(features),
        }

    def _add_grid(
        self, layout, map_item, interval_x=1.0, interval_y=1.0, crs_authid=None
    ):
        from qgis.core import QgsLayoutItemMapGrid, QgsCoordinateReferenceSystem
        from qgis.PyQt.QtGui import QColor

        grids = map_item.grids()
        if grids.size() > 0:
            grid = grids.grid(0)
        else:
            grid = QgsLayoutItemMapGrid("Grid 1", map_item)
            grids.addGrid(grid)

        grid.setEnabled(True)
        grid.setIntervalX(float(interval_x))
        grid.setIntervalY(float(interval_y))

        if crs_authid:
            grid.setCrs(QgsCoordinateReferenceSystem(crs_authid))

        # Set grid to interior and exterior line style with grey color
        grid.setFrameStyle(QgsLayoutItemMapGrid.InteriorExteriorTicks)
        grid.setFramePenColor(QColor("grey"))

        # Explicitly set grid line color (not just frame)
        from qgis.core import QgsSimpleLineSymbolLayer, QgsLineSymbol

        line_symbol = QgsLineSymbol.createSimple({"color": "grey", "width": "0.1"})
        grid.setLineSymbol(line_symbol)

        # Configure annotations with dynamic precision based on interval
        grid.setAnnotationEnabled(True)

        # Calculate precision: if interval is 0.001, we need 3 decimals.
        # If interval >= 1, we need 0 decimals.
        import math

        try:
            min_interval = min(float(interval_x), float(interval_y))
            if (
                min_interval <= 0
                or math.isnan(min_interval)
                or math.isinf(min_interval)
            ):
                precision = 1
            elif min_interval >= 1:
                precision = 0
            else:
                # e.g. 0.1 -> log10(-1) -> 1
                # e.g. 0.05 -> log10(-1.3) -> 1? No, we need 2 for 0.05 usually?
                # Actually, for 0.05, 1 decimal is 0.1. So we need 2.
                # Let's use ceil(abs(log10))
                precision = int(math.ceil(abs(math.log10(min_interval))))
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error calculating grid precision: {e}", LOG_TAG, Qgis.Warning
            )
            precision = 1

        grid.setAnnotationPrecision(precision)

        # Determine CRS to check if geographic
        grid_crs = grid.crs()
        if not grid_crs.isValid():
            grid_crs = map_item.crs()

        if grid_crs.isGeographic():
            grid.setAnnotationFormat(QgsLayoutItemMapGrid.DecimalWithSuffix)
        else:
            grid.setAnnotationFormat(QgsLayoutItemMapGrid.Decimal)

        # Set annotation direction to Vertical for Left/Right to save space
        grid.setAnnotationDirection(
            QgsLayoutItemMapGrid.Vertical, QgsLayoutItemMapGrid.Left
        )
        grid.setAnnotationDirection(
            QgsLayoutItemMapGrid.Vertical, QgsLayoutItemMapGrid.Right
        )

        return grid

    def _add_title(self, layout, title_text, font_size=24):
        from qgis.core import (
            QgsLayoutItemLabel,
            QgsLayoutPoint,
            QgsUnitTypes,
            QgsLayoutItem,
        )
        from qgis.PyQt.QtGui import QFont

        # Check if a title label already exists at the top center
        existing_title = None
        for item in layout.items():
            if isinstance(item, QgsLayoutItemLabel):
                # Check if this label is positioned at top-center (likely a title)
                # We consider it a title if it's within 10mm of the top
                pos = item.pagePos()
                if pos.y() < 10:  # Within 10mm from top
                    existing_title = item
                    break

        if existing_title:
            # Update existing title
            title = existing_title
            title.setText(title_text)
            title.setFont(QFont("SimHei", int(font_size), QFont.Bold))
            title.adjustSizeToText()
            QgsMessageLog.logMessage(
                f"Updated existing title to: {title_text}", LOG_TAG, Qgis.Info
            )
        else:
            # Create new title
            title = QgsLayoutItemLabel(layout)
            title.setText(title_text)
            title.setFont(QFont("SimHei", int(font_size), QFont.Bold))
            title.setHAlign(Qt.AlignHCenter)
            title.setVAlign(Qt.AlignVCenter)
            title.adjustSizeToText()

            layout.addLayoutItem(title)

            # Position at top-center
            title.setReferencePoint(QgsLayoutItem.UpperMiddle)
            page_width = layout.pageCollection().page(0).pageSize().width()
            x = page_width / 2
            y = 5  # 5mm from top
            title.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
            QgsMessageLog.logMessage(
                f"Created new title: {title_text}", LOG_TAG, Qgis.Info
            )

        return title

    def _add_scalebar(
        self, layout, map_item, style="Single Box", position="BottomLeft"
    ):
        from qgis.core import (
            QgsLayoutItemScaleBar,
            QgsLayoutPoint,
            QgsUnitTypes,
            QgsLayoutItem,
            QgsDistanceArea,
            QgsProject,
            QgsPointXY,
        )
        from qgis.PyQt.QtGui import QFont

        # Check if scalebar already exists to avoid duplicates
        existing_scalebars = [
            item for item in layout.items() if isinstance(item, QgsLayoutItemScaleBar)
        ]
        if existing_scalebars:
            scalebar = existing_scalebars[0]
        else:
            scalebar = QgsLayoutItemScaleBar(layout)
            layout.addLayoutItem(scalebar)

        scalebar.setLinkedMap(map_item)
        scalebar.applyDefaultSize()
        scalebar.setStyle(style)

        # Explicitly set height and font size for better visibility
        scalebar.setHeight(6)  # 6mm height
        scalebar.setFont(QFont("Arial", 12))
        scalebar.setLabelBarSpace(3)  # Space between bar and text

        # Force Metric Units (Kilometers)
        scalebar.setUnits(QgsUnitTypes.DistanceKilometers)

        # Calculate map width in meters using QgsDistanceArea
        extent = map_item.extent()
        crs = map_item.crs()
        da = QgsDistanceArea()
        da.setSourceCrs(crs, QgsProject.instance().transformContext())

        # Ensure a valid ellipsoid for WGS84 distance calculation
        ellipsoid = QgsProject.instance().ellipsoid()
        if not ellipsoid:
            ellipsoid = "WGS84"
        da.setEllipsoid(ellipsoid)

        # Measure width at the center latitude
        p1 = QgsPointXY(extent.xMinimum(), extent.center().y())
        p2 = QgsPointXY(extent.xMaximum(), extent.center().y())
        width_meters = da.measureLine(p1, p2)

        # Segment size ~ 1/4 width (larger segments)
        segment_meters = width_meters / 4.0
        segment_km = segment_meters / 1000.0

        # Round to nice interval
        nice_segment_km = self._calculate_nice_interval(segment_km)
        scalebar.setUnitsPerSegment(nice_segment_km)

        scalebar.setNumberOfSegments(2)  # 2 segments on the right
        scalebar.setNumberOfSegmentsLeft(0)
        scalebar.update()

        # Position
        page_size = layout.pageCollection().page(0).pageSize()
        # Add padding to move it inside the map frame
        padding = 10
        margin_bottom = 30  # Match layout margin
        margin_left = 30  # Match layout margin

        if position == "BottomRight":
            scalebar.setReferencePoint(QgsLayoutItem.LowerRight)
            x = page_size.width() - margin_left - padding
            y = page_size.height() - margin_bottom - padding
        else:  # Default BottomLeft
            scalebar.setReferencePoint(QgsLayoutItem.LowerLeft)
            x = margin_left + padding
            y = page_size.height() - margin_bottom - padding

        scalebar.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
        return scalebar

    def _add_legend(self, layout, map_item):
        from qgis.core import (
            QgsLayoutItemLegend,
            QgsLayoutPoint,
            QgsUnitTypes,
            QgsLayoutItem,
        )

        # Check if legend already exists to avoid duplicates
        existing_legends = [
            item for item in layout.items() if isinstance(item, QgsLayoutItemLegend)
        ]
        if existing_legends:
            legend = existing_legends[0]
        else:
            legend = QgsLayoutItemLegend(layout)
            layout.addLayoutItem(legend)

        if map_item:
            legend.setLinkedMap(map_item)
            legend.setLegendFilterByMapEnabled(True)

        # Position at bottom-right with margin from frame
        page_size = layout.pageCollection().page(0).pageSize()
        legend.setReferencePoint(QgsLayoutItem.LowerRight)
        legend_margin = 20  # mm - increased from 15mm for better separation from frame
        x = page_size.width() - legend_margin
        y = page_size.height() - legend_margin
        legend.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
        return legend

    def action_create_print_layout(self, params):
        # QGIS Print Layout: use QgsPrintLayout/QgsLayout API
        from datetime import datetime
        from qgis.core import (
            QgsPrintLayout,
            QgsLayoutItemMap,
            QgsProject,
            QgsLayoutSize,
            QgsUnitTypes,
            QgsRectangle,
        )

        layout_mgr = QgsProject.instance().layoutManager()
        layout_name = params.get("layout_name")
        map_title = params.get("map_title") or params.get("title")

        if map_title:
            # If map title is given, use it as layout name too (override)
            layout_name = map_title

        if not layout_name:
            # Use current datetime as name if not provided
            layout_name = datetime.now().strftime("Layout-%Y%m%d-%H%M%S")

        # Remove any existing with same name (QGIS error if duplicate)
        existing = layout_mgr.layoutByName(layout_name)
        if existing:
            layout_mgr.removeLayout(existing)

        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.setName(layout_name)
        layout_mgr.addLayout(layout)

        # 1. Calculate Data Extent from Content Layers (ignore WMS/Basemaps)
        canvas = self.iface.mapCanvas()
        visible_layers = canvas.layers()
        # Force WGS84 as requested to ensure consistent scale bar/grid behavior
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        # Filter out WMS/XYZ layers (usually basemaps with global extent)
        content_layers = [
            l for l in visible_layers if l.isValid() and l.providerType() != "wms"
        ]

        full_extent = QgsRectangle()
        full_extent.setMinimal()
        has_content_extent = False

        if content_layers:
            from qgis.core import QgsCoordinateTransform

            for layer in content_layers:
                if not layer.extent().isEmpty():
                    # Transform extent to Target CRS (WGS84)
                    layer_extent = layer.extent()
                    if layer.crs() != target_crs:
                        try:
                            transform = QgsCoordinateTransform(
                                layer.crs(), target_crs, QgsProject.instance()
                            )
                            layer_extent = transform.transformBoundingBox(
                                layer.extent()
                            )
                            QgsMessageLog.logMessage(
                                f"Transformed layer {layer.name()} extent to WGS84",
                                LOG_TAG,
                                Qgis.Info,
                            )
                        except Exception as e:
                            QgsMessageLog.logMessage(
                                f"Failed to transform extent for layer {layer.name()}: {e}",
                                LOG_TAG,
                                Qgis.Warning,
                            )

                    full_extent.combineExtentWith(layer_extent)
                    has_content_extent = True

        if not has_content_extent:
            full_extent = canvas.extent()

        # 2. Determine Page Size based on Aspect Ratio
        data_width = full_extent.width()
        data_height = full_extent.height()

        if data_height == 0:
            data_height = 1
        if data_width == 0:
            data_width = 1
        data_ratio = data_width / data_height

        # Define margins
        # 2. Determine Page Size based on Aspect Ratio
        # Use larger margins to accommodate long coordinate labels
        margin_top = 30
        margin_bottom = 30
        margin_left = 30
        margin_right = 30

        # Calculate required map area based on data aspect ratio
        # Start with A4 landscape as base (297mm wide) and calculate height
        target_width = 297
        available_width = target_width - margin_left - margin_right
        available_height = available_width / data_ratio
        target_height = available_height + margin_top + margin_bottom

        # Constraints
        MAX_HEIGHT = 420  # A3 Portrait / A2 Landscape approx
        MIN_HEIGHT = 148  # A5 Landscape
        MAX_WIDTH = 420  # A3 Landscape
        MIN_WIDTH = 148  # A5 Portrait

        # Check Height Constraints
        if target_height > MAX_HEIGHT:
            # Too tall: Fix height to max, recalculate width
            target_height = MAX_HEIGHT
            available_height = target_height - margin_top - margin_bottom
            available_width = available_height * data_ratio
            target_width = available_width + margin_left + margin_right
        elif target_height < MIN_HEIGHT:
            # Too short: Fix height to min, recalculate width
            target_height = MIN_HEIGHT
            available_height = target_height - margin_top - margin_bottom
            available_width = available_height * data_ratio
            target_width = available_width + margin_left + margin_right

        # Pass 2: Check Width Constraints (Sanity check)
        if target_width > MAX_WIDTH:
            # Too wide: Fix width to max, recalculate height (accepting aspect ratio mismatch if needed, but try to fit)
            target_width = MAX_WIDTH
            available_width = target_width - margin_left - margin_right
            available_height = available_width / data_ratio
            target_height = available_height + margin_top + margin_bottom
        elif target_width < MIN_WIDTH:
            # Too narrow: Fix width to min
            target_width = MIN_WIDTH
            available_width = target_width - margin_left - margin_right
            # Recalculate height to maintain aspect ratio
            available_height = available_width / data_ratio
            target_height = available_height + margin_top + margin_bottom

        # Final dimensions
        page_width = target_width
        page_height = target_height

        pc = layout.pageCollection()
        page = pc.page(0)
        page.setPageSize(
            QgsLayoutSize(page_width, page_height, QgsUnitTypes.LayoutMillimeters)
        )

        # 3. Create Map Item with Margins
        map_item = QgsLayoutItemMap(layout)

        x = margin_left
        y = margin_top
        w = available_width
        h = available_height

        map_item.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
        map_item.attemptResize(QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters))
        map_item.setFrameEnabled(True)
        map_item.setFrameStrokeWidth(
            QgsLayoutMeasurement(0.6, QgsUnitTypes.LayoutMillimeters)
        )
        layout.addLayoutItem(map_item)

        map_item.setCrs(target_crs)  # Explicitly set map CRS to WGS84
        buffered_extent = QgsRectangle(full_extent)
        buffered_extent.scale(1.05)
        map_item.zoomToExtent(buffered_extent)
        map_item.setLayers(visible_layers)

        # 5. Add Decorations (Automated)
        # Grid (Smart Interval Calculation)
        # Calculate X and Y intervals independently to handle long/thin maps
        map_width_units = buffered_extent.width()
        map_height_units = buffered_extent.height()

        interval_x = self._calculate_nice_interval(map_width_units / 5.0)
        interval_y = self._calculate_nice_interval(map_height_units / 5.0)

        self._add_grid(layout, map_item, interval_x=interval_x, interval_y=interval_y)

        # Title
        title_text = map_title if map_title else "Title"
        self._add_title(layout, title_text)

        # Scale Bar (Smart Placement)
        # Heuristic: Check if data centroid is left or right of center
        # If data is left-heavy, put scale bar on right.
        # Since we zoom to extent, data is centered.
        # So we default to Bottom Left, but we can randomize or alternate if needed.
        # User asked: "depending on where there is much more space"
        # Since we fit the page to data, space is equal.
        # We'll default to Bottom Left.
        self._add_scalebar(layout, map_item, position="BottomLeft")

        # Legend (Smart Placement)
        self._add_legend(layout, map_item)

        # 6. Open Layout Designer
        self.iface.openLayoutDesigner(layout)

        return {
            "status": "success",
            "layout_name": layout_name,
            "page_size": f"{page_width:.1f}mm x {page_height:.1f}mm",
            "aspect_ratio": f"{data_ratio:.2f}",
        }

    def _get_layout_by_name(self, layout_name):
        layout_mgr = QgsProject.instance().layoutManager()
        if layout_name:
            layout = layout_mgr.layoutByName(layout_name)
        else:
            layouts = layout_mgr.printLayouts()
            layout = layouts[-1] if layouts else None
        return layout

    def _calculate_nice_interval(self, val):
        """Calculate a 'nice' grid interval (1, 2, 5, 10, etc.)"""
        import math

        # Robust check for NaN, Inf, or Zero
        if val is None or val <= 0 or math.isnan(val) or math.isinf(val):
            return 1.0

        exponent = math.floor(math.log10(val))
        fraction = val / (10**exponent)

        if fraction < 1.5:
            nice_fraction = 1
        elif fraction < 3:
            nice_fraction = 2
        elif fraction < 7:
            nice_fraction = 5
        else:
            nice_fraction = 10

        return nice_fraction * (10**exponent)

    def _get_map_item(self, layout):
        from qgis.core import QgsLayoutItemMap

        for item in layout.items():
            if isinstance(item, QgsLayoutItemMap):
                return item
        return None

    def action_add_layout_grid(self, params):
        layout_name = params.get("layout_name")
        interval_x = params.get("interval_x", 1.0)
        interval_y = params.get("interval_y", 1.0)
        crs_authid = params.get("crs")

        layout = self._get_layout_by_name(layout_name)
        if not layout:
            return {"status": "error", "message": "Layout not found"}
        map_item = self._get_map_item(layout)
        if not map_item:
            return {"status": "error", "message": "Map item not found"}

        self._add_grid(layout, map_item, interval_x, interval_y, crs_authid)
        layout.refresh()
        return {"status": "success", "message": "Grid added to layout"}

    def action_add_layout_legend(self, params):
        layout_name = params.get("layout_name")
        layout = self._get_layout_by_name(layout_name)
        if not layout:
            return {"status": "error", "message": "Layout not found"}
        map_item = self._get_map_item(layout)

        self._add_legend(layout, map_item)
        return {"status": "success", "message": "Legend added to layout"}

    def action_add_layout_scalebar(self, params):
        layout_name = params.get("layout_name")
        style = params.get("style", "Single Box")

        layout = self._get_layout_by_name(layout_name)
        if not layout:
            return {"status": "error", "message": "Layout not found"}
        map_item = self._get_map_item(layout)
        if not map_item:
            return {"status": "error", "message": "Map item not found"}

        self._add_scalebar(layout, map_item, style=style)
        return {"status": "success", "message": "Scalebar added to layout"}

    def action_add_layout_title(self, params):
        layout_name = params.get("layout_name")
        title_text = params.get("title", "Map Title")
        font_size = params.get("font_size", 24)

        layout = self._get_layout_by_name(layout_name)
        if not layout:
            return {"status": "error", "message": "Layout not found"}

        self._add_title(layout, title_text, font_size)
        return {"status": "success", "message": f"Title set to: {title_text}"}

    def action_export_layout(self, params):
        """Export a print layout to PDF or image format"""
        from qgis.core import QgsLayoutExporter
        import os

        layout_name = params.get("layout_name")
        output_path = params.get("output_path")
        format_type = params.get(
            "format", "png"
        ).lower()  # png (default), pdf, jpg, svg
        dpi = params.get("dpi", 300)

        # Get layout first (needed for default filename)
        layout = self._get_layout_by_name(layout_name)
        if not layout:
            return {"status": "error", "message": "Layout not found"}

        # If no output path provided, use Desktop with layout name
        if not output_path:
            desktop_path = os.path.expanduser("~/Desktop")
            layout_name_clean = layout.name().replace(" ", "_")
            output_path = os.path.join(
                desktop_path, f"{layout_name_clean}.{format_type}"
            )
        else:
            # Expand user path if needed
            output_path = os.path.expanduser(output_path)

        try:
            # Create exporter
            exporter = QgsLayoutExporter(layout)

            # Export based on format
            if format_type == "pdf":
                # Ensure .pdf extension
                if not output_path.endswith(".pdf"):
                    output_path += ".pdf"

                # Create export settings
                pdf_settings = QgsLayoutExporter.PdfExportSettings()
                pdf_settings.dpi = dpi

                result = exporter.exportToPdf(output_path, pdf_settings)

            elif format_type in ["png", "jpg", "jpeg"]:
                # Ensure correct extension
                if format_type == "jpeg":
                    format_type = "jpg"
                if not output_path.endswith(f".{format_type}"):
                    output_path += f".{format_type}"

                # Create export settings
                image_settings = QgsLayoutExporter.ImageExportSettings()
                image_settings.dpi = dpi

                result = exporter.exportToImage(output_path, image_settings)

            elif format_type == "svg":
                # Ensure .svg extension
                if not output_path.endswith(".svg"):
                    output_path += ".svg"

                # Create export settings
                svg_settings = QgsLayoutExporter.SvgExportSettings()
                svg_settings.dpi = dpi

                result = exporter.exportToSvg(output_path, svg_settings)

            else:
                return {
                    "status": "error",
                    "message": f"Unsupported format: {format_type}. Supported formats: pdf, png, jpg, svg",
                }

            # Check result
            if result == QgsLayoutExporter.Success:
                return {
                    "status": "success",
                    "message": f"Layout exported successfully to {output_path}",
                    "output_path": output_path,
                    "format": format_type,
                    "dpi": dpi,
                }
            else:
                error_messages = {
                    QgsLayoutExporter.FileError: "File error - check permissions and path",
                    QgsLayoutExporter.PrintError: "Print error",
                    QgsLayoutExporter.MemoryError: "Memory error - try reducing DPI",
                    QgsLayoutExporter.IteratorError: "Iterator error",
                    QgsLayoutExporter.Canceled: "Export was canceled",
                }
                error_msg = error_messages.get(result, f"Unknown error code: {result}")
                return {"status": "error", "message": f"Export failed: {error_msg}"}

        except Exception as e:
            return {
                "status": "error",
                "message": f"Export error: {str(e)}",
                "traceback": traceback.format_exc(),
            }


class QgisSocketServer(QtCore.QThread):
    def __init__(self, handler, host="localhost", port=9876):
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
            self.socket.settimeout(
                1.0
            )  # 1 second timeout to prevent indefinite blocking
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            QgsMessageLog.logMessage(
                f"QGIS Socket Server listening on {self.host}:{self.port}",
                LOG_TAG,
                Qgis.Info,
            )

            while self.running:
                try:
                    conn, addr = self.socket.accept()
                    # Start a new thread for each client to handle multiple concurrent MCP clients
                    thread = threading.Thread(
                        target=self.handle_client, args=(conn, addr), daemon=True
                    )
                    thread.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        QgsMessageLog.logMessage(
                            f"Accept error: {e}", LOG_TAG, Qgis.Critical
                        )

        except Exception as e:
            QgsMessageLog.logMessage(
                f"Server startup error: {e}", LOG_TAG, Qgis.Critical
            )

    def handle_client(self, conn, addr):
        """Handle individual client connection in a separate thread."""
        QgsMessageLog.logMessage(f"New connection from {addr}", LOG_TAG, Qgis.Info)
        with conn:
            # Set a timeout on the connection to avoid hanging threads
            conn.settimeout(120.0)
            while self.running:
                try:
                    data = b""
                    message_complete = False
                    while True:
                        try:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                            # Try to see if we have a complete JSON message
                            try:
                                decoded = data.decode("utf-8")
                                json_obj = json.loads(decoded)
                                message_complete = True
                                break
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                        except socket.timeout:
                            # Inner timeout for receiving chunks
                            if not data:  # No data at all, just keep waiting
                                continue
                            else:  # We had some data but it timed out
                                break

                    if not message_complete:
                        break

                    # Process the request and send response
                    response = self.process_request(json_obj)
                    conn.sendall(json.dumps(response).encode("utf-8"))

                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Client error from {addr}: {e}", LOG_TAG, Qgis.Warning
                    )
                    break

        QgsMessageLog.logMessage(f"Connection from {addr} closed", LOG_TAG, Qgis.Info)

    def process_request(self, command):
        cmd_type = command.get("type", "unknown")
        QgsMessageLog.logMessage(
            f"Processing socket request: {cmd_type}", LOG_TAG, Qgis.Info
        )
        import time

        start_time = time.time()

        response = self.handler.execute_sync(command)

        duration = time.time() - start_time
        QgsMessageLog.logMessage(
            f"Socket request {cmd_type} finished in {duration:.3f}s", LOG_TAG, Qgis.Info
        )
        return response

    def stop(self):
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
