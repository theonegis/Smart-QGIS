# Legacy direct-tool research baseline

This mode exists only for controlled comparison. It has no durable task journal, continuation token, correction gate, or task recovery.

Use the public names returned by the live tool list. The current direct surface uses self-describing names such as `project_manage`, `load_data`, `layer_manage`, `feature_info`, `algorithm_info`, `processing_execute`, `style_vector`, `style_vector_graduated`, `style_raster`, `render_raster`, `qml_style_manage`, `vector_data_manage`, `layout_manage`, `export_map`, and `add_basemap`.

For Processing, discover an exact installed ID with a narrow `algorithm_info` query, read its help, and then call `processing_execute` with schema-correct parameters. Do not infer parameter names from memory. Apply the same three-consecutive-non-progress stop rule from the main skill even though the server cannot enforce it.

For maps, create a layout with title, legend, scale bar, and coordinate annotations unless the user explicitly omits an element. Persist requested processing outputs and save the QGIS project when requested. Claim only outputs confirmed by tool results.
