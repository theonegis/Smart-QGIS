"""Small, deterministic safety checks attached to mutating GIS operations.

The reliable boundary is deliberately narrow: input/parameter validation happens
before execution and basic artifact usability happens afterwards.  Algorithm
families must not silently turn implementation details into acceptance criteria;
task-specific semantics belong to explicit user-requirement checks.
"""

import math
import re
import shlex

from pydantic import TypeAdapter

from .contracts import Check
from .task_store import TaskError


def clip_boundary_rule(extra):
    """Only this exact GDAL warp option has a reliable-mode adapter."""
    if extra is None or extra == "":
        return "pixel_center"
    if extra == "-wo CUTLINE_ALL_TOUCHED=TRUE":
        return "all_touched"
    if extra == "-wo CUTLINE_ALL_TOUCHED=FALSE":
        return "pixel_center"
    raise TaskError(
        "UNSUPPORTED_SIDE_EFFECT", "Unadapted additional GDAL arguments",
        next_action="For mask clipping EXTRA may only be '-wo CUTLINE_ALL_TOUCHED=TRUE' or '-wo CUTLINE_ALL_TOUCHED=FALSE'; omit EXTRA for the default pixel-center rule",
    )


def gdal_translate_extra(extra):
    """Validate the side-effect-free gdal_translate options exposed by QGIS.

    ``EXTRA`` is part of the installed algorithm schema, so reliable mode must
    not reject it wholesale.  Only band selection and numeric georeferencing
    are admitted here; file arguments, response files, creation options and
    unknown switches remain outside the managed execution boundary.
    """
    if extra is None or extra == "":
        return
    if not isinstance(extra, str):
        raise TaskError(
            "INVALID_PARAMETERS",
            "gdal:translate EXTRA must be a command-line string",
        )
    try:
        tokens = shlex.split(extra, posix=True)
    except ValueError as exc:
        raise TaskError(
            "INVALID_PARAMETERS",
            "gdal:translate EXTRA has invalid quoting",
        ) from exc

    index = 0
    seen_extent = False
    while index < len(tokens):
        option = tokens[index]
        if option == "-b":
            if index + 1 >= len(tokens) or not re.fullmatch(r"[1-9][0-9]*", tokens[index + 1]):
                raise TaskError(
                    "INVALID_PARAMETERS",
                    "gdal:translate -b requires a positive one-based band number",
                )
            index += 2
            continue
        if option == "-a_ullr":
            if seen_extent or index + 4 >= len(tokens):
                raise TaskError(
                    "INVALID_PARAMETERS",
                    "gdal:translate -a_ullr requires exactly four numeric coordinates",
                )
            values = tokens[index + 1:index + 5]
            try:
                coordinates = [float(value) for value in values]
            except ValueError as exc:
                raise TaskError(
                    "INVALID_PARAMETERS",
                    "gdal:translate -a_ullr coordinates must be numeric",
                ) from exc
            if not all(math.isfinite(value) for value in coordinates):
                raise TaskError(
                    "INVALID_PARAMETERS",
                    "gdal:translate -a_ullr coordinates must be finite",
                )
            seen_extent = True
            index += 5
            continue
        raise TaskError(
            "UNSUPPORTED_SIDE_EFFECT",
            "Unsupported gdal:translate EXTRA option",
            evidence={"option": option, "allowed_options": ["-b", "-a_ullr"]},
            next_action=(
                "Use dedicated algorithm parameters where available. EXTRA for gdal:translate "
                "only accepts -b <positive band> and -a_ullr <ulx> <uly> <lrx> <lry>."
            ),
        )


def family_checks(step, defaults=None, *, map_omissions=()):
    """Return only universal postconditions, never inferred quality requirements.

    ``defaults`` remains accepted for API compatibility with persisted callers.
    It must not be used to derive acceptance requirements from algorithm choices.
    """
    if any(check.id.startswith("system_") for check in [*step.preconditions, *step.postconditions]):
        raise TaskError(
            "RESERVED_CHECK_ID", "system_ check IDs are reserved for server rules",
            next_action=(
                "Omit system_ checks copied from contract_get; the server regenerates them. "
                "Keep all required non-system checks unchanged in their original preconditions/postconditions."
            ),
        )
    pre, post = [], []

    def add(destination, kind, target, **params):
        destination.append(
            TypeAdapter(Check).validate_python(
                {
                    "id": f"system_{len(pre) + len(post)}_{kind}",
                    "kind": kind,
                    "target": target,
                    "source": "algorithm_rule",
                    "basis": "Basic output usability invariant",
                    "evidence": ["operation:" + step.operation],
                    "required": True,
                    **params,
                }
            )
        )

    for output in step.outputs:
        # Readability/type is checked by TaskCoordinator.basic_outputs.  A valid
        # spatial reference is the only additional universal condition for GIS
        # datasets; geometry, coverage, precision, ranges, NoData encodings,
        # styling and layer order are not inferred requirements.
        if output.kind in {"vector", "raster"}:
            add(post, "crs_valid", output.id)

    if step.operation == "layout" and step.arguments["action"] == "create":
        omissions = set(map_omissions)
        required = {
            "require_title": "title" not in omissions,
            "require_legend": "legend" not in omissions,
            "require_scalebar": "scalebar" not in omissions,
            "require_north_arrow": "north_arrow" not in omissions and step.arguments.get("north_arrow", False),
            "require_grid": "coordinates" not in omissions,
        }
        if any(required.values()):
            for output in step.outputs:
                add(
                    post,
                    "layout_content",
                    output.id,
                    **required,
                    grid_crs=(
                        step.arguments.get("grid_crs")
                        if required["require_grid"]
                        else None
                    ),
                    element_placements={
                        name: {
                            key: value[key] for key in ("frame", "anchor")
                            if value.get(key) is not None
                        }
                        for name, value in (step.arguments.get("map_elements") or {}).items()
                        if value and any(value.get(key) is not None for key in ("frame", "anchor"))
                    },
                    min_page_coverage=(step.arguments.get("map_frame") or {}).get("min_page_coverage"),
                )

    return pre, post
