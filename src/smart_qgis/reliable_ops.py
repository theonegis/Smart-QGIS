"""Private recovery operations executed only inside the QGIS worker.

Keep this module independent of Pydantic, LangChain and the MCP environment.
"""

import importlib
import json
import math
import platform
from pathlib import Path

from osgeo import gdal
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsLayoutItemMap,
    QgsRasterLayer,
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QT_VERSION_STR

if __package__:
    from .raster_display import persist_views
else:
    from raster_display import persist_views


def environment(engine):
    validation_dependencies = {}
    for name in ("numpy", "shapely", "pyproj"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            validation_dependencies[name] = None
        else:
            validation_dependencies[name] = module.__version__
    return {
        "qgis": Qgis.QGIS_VERSION,
        "gdal": gdal.VersionInfo(),
        "python": platform.python_version(),
        "qt": QT_VERSION_STR,
        "providers": sorted(p.id() for p in engine.app.processingRegistry().providers()),
        "validation_dependencies": validation_dependencies,
    }


def data_layer(engine, reference, kind=None):
    layer = engine.project.mapLayer(reference)
    if layer is not None:
        return layer
    path = Path(reference.split("|")[0])
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Inspection requires an existing absolute file or exact loaded layer ID")
    if kind != "raster":
        candidate = QgsVectorLayer(reference, path.stem, "ogr")
        if candidate.isValid():
            return candidate
    candidate = QgsRasterLayer(reference, path.stem, "gdal")
    if not candidate.isValid():
        raise ValueError("Source is not a readable vector or raster")
    return candidate


def inspect(engine, a):
    layer = data_layer(engine, a["source"], a.get("kind"))
    result = engine.layer_info(layer)
    result["crs_wkt"] = layer.crs().toWkt()
    result["geographic"] = layer.crs().isGeographic()
    result["map_units"] = str(layer.crs().mapUnits())
    if isinstance(layer, QgsRasterLayer):
        result["pixel_size"] = [layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY()]
        provider = layer.dataProvider()
        result["nodata"] = []
        for band in range(1, layer.bandCount() + 1):
            value = (
                provider.sourceNoDataValue(band) if provider.sourceHasNoDataValue(band) else None
            )
            result["nodata"].append("nan" if value is not None and math.isnan(value) else value)
    return result


def snapshot(engine, a):
    directory = Path(a["directory"])
    if not directory.is_absolute():
        raise ValueError("Snapshot directory must be absolute")
    directory.mkdir(parents=True, exist_ok=True)
    persist_views(engine.project, directory / "display-views")
    memory_dir = directory / "materialized"
    selections = {}
    for layer in engine.ordered_layers():
        if not layer.isValid():
            raise ValueError("Cannot checkpoint invalid layers")
        if isinstance(layer, QgsVectorLayer) and layer.providerType() == "memory":
            memory_dir.mkdir(exist_ok=True)
            path = memory_dir / f"{layer.id()}.gpkg"
            if path.exists():
                raise ValueError("Snapshot cannot overwrite an existing materialization")
            selected = set(layer.selectedFeatureIds())
            subset = layer.subsetString()
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = "features"
            options.fileEncoding = "UTF-8"
            # Avoid conflicts with a user's fid column, including duplicate primary keys.
            options.layerOptions = ["FID=__smart_qgis_storage_id"]
            if layer.fields().indexFromName("__smart_qgis_storage_id") >= 0:
                raise ValueError("Reserved storage ID field conflicts with input attributes")
            # A subset is view state, not permission to discard hidden features.
            # Restore the original view even if materialization raises.
            if subset and not layer.setSubsetString(""):
                raise ValueError("Cannot clear memory filter for full materialization")
            try:
                originals = list(layer.getFeatures())
                result = QgsVectorFileWriter.writeAsVectorFormatV3(
                    layer, str(path), engine.project.transformContext(), options
                )
            finally:
                if subset and not layer.setSubsetString(subset):
                    raise ValueError("Cannot restore memory filter after materialization")
                layer.selectByIds(list(selected))
            if result[0] != QgsVectorFileWriter.NoError:
                raise ValueError(str(result))
            persisted = QgsVectorLayer(str(path), layer.name(), "ogr")
            saved = list(persisted.getFeatures())
            if len(originals) != len(saved):
                raise ValueError("Materialization changed feature count")
            # Verify the mapping before relying on export order for selection restoration.
            selected_ids = []
            names = [field.name() for field in layer.fields()]
            for before, after in zip(originals, saved, strict=True):
                if any(before[name] != after[name] for name in names):
                    raise ValueError("Materialization changed attributes or feature order")
                if before.hasGeometry() != after.hasGeometry() or (
                    before.hasGeometry() and not before.geometry().equals(after.geometry())
                ):
                    raise ValueError("Materialization changed geometry or feature order")
                if before.id() in selected:
                    selected_ids.append(after.id())
            renderer = layer.renderer().clone() if layer.renderer() else None
            labeling = layer.labeling().clone() if layer.labeling() else None
            labels_enabled = layer.labelsEnabled()
            layer.setDataSource(str(path), layer.name(), "ogr")
            if renderer:
                layer.setRenderer(renderer)
            if labeling:
                layer.setLabeling(labeling)
            layer.setLabelsEnabled(labels_enabled)
            if subset and not layer.setSubsetString(subset):
                raise ValueError("Materialized provider cannot preserve vector filter")
            layer.selectByIds(selected_ids)
        if isinstance(layer, QgsVectorLayer):
            selections[layer.id()] = {
                "selected": layer.selectedFeatureIds(),
                "subset": layer.subsetString(),
            }
    project_path = directory / "project.qgz"
    if project_path.exists():
        raise ValueError("Checkpoint project already exists")
    if not engine.project.write(str(project_path)):
        raise ValueError("Failed to write checkpoint project")
    supplemental = {"environment": environment(engine), "vectors": selections}
    (directory / "state.json").write_text(json.dumps(supplemental), encoding="utf-8")
    return {
        "project": str(project_path),
        "supplemental": str(directory / "state.json"),
        "environment": supplemental["environment"],
        "info": engine.info(),
    }


def restore(engine, a):
    state = json.loads(Path(a["supplemental"]).read_text(encoding="utf-8"))
    if state["environment"] != environment(engine):
        raise ValueError("Checkpoint environment does not match this worker")
    engine.project_op({"action": "open", "path": a["project"]})
    for layer_id, saved in state["vectors"].items():
        layer = engine.project.mapLayer(layer_id)
        if not isinstance(layer, QgsVectorLayer):
            raise ValueError("Checkpoint lost a vector layer identity")
        if layer.subsetString() != saved["subset"] and not layer.setSubsetString(saved["subset"]):
            raise ValueError("Cannot restore vector filter")
        layer.selectByIds(saved["selected"])
        if set(layer.selectedFeatureIds()) != set(saved["selected"]):
            raise ValueError("Cannot restore selection")
    return engine.info()


def validate(engine, a):
    """Return measured evidence. Unknown checks are never implicitly passed."""
    check, assets = a["check"], a["assets"]
    kind = check["kind"]
    result = {"id": check["id"], "status": "unverified", "scope": "full", "evidence": {}}
    if kind == "external_review":
        result["evidence"] = {"reason": "External review has not been supplied"}
        return result
    target = assets[check["target"]]
    reference = assets.get(check.get("reference", ""))
    try:
        if kind in {"layout_layers", "legend_consistent", "layout_content"}:
            layout = engine.project.layoutManager().layoutByName(target["layout"])
            if layout is None:
                raise ValueError("Layout not found")
            maps = [item for item in layout.items() if isinstance(item, QgsLayoutItemMap)]
            if kind == "layout_layers":
                actual = [layer.id() for item in maps for layer in item.layers()]
                expected = []
                for key in check["layers"]:
                    asset = assets[key]
                    if asset.get("layer_id"):
                        expected.append(asset["layer_id"])
                        continue
                    path = asset.get("path")
                    matches = [
                        layer.id()
                        for layer in engine.project.mapLayers().values()
                        if path
                        and layer.providerType() != "wms"
                        and Path(layer.source().split("|")[0]).resolve() == Path(path).resolve()
                    ]
                    if len(matches) != 1:
                        raise ValueError(
                            f"Logical asset {key} has {len(matches)} loaded layers; use an unambiguous layer asset"
                        )
                    expected.append(matches[0])
                passed = actual == expected
                result["evidence"] = {"actual": actual, "expected": expected}
            elif kind == "layout_content":
                import spatial_validation

                passed, result["evidence"] = spatial_validation.layout_content(layout, check)
            else:
                import spatial_validation

                passed, result["evidence"] = spatial_validation.legend(engine, layout)
        elif kind == "provenance":
            passed = set(check["inputs"]) <= set(target.get("inputs", []))
            result["evidence"] = {"recorded_inputs": target.get("inputs", [])}
        elif kind == "readable" and check["data_kind"] not in {"vector", "raster"}:
            result["evidence"] = {"reason": "File-format validation runs in the coordinator"}
            return result
        else:
            layer = data_layer(engine, target.get("layer_id") or target["path"])
            if kind == "readable":
                passed = layer.isValid() and (
                    isinstance(layer, QgsVectorLayer)
                    if check["data_kind"] == "vector"
                    else isinstance(layer, QgsRasterLayer)
                )
            elif kind == "crs":
                expected = QgsCoordinateReferenceSystem(check["expected"])
                passed = expected.isValid() and layer.crs() == expected
                result["evidence"] = {"actual": layer.crs().authid()}
            elif kind == "crs_valid":
                passed = layer.crs().isValid()
                result["evidence"] = {"crs": layer.crs().authid()}
            elif kind == "coordinate_units":
                units = layer.crs().mapUnits()
                expected = check["expected"]
                passed = layer.crs().isValid() and (
                    units == QgsUnitTypes.DistanceMeters
                    if expected == "meters"
                    else units == QgsUnitTypes.DistanceDegrees
                    if expected == "degrees"
                    else not layer.crs().isGeographic()
                )
                result["evidence"] = {
                    "map_units": str(units),
                    "geographic": layer.crs().isGeographic(),
                }
            elif kind == "raster_range":
                import spatial_validation

                passed, result["evidence"] = spatial_validation.raster_range(layer, check)
                result["scope"] = check.get("scope", "full")
            elif kind == "fields":
                names = [field.name() for field in layer.fields()]
                passed = set(check["names"]) <= set(names)
                result["evidence"] = {"fields": names}
            elif kind == "geometry_valid":
                invalid, total = 0, 0
                for feature in layer.getFeatures():
                    total += 1
                    if not feature.hasGeometry() or not feature.geometry().isGeosValid():
                        invalid += 1
                passed = invalid == 0
                result["evidence"] = {"features": total, "invalid": invalid}
            elif kind == "nodata":
                provider, band = layer.dataProvider(), check["band"]
                value = (
                    provider.sourceNoDataValue(band)
                    if provider.sourceHasNoDataValue(band)
                    else None
                )
                passed = (
                    (value is not None and math.isnan(value))
                    if check["value"] == "nan"
                    else (value == check["value"])
                )
                result["evidence"] = {
                    "actual": "nan" if value is not None and math.isnan(value) else value
                }
            elif kind in {"spatial_overlap", "raster_grid", "raster_mask", "raster_values"}:
                import spatial_validation

                other = data_layer(engine, reference.get("layer_id") or reference["path"])
                if kind == "spatial_overlap":
                    passed, result["evidence"] = spatial_validation.overlap(engine, layer, other)
                elif kind == "raster_grid":
                    passed, result["evidence"] = spatial_validation.grid(layer, other, check)
                elif kind == "raster_values":
                    passed, result["evidence"] = spatial_validation.raster_values(
                        layer, other, check
                    )
                    result["scope"] = check.get("scope", "sample")
                else:
                    source = assets.get(check.get("source_raster", ""))
                    source_layer = (
                        data_layer(engine, source.get("layer_id") or source["path"])
                        if source
                        else None
                    )
                    passed, result["evidence"] = spatial_validation.raster_mask(
                        engine, layer, other, check, source_layer
                    )
                    result["scope"] = check.get("scope", "sample")
            else:
                result["evidence"] = {"reason": "Validator has not been implemented"}
                return result
        result["status"] = "passed" if passed else "failed"
    except Exception as exc:
        result["status"] = "failed"
        result["evidence"] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


def dispatch(engine, operation, arguments):
    operations = {
        "_environment": lambda a: environment(engine),
        "_validate_crs": lambda a: {
            "valid": QgsCoordinateReferenceSystem(a["value"]).isValid()
        },
        "_inspect": lambda a: inspect(engine, a),
        "_processing_preflight": engine.processing_preflight,
        "_snapshot": lambda a: snapshot(engine, a),
        "_restore": lambda a: restore(engine, a),
        "_validate": lambda a: validate(engine, a),
    }
    if operation not in operations:
        raise ValueError("Unknown recovery operation")
    return operations[operation](arguments)
