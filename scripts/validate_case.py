"""Validate GIS case artifacts with GDAL (run using the QGIS Python runtime).

Samples pixel centres against the original DEM and polygon mask; never publishes
source attributes or absolute paths. Outputs a small JSON evidence summary.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()


def validate(data, output):
    raster = gdal.Open(str(output / "Elevation.tif"))
    source = gdal.Open(str(data / "DEM.tif"))
    assert raster and source
    band = raster.GetRasterBand(1)
    assert band.GetNoDataValue() == 0
    gt, source_gt = raster.GetGeoTransform(), source.GetGeoTransform()
    boundary = ogr.Open(str(data / "ShannXi.shp"))
    layer = boundary.GetLayer(0)
    target_crs = osr.SpatialReference(wkt=raster.GetProjection())
    target_crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    mask_crs = layer.GetSpatialRef()
    mask_crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(target_crs, mask_crs)
    geometries = [f.GetGeometryRef().Clone() for f in layer]
    valid = outside = 0
    values = []
    for y in np.linspace(0, raster.RasterYSize - 1, 35, dtype=int):
        for x in np.linspace(0, raster.RasterXSize - 1, 35, dtype=int):
            px, py = gdal.ApplyGeoTransform(gt, int(x) + 0.5, int(y) + 0.5)
            point = ogr.Geometry(ogr.wkbPoint)
            point.AddPoint(px, py)
            point.Transform(transform)
            inside = any(geometry.Contains(point) for geometry in geometries)
            value = float(band.ReadAsArray(int(x), int(y), 1, 1)[0, 0])
            if not inside:
                assert value == 0, "Non-NoData pixel outside mask"
                outside += 1
            elif value != 0:
                sx, sy = gdal.ApplyGeoTransform(gdal.InvGeoTransform(source_gt), px, py)
                expected = float(source.GetRasterBand(1).ReadAsArray(int(sx), int(sy), 1, 1)[0, 0])
                assert value == expected, "Clipped elevation differs from source"
                valid += 1
                values.append(value)
    assert valid > 100 and outside > 100
    for name in ["map.png", "map.pdf", "project.qgz"]:
        assert (output / name).is_file() and (output / name).stat().st_size > 1000
    return {
        "dimensions": [raster.RasterXSize, raster.RasterYSize],
        "nodata": 0,
        "valid_samples_matching_source": valid,
        "outside_mask_samples_nodata": outside,
        "sample_minimum": min(values),
        "sample_maximum": max(values),
        "outputs_nonempty": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.data, args.output), indent=2))
