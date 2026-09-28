"""Run the same natural-language acceptance case in an existing agent harness.

Generated configs, prompts and logs contain local paths; keep --output private.
No credentials are copied and no global agent configuration is changed.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from smart_qgis.contract_policy import MINIMUM_ACCEPTANCE_POLICY, PARAMETER_HELP_POLICY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("client", choices=["codex", "hermes"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument('--prompt-file', type=Path, help='Private goal prompt for a separate execution benchmark; defaults to the Shaanxi case')
    parser.add_argument('--exact-prompt', action='store_true',
                        help='Use --prompt-file verbatim without appending acceptance policies')
    parser.add_argument(
        "--tool-discovery", choices=["default", "deferred", "direct"], default="default",
        help="Hermes experiment only: default harness behavior, deferred discovery, or eager direct tool schemas",
    )
    parser.add_argument(
        "--resume-task",
        help="Resume this existing reliable task; forbids replacing it with a new task",
    )
    parser.add_argument(
        "--state-dir", type=Path, help="Existing reliable state root for a resume test"
    )
    parser.add_argument(
        "--timeout", type=int, default=2400, help="Client wall-clock budget in seconds"
    )
    parser.add_argument(
        "--context-length",
        type=int,
        default=65536,
        help="Hermes context budget; match the model server's actual window",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high"],
        help="Optional Hermes reasoning setting for controlled comparisons",
    )
    parser.add_argument("--execution-mode", choices=["reliable", "legacy"], default="reliable")
    args = parser.parse_args()
    if args.timeout <= 0 or args.context_length <= 0:
        parser.error("Timeout and context length must be positive")
    if args.exact_prompt and args.prompt_file is None:
        parser.error("--exact-prompt requires --prompt-file")
    if args.client != "hermes" and args.tool_discovery != "default":
        parser.error("--tool-discovery applies only to Hermes")
    if args.resume_task and (args.execution_mode != "reliable" or args.state_dir is None):
        parser.error("--resume-task requires reliable mode and --state-dir")
    output, data = args.output.resolve(), args.data.resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "server-snapshot"
    if snapshot.exists():
        parser.error("Use a new output directory; an experiment snapshot already exists")
    executable = shutil.which(args.client)
    if not executable:
        parser.error(f"{args.client} is not installed")
    prompt = f"""Use ONLY smart_qgis MCP tools to complete this real GIS acceptance test. Do not write code or use terminal tools.
1. Create a project in EPSG:4326. Load vector {data / "ShannXi.shp"} as Boundary and raster {data / "DEM.tif"} as DEM.
2. Inspect help for gdal:cliprasterbymasklayer, then clip DEM by Boundary, NODATA=0, CROP_TO_CUTLINE=true, KEEP_RESOLUTION=true, OUTPUT={output / "Elevation.tif"}. Load output.
3. Style Elevation using Viridis. Style Boundary with transparent fill and black 0.4 mm outline. Hide original DEM.
4. Create layout Elevation with layers [Boundary, Elevation] in that top-to-bottom order, extent_layer=Boundary, title 陕西省海拔高度空间分布图, legend, scalebar and grid all enabled.
5. Export layout to {output / "map.png"} and {output / "map.pdf"}, then save project to {output / "project.qgz"}.
6. Reopen that project and inspect its layers and layouts. Report the actual successful outputs and any errors.
Use returned layer IDs; invoke tools sequentially. Existing output paths may be overwritten for this test if necessary. Do not claim completion without successful tools."""
    if args.execution_mode == "reliable":
        prompt = f"""Use ONLY smart_qgis MCP tools; do not write code or use terminal tools.
