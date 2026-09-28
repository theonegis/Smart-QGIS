"""Measured spatial checks, running with QGIS/GDAL rather than the agent model."""

import math

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsLayerTree,
    QgsLayerTreeModel,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemScaleBar,
    QgsLegendSettings,
    QgsRasterLayer,
    QgsRenderContext,
)
from qgis.PyQt.QtCore import Qt


def footprint(engine, layer, target_crs):
    if isinstance(layer, QgsRasterLayer):
        dataset = gdal.Open(layer.source())
        if dataset is None:
            raise ValueError("Local GDAL raster required for footprint validation")
        from qgis.core import QgsPointXY

        transform = dataset.GetGeoTransform()
        corners = [
            (0, 0),
            (dataset.RasterXSize, 0),
            (dataset.RasterXSize, dataset.RasterYSize),
            (0, dataset.RasterYSize),
            (0, 0),
        ]
        geometry = QgsGeometry.fromPolygonXY(
            [[QgsPointXY(*gdal.ApplyGeoTransform(transform, x, y)) for x, y in corners]]
        )
    else:
        geometries = [
            feature.geometry() for feature in layer.getFeatures() if feature.hasGeometry()
        ]
        if not geometries:
            return QgsGeometry()
        geometry = QgsGeometry.unaryUnion(geometries)
        if geometry.isNull():
            raise ValueError("Could not construct vector footprint")
    if not layer.crs().isValid() or not target_crs.isValid():
        raise ValueError("Valid CRS required for spatial comparison")
    if layer.crs() != target_crs:
        geometry.transform(QgsCoordinateTransform(layer.crs(), target_crs, engine.project))
    return geometry


def overlap(engine, first, second):
    # For rasters this is the grid footprint, not a claim about valid data coverage.
    a = footprint(engine, first, first.crs())
    b = footprint(engine, second, first.crs())
    return not a.isEmpty() and not b.isEmpty() and a.intersects(b), {
        "method": "geometry intersection in target CRS; rasters use affine grid footprint",
    }


def grid(first, second, check):
    a, b = gdal.Open(first.source()), gdal.Open(second.source())
    if a is None or b is None:
        raise ValueError("Grid validation requires GDAL rasters")
    x, y = a.GetGeoTransform(), b.GetGeoTransform()
    tolerance = check["tolerance"]
    inverse = gdal.InvGeoTransform(y)
    if inverse is None:
        raise ValueError("Reference affine transform is singular")
    pixel, line = gdal.ApplyGeoTransform(inverse, x[0], x[3])
    aligned = gdal.ApplyGeoTransform(y, round(pixel), round(line))
    passed = (
        first.crs() == second.crs()
        and all(abs(x[i] - y[i]) <= tolerance for i in (1, 2, 4, 5))
        and abs(x[0] - aligned[0]) <= tolerance
        and abs(x[3] - aligned[1]) <= tolerance
    )
    if check.get("match_extent"):
        passed &= (a.RasterXSize, a.RasterYSize) == (b.RasterXSize, b.RasterYSize)
        passed &= abs(x[0] - y[0]) <= tolerance and abs(x[3] - y[3]) <= tolerance
    return bool(passed), {
        "actual_transform": x,
        "reference_transform": y,
        "origin_in_reference_pixels": [pixel, line],
    }


