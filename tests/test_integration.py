"""Real PyQGIS integration, synthetic public-domain fixtures only."""

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.integration


async def test_mcp_vector_roundtrip(tmp_path):
    if not Path("/Applications/QGIS.app").exists() and not os.getenv("SMART_QGIS_PYTHON"):
        pytest.skip("Set SMART_QGIS_PYTHON to enable real QGIS tests")
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "smart_qgis.server"], env=os.environ.copy()
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=120)) as session:
            await session.initialize()
            tool_list = await session.list_tools()
            assert len(tool_list.tools) == 15

            async def call(tool, **args):
                response = await session.call_tool(tool, args)
                assert not response.isError, response.content
                return json.loads(response.content[0].text)

            geojson = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"value": i, "category": "A" if i < 3 else "B"},
                        "geometry": {"type": "Point", "coordinates": [100 + i, 30 + i]},
                    }
                    for i in range(1, 6)
                ],
            }
            loaded = await call("vector_data", action="create", name="points", geojson=geojson)
            layer = loaded["id"]
            assert loaded["feature_count"] == 5
            stats = await call("vector_data", action="statistics", layer=layer, field="value")
            assert stats["mean"] == 3 and stats["sum"] == 15
            assert (
                await call("vector_data", action="select", layer=layer, expression='"value" > 3')
            )["selected"] == 2
            await call(
                "vector_data",
                action="export",
                layer=layer,
                selected_only=True,
                path=str(tmp_path / "selected.gpkg"),
            )
            exported = await call("load_data", path=str(tmp_path / "selected.gpkg"))
            assert exported["feature_count"] == 2
            for method in ["quantile", "equal_interval", "jenks"]:
                styled = await call(
                    "style_graduated", layer=layer, field="value", classes=3, method=method
                )
                assert styled["classes"] == 3
            await call(
                "style_vector",
                layer=layer,
                category_field="category",
                categories=[{"value": "A", "color": "red"}, {"value": "B", "color": "blue"}],
                label_field="category",
            )
            await call("style_file", action="save", layer=layer, path=str(tmp_path / "style.qml"))
            await call("style_file", action="load", layer=layer, path=str(tmp_path / "style.qml"))
            sample = await call("features", layer=layer, limit=2, expression='"value" > 2')
            assert len(sample["features"]) == 2
            info = await call("algorithms", action="help", algorithm="native:buffer")
            assert "DISTANCE" in {p["name"] for p in info["parameters"]}
            buffered = await call(
                "run_processing",
                algorithm="native:buffer",
                parameters={
                    "INPUT": layer,
                    "DISTANCE": 0.1,
                    "SEGMENTS": 5,
                    "DISSOLVE": False,
                    "OUTPUT": str(tmp_path / "buffer.gpkg"),
                },
            )
            assert buffered["loaded_layers"][0]["feature_count"] == 5
            bad = await session.call_tool(
                "run_processing", {"algorithm": "native:buffer", "parameters": {"WRONG": 1}}
            )
            assert bad.isError
            await call(
                "layout",
                name="Test",
                title="Synthetic map",
                layers=[exported["id"]],
                extent_layer=exported["id"],
            )
            await call("export_map", layout="Test", path=str(tmp_path / "map.pdf"))
            assert (tmp_path / "map.pdf").stat().st_size > 1000
            refused = await session.call_tool(
                "project", {"action": "save", "path": str(tmp_path / "test.qgz")}
            )
            assert refused.isError
            # Memory layers cannot persist; only file-backed layers are asserted on reopen below.
            await call("layers", action="remove", layer=layer)
            await call("project", action="save", path=str(tmp_path / "test.qgz"), overwrite=True)
            info = await call("project", action="open", path=str(tmp_path / "test.qgz"))
            assert "Test" in info["layouts"]
            assert len(info["layers"]) == 2
            await call("export_map", layout="Test", path=str(tmp_path / "reopened.png"))
            assert (await session.list_resources()).resources
            assert (await session.list_prompts()).prompts
            assert (await session.read_resource("qgis://project")).contents
            assert (
                await session.get_prompt("dem-map", {"data_dir": "/data", "output_dir": "/output"})
            ).messages
            results = await asyncio.gather(call("layers"), call("project"))
            assert len(results[0]["layers"]) == len(results[1]["layers"])


async def test_raster_rendering_processing_and_basemap(tmp_path):
    from smart_qgis.bridge import QgisBridge
    from smart_qgis.runtime import worker_environment
    from smart_qgis.tools import build_tools

    executable, env = worker_environment()
    fixture_code = """
from osgeo import gdal, osr
import numpy as np
import sys
srs=osr.SpatialReference();srs.ImportFromEPSG(32649)
ds=gdal.GetDriverByName('GTiff').Create(sys.argv[1],64,64,3,gdal.GDT_Float32)
ds.SetGeoTransform([400000,30,0,4000000,0,-30]);ds.SetProjection(srs.ExportToWkt())
for band in range(1,4):
 ds.GetRasterBand(band).WriteArray((np.indices((64,64))[0]+np.indices((64,64))[1]*2+band).astype('float32'))
ds=None
"""
    process = await asyncio.create_subprocess_exec(
        executable, "-c", fixture_code, str(tmp_path / "rgb.tif"), env=env
    )
    assert await process.wait() == 0
    bridge = QgisBridge(120)
    tools = {tool.name: tool for tool in build_tools(bridge)}

    async def call(tool, **args):
        return await tools[tool].ainvoke(args)

    try:
        raster = await call("load_data", path=str(tmp_path / "rgb.tif"), kind="raster")
        for mode in ["gray", "rgb", "hillshade"]:
            assert (await call("render_raster", layer=raster["id"], mode=mode))["renderer"]
        styled = await call("style_raster", layer=raster["id"])
        assert styled["minimum"] == 1 and styled["maximum"] == 190
        slope = await call(
            "run_processing",
            algorithm="gdal:slope",
            parameters={
                "INPUT": raster["id"],
                "BAND": 1,
                "SCALE": 1,
                "AS_PERCENT": False,
                "OUTPUT": str(tmp_path / "slope.tif"),
            },
        )
        assert slope["loaded_layers"][0]["width"] == 64
        await call(
            "layout",
            name="Raster",
            layers=[raster["id"]],
            extent_layer=raster["id"],
            crs="EPSG:32649",
        )
        await call("export_map", layout="Raster", path=str(tmp_path / "raster.png"))
        # Creating an XYZ layer is offline; actual provider/network rendering is a separate acceptance test.
        basemap = await call("add_basemap", service="osm")
        assert basemap["provider"] == "wms"
        basemap_google = await call(
            "add_basemap",
            service="google",
            url="https://example.invalid/{z}/{x}/{y}.png",
            attribution="Synthetic URL; no request intended",
        )
        assert basemap_google["provider"] == "wms"
        info = await call("layers")
        assert info["layers"][-1]["id"] == basemap_google["id"]
        with pytest.raises(Exception, match="Band exceeds"):
            await call("render_raster", layer=raster["id"], band=4)
    finally:
        await bridge.close()
