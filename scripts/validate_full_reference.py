"""Independent all-cell DEM reference using original-CRS point containment.

Run with QGIS Python (GDAL, Shapely 2 and PyProj). No runtime validators are reused.
Reports aggregate errors rather than stopping at the first mismatching cell.
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import shapely
from osgeo import gdal, ogr
from pyproj import CRS, Transformer
from shapely.ops import transform as transform_geometry


def window(dataset, x, y, width, height):
    band = dataset.GetRasterBand(1)
    values = band.ReadAsArray(x, y, width, height)
    valid = np.isfinite(values) & (band.GetMaskBand().ReadAsArray(x, y, width, height) != 0)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        valid &= values != nodata
    for i in range(1, dataset.RasterCount + 1):
        alpha = dataset.GetRasterBand(i)
        if alpha.GetColorInterpretation() == gdal.GCI_AlphaBand:
            opacity = alpha.ReadAsArray(x, y, width, height)
            valid &= np.isfinite(opacity) & (opacity > 0)
    return values, valid


def validate(summary_path, block_size=512, geometry_model="original_crs", *, candidate=False, diagnostic_limit=10000, boundary_rule="pixel_center"):
    if diagnostic_limit < 0:
        raise ValueError("diagnostic_limit must be nonnegative")
    if geometry_model not in {"original_crs", "transformed_vertices"}:
        raise ValueError("Unknown geometry model")
    if boundary_rule not in {"pixel_center", "all_touched"}:
        raise ValueError("Unknown boundary rule")
    if boundary_rule == "all_touched" and geometry_model != "transformed_vertices":
        raise ValueError("all_touched requires transformed_vertices")
    started = time.monotonic()
    document = json.loads(Path(summary_path).read_text())
    if candidate:
        if document.get('status') not in {'UNCOMMITTED', 'COMMITTED_STEP'}:
            raise ValueError('Artifact manifest must declare UNCOMMITTED or COMMITTED_STEP status')
        assets = document['assets']
    else:
        completed = [task for task in document['tasks'] if task['status'] == 'COMPLETED']
        if len(completed) != 1:
            raise ValueError('Exactly one completed task is required')
        assets = completed[0]['assets']
    source, output = [gdal.Open(assets[key]['path']) for key in ('dem', 'elevation')]
    boundary = ogr.Open(assets['boundary']['path'])
    layer = boundary.GetLayer(0)
    polygon = shapely.union_all([
        shapely.from_wkb(bytes(feature.GetGeometryRef().ExportToWkb())) for feature in layer
    ])
    if polygon.is_empty or not polygon.is_valid:
        raise ValueError('Reference requires a valid nonempty original boundary')
    shapely.prepare(polygon)
    source_crs, output_crs = [CRS.from_wkt(dataset.GetProjection()) for dataset in (source, output)]
    if source_crs != output_crs:
        raise ValueError('Reference requires equal source/output CRS')
    transform = Transformer.from_crs(source_crs, CRS.from_wkt(layer.GetSpatialRef().ExportToWkt()), always_xy=True)
    forward = Transformer.from_crs(CRS.from_wkt(layer.GetSpatialRef().ExportToWkt()), source_crs, always_xy=True)
    forward_polygon = transform_geometry(forward.transform, polygon)
    shapely.prepare(forward_polygon)
    boundary_line = polygon.boundary
    discrepancy_diagnostics = {"mask_disagreements": 0, "forward_vertex_polygon_agrees_with_output": 0,
                               "comparison_rule": "pixel_center_strict_interior",
                               "maximum_distance_to_original_boundary": 0.0,
                               "diagnostic_points_tested": 0, "diagnostic_limit": diagnostic_limit,
                               "selection": "First mismatching points in block scan order; correctness counts remain full",
                               "distance_units": CRS.from_wkt(layer.GetSpatialRef().ExportToWkt()).axis_info[0].unit_name}
    gt, out_gt = source.GetGeoTransform(), output.GetGeoTransform()
    if boundary_rule == "all_touched" and not (gt[1] > 0 and gt[5] < 0 and gt[2] == gt[4] == 0):
        raise ValueError("all_touched reference requires a north-up grid")
    if not np.allclose(np.array(gt)[[1, 2, 4, 5]], np.array(out_gt)[[1, 2, 4, 5]], rtol=0, atol=1e-12):
        raise ValueError('Grid vectors differ')
    dx, dy = gdal.ApplyGeoTransform(gdal.InvGeoTransform(gt), out_gt[0], out_gt[3])
    if not np.allclose([dx, dy], np.round([dx, dy]), rtol=0, atol=1e-7):
        raise ValueError('Output origin is not source-aligned')
    dx, dy = round(dx), round(dy)
    if dx < 0 or dy < 0 or dx + output.RasterXSize > source.RasterXSize or dy + output.RasterYSize > source.RasterYSize:
        raise ValueError('Reference requires output extent within source extent')
    for dataset in (source, output):
        data_bands = [i for i in range(1, dataset.RasterCount + 1)
                      if dataset.GetRasterBand(i).GetColorInterpretation() != gdal.GCI_AlphaBand]
        if data_bands != [1]:
            raise ValueError('This DEM reference requires exactly one data band at index 1')
    counts = dict.fromkeys(('tested_source_cells', 'inside_valid_matching', 'outside_nodata',
                           'outside_valid', 'inside_missing', 'inside_unexpected_valid',
                           'value_mismatches', 'nonfinite_coordinates'), 0)
    for y in range(0, source.RasterYSize, block_size):
        for x in range(0, source.RasterXSize, block_size):
            width, height = min(block_size, source.RasterXSize - x), min(block_size, source.RasterYSize - y)
            cols, rows = np.meshgrid(np.arange(x, x + width) + .5, np.arange(y, y + height) + .5)
            px, py = gt[0] + cols * gt[1] + rows * gt[2], gt[3] + cols * gt[4] + rows * gt[5]
            bx, by = transform.transform(px, py)
            finite = np.isfinite(bx) & np.isfinite(by)
            inside = (
                shapely.contains_xy(polygon, bx, by) & finite
                if geometry_model == "original_crs"
                else shapely.contains_xy(forward_polygon, px, py)
            )
            if boundary_rule == "all_touched":
                # Independent of GDAL rasterization; only boundary tiles need boxes.
                tile = shapely.box(px.min() - gt[1]/2, py.min() + gt[5]/2,
                                   px.max() + gt[1]/2, py.max() - gt[5]/2)
                if shapely.covers(forward_polygon, tile):
                    inside[:] = True
                elif shapely.intersects(forward_polygon, tile):
                    positions = np.flatnonzero(~inside)
                    boxes = shapely.box(px.flat[positions] - gt[1]/2, py.flat[positions] + gt[5]/2,
                                        px.flat[positions] + gt[1]/2, py.flat[positions] - gt[5]/2)
                    touched = shapely.intersects(forward_polygon, boxes)
                    inside.flat[positions[touched]] = shapely.area(
                        shapely.intersection(forward_polygon, boxes[touched])
                    ) > 0
            expected, source_valid = window(source, x, y, width, height)
            actual = np.zeros(expected.shape, dtype=expected.dtype)
            valid = np.zeros(expected.shape, dtype=bool)
            left, top = max(x, dx), max(y, dy)
            right, bottom = min(x + width, dx + output.RasterXSize), min(y + height, dy + output.RasterYSize)
            if right > left and bottom > top:
                values, mask = window(output, left - dx, top - dy, right - left, bottom - top)
                section = np.s_[top-y:bottom-y, left-x:right-x]
                actual[section], valid[section] = values, mask
            wanted = inside & source_valid
            disagreement = (wanted != valid) & finite
            if disagreement.any():
                n = int(disagreement.sum())
                discrepancy_diagnostics["mask_disagreements"] += n
                remaining = diagnostic_limit - discrepancy_diagnostics["diagnostic_points_tested"]
                positions = np.flatnonzero(disagreement)[:max(0, remaining)]
                discrepancy_diagnostics["diagnostic_points_tested"] += len(positions)
                forward_inside = shapely.contains_xy(forward_polygon, px.flat[positions], py.flat[positions])
                discrepancy_diagnostics["forward_vertex_polygon_agrees_with_output"] += int(
                    ((forward_inside & source_valid.flat[positions]) == valid.flat[positions]).sum()
                )
                if len(positions):
                    distances = shapely.distance(boundary_line, shapely.points(bx.flat[positions], by.flat[positions]))
                    discrepancy_diagnostics["maximum_distance_to_original_boundary"] = max(
                        discrepancy_diagnostics["maximum_distance_to_original_boundary"], float(distances.max())
                    )
            equal = actual == expected
            counts['tested_source_cells'] += width * height
            counts['inside_valid_matching'] += int((wanted & valid & equal).sum())
            counts['outside_nodata'] += int((~inside & ~valid).sum())
            counts['outside_valid'] += int((~inside & valid).sum())
            counts['inside_missing'] += int((wanted & ~valid).sum())
            counts['inside_unexpected_valid'] += int((inside & ~source_valid & valid).sum())
            counts['value_mismatches'] += int((wanted & valid & ~equal).sum())
            counts['nonfinite_coordinates'] += int((~finite).sum())
    errors = sum(counts[key] for key in ('outside_valid', 'inside_missing', 'inside_unexpected_valid',
                                       'value_mismatches', 'nonfinite_coordinates'))
    discrepancy_diagnostics['scope'] = (
        'full' if discrepancy_diagnostics['diagnostic_points_tested'] == discrepancy_diagnostics['mask_disagreements']
        else 'sample'
    )
    if not discrepancy_diagnostics['diagnostic_points_tested']:
        discrepancy_diagnostics['maximum_distance_to_original_boundary'] = None
    return {
        'input_kind': (('committed_step' if document['status'] == 'COMMITTED_STEP' else
                        'uncommitted_candidate') if candidate else 'completed_task'),
        'scope': 'full', 'passed': errors == 0 and counts['inside_valid_matching'] > 0,
        'geometry_model': geometry_model,
        'method': ('Every source pixel center transformed into original boundary CRS; Shapely point containment'
                   if geometry_model == 'original_crs' else
                   'Transform original polygon vertices to source CRS; Shapely positive-area pixel-polygon intersection'
                   if boundary_rule == 'all_touched' else
                   'Transform original polygon vertices to source CRS; Shapely containment of every source pixel center'),
        'boundary_rule': 'positive_area_overlap' if boundary_rule == 'all_touched' else 'pixel_center_strict_interior',
        'value_tolerance': 0,
        'includes_omitted_output_extent': True, 'counts': counts,
        'duration_seconds': round(time.monotonic() - started, 3),
        'discrepancy_diagnostics': discrepancy_diagnostics,
        'limitations': ['DEM with one data band, aligned subset grid', 'No cartographic assessment',
                        'Zero-area edge/vertex contact is outside; GDAL degeneracies may differ'
                        if boundary_rule == 'all_touched' else
                        'Exact boundary points count as outside; no implicit edge tolerance'],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--summary', type=Path)
    inputs.add_argument('--candidate', type=Path, help='UNCOMMITTED or COMMITTED_STEP artifact manifest; never changes task state or certifies task completion')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--geometry-model', choices=['original_crs', 'transformed_vertices'], default='original_crs')
    parser.add_argument('--diagnostic-limit', type=int, default=10000)
    parser.add_argument('--boundary-rule', choices=['pixel_center', 'all_touched'], default='pixel_center')
    args = parser.parse_args()
    gdal.UseExceptions()
    result = validate(args.candidate or args.summary, geometry_model=args.geometry_model,
                      candidate=args.candidate is not None, diagnostic_limit=args.diagnostic_limit,
                      boundary_rule=args.boundary_rule)
    args.report.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
