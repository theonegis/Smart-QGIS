"""Private JSON-lines worker. All QGIS objects live on this process' main thread.

Run with the QGIS distribution's Python. Only standard library and QGIS imports
are used here, keeping the MCP/LangChain environment independent of Qt's ABI.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

# Protect the private protocol even if GDAL/native libraries write to fd 1.
protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
worker_profile = tempfile.TemporaryDirectory(prefix="smart-qgis-profile-")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = worker_profile.name
os.environ["QGIS_AUTH_DB_DIR_PATH"] = worker_profile.name
sys.path.insert(0, os.environ.get("SMART_QGIS_PLUGIN_PATH", "/usr/share/qgis/python/plugins"))

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsFeatureRequest,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemMapGrid,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsMapLayer,
    QgsPalLayerSettings,
    QgsPrintLayout,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsReadWriteContext,
    QgsRectangle,
    QgsRendererCategory,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor, QFont  # noqa: E402


def crs(value):
    result = QgsCoordinateReferenceSystem(value)
    if not result.isValid():
        raise ValueError(f"Invalid CRS: {value}")
    return result


def destination(value, suffixes=None, overwrite=False):
    if not value or not Path(value).expanduser().is_absolute():
        raise ValueError("Output path must be absolute")
    path = Path(value).expanduser().resolve()
    if suffixes and path.suffix.lower() not in suffixes:
        raise ValueError(f"Expected extension: {', '.join(suffixes)}")
    if path.exists() and not overwrite:
        raise ValueError(f"Output already exists: {path}. Use a new path or overwrite=true.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def color(value):
    result = QColor(value)
    if not result.isValid():
        raise ValueError(f"Invalid color: {value}")
    return result


def plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, QgsMapLayer):
        return {"id": value.id(), "source": value.source()}
    return str(value)


class Engine:
    def __init__(self):
        self.profile = worker_profile
        QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
        self.app = QgsApplication([], False, self.profile.name)
        self.app.initQgis()
        from processing.core.Processing import Processing

        Processing.initialize()
        self.project = QgsProject.instance()
        self.contexts = []  # Own temporary results for the lifetime of this project.
        self.project.setCrs(crs("EPSG:4326"))

    def close(self):
        self.project.clear()
        self.contexts.clear()
        self.app.exitQgis()
        self.profile.cleanup()

    def resolve(self, reference, expected=None):
        layer = self.project.mapLayer(reference or "")
        if layer is None:
            candidates = self.project.mapLayersByName(reference or "")
            if len(candidates) != 1:
                raise ValueError(f"Layer must be an exact ID or unique name: {reference}")
            layer = candidates[0]
        if expected and not isinstance(layer, expected):
            raise ValueError(f"Wrong layer type: {reference}")
        return layer

    def layer_info(self, layer):
        extent = layer.extent()
        node = self.project.layerTreeRoot().findLayer(layer.id())
        result = {
            "id": layer.id(),
            "name": layer.name(),
            "valid": layer.isValid(),
            "source": layer.source(),
            "provider": layer.providerType(),
            "crs": layer.crs().authid(),
            "extent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
            "visible": node.isVisible() if node else False,
            "attribution": layer.serverProperties().attribution(),
        }
        if isinstance(layer, QgsVectorLayer):
            result.update(
                kind="vector",
                feature_count=layer.featureCount(),
                fields=[{"name": f.name(), "type": f.typeName()} for f in layer.fields()],
            )
        elif isinstance(layer, QgsRasterLayer):
            result.update(
                kind="raster", bands=layer.bandCount(), width=layer.width(), height=layer.height()
            )
        return result

    def ordered_layers(self):
        return list(self.project.layerTreeRoot().layerOrder())

    def info(self):
        return {
            "qgis_version": Qgis.QGIS_VERSION,
            "headless": True,
            "path": self.project.fileName(),
            "title": self.project.title(),
            "crs": self.project.crs().authid(),
            "layers": [self.layer_info(layer) for layer in self.ordered_layers()],
            "layouts": [layout.name() for layout in self.project.layoutManager().layouts()],
            "providers": [p.id() for p in QgsApplication.processingRegistry().providers()],
        }

    def project_op(self, a):
        action = a["action"]
        if action == "create":
            target_crs = crs(a.get("crs", "EPSG:4326"))
            path = (
                destination(a["path"], [".qgs", ".qgz"], a.get("overwrite", False))
                if a.get("path")
                else None
            )
            self.project.clear()
            self.contexts.clear()
            self.project.setCrs(target_crs)
            self.project.setTitle(a.get("title", "Smart-QGIS"))
            metadata = self.project.metadata()
            metadata.setAuthor("")
            self.project.setMetadata(metadata)
            if path:
                self.project.setFileName(path)
        elif action == "open":
            path = Path(a.get("path") or "").expanduser()
            if not path.is_absolute() or not path.is_file():
                raise ValueError("Project path must be an existing absolute file")
            if not self.project.read(str(path)):
                raise ValueError("Failed to read project")
            self.contexts.clear()
            invalid = [
                layer_item.name()
                for layer_item in self.project.mapLayers().values()
                if not layer_item.isValid()
            ]
            if invalid:
                raise ValueError(f"Project loaded with missing/invalid layers: {invalid}")
        elif action == "save":
            memory = [
                layer.name()
                for layer in self.project.mapLayers().values()
                if layer.providerType() == "memory"
            ]
            if memory:
                raise ValueError(
                    f"Export memory layers to persistent files and reload before saving: {memory}"
                )
            path = destination(
                a.get("path") or self.project.fileName(),
                [".qgs", ".qgz"],
                a.get("overwrite", False),
            )
            if not self.project.write(path):
                raise ValueError("Failed to save project")
        return self.info()

    def load_data(self, a):
        source = a["path"]
        name = a.get("name") or Path(source.split("|")[0]).stem
        if a.get("kind", "vector") == "vector":
            layer = QgsVectorLayer(source, name, a.get("provider") or "ogr")
        else:
            layer = QgsRasterLayer(source, name, a.get("provider") or "gdal")
        if not layer.isValid():
            raise ValueError(f"Cannot load {a.get('kind', 'vector')} source: {source}")
        self.project.addMapLayer(layer)
        return self.layer_info(layer)

    def add_basemap(self, a):
        service, url = a.get("service", "osm"), a.get("url")
        attribution = a.get("attribution") or ""
        if service == "osm":
            url = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            attribution = "© OpenStreetMap contributors"
        elif service == "google" and not url:
            variant = {"roadmap": "m", "terrain": "p", "satellite": "s"}[
                a.get("google_style", "roadmap")
            ]
            url = f"https://mt0.google.com/vt/lyrs={variant}&x={{x}}&y={{y}}&z={{z}}"
            attribution = "© Google"
        elif not url:
            raise ValueError("This service requires a URL/provider URI")
        if a.get("zmax", 19) < a.get("zmin", 0):
            raise ValueError("zmax must be >= zmin")
        if service in ("osm", "google", "xyz"):
            if not url.startswith(("https://", "http://")) or not all(
                v in url for v in ("{x}", "{y}", "{z}")
            ):
                raise ValueError("XYZ URL must be HTTP(S) and contain {x}, {y}, {z}")
            uri = urlencode(
                {"type": "xyz", "url": url, "zmin": a.get("zmin", 0), "zmax": a.get("zmax", 19)}
            )
        else:
            uri = url
        layer = QgsRasterLayer(uri, a.get("name") or service.upper(), "wms")
        if not layer.isValid():
            raise ValueError(
                "Invalid remote layer; verify URL, authorization and network connectivity"
            )
        layer.serverProperties().setAttribution(attribution)
        layer.setCustomProperty("smart_qgis/basemap", True)
        before = self.ordered_layers()
        self.project.addMapLayer(layer)
        self.set_order(before + [layer])
        result = self.layer_info(layer)
        result["note"] = (
            "Layer created; tile availability is verified when rendering, not by isValid()."
        )
        return result

    def set_order(self, layers):
        root = self.project.layerTreeRoot()
        root.setHasCustomLayerOrder(True)
        root.setCustomLayerOrder(layers)

    def layers(self, a):
        action = a.get("action", "list")
        if action == "order":
            layers = [self.resolve(ref) for ref in a.get("order") or []]
            if len(layers) != len(self.project.mapLayers()) or len(
                {layer_item.id() for layer_item in layers}
            ) != len(layers):
                raise ValueError("order must contain every layer exactly once")
            self.set_order(layers)
        elif action != "list":
            layer = self.resolve(a.get("layer"))
            if action == "remove":
                self.project.removeMapLayer(layer.id())
            elif action == "rename":
                if not a.get("name"):
                    raise ValueError("name is required")
                layer.setName(a["name"])
            elif action == "visibility":
                self.project.layerTreeRoot().findLayer(layer.id()).setItemVisibilityChecked(
                    a.get("visible", True)
                )
        return {"layers": [self.layer_info(layer_item) for layer_item in self.ordered_layers()]}

    def features(self, a):
        layer = self.resolve(a["layer"], QgsVectorLayer)
        request = QgsFeatureRequest().setLimit(a.get("limit", 10))
        if a.get("expression"):
            expression = QgsExpression(a["expression"])
            if expression.hasParserError():
                raise ValueError(expression.parserErrorString())
            request.setFilterExpression(a["expression"])
        features = []
        for f in layer.getFeatures(request):
            features.append(
                {
                    "type": "Feature",
                    "id": f.id(),
                    "geometry": json.loads(f.geometry().asJson()) if f.hasGeometry() else None,
                    "properties": {
                        field.name(): plain(f[field.name()]) for field in layer.fields()
                    },
                }
            )
        return {"type": "FeatureCollection", "crs": layer.crs().authid(), "features": features}

    def style_vector(self, a):
        layer = self.resolve(a["layer"], QgsVectorLayer)
        fill, stroke = color(a.get("color", "#4c78a8")), color(a.get("outline", "#202020"))

        def symbol(fill_color):
            s = QgsSymbol.defaultSymbol(layer.geometryType())
            s.setColor(fill_color)
            sl = s.symbolLayer(0)
            geometry = int(layer.geometryType())
            if geometry == 2:  # Polygon
                sl.setStrokeColor(stroke)
                sl.setStrokeWidth(a.get("width", 0.4))
            elif geometry == 1:
                s.setWidth(a.get("width", 0.4))
            elif geometry == 0:
                s.setSize(a.get("size", 2))
                if hasattr(sl, "setStrokeColor"):
                    sl.setStrokeColor(stroke)
                    sl.setStrokeWidth(a.get("width", 0.4))
            return s

        field = a.get("category_field")
        if field:
            if layer.fields().indexFromName(field) < 0 or not a.get("categories"):
                raise ValueError("Provide an existing category_field and nonempty categories")
            categories = [
                QgsRendererCategory(
                    c["value"], symbol(color(c["color"])), str(c.get("label", c["value"]))
                )
                for c in a["categories"]
            ]
            renderer = QgsCategorizedSymbolRenderer(field, categories)
        else:
            renderer = QgsSingleSymbolRenderer(symbol(fill))
        if a.get("label_field"):
            field = a["label_field"]
            if layer.fields().indexFromName(field) < 0:
                raise ValueError("Unknown label_field")
            settings = QgsPalLayerSettings()
            settings.fieldName = field
            fmt = QgsTextFormat()
            fmt.setFont(QFont("Arial", 10))
            fmt.setSize(10)
            settings.setFormat(fmt)
            layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            layer.setLabelsEnabled(True)
        layer.setRenderer(renderer)
        layer.setOpacity(a.get("opacity", 1))
        layer.triggerRepaint()
        return {"layer": layer.id(), "renderer": renderer.type(), "opacity": layer.opacity()}

    def style_raster(self, a):
        layer = self.resolve(a["layer"], QgsRasterLayer)
        band = a.get("band", 1)
        if band > layer.bandCount():
            raise ValueError("Band exceeds raster band count")
        ramp = QgsStyle.defaultStyle().colorRamp(a.get("ramp", "Viridis"))
        if ramp is None:
            raise ValueError("Unknown color ramp; call algorithms(action='ramps')")
        minimum, maximum = a.get("minimum"), a.get("maximum")
        if minimum is None or maximum is None:
            stats = layer.dataProvider().bandStatistics(band)
            minimum = stats.minimumValue if minimum is None else minimum
            maximum = stats.maximumValue if maximum is None else maximum
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum > maximum:
            raise ValueError("Invalid raster statistics/range")
        if minimum == maximum:
            maximum = minimum + 1
        shader = QgsColorRampShader(minimum, maximum)
        shader.setColorRampType(QgsColorRampShader.Interpolated)
        classes = a.get("classes", 8)
        items = []
        for i in range(classes):
            ratio = i / (classes - 1)
            value = minimum + ratio * (maximum - minimum)
            items.append(QgsColorRampShader.ColorRampItem(value, ramp.color(ratio), f"{value:.0f}"))
        shader.setColorRampItemList(items)
        raster_shader = QgsRasterShader(minimum, maximum)
        raster_shader.setRasterShaderFunction(shader)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, raster_shader)
        renderer.setClassificationMin(minimum)
        renderer.setClassificationMax(maximum)
        layer.setRenderer(renderer)
        layer.setOpacity(a.get("opacity", 1))
        return {
            "layer": layer.id(),
            "ramp": a.get("ramp", "Viridis"),
            "minimum": minimum,
            "maximum": maximum,
            "band": band,
        }

    def algorithms(self, a):
        registry = QgsApplication.processingRegistry()
        if a.get("action") == "ramps":
            return {"ramps": sorted(QgsStyle.defaultStyle().colorRampNames())}
        if a.get("action") == "help":
            algorithm = registry.algorithmById(a.get("algorithm") or "")
            if not algorithm:
                raise ValueError("Unknown algorithm ID")
            return {
                "id": algorithm.id(),
                "name": algorithm.displayName(),
                "help": algorithm.shortHelpString(),
                "parameters": [
                    {
                        "name": p.name(),
                        "description": p.description(),
                        "type": p.type(),
                        "default": plain(p.defaultValue()),
                        "definition": plain(p.toVariantMap()),
                    }
                    for p in algorithm.parameterDefinitions()
                ],
                "outputs": [
                    {"name": p.name(), "description": p.description(), "type": p.type()}
                    for p in algorithm.outputDefinitions()
                ],
            }
        query = a.get("query", "").casefold()
        matches = sorted(
            [
                {"id": alg.id(), "name": alg.displayName(), "provider": alg.provider().id()}
                for alg in registry.algorithms()
                if query in (alg.id() + " " + alg.displayName()).casefold()
            ],
            key=lambda x: x["id"],
        )
        offset, limit = a.get("offset", 0), a.get("limit", 30)
        return {
            "total": len(matches),
            "algorithms": matches[offset : offset + limit],
            "offset": offset,
        }

    def run_processing(self, a):
        alg = QgsApplication.processingRegistry().algorithmById(a["algorithm"])
        if not alg:
            raise ValueError("Unknown algorithm; discover available IDs first")
        params = a["parameters"].copy()
        known = {p.name() for p in alg.parameterDefinitions()}
        unknown = set(params) - known
        if unknown:
            raise ValueError(f"Unknown algorithm parameters: {sorted(unknown)}")
        # Only resolve IDs in input layer parameters: never reinterpret output names/expressions.
        for definition in alg.parameterDefinitions():
            key = definition.name()
            if key not in params or definition.isDestination():
                continue
            if definition.type() in {"source", "vector", "raster", "maplayer", "multilayer"}:

                def resolve_id(v):
                    return self.project.mapLayer(v) or v if isinstance(v, str) else v

                params[key] = (
                    [resolve_id(v) for v in params[key]]
                    if isinstance(params[key], list)
                    else resolve_id(params[key])
                )
        context = QgsProcessingContext()
        context.setProject(self.project)
        feedback = QgsProcessingFeedback()
        valid, message = alg.checkParameterValues(params, context)
        if not valid:
            raise ValueError(message)
        import processing

        result = processing.run(alg, params, context=context, feedback=feedback)
        loaded = []
        if a.get("load_outputs", True):
            for _key, value in result.items():
                values = value if isinstance(value, list) else [value]
                for source in values:
                    layer = source if isinstance(source, QgsMapLayer) else None
                    if layer is None and isinstance(source, str):
                        layer = context.takeResultLayer(source)
                        if layer is None and Path(source).is_file():
                            candidate = QgsVectorLayer(source, Path(source).stem, "ogr")
                            if not candidate.isValid():
                                candidate = QgsRasterLayer(source, Path(source).stem, "gdal")
                            layer = candidate if candidate.isValid() else None
                    if layer is not None and layer.isValid():
                        self.project.addMapLayer(layer)
                        loaded.append(self.layer_info(layer))
        self.contexts.append(context)
        return {
            "algorithm": alg.id(),
            "outputs": plain(result),
            "loaded_layers": loaded,
            "log": feedback.textLog()[-12000:],
        }

    def transformed_extent(self, layer, target):
        if not layer.crs().isValid():
            raise ValueError(f"Layer has no valid CRS: {layer.name()}")
        return QgsCoordinateTransform(layer.crs(), target, self.project).transformBoundingBox(
            layer.extent()
        )

    def layout(self, a):
        manager = self.project.layoutManager()
        action = a.get("action", "create")
        name = a.get("name", "Map")
        existing = manager.layoutByName(name)
        if action == "list":
            return {"layouts": [layer_item.name() for layer_item in manager.layouts()]}
        if action in ("remove", "template"):
            if existing is None:
                raise ValueError("Unknown layout")
            if action == "remove":
                manager.removeLayout(existing)
                return {"removed": name}
            path = destination(a.get("path"), [".qpt"], a.get("overwrite", False))
            if not existing.saveAsTemplate(path, QgsReadWriteContext()):
                raise ValueError("Failed to save layout template")
            return {"path": path}
        if existing and not a.get("overwrite", False):
            raise ValueError("Layout exists; use another name or overwrite=true")
        layers = (
            [self.resolve(v) for v in a["layers"]]
            if a.get("layers") is not None
            else [
                layer_item
                for layer_item in self.ordered_layers()
                if self.project.layerTreeRoot().findLayer(layer_item.id()).isVisible()
            ]
        )
        if not layers:
            raise ValueError("A map needs at least one layer")
        target = crs(a["crs"]) if a.get("crs") else self.project.crs()
        if a.get("extent"):
            extent = QgsRectangle(*a["extent"])
            if a["extent"][0] >= a["extent"][2] or a["extent"][1] >= a["extent"][3]:
                raise ValueError("Extent bounds must be ordered")
        elif a.get("extent_layer"):
            extent = self.transformed_extent(self.resolve(a["extent_layer"]), target)
        else:
            content = [layer_item for layer_item in layers if layer_item.providerType() != "wms"]
            if not content:
                raise ValueError("Basemap-only maps require explicit extent")
            extent = QgsRectangle()
            extent.setMinimal()
            for layer in content:
                extent.combineExtentWith(self.transformed_extent(layer, target))
        if extent.isEmpty() or not extent.isFinite():
            raise ValueError("Map extent must be finite and nonempty")
        extent.scale(1.08)
        layout = QgsPrintLayout(self.project)
        layout.initializeDefaults()
        layout.setName(name)
        width, height = a.get("width_mm", 210), a.get("height_mm", 297)
        layout.pageCollection().pages()[0].setPageSize(QgsLayoutSize(width, height))
        layout.renderContext().setDpi(150)
        map_item = QgsLayoutItemMap(layout)
        map_item.setId("main-map")
        layout.addLayoutItem(map_item)
        map_item.attemptMove(QgsLayoutPoint(12, 28))
        map_item.attemptResize(QgsLayoutSize(width - 24, height - 60))
        map_item.setCrs(target)
        map_item.setLayers(layers)
        map_item.setKeepLayerSet(True)
        map_item.zoomToExtent(extent)
        map_item.setFrameEnabled(True)

        def label(text, x, y, w, h, size):
            item = QgsLayoutItemLabel(layout)
            item.setText(text)
            fmt = QgsTextFormat()
            fmt.setFont(QFont("Arial", size))
            fmt.setSize(size)
            item.setTextFormat(fmt)
            layout.addLayoutItem(item)
            item.attemptMove(QgsLayoutPoint(x, y))
            item.attemptResize(QgsLayoutSize(w, h))
            return item

        label(a.get("title") or name, 12, 8, width - 24, 15, 18)
        if a.get("grid", True):
            grid = QgsLayoutItemMapGrid("WGS84 graticule", map_item)
            map_item.grids().addGrid(grid)
            grid.setEnabled(True)
            grid.setCrs(crs("EPSG:4326"))
            geo_extent = QgsCoordinateTransform(
                target, crs("EPSG:4326"), self.project
            ).transformBoundingBox(map_item.extent())
            span = max(geo_extent.width(), geo_extent.height()) / 5
            power = 10 ** math.floor(math.log10(span))
            interval = next((n * power for n in (1, 2, 5, 10) if n * power >= span), 10 * power)
            grid.setIntervalX(interval)
            grid.setIntervalY(interval)
            grid.setStyle(QgsLayoutItemMapGrid.FrameAnnotationsOnly)
            grid.setFrameStyle(QgsLayoutItemMapGrid.ExteriorTicks)
            grid.setAnnotationEnabled(True)
            grid.setAnnotationFormat(QgsLayoutItemMapGrid.Decimal)
            grid.setAnnotationPrecision(1)
            grid_format = QgsTextFormat()
            grid_format.setFont(QFont("Arial", 8))
            grid_format.setSize(8)
            grid.setAnnotationTextFormat(grid_format)
            for side in (QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
                grid.setAnnotationDisplay(QgsLayoutItemMapGrid.LongitudeOnly, side)
            for side in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right):
                grid.setAnnotationDisplay(QgsLayoutItemMapGrid.LatitudeOnly, side)
        if a.get("legend", True):
            legend = QgsLayoutItemLegend(layout)
            legend.setTitle("Legend")
            legend.setLinkedMap(map_item)
            if hasattr(Qgis, "LegendSyncMode"):
                legend.setSyncMode(Qgis.LegendSyncMode.Manual)
            else:
                legend.setAutoUpdateModel(False)
            root = legend.model().rootGroup()
            root.clear()
            for layer in layers:
                if layer.providerType() != "wms":
                    root.addLayer(layer)
            layout.addLayoutItem(legend)
            legend.attemptMove(QgsLayoutPoint(16, 34))
            legend.setBackgroundEnabled(True)
            legend.setBackgroundColor(QColor(255, 255, 255, 220))
        if a.get("scalebar", True):
            scale = QgsLayoutItemScaleBar(layout)
            scale.setStyle("Single Box")
            scale.setLinkedMap(map_item)
            scale.applyDefaultSize()
            scale.setUnits(QgsUnitTypes.DistanceKilometers)
            scale.setUnitsPerSegment(
                max(1, round(map_item.extent().width() / 10000))
                if not target.isGeographic()
                else 50
            )
            scale.setUnitLabel("km")
            scale.setNumberOfSegments(2)
            scale.setNumberOfSegmentsLeft(0)
            layout.addLayoutItem(scale)
            scale.attemptMove(QgsLayoutPoint(12, height - 22))
        attribution = " · ".join(
            dict.fromkeys(
                layer_item.serverProperties().attribution()
                for layer_item in layers
                if layer_item.serverProperties().attribution()
            )
        )
        if attribution:
            label(attribution, 12, height - 10, width - 24, 7, 7)
        if existing:
            manager.removeLayout(existing)
        manager.addLayout(layout)
        return {
            "name": name,
            "crs": target.authid(),
            "layers": [layer_item.id() for layer_item in layers],
            "width_mm": width,
            "height_mm": height,
        }

    def export_map(self, a):
        layout = self.project.layoutManager().layoutByName(a.get("layout", "Map"))
        if layout is None:
            raise ValueError("Unknown layout")
        path = destination(
            a["path"], [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf"], a.get("overwrite", False)
        )
        exporter = QgsLayoutExporter(layout)
        if path.lower().endswith(".pdf"):
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.exportMetadata = False
            settings.dpi = a.get("dpi", 150)
            result = exporter.exportToPdf(path, settings)
        else:
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.exportMetadata = False
            settings.dpi = a.get("dpi", 150)
            result = exporter.exportToImage(path, settings)
        if (
            result != QgsLayoutExporter.Success
            or not Path(path).is_file()
            or Path(path).stat().st_size == 0
        ):
            raise ValueError(f"Map export failed with QGIS code {result}")
        errors = [
            error.message
            for item in layout.items()
            if isinstance(item, QgsLayoutItemMap)
            for error in item.renderingErrors()
        ]
        if errors:
            raise ValueError(f"Map rendered with errors; output may be incomplete: {errors}")
        return {
            "path": path,
            "bytes": Path(path).stat().st_size,
            "dpi": settings.dpi,
            "layout": layout.name(),
        }

    def dispatch(self, operation, arguments):
        import extra_ops

        extra = {
            "style_file": lambda a: extra_ops.style_file(self, a, destination),
            "vector_data": lambda a: extra_ops.vector_data(self, a, destination, crs),
            "render_raster": lambda a: extra_ops.render_raster(self, a),
            "style_graduated": lambda a: extra_ops.style_graduated(self, a),
        }
        if operation in extra:
            return extra[operation](arguments)
        allowed = {
            "project": self.project_op,
            "load_data": self.load_data,
            "add_basemap": self.add_basemap,
            "layers": self.layers,
            "features": self.features,
            "style_vector": self.style_vector,
            "style_raster": self.style_raster,
            "algorithms": self.algorithms,
            "run_processing": self.run_processing,
            "layout": self.layout,
            "export_map": self.export_map,
        }
        if operation not in allowed:
            raise ValueError("Unknown operation")
        return allowed[operation](arguments)


def main():
    engine = Engine()
    try:
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                result = engine.dispatch(request["operation"], request["arguments"])
                response = {"id": request["id"], "result": plain(result)}
            except Exception as exc:
                response = {"id": request.get("id"), "error": f"{type(exc).__name__}: {exc}"}
            protocol.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    finally:
        engine.close()
        protocol.close()


if __name__ == "__main__":
    main()
