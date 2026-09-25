"""Additional worker operations: common visualizations, QML and vector exchange."""

import json
import math
from pathlib import Path

from qgis.core import (
    QgsClassificationEqualInterval,
    QgsClassificationJenks,
    QgsClassificationQuantile,
    QgsContrastEnhancement,
    QgsExpression,
    QgsFeatureRequest,
    QgsGraduatedSymbolRenderer,
    QgsHillshadeRenderer,
    QgsMultiBandColorRenderer,
    QgsRasterLayer,
    QgsSingleBandGrayRenderer,
    QgsStyle,
    QgsSymbol,
    QgsVectorFileWriter,
    QgsVectorLayer,
)


def style_file(engine, a, destination):
    layer = engine.resolve(a["layer"])
    if a["action"] == "save":
        path = destination(a["path"], [".qml"], a.get("overwrite", False))
        message, success = layer.saveNamedStyle(path)
    else:
        path = Path(a["path"]).expanduser()
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".qml":
            raise ValueError("Expected an existing absolute .qml path")
        message, success = layer.loadNamedStyle(str(path))
    if not success:
        raise ValueError(message)
    return {"layer": layer.id(), "path": str(path), "action": a["action"]}


def vector_data(engine, a, destination, crs):
    action = a["action"]
    if action == "create":
        data = a.get("geojson")
        if not data or data.get("type") != "FeatureCollection":
            raise ValueError("geojson must be a FeatureCollection")
        source = QgsVectorLayer(json.dumps(data), a["name"], "ogr")
        if not source.isValid():
            raise ValueError("Invalid GeoJSON")
        layer = source.materialize(QgsFeatureRequest())
        layer.setName(a["name"])
        layer.setCrs(crs(a["crs"]))
        engine.project.addMapLayer(layer)
        return engine.layer_info(layer)
    layer = engine.resolve(a.get("layer"), QgsVectorLayer)
    if action == "clear_selection":
        layer.removeSelection()
        return {"selected": 0}
    if action == "select":
        expression = QgsExpression(a.get("expression") or "")
        if expression.hasParserError():
            raise ValueError(expression.parserErrorString())
        layer.selectByExpression(a["expression"])
        return {"selected": layer.selectedFeatureCount()}
    if action == "statistics":
        field = a.get("field")
        index = layer.fields().indexFromName(field or "")
        if index < 0:
            raise ValueError("An existing numeric field is required")
        count, missing, total, minimum, maximum = 0, 0, 0.0, math.inf, -math.inf
        for feature in layer.getFeatures():
            try:
                value = float(feature[index])
                if not math.isfinite(value):
                    raise ValueError("nonfinite")
            except (ValueError, TypeError):
                missing += 1
                continue
            count += 1
            total += value
            minimum, maximum = min(minimum, value), max(maximum, value)
        return {
            "field": field,
            "count": count,
            "null_or_nonnumeric": missing,
            "minimum": minimum if count else None,
            "maximum": maximum if count else None,
            "sum": total,
            "mean": total / count if count else None,
        }
    path = destination(a.get("path"), [".gpkg", ".geojson", ".shp"], a.get("overwrite", False))
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = {".gpkg": "GPKG", ".geojson": "GeoJSON", ".shp": "ESRI Shapefile"}[
        Path(path).suffix.lower()
    ]
    options.fileEncoding = "UTF-8"
    options.onlySelectedFeatures = a.get("selected_only", False)
    from qgis.core import QgsCoordinateTransform

    options.ct = QgsCoordinateTransform(layer.crs(), crs(a["crs"]), engine.project)
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, engine.project.transformContext(), options
    )
    if result[0] != QgsVectorFileWriter.NoError:
        raise ValueError(str(result))
    return {"path": path, "crs": a["crs"], "selected_only": options.onlySelectedFeatures}


def render_raster(engine, a):
    layer = engine.resolve(a["layer"], QgsRasterLayer)
    provider = layer.dataProvider()
    bands = [a["red"], a["green"], a["blue"]] if a["mode"] == "rgb" else [a["band"]]
    if any(b > layer.bandCount() for b in bands):
        raise ValueError("Band exceeds raster band count")

    def enhancement(band):
        stats = provider.bandStatistics(band)
        low, high = stats.minimumValue, stats.maximumValue
        if not math.isfinite(low) or not math.isfinite(high):
            raise ValueError("Raster has no finite statistics")
        e = QgsContrastEnhancement(provider.dataType(band))
        e.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
        e.setMinimumValue(low)
        e.setMaximumValue(high if high > low else low + 1)
        return e

    if a["mode"] == "gray":
        renderer = QgsSingleBandGrayRenderer(provider, a["band"])
        renderer.setContrastEnhancement(enhancement(a["band"]))
    elif a["mode"] == "rgb":
        renderer = QgsMultiBandColorRenderer(provider, *bands)
        renderer.setRedContrastEnhancement(enhancement(bands[0]))
        renderer.setGreenContrastEnhancement(enhancement(bands[1]))
        renderer.setBlueContrastEnhancement(enhancement(bands[2]))
    else:
        renderer = QgsHillshadeRenderer(provider, a["band"], a["azimuth"], a["altitude"])
        renderer.setZFactor(a["z_factor"])
    layer.setRenderer(renderer)
    layer.setOpacity(a["opacity"])
    return {"layer": layer.id(), "renderer": renderer.type(), "bands": bands}


def style_graduated(engine, a):
    layer = engine.resolve(a["layer"], QgsVectorLayer)
    index = layer.fields().indexFromName(a["field"])
    if index < 0 or not layer.fields()[index].isNumeric():
        raise ValueError("Graduated styles require an existing numeric field")
    ramp = QgsStyle.defaultStyle().colorRamp(a["ramp"])
    if ramp is None:
        raise ValueError("Unknown color ramp")
    method = {
        "equal_interval": QgsClassificationEqualInterval,
        "quantile": QgsClassificationQuantile,
        "jenks": QgsClassificationJenks,
    }[a["method"]]()
    renderer = QgsGraduatedSymbolRenderer(a["field"], [])
    renderer.setSourceSymbol(QgsSymbol.defaultSymbol(layer.geometryType()))
    renderer.setSourceColorRamp(ramp)
    renderer.setClassificationMethod(method)
    renderer.updateClasses(layer, a["classes"])
    renderer.updateColorRamp(ramp)
    layer.setRenderer(renderer)
    return {
        "layer": layer.id(),
        "renderer": renderer.type(),
        "classes": len(renderer.ranges()),
        "ranges": [
            {"lower": r.lowerValue(), "upper": r.upperValue(), "label": r.label()}
            for r in renderer.ranges()
        ],
    }
