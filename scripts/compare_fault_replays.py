"""Compare discarded completed outputs with committed replays using decoded pixels.

Run with QGIS Python. This measures replay equivalence, not independent GIS truth.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from osgeo import gdal, osr


def compare(first_path, second_path):
    first, second = gdal.Open(first_path), gdal.Open(second_path)
    shape = (first.RasterXSize, first.RasterYSize, first.RasterCount)
    if shape != (second.RasterXSize, second.RasterYSize, second.RasterCount):
        return {'passed': False, 'reason': 'Dimensions differ'}
    if first.GetGeoTransform() != second.GetGeoTransform():
        return {'passed': False, 'reason': 'Affine grids differ'}
    crs_a, crs_b = first.GetProjection(), second.GetProjection()
    if bool(crs_a) != bool(crs_b) or (crs_a and not osr.SpatialReference(wkt=crs_a).IsSame(osr.SpatialReference(wkt=crs_b))):
        return {'passed': False, 'reason': 'CRS differs'}
    mismatches, checked = 0, 0
    for band_number in range(1, first.RasterCount + 1):
        a, b = first.GetRasterBand(band_number), second.GetRasterBand(band_number)
        no_a, no_b = a.GetNoDataValue(), b.GetNoDataValue()
        same_nodata = no_a == no_b or (no_a is not None and no_b is not None and np.isnan(no_a) and np.isnan(no_b))
        if not same_nodata or a.DataType != b.DataType or a.GetColorInterpretation() != b.GetColorInterpretation():
            return {'passed': False, 'reason': 'Band type, color interpretation or NoData differs'}
        for y in range(0, first.RasterYSize, 256):
            for x in range(0, first.RasterXSize, 512):
                width, height = min(512, first.RasterXSize-x), min(256, first.RasterYSize-y)
                left, right = a.ReadAsArray(x,y,width,height), b.ReadAsArray(x,y,width,height)
                equal = (left == right) | (np.isnan(left) & np.isnan(right))
                equal &= a.GetMaskBand().ReadAsArray(x,y,width,height) == b.GetMaskBand().ReadAsArray(x,y,width,height)
                mismatches += int((~equal).sum())
                checked += width * height
    return {'passed': mismatches == 0, 'pixel_bands_checked': checked,
            'mismatches': mismatches, 'numeric_tolerance': 0}


def validate(report):
    results = []
    for fault in report['faults']:
        operation = fault['operation']
        args = fault['resolved_arguments']
        if operation == 'run_processing':
            first = args['parameters']['OUTPUT']
            committed = fault['committed_result']['assets'][fault['step']]['path']
        elif operation == 'export_map':
            first = args['path']
            committed = fault['committed_result']['assets'][fault['step']]['path']
        else:
            continue
        results.append({'step': fault['step'], **compare(first, committed)})
    return {'scope': 'all decoded pixels of replayed raster/PNG outputs',
            'comparison': 'Successful discarded attempt versus committed replay',
            'passed': bool(results) and all(row['passed'] for row in results), 'results': results,
            'limitations': ['Does not establish independent scientific correctness',
                            'Does not compare PDF bytes or visual aesthetics',
                            'Layer/style restoration is checked by task validation and project inspection']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    gdal.UseExceptions()
    result = validate(json.loads(args.report.read_text()))
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
