"""Independent NumPy Horn slope reference for projected, north-up metre grids.

Run with QGIS Python for GDAL I/O. Never calls GDAL's terrain algorithm.
Scope: degrees, scale=1, no edge interpolation, one elevation band.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from osgeo import gdal, osr


def valid(band, x, y, width, height):
    values = band.ReadAsArray(x, y, width, height).astype('float64')
    mask = np.isfinite(values) & (band.GetMaskBand().ReadAsArray(x, y, width, height) != 0)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        mask &= values != nodata
    return values, mask


def validate(source_path, slope_path, tolerance=1e-5):
    if not np.isfinite(tolerance) or tolerance < 0:
        raise ValueError('Tolerance must be finite and nonnegative')
    source, slope = gdal.Open(str(source_path)), gdal.Open(str(slope_path))
    gt = source.GetGeoTransform()
    size = (source.RasterXSize, source.RasterYSize)
    crs = osr.SpatialReference(wkt=source.GetProjection())
    if (source.RasterCount != 1 or slope.RasterCount != 1 or min(size) < 3
            or size != (slope.RasterXSize, slope.RasterYSize)
            or gt != slope.GetGeoTransform()
            or not crs.IsSame(osr.SpatialReference(wkt=slope.GetProjection()))):
        raise ValueError('Expected single-band rasters on identical grids and CRS')
    if gt[2] or gt[4] or gt[1] <= 0 or gt[5] >= 0 or not crs.IsProjected() or abs(crs.GetLinearUnits()-1) > 1e-12:
        raise ValueError('Reference requires a north-up projected metre grid')
    sb, ob = source.GetRasterBand(1), slope.GetRasterBand(1)
    count = mask_errors = value_errors = expected_valid = 0
    max_error = 0.0
    for y in range(0, size[1], 256):
        height = min(256, size[1]-y)
        # Halo allows each output block to use the same 3x3 stencil.
        y0, y1 = max(0, y-1), min(size[1], y+height+1)
        z, good = valid(sb, 0, y0, size[0], y1-y0)
        pad_top, pad_bottom = int(y == 0), int(y+height == size[1])
        z = np.pad(z, ((pad_top, pad_bottom), (1, 1)), constant_values=0)
        good = np.pad(good, ((pad_top, pad_bottom), (1, 1)), constant_values=False)
        expected_mask = np.ones((height, size[0]), dtype=bool)
        for row in range(3):
            for col in range(3):
                expected_mask &= good[row:row+height, col:col+size[0]]
        dx = ((z[:-2, 2:] + 2*z[1:-1, 2:] + z[2:, 2:])
              - (z[:-2, :-2] + 2*z[1:-1, :-2] + z[2:, :-2])) / (8*gt[1])
        dy = ((z[2:, :-2] + 2*z[2:, 1:-1] + z[2:, 2:])
              - (z[:-2, :-2] + 2*z[:-2, 1:-1] + z[:-2, 2:])) / (8*abs(gt[5]))
        expected = np.degrees(np.arctan(np.hypot(dx, dy)))
        actual, actual_mask = valid(ob, 0, y, size[0], height)
        both = expected_mask & actual_mask
        errors = np.abs(expected[both] - actual[both])
        if errors.size:
            max_error = max(max_error, float(errors.max()))
        mask_errors += int(np.count_nonzero(expected_mask != actual_mask))
        value_errors += int(np.count_nonzero(errors > tolerance))
        expected_valid += int(expected_mask.sum())
        count += height * size[0]
    return {'passed': mask_errors == 0 and value_errors == 0,
            'reference': 'Independent NumPy Horn 3x3 derivatives, atan gradient magnitude',
            'scope': 'All pixels; degrees; scale=1; elevation metres; no edge interpolation',
            'pixels_checked': count, 'expected_valid': expected_valid,
            'mask_mismatches': mask_errors, 'value_mismatches': value_errors,
            'maximum_absolute_error_degrees': max_error, 'absolute_tolerance_degrees': tolerance,
            'dependencies': {'numpy': np.__version__, 'gdal_io': gdal.VersionInfo('--version')},
            'limitations': ['Does not validate the preceding reprojection or vertical datum',
                            'Elevation units and selected method must be established separately']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--slope', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tolerance', type=float, default=1e-5)
    args = parser.parse_args()
    gdal.UseExceptions()
    result = validate(args.source, args.slope, args.tolerance)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
