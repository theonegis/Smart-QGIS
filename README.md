# Smart-QGIS MCP

Smart-QGIS is a headless local MCP server for QGIS data processing and map production. The MCP client supplies the model and conversation; Smart-QGIS runs PyQGIS in an isolated worker without opening QGIS Desktop.

It supports vector/raster data, project and layer management, map services, styling, copy-on-write vector edits, installed QGIS Processing algorithms, layouts and map export. The default `reliable` mode adds a SQLite task journal, basic input/parameter/output checks and recovery. Contracts remain minimal, unresolved required choices are asked rather than guessed, and maps retain a title, legend, scale bar and coordinate annotations unless the user explicitly omits one. A map title, legend heading or layout revision rebuilds only the layout and export; it does not recompute valid analysis results.

## Install

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/) and QGIS with Python bindings. macOS, Windows, Ubuntu and Fedora are supported; use QGIS's bundled Python when automatic discovery does not find a nonstandard installation.

```bash
uv sync --locked
uv run smart-qgis
```

Example MCP configuration:

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

## Use

Start a reliable task with `task_start`. Standard workflows continue through no-argument `task_execute`; general Processing uses an exact installed algorithm ID with `prepare_algorithm`. Use `project_info` and `data_info` for inspection, `task_update` for project/layer/service/style/output changes, and `task_resume` after a restart. Use `--execution-mode legacy` only when the direct-tool interface without task recovery is explicitly required.

- [Usage and recovery](docs/reliable-usage.md)
- [Client configuration](docs/clients.md)
- [Architecture](docs/development.md)
- [Testing](docs/testing.md)
- [Agent skill](skills/smart-qgis-mcp/SKILL.md)

### Hermes Weixin profile switch

For a local Hermes Weixin DM, install a restricted Smart-QGIS profile so the model sees only Smart-QGIS MCP and clarification tools:

```bash
./scripts/install_hermes_smart_qgis.sh
```

Restart Hermes once, then use `/smart-qgis on` before GIS work and `/smart-qgis off` to return to the default profile. `/smart-qgis status` reports the active mode and `/smart-qgis tools` lists the restricted GIS tool surface.
