---
name: smart-qgis-mcp
description: Use Smart-QGIS MCP reliably for GIS inspection, QGIS Processing, styling, single-frame maps, exports, recovery, and user-guided correction. Use the legacy reference only for an explicitly requested research baseline.
---

# Smart-QGIS MCP

The live MCP tool list and schemas are authoritative. This skill gives decision rules, not task-specific paths, layer names, algorithms, parameter values, or fixed recipes.

## Invocation and task lifecycle

- Pass the published argument object directly to a native MCP tool. If Hermes exposes Smart-QGIS through `tool_call`, wrap the complete object exactly as `{"calls":[{"name":"mcp__smart_qgis__TOOL","arguments":{...}}]}`. Fields beside `name` are not MCP arguments and are discarded. Send one deferred Smart-QGIS call at a time. If a gateway says required fields are missing although the draft contains them, inspect this envelope once; do not change GIS parameters.
- Treat the advertised Smart-QGIS tools as a closed set. Never search for or invoke an unadvertised tool. Use only Smart-QGIS for a task when the user or active profile requires it.
- Start a new request once with `task_start`: preserve the user's goal, use logical IDs with absolute input paths, declare requested outputs, and keep `contract={}` unless the user explicitly adds acceptance requirements. Put an exact requested filename in deliverable `path`, a requested folder in `directory`, or omit both for the system temporary output directory. Set `overwrite_existing_outputs=true` when the user explicitly authorizes replacement at those declared final paths; its default is false. The server also recognizes an unambiguous original instruction such as “同名文件请直接覆盖”, but the explicit field is preferred because it avoids an extra interpretation turn. Do not call `algorithm_info` before a task exists.
- If `task_start`, `task_update`, `task_answer`, `task_resume`, or `prepare_algorithm` returns `task_execute`, call the no-argument `task_execute`. The server owns action handles, internal steps, checkpoints, and tokens.
- Never replace a task after an error, timeout, or reconnect. `task_diagnose` is read-only; `task_resume` reattaches the task; `task_restart` applies a user-authorized failed-operation, analysis, or map restart. Do not expose or guess internal IDs.
- Record only a user's actual answer with `task_answer`, and only actual later instructions or overwrite approval with `task_update`. An identical answer may be resubmitted after an interrupted response; a different replacement answer is invalid.

## Resolve facts without guessing

Use the narrowest authoritative source:

- Request and inspected inputs: paths, outputs, explicit CRS/scientific/cartographic choices.
- `project_info`: exact loaded layer IDs/names, order, visibility, project CRS and layouts. Never infer a loaded name from a filename or logical ID.
- `data_info`: kind, CRS, extent, fields, bands and bounded feature samples/statistics.
- Tool schema or exact installed-algorithm help: parameter names, types, enums, defaults, destinations and expression syntax.

Ask one concise question when a required semantic value with no documented default remains unknown. Never guess a CRS, field, band, enum, layer identity, expression meaning, service endpoint, scientific method, or overwrite decision. Optional values do not become mandatory questions.

## Common project and display operations

- `task_start.project` uses `current`, `create`, or `open`; opening needs an exact absolute `.qgz/.qgs`. Declare a project deliverable to save or save-as. `project_update.crs` changes display CRS only; it does not reproject data.
- `layer_operations` supports rename, remove, visibility, opacity, subset order and grouping. Prefer logical IDs or exact IDs copied from `project_info`.
- `data_info.query` supports a bounded vector sample or numeric-field statistics with an optional verified QGIS expression; it never changes selection.
- `services` supports OSM, XYZ, WMS, WMTS and WFS. OSM needs no URL; XYZ needs an exact tile template; WMS/WMTS need an exact endpoint plus layer or a complete user URI; WFS needs an endpoint plus feature type and is a vector overlay. Use only supplied service CRS/style/version/`authcfg`; never request raw credentials. Basemaps stay below thematic layers and never set extent.
- Vector styles are `single`, `categorized`, `graduated`, `rule_based`, or declared `qml`. Verify fields, categories and rule expressions first. Raster styles are `continuous`, `mask`, `gray`, `rgb`, `hillshade`, or declared `qml`; RGB needs three explicit one-based bands. Do not infer styles, bands or QML files from names.
- `data_operations` creates, exports, or copy-edits vectors into a declared vector output. Use feature IDs returned by `data_info`; never edit an input file in place. Use Processing for field calculations, joins, geometry repair and other conversions.
- Submit project/data operations separately from a presentation revision so each has an unambiguous checkpoint.

## Processing discovery and preparation

Work one semantic step at a time. Do not inventory later algorithms before current output metadata exists.

1. If an exact installed algorithm ID is already verified, skip listing. Otherwise call `algorithm_info(action="list")` once with 1–3 discriminating terms and an exact provider/group filter when known. Multiple terms use AND matching. A no-match `suggestions` list contains candidates only: verify one by name/provider or retry once with fewer/corrected terms; never execute it automatically.
2. Use `algorithm_info(action="help", algorithm="provider:id")` only when parameter semantics, choices, destinations, or expression syntax are needed. Use `include_details=true` only when compact help lacks the needed fact. Stop discovery after selecting one suitable exact ID.
3. Call `prepare_algorithm` once: map layer/source parameters in `inputs`, destination parameters in `outputs`, and only known bands/numbers/enums/CRS/expressions in `parameters`. It reads live help, normalizes unambiguous case/JSON/enum representation, applies documented defaults, and runs QGIS native preflight. Omit an unknown required value so it becomes a structured question.
4. Execute only the server-returned action. Read new raster summaries before downstream use. `all_nodata=true` is unusable evidence; inspect the formula/alignment/NoData cause once or ask after the correction limit. Scientific values may use raster statistics only when `statistics_approximate=false`.

