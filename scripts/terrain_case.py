"""Additional paper workflow: reproject clipped DEM, slope, hillshade and map."""

import argparse
import asyncio
from pathlib import Path

from smart_qgis.bridge import QgisBridge
from smart_qgis.tools import build_tools


async def run(elevation, output):
    output.mkdir(parents=True, exist_ok=True)
    bridge = QgisBridge(600)
    tools = {tool.name: tool for tool in build_tools(bridge)}

    async def call(tool, **args):
        result = await tools[tool].ainvoke(args)
        print(tool, "OK", flush=True)
        return result

    try:
        await call("project_manage", action="create", crs="EPSG:32649", title="Shaanxi terrain")
        await call("algorithm_info", action="help", algorithm="gdal:warpreproject")
        projected = await call(
            "processing_execute",
            algorithm="gdal:warpreproject",
            parameters={
                "INPUT": str(elevation),
                "TARGET_CRS": "EPSG:32649",
                "TARGET_RESOLUTION": 300,
                "RESAMPLING": 1,
                "NODATA": -9999,
                "OUTPUT": str(output / "dem_utm.tif"),
            },
        )
        dem = projected["loaded_layers"][0]["id"]
        await call("render_raster", layer=dem, mode="hillshade")
        await call("algorithm_info", action="help", algorithm="gdal:slope")
        slope = await call(
            "processing_execute",
            algorithm="gdal:slope",
            parameters={
                "INPUT": dem,
                "BAND": 1,
                "SCALE": 1,
                "AS_PERCENT": False,
                "OUTPUT": str(output / "slope.tif"),
            },
        )
        slope_id = slope["loaded_layers"][0]["id"]
        await call("style_raster", layer=slope_id, ramp="Viridis", opacity=0.8)
        await call(
            "layout_manage",
            name="Slope",
            title="陕西省坡度分布图",
            layers=[slope_id, dem],
            extent_layer=dem,
        )
        await call("export_map", layout="Slope", path=str(output / "slope.png"))
        await call("export_map", layout="Slope", path=str(output / "slope.pdf"))
        await call("project_manage", action="save", path=str(output / "terrain.qgz"))
    finally:
        await bridge.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--elevation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.elevation.resolve(), args.output.resolve()))
