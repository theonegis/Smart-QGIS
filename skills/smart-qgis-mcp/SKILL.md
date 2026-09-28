---
name: smart-qgis-mcp
description: Use Smart-QGIS MCP reliably for GIS data inspection, QGIS Processing, styling, layouts, exports, task recovery, and user-guided correction. Apply whenever an agent must choose or call Smart-QGIS tools; use the legacy reference only for an explicitly requested research baseline.
---

# Smart-QGIS MCP

Use the live MCP tool list and schemas as the authority. This skill supplies reusable decision rules; it does not replace schema discovery, prescribe a workflow for one benchmark case, or authorize extra analysis.

Apply rules by information type and task state, not by memorizing a fixed tool chain. Do not encode dataset paths, layer names, field names, CRS values, algorithm IDs, parameter values, or multi-step recipes that are valid only for one task. Examples below illustrate syntax or a decision boundary; they are not mandatory workflows.

## Reliable workflow

1. If there is no task ID, the first Smart-QGIS domain call must be `task_start`, once, with the original goal, absolute input paths, requested deliverables, and an empty/minimal contract. Do not call `algorithm_info` before the task exists. Do not add acceptance rules beyond basic validity and explicit user requirements.
2. If `task_start` returns `task_execute_next`, call it with no arguments. The compact MCP service owns the current machine action handle; do not copy, infer, or supply continuation tokens, and do not rediscover a workflow the server already selected.
3. For a Processing route, determine one exact installed algorithm ID, then call `prepare_algorithm` once for that step. Bind logical task assets through `inputs`, managed results through `outputs`, and include only scalar parameters actually known from the user, inspected data, or live algorithm help. `prepare_algorithm` must read that exact help, mechanically normalize unambiguous name-case, JSON type, and enum-label representations, and pass native QGIS preflight before it creates an executable step contract.
4. Call `task_execute_next` with no arguments. Continue the same task until it reports `COMPLETED`, asks a structured question, requires real user guidance, or demonstrates an irreducible blocker.

For multi-step Processing, work just-in-time: discover, prepare and execute the next semantic operation before searching for algorithms for later operations. Do not inventory a whole future toolchain up front. A future step may depend on the actual output metadata or error from the current step, and early searches enlarge local-model context without advancing the task.

Never create a replacement task after an error, timeout, or reconnect. Use `task_diagnose` for read-only diagnosis and `task_recover` to reattach the existing task. If a committed result is proven wrong or unusable, invalidate it with `task_invalidate`, then prepare a new repair step using the returned state and the documented repair fields. Do not invalidate a merely uncommitted failed attempt.

For map coordinate annotations, supply a valid CRS identifier (for example `EPSG:4326` for geographic longitude/latitude), or omit the optional value to use the default. If a user chooses a different annotation CRS after a layout failure, record that actual choice with `task_record_guidance.map_coordinate_crs` and continue the same task; do not redo correct analysis or create a new task.

Treat the MCP tools advertised in the current session as a closed set. When a client exposes only a generic MCP gateway, list the named Smart-QGIS server once; that returned catalogue is the closed set. Never use tool search, invoke, or infer a tool name that is not in that catalogue. When a host does not expose the reply-recording capability, state the exact structured question and end the turn; the host owns recording the reply. Never fabricate user guidance.

## Resolve task context and exact identifiers

Before a mutation, identify only the unresolved facts needed by the next operation. Treat each kind of reference according to its source of truth:

- Use the original request and inspected input metadata for paths, requested outputs, explicit CRS choices, and user-supplied scientific or cartographic decisions.
- Whenever any tool argument, style, layout, selection, expression, or algorithm parameter requires an exact loaded-project layer name or layer ID and it is unknown, call `read_current_project` once. Copy `layers[].name` when the schema asks for a name, or the returned layer ID when it asks for an ID. Do not guess from a file name, path, logical asset ID, or parameter name.
- Obtain field names, band counts, geometry types, extents, CRS metadata, and other data-dependent values from the narrowest available inspection result. Never infer them from naming conventions.
- Obtain enum values, parameter names, types, defaults, and expression capabilities from the exact tool schema or installed algorithm help.
- If the needed source contains no usable value, do not repeat the same lookup. Choose a valid route that does not require that value, complete an approved prerequisite such as loading the data, or ask the user.

