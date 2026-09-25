"""Stdio MCP server; agent harnesses own reasoning, credentials and conversation."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)

from .bridge import QgisBridge
from .tools import build_tools

INSTRUCTIONS = """Smart-QGIS runs GIS operations without QGIS Desktop. This process owns one project.
Use absolute paths and returned layer IDs. Inspect algorithm help before processing.
Persist processing outputs and save the project to retain state after disconnecting.
Create/open replaces current project state. Mutating calls are serialized, not transactional.
For a DEM map: load boundary and DEM, clip by mask with a declared NoData value,
style the clipped DEM and a transparent boundary, create a layout using extent_layer,
export PNG/PDF and save QGZ. Only claim success after tools report success.
Reasoning and model selection belong to the calling agent, not this server."""


def make_server(bridge):
    server = Server("smart-qgis", version="2.0.0", instructions=INSTRUCTIONS)
    registry = {tool.name: tool for tool in build_tools(bridge)}

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.args_schema.model_json_schema(),
            )
            for t in registry.values()
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name not in registry:
            raise ValueError(f"Unknown tool: {name}")
        result = await registry[name].ainvoke(arguments)
        return [
            TextContent(type="text", text=json.dumps(result, ensure_ascii=False, allow_nan=False))
        ]

    @server.list_resources()
    async def list_resources():
        return [Resource(uri="qgis://project", name="Current project", mimeType="application/json")]

    @server.read_resource()
    async def read_resource(uri):
        if str(uri) != "qgis://project":
            raise ValueError("Unknown resource")
        return json.dumps(await bridge.call("project", {"action": "info"}), ensure_ascii=False)

    @server.list_prompts()
    async def list_prompts():
        return [
            Prompt(
                name="dem-map",
                description="Plan the paper's DEM mapping workflow",
                arguments=[
                    PromptArgument(name="data_dir", required=True),
                    PromptArgument(name="output_dir", required=True),
                ],
            )
        ]

    @server.get_prompt()
    async def get_prompt(name, arguments):
        if name != "dem-map":
            raise ValueError("Unknown prompt")
        data, output = arguments["data_dir"], arguments["output_dir"]
        text = (
            f"Load {data}/ShannXi.shp and {data}/DEM.tif. Inspect gdal:cliprasterbymasklayer "
            f"then clip DEM by boundary with NODATA=0 to {output}/Elevation.tif. "
            "Apply Viridis to Elevation, style boundary transparent with black 0.4 mm outline. "
            "Create an A4 map titled 陕西省海拔高度空间分布图, extent from boundary, "
            f"legend, scale bar and graticule. Export {output}/map.png and {output}/map.pdf "
            f"and save {output}/project.qgz. Report actual outputs and any errors."
        )
        return GetPromptResult(
            messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))]
        )

    return server


async def serve(timeout=600):
    bridge = QgisBridge(timeout)
    server = make_server(bridge)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await bridge.close()


def main():
    parser = argparse.ArgumentParser(description="Smart-QGIS 2.0 headless MCP (stdio)")
    parser.add_argument(
        "--timeout", type=float, default=600, help="Worker operation timeout in seconds"
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(serve(args.timeout))


if __name__ == "__main__":
    main()
