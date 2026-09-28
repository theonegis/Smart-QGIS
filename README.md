# Smart-QGIS MCP

Smart-QGIS is a headless local MCP server for QGIS data processing and map production. The MCP client supplies the model and conversation; Smart-QGIS runs PyQGIS in an isolated worker without opening QGIS Desktop.

It supports vector/raster data, projects, styling, installed QGIS Processing algorithms, layouts and map export. The default `reliable` mode adds a SQLite task journal, basic input/parameter/output checks and recovery. Contracts remain minimal, unresolved required choices are asked rather than guessed, and maps retain a title, legend, scale bar and coordinate annotations unless the user explicitly omits one.

## Install

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/) and QGIS with Python bindings.

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

Start a reliable task with `task_start`. Standard workflows continue through no-argument `task_execute_next`; general Processing uses an exact installed algorithm ID with `prepare_algorithm`. After a restart, reattach the same task with `task_recover`. Use `--execution-mode legacy` only when the direct-tool interface without task recovery is explicitly required.

- [Usage and recovery](docs/reliable-usage.md)
- [Client configuration](docs/clients.md)
- [Architecture](docs/development.md)
- [Testing](docs/testing.md)
- [Agent skill](skills/smart-qgis-mcp/SKILL.md)

```bash
uv run ruff check src tests
uv run pytest -q
```