A required semantic value with no documented default that cannot be uniquely obtained from the request or inspected state must become a concise user question. Never guess a CRS, field, band, enum, layer identity, expression meaning, or scientific choice. Ask only for facts that materially change execution; do not turn optional values into required questions.

## Algorithm discovery and preparation

Parameter guessing is never part of the reliable workflow. The public MCP schema is authoritative for Smart-QGIS tool arguments; live QGIS registry help is authoritative for Processing arguments. `prepare_algorithm` reads that live help itself, so do not make a separate help call merely to repeat defaults or types.

Skip listing when the exact installed algorithm ID is already known and verified. Otherwise:

- Start with `algorithm_info(action="list", query="...")` using one or two discriminating words. Multiple query words use AND matching, so an empty result is a reason to remove words, not add more.
- Prefer a narrow `provider` or `group` filter over dumping a provider's registry. Keep `limit` a JSON integer, not a quoted string.
- Once a plausible ID is found, call `algorithm_info(action="help", algorithm="exact:id")` only when the agent must understand parameter semantics, choices, destination names, or expression syntax before preparing the step. Inspect `required`, `has_default`, `choices`, types and destinations. Use `include_details=true` only when compact help lacks one needed detail.
- Do not repeatedly list the same candidates, request large pages for manual scanning, or continue searching after help identifies a suitable algorithm.
- In `prepare_algorithm`, put layer/source bindings only in `inputs`, destination bindings only in `outputs`, and bands, numbers, enums, CRS values and expressions only in `parameters`. Omit an unknown required value. Do not manually retry with guessed spelling, types or values: the service corrects representation errors, applies documented defaults and turns genuinely missing semantic values into structured questions.

For any geoprocessing request, use the same general sequence: identify the intended operation, resolve one exact installed algorithm, let `prepare_algorithm` read its live help (view help explicitly only when needed as above), resolve any returned semantic question, then execute only the server-bound handle. The selected algorithm and number of steps depend on the current request and installed providers; do not prefer a remembered algorithm merely because it appeared in an earlier task.

## Expressions and embedded references

Treat every expression language as tool- and algorithm-specific, including raster calculations, field expressions, filters, selections, and labeling. First inspect the exact selected tool or installed algorithm; do not transfer aliases, operators, functions, quoting rules, or field syntax from another provider.

When an expression contains a layer, field, or band reference, resolve the exact identifier using the general rules above before composing the expression. Use only syntax and functions documented for the selected implementation. For example, when the installed algorithm is `qgis:rastercalculator`, include referenced rasters in `LAYERS` and represent a band with a double-quoted loaded layer name, `@`, and a one-based band number, such as `"dem@1"`. This example explains that calculator's reference form; it does not select the calculator or define a task-specific formula.

Never substitute an unquoted name, positional alias, path, or `asset:<id>` for a raster-band reference unless the selected algorithm explicitly documents that syntax. Use only operators and functions documented for that exact installed calculator. If execution reports `Error parsing formula`, preserve the error, recheck the layer names once with `read_current_project`, and correct one evidenced syntax issue. Do not cycle through guessed aliases or functions. If the required operation is not expressible with verified syntax, discover a dedicated installed algorithm or ask the user for a method hint.

Read the raster summary returned for every new raster before using it downstream. A `RASTER_ALL_NODATA` warning or `raster_summary.all_nodata=true` is evidence that the step produced no usable pixels, even when the file opens successfully. Do not silently treat that warning as task progress or add a new acceptance rule: inspect the exact formula, input alignment and NoData handling, repair the evidenced cause once, or ask the user after the correction limit.

Raster summaries in the compact reliable interface use full-resolution statistics and mark `statistics_approximate=false`. Values that drive scientific parameters, such as min-max normalization bounds, may be used only when that flag is false. If an older or external result explicitly reports approximate statistics, do not turn its sampled extrema into a formula; obtain exact statistics through an available inspection route or ask for guidance.

## Stop unproductive loops

Track consecutive calls that do not advance the task. Count all client-visible failures, including calls rejected by an MCP gateway before Smart-QGIS can record them.

