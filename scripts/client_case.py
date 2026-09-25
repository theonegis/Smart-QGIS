"""Run the same natural-language acceptance case in an existing agent harness.

Generated configs, prompts and logs contain local paths; keep --output private.
No credentials are copied and no global agent configuration is changed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("client", choices=["codex", "hermes"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    output, data = args.output.resolve(), args.data.resolve()
    output.mkdir(parents=True, exist_ok=True)
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
    (output / "prompt.txt").write_text(prompt)
    env = os.environ.copy()
    if args.client == "codex":
        command = [
            executable,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--json",
            "-m",
            args.model,
            "-s",
            "workspace-write",
            "-c",
            f"mcp_servers.smart_qgis.command={json.dumps(sys.executable)}",
            "-c",
            'mcp_servers.smart_qgis.args=["-m","smart_qgis.server"]',
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
                "context_length": 65536,
            },
            "agent": {"max_iterations": 40},
            "mcp_servers": {
                "smart_qgis": {
                    "command": sys.executable,
                    "args": ["-m", "smart_qgis.server"],
                    "timeout": 900,
                    "connect_timeout": 90,
                    "supports_parallel_tool_calls": False,
                }
            },
        }
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
    with (output / "client.log").open("w") as log:
        completed = subprocess.run(
            command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=2400
        )
    print(
        json.dumps(
            {
                "client": args.client,
                "returncode": completed.returncode,
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
    raise SystemExit(completed.returncode or (0 if outputs_ok else 1))


if __name__ == "__main__":
    main()
