# Smart-QGIS 2.0

**Use QGIS from your AI agent, entirely in the background.**

Smart-QGIS is a local stdio MCP server for Codex, Hermes, and other MCP clients. Your existing agent handles conversation, planning, model calls, and approvals. Smart-QGIS uses LangChain `StructuredTool` definitions and an isolated PyQGIS worker to process geospatial data and produce maps. No QGIS desktop window or separate agent harness is required.

## Migrating from 1.0

**Development checkout:** task contracts and recovery are under active development. The server defaults to the new reliable task interface, requiring agent-generated task and step contracts before mutations. `--execution-mode legacy` is retained solely as an unjournaled direct-tool baseline for research comparisons, not as a compatibility promise.

Reliable mode exposes a compact task surface plus read-only algorithm discovery. `task_start` checks every input, locks the minimal task contract and selects the route; a standard map/project then needs one `task_execute_next` call carrying only an opaque server-bound token. General Processing uses the exact algorithm ID with `prepare_algorithm`, which reads live registry metadata and persists typed questions for unresolved required parameters. Direct mutation and low-level lifecycle tools are hidden from this default mode. No client extension or model API key is required.

Empty task contracts and standard workflows require no schema or algorithm discovery. Input `kind` is optional at the compact entry: the service inspects vector/raster files and records the actual type before execution. The server owns basic preflight/postflight checks; contracts add only explicit user requirements and may be empty otherwise.

Reliable task databases and artifacts are retained for recovery. On service startup, whole `COMPLETED` or `CANCELLED` task directories older than 10 days and `BLOCKED` task directories older than 60 days are removed by default. Running, unfinished, busy, or question-waiting tasks are never removed. Use `--retention-days` and `--failed-retention-days` to change these periods; `0` disables the corresponding cleanup. `--timeout` sets the per-operation MCP worker limit (default: 900 seconds); an agent's thinking timeout must be enforced by its host.

Processing parameters are checked before a step is approved; output format/readability and spatial CRS are checked after execution. Maps include a title, legend, scale bar, and geographic or projected coordinate annotations by default; an element may be omitted only when the user explicitly requests it. Agents must ask about material unresolved choices before running. By default, three consecutive rejected preparation or execution-argument requests—or three semantic repair plans for one failure—require an actual user response through `task_record_guidance` before further correction. Set `--correction-limit N` in the MCP server startup arguments to choose another positive limit. `task_answer` is for typed questions about missing required algorithm parameters. A failed Processing step can be replaced through `prepare_algorithm(repairs_step=...)`; an incorrect committed result is first invalidated with `task_invalidate`, then resubmitted with the same output ID. User guidance opens a new correction window without weakening locked requirements. See the reliable task guide for persistence and host-trust limits.

Version 1.0 was a QGIS plugin with a Qt chat panel and a Socket bridge. It is preserved for archival purposes in both the **`1.0` branch**.

The historical 2.0 snapshot is preserved by the **`2.0` tag**; **`main`** develops the new 2.0 task interface. Register it in your agent client instead of copying it into the QGIS plugins directory. The legacy research baseline is not maintained as a user-facing compatibility layer.

## Features

- **Projects and data:** create, open, and save projects; load vector/raster files, OSM, Google and custom XYZ tiles, WMS, and WMTS.
- **Layers and visualization:** ordering, visibility, names, categorized/graduated symbols, labels, opacity, color ramps, grayscale, RGB, hillshade, and QML styles.
- **Geoprocessing:** discover and run installed QGIS/GDAL algorithms, including clipping, buffers, overlays, reprojection, raster calculations, and slope analysis.
- **Features:** filtering, selection, field statistics, in-memory GeoJSON layers, and GPKG/GeoJSON/SHP export.
- **Mapping:** editable print layouts, titles, legends, scale bars, graticules, QPT templates, and PNG/JPEG/TIFF/PDF export.

## Installation

Install Python 3.11+, [uv](https://docs.astral.sh/uv/), and QGIS with Python bindings. On macOS, `/Applications/QGIS.app` is detected automatically. See the [development guide](docs/development.md) for other runtime locations.

```bash
uv sync --locked
uv run smart-qgis
```

The last command starts a stdio server and waits for MCP input. It does not open a GUI or a web page. Normally, your agent client starts this process for you.

## Connect your agent

Use absolute paths in your client's configuration:

```json
{
  "mcpServers": {
    "smart_qgis": {
      "command": "/path/to/Smart-QGIS/.venv/bin/python",
      "args": ["-m", "smart_qgis.server"]
    }
  }
}
```

Codex and Hermes use their own configuration formats; see the [client setup guide](docs/clients.md). Model selection and credentials remain in the client. Smart-QGIS itself requires no LLM API key. A client tool timeout of 900 seconds is recommended for larger datasets.

## Example

After connecting, ask your agent:

> Load `/data/ShannXi/ShannXi.shp` and `/data/ShannXi/DEM.tif`. Inspect the data, then clip the DEM to the boundary while preserving valid elevations and save it as `/output/Elevation.tif`. Choose a NoData representation that does not conflict with valid source values. Apply Viridis to the elevation layer and a transparent fill with a black outline to the boundary. Create a map with a title, legend, scale bar, and graticule. Export PNG and PDF files and save the QGIS project.

The server also provides a `dem-map` prompt template and the `qgis://project` resource.

Each MCP process owns one project. Save it before disconnecting; export and reload memory layers before saving. Processing algorithms may overwrite their output files, so choose output paths explicitly. Online basemaps require network access. Google roadmap, terrain, and satellite presets support custom URL overrides; follow the provider's access and attribution requirements.

## Validation and documentation

Validated on macOS with QGIS 4.2.2 through real Codex and Pi + Ollama sessions, including clipping, styling, map export, and project reopening. Other platforms and QGIS distributions require their own validation.

Technical documentation is written in Chinese:

- [Development and tool reference](docs/development.md)
- [Client configuration](docs/clients.md)
- [Tests and reproducible cases](docs/testing.md)

```bash
uv run ruff check src scripts tests
uv run pytest -q
```

Contribution is welcome!
