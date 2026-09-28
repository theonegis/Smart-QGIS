"""Independent spherical Web Mercator reference for the synthetic point fixture.

Uses GDAL only to read the result; coordinate expectations use the EPSG:3857
spherical formula. This is not a general-purpose projection validator.
"""

import argparse
import json
import math
from pathlib import Path

from osgeo import ogr, osr


def validate(source, result, tolerance=0.001):
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError('Tolerance must be finite and nonnegative')
    features = json.loads(Path(source).read_text())['features']
    expected = {}
    for feature in features:
        key = feature['properties']['value']
        lon, lat = feature['geometry']['coordinates']
        if feature['geometry']['type'] != 'Point' or key in expected or abs(lat) >= 85.05112878:
            raise ValueError('Expected unique value attributes and ordinary WGS84 points')
        expected[key] = (6378137 * math.radians(lon),
                         6378137 * math.log(math.tan(math.pi/4 + math.radians(lat)/2)))
    ds = ogr.Open(str(result))
    if ds is None or ds.GetLayerCount() != 1:
        raise ValueError('Expected one readable vector layer')
    layer = ds.GetLayer(0)
    target = osr.SpatialReference()
    target.ImportFromEPSG(3857)
    crs_matches = bool(layer.GetSpatialRef() and layer.GetSpatialRef().IsSame(target))
    seen, errors, maximum = set(), 0, 0.0
    for feature in layer:
        key = feature.GetField('value')
        geom = feature.GetGeometryRef()
        if key not in expected or key in seen or geom is None or geom.IsEmpty() or ogr.GT_Flatten(geom.GetGeometryType()) != ogr.wkbPoint:
            errors += 1
            continue
        seen.add(key)
        error = math.hypot(geom.GetX()-expected[key][0], geom.GetY()-expected[key][1])
        if not math.isfinite(error):
            errors += 1
            continue
        maximum = max(maximum, error)
        errors += int(error > tolerance)
    missing = len(set(expected)-seen)
    return {'passed': crs_matches and errors == 0 and missing == 0 and bool(expected),
            'reference': 'Independent spherical Web Mercator formula, radius 6378137 metres',
            'crs_matches': crs_matches, 'source_features': len(expected),
            'matched_value_attributes': len(seen), 'missing_features': missing,
            'invalid_or_mismatched_features': errors, 'maximum_position_error_metres': maximum,
            'absolute_tolerance_metres': tolerance,
            'limitations': ['Synthetic WGS84 points with unique value attributes only',
                            'Not a general CRS transformation reference']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.source, args.result)
    args.report.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