Produce a map of Shaanxi elevation using boundary {data / "ShannXi.shp"} and DEM {data / "DEM.tif"}.
The deliverables are a boundary-clipped elevation raster preserving the original grid and valid source elevations,
an editable QGIS project, and PNG and PDF maps with the title 陕西省海拔高度空间分布图, an elevation legend,
scale bar and coordinate graticule. Use a suitable elevation color ramp and a transparent boundary outline.
Inspect the actual inputs and algorithm documentation. You must generate the task and step contracts yourself
from the user's goal and data evidence, using the schemas provided by the MCP tools; no acceptance contract
is supplied in this prompt. Resolve missing nonessential presentation choices yourself.
Keep outputs in the server-managed task directory. Use the reliable task lifecycle and only declare success
after task_finish passes. Report the task ID, actual artifact paths and any unresolved checks.
If a tool rejects an input, read its diagnostic and fix the request without weakening the user's requirements.
"""
    if args.prompt_file:
        prompt = args.prompt_file.read_text()
        if not prompt.strip():
            parser.error('--prompt-file must contain a nonempty goal')
    if args.execution_mode == "reliable" and not args.exact_prompt:
        prompt += "\n" + MINIMUM_ACCEPTANCE_POLICY + "\n" + PARAMETER_HELP_POLICY + "\n"
    (output / "prompt.txt").write_text(prompt)
    if args.resume_task:
        prompt += (
            f"\nThis is a recovery execution test. Continue existing task {args.resume_task} using task_recover. "
            "Do not call task_begin or substitute a new task. Keep the locked acceptance contract. "
            "Inspect task_diagnose and the persisted contracts. For a FAILED or INVALIDATED step, "
            "submit a corrected step contract with repairs_step referencing that step; preserve its required checks. "
            "Use task_invalidate only when a previously COMMITTED result needs to be invalidated. "
            "Rebuild only invalidated or failed results, then use task_validate to check final deliverables. "
            "If historical project dependencies are incomplete, use the diagnostic to expand the repair scope. "
            "Report success only when this exact task passes task_finish.\n"
        )
        (output / "prompt.txt").write_text(prompt)
    package = Path(__file__).resolve().parents[1] / "src" / "smart_qgis"
    shutil.copytree(
        package, snapshot / "smart_qgis", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    manifest = {
        "client": args.client,
        "model": args.model,
        "execution_mode": args.execution_mode,
        "context_length": args.context_length if args.client == "hermes" else None,
        "reasoning_effort": args.reasoning_effort if args.client == "hermes" else None,
        "tool_discovery": args.tool_discovery if args.client == "hermes" else None,
        "timeout_seconds": args.timeout,
        "resume_task": args.resume_task,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "state_directory": str(args.state_dir.resolve()) if args.state_dir else "state",
        "source_sha256": {
            str(path.relative_to(snapshot)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(snapshot.rglob("*.py"))
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    env = os.environ.copy()
    server_args = ["-m", "smart_qgis.server", "--execution-mode", args.execution_mode]
    state_directory = str(args.state_dir.resolve() if args.state_dir else output / "state")
    if args.resume_task:
        database = Path(state_directory) / args.resume_task / "task.sqlite3"
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source_db:
            with sqlite3.connect(output / "initial-task.sqlite3") as snapshot_db:
                source_db.backup(snapshot_db)
    if args.client == "codex":
        command = [
            executable,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "-m",
            args.model,
            "-s",
            "workspace-write",
            "-c",
            f"mcp_servers.smart_qgis.command={json.dumps(sys.executable)}",
            "-c",
            "mcp_servers.smart_qgis.args=" + json.dumps(server_args),
            "-c",
            "mcp_servers.smart_qgis.env.SMART_QGIS_STATE_DIR=" + json.dumps(state_directory),
            "-c",
            "mcp_servers.smart_qgis.env.PYTHONPATH=" + json.dumps(str(snapshot)),
            "-c",
            "mcp_servers.smart_qgis.tool_timeout_sec=900",
            "-c",
            "mcp_servers.smart_qgis.startup_timeout_sec=90",
            "-c",
            'mcp_servers.smart_qgis.default_tools_approval_mode="approve"',
            "-o",
            str(output / "final.txt"),
            prompt,
        ]
    else:
        import yaml

        home = output / "hermes-home"
        home.mkdir(exist_ok=True)
        config = {
            "model": {
                "default": args.model,
                "provider": "custom",
                "base_url": "http://127.0.0.1:11434/v1",
                "context_length": args.context_length,
                "ollama_num_ctx": args.context_length,
            },
            "agent": {"max_iterations": 100 if args.execution_mode == "reliable" else 40},
            "mcp_servers": {
                "smart_qgis": {
                    "command": sys.executable,
                    "args": server_args,
                    "env": {"SMART_QGIS_STATE_DIR": state_directory, "PYTHONPATH": str(snapshot)},
                    "timeout": 900,
                    "connect_timeout": 90,
                    "supports_parallel_tool_calls": False,
                }
            },
        }
        if args.reasoning_effort is not None:
            config["agent"]["reasoning_effort"] = args.reasoning_effort
        if args.tool_discovery != "default":
            config["tools"] = {"tool_search": {
                "enabled": "off" if args.tool_discovery == "direct" else "on"
            }}
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        env["HERMES_HOME"] = str(home)
        env["OPENAI_API_KEY"] = "ollama"
        command = [
            executable,
            "--ignore-rules",
            "--model",
            args.model,
            "--provider",
            "custom",
            "--toolsets",
            "smart_qgis",
            "--usage-file",
            str(output / "usage.json"),
            "-z",
            prompt,
        ]
    timed_out = False
    with (output / "client.log").open("w") as log:
        with subprocess.Popen(
            command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        ) as completed:
            try:
                completed.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(completed.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    completed.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(completed.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    completed.wait()
    returncode = 124 if timed_out else completed.returncode
    if args.execution_mode == "reliable":
        tasks = []
        for database in Path(state_directory).glob("*/task.sqlite3"):
            if args.resume_task and database.parent.name != args.resume_task:
                continue
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute("SELECT id,status,checkpoint FROM task").fetchone()
                if row:
                    cp = json.loads(row["checkpoint"]) if row["checkpoint"] else {}
                    tasks.append(
                        {
                            "id": row["id"],
                            "status": row["status"],
                            "assets": cp.get("assets", {}),
                            "attempts": connection.execute(
                                "SELECT count(*) FROM attempts"
                            ).fetchone()[0],
                        }
                    )
        summary = {
            "client": args.client,
            "model": args.model,
            "execution_mode": args.execution_mode,
            "returncode": returncode,
            "timed_out": timed_out,
            "tasks": tasks,
        }
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "client": args.client,
                    "returncode": returncode,
                    "tasks": [{"id": task["id"], "status": task["status"]} for task in tasks],
                }
            )
        )
        raise SystemExit(
            returncode or (0 if any(task["status"] == "COMPLETED" for task in tasks) else 1)
        )
    print(
        json.dumps(
            {
                "client": args.client,
                "returncode": returncode,
                "outputs": {
                    name: (output / name).exists()
                    for name in ["Elevation.tif", "map.png", "map.pdf", "project.qgz"]
                },
            }
        )
    )
    outputs_ok = all(
        (output / name).is_file() and (output / name).stat().st_size > 0
        for name in ["Elevation.tif", "map.png", "map.pdf", "project.qgz"]
    )
    raise SystemExit(returncode or (0 if outputs_ok else 1))


if __name__ == "__main__":
    main()
