"""Reproduce the paper through real stdio MCP, without any agent/model dependency."""

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(data: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    transcript = []
    server = StdioServerParameters(command=sys.executable, args=["-m", "smart_qgis.server"])
    async with stdio_client(server) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timedelta(minutes=15)) as session:
            await session.initialize()

            async def call(tool_name, **arguments):
                result = await session.call_tool(tool_name, arguments)
                text = "\n".join(c.text for c in result.content if c.type == "text")
                transcript.append(
                    dict(tool=tool_name, arguments=arguments, result=text, error=result.isError)
                )
                (output / "transcript.json").write_text(
                    json.dumps(transcript, ensure_ascii=False, indent=2)
                )
                if result.isError:
                    raise RuntimeError(text)
                print(tool_name, "OK", flush=True)
                return json.loads(text)

            await call("project", action="create", title="Shaanxi elevation", crs="EPSG:4326")
            boundary = await call("load_data", path=str(data / "ShannXi.shp"), name="Boundary")
            dem = await call("load_data", path=str(data / "DEM.tif"), name="DEM", kind="raster")
            await call("algorithms", action="help", algorithm="gdal:cliprasterbymasklayer")
            result = await call(
                "run_processing",
                algorithm="gdal:cliprasterbymasklayer",
                parameters={
                    "INPUT": dem["id"],
                    "MASK": boundary["id"],
                    "NODATA": 0,
                    "CROP_TO_CUTLINE": True,
                    "KEEP_RESOLUTION": True,
                    "OUTPUT": str(output / "Elevation.tif"),
                },
            )
            elevation = result["loaded_layers"][0]["id"]
            await call("style_raster", layer=elevation, ramp="Viridis")
            await call(
                "style_vector",
                layer=boundary["id"],
                color="transparent",
                outline="black",
                width=0.4,
            )
            await call("layers", action="visibility", layer=dem["id"], visible=False)
            await call(
                "layout",
                name="Elevation",
                title="陕西省海拔高度空间分布图",
                layers=[boundary["id"], elevation],
                extent_layer=boundary["id"],
            )
            await call("export_map", layout="Elevation", path=str(output / "map.png"))
            await call("export_map", layout="Elevation", path=str(output / "map.pdf"))
            await call("layout", action="template", name="Elevation", path=str(output / "map.qpt"))
            await call("project", action="save", path=str(output / "project.qgz"))
            await call("project", action="open", path=str(output / "project.qgz"))
            await call("export_map", layout="Elevation", path=str(output / "reopened.png"))
            print("Saved, reopened and rendered project successfully", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.data.resolve(), args.output.resolve()))
