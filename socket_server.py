import socket
import json
import threading
import traceback
from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsProject,
    QgsVectorLayer,
    QgsMapRendererParallelJob
)
from qgis.PyQt import QtCore
from qgis.PyQt.QtCore import Qt, QObject, QSize, pyqtSlot, pyqtSignal

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

    @staticmethod
    def _get_layer_by_id(layer_id):
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
        from qgis.core import Qgis
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

    @staticmethod
    def action_get_layers(params):
        layers = []
        for layer in QgsProject.instance().mapLayers().values():
            layers.append({
                "id": layer.id(),
                "name": layer.name(),
                "type": layer.type().name,
                "crs": layer.crs().authid()
            })
        return {"status": "success", "layers": layers}

    @staticmethod
    def action_remove_layer(params):
        layer_id = params.get("layer_id")
        QgsProject.instance().removeMapLayer(layer_id)
        return {"status": "success"}

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
        
        from qgis.core import QgsFeatureRequest
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
        from qgis import processing
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
            local_scope = {"iface": self.iface, "QgsProject": QgsProject}
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
        
        # Add fields
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QVariant
        
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
            
        from qgis.core import QgsFeature, QgsGeometry
        
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
        from qgis.core import QgsWkbTypes
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
        from qgis.core import QgsFeatureRequest, QgsFeature
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
