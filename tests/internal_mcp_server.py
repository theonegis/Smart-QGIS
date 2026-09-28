"""Test-only stdio server exposing coordinator internals for protocol coverage."""

import asyncio

from mcp.server.stdio import stdio_server

from smart_qgis.bridge import QgisBridge
from smart_qgis.coordinator import TaskCoordinator
from smart_qgis.server import make_server


async def serve():
    bridge = TaskCoordinator(QgisBridge())
    server = make_server(bridge, compact_tools=False)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(serve())
