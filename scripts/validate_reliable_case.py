"""Independent sampled reference check, run with the QGIS distribution's Python.

Uses original polygon point containment rather than the runtime's block rasterizer.
This is sampled evidence, not a full boundary-equivalence proof. Keep the input
client summary private; the emitted report contains no source paths or attributes.
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()


def validate(summary_path, sample_axis=101):
    summary = json.loads(summary_path.read_text())
    completed = [task for task in summary["tasks"] if task["status"] == "COMPLETED"]
    if len(completed) != 1:
        raise ValueError("Expected exactly one completed task")
    assets = completed[0]["assets"]
    source = gdal.Open(assets["dem"]["path"])
    output = gdal.Open(assets["elevation"]["path"])
    boundary = ogr.Open(assets["boundary"]["path"])
    layer = boundary.GetLayer(0)
    geometries = [feature.GetGeometryRef().Clone() for feature in layer]
    output_crs = osr.SpatialReference(wkt=output.GetProjection())
    source_crs = osr.SpatialReference(wkt=source.GetProjection())
    assert output_crs.IsSame(source_crs), "Source/output CRS differ"
    output_crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    mask_crs = layer.GetSpatialRef().Clone()
    mask_crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(output_crs, mask_crs)
    gt, source_gt = output.GetGeoTransform(), source.GetGeoTransform()
    assert np.allclose(
        np.array(gt)[[1, 2, 4, 5]], np.array(source_gt)[[1, 2, 4, 5]], rtol=0, atol=1e-12
    ), "Grid vectors changed"
    inverse = gdal.InvGeoTransform(source_gt)
    origin = gdal.ApplyGeoTransform(inverse, gt[0], gt[3])
    assert np.allclose(origin, np.round(origin), rtol=0, atol=1e-7), "Grid origin misaligned"
    band, source_band = output.GetRasterBand(1), source.GetRasterBand(1)
    counts = {
        "tested": 0,
        "inside_valid_matching": 0,
        "outside_nodata": 0,
        "inside_source_nodata": 0,
    }

    def pixel(dataset_band, x, y):
        value = float(dataset_band.ReadAsArray(x, y, 1, 1)[0, 0])
        valid = bool(dataset_band.GetMaskBand().ReadAsArray(x, y, 1, 1)[0, 0])
        nodata = dataset_band.GetNoDataValue()
        valid = valid and math.isfinite(value) and (nodata is None or value != nodata)
        # Some floating-point TIFFs do not expose Alpha through the GDAL mask.
        dataset = dataset_band.GetDataset()
        for index in range(1, dataset.RasterCount + 1):
            alpha = dataset.GetRasterBand(index)
            if alpha.GetColorInterpretation() == gdal.GCI_AlphaBand:
                opacity = float(alpha.ReadAsArray(x, y, 1, 1)[0, 0])
                valid = valid and math.isfinite(opacity) and opacity > 0
        return value, valid

    for y in np.unique(np.linspace(0, output.RasterYSize - 1, sample_axis, dtype=int)):
        for x in np.unique(np.linspace(0, output.RasterXSize - 1, sample_axis, dtype=int)):
            x, y = int(x), int(y)
            px, py = gdal.ApplyGeoTransform(gt, x + 0.5, y + 0.5)
            point = ogr.Geometry(ogr.wkbPoint)
            point.AddPoint(px, py)
            point.Transform(transform)
            inside = any(geometry.Contains(point) for geometry in geometries)
            value, valid = pixel(band, x, y)
            counts["tested"] += 1
            if not inside:
                assert not valid, "Valid pixel outside original boundary"
                counts["outside_nodata"] += 1
                continue
            sx, sy = gdal.ApplyGeoTransform(inverse, px, py)
            expected, source_valid = pixel(source_band, math.floor(sx), math.floor(sy))
            assert valid == source_valid, "Inside-mask validity differs from source"
            if valid:
                assert value == expected, "Elevation changed"
                counts["inside_valid_matching"] += 1
            else:
                counts["inside_source_nodata"] += 1
    assert counts["inside_valid_matching"] > 100 and counts["outside_nodata"] > 100
    for asset in ("project", "png", "pdf"):
        assert Path(assets[asset]["path"]).stat().st_size > 1000
    return {
        "scope": "sample",
        "method": "regular pixel centers; original polygon containment",
        "grid_aligned": True,
        "value_tolerance": 0,
        "nodata": band.GetNoDataValue(),
        "counts": counts,
        "export_files_nonempty": True,
        "limitations": [
            "Not a full pixel scan",
            "No cartographic visual assessment",
            "Does not test omitted cells beyond the output extent",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.summary), indent=2))