Expression syntax belongs to the selected tool/algorithm. Resolve referenced layer names, fields and bands first and use only its documented quoting, operators and functions. For `qgis:rastercalculator`, a band reference is a double-quoted loaded name plus one-based band, for example `"dem@1"`, and the raster must be in `LAYERS`. Do not transfer this syntax to another calculator. After a parse error, recheck project names once and correct one evidenced issue; do not cycle through aliases.

## Stop loops and recover safely

- Count client-visible non-progress: repeated errors/queries, unusable candidates, schema rejection, or calls that do not narrow the next decision. Progress creates/advances a task, commits/invalidates a step, records a new real answer, or obtains information used immediately.
- After three consecutive non-progress calls, stop and ask for the missing fact or a first-step/method hint. Include the task ID and last concrete error. Do not make a fourth speculative call.
- After model/tool timeout, do not replay a mutation. Diagnose the same task, obtain real guidance, record it, then resume/restart as indicated.
- On `OUTPUT_EXISTS`, ask whether the exact reported regular file may be replaced unless the user already explicitly authorized it. Choose the matching public `decision_calls` entry from the error and pass its complete structured `output_conflict` to `task_update`; this is preferred. An unambiguous real answer such as “直接覆盖” is also bound by the server to the exact pending path. A valid decision automatically resumes only the failed operation and returns no-argument `task_execute`. Do not call `task_restart`, delete with another host tool, or infer approval.
- If an explicit requested option is absent from the published schema, report it as unsupported and ask whether a supported alternative is acceptable. Do not invent hidden parameters or retry approximations.

## Single-frame cartography

Unless explicitly removed, a map contains a title, legend, scale bar and coordinate annotations. A north arrow is added only when requested. Reader-facing language follows the user's language while preserving proper names and units. Keep `map_language=auto` unless the user explicitly asks to force generated map text to Chinese (`zh`) or English (`en`).

- `map_crs` is an on-the-fly display CRS; use Processing to create reprojected data. Coordinate annotations have independent `coordinate_crs`, sides, decimal/degree-minute/degree-minute-second format, precision, geographic E/W/N/S suffixes, `sparse|moderate|dense` density (default `moderate`) and optional grid lines. The service resolves longitude and latitude independently from label counts—not fixed degree/metre steps—using standard 1/2/5×10ⁿ intervals: at least 2 labels per axis for `sparse`, 4 for `moderate`, and 6 for `dense`. Degree formats and E/W/N/S require a geographic annotation CRS.
- Set extent from the valid thematic-data footprint (combined bounds for multiple thematic layers). A thematic raster fills the map frame exactly; do not add automatic internal whitespace around it. Vector-only maps may keep modest breathing room. Basemap coverage must not enlarge the extent. Preserve aspect ratio and requested data.
- The main map frame targets at least 80% of both page width and page height by default. Default `map_frame.mode` is `maximize`; automatic pages expand around the frame rather than shrinking it. Before export the service checks the actual bounds of every visible layout element and enlarges the relevant page margin if needed. If the measured legend, scale bar or other element needs more room, it may relax the automatic coverage target to retain every element; no map is exported with a cropped visible item. If no supported page can contain them, it returns a clear failure. `min_page_coverage` remains an optional page-area acceptance check.
- Place legend and scale bar in real thematic blank space inside the frame only when they fit without covering data/coordinates. Otherwise count visible thematic layers (each visible raster, point, line or polygon layer is one legend item; basemaps do not count): at most three puts the legend below the map-frame left and the scale bar on the same row at map-frame right; more than three uses a vertical right-side column with legend and scale bar left-aligned. An automatic short bottom legend has no generic `图例`/`Legend` heading; an automatic right-side long legend has one. `show_legend_title=true|false` explicitly overrides that default. Automatic pages use a fixed 500 mm short-edge scale (the long edge follows data aspect): coordinate labels are 42 pt, title 58 pt, legend 48 pt, and scale-bar text 36 pt. Keep safe space around all coordinate labels, including a dedicated width allowance on the left and right so outermost numbers cannot be clipped. A one-layer legend has exactly one item label: an explicit reader-facing name replaces the layer name and suppresses a separate heading unless the user explicitly forces a generic heading. Suppress raw `Band 1`-style labels while retaining the meaningful layer name. Before measuring and positioning a legend, the server refreshes its QGIS model and reserves a paint-safe raster-legend footer; it must never export a clipped legend.
- Explicit positions override defaults. Every title/legend/scalebar/north-arrow `frame=inside|outside` and nine-position `anchor` is relative to the main map frame—not the page. The service retains a visual gap between every element and the frame in either mode; do not assign two elements the same explicit position. Legend adds `flow` and `border`; scale bar adds common `units` and `style`. Coordinate labels remain attached to frame sides.
- Keep elements non-overlapping and legible. Legend entries use current QGIS layer names, meaningful categories and sensible precision; omit technical `Band 1` labels. For a one-layer legend, an explicit reader-facing legend name replaces the layer name and suppresses a separate heading: display it once, in one language. Share a color scale only for the same quantity, units and classification range.
- For a finished map revision, put only the actual change in `task_update` (for example title, legend heading, layers, styles, positions, frame or coordinate choices), then execute the returned action. Preserve valid analysis outputs.

Claim success only when `task_execute` returns `COMPLETED` and requested artifacts are present and openable. Return a compact digest: task ID/status, requested deliverables, verified absolute paths, essential checks, and any one pending decision. Do not repeat raw help, internal paths, logs or implementation tokens. Visually inspect a completed map only when the user explicitly requests review.

## Legacy research baseline

Only when the evaluation explicitly selects `--execution-mode legacy`, read [references/legacy.md](references/legacy.md).
