import asyncio

import pytest
from pydantic import ValidationError

from smart_qgis.tools import RasterStyle, build_tools


class RecordingBridge:
    def __init__(self):
        self.calls = []

    async def call(self, operation, arguments):
        self.calls.append((operation, arguments))
        return {"operation": operation}


async def test_langchain_contract_validates_and_dispatches():
    bridge = RecordingBridge()
    tools = {t.name: t for t in build_tools(bridge)}
    assert len(tools) == 15
    await tools["project"].ainvoke({"action": "info"})
    assert bridge.calls[-1][0] == "project"
    with pytest.raises(ValidationError):
        await tools["export_map"].ainvoke({"path": "/tmp/map.png", "dpi": -1})
    assert len(bridge.calls) == 1
    with pytest.raises(ValidationError):
        RasterStyle(layer="r", minimum=float("nan"))


async def test_tools_do_not_capture_last_operation():
    bridge = RecordingBridge()
    tools = {t.name: t for t in build_tools(bridge)}
    await asyncio.gather(tools["layers"].ainvoke({}), tools["algorithms"].ainvoke({}))
    assert {name for name, _ in bridge.calls} == {"layers", "algorithms"}
