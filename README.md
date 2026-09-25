# Smart-QGIS 2.0

**Use QGIS from your AI agent, entirely in the background.**

Smart-QGIS is a local stdio MCP server for Codex, Hermes, and other MCP clients. Your existing agent handles conversation, planning, model calls, and approvals. Smart-QGIS uses LangChain `StructuredTool` definitions and an isolated PyQGIS worker to process geospatial data and produce maps. No QGIS desktop window or separate agent harness is required.

## Migrating from 1.0

Version 1.0 was a QGIS plugin with a Qt chat panel and a Socket bridge. It is preserved for archival purposes in both the **`1.0` branch and `1.0` tag**; ongoing development takes place on `main`.

Version 2.0 is a standalone MCP service on **`main`**, marked by the **`2.0` tag**. Register it in your agent client instead of copying it into the QGIS plugins directory. Existing `.qgs` and `.qgz` projects can still be opened when their data sources are accessible.

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

> Load `/data/ShannXi/ShannXi.shp` and `/data/ShannXi/DEM.tif`. Clip the DEM to the boundary with NoData set to 0 and save it as `/output/Elevation.tif`. Apply Viridis to the elevation layer and a transparent fill with a black outline to the boundary. Create a map with a title, legend, scale bar, and graticule. Export PNG and PDF files and save the QGIS project.

The server also provides a `dem-map` prompt template and the `qgis://project` resource.

Each MCP process owns one project. Save it before disconnecting; export and reload memory layers before saving. Processing algorithms may overwrite their output files, so choose output paths explicitly. Online basemaps require network access. Google roadmap, terrain, and satellite presets support custom URL overrides; follow the provider's access and attribution requirements.

## Validation and documentation

Validated on macOS with QGIS 4.2.2 through real Codex and Hermes + Ollama sessions, including clipping, styling, map export, and project reopening. Other platforms and QGIS distributions require their own validation.

Technical documentation is written in Chinese:

- [Development and tool reference](docs/development.md)
- [Client configuration](docs/clients.md)
- [Tests and reproducible cases](docs/testing.md)

```bash
uv run ruff check src scripts tests
uv run pytest -q
```

Local datasets, generated maps, credentials, and raw client logs are not included in the repository.
