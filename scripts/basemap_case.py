"""Render a small real tile-service map for manual visual/network acceptance."""

import argparse
import asyncio
from pathlib import Path

from smart_qgis.bridge import QgisBridge
from smart_qgis.tools import build_tools


async def run(service, output):
    output.mkdir(parents=True, exist_ok=True)
    bridge = QgisBridge(120)
    tools = {tool.name: tool for tool in build_tools(bridge)}

    async def call(tool, **args):
        return await tools[tool].ainvoke(args)

    try:
        layer = await call("add_basemap", service=service)
        await call(
            "layout",
            name="Basemap",
            title=f"{service.upper()} · Xi’an",
            layers=[layer["id"]],
            crs="EPSG:4326",
            extent=[108.93, 34.24, 108.96, 34.27],
            legend=False,
            scalebar=False,
            grid=False,
            width_mm=140,
            height_mm=140,
        )
        print(
            await call("export_map", layout="Basemap", path=str(output / f"{service}.png"), dpi=100)
        )
    finally:
        await bridge.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", choices=["osm", "google"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.service, args.output.resolve()))