def samples(dataset, check):
    """Yield bounded windows and a boolean selection. Sample selection is reproducible."""
    width, height = dataset.RasterXSize, dataset.RasterYSize
    if check.get("scope", "sample") == "full":
        # Keep peak temporary arrays bounded, including very wide rasters.
        for y in range(0, height, 256):
            for x in range(0, width, 256):
                yield x, y, min(256, width - x), min(256, height - y), None
    else:
        count = min(check.get("sample_size", 1225), width * height)
        indices = np.random.default_rng(check.get("seed", 0)).choice(
            width * height, count, replace=False
        )
        tiles = {}
        for index in indices:
            row, column = divmod(int(index), width)
            tile = (column // 256 * 256, row // 256 * 256)
            tiles.setdefault(tile, []).append((row - tile[1], column - tile[0]))
        for (x, y), positions in sorted(tiles.items()):
            w, h = min(256, width - x), min(256, height - y)
            selected = np.zeros((h, w), dtype=bool)
            for row, column in positions:
                selected[row, column] = True
            yield x, y, w, h, selected


def data_bands(dataset):
    bands = [i for i in range(1, dataset.RasterCount + 1)
             if dataset.GetRasterBand(i).GetColorInterpretation() != gdal.GCI_AlphaBand]
    if not bands:
        raise ValueError("Raster has no data bands")
    return bands


def paired_data_bands(target, reference):
    first, second = data_bands(target), data_bands(reference)
    if len(first) != len(second):
        raise ValueError("Comparison requires equal data band counts (excluding explicit Alpha bands)")
    return list(zip(first, second, strict=True))


def read_valid(band, x, y, width, height):
    values = band.ReadAsArray(x, y, width, height)
    if values is None:
        raise ValueError("Cannot read raster window")
    valid = np.isfinite(values)
    nodata = band.GetNoDataValue()
    if nodata is not None and not math.isnan(nodata):
        valid &= values != nodata
    mask = band.GetMaskBand().ReadAsArray(x, y, width, height)
    if mask is not None:
        valid &= mask != 0
    # GDAL may report GMF_ALL_VALID for floating-point Gray + Alpha TIFFs.
    # Honor explicit transparency even when GetMaskBand does not expose it.
    dataset = band.GetDataset()
    for index in range(1, dataset.RasterCount + 1):
        alpha = dataset.GetRasterBand(index)
        if alpha.GetColorInterpretation() == gdal.GCI_AlphaBand:
            opacity = alpha.ReadAsArray(x, y, width, height)
            if opacity is None:
                raise ValueError("Cannot read raster Alpha window")
            valid &= np.isfinite(opacity) & (opacity > 0)
    return values, valid


def aligned_offset(first, second):
    inverse = gdal.InvGeoTransform(second.GetGeoTransform())
    if inverse is None:
        raise ValueError("Reference raster affine transform is singular")
    transform = first.GetGeoTransform()
    x, y = gdal.ApplyGeoTransform(inverse, transform[0], transform[3])
    # Dimensionless tolerance is only for floating-point grid indexing, not pixel values.
    if abs(x - round(x)) > 1e-7 or abs(y - round(y)) > 1e-7:
        raise ValueError("Pixel-value comparison requires aligned pixel origins")
    reference = second.GetGeoTransform()
    if not np.allclose(
        np.array(transform)[[1, 2, 4, 5]], np.array(reference)[[1, 2, 4, 5]], rtol=1e-12, atol=0
    ):
        raise ValueError("Pixel-value comparison requires equal affine grid vectors")
    return round(x), round(y)


def reference_window(dataset, band_number, x, y, width, height):
    values = np.zeros((height, width), dtype=np.float64)
    valid = np.zeros((height, width), dtype=bool)
    left, top = max(0, x), max(0, y)
    right, bottom = min(dataset.RasterXSize, x + width), min(dataset.RasterYSize, y + height)
    if right > left and bottom > top:
        data, mask = read_valid(
            dataset.GetRasterBand(band_number), left, top, right - left, bottom - top
        )
        section = np.s_[top - y : bottom - y, left - x : right - x]
        values[section], valid[section] = data, mask
    return values, valid


def raster_values(first, second, check):
    if first.crs() != second.crs():
        raise ValueError("Source pixel equality requires the same CRS")
    target, reference = gdal.Open(first.source()), gdal.Open(second.source())
    band_pairs = paired_data_bands(target, reference)
    dx, dy = aligned_offset(target, reference)
    tested, compared, failures = 0, 0, 0
    for x, y, width, height, selected in samples(target, check):
        for band, reference_band in band_pairs:
            actual, valid = read_valid(target.GetRasterBand(band), x, y, width, height)
            expected, source_valid = reference_window(
                reference, reference_band, x + dx, y + dy, width, height
            )
            mask = np.ones_like(valid) if selected is None else selected
            tested += int(mask.sum())
            compared += int((mask & valid).sum())
            equal = np.isclose(
                actual, expected, atol=check["absolute_tolerance"], rtol=check["relative_tolerance"]
            )
            failures += int((mask & valid & (~source_valid | ~equal)).sum())
    return failures == 0, {
        "tested_pixel_bands": tested,
        "compared_valid_pixel_bands": compared,
        "mismatches": failures,
        "scope": check.get("scope", "sample"),
        "seed": check.get("seed", 0),
        "semantics": "Compare valid output pixels; use raster_mask with source_raster to detect missing valid pixels",
    }


def raster_range(layer, check):
    dataset = gdal.Open(layer.source())
    count, violations = 0, 0
    low, high = math.inf, -math.inf
    for x, y, width, height, selected in samples(dataset, check):
        for band in data_bands(dataset):
            values, valid = read_valid(dataset.GetRasterBand(band), x, y, width, height)
            if selected is not None:
                valid &= selected
            data = values[valid]
            count += data.size
            if data.size:
                low, high = min(low, float(data.min())), max(high, float(data.max()))
                violations += int((data < check["minimum"]).sum())
                if check.get("maximum") is not None:
                    violations += int((data > check["maximum"]).sum())
    return violations == 0 and count >= check.get("minimum_valid_pixels", 1), {
        "valid_pixel_bands": count,
        "out_of_range": violations,
        "minimum": low if count else None,
        "maximum": high if count else None,
    }


def raster_mask(engine, raster, boundary, check, source=None):
    dataset = gdal.Open(raster.source())
    shape = footprint(engine, boundary, raster.crs())
    memory = ogr.GetDriverByName("Memory").CreateDataSource("")
    srs = osr.SpatialReference()
    srs.ImportFromWkt(raster.crs().toWkt())
    vector = memory.CreateLayer("mask", srs=srs, geom_type=ogr.wkbUnknown)
    if not shape.isEmpty():
        feature = ogr.Feature(vector.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkb(bytes(shape.asWkb())))
        if vector.CreateFeature(feature) != 0:
            raise ValueError("Could not prepare mask geometry")
        feature = None
    reference, dx, dy = None, 0, 0
    band_pairs = [(band, None) for band in data_bands(dataset)]
    if source is not None:
        if source.crs() != raster.crs():
            raise ValueError("Mask coverage source must use the output CRS and aligned grid")
        reference = gdal.Open(source.source())
        dx, dy = aligned_offset(dataset, reference)
        band_pairs = paired_data_bands(dataset, reference)

    geometry_model = check.get("geometry_model", "transformed_vertices")
    original_polygon, point_transform = None, None
    geometry_dependencies = {"gdal": gdal.VersionInfo()}
    if geometry_model == "original_crs":
        if check["boundary_rule"] != "pixel_center":
            raise ValueError("original_crs requires pixel_center")
        # Optional runtime dependencies: lack of support fails explicitly, never
        # silently falls back to a different scientific boundary interpretation.
        import shapely
        from pyproj import CRS, Transformer
        from pyproj import __version__ as pyproj_version

        geometry_dependencies.update(shapely=shapely.__version__, pyproj=pyproj_version)

        original_shape = footprint(engine, boundary, boundary.crs())
        original_polygon = shapely.from_wkb(bytes(original_shape.asWkb()))
        if original_polygon.is_empty or not original_polygon.is_valid:
            raise ValueError("Original boundary must be nonempty and valid")
        shapely.prepare(original_polygon)
        point_transform = Transformer.from_crs(
            CRS.from_wkt(raster.crs().toWkt()), CRS.from_wkt(boundary.crs().toWkt()), always_xy=True
        )
    elif geometry_model != "transformed_vertices":
        raise ValueError("Unknown boundary geometry model")

    def burn(grid, x, y, width, height):
        transform = grid.GetGeoTransform()
        if original_polygon is not None:
            columns, rows = np.meshgrid(np.arange(x, x + width) + .5, np.arange(y, y + height) + .5)
            px = transform[0] + columns * transform[1] + rows * transform[2]
            py = transform[3] + columns * transform[4] + rows * transform[5]
            bx, by = point_transform.transform(px, py)
            if not np.all(np.isfinite(bx) & np.isfinite(by)):
                raise ValueError("Nonfinite pixel center coordinates in boundary CRS")
            return shapely.contains_xy(original_polygon, bx, by)
        tile = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
        origin = gdal.ApplyGeoTransform(transform, x, y)
        tile.SetGeoTransform(
            (origin[0], transform[1], transform[2], origin[1], transform[4], transform[5])
        )
        tile.SetProjection(grid.GetProjection())
        options = ["ALL_TOUCHED=TRUE"] if check["boundary_rule"] == "all_touched" else []
        vector.ResetReading()
        if gdal.RasterizeLayer(tile, [1], vector, burn_values=[1], options=options) != 0:
            raise ValueError("Could not rasterize validation mask")
        return tile.ReadAsArray() != 0

    checked, outside, missing, inside = 0, 0, 0, 0
    for x, y, width, height, selected in samples(dataset, check):
        mask = burn(dataset, x, y, width, height)
        chosen = np.ones_like(mask) if selected is None else selected
        inside += int((chosen & mask).sum())
        for band, reference_band in band_pairs:
            _, valid = read_valid(dataset.GetRasterBand(band), x, y, width, height)
            checked += int(chosen.sum())
            outside += int((chosen & valid & ~mask).sum())
            if reference is not None:
                _, expected_valid = reference_window(reference, reference_band, x + dx, y + dy, width, height)
                missing += int((chosen & mask & expected_valid & ~valid).sum())
    omitted, reference_tested = 0, 0
    if reference is not None:
        # Check source cells outside the output extent as well: a truncated crop must not pass.
        for x, y, width, height, selected in samples(reference, check):
            columns, rows = np.meshgrid(np.arange(x, x + width), np.arange(y, y + height))
            beyond_output = (
                (columns < dx)
                | (columns >= dx + dataset.RasterXSize)
                | (rows < dy)
                | (rows >= dy + dataset.RasterYSize)
            )
            if selected is not None:
                beyond_output &= selected
            reference_tested += int(beyond_output.sum())
            if not beyond_output.any():
                continue
            mask = burn(reference, x, y, width, height)
            for band in data_bands(reference):
                _, valid = read_valid(reference.GetRasterBand(band), x, y, width, height)
                omitted += int((beyond_output & mask & valid).sum())
    return outside == 0 and missing == 0 and omitted == 0, {
        "tested_pixel_bands": checked,
        "sampled_inside_mask": inside,
        "valid_pixels_outside_mask": outside,
        "missing_valid_pixels_inside_mask": missing,
        "coverage_checked": reference is not None,
        "omitted_valid_pixels_outside_output_extent": omitted,
        "reference_cells_tested_outside_output": reference_tested,
        "scope": check.get("scope", "sample"),
        "seed": check.get("seed", 0),
        "boundary_rule": check["boundary_rule"],
        "geometry_model": geometry_model,
        "geometry_dependencies": geometry_dependencies,
        "boundary_points": "strict outside" if geometry_model == "original_crs" else "GDAL rasterization rule",
    }


def layout_content(layout, check):
    def exported(item):
        return item.isVisible() and not item.excludeFromExports()

    map_item = layout.itemById(check.get("map_item", "main-map"))
    if not isinstance(map_item, QgsLayoutItemMap) or not exported(map_item):
        return False, {"reason": "Required map is missing, hidden or excluded from export"}
    labels = [item.text() for item in layout.items()
              if isinstance(item, QgsLayoutItemLabel) and exported(item)]
    missing = [text for text in check.get("texts", []) if text not in labels]
    title = layout.itemById("map-title")
    title_ok = (
        isinstance(title, QgsLayoutItemLabel)
        and exported(title)
        and bool(title.text().strip())
    )
    legends = [
        item
        for item in layout.items()
        if isinstance(item, QgsLayoutItemLegend)
        and exported(item)
        and item.linkedMap() == map_item
    ]
    scales = [item for item in layout.items()
              if isinstance(item, QgsLayoutItemScaleBar) and exported(item)
              and item.linkedMap() == map_item
              and item.unitsPerSegment() > 0 and item.numberOfSegments() > 0]
    valid_grids = [
        grid
        for grid in map_item.grids().asList()
        if grid.enabled()
        and grid.annotationEnabled()
        and grid.crs().isValid()
        and grid.intervalX() > 0
        and grid.intervalY() > 0
    ]
    expected = (
        QgsCoordinateReferenceSystem(check["grid_crs"])
        if check.get("grid_crs") is not None
        else None
    )
    grid_ok = bool(valid_grids) if check.get("require_grid") else True
    if expected is not None:
        grid_ok = expected.isValid() and any(grid.crs() == expected for grid in valid_grids)
    passed = (
        not missing
        and (not check.get("require_title") or title_ok)
        and (not check.get("require_legend") or bool(legends))
        and (not check.get("require_scalebar") or bool(scales))
        and grid_ok
    )
    return passed, {
        "missing_texts": missing,
        "title_present": title_ok,
        "linked_visible_legends": len(legends),
        "linked_visible_scalebars": len(scales),
        "annotated_coordinate_grids": len(valid_grids),
        "grid_matches": grid_ok,
        "method": "Title/legend/scale export visibility and annotated coordinate-grid validity",
        "limits": "Does not prove rendered text legibility, absence of overlap, scale calibration or cartographic quality",
    }


def legend(engine, layout):
    legends = [item for item in layout.items() if isinstance(item, QgsLayoutItemLegend)]
    if not legends:
        return False, {"reason": "Layout has no legend"}
    inspected = []
    for item in legends:
        map_item = item.linkedMap()
        if map_item is None:
            return False, {"reason": "Legend is not linked to a map"}
        # A style override can make the map differ from the project's current renderer.
        if map_item.layerStyleOverrides() or item.model().layerStyleOverrides():
            return False, {
                "reason": "Map/legend style overrides require matching renderer validation"
            }
        expected_layers = [layer for layer in map_item.layers() if layer.providerType() != "wms"]
        actual_layers = item.model().rootGroup().findLayers()
        if [node.layerId() for node in actual_layers] != [layer.id() for layer in expected_layers]:
            return False, {"reason": "Legend layer membership/order differs from map"}
        reference_root = QgsLayerTree()
        for layer in expected_layers:
            reference_root.addLayer(layer)
        reference_model = QgsLayerTreeModel(reference_root)
        settings, context = QgsLegendSettings(), QgsRenderContext()
        for actual, expected in zip(actual_layers, reference_root.findLayers(), strict=True):

            def entries(model, layer_node, settings=settings, context=context):
                return [
                    {
                        "label": str(node.data(Qt.ItemDataRole.DisplayRole)),
                        "symbol": node.exportSymbolToJson(settings, context),
                    }
                    for node in model.layerLegendNodes(layer_node)
                ]

            actual_entries = entries(item.model(), actual)
            expected_entries = entries(reference_model, expected)
            if actual.name() != expected.name() or actual_entries != expected_entries:
                return False, {
                    "reason": "Legend labels/symbols differ from the layer renderer",
                    "layer_id": actual.layerId(),
                }
            inspected.append({"layer_id": actual.layerId(), "entries": len(actual_entries)})
    return True, {
        "legends": len(legends),
        "inspected": inspected,
        "method": "ordered layers, labels and QGIS-rendered legend symbol equality",
        "limits": "Does not assess legibility, label collision or aesthetics",
    }
