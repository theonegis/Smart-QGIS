"""Generic, registry-driven QGIS Processing parameter preparation.

This module deliberately knows parameter *types*, not algorithm IDs.  QGIS
algorithm help is the authority for required values, defaults and choices.
"""

from __future__ import annotations

import ast
import math
import re
from typing import Any, Literal

from pydantic import Field

from .contracts import Model, Nonempty, SafeId
from .task_store import TaskError

AnswerType = Literal["crs", "field", "enum", "layer", "destination", "value"]
ResolutionStatus = Literal[
    "provided", "answered", "default", "optional", "unresolved"
]


class AlgorithmParameter(Model):
    name: Nonempty
    description: Nonempty
    type: Nonempty
    destination: bool = False
    required: bool = True
    has_default: bool = False
    default: Any = None
    choices: list[dict[str, Any]] = Field(default_factory=list)
    multiple: bool = False
    parent_parameter: str | None = None
    definition: dict[str, Any] = Field(default_factory=dict)


class ParameterResolution(Model):
    parameter: Nonempty
    status: ResolutionStatus
    source: Literal["request", "answer", "qgis_default", "omitted", "missing"]
    value: Any = None


class StructuredQuestion(Model):
    id: SafeId
    parameter: Nonempty
    answer_type: AnswerType
    prompt: Nonempty
    choices: list[dict[str, Any]] = Field(default_factory=list)
    multiple: bool = False


