"""Stdio MCP server; agent harnesses own reasoning, credentials and conversation."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

import jsonschema
from langsmith.run_helpers import tracing_context
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)
from pydantic import ValidationError

from .bridge import QgisBridge
from .contract_policy import MINIMUM_ACCEPTANCE_POLICY, PARAMETER_HELP_POLICY
from .coordinator import TaskCoordinator
from .task_store import TaskError
from .task_tools import ARGUMENT_RULE_MESSAGES
from .tools import build_tools

LEGACY_INSTRUCTIONS = """Smart-QGIS legacy is an unjournaled direct GIS-tool baseline for evaluation.
Use absolute paths and returned layer IDs. Inspect algorithm help before processing.
Persist processing outputs and save the project to retain state after disconnecting.
Create/open replaces current project state. Mutating calls are serialized, not transactional.
Only claim success after tools report success. No task contracts or recovery are provided.
Reasoning and model selection belong to the calling agent, not this server."""

RELIABLE_INSTRUCTIONS = """Smart-QGIS is a headless durable GIS execution service. The host owns all reasoning.
For a new request call task_start once with the original goal, absolute-path inputs and declared deliverables. Input kind is optional: the service inspects every vector/raster input and determines its actual kind before locking the task contract. Keep contract={} unless the user explicitly requested extra acceptance checks.
For a standard map/layout/export/editable-project request, task_start returns task_execute_next. Call it with no arguments; the service owns the current action handle, workflow selection, approved parameters, execution, final validation and completion. Do not rediscover another tool or repeat approved arguments.
Standard maps contain a title, legend, scale bar and coordinate annotations unless the user's explicit contract omits an element. The workflow retains strict per-step validation and a checkpoint after every internal step.
For Processing, determine the exact installed algorithm ID (use algorithm_info list only for discovery, and help only when parameter semantics or expression syntax must be understood), then call prepare_algorithm once. It always reads live QGIS parameter help, mechanically corrects unambiguous parameter-name case and JSON type/enum representations before the step contract, binds declared assets and outputs, applies documented defaults, and returns typed questions only for unresolved required values. Put layer/source bindings in inputs, destinations in outputs, and known bands/numbers/enums/CRS/expressions in parameters. Omit unknown required values; never guess them.
For a pre-decomposed controlled workflow, task_start may include a frozen plan of exact algorithm IDs, logical bindings and known parameter values. Call task_execute_next with no arguments; the service validates and executes the plan step by step. A missing required value returns a structured question instead of being guessed.
After an MCP restart, call task_recover with the existing task_id before any other task mutation. Then call task_execute_next with no arguments when an action is pending. task_diagnose is read-only diagnosis after reconnect, a lost response or an error; it does not re-execute. After any thinking or tool timeout, stop and ask the user for guidance; for a tool timeout, use task_diagnose first, record the real guidance with task_record_guidance or task_answer, then call task_recover. Use task_answer only after presenting a required question to the user and receiving the actual answer. When correction or semantic repair reaches the configured limit, ask the user and record their actual guidance with task_record_guidance before continuing the same task.
Never invent task IDs, versions, idempotency keys or parameter values. Machine continuation and action tokens are owned by the service and are intentionally absent from the compact MCP schema. Missing required choices with no documented or data-derived default must be asked, not guessed.
The MCP tools advertised in this session are the complete callable surface. Never search for, infer, or invoke an unadvertised tool name; when a required host capability is absent, state the exact user question and stop the turn.
Reliable mode intentionally hides direct mutation and low-level lifecycle tools. Internally, every operation still passes the same task contract, parameter preflight, basic output checks, durable journal and checkpoint path.
If a Processing step fails before commit, use prepare_algorithm with repairs_step naming the failed step. If a committed result is wrong or unusable, call task_invalidate on its producer first, then prepare_algorithm with repairs_step and the original logical output ID. Do not overwrite a still-committed asset.
Only claim completion when task_execute_next returns COMPLETED; missing or unverified required checks block completion.
Do not add analysis or reprojection steps the user did not request merely because a CRS has no authority ID; a passed crs_valid check may represent a valid custom CRS, and QGIS layouts transform valid layers on the fly.
""" + MINIMUM_ACCEPTANCE_POLICY + "\n" + PARAMETER_HELP_POLICY


def make_server(bridge, *, compact_tools=None):
    reliable = isinstance(bridge, TaskCoordinator)
    server = Server("smart-qgis", version="2.0.0", instructions=(
        RELIABLE_INSTRUCTIONS if reliable else LEGACY_INSTRUCTIONS
    ))
    compact = reliable if compact_tools is None else compact_tools
    registry = {
        tool.name: tool
        for tool in build_tools(bridge, compact=compact)
    }
    schemas = {name: tool.args_schema.model_json_schema() for name, tool in registry.items()}
    if compact:
        for schema in schemas.values():
            schema.get("properties", {}).pop("continuation_token", None)
            if "required" in schema:
                schema["required"] = [
                    name for name in schema["required"] if name != "continuation_token"
                ]
    validators = {name: jsonschema.validators.validator_for(schema)(schema)
                  for name, schema in schemas.items()}

    def error_result(error):
        payload = error.payload
        if reliable and compact:
            payload = bridge.compact_response(payload)
        return CallToolResult(isError=True, content=[
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        ])

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name=t.name,
                description=t.description,
                inputSchema=schemas[t.name],
            )
            for t in registry.values()
        ]

    # Keep the same published JSON Schema validation, but own its error envelope
    # so SDK prose does not replace stable codes or echo submitted values.
    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        if name not in registry:
            return error_result(TaskError("UNKNOWN_TOOL", "Choose a tool returned by tools/list",
                                          phase="arguments", next_action="tools/list"))
        try:
            if reliable:
                bridge.correction_gate(name, arguments)
            validators[name].validate(arguments)
            # Do not let ambient LangChain tracing export paths, data or user goals.
            # Reliable mode has a separate explicit, allowlisted event exporter.
            with tracing_context(enabled=False):
                result = await registry[name].ainvoke(arguments)
        except jsonschema.ValidationError as exc:
            evidence = {"field": list(exc.absolute_path), "rule": exc.validator}
            if exc.validator == "required":
                evidence["required_fields"] = exc.schema.get("required", [])
            elif exc.validator == "additionalProperties":
                evidence["allowed_fields"] = sorted(exc.schema.get("properties", {}))
            elif exc.validator == "enum":
                evidence["allowed_values"] = exc.schema["enum"]
            elif exc.validator == "type":
                evidence["expected_type"] = exc.schema["type"]
                if exc.schema["type"] == "object":
                    evidence["required_fields"] = exc.schema.get("required", [])
                    evidence["allowed_fields"] = sorted(exc.schema.get("properties", {}))
            error = TaskError(
                "INVALID_ARGUMENTS", "Tool arguments do not match the published schema",
                phase="arguments", evidence=evidence,
                next_action="Read this tool's schema from tools/list; correct the indicated fields before retrying",
            )
            if reliable:
                bridge.record_correction_failure(name, error, arguments)
            return error_result(error)
        except ValidationError as exc:
            error = TaskError(
                "INVALID_ARGUMENTS", "Tool argument constraints are not satisfied",
                phase="arguments", evidence={"errors": [
                    {"field": list(error["loc"]), "rule": error["type"],
                     **({"message": ARGUMENT_RULE_MESSAGES[error["type"]]}
                        if error["type"] in ARGUMENT_RULE_MESSAGES else {})}
                    for error in exc.errors(include_input=False, include_url=False)[:12]
                ], "total_errors": exc.error_count()},
                next_action="Check the tool's field requirements and mutually exclusive options",
            )
            if reliable:
                bridge.record_correction_failure(name, error, arguments)
            return error_result(error)
        except TaskError as exc:
            return error_result(exc)
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
                    PromptArgument(name="output_dir", required=not reliable),
                ],
            )
        ]

    @server.get_prompt()
    async def get_prompt(name, arguments):
        if name != "dem-map":
            raise ValueError("Unknown prompt")
        data, output = arguments["data_dir"], arguments.get("output_dir", "")
        if reliable:
            text = (
            f"Create an elevation map using {data}/ShannXi.shp and {data}/DEM.tif. "
            "Deliver a boundary-clipped DEM preserving the source grid and valid elevations, "
            "an editable QGIS project, and PNG/PDF maps titled 陕西省海拔高度空间分布图 "
            "with an elevation legend, scale bar and coordinate graticule. "
            "Call task_start with these inputs and deliverables, keep the contract minimal, "
            "then call task_execute_next with no arguments. The service inspects inputs and "
            "owns the standard workflow. Report the completed task ID and artifacts."
            "\n" + MINIMUM_ACCEPTANCE_POLICY
            + "\n" + PARAMETER_HELP_POLICY
            )
        else:
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


async def serve(timeout=900, execution_mode="reliable", correction_limit=3,
                terminal_retention_days=10, failed_retention_days=60):
    bridge = QgisBridge(timeout)
    if execution_mode == "reliable":
        bridge = TaskCoordinator(
            bridge, correction_limit=correction_limit,
            terminal_retention_days=terminal_retention_days,
            failed_retention_days=failed_retention_days,
        )
    server = make_server(bridge)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await bridge.close()


def main():
    parser = argparse.ArgumentParser(description="Smart-QGIS 2.0 headless MCP (stdio)")
    parser.add_argument(
        "--execution-mode", choices=["reliable", "legacy"], default="reliable",
        help="Reliable task interface (default) or unjournaled legacy research baseline",
    )
    parser.add_argument(
        "--timeout", type=float, default=900, help="Worker operation timeout in seconds (default: 900)"
    )
    parser.add_argument(
        "--correction-limit", type=int, default=3,
        help="Rejected preparation/argument calls before asking the user (default: 3)",
    )
    parser.add_argument(
        "--retention-days", type=int, default=10,
        help="Days to retain completed or cancelled task directories; 0 disables cleanup (default: 10)",
    )
    parser.add_argument(
        "--failed-retention-days", type=int, default=60,
        help="Days to retain blocked task directories; 0 disables cleanup (default: 60)",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if args.correction_limit <= 0:
        parser.error("correction-limit must be positive")
    if args.retention_days < 0 or args.failed_retention_days < 0:
        parser.error("retention days must be nonnegative")
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(serve(args.timeout, args.execution_mode, args.correction_limit,
                      args.retention_days, args.failed_retention_days))


if __name__ == "__main__":
    main()
