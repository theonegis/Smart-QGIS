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

RELIABLE_INSTRUCTIONS = """Smart-QGIS is a headless durable GIS service; the client owns reasoning and user dialogue.
Call task_start once with the original goal, absolute inputs and requested deliverables. Input kind may be omitted for inspection. Set overwrite_existing_outputs=true when the user explicitly authorized replacing files at those declared paths; the server also honors an unambiguous original instruction such as “同名文件请直接覆盖”. Otherwise omit it. Keep contract={} except for explicit extra acceptance requirements. Standard workflows and frozen plans return no-argument task_execute; call it without rediscovery or copied internal state.
For general Processing, identify one exact installed algorithm ID, using algorithm_info only when discovery/help is needed. Then call prepare_algorithm once with layer bindings in inputs, destinations in outputs and only known scalar/band/enum/CRS/expression values in parameters. It reads live help, normalizes representation, applies documented defaults and runs native preflight. Missing required semantic values become questions; never guess them.
Use exact identifiers returned by project_info/data_info. Services, styles, project/layer changes and copy-on-write vector edits follow their published schemas; never invent endpoints, credentials, fields, bands or layer names. A basemap never controls thematic extent. Standard maps keep title, legend, scale bar and coordinate annotations unless explicitly omitted; north arrow is opt-in.
Use task_answer only for the user's actual answer and task_update only for actual later instructions. task_diagnose is read-only. After restart/lost attachment use task_resume with the same task ID; use task_restart for a user-authorized failed operation, analysis or map rebuild. After timeout or the correction limit, stop automatic retries and obtain real guidance. Never expose or invent internal steps, versions, keys or tokens.
On OUTPUT_EXISTS ask about that exact file. Prefer task_update.output_conflict action=overwrite with the matching public decision_calls arguments. A clear actual user approval such as “同名文件请直接覆盖” is also safely bound by the server to that one pending path; vague references are rejected. A valid task_update automatically resumes only the failed operation and returns task_execute; never delete the file with another tool. Advertised tools are the complete callable surface: do not search for unadvertised names. Claim success only when task_execute returns COMPLETED and required outputs/checks pass. Do not add unrequested analysis or reprojection merely because a valid CRS lacks an authority ID.
""" + MINIMUM_ACCEPTANCE_POLICY + "\n" + PARAMETER_HELP_POLICY


def compact_published_schema(schema):
    """Remove generated prose that costs context but changes no validation rule."""

    def visit(value, path=()):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key == "title" and (not path or path[-1] != "properties"):
                    continue
                if key == "description" and (
                    not path or (len(path) >= 2 and path[-2] == "$defs")
                ):
                    # Tool descriptions explain the root purpose.  Pydantic
                    # class docstrings repeat that purpose inside $defs; field
                    # descriptions remain intact because they guide arguments.
                    continue
                result[key] = visit(child, (*path, key))
            return result
        if isinstance(value, list):
            return [visit(child, path) for child in value]
        return value

    return visit(schema)


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
    schemas = {
        name: compact_published_schema(tool.args_schema.model_json_schema())
        for name, tool in registry.items()
    }
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
                # Safe public names intentionally hide only lower-level
                # recovery and inspection operations. task_execute is the
                # canonical routed executor throughout reliable mode.
                operation = {
                    "task_resume": "task_recover",
                    "project_info": "project",
                    "data_info": "inspect_data",
                }.get(name, name) if compact else name
                bridge.correction_gate(operation, arguments)
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
            "then call task_execute with no arguments. The service inspects inputs and "
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