class AlgorithmResolution(Model):
    plan_id: SafeId
    algorithm: Nonempty
    step_id: SafeId
    status: Literal["READY", "WAITING_FOR_USER", "PLANNED"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    resolutions: list[ParameterResolution] = Field(default_factory=list)
    questions: list[StructuredQuestion] = Field(default_factory=list)
    normalizations: list[dict[str, Any]] = Field(default_factory=list)


_GDAL_GLOBAL_REDUCTIONS = {
    "amin", "amax", "max", "mean", "median", "min", "nanmax", "nanmean",
    "nanmedian", "nanmin", "nanpercentile", "nansum", "nanstd", "percentile",
    "std", "sum",
}
_GDAL_FUNCTION_ARITY = {
    "logical_and": 2,
    "logical_not": 1,
    "logical_or": 2,
    "where": 3,
}


def validate_processing_expressions(algorithm: str, parameters: dict[str, Any]) -> None:
    """Reject common expression-dialect errors before QGIS executes anything.

    This is deliberately narrow. It validates only stable syntax properties of
    GDAL's raster-calculator formula and never invents scientific values or a
    replacement formula.
    """
    if algorithm.casefold() != "gdal:rastercalculator":
        return
    formula = parameters.get("FORMULA")
    if not isinstance(formula, str) or not formula.strip():
        return
    if re.search(r"\b(?:np|numpy)\s*\.", formula):
        raise TaskError(
            "INVALID_EXPRESSION_DIALECT",
            "GDAL raster calculator formulas use bare supported functions, not np./numpy namespaces",
            phase="preparation",
            evidence={"algorithm": algorithm, "parameter": "FORMULA", "dialect": "GDAL NumPy-style formula"},
            next_action=(
                "Use algorithm_info for the selected algorithm and supply a GDAL-compatible "
                "formula, or use a dedicated Processing algorithm for the intended operation."
            ),
        )
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise TaskError(
            "INVALID_EXPRESSION",
            "GDAL raster calculator formula is not valid expression syntax",
            phase="preparation",
            evidence={"algorithm": algorithm, "parameter": "FORMULA", "offset": exc.offset},
            next_action="Correct the formula syntax using the selected algorithm's documented dialect.",
        ) from exc
    if any(isinstance(node, ast.BoolOp) for node in ast.walk(tree)):
        raise TaskError(
            "INVALID_EXPRESSION_DIALECT",
            "Python 'and'/'or' are not array operators in GDAL raster calculator formulas",
            phase="preparation",
            evidence={"algorithm": algorithm, "parameter": "FORMULA"},
            next_action="Use supported element-wise logical functions or operators.",
        )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id.casefold()
        if name in _GDAL_GLOBAL_REDUCTIONS:
            raise TaskError(
                "NONLOCAL_RASTER_EXPRESSION",
                "Global raster statistics cannot be computed reliably inside a block-wise raster calculator formula",
                phase="preparation",
                evidence={"algorithm": algorithm, "parameter": "FORMULA", "function": name},
                next_action=(
                    "Compute or obtain the required statistic first, then pass the explicit value, "
                    "or select a dedicated rescaling/statistics algorithm."
                ),
            )
        expected = _GDAL_FUNCTION_ARITY.get(name)
        if expected is not None and (len(node.args) != expected or node.keywords):
            raise TaskError(
                "INVALID_EXPRESSION",
                f"GDAL raster calculator function {name} requires exactly {expected} positional arguments",
                phase="preparation",
                evidence={
                    "algorithm": algorithm,
                    "parameter": "FORMULA",
                    "function": name,
                    "expected_arguments": expected,
                    "received_arguments": len(node.args),
                },
                next_action="Correct the function call using the selected algorithm's documented dialect.",
            )


def normalize_parameter(raw: dict[str, Any]) -> AlgorithmParameter:
    """Reduce version-specific QGIS help to the stable facts preparation needs."""
    definition = raw.get("definition") or {}
    parent = next(
        (
            definition.get(key)
            for key in (
                "parent_layer_parameter_name",
                "parent_layer",
                "parentLayerParameterName",
                "parent_parameter_name",
                "parentParameterName",
            )
            if definition.get(key)
        ),
        None,
    )
    choices = raw.get("choices") or []
    if not choices and raw.get("type") == "enum":
        options = definition.get("options") or []
        strings = bool(definition.get("uses_static_strings"))
        choices = [
            {"value": label if strings else index, "label": label}
            for index, label in enumerate(options)
        ]
    return AlgorithmParameter(
        name=raw["name"],
        description=raw.get("description") or raw["name"],
        type=raw.get("type") or definition.get("parameter_type") or "value",
        destination=bool(raw.get("destination")),
        required=bool(raw.get("required", True)),
        has_default=bool(raw.get("has_default", False)),
        default=raw.get("default"),
        choices=choices,
        multiple=bool(
            raw.get("multiple")
            or definition.get("allow_multiple")
            or definition.get("allowMultiple")
        ),
        parent_parameter=parent,
        definition=definition,
    )


def normalize_algorithm_help(help_result: dict[str, Any]) -> list[AlgorithmParameter]:
    return [normalize_parameter(item) for item in help_result.get("parameters", [])]


def destination_asset_kind(parameter: AlgorithmParameter) -> str | None:
    """Return a safe automatic working-asset kind for common QGIS sinks.

    A Processing chain normally has many private raster/vector products.  Making
    an agent declare every one in the task acceptance contract turns bookkeeping
    into a source of retries, although it is not an acceptance requirement.  The
    live registry already identifies the common sink types, so use that fact for
    durable *working* assets.  Ambiguous destinations remain explicit rather
    than being guessed.
    """
    if not parameter.destination:
        return None
    kind = parameter.type.casefold()
    if "raster" in kind:
        return "raster"
    if "vector" in kind or "feature" in kind:
        return "vector"
    # QGIS uses the bare ``sink`` parameter type for feature outputs (for
    # example field calculator and overlay algorithms).  Treat it as vector;
    # non-spatial table/file destinations use distinct types and remain explicit.
    if kind == "sink":
        return "vector"
    return None


def _answer_type(parameter: AlgorithmParameter) -> AnswerType:
    kind = parameter.type.casefold()
    if parameter.destination:
        return "destination"
    if kind == "crs" or "crs" in kind:
        return "crs"
    if "field" in kind:
        return "field"
    if kind == "enum" or parameter.choices:
        return "enum"
    if kind in {"source", "vector", "raster", "maplayer", "multilayer"}:
        return "layer"
    return "value"


def _validate_answer(question: StructuredQuestion, value: Any) -> Any:
    if question.multiple:
        values = value if isinstance(value, list) else [value]
    else:
        if isinstance(value, list):
            raise TaskError(
                "INVALID_ANSWER", "This question requires one value",
                evidence={"question_id": question.id},
            )
        values = [value]
    if question.choices:
        allowed = [item["value"] for item in question.choices]
        labels = {str(item["label"]).casefold(): item["value"] for item in question.choices}
        normalized = []
        for item in values:
            if item in allowed:
                normalized.append(item)
            elif isinstance(item, str) and item.casefold() in labels:
                normalized.append(labels[item.casefold()])
            else:
                raise TaskError(
                    "INVALID_ANSWER", "Answer is not one of the allowed choices",
                    evidence={"question_id": question.id, "choices": question.choices},
                )
        values = normalized
    if any(item is None or (isinstance(item, str) and not item.strip()) for item in values):
        raise TaskError("INVALID_ANSWER", "A required answer cannot be empty")
    return values if question.multiple else values[0]


def _normalize_matrix(value: Any) -> Any:
    """Canonicalize explicit QGIS matrix rows without inferring values."""
    if not isinstance(value, list) or not value or not all(isinstance(row, (list, dict)) for row in value):
        return value
    flattened: list[Any] = []
    for row in value:
        if isinstance(row, dict):
            if not {"minimum", "maximum", "value"}.issubset(row):
                return value
            flattened.extend([row["minimum"], row["maximum"], row["value"]])
        elif len(row) == 3:
            flattened.extend(row)
        elif len(row) == 2:
            flattened.extend([row[0], row[0], row[1]])
        else:
            return value
    return flattened


def canonicalize_parameter_names(
    supplied: dict[str, Any], specifications: list[AlgorithmParameter]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply only unambiguous case corrections from live algorithm help."""
    exact = {item.name for item in specifications}
    folded: dict[str, list[str]] = {}
    for name in exact:
        folded.setdefault(name.casefold(), []).append(name)
    result: dict[str, Any] = {}
    normalizations: list[dict[str, Any]] = []
    for provided, value in supplied.items():
        canonical = provided
        matches = folded.get(provided.casefold(), [])
        if provided not in exact and len(matches) == 1:
            canonical = matches[0]
            normalizations.append({
                "parameter": canonical,
                "from": provided,
                "to": canonical,
                "reason": "parameter_name_case",
            })
        if canonical in result:
            raise TaskError(
                "INVALID_PARAMETERS", "Multiple supplied names resolve to one algorithm parameter",
                evidence={"parameter": canonical},
            )
        result[canonical] = value
    return result, normalizations


def _choice_value(parameter: AlgorithmParameter, value: Any) -> Any:
    allowed = [item["value"] for item in parameter.choices]
    if value in allowed:
        return value
    if not isinstance(value, str):
        return value
    labels = {
        str(item["label"]).strip().casefold(): item["value"]
        for item in parameter.choices
    }
    if value.strip().casefold() in labels:
        return labels[value.strip().casefold()]
    for candidate in allowed:
        if not isinstance(candidate, str) and str(candidate) == value.strip():
            return candidate
    return value


def normalize_supplied_value(parameter: AlgorithmParameter, value: Any) -> Any:
    """Losslessly coerce a model value using live type and choice metadata.

    This deliberately fixes representation errors only.  It never selects a
    missing CRS, field, enum, expression, band, or scientific value.
    """
    if parameter.multiple and parameter.choices:
        values = value if isinstance(value, list) else [value]
        return [_choice_value(parameter, item) for item in values]
    if parameter.choices:
        return _choice_value(parameter, value)
    kind = parameter.type.casefold()
    if kind == "matrix":
        return _normalize_matrix(value)
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if kind in {"boolean", "bool"} and stripped.casefold() in {"true", "false"}:
        return stripped.casefold() == "true"
    integer = kind in {"band", "integer", "int"} or (
        kind == "number" and parameter.definition.get("data_type") == 0
    )
    numeric = integer or kind in {"number", "distance", "scale"}
    if integer:
        try:
            parsed = int(stripped)
        except ValueError:
            return value
        return parsed if str(parsed) == stripped or stripped in {f"+{parsed}", f"-{abs(parsed)}"} else value
    if numeric:
        try:
            parsed = float(stripped)
        except ValueError:
            return value
        return parsed if math.isfinite(parsed) else value
    return value


def resolve_parameters(
    *,
    plan_id: str,
    algorithm: str,
    step_id: str,
    specifications: list[AlgorithmParameter],
    supplied: dict[str, Any],
    answers: dict[str, Any] | None = None,
    contextual_choices: dict[str, list[Any]] | None = None,
) -> AlgorithmResolution:
    """Resolve only explicit, persisted, or QGIS-documented values; never guess."""
    answers = answers or {}
    contextual_choices = contextual_choices or {}
    known = {item.name for item in specifications}
    unknown = sorted(set(supplied) - known)
    if unknown:
        raise TaskError(
            "INVALID_PARAMETERS", "Unknown algorithm parameter names",
            evidence={"unknown_parameters": unknown, "allowed_parameters": sorted(known)},
        )
    by_name = {item.name: item for item in specifications}
    parameters = {}
    normalizations = []
    for name, value in supplied.items():
        normalized = normalize_supplied_value(by_name[name], value)
        parameters[name] = normalized
        if normalized != value or type(normalized) is not type(value):
            normalizations.append({
                "parameter": name,
                "from": value,
                "to": normalized,
                "reason": "live_help_type_or_choice",
            })
    resolutions = []
    questions = []
    for spec in specifications:
        if spec.name in supplied:
            resolutions.append(ParameterResolution(
                parameter=spec.name, status="provided", source="request",
                value=parameters[spec.name],
            ))
            continue
        question_id = f"q_{plan_id[:16]}_{spec.name}"[:128]
        if question_id in answers:
            question = StructuredQuestion(
                id=question_id,
                parameter=spec.name,
                answer_type=_answer_type(spec),
                prompt=f"请提供 {spec.description}（{spec.name}）。",
                choices=spec.choices,
                multiple=spec.multiple,
            )
            value = _validate_answer(question, answers[question_id])
            parameters[spec.name] = value
            resolutions.append(ParameterResolution(
                parameter=spec.name, status="answered", source="answer", value=value,
            ))
            continue
        if spec.has_default:
            resolutions.append(ParameterResolution(
                parameter=spec.name, status="default", source="qgis_default",
                value=spec.default,
            ))
            continue
        if not spec.required:
            resolutions.append(ParameterResolution(
                parameter=spec.name, status="optional", source="omitted",
            ))
            continue
        choices = spec.choices
        if contextual_choices.get(spec.name):
            choices = [
                {"value": item, "label": str(item)}
                for item in contextual_choices[spec.name]
            ]
        question = StructuredQuestion(
            id=question_id,
            parameter=spec.name,
            answer_type=_answer_type(spec),
            prompt=f"算法 {algorithm} 的必选参数“{spec.description}”（{spec.name}）没有默认值，请指定。",
            choices=choices,
            multiple=spec.multiple,
        )
        questions.append(question)
        resolutions.append(ParameterResolution(
            parameter=spec.name, status="unresolved", source="missing",
        ))
    return AlgorithmResolution(
        plan_id=plan_id,
        algorithm=algorithm,
        step_id=step_id,
        status="WAITING_FOR_USER" if questions else "READY",
        parameters=parameters,
        resolutions=resolutions,
        questions=questions,
        normalizations=normalizations,
    )