A call is non-progress when it repeats the same error or query, returns no usable new candidate, repeats information already obtained, is rejected for a schema/type error, or leaves the agent at the same decision without narrowing it. A call is progress when it creates or advances the task, commits or invalidates a step, answers a genuinely new required question, or returns new information directly used in the next decision.

After three consecutive non-progress calls, do not make a fourth speculative call. End the turn with one concise question asking the user for the missing fact or a first-step/method hint. Report the existing task ID and the last concrete error. A successful read-only response resets the count only when it materially narrows the next action.

After a thinking or tool timeout, do not automatically replay a mutation. Diagnose the existing task, ask the user what to do next, record the real reply through the available host path, and then recover the same task.

## Output rules

For a map, retain a title, legend, scale bar, and coordinate annotations unless the user explicitly removes an element. Use the user's requested geographic or projected coordinate system when specified; otherwise follow the server's documented default.

Use the user's conversation language as the default language for reader-facing map text: normally Chinese for a Chinese request and English for an English request. Preserve proper nouns, established units, and user-specified wording; do not invent translations for unknown categories. Keep the title, legend, labels, and notes linguistically consistent where their meanings are known.

Base the map extent on the minimum bounding rectangle of the relevant data's actual footprint (valid cells for rasters, geometry for vectors); for multiple thematic layers, use the combined bounds needed to show them. A basemap must not enlarge this extent. Add only modest breathing room and match the map frame's proportions where practical, without cropping requested data merely to fill the frame.

Place the mapped area visually near the center of its map frame without large, uninformative blank margins. For legend and scale-bar placement, inspect the actual free space around the thematic result, ignoring basemap coverage. If they fit in existing blank areas inside the map frame without obscuring data or coordinate annotations, place them there; otherwise place whichever does not fit outside. When placing elements outside, compare a side column with a bottom row and choose the arrangement that leaves less unused space on the whole page; a bottom row can put the legend on the left and scale bar on the right. Size the page or footer to the actual map and element bounds; do not leave a tall empty strip below a short legend or scale bar. Do not create extra blank space, stretch or crop data merely to fit elements. Keep elements mutually separated, text legible, symbol contrast clear, scale units appropriate to the map CRS, and any available source attribution readable. Adjust layout, sizing, or extent before dropping a required element; honor any explicit user exception.

Keep the legend reader-facing: use meaningful layer and category names, sensible numeric precision, and omit technical raster band labels such as `Band 1` when they add no interpretive value. Use one shared color scale only when layers show the same quantity with the same units and classification range; identical palette names alone do not justify merging legends or forcing equal minima and maxima. Give different quantities distinct legends, and show a mask or category layer as a category rather than a misleading continuous numeric ramp. Preserve the actual symbol or color meaning; do not remove a useful legend merely to hide the band label.

When the user supplies different styles for named raster results, put only those known choices in `task_start.raster_styles`, keyed by logical layer ID. Use `mode="continuous"` with a ramp for a measured surface, or `mode="mask"` with a color and label for valid cells that all mean one category. The service can later update these presentation choices from actual user guidance without recomputing sound analysis outputs. Do not infer a mask, palette, or opacity from the layer name alone.

When `task_answer` is interrupted after the answer was durably recorded but before an executable step was returned, recover the same task and submit the same answer again. The service treats an identical saved answer as an idempotent continuation; it rejects a different replacement answer. Never create another task to escape this partial state.

When a sparse analysis result needs a contextual background to remain legible, set `task_start.layers` to the ordered logical layer IDs (topmost first), including the result and a relevant loaded context layer. For Processing tasks these may be planned intermediate outputs. Omit `layers` when the final outputs alone make a useful map.

If review of an existing map reveals missing context layers, ask for or follow the user's actual layer-order guidance. The host may record that answer as `task_record_guidance.map_layers`; invalidate the committed layout and its export, then recover the same task to regenerate the map. Do not recompute valid analysis rasters merely to revise the presentation.

Claim success only when the task reports `COMPLETED` and the requested artifacts are present and openable. Report failed or blocked status honestly; intended steps and model prose are not results.

## Legacy research baseline

If and only if the user or evaluation condition explicitly selects `--execution-mode legacy`, read [references/legacy.md](references/legacy.md). Do not use legacy instructions in reliable mode.
