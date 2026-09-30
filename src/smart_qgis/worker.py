"""Private JSON-lines worker. All QGIS objects live on this process' main thread.

Run with the QGIS distribution's Python. Only standard library and QGIS imports
are used here, keeping the MCP/LangChain environment independent of Qt's ABI.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from collections import OrderedDict
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode

# Protect the private protocol even if GDAL/native libraries write to fd 1.
protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
worker_profile = tempfile.TemporaryDirectory(prefix="smart-qgis-profile-")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = worker_profile.name
os.environ["QGIS_AUTH_DB_DIR_PATH"] = worker_profile.name
plugin_path = os.environ.get("SMART_QGIS_PLUGIN_PATH")
if plugin_path:
    sys.path.insert(0, plugin_path)

from qgis.analysis import QgsRasterCalcNode  # noqa: E402
from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsFeatureRequest,
    QgsFillSymbol,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemMapGrid,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsLineSymbol,
    QgsMapLayer,
    QgsMapLayerLegendUtils,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsMarkerSymbol,
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
    QgsRuleBasedRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QSize, Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QFont  # noqa: E402

if __package__:
    from .raster_display import normalize_alpha, persist_views
else:
    from raster_display import normalize_alpha, persist_views


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


class InvalidParameters(ValueError):
    """QGIS rejected parameters before starting the Processing algorithm."""


def parameter_help(parameter):
    definition = plain(parameter.toVariantMap())
    optional = bool(parameter.flags() & Qgis.ProcessingParameterFlag.Optional)
    default = plain(parameter.defaultValue())
    result = {
        "name": parameter.name(), "description": parameter.description(),
        "type": parameter.type(), "destination": parameter.isDestination(),
        "required": not optional, "has_default": default is not None,
        "default": default, "definition": definition,
    }
    if parameter.type() == "enum":
        static_strings = bool(definition.get("uses_static_strings"))
        result["choices"] = [
            {"value": label if static_strings else index, "label": label}
            for index, label in enumerate(definition.get("options", []))
        ]
        result["multiple"] = bool(definition.get("allow_multiple"))
        if static_strings:
            result["value_format"] = (
                "Array of string choice values" if definition.get("allow_multiple")
                else "String choice value"
            )
        else:
            result["value_format"] = (
                "Array of integer choice values, not labels"
                if definition.get("allow_multiple")
                else "Integer choice value, not its label"
            )
    elif parameter.type() == "extent":
        result["value_format"] = (
            "QGIS extent string: xmin,xmax,ymin,ymax [EPSG:code]; "
            "use comma separators and x bounds before y bounds"
        )
    return result


class Engine:
    def __init__(self):
        self.profile = worker_profile
        if prefix := os.environ.get("QGIS_PREFIX_PATH"):
            QgsApplication.setPrefixPath(prefix, True)
        self.app = QgsApplication([], False, self.profile.name)
        self.app.initQgis()
        from processing.core.Processing import Processing

        Processing.initialize()
        # QgsApplication restores its bundled PYTHONHOME during initialization.
        # External Processing providers such as the standalone macOS GRASS app
        # launch a different Python and must not inherit QGIS's Python runtime.
        os.environ.pop("PYTHONHOME", None)
        os.environ.pop("PYTHONPATH", None)
        self.project = QgsProject.instance()
        self.contexts = []  # Own temporary results for the lifetime of this project.
        # Processing providers are initialized once per worker.  Build the
        # searchable catalog lazily, then reuse it instead of walking the full
        # registry for every model query.
        self._algorithm_catalog = None
        self._algorithm_search_cache = OrderedDict()
        self._algorithm_help_cache = {}
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
                geometry_type=QgsWkbTypes.displayString(layer.wkbType()),
                fields=[{"name": f.name(), "type": f.typeName()} for f in layer.fields()],
            )
        elif isinstance(layer, QgsRasterLayer):
            result.update(
                kind="raster", bands=layer.bandCount(), width=layer.width(), height=layer.height()
            )
        return result

    @staticmethod
    def raster_summary(layer):
        """Return exact compact output-health facts without adding acceptance rules."""
        from osgeo import gdal

        previous_pam = gdal.GetThreadLocalConfigOption("GDAL_PAM_ENABLED")
        gdal.SetThreadLocalConfigOption("GDAL_PAM_ENABLED", "NO")
        try:
            dataset = gdal.Open(layer.source().split("|", 1)[0], gdal.GA_ReadOnly)
            if dataset is None:
                return None
            bands = []
            for index in range(1, dataset.RasterCount + 1):
                band = dataset.GetRasterBand(index)
                statistics_error = None
                gdal.PushErrorHandler("CPLQuietErrorHandler")
                try:
                    try:
                        statistics = band.GetStatistics(False, True)
                    except RuntimeError as exc:
                        statistics = None
                        statistics_error = str(exc)
                finally:
                    gdal.PopErrorHandler()
                valid_text = band.GetMetadataItem("STATISTICS_VALID_PERCENT")
                try:
                    valid_percent = float(valid_text) if valid_text is not None else None
                except ValueError:
                    valid_percent = None
                all_nodata = valid_percent == 0 or (
                    statistics is None
                    and statistics_error is not None
                    and "no valid pixels" in statistics_error.casefold()
                )
                if all_nodata:
                    valid_percent = 0.0
                bands.append({
                    "band": index,
                    "nodata": plain(band.GetNoDataValue()),
                    "valid_percent": valid_percent,
                    "minimum": None if all_nodata or not statistics else plain(statistics[0]),
                    "maximum": None if all_nodata or not statistics else plain(statistics[1]),
                })
            return {
                "bands": bands,
                "all_nodata": bool(bands) and all(item["valid_percent"] == 0 for item in bands),
                "statistics_approximate": False,
            }
        finally:
            gdal.SetThreadLocalConfigOption("GDAL_PAM_ENABLED", previous_pam)

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
            target_crs = crs(a.get("crs") or "EPSG:4326")
            path = (
                destination(a["path"], [".qgs", ".qgz"], a.get("overwrite", False))
                if a.get("path")
                else None
            )
            self.project.clear()
            self.contexts.clear()
            self.project.setCrs(target_crs)
            self.project.setTitle(a.get("title") or "Smart-QGIS")
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
        elif action == "update":
            if a.get("crs"):
                self.project.setCrs(crs(a["crs"]))
            if a.get("title") is not None:
                self.project.setTitle(a["title"])
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
            persist_views(self.project, Path(path).with_suffix(".sources"))
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
        service, url, uri = a.get("service", "osm"), a.get("url"), a.get("uri")
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
        elif not (url or uri):
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
        elif uri is None:
            values = {"url": url}
            if service == "wfs":
                values.update({
                    "typename": a.get("type_name"),
                    "version": a.get("version") or "2.0.0",
                })
                if a.get("crs"):
                    values["srsname"] = a["crs"]
            else:
                values.update({
                    "layers": a.get("layer_name"),
                    "styles": a.get("style_name") or "",
                    "format": a.get("image_format", "image/png"),
                    "crs": a.get("crs") or self.project.crs().authid(),
                })
                if service == "wmts":
                    values["type"] = "wmts"
            if a.get("authcfg"):
                values["authcfg"] = a["authcfg"]
            uri = urlencode({key: value for key, value in values.items() if value is not None})
        layer = (
            QgsVectorLayer(uri, a.get("name") or "WFS", "WFS")
            if service == "wfs"
            else QgsRasterLayer(uri, a.get("name") or service.upper(), "wms")
        )
        if not layer.isValid():
            raise ValueError(
                "Invalid remote layer; verify URL, authorization and network connectivity"
            )
        layer.serverProperties().setAttribution(attribution)
        role = a.get("role") or ("overlay" if service == "wfs" else "basemap")
        layer.setCustomProperty("smart_qgis/basemap", role == "basemap")
        layer.setCustomProperty("smart_qgis/service", service)
        layer.setCustomProperty("smart_qgis/service_role", role)
        before = self.ordered_layers()
        self.project.addMapLayer(layer)
        self.set_order(before + [layer] if role == "basemap" else [layer] + before)
        result = self.layer_info(layer)
        result.update({"service": service, "role": role, "remote": True})
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
            requested = [self.resolve(ref) for ref in a.get("order") or []]
            if len({layer_item.id() for layer_item in requested}) != len(requested):
                raise ValueError("order must not contain duplicate layers")
            remaining = [layer for layer in self.ordered_layers() if layer not in requested]
            self.set_order(requested + remaining)
        elif action == "group":
            refs = a.get("layers") or []
            if not a.get("name") or not refs:
                raise ValueError("group requires name and layers")
            root = self.project.layerTreeRoot()
            group = root.findGroup(a["name"]) or root.addGroup(a["name"])
            for ref in refs:
                layer = self.resolve(ref)
                node = root.findLayer(layer.id())
                if node is None:
                    raise ValueError(f"Layer tree node not found: {ref}")
                clone = node.clone()
                group.addChildNode(clone)
                node.parent().removeChildNode(node)
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
            elif action == "opacity":
                if a.get("opacity") is None:
                    raise ValueError("opacity is required")
                layer.setOpacity(a["opacity"])
                layer.triggerRepaint()
        return {"layers": [self.layer_info(layer_item) for layer_item in self.ordered_layers()]}

    def features(self, a):
        try:
            layer = self.resolve(a["layer"], QgsVectorLayer)
        except ValueError:
            source = Path(str(a["layer"])).expanduser()
            if not source.is_absolute() or not source.is_file():
                raise
            layer = QgsVectorLayer(str(source), source.stem, "ogr")
            if not layer.isValid():
                raise ValueError(f"Cannot inspect vector source: {source}") from None
        request = QgsFeatureRequest()
        if a.get("action", "sample") == "sample":
            request.setLimit(a.get("limit", 10))
        if a.get("expression"):
            expression = QgsExpression(a["expression"])
            if expression.hasParserError():
                raise ValueError(expression.parserErrorString())
            request.setFilterExpression(a["expression"])
        if a.get("action", "sample") == "statistics":
            field = a.get("field")
            index = layer.fields().indexFromName(field or "")
            if index < 0 or not layer.fields()[index].isNumeric():
                raise ValueError("Statistics require an existing numeric field")
            count, missing, total, minimum, maximum = 0, 0, 0.0, math.inf, -math.inf
            for feature in layer.getFeatures(request):
                try:
                    value = float(feature[index])
                    if not math.isfinite(value):
                        raise ValueError
                except (TypeError, ValueError):
                    missing += 1
                    continue
                count += 1
                total += value
                minimum, maximum = min(minimum, value), max(maximum, value)
            return {
                "field": field, "count": count, "null_or_nonnumeric": missing,
                "minimum": minimum if count else None, "maximum": maximum if count else None,
                "sum": total, "mean": total / count if count else None,
            }
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
        line_styles = {
            "solid": "solid", "dash": "dash", "dot": "dot",
            "dash_dot": "dash dot", "dash_dot_dot": "dash dot dot",
        }

        def symbol(spec=None):
            spec = spec or a
            fill = spec.get("color", a.get("color", "#4c78a8"))
            stroke = spec.get("outline", a.get("outline", "#202020"))
            width = spec.get("width", a.get("width", 0.4))
            size = spec.get("size", a.get("size", 2))
            line_style = line_styles[a.get("line_style", "solid")]
            geometry = int(layer.geometryType())
            if geometry == 0:
                return QgsMarkerSymbol.createSimple({
                    "name": a.get("marker", "circle"), "color": fill,
                    "outline_color": stroke, "outline_width": str(width),
                    "size": str(size),
                })
            if geometry == 1:
                return QgsLineSymbol.createSimple({
                    "line_color": fill, "line_width": str(width),
                    "line_style": line_style,
                })
            if geometry == 2:
                return QgsFillSymbol.createSimple({
                    "color": fill, "outline_color": stroke,
                    "outline_width": str(width), "outline_style": line_style,
                })
            result = QgsSymbol.defaultSymbol(layer.geometryType())
            result.setColor(color(fill))
            return result

        field = a.get("category_field")
        renderer_name = a.get("renderer", "single")
        if renderer_name == "rule_based":
            rules = a.get("rules") or []
            if not rules:
                raise ValueError("rule_based renderer requires nonempty rules")
            renderer = QgsRuleBasedRenderer(symbol())
            root = renderer.rootRule()
            for child in list(root.children()):
                root.removeChild(child)
            available = {field.name() for field in layer.fields()}
            for rule in rules:
                expression = QgsExpression(rule["expression"])
                if expression.hasParserError():
                    raise ValueError(expression.parserErrorString())
                missing = set(expression.referencedColumns()) - available
                if missing:
                    raise ValueError(f"Rule expression references unknown fields: {sorted(missing)}")
                root.appendChild(QgsRuleBasedRenderer.Rule(
                    symbol(rule), 0, 0, rule["expression"], rule["label"],
                ))
        elif field:
            if layer.fields().indexFromName(field) < 0 or not a.get("categories"):
                raise ValueError("Provide an existing category_field and nonempty categories")
            categories = [
                QgsRendererCategory(
                    c["value"], symbol(c), str(c.get("label", c["value"]))
                )
                for c in a["categories"]
            ]
            renderer = QgsCategorizedSymbolRenderer(field, categories)
        else:
            renderer = QgsSingleSymbolRenderer(symbol())
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
        mode = a.get("mode", "continuous")
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
        if mode == "mask":
            shader.setColorRampType(QgsColorRampShader.Discrete)
            shader.setColorRampItemList([
                QgsColorRampShader.ColorRampItem(
                    maximum, color(a.get("color", "#666666")),
                    a.get("label") or layer.name(),
                )
            ])
        else:
            requested_ramp = a.get("ramp", "Viridis")
            available_ramps = QgsStyle.defaultStyle().colorRampNames()
            canonical_ramp = next(
                (name for name in available_ramps if name.casefold() == str(requested_ramp).casefold()),
                requested_ramp,
            )
            ramp = QgsStyle.defaultStyle().colorRamp(canonical_ramp)
            if ramp is None:
                raise ValueError("Unknown color ramp; call algorithms(action='ramps')")
            shader.setColorRampType(QgsColorRampShader.Interpolated)
            classes = a.get("classes", 8)
            items = []
            for i in range(classes):
                ratio = i / (classes - 1)
                value = minimum + ratio * (maximum - minimum)
                # QGIS stores shader-item colors as 8-bit RGB(A) in QGZ.
                stable_color = QColor(*ramp.color(ratio).getRgb())
                items.append(QgsColorRampShader.ColorRampItem(value, stable_color, f"{value:.0f}"))
            shader.setColorRampItemList(items)
            legend_settings = shader.legendSettings()
            legend_settings.setMinimumLabel(f"{minimum:.3g}")
            legend_settings.setMaximumLabel(f"{maximum:.3g}")
            shader.setLegendSettings(legend_settings)
        raster_shader = QgsRasterShader(minimum, maximum)
        raster_shader.setRasterShaderFunction(shader)
        # Replacing the renderer otherwise drops the source Alpha mask and paints
        # clipped-out pixels as valid elevations. Preserve an explicit selection;
        # discover provider Alpha too, so restyling repairs older saved renderers.
        previous = layer.renderer()
        alpha_band = previous.alphaBand() if previous else -1
        if not 1 <= alpha_band <= layer.bandCount():
            alpha_band = next((index for index in range(1, layer.bandCount() + 1)
                               if layer.dataProvider().colorInterpretation(index)
                               == Qgis.RasterColorInterpretation.AlphaBand), -1)
        if alpha_band > 0:
            normalize_alpha(layer, alpha_band, worker_profile.name)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, raster_shader)
        renderer.setAlphaBand(alpha_band)
        renderer.setClassificationMin(minimum)
        renderer.setClassificationMax(maximum)
        layer.setRenderer(renderer)
        layer.setOpacity(a.get("opacity", 1))
        return {
            "layer": layer.id(),
            "mode": mode,
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
            algorithm_id = (a.get("algorithm") or "").strip()
            if algorithm_id in self._algorithm_help_cache:
                return self._algorithm_help_cache[algorithm_id]
            algorithm = registry.algorithmById(algorithm_id)
            if not algorithm:
                raise ValueError("Unknown algorithm ID")
            result = {
                "id": algorithm.id(),
                "name": algorithm.displayName(),
                "help": algorithm.shortHelpString(),
                "parameters": [parameter_help(p) for p in algorithm.parameterDefinitions()],
                "outputs": [
                    {"name": p.name(), "description": p.description(), "type": p.type()}
                    for p in algorithm.outputDefinitions()
                ],
            }
            self._algorithm_help_cache[algorithm_id] = result
            return result

        catalog = self.algorithm_catalog(registry)
        query = " ".join((a.get("query") or "").casefold().split())
        terms = self.search_terms(query)
        provider = (a.get("provider") or "").strip().casefold()
        group = (a.get("group") or "").strip().casefold()
        cache_key = (query, provider, group)
        cached = self._algorithm_search_cache.get(cache_key)
        if cached is None:
            eligible = [
                entry for entry in catalog
                if (not provider or entry["provider"].casefold() == provider)
                and (not group or entry["group"].casefold() == group)
            ]
            matches = [entry for entry in eligible if all(term in entry["_search"] for term in terms)]
            matches.sort(key=lambda entry: self.algorithm_rank(entry, query, terms))
            suggestions = []
            suggested_terms = {}
            if terms and not matches:
                suggestions, suggested_terms = self.algorithm_suggestions(eligible, terms)
            cached = (matches, suggestions, suggested_terms)
            self._algorithm_search_cache[cache_key] = cached
            self._algorithm_search_cache.move_to_end(cache_key)
            while len(self._algorithm_search_cache) > 128:
                self._algorithm_search_cache.popitem(last=False)
        else:
            self._algorithm_search_cache.move_to_end(cache_key)

        matches, suggestions, suggested_terms = cached
        offset, limit = a.get("offset", 0), a.get("limit", 12)
        result = {
            "total": len(matches),
            "algorithms": [self.public_algorithm(entry) for entry in matches[offset : offset + limit]],
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(matches) else None,
            "match_mode": "all_terms",
        }
        if suggestions:
            result.update(
                suggestions=[self.public_algorithm(entry) for entry in suggestions[: min(limit, 5)]],
                suggested_terms=suggested_terms,
                next_action=(
                    "No algorithm matched every query term. Choose a returned suggestion only if its "
                    "name and provider match the requested operation, or retry once with fewer/corrected "
                    "keywords; do not execute a suggestion automatically."
                ),
            )
        return result

    @staticmethod
    def search_terms(value):
        """Normalize model-supplied keywords without changing their semantics."""
        return tuple(dict.fromkeys(re.findall(r"[^\W_]+", value, flags=re.UNICODE)))

    @staticmethod
    def public_algorithm(entry):
        return {key: entry[key] for key in ("id", "name", "provider", "group", "group_name")}

    def algorithm_catalog(self, registry):
        if self._algorithm_catalog is not None:
            return self._algorithm_catalog
        catalog = []
        for algorithm in registry.algorithms():
            try:
                tags = algorithm.tags() or []
            except (AttributeError, TypeError):
                tags = []
            provider = algorithm.provider()
            entry = {
                "id": algorithm.id(),
                "name": algorithm.displayName(),
                "provider": provider.id(),
                "group": algorithm.groupId(),
                "group_name": algorithm.group(),
            }
            search_parts = [
                entry["id"], entry["name"], entry["provider"],
                provider.name(), entry["group"], entry["group_name"], *tags,
            ]
            entry["_search"] = " ".join(str(part) for part in search_parts if part).casefold()
            entry["_tokens"] = set(self.search_terms(entry["_search"]))
            catalog.append(entry)
        self._algorithm_catalog = tuple(catalog)
        return self._algorithm_catalog

    @staticmethod
    def algorithm_rank(entry, query, terms):
        algorithm_id = entry["id"].casefold()
        short_id = algorithm_id.partition(":")[2]
        name = entry["name"].casefold()
        words = entry["_tokens"]
        if query == algorithm_id:
            tier = 0
        elif query == short_id:
            tier = 1
        elif query == name:
            tier = 2
        elif query and query in algorithm_id:
            tier = 3
        elif query and query in name:
            tier = 4
        elif terms and all(term in words for term in terms):
            tier = 5
        elif terms and all(any(word.startswith(term) for word in words) for term in terms):
            tier = 6
        else:
            tier = 7
        return tier, len(algorithm_id), algorithm_id

    def algorithm_suggestions(self, eligible, terms):
        """Return bounded candidates for a failed search, never an executable choice."""
        vocabulary = sorted({token for entry in eligible for token in entry["_tokens"]})
        corrected = {}
        for term in terms:
            if any(term in entry["_search"] for entry in eligible):
                continue
            close = sorted(
                (
                    (SequenceMatcher(None, term, token).ratio(), token)
                    for token in vocabulary
                    if abs(len(token) - len(term)) <= max(3, len(term) // 2)
                ),
                reverse=True,
            )
            choices = [token for ratio, token in close[:3] if ratio >= 0.72]
            if choices:
                corrected[term] = choices
        usable_terms = {
            term for term in terms if any(term in entry["_search"] for entry in eligible)
        }
        usable_terms.update(choice for choices in corrected.values() for choice in choices[:1])
        candidates = [
            entry for entry in eligible
            if usable_terms and any(term in entry["_search"] for term in usable_terms)
        ]
        candidates.sort(
            key=lambda entry: (
                -sum(term in entry["_search"] for term in usable_terms),
                self.algorithm_rank(entry, " ".join(terms), terms),
            )
        )
        return candidates[:5], corrected

    def prepare_processing(self, a):
        alg = QgsApplication.processingRegistry().algorithmById(a["algorithm"])
        if not alg:
            raise ValueError("Unknown algorithm; discover available IDs first")
        params = a["parameters"].copy()
        known = {p.name() for p in alg.parameterDefinitions()}
        unknown = set(params) - known
        if unknown:
            raise InvalidParameters(f"Unknown algorithm parameters: {sorted(unknown)}")
        # Only resolve IDs in input layer parameters: never reinterpret output names/expressions.
        # GRASS Processing is an exception to the usual path-or-layer QGIS convention.
        # Its provider dereferences a raster/vector source as a project layer while
        # constructing a GRASS location.  A bare file path can therefore become
        # ``None`` inside the provider (and later fail at ``layer.crs()``).  Bind
        # task-owned file inputs to real project layers before GRASS sees them.
        is_grass = (
            alg.id().casefold().startswith("grass:")
            or alg.provider().id().casefold().startswith("grass")
        )
        for definition in alg.parameterDefinitions():
            key = definition.name()
            if key not in params or definition.isDestination():
                continue
            if definition.type() in {"source", "vector", "raster", "maplayer", "multilayer"}:
                layer_type = definition.type()

                def resolve_id(v, layer_type=layer_type):
                    if not isinstance(v, str):
                        return v
                    layer = self.project.mapLayer(v)
                    if layer is not None:
                        return layer
                    if is_grass:
                        return self.grass_input_layer(v, layer_type)
                    return v

                params[key] = (
                    [resolve_id(v) for v in params[key]]
                    if isinstance(params[key], list)
                    else resolve_id(params[key])
                )
        context = QgsProcessingContext()
        context.setProject(self.project)
        if alg.id() in {"native:rastercalc", "qgis:rastercalculator"}:
            expression = params.get("EXPRESSION")
            if isinstance(expression, str) and QgsRasterCalcNode.parseRasterCalcString(
                expression, ""
            ) is None:
                raise InvalidParameters(
                    "Invalid QGIS raster calculator EXPRESSION syntax; use its documented "
                    "double-quoted layer@band references and supported operators"
                )
        valid, message = alg.checkParameterValues(params, context)
        if not valid:
            raise InvalidParameters(message)
        return alg, params, context

    def grass_input_layer(self, reference, parameter_type):
        """Materialize a file source as a valid project layer for GRASS.

        Other Processing providers normally accept file paths directly.  The
        QGIS GRASS provider, however, later calls ``layer.crs()`` on its input,
        so a path which it cannot resolve to a project layer causes an internal
        AttributeError rather than a useful parameter error.
        """
        for layer in self.project.mapLayers().values():
            if layer.source() == reference:
                return layer

        source = reference.split("|", 1)[0]
        path = Path(source)
        if not path.is_absolute() or not path.is_file():
            return reference

        name = path.stem
        layer_types = (
            (QgsRasterLayer,) if parameter_type == "raster"
            else (QgsVectorLayer,) if parameter_type == "vector"
            else (QgsRasterLayer, QgsVectorLayer)
        )
        layer = None
        for layer_type in layer_types:
            candidate = (
                QgsRasterLayer(reference, name, "gdal")
                if layer_type is QgsRasterLayer
                else QgsVectorLayer(reference, name, "ogr")
            )
            if candidate.isValid():
                layer = candidate
                break
        if layer is None:
            return reference
        if not layer.crs().isValid():
            raise InvalidParameters(
                f"GRASS Processing input has no valid CRS: {reference}"
            )
        self.project.addMapLayer(layer)
        return layer

    def processing_preflight(self, a):
        self.prepare_processing(a)
        return {"valid": True, "executed": False}

    def run_processing(self, a):
        alg, params, context = self.prepare_processing(a)
        feedback = QgsProcessingFeedback()
        import processing

        result = processing.run(alg, params, context=context, feedback=feedback)
        loaded = []
        warnings = []
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
                        info = self.layer_info(layer)
                        if isinstance(layer, QgsRasterLayer):
                            summary = self.raster_summary(layer)
                            if summary is not None:
                                info["raster_summary"] = summary
                                if summary["all_nodata"]:
                                    warnings.append({
                                        "code": "RASTER_ALL_NODATA",
                                        "message": "A raster output contains no valid pixels",
                                        "layer_id": layer.id(),
                                        "source": layer.source(),
                                    })
                        loaded.append(info)
        self.contexts.append(context)
        return {
            "algorithm": alg.id(),
            "outputs": plain(result),
            "loaded_layers": loaded,
            "warnings": warnings,
            "log": feedback.textLog()[-12000:],
        }

    def transformed_extent(self, layer, target):
        if not layer.crs().isValid():
            raise ValueError(f"Layer has no valid CRS: {layer.name()}")
        return QgsCoordinateTransform(layer.crs(), target, self.project).transformBoundingBox(
            layer.extent()
        )

    def visible_extent(self, layer, target):
        """Use the footprint of valid raster cells when it is cheap to inspect."""
        if not isinstance(layer, QgsRasterLayer) or layer.providerType() != "gdal":
            return self.transformed_extent(layer, target)
        import numpy as np
        from osgeo import gdal

        dataset = gdal.Open(layer.source().split("|", 1)[0], gdal.GA_ReadOnly)
        if (dataset is None or dataset.RasterCount < 1
                or dataset.RasterXSize * dataset.RasterYSize > 25_000_000):
            return self.transformed_extent(layer, target)
        band = dataset.GetRasterBand(1)
        values = band.ReadAsArray()
        valid = np.isfinite(values)
        nodata = band.GetNoDataValue()
        if nodata is not None:
            valid &= values != nodata
        rows = np.flatnonzero(valid.any(axis=1))
        columns = np.flatnonzero(valid.any(axis=0))
        if not len(rows) or not len(columns):
            return self.transformed_extent(layer, target)
        transform = dataset.GetGeoTransform()
        corners = [
            (x, y)
            for x in (int(columns[0]), int(columns[-1]) + 1)
            for y in (int(rows[0]), int(rows[-1]) + 1)
        ]
        points = [
            (transform[0] + x * transform[1] + y * transform[2],
             transform[3] + x * transform[4] + y * transform[5])
            for x, y in corners
        ]
        extent = QgsRectangle(
            min(x for x, _ in points), min(y for _, y in points),
            max(x for x, _ in points), max(y for _, y in points),
        )
        return QgsCoordinateTransform(layer.crs(), target, self.project).transformBoundingBox(extent)

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
        target = crs(a["crs"]) if a.get("crs") else layers[0].crs()
        if a.get("extent"):
            extent = QgsRectangle(*a["extent"])
            if a["extent"][0] >= a["extent"][2] or a["extent"][1] >= a["extent"][3]:
                raise ValueError("Extent bounds must be ordered")
        elif a.get("extent_layer"):
            extent = self.visible_extent(self.resolve(a["extent_layer"]), target)
        else:
            content = [layer_item for layer_item in layers if layer_item.providerType() != "wms"]
            if not content:
                raise ValueError("Basemap-only maps require explicit extent")
            extent = QgsRectangle()
            extent.setMinimal()
            for layer in content:
                extent.combineExtentWith(self.visible_extent(layer, target))
        if extent.isEmpty() or not extent.isFinite():
            raise ValueError("Map extent must be finite and nonempty")
        extent.scale(1.08)
        layout = QgsPrintLayout(self.project)
        layout.initializeDefaults()
        layout.setName(name)
        requested_elements = a.get("map_elements") or {}
        thematic_layers = [
            layer_item for layer_item in layers
            if layer_item.providerType() != "wms"
            and not layer_item.customProperty("smart_qgis/basemap", False)
        ]

        def usable_inside_anchors():
            """Find genuinely empty thematic corners; basemap pixels never count as evidence."""
            if not thematic_layers:
                return []
            settings = QgsMapSettings()
            settings.setLayers(thematic_layers)
            settings.setDestinationCrs(target)
            settings.setExtent(extent)
            settings.setOutputSize(QSize(160, 160))
            settings.setBackgroundColor(QColor(0, 0, 0, 0))
            job = QgsMapRendererParallelJob(settings)
            job.start()
            job.waitForFinished()
            image = job.renderedImage()
            windows = {
                "top_left": (0, 0, 58, 48),
                "top_right": (102, 0, 160, 48),
                "bottom_left": (0, 112, 58, 160),
                "bottom_right": (102, 112, 160, 160),
            }
            scores = {}
            for anchor, (x0, y0, x1, y1) in windows.items():
                painted = sum(
                    1 for y in range(y0, y1) for x in range(x0, x1)
                    if image.pixelColor(x, y).alpha() > 8
                )
                scores[anchor] = painted / max(1, (x1 - x0) * (y1 - y0))
            # A corner must be mostly transparent, not just lighter than its
            # neighbors. Preference order keeps legend and scale conventions.
            preference = {name: index for index, name in enumerate(
                ("top_right", "bottom_right", "bottom_left", "top_left")
            )}
            return sorted(
                (anchor for anchor, score in scores.items() if score <= 0.50),
                key=lambda anchor: (scores[anchor], preference[anchor]),
            )

        def legend_item_count():
            count = 0
            for layer_item in thematic_layers:
                try:
                    count += max(1, len(layer_item.legendSymbologyItems()))
                except Exception:
                    count += 1
            return max(1, count)

        blank_anchors = usable_inside_anchors()
        automatic_inside_space = len(blank_anchors) >= 2
        automatic_legend_items = legend_item_count()
        defaults = {
            "title": {"frame": "outside", "anchor": "top"},
            "legend": {"frame": "inside", "anchor": "top_right"},
            "scalebar": {"frame": "inside", "anchor": "bottom_right"},
            "north_arrow": {"frame": "inside", "anchor": "top_left"},
        }
        if automatic_inside_space:
            legend_anchor = next(
                (item for item in ("top_right", "bottom_left", "top_left", "bottom_right")
                 if item in blank_anchors), blank_anchors[0]
            )
            scale_anchor = next(
                (item for item in ("bottom_right", "bottom_left", "top_right", "top_left")
                 if item in blank_anchors and item != legend_anchor), blank_anchors[1]
            )
            defaults["legend"]["anchor"] = legend_anchor
            defaults["scalebar"]["anchor"] = scale_anchor
            remaining = [item for item in blank_anchors if item not in {legend_anchor, scale_anchor}]
            if remaining:
                defaults["north_arrow"]["anchor"] = remaining[0]
            else:
                defaults["north_arrow"] = {"frame": "outside", "anchor": "top_left"}
        # Default placements are evidence-driven.  If thematic data leave a
        # transparent area, keep required elements inside it.  Otherwise use a
        # compact bottom row for a short legend, or a right-side column for a
        # longer one.  Explicit user placements always override this policy.
        if not automatic_inside_space:
            if automatic_legend_items <= 3:
                defaults["legend"] = {"frame": "outside", "anchor": "bottom_left"}
                defaults["scalebar"] = {"frame": "outside", "anchor": "bottom_right"}
            else:
                defaults["legend"] = {"frame": "outside", "anchor": "top_right"}
                defaults["scalebar"] = {"frame": "outside", "anchor": "bottom_right"}
            defaults["north_arrow"] = {"frame": "outside", "anchor": "top_left"}

        def placement(name):
            requested = requested_elements.get(name) or {}
            default = defaults[name]
            return {
                "frame": requested.get("frame") or default["frame"],
                "anchor": requested.get("anchor") or default["anchor"],
            }

        placements = {name: placement(name) for name in defaults}
        resolved_legend_flow = None
        resolved_scalebar = None
        resolved_annotations = None
        enabled_elements = {
            "title": a.get("show_title", True),
            "legend": a.get("legend", True),
            "scalebar": a.get("scalebar", True),
            "north_arrow": a.get("north_arrow", False),
        }
        outside_names = [
            name for name, value in placements.items()
            if enabled_elements[name] and value["frame"] == "outside"
        ]
        outside_bottom = any(placements[name]["anchor"].startswith("bottom") for name in outside_names)
        outside_top = any(placements[name]["anchor"].startswith("top") for name in outside_names)
        outside_left = any(placements[name]["anchor"].endswith("left") or placements[name]["anchor"] == "left"
                           for name in outside_names)
        outside_right = any(placements[name]["anchor"].endswith("right") or placements[name]["anchor"] == "right"
                            for name in outside_names)
        # A fixed A4 page makes near-square thematic layers look like small
        # thumbnails when a title or outside elements reserve a narrow band.
        # Size an automatic page around the real data ratio instead. The map
        # keeps its geographic aspect and full extent; only unused paper moves.
        extent_ratio = extent.width() / extent.height()
        orientation = a.get("page_orientation", "auto")
        requested_orientation = orientation
        if orientation == "auto":
            orientation = "landscape" if extent_ratio >= 1.15 else "portrait"
        left_margin = 12 + (58 if outside_left else 0)
        right_margin = 12 + (64 if outside_right else 0)
        top_margin = 28 + (24 if outside_top and placements["title"]["anchor"] != "top" else 0)
        # A footer is a placement band, not a fixed 45 mm page reservation.
        bottom_margin = 12 + (32 if outside_bottom else 0)
        map_frame = a.get("map_frame") or {}
        frame_mode = map_frame.get("mode", "auto")
        if frame_mode not in {"auto", "maximize"}:
            raise ValueError("map_frame.mode must be auto or maximize")
        if requested_orientation == "auto":
            if orientation == "landscape":
                width = 297
                map_width = width - left_margin - right_margin
                map_height = map_width / extent_ratio
                height = top_margin + bottom_margin + map_height
                # A nearly square wide layer needs a compact near-square page,
                # rather than a small thumbnail in fixed A4 landscape.
                if height > 297:
                    orientation = "portrait"
                    height = 297
                    map_height = height - top_margin - bottom_margin
                    map_width = map_height * extent_ratio
                    width = map_width + left_margin + right_margin
            else:
                # Unlike a fixed A4 portrait page, a compact portrait page
                # keeps a square or tall thematic frame dominant instead of
                # leaving a long, unused lower strip.
                map_height = 240
                map_width = map_height * extent_ratio
                width = map_width + left_margin + right_margin
                height = top_margin + bottom_margin + map_height
        elif orientation == "landscape":
            width, height = 297, 210
            map_width, map_height = None, None
        else:
            width, height = 210, 297
            map_width, map_height = None, None
        if width < 100 or height < 100 or width > 1000 or height > 1000:
            raise ValueError("Automatic page size is outside supported layout bounds")
        layout.pageCollection().pages()[0].setPageSize(QgsLayoutSize(width, height))
        layout.renderContext().setDpi(150)
        frame_width = width - left_margin - right_margin
        frame_height = height - top_margin - bottom_margin
        if frame_width <= 20 or frame_height <= 20:
            raise ValueError("Requested outside map-element placements leave no usable map frame")
        if map_width is not None and map_height is not None:
            map_width = min(map_width, frame_width)
            map_height = min(map_height, frame_height)
        elif extent.width() / extent.height() >= frame_width / frame_height:
            map_width, map_height = frame_width, frame_width * extent.height() / extent.width()
        else:
            map_width, map_height = frame_height * extent.width() / extent.height(), frame_height
        map_x = left_margin + (frame_width - map_width) / 2
        map_y = top_margin + (frame_height - map_height) / 2
        map_item = QgsLayoutItemMap(layout)
        map_item.setId("main-map")
        layout.addLayoutItem(map_item)
        map_item.attemptMove(QgsLayoutPoint(map_x, map_y))
        map_item.attemptResize(QgsLayoutSize(map_width, map_height))
        map_item.setCrs(target)
        map_item.setLayers(layers)
        map_item.setKeepLayerSet(True)
        map_item.zoomToExtent(extent)
        map_item.setFrameEnabled(True)
        map_item.setCustomProperty(
            "smart-qgis:page-coverage", round((map_width * map_height) / (width * height), 4)
        )

        def position_for(name, item_width, item_height):
            choice = placements[name]
            anchor = choice["anchor"]
            inside = choice["frame"] == "inside"
            if inside:
                x_left, x_right = map_x + 4, map_x + map_width - item_width - 4
                y_top, y_bottom = map_y + 4, map_y + map_height - item_height - 4
                x_center, y_center = map_x + (map_width - item_width) / 2, map_y + (map_height - item_height) / 2
            else:
                x_left, x_right = 12, width - item_width - 12
                y_top = 8 if name == "title" or anchor == "top" else map_y
                y_bottom = map_y + map_height + 8
                x_center, y_center = (width - item_width) / 2, map_y + (map_height - item_height) / 2
            x = x_left if anchor.endswith("left") or anchor == "left" else (
                x_right if anchor.endswith("right") or anchor == "right" else x_center
            )
            y = y_top if anchor.startswith("top") or anchor == "top" else (
                y_bottom if anchor.startswith("bottom") or anchor == "bottom" else y_center
            )
            return x, y

        def place(item, name, fallback_width, fallback_height):
            size = item.sizeWithUnits()
            item_width = max(fallback_width, size.width())
            item_height = max(fallback_height, size.height())
            item.attemptMove(QgsLayoutPoint(*position_for(name, item_width, item_height)))
            item.setCustomProperty("smart-qgis:frame", placements[name]["frame"])
            item.setCustomProperty("smart-qgis:anchor", placements[name]["anchor"])
            return item

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

        if a.get("show_title", True):
            title_width = (
                max(20, map_width - 8)
                if placements["title"]["frame"] == "inside"
                else width - 24
            )
            title_item = label(a.get("title") or name, 12, 8, title_width, 15,
                               min(18, max(11, round(width / 12))))
            title_item.setId("map-title")
            title_anchor = placements["title"]["anchor"]
            title_item.setHAlign(
                Qt.AlignmentFlag.AlignLeft
                if title_anchor.endswith("left") or title_anchor == "left"
                else Qt.AlignmentFlag.AlignRight
                if title_anchor.endswith("right") or title_anchor == "right"
                else Qt.AlignmentFlag.AlignHCenter
            )
            place(title_item, "title", min(title_width, 90), 15)
        if a.get("grid", True):
            grid_reference = crs(a.get("grid_crs") or "EPSG:4326")
            annotation_options = a.get("coordinate_annotations") or {}
            annotation_format = annotation_options.get("format", "auto")
            if annotation_format in {"degree_minute", "degree_minute_second"} and not grid_reference.isGeographic():
                raise ValueError("Degree-based coordinate labels require a geographic annotation CRS")
            if annotation_options.get("cardinal_directions") is True and not grid_reference.isGeographic():
                raise ValueError("E/W/N/S coordinate suffixes require a geographic annotation CRS")
            grid = QgsLayoutItemMapGrid("Coordinate annotations", map_item)
            map_item.grids().addGrid(grid)
            grid.setEnabled(True)
            grid.setCrs(grid_reference)
            grid_extent = QgsCoordinateTransform(
                target, grid_reference, self.project
            ).transformBoundingBox(map_item.extent())
            density_divisor = {
                "auto": 5, "dense": 8, "sparse": 3,
            }[annotation_options.get("density", "auto")]
            span = max(grid_extent.width(), grid_extent.height()) / density_divisor
            power = 10 ** math.floor(math.log10(span))
            interval = next((n * power for n in (1, 2, 5, 10) if n * power >= span), 10 * power)
            grid.setIntervalX(interval)
            grid.setIntervalY(interval)
            grid.setStyle(
                QgsLayoutItemMapGrid.Solid
                if annotation_options.get("grid_lines", False)
                else QgsLayoutItemMapGrid.FrameAnnotationsOnly
            )
            grid.setFrameStyle(QgsLayoutItemMapGrid.ExteriorTicks)
            grid.setAnnotationEnabled(True)
            suffixes = annotation_options.get("cardinal_directions")
            annotation_formats = {
                ("auto", False): QgsLayoutItemMapGrid.Decimal,
                ("auto", True): QgsLayoutItemMapGrid.DecimalWithSuffix,
                ("decimal", False): QgsLayoutItemMapGrid.Decimal,
                ("decimal", True): QgsLayoutItemMapGrid.DecimalWithSuffix,
                ("degree_minute", False): QgsLayoutItemMapGrid.DegreeMinuteNoSuffix,
                ("degree_minute", True): QgsLayoutItemMapGrid.DegreeMinute,
                ("degree_minute_second", False): QgsLayoutItemMapGrid.DegreeMinuteSecondNoSuffix,
                ("degree_minute_second", True): QgsLayoutItemMapGrid.DegreeMinuteSecond,
            }
            show_suffixes = bool(suffixes) if suffixes is not None else False
            grid.setAnnotationFormat(annotation_formats[(annotation_format, show_suffixes)])
            precision = annotation_options.get("precision")
            grid.setAnnotationPrecision(1 if precision is None else precision)
            grid_format = QgsTextFormat()
            grid_format.setFont(QFont("Arial", 8))
            grid_format.setSize(8)
            grid.setAnnotationTextFormat(grid_format)
            sides = set(annotation_options.get("sides") or
                        ("top", "bottom", "left", "right"))
            for side_name, side, display in (
                ("top", QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.LongitudeOnly),
                ("bottom", QgsLayoutItemMapGrid.Bottom, QgsLayoutItemMapGrid.LongitudeOnly),
                ("left", QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.LatitudeOnly),
                ("right", QgsLayoutItemMapGrid.Right, QgsLayoutItemMapGrid.LatitudeOnly),
            ):
                grid.setAnnotationDisplay(display if side_name in sides else QgsLayoutItemMapGrid.HideAll, side)
            resolved_annotations = {
                "crs": grid_reference.authid(),
                "format": annotation_format,
                "precision": 1 if precision is None else precision,
                "cardinal_directions": show_suffixes,
                "density": annotation_options.get("density", "auto"),
                "interval": interval,
                "grid_lines": annotation_options.get("grid_lines", False),
                "sides": sorted(sides),
            }
        if a.get("legend", True):
            legend = QgsLayoutItemLegend(layout)
            map_title = a.get("title") or name
            map_language = a.get("map_language", "auto")
            if map_language not in {"auto", "zh", "en"}:
                raise ValueError("map_language must be auto, zh or en")
            use_chinese = map_language == "zh" or (
                map_language == "auto" and bool(re.search(r"[\u4e00-\u9fff]", map_title))
            )
            if a.get("show_legend_title", True):
                legend.setTitle(
                    a.get("legend_title")
                    or ("图例" if use_chinese else "Legend")
                )
            else:
                legend.setTitle("")
            legend.setLinkedMap(map_item)
            if hasattr(Qgis, "LegendSyncMode"):
                legend.setSyncMode(Qgis.LegendSyncMode.Manual)
            else:
                legend.setAutoUpdateModel(False)
            root = legend.model().rootGroup()
            root.clear()
            for layer in layers:
                if layer.providerType() != "wms":
                    node = root.addLayer(layer)
                    legend_nodes = legend.model().layerLegendNodes(node)
                    if (len(legend_nodes) == 2
                            and re.fullmatch(r"Band \d+(?: \([^)]*\))?", str(legend_nodes[0].data(0)))):
                        QgsMapLayerLegendUtils.setLegendNodeUserLabel(node, 0, " ")
                        legend.model().refreshLayerLegend(node)
            legend_flow = (requested_elements.get("legend") or {}).get("flow", "auto")
            if legend_flow == "horizontal" or (
                legend_flow == "auto"
                and placements["legend"] == {"frame": "outside", "anchor": "bottom_left"}
                and automatic_legend_items <= 3
            ):
                legend.setColumnCount(automatic_legend_items)
            elif legend_flow == "vertical":
                legend.setColumnCount(1)
            resolved_legend_flow = legend_flow
            layout.addLayoutItem(legend)
            legend.adjustBoxSize()
            legend.setId("map-legend")
            place(legend, "legend", 55, 28)
            legend.setBackgroundEnabled(True)
            legend.setBackgroundColor(QColor(255, 255, 255, 230))
            legend_border = (requested_elements.get("legend") or {}).get("border")
            legend.setFrameEnabled(False if legend_border is None else legend_border)
        if a.get("scalebar", True):
            scale = QgsLayoutItemScaleBar(layout)
            scale_options = requested_elements.get("scalebar") or {}
            scale_styles = {
                "single_box": "Single Box",
                "double_box": "Double Box",
                "line_ticks_middle": "Line Ticks Middle",
            }
            scale.setStyle(scale_styles[scale_options.get("style", "single_box")])
            scale.setLinkedMap(map_item)
            scale.applyDefaultSize()
            map_extent = map_item.extent()
            if target.isGeographic():
                width_km = (map_extent.width() * 111.32
                            * max(0.01, math.cos(math.radians(map_extent.center().y()))))
            else:
                width_km = (map_extent.width() * QgsUnitTypes.fromUnitToUnitFactor(
                    target.mapUnits(), QgsUnitTypes.DistanceKilometers
                ))
            requested_units = scale_options.get("units", "auto")
            if requested_units == "auto":
                requested_units = "meters" if width_km < 1 else "kilometers"
            unit_options = {
                "meters": (QgsUnitTypes.DistanceMeters, "m", width_km * 1000),
                "kilometers": (QgsUnitTypes.DistanceKilometers, "km", width_km),
                "miles": (QgsUnitTypes.DistanceMiles, "mi", width_km / 1.609344),
            }
            scale_units, unit_label, width_in_units = unit_options[requested_units]
            scale.setUnits(scale_units)
            scale.setUnitsPerSegment(float(f"{max(width_in_units / 10, 0.000001):.2g}"))
            scale.setUnitLabel(unit_label)
            scale.setNumberOfSegments(2)
            scale.setNumberOfSegmentsLeft(0)
            resolved_scalebar = {
                "units": requested_units,
                "style": scale_options.get("style", "single_box"),
            }
            layout.addLayoutItem(scale)
            scale.refresh()
            scale.setId("map-scalebar")
            place(scale, "scalebar", 42, 8)
        if a.get("north_arrow", False):
            arrow_path = next((
                str(path) for root in QgsApplication.svgPaths()
                for path in Path(root).glob("**/*NorthArrow*.svg")
            ), None)
            if arrow_path is None:
                raise ValueError("QGIS north-arrow SVG is unavailable")
            north_arrow = QgsLayoutItemPicture(layout)
            north_arrow.setPicturePath(arrow_path)
            north_arrow.setId("map-north-arrow")
            layout.addLayoutItem(north_arrow)
            north_arrow.attemptResize(QgsLayoutSize(14, 14))
            place(north_arrow, "north_arrow", 14, 14)
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
            "page_orientation": orientation,
            "map_frame_mode": frame_mode,
            "map_frame_page_coverage": map_item.customProperty("smart-qgis:page-coverage"),
            "resolved_element_placements": {
                name: placements[name] for name, enabled in enabled_elements.items() if enabled
            },
            "legend_layer_names": [
                layer_item.name() for layer_item in layers if layer_item.providerType() != "wms"
            ],
            "legend_flow": resolved_legend_flow,
            "legend_border": (
                False
                if (requested_elements.get("legend") or {}).get("border") is None
                else (requested_elements.get("legend") or {}).get("border")
            ) if a.get("legend", True) else None,
            "scalebar": resolved_scalebar,
            "coordinate_annotations": resolved_annotations,
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

        if operation.startswith("_"):
            import reliable_ops

            return reliable_ops.dispatch(self, operation, arguments)

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
                response = {
                    "id": request.get("id"), "error": f"{type(exc).__name__}: {exc}",
                    "error_code": "INVALID_PARAMETERS" if isinstance(exc, InvalidParameters) else "OPERATION_FAILED",
                }
            protocol.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    finally:
        engine.close()
        protocol.close()


if __name__ == "__main__":
    main()
