"""Serialized durable execution boundary between MCP tools and the QGIS worker."""

from __future__ import annotations

import asyncio
import base64
import copy
import difflib
import hashlib
import hmac
import json
import random
import re
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

from pydantic import ValidationError

from .algorithm_parameters import (
    AlgorithmParameter,
    AlgorithmResolution,
    canonicalize_parameter_names,
    destination_asset_kind,
    normalize_algorithm_help,
    resolve_parameters,
    validate_processing_expressions,
)
from .algorithm_rules import clip_boundary_rule, family_checks, gdal_translate_extra
from .bridge import QgisBridge, WorkerError
from .contract_policy import MANAGED_OUTPUT_GUIDANCE
from .contracts import (
    Deliverable,
    Output,
    StepContract,
    TaskContract,
    validate_task_contract,
    verifier_catalog,
)
from .observability import TraceExporter
from .task_store import (
    TaskError,
    TaskStore,
    canonical,
    fingerprint,
    state_root,
    sync_tree,
    temporary_output_root,
)

CONTEXT = {"task_id", "step_id", "continuation_token"}
READ_ACTIONS = {
    "project": {"info"},
    "layers": {"list"},
    "layout": {"list"},
    "vector_data": {"statistics"},
}
DEFAULT_ACTION = {"project": "info", "layers": "list", "layout": "create"}
EXTENSIONS = {
    "vector": ".gpkg",
    "raster": ".tif",
    "project": ".qgz",
    "image": ".png",
    "pdf": ".pdf",
    "style": ".qml",
    "template": ".qpt",
}

# A user may state output replacement as part of the natural-language goal,
# while a compact MCP client can omit the matching boolean/structured field.
# Recognize only unequivocal authorization. The resulting permission remains
# limited to exact declared final deliverables; temporary paths, directories
# and symlinks are never covered.
OVERWRITE_DENIAL = re.compile(
    r"(?:不(?:要|得|可)|禁止|先询问|需要询问).{0,12}覆盖|"
    r"\b(?:do\s+not|never|ask\s+(?:me\s+)?before)\s+overwrite\b",
    re.IGNORECASE,
)
OVERWRITE_AUTHORIZATION = re.compile(
    r"(?:同名|重名|已有|已存在|重复|目标|输出).{0,16}(?:文件|路径)?.{0,8}"
    r"(?:请|可|就|则)?(?:直接|自动|允许|可以)?覆盖|"
    r"(?:直接|自动|允许|可以).{0,8}覆盖(?:同名|重名|已有|已存在|重复|目标|输出)?|"
    r"\b(?:overwrite|replace)\s+(?:any\s+)?(?:existing|same[- ]named|duplicate)\s+"
    r"(?:output\s+)?files?\b",
    re.IGNORECASE,
)


def explicitly_authorizes_output_overwrite(text):
    """Return true only for a direct, unnegated output-replacement instruction."""
    return bool(text and not OVERWRITE_DENIAL.search(text) and OVERWRITE_AUTHORIZATION.search(text))


def is_read(operation, arguments):
    return operation in {"algorithms", "algorithm_info", "features", "feature_info", "layer_info"} or arguments.get(
        "action", DEFAULT_ACTION.get(operation)
    ) in READ_ACTIONS.get(operation, set())


class TaskCoordinator:
    reliable = True

    def __init__(self, bridge=None, root=None, *, correction_limit=3,
                 terminal_retention_days=10, failed_retention_days=60):
        if correction_limit < 1:
            raise ValueError("correction_limit must be positive")
        self.bridge = bridge or QgisBridge()
        self.correction_limit = correction_limit
        self.root = Path(root) if root is not None else state_root()
        self.cleanup_report = TaskStore.cleanup_expired(
            self.root, terminal_retention_days=terminal_retention_days,
            failed_retention_days=failed_retention_days,
        )
        self.store = None
        self.lock = asyncio.Lock()
        self.traces = TraceExporter()
        self.contract_catalog_delivered = False

    async def close(self):
        self.traces.close()
        await self.bridge.close()
        if self.store:
            self.store.close()
            self.store = None

    async def fresh_worker(self):
        timeout = self.bridge.timeout
        await self.bridge.close(abort=True)
        self.bridge = QgisBridge(timeout)

    def require_task(self, task_id=None):
        if self.store is None or (task_id is not None and self.store.task_id != task_id):
            raise TaskError(
                "TASK_REQUIRED",
                "Begin or resume the requested task",
                next_action="task_begin or task_recover",
            )
        return self.store

    @staticmethod
    def default_map_title(goal):
        """Use a concise first sentence when no reader-facing title was supplied."""
        return re.split(r"(?<=[.!?。！？])\s+", goal.strip(), maxsplit=1)[0] or goal

    def issue_continuation(self, purpose="continue", step_id=None, *, state_version=None):
        store = self.require_task()
        task = store.task()
        payload = {
            "task_id": store.task_id,
            "state_version": task["state_version"] if state_version is None else state_version,
            "contract_version": task["contract_version"],
            "purpose": purpose,
        }
        if step_id is not None:
            payload["step_id"] = step_id
        encoded = base64.urlsafe_b64encode(canonical(payload).encode()).decode().rstrip("=")
        signature = hashlib.sha256(
            f"smart-qgis-continuation-v1:{store.task_id}:{encoded}".encode()
        ).hexdigest()[:32]
        return encoded + "." + signature

    def issue_route(self, next_call):
        """Persist an exact next call and return a short, copy-safe opaque handle."""
        store = self.require_task()
        task = store.task()
        arguments = {
            key: value
            for key, value in next_call["arguments"].items()
            if key not in {"task_id", "continuation_token"}
        }
        route_id = uuid.uuid4().hex
        store.event("ROUTE_ISSUED", {
            "route_id": route_id,
            "state_version": task["state_version"],
            "contract_version": task["contract_version"],
            "next_tool": next_call["tool"],
            "arguments": arguments,
        })
        return route_id

    def compact_next_call(self, next_call):
        return {
            "tool": "task_execute",
            "arguments": {"continuation_token": self.issue_route(next_call)},
        }

    def compact_route_handle(self):
        """Return or rebuild the one current action route for token-free MCP clients."""
        store = self.require_task()
        task = store.task()
        completed = store.db.execute(
            "SELECT body FROM events WHERE kind='ROUTE_COMPLETED' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if task["status"] == "COMPLETED" and completed:
            return json.loads(completed["body"])["route_id"]

        issued = list(store.db.execute(
            "SELECT body FROM events WHERE kind='ROUTE_ISSUED' ORDER BY sequence DESC"
        ))
        for row in issued:
            route = json.loads(row["body"])
            if (
                route.get("state_version") == task["state_version"]
                and route.get("contract_version") == task["contract_version"]
            ):
                return route["route_id"]

        for row in issued:
            route = json.loads(row["body"])
            target = route.get("next_tool")
            arguments = dict(route.get("arguments", {}))
            if target == "step_execute":
                step_id = arguments.get("step_id")
                step = store.db.execute(
                    "SELECT status FROM steps WHERE id=?", (step_id,)
                ).fetchone()
                if not step or step["status"] != "PLANNED":
                    continue
                continuation = self.issue_continuation("execute", step_id)
            elif target == "presentation_continue":
                if not self.presentation_pending():
                    continue
                continuation = self.issue_continuation()
            elif target in {"workflow_run", "plan_execute"}:
                if task["status"] != "READY":
                    continue
                continuation = self.issue_continuation()
            else:
                continue
            arguments.update({
                "task_id": store.task_id,
                "continuation_token": continuation,
            })
            return self.issue_route({"tool": target, "arguments": arguments})

        planned = list(store.db.execute(
            "SELECT id FROM steps WHERE status='PLANNED' ORDER BY rowid"
        ))
        if len(planned) == 1:
            step_id = planned[0]["id"]
            return self.issue_route({
                "tool": "step_execute",
                "arguments": {
                    "task_id": store.task_id,
                    "step_id": step_id,
                    "continuation_token": self.issue_continuation("execute", step_id),
                },
            })
        raise TaskError(
            "NO_PENDING_ACTION",
            "The task has no unique server-bound action to execute",
            next_action="Prepare the next Processing algorithm or inspect task_diagnose",
        )

    def compact_arguments(self, operation, arguments):
        """Inject machine-owned state handles hidden from the compact MCP schema."""
        arguments = dict(arguments)
        if operation == "project" and "action" not in arguments:
            arguments["action"] = "info"
        if operation == "task_update" and "instruction" in arguments:
            # The public task_update tool has no internal clarification terms.
            arguments["question"] = "User-requested task update"
            arguments["user_response"] = arguments.pop("instruction")
        if operation == "prepare_algorithm" and "step_id" not in arguments:
            store = self.require_task(arguments.get("task_id"))
            base = "processing_" + "".join(
                char if char.isalnum() else "_" for char in arguments["algorithm"]
            )[:96]
            existing = {row[0] for row in store.db.execute("SELECT id FROM steps")}
            step_id, number = base, 2
            while step_id in existing:
                suffix = f"_{number}"
                step_id = base[:128 - len(suffix)] + suffix
                number += 1
            arguments["step_id"] = step_id
            # A failed operation is repaired only when its declared logical
            # outputs match the new request; clients never name that step.
            failed = [row["id"] for row in store.db.execute(
                "SELECT id,status,body FROM steps ORDER BY rowid DESC"
            ) if row["status"] in {"FAILED", "INVALIDATED"}
                      and set(arguments.get("outputs", {}).values()).intersection(
                          output.id for output in StepContract.model_validate_json(row["body"]).outputs
                      )]
            if failed:
                arguments["repairs_step"] = failed[0]
        if operation == "task_execute" and not arguments.get("continuation_token"):
            arguments["continuation_token"] = self.compact_route_handle()
            return arguments
        if operation == "task_recover" and arguments.get("retry_step") and not arguments.get(
            "continuation_token"
        ):
            task_id = arguments.get("task_id")
            if self.store is None or self.store.task_id != task_id:
                candidate = TaskStore(self.root, task_id)
                if self.store:
                    if self.store.unresolved():
                        candidate.close()
                        raise TaskError(
                            "ATTEMPT_UNRESOLVED", "Reconcile the attached task first"
                        )
                    self.store.close()
                self.store = candidate
            arguments["continuation_token"] = self.issue_continuation()
            return arguments
        if operation in {
            "prepare_algorithm", "task_answer", "task_invalidate",
            "task_update", "task_restart", "task_stop",
        } and not arguments.get("continuation_token"):
            task_id = arguments.get("task_id")
            if task_id is not None:
                self.require_task(task_id)
                arguments["continuation_token"] = self.issue_continuation()
        return arguments

    def compact_response(self, value):
        """Remove machine handles from model-visible compact MCP responses."""
        if isinstance(value, list):
            return [self.compact_response(item) for item in value]
        if isinstance(value, str):
            # Durable internals may occur inside nested failure evidence.  A
            # compact client must only ever be directed to its public tools.
            for private, public in (
                ("task_recover", "task_resume"),
                ("task_begin", "task_start"),
                ("inspect_data", "data_info"),
            ):
                value = value.replace(private, public)
            return value
        if not isinstance(value, dict):
            return value
        result = {
            key: self.compact_response(item)
            for key, item in value.items()
            if key != "continuation_token"
        }
        next_call = result.get("next_call")
        if isinstance(next_call, dict) and next_call.get("tool") in {
            "step_execute", "task_execute",
        }:
            result["next_call"] = {"tool": "task_execute", "arguments": {}}
        return result

    def presentation_options(self):
        """Read durable, non-contract display choices supplied to ``task_start``."""
        row = self.require_task().db.execute(
            "SELECT body FROM events WHERE kind='TASK_PRESENTATION' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["body"]) if row else {}

    @staticmethod
    def deliverable_destination(item):
        """Return the exact final path governed by an output decision, if any."""
        if item.get("path"):
            return str(Path(item["path"]).expanduser().resolve())
        if item.get("directory") and item.get("kind") in EXTENSIONS:
            return str(
                (Path(item["directory"]).expanduser() /
                 (item["id"] + EXTENSIONS[item["kind"]])).resolve()
            )
        return None

    def pending_output_conflict(self):
        """Return the newest failed output conflict that lacks a later decision."""
        store = self.require_task()
        for row in store.db.execute(
            "SELECT a.step_id,a.failure FROM attempts a JOIN steps s ON s.id=a.step_id "
            "WHERE a.status='FAILED' AND s.status='FAILED' ORDER BY a.created DESC"
        ):
            failure = json.loads(row["failure"] or "{}")
            if failure.get("code") != "OUTPUT_EXISTS":
                continue
            path = (failure.get("evidence") or {}).get("path")
            if path and self.output_conflict_resolution(path) is None:
                return {"step_id": row["step_id"], "path": str(Path(path).resolve())}
        return None

    def authorize_declared_output_overwrites(self, deliverables, *, source):
        """Persist one explicit task-start authorization per exact final path."""
        with self.require_task().transaction():
            for item in deliverables:
                destination = self.deliverable_destination(item)
                if destination is not None:
                    self.store.event("OUTPUT_CONFLICT_RESOLUTION", {
                        "path": destination,
                        "action": "overwrite",
                        "source": source,
                    })

    def presentation_pending(self):
        task = self.require_task().task()
        maps = [item for item in task["deliverables"]
                if item["kind"] in {"image", "pdf", "project", "layout"}]
        assets = self.assets()
        data_ids = self.presentation_data_ids(assets)
        return bool(maps and data_ids
                    and all(item in assets and assets[item].get("layer_id") for item in data_ids)
                    and any(item["id"] not in assets for item in maps))

    def presentation_data_ids(self, assets=None):
        """Return retained map layers for either analysis-plus-map or map-only tasks."""
        assets = self.assets() if assets is None else assets
        declared = [item["id"] for item in self.require_task().task()["deliverables"]
                    if item["kind"] in {"vector", "raster"}]
        if declared:
            return declared
        # A map-only workflow has no vector/raster deliverable.  Its invalidated
        # layout still records exactly which loaded layer assets are safe to reuse.
        for row in reversed(list(self.store.db.execute("SELECT body FROM steps ORDER BY rowid"))):
            step = StepContract.model_validate_json(row["body"])
            if step.operation == "layout":
                return [key for key in step.inputs if key in assets and assets[key].get("layer_id")]
        return []

    def read_continuation(self, token, *, purpose="continue", step_id=None):
        store = self.require_task()
        try:
            encoded, signature = token.split(".", 1)
            expected = hashlib.sha256(
                f"smart-qgis-continuation-v1:{store.task_id}:{encoded}".encode()
            ).hexdigest()[:32]
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        except (AttributeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TaskError(
                "INVALID_CONTINUATION", "Continuation token is invalid",
                next_action="Use the latest continuation_token returned by this task",
            ) from exc
        if payload.get("task_id") != store.task_id:
            raise TaskError("INVALID_CONTINUATION", "Continuation token belongs to another task")
        if payload.get("purpose") != purpose or (
            step_id is not None and payload.get("step_id") != step_id
        ):
            raise TaskError(
                "INVALID_CONTINUATION", "Continuation token does not authorize this next action",
                evidence={"expected_purpose": purpose, "step_id": step_id},
                next_action="Use the token from the immediately preceding workflow response",
            )
        return payload

    def route_record(self, token):
        if not isinstance(token, str) or len(token) != 32:
            raise TaskError(
                "INVALID_CONTINUATION", "The task_execute handle is invalid",
                next_action="Copy the latest 32-character token unchanged",
            )
        try:
            int(token, 16)
        except ValueError as exc:
            raise TaskError(
                "INVALID_CONTINUATION", "The task_execute handle is invalid",
                next_action="Copy the latest 32-character token unchanged",
            ) from exc
        store = self.require_task()
        for row in store.db.execute(
            "SELECT body FROM events WHERE kind='ROUTE_ISSUED' ORDER BY sequence DESC"
        ):
            payload = json.loads(row["body"])
            if payload.get("route_id") == token:
                return payload
        raise TaskError(
            "INVALID_CONTINUATION", "The task_execute handle is unknown",
            next_action="Copy the latest 32-character token unchanged",
        )

    def routed_arguments(self, token):
        payload = self.route_record(token)
        store = self.require_task()
        task = store.task()
        if (
            payload.get("state_version") != task["state_version"]
            or payload.get("contract_version") != task["contract_version"]
        ):
            raise TaskError(
                "STALE_CONTINUATION",
                "The action token is older than the current task state",
                next_action="Use the latest task_execute token returned by the service",
            )
        target = payload.get("next_tool")
        allowed = {
            "task_contract_submit", "workflow_run", "step_execute", "task_finish", "plan_execute",
            "presentation_continue",
        }
        if target not in allowed:
            raise TaskError(
                "INVALID_CONTINUATION",
                "The action token does not contain an allowed compact next action",
            )
        arguments = dict(payload.get("arguments") or {})
        step_id = arguments.get("step_id")
        purpose = "execute" if target == "step_execute" else "continue"
        arguments.update({
            "task_id": store.task_id,
            "continuation_token": self.issue_continuation(
                purpose, step_id, state_version=payload["state_version"]
            ),
        })
        return target, arguments

    def authorize_continuation(self, arguments, *, purpose="continue", step_id=None):
        payload = self.read_continuation(
            arguments.get("continuation_token"), purpose=purpose, step_id=step_id
        )
        return {
            "expected_state_version": payload["state_version"],
            "contract_version": payload["contract_version"],
            "idempotency_key": uuid.uuid5(
                uuid.NAMESPACE_URL,
                "smart-qgis:" + arguments["continuation_token"],
            ).hex,
        }

    def assets(self):
        store = self.require_task()
        task = store.task()
        assets = {
            key: {"path": item["path"], "kind": item["kind"], "input": True}
            for key, item in task["inputs"].items()
        }
        assets.update((task["checkpoint"] or {}).get("assets", {}))
        invalid = {
            row["id"] for row in store.db.execute("SELECT id FROM steps WHERE status='INVALIDATED'")
        }
        return {key: value for key, value in assets.items() if value.get("step_id") not in invalid}

    def status(self, include_details=False):
        store = self.require_task()
        task = store.task()
        correction_failures = self.correction_failures()
        intervention_reason = self.user_intervention_reason()
        checkpoint = task["checkpoint"]
        if checkpoint and not include_details:
            checkpoint = {
                "project": checkpoint["project"],
                "environment": checkpoint["environment"],
                "fingerprinted_sources": len(checkpoint.get("fingerprints", [])),
            }
        attempts = [
            dict(row)
            for row in store.db.execute(
                "SELECT id,step_id,status,failure FROM attempts ORDER BY created"
            )
        ]
        for attempt in attempts:
            failure = json.loads(attempt["failure"]) if attempt["failure"] else None
            attempt["failure"] = (
                failure
                if include_details or not failure
                else {
                    "code": failure.get("code"),
                    "message": failure.get("message"),
                    "phase": failure.get("phase", "unknown"),
                    "retryable": failure.get("retryable", False),
                }
            )
        result = {
            "task_id": store.task_id,
            "goal": task["goal"],
            "status": task["status"],
            "continuation_token": self.issue_continuation(),
            "correction_budget": {"rejected_submissions": correction_failures, "limit": self.correction_limit,
                                  "requires_user_input": correction_failures >= self.correction_limit},
            "user_intervention_required": intervention_reason is not None,
            "intervention_reason": intervention_reason,
            "input_versions": {key: item["fingerprint"]["digest"] for key, item in task["inputs"].items()},
            "assets": self.assets(),
            "checkpoint": checkpoint,
            "steps": [
                {
                    **dict(row),
                    "dependencies": json.loads(row["dependencies"]),
                }
                for row in store.db.execute("SELECT id,status,dependencies FROM steps")
            ],
            "attempts": attempts,
            "questions": store.pending_questions(),
        }
        if include_details:
            result["diagnostics"] = {
                "state_version": task["state_version"],
                "contract_version": task["contract_version"],
            }
        return result

    def compact_status(self):
        status = self.status()
        return {
            "task_id": status["task_id"],
            "status": status["status"],
            "continuation_token": status["continuation_token"],
            "correction_budget": status["correction_budget"],
            "user_intervention_required": status["user_intervention_required"],
            "intervention_reason": status["intervention_reason"],
            "assets": {
                key: {
                    name: value[name]
                    for name in ("kind", "input", "path", "layer_id", "layout", "raster_summary") if name in value
                }
                for key, value in status["assets"].items()
            },
            "steps": status["steps"],
            "attempts": [
                {
                    "step_id": item["step_id"],
                    "status": item["status"],
                    **({"failure": item["failure"]} if item.get("failure") else {}),
                }
                for item in status["attempts"]
            ],
            "questions": status["questions"],
        }

    def task_delta(self, *, continuation_token=None):
        """Small mutation response; task_diagnose owns full state retrieval."""
        store = self.require_task()
        return {
            "task_id": store.task_id,
            "status": store.task()["status"],
            "continuation_token": continuation_token or self.issue_continuation(),
        }

    def deliverable_assets(self):
        task = self.require_task().task()
        assets = self.assets()
        return {
            item["id"]: {
                key: assets[item["id"]][key]
                for key in ("kind", "path", "layer_id", "layout", "raster_summary")
                if key in assets[item["id"]]
            }
            for item in task["deliverables"]
            if item["id"] in assets
        }

    def next_call_after_execution(self, continuation_token):
        task = self.require_task().task()
        delivered = self.deliverable_assets()
        if all(item["id"] in delivered for item in task["deliverables"]):
            return {
                "tool": "task_finish",
                "arguments": {
                    "task_id": self.store.task_id,
                    "continuation_token": continuation_token,
                },
            }
        return {
            "tool": "step_prepare",
            "arguments": {
                "task_id": self.store.task_id,
                "continuation_token": continuation_token,
            },
        }

    def execution_delta(self, outcome, *, include_details=False):
        result = {
            key: outcome[key]
            for key in (
                "result", "assets", "task_id", "step_id", "attempt_id",
                "continuation_token",
            )
            if key in outcome
        }
        reports = outcome.get("validation", [])
        result["validation"] = (
            reports
            if include_details
            else [
                {"id": report["id"], "status": report["status"]}
                for report in reports
            ]
        )
        result["next_call"] = self.next_call_after_execution(
            outcome["continuation_token"]
        )
        return result

    def current_contract(self):
        store = self.require_task()
        row = store.db.execute(
            "SELECT body FROM contracts WHERE version=?", (store.task()["contract_version"],)
        ).fetchone()
        if row is None:
            raise TaskError(
                "CONTRACT_REQUIRED", "Submit a task contract", next_action="task_contract_submit"
            )
        return TaskContract.model_validate_json(row[0])

    def correction_failures(self):
        if self.store is None:
            # No task exists yet, so there is no durable task-scoped correction
            # budget to consume. Schema feedback before task creation must not
            # poison the next independent task.
            return 0
        return self.store.db.execute(
            "SELECT count(*) FROM events WHERE kind='CORRECTION_REJECTED' AND sequence > "
            "COALESCE((SELECT max(sequence) FROM events WHERE kind='CORRECTION_RESET'),0)"
        ).fetchone()[0]

    def timeout_needs_user(self):
        if self.store is None:
            return False
        row = self.store.db.execute(
            "SELECT kind FROM events WHERE kind IN "
            "('USER_INTERVENTION_REQUIRED','USER_CLARIFICATION','QUESTION_ANSWERED') "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return bool(row and row[0] == "USER_INTERVENTION_REQUIRED")

    def user_intervention_reason(self):
        if self.timeout_needs_user():
            return "tool_timeout"
        if self.correction_failures() >= self.correction_limit:
            return "correction_limit"
        return None

    def record_correction_failure(self, operation, error, arguments=None):
        arguments = arguments if isinstance(arguments, dict) else {}
        code = error.payload['code']
        diagnostic = {
            "task_diagnose", "contract_get", "contract_help", "inspect_data", "layer_info",
            "task_recover", "task_update", "task_answer",
        }
        invalid_execution = {
            "INVALID_ARGUMENTS", "INVALID_PARAMETERS", "CONTRACT_REQUIRED", "STEP_CONTRACT_REQUIRED",
            "STEP_MISMATCH", "INVALID_REPAIR", "UNKNOWN_STEP", "UNKNOWN_OPERATION",
        }
        discovery_rejection = operation == "algorithm_info" and code in {
            "INVALID_ARGUMENTS", "INVALID_PARAMETERS", "OPERATION_FAILED", "UNKNOWN_ALGORITHM",
        }
        if code in {"CLARIFICATION_REQUIRED", "USER_INTERVENTION_REQUIRED", "WORKER_TIMEOUT", "TASK_AMBIGUOUS", "OUTPUT_EXISTS"} or operation in diagnostic or (is_read(operation, arguments) and not discovery_rejection):
            return
        if operation not in {
            "task_begin", "task_start", "task_execute", "step_execute", "task_contract_submit",
            "step_prepare", "workflow_run", "step_contract_submit", "prepare_algorithm",
            "algorithm_info",
        } and code not in invalid_execution:
            return
        if self.store:
            self.store.event("CORRECTION_REJECTED", {"code": error.payload['code']})
        else:
            return
        count = self.correction_failures()
        error.payload["correction_budget"] = {"rejected_submissions": count, "limit": self.correction_limit, "remaining": max(0, self.correction_limit-count)}
        if count >= self.correction_limit:
            error.payload["next_action"] = "Stop automatic correction. Explain the unresolved problem and ask the user how to proceed. Record their actual answer with task_update before continuing."

    def correction_gate(self, operation, arguments):
        arguments = arguments if isinstance(arguments, dict) else {}
        if self.store is None and operation in {"algorithm_info", "algorithms"}:
            raise TaskError(
                "TASK_REQUIRED",
                "Create or recover the task before discovering Processing algorithms",
                phase="preparation",
                next_action=(
                    "For a new request call task_start first. For an existing task call "
                    "task_recover with its task_id, then use algorithm_info only if the "
                    "returned route requires Processing discovery."
                ),
            )
        if (
            self.store is not None
            and self.correction_failures() < self.correction_limit
            and operation in {"task_begin", "task_start"}
            and self.store.task()["status"] not in {"COMPLETED", "CANCELLED"}
        ):
            raise TaskError(
                "ACTIVE_TASK_EXISTS",
                "An unfinished task is already attached; a replacement task cannot bypass its recovery state",
                phase="recovery",
                evidence={"task_id": self.store.task_id, "status": self.store.task()["status"]},
                next_action="Use task_diagnose or task_recover for the existing task. Record actual user guidance before continuing when requested.",
            )
        diagnostic_allowed = {
            "task_diagnose", "contract_get", "contract_help", "inspect_data", "layer_info",
            "task_update", "task_answer",
        }
        if self.timeout_needs_user() and operation not in diagnostic_allowed:
            raise TaskError(
                "USER_INTERVENTION_REQUIRED",
                "A QGIS tool call timed out; wait for user guidance before continuing",
                phase="recovery",
                next_action="Show the timeout and current task status to the user. "
                            "After their actual instruction, record it with task_update or task_answer.",
            )
        # A cached response is not another execution or correction attempt.
        # The server derives its private idempotency key from the opaque token.
        if (
            self.store
            and arguments.get("task_id") == self.store.task_id
            and isinstance(arguments.get("continuation_token"), str)
            and operation == "step_execute"
        ):
            try:
                authorization = self.authorize_continuation(
                    arguments, purpose="execute", step_id=arguments.get("step_id")
                )
            except TaskError:
                pass
            else:
                cached = self.store.db.execute(
                    "SELECT status FROM attempts WHERE idempotency_key=?",
                    (authorization["idempotency_key"],),
                ).fetchone()
                if cached and cached[0] == "COMMITTED":
                    return
        post_limit_allowed = {*diagnostic_allowed, "task_recover"}
        if self.correction_failures() >= self.correction_limit and operation not in post_limit_allowed:
            raise TaskError(
                "CLARIFICATION_REQUIRED", "Preparation or execution-argument correction limit reached; automatic correction is stopped",
                phase="preflight", evidence={"rejected_submissions": self.correction_failures(), "limit": self.correction_limit},
                next_action="Stop retrying. Explain the unresolved input or parameter choice and the latest error to the user. After their actual answer, call task_update. Do not create another task to evade this limit.",
            )

    async def clarify(self, a):
        if self.store is None:
            if a.get("task_id") is not None:
                raise TaskError("TASK_REQUIRED", "Resume the task before recording its clarification")
            return {"status": "PREPARING", "correction_budget": {"rejected_submissions": 0, "limit": self.correction_limit},
                    "note": "No task exists yet; start a task after the user's clarification"}
        store = self.require_task(a["task_id"])
        store.require_version(a["expected_state_version"])
        pending_conflict = self.pending_output_conflict()
        resolution = a.get("output_conflict")
        resolution_source = "structured_task_update"
        if (
            pending_conflict
            and resolution is None
            and explicitly_authorizes_output_overwrite(a.get("user_response", ""))
        ):
            # Bind the user's explicit plain-language answer to the exact path
            # reported by the server; the client never chooses another path.
            resolution = {"path": pending_conflict["path"], "action": "overwrite"}
            a = {**a, "output_conflict": resolution}
            resolution_source = "explicit_task_update_instruction"
        if pending_conflict and resolution is None:
            path = pending_conflict["path"]
            raise TaskError(
                "OUTPUT_CONFLICT_DECISION_REQUIRED",
                "The pending output conflict requires the structured output_conflict field; instruction text alone is not authorization",
                phase="authorization",
                evidence={
                    "path": path,
                    "decision_calls": {
                        "retry": {
                            "tool": "task_update",
                            "arguments": {
                                "task_id": store.task_id,
                                "instruction": "The user confirmed that this exact file was removed",
                                "output_conflict": {"path": path, "action": "retry"},
                            },
                        },
                        "overwrite": {
                            "tool": "task_update",
                            "arguments": {
                                "task_id": store.task_id,
                                "instruction": "The user explicitly authorized replacement of this exact file",
                                "output_conflict": {"path": path, "action": "overwrite"},
                            },
                        },
                    },
                },
                next_action=(
                    "Ask whether this exact file may be replaced. After explicit approval, call "
                    "task_update with output_conflict.path copied exactly and action=overwrite."
                ),
            )
        if resolution is not None:
            if pending_conflict is None:
                raise TaskError(
                    "OUTPUT_CONFLICT_NOT_PENDING",
                    "No unresolved OUTPUT_EXISTS failure is waiting for an overwrite decision",
                    phase="authorization",
                    next_action="Use task_diagnose to inspect the current failure before submitting an output decision",
                )
            if resolution["path"] != pending_conflict["path"]:
                raise TaskError(
                    "OUTPUT_CONFLICT_PATH_MISMATCH",
                    "The overwrite decision must use the exact path reported by OUTPUT_EXISTS",
                    phase="authorization",
                    evidence={"expected_path": pending_conflict["path"]},
                    next_action="Copy expected_path unchanged into task_update.output_conflict.path",
                )
            if resolution["action"] == "retry" and Path(resolution["path"]).exists():
                raise TaskError(
                    "OUTPUT_STILL_EXISTS",
                    "Retry without overwrite is allowed only after the existing file was removed",
                    phase="authorization",
                    evidence={"path": resolution["path"]},
                    next_action="Remove the exact file or explicitly authorize overwrite, then call task_update",
                )
        map_coordinate_crs = self.coordinate_crs(a.get("map_coordinate_crs"))
        annotation_crs = await self.validate_coordinate_crs(map_coordinate_crs)
        annotation_format = (a.get("map_coordinate_annotations") or {}).get("format", "auto")
        if (
            annotation_format in {"degree_minute", "degree_minute_second"}
            and annotation_crs is not None
            and not annotation_crs.get("geographic", False)
        ):
            raise TaskError(
                "INVALID_COORDINATE_FORMAT",
                "Degree-based coordinate labels require a geographic annotation CRS",
            )
        if (
            (a.get("map_coordinate_annotations") or {}).get("cardinal_directions") is True
            and annotation_crs is not None
            and not annotation_crs.get("geographic", False)
        ):
            raise TaskError(
                "INVALID_COORDINATE_FORMAT",
                "E/W/N/S coordinate suffixes require a geographic annotation CRS",
            )
        map_crs = self.coordinate_crs(a.get("map_crs"))
        await self.validate_coordinate_crs(map_crs, label="map display CRS")
        operational_changed = any(a.get(name) is not None for name in (
            "services", "layer_operations", "data_operations", "project_update",
        ))
        presentation_changed = any(a.get(name) is not None for name in
               ("map_layers", "map_title", "legend_title", "map_language", "show_legend_title", "north_arrow",
                "page_orientation", "map_crs", "map_frame", "map_elements", "map_coordinate_annotations",
                "map_raster_styles", "map_vector_styles", "map_coordinate_crs", "basemap"))
        if operational_changed and presentation_changed:
            raise TaskError(
                "UPDATE_SPLIT_REQUIRED",
                "Apply project/data operations and presentation changes in separate task_update calls",
                next_action="Execute the project operation first, then submit the map revision",
            )
        if (a.get("project_update") or {}).get("crs"):
            await self.validate_coordinate_crs(a["project_update"]["crs"])
        with store.transaction():
            store.event("USER_CLARIFICATION", {"question": a["question"], "user_response": a["user_response"]})
            if a.get("output_conflict") is not None:
                store.event("OUTPUT_CONFLICT_RESOLUTION", {
                    **a["output_conflict"], "source": resolution_source,
                })
            if presentation_changed:
                store.event("TASK_PRESENTATION", {
                    **self.presentation_options(),
                    **({"layers": a["map_layers"]} if a.get("map_layers") is not None else {}),
                    **({"title": a["map_title"]} if a.get("map_title") is not None else {}),
                    **({"legend_title": a["legend_title"]} if a.get("legend_title") is not None else {}),
                    **({"map_language": a["map_language"]} if a.get("map_language") is not None else {}),
                    **({"show_legend_title": a["show_legend_title"]}
                       if a.get("show_legend_title") is not None else {}),
                    **({"north_arrow": a["north_arrow"]}
                       if a.get("north_arrow") is not None else {}),
                    **({"page_orientation": a["page_orientation"]}
                       if a.get("page_orientation") is not None else {}),
                    **({"map_crs": map_crs} if map_crs is not None else {}),
                    **({"map_frame": a["map_frame"]}
                       if a.get("map_frame") is not None else {}),
                    **({"map_elements": a["map_elements"]}
                       if a.get("map_elements") is not None else {}),
                    **({"coordinate_annotations": a["map_coordinate_annotations"]}
                       if a.get("map_coordinate_annotations") is not None else {}),
                    **({"coordinate_crs": map_coordinate_crs}
                       if map_coordinate_crs is not None else {}),
                    **({"raster_styles": a["map_raster_styles"]}
                       if a.get("map_raster_styles") is not None else {}),
                    **({"vector_styles": a["map_vector_styles"]}
                       if a.get("map_vector_styles") is not None else {}),
                    **({"basemap": a["basemap"]} if a.get("basemap") is not None else {}),
                })
            if operational_changed:
                store.event("TASK_OPERATIONS", {
                    "services": a.get("services") or {},
                    "layer_operations": a.get("layer_operations") or [],
                    "data_operations": a.get("data_operations") or [],
                    "project_update": a.get("project_update"),
                })
            store.event("CORRECTION_RESET", {"source": "user_clarification"})
            store.db.execute(
                "UPDATE task SET status=?,state_version=state_version+1",
                ("READY" if operational_changed else store.task()["status"],),
            )
        if resolution is not None:
            # Resolving a file conflict is an authorization decision, not a
            # request to rebuild valid analysis.  Re-plan only the failed
            # operation and return the normal public task_execute route.
            return await self.resume({
                "task_id": store.task_id,
                "retry_step": pending_conflict["step_id"],
                "continuation_token": self.issue_continuation(),
                "expected_state_version": store.task()["state_version"],
                "reason": "User resolved the exact final-output conflict",
            })
        if operational_changed:
            route = self.compact_next_call({
                "tool": "workflow_run",
                "arguments": {
                    "task_id": store.task_id,
                    "continuation_token": self.issue_continuation(),
                    "workflow": "project_operations",
                    "services": a.get("services") or {},
                    "layer_operations": a.get("layer_operations") or [],
                    "data_operations": a.get("data_operations") or [],
                    "project_update": a.get("project_update"),
                },
            })
            return {
                **self.task_delta(),
                "status": "READY",
                "completion_state": "NOT_COMPLETED",
                "next_call": route,
                "next_action": "Call task_execute once; the server will checkpoint each requested operation.",
            }
        # A reader-facing revision must never re-run correct GIS analysis.  If
        # a completed layout exists, invalidate that layout and its dependent
        # exports as one bounded presentation-only repair.
        if presentation_changed:
            layout_step = next((row["id"] for row in reversed(list(store.db.execute(
                "SELECT id,status,body FROM steps ORDER BY rowid"
            ))) if row["status"] == "COMMITTED"
                and StepContract.model_validate_json(row["body"]).operation == "layout"), None)
            if layout_step:
                repair_result = await self.repair({
                    "task_id": store.task_id,
                    "expected_state_version": store.task()["state_version"],
                    "steps": [layout_step],
                    "reason": "User requested a presentation-only map revision",
                })
                # Reattach the already-known task before issuing the compact
                # continuation.  A client then has exactly one required next
                # call instead of having to infer that a recovery response is
                # still non-terminal.
                result = await self.resume({"task_id": store.task_id})
                result["invalidated_steps"] = repair_result["invalidated_steps"]
                result["presentation_rebuild"] = True
                result["status"] = "READY"
                result["completion_state"] = "NOT_COMPLETED"
                result["next_call"] = {"tool": "task_execute", "arguments": {}}
                result["next_action"] = (
                    "The revised map has NOT been created yet. Call task_execute once with "
                    "no arguments to rebuild and validate only the layout and exports; report "
                    "completion only if that call returns COMPLETED."
                )
                return result
        return self.compact_status()

    async def stop_task(self, a):
        """Stop at the last durable checkpoint without deleting evidence."""
        store = self.require_task(a["task_id"])
        with store.transaction():
            store.db.execute("UPDATE task SET status='CANCELLED',state_version=state_version+1")
            store.event("TASK_STOPPED", {"reason": a["reason"]})
        return {"task_id": store.task_id, "state": "stopped", "status": "CANCELLED"}

    async def restart_task(self, a):
        """Restart a server-selected scope without exposing step identifiers."""
        store = self.require_task(a["task_id"])
        if a["scope"] == "map":
            layout = next((row["id"] for row in reversed(list(store.db.execute(
                "SELECT id,status,body FROM steps ORDER BY rowid"
            ))) if row["status"] == "COMMITTED"
                and StepContract.model_validate_json(row["body"]).operation == "layout"), None)
            if layout is None:
                raise TaskError("MAP_RESTART_UNAVAILABLE", "No completed map is available to rebuild")
            await self.repair({
                "task_id": store.task_id,
                "expected_state_version": store.task()["state_version"],
                "steps": [layout], "reason": a["instruction"],
            })
            return await self.resume({"task_id": store.task_id})
        failed = next((row["id"] for row in reversed(list(store.db.execute(
            "SELECT id,status FROM steps ORDER BY rowid"
        ))) if row["status"] == "FAILED"), None)
        if a["scope"] == "failed_operation" and failed:
            return await self.resume({
                "task_id": store.task_id, "retry_step": failed,
                "continuation_token": self.issue_continuation(), "reason": a["instruction"],
            })
        raise TaskError(
            "RESTART_NEEDS_REVISION",
            "The requested restart would change the processing meaning or has no failed operation",
            next_action="Use task_update with the user's concrete revised method, parameter, input or map requirement",
        )

    async def call(self, operation, arguments):
        started = time.monotonic()
        success = False
        try:
            result = await self.dispatch(operation, arguments)
            success = True
            return result
        finally:
            self.traces.emit(
                operation,
                task_id=self.store.task_id if self.store else None,
                success=success,
                duration_ms=(time.monotonic() - started) * 1000,
            )

    async def dispatch(self, operation, arguments):
        async with self.lock:
            try:
                self.correction_gate(operation, arguments)
                previous_version = self.store.task()["state_version"] if self.store else None
                result = await self.dispatch_locked(operation, arguments)
                committed_progress = (
                    self.store
                    and isinstance(result, dict)
                    and self.store.task()["state_version"] != previous_version
                    and (
                        result.get("attempt_id")
                        or result.get("status") == "COMPLETED"
                        or bool(result.get("completed_steps"))
                    )
                )
                if committed_progress:
                    self.store.event("CORRECTION_RESET", {"source": "committed_execution"})
                if self.store and isinstance(result, dict) and operation in {
                    "task_begin", "task_start", "task_execute", "task_contract_submit",
                    "step_prepare", "workflow_run", "step_contract_submit", "prepare_algorithm",
                }:
                    failures = self.correction_failures()
                    result["correction_budget"] = {
                        "rejected_submissions": failures,
                        "limit": self.correction_limit,
                        "requires_user_input": failures >= self.correction_limit,
                    }
                return result
            except (TaskError, WorkerError, OSError) as exc:
                if not isinstance(exc, TaskError):
                    exc = TaskError(
                        "FILESYSTEM_ERROR" if isinstance(exc, OSError) else (exc.code or "WORKER_UNAVAILABLE"),
                        str(exc),
                        next_action="Inspect task_diagnose; resolve the underlying failure and use task_recover before retrying",
                    )
                self.record_correction_failure(operation, exc, arguments)
                if exc.payload["code"] == "WORKER_TIMEOUT" and self.store:
                    self.store.event("USER_INTERVENTION_REQUIRED", {"operation": operation})
                    exc.payload["next_action"] = (
                        "Stop automatic retries. Show the timeout and task status to the user; "
                        "continue only after their actual instruction is recorded with task_update or task_answer."
                    )
                if not exc.payload.get("phase"):
                    exc.payload["phase"] = {
                        "task_begin": "preparation", "task_start": "preparation",
                        "task_execute": "execution", "step_execute": "execution",
                        "prepare_algorithm": "preparation",
                        "task_answer": "clarification", "inspect_data": "inspection",
                        "task_contract_submit": "contract", "step_prepare": "contract",
                        "workflow_run": "execution",
                        "step_contract_submit": "contract",
                        "contract_help": "contract",
                        "task_recover": "recovery", "task_invalidate": "recovery",
                        "task_revise_inputs": "input_revision",
                        "task_validate": "validation", "task_finish": "validation",
                        "task_checkpoint": "checkpoint",
                    }.get(operation, "inspection" if is_read(operation, arguments) else "authorization")
                if self.store and arguments.get("task_id", self.store.task_id) == self.store.task_id:
                    task = self.store.task()
                    cp = task["checkpoint"]
                    exc.payload["task_id"] = self.store.task_id
                    exc.payload["continuation_token"] = self.issue_continuation()
                    exc.payload["checkpoint"] = (
                        {"project": cp["project"],
                         "verification": "Rechecked by task_recover before reuse"} if cp else None
                    )
                if not exc.payload.get("next_action"):
                    exc.payload["next_action"] = "Inspect evidence and task_diagnose; correct the request before retrying"
                raise exc

    async def dispatch_locked(self, operation, arguments):
        if operation in {"task_diagnose", "task_answer"} and (
            self.store is None or self.store.task_id != arguments.get("task_id")
        ):
            candidate = TaskStore(self.root, arguments["task_id"])
            if self.store:
                if self.store.unresolved():
                    candidate.close()
                    raise TaskError("ATTEMPT_UNRESOLVED", "Reconcile the attached task first")
                self.store.close()
            self.store = candidate
        if operation == "contract_help":
            if arguments.get("structure") in {"task", "step"}:
                return verifier_catalog()[arguments["structure"]]
            if arguments.get("kinds"):
                return {"validators": {kind: verifier_catalog(kind) for kind in arguments["kinds"]}}
            if arguments.get("kind"):
                return verifier_catalog(arguments["kind"])
            if self.contract_catalog_delivered:
                return {
                    "already_provided": True,
                    "next_action": (
                        "Reuse the stage schema. Call contract_help with kind or kinds "
                        "only for validators required by explicit user requirements."
                    ),
                }
            self.contract_catalog_delivered = True
            catalog = verifier_catalog()
            return {
                "validator_kinds": catalog["validators"],
                "instructions": (
                    "Task and step structures arrive with their workflow stage. "
                    "Fetch only validators required by explicit user requirements."
                ),
            }
        if operation.startswith("_"):
            raise TaskError(
                "PRIVATE_OPERATION", "Private worker operations are not public tools"
            )
        handlers = {
            "task_start": self.run_task,
            "plan_execute": self.execute_plan,
            "task_execute": self.continue_task,
            "presentation_continue": self.continue_presentation,
            "task_answer": self.answer_algorithm,
            "prepare_algorithm": self.prepare_algorithm,
            "task_update": self.clarify,
            "task_restart": self.restart_task,
            "task_stop": self.stop_task,
            "task_begin": self.begin,
            "inspect_data": self.inspect,
            "task_contract_submit": self.submit_task_contract,
            "step_prepare": self.prepare_step,
            "workflow_run": self.run_workflow,
            "step_contract_submit": self.submit_step_contract,
            "task_recover": self.resume,
            "task_checkpoint": self.checkpoint,
            "task_validate": self.validate_task,
            "task_finish": self.finish,
            "task_invalidate": self.repair,
            "task_revise_inputs": self.revise_inputs,
        }
        if operation == "task_diagnose":
            self.require_task(arguments["task_id"])
            return (
                self.status(include_details=True)
                if arguments.get("include_details") else self.compact_status()
            )
        if operation == "contract_get":
            store = self.require_task(arguments["task_id"])
            result = {
                "task_id": store.task_id,
                "continuation_token": self.issue_continuation(),
            }
            step_id = arguments.get("step_id")
            if step_id is None:
                return {**result, "kind": "task", "contract": self.current_contract().model_dump()}
            row = store.db.execute(
                "SELECT body,status FROM steps WHERE id=?", (step_id,)
            ).fetchone()
            if row is None:
                raise TaskError("UNKNOWN_STEP", "No persisted contract for this step",
                                next_action="Read task_diagnose.steps and choose an existing step ID")
            return {**result, "kind": "step", "step_id": step_id, "status": row["status"],
                    "contract": json.loads(row["body"]),
                    "repair_guidance": (
                        "For a corrected FAILED/INVALIDATED step, set repairs_step to this step_id. Copy required "
                        "non-system checks unchanged in their original preconditions/postconditions. "
                        "Omit all system_ checks: the server regenerates them for the chosen operation. "
                        "step_contract_submit(inherit_required_checks=true) can copy omitted required checks "
                        "from repairs_step; explicitly changed checks are still rejected. "
                        "Reading this contract does not authorize execution."
                    )}
        if operation == "step_execute":
            store = self.require_task(arguments["task_id"])
            row = store.db.execute("SELECT body FROM steps WHERE id=?", (arguments["step_id"],)).fetchone()
            if row is None:
                raise TaskError("STEP_CONTRACT_REQUIRED", "Submit a step contract before execution",
                                next_action="step_contract_submit; then step_execute with the approved step ID")
            step = StepContract.model_validate_json(row["body"])
            authorization = self.authorize_continuation(
                arguments, purpose="execute", step_id=arguments["step_id"]
            )
            outcome = await self.execute(step.operation, step.arguments, {
                "task_id": store.task_id, "step_id": arguments["step_id"], **authorization,
            })
            return self.execution_delta(
                outcome, include_details=arguments.get("include_details", False)
            )
        continuation_mutations = {
            "task_checkpoint", "task_finish", "task_invalidate", "task_revise_inputs",
        }
        if operation in continuation_mutations or (
            operation == "task_validate" and arguments.get("revalidate_steps")
        ) or (operation == "task_recover" and arguments.get("retry_step")) or (
            operation in {"task_update", "task_answer"} and self.store is not None
        ):
            authorization = self.authorize_continuation(arguments)
            arguments = {**arguments, **authorization}
        if operation in handlers:
            return await handlers[operation](arguments)
        metadata = {key: arguments.get(key) for key in CONTEXT}
        clean = {key: value for key, value in arguments.items() if key not in CONTEXT}
        if is_read(operation, clean):
            if metadata["task_id"]:
                self.require_task(metadata["task_id"])
            result = await self.bridge.call(
                operation, self.resolve(clean, self.assets() if self.store else {})
            )
            if operation == "algorithms" and clean.get("action", "list") != "ramps":
                entries = result.get("algorithms")
                entries = [result] if entries is None else [
                    *entries, *result.get("suggestions", [])
                ]
                for item in entries:
                    item["reliable_supported"] = True
                result["execution_mode"] = "reliable"
                if clean.get("action") == "help" and not clean.get("include_details"):
                    result.pop("help", None)
                    for parameter in result.get("parameters", []):
                        parameter.pop("definition", None)
                if clean.get("include_details"):
                    result["execution_note"] = (
                        "Installed algorithms use the generic prepare_algorithm path; "
                        "discovery does not authorize execution."
                    )
            return result
        if metadata.get("continuation_token"):
            authorization = self.authorize_continuation(
                metadata, purpose="execute", step_id=metadata.get("step_id")
            )
            metadata.update(authorization)
        required_context = {"task_id", "step_id", "continuation_token"}
        missing_context = sorted(key for key in required_context if metadata.get(key) is None)
        if missing_context:
            raise TaskError(
                "CONTRACT_REQUIRED",
                "Mutation authorization fields are missing from the top-level tool arguments",
                evidence={"missing_fields": missing_context,
                          "placement": "Alongside algorithm/parameters; never inside parameters",
                          "context_sources": {
                              "task_id": "task_begin or the existing task",
                              "step_id": "Approved step contract ID",
                              "continuation_token": "The approved step_contract_submit response",
                          }},
                next_action=(
                    "Continue the existing task and copy the approved continuation_token. "
                    "Do not create a replacement task."
                    if self.store else
                    "task_begin, inspect_data, task_contract_submit, then step_contract_submit"
                ),
            )
        return await self.execute(operation, clean, metadata)

    async def begin(self, a):
        return await self.create_task(a)

    @staticmethod
    def coordinate_crs(value):
        return "EPSG:4326" if isinstance(value, str) and value.strip().lower() == "geographic" else value

    async def validate_coordinate_crs(self, value, *, label="coordinate annotation CRS"):
        if value is None:
            return None
        result = await self.bridge.call("_validate_crs", {"value": value})
        if not result["valid"]:
            raise TaskError(
                "INVALID_CRS", f"{label.capitalize()} is invalid",
                evidence={label.replace(" ", "_"): value},
                next_action="Choose a valid installed CRS such as EPSG:4326, or omit this optional field",
            )
        return result

    async def create_task(self, a, *, inspect_inputs=False, coordinate_crs=None):
        if self.store and self.store.unresolved():
            raise TaskError("ATTEMPT_UNRESOLVED", "Resume the current task before switching")
        inputs = {}
        inspections = {}
        if not inspect_inputs:
            for key, item in a["inputs"].items():
                inputs[key] = {
                    **item,
                    "fingerprint": await asyncio.to_thread(fingerprint, item["path"]),
                }
        if self.store:
            self.store.close()
            self.store = None
        await self.fresh_worker()
        await self.validate_coordinate_crs(coordinate_crs)
        environment = await self.bridge.call("_environment", {})
        if inspect_inputs:
            for key, item in a["inputs"].items():
                path = item["path"]
                hint = item.get("kind")
                if hint == "style" or (hint is None and Path(path).suffix.lower() == ".qml"):
                    await asyncio.to_thread(ElementTree.parse, path)
                    kind = "style"
                    metadata = {"source": path, "name": Path(path).stem,
                                "kind": kind, "valid": True}
                else:
                    metadata = await self.bridge.call(
                        "_inspect", {"source": path, "kind": None}
                    )
                    kind = metadata["kind"]
                    if hint is not None and hint != kind:
                        metadata["declared_kind"] = hint
                        metadata["kind_corrected"] = True
                inputs[key] = {
                    "path": path,
                    "kind": kind,
                    "fingerprint": await asyncio.to_thread(fingerprint, path),
                }
                inspections[key] = metadata
        self.store = TaskStore.create(self.root, a["goal"], inputs, a["deliverables"], environment)
        cp = await self.make_checkpoint(self.store.directory / "initial", {})
        with self.store.transaction():
            self.store.db.execute("UPDATE task SET checkpoint=?", (canonical(cp),))
            for key, metadata in inspections.items():
                self.store.event(
                    "DATA_INSPECTED", {"source": f"asset:{key}", "metadata": metadata}
                )
        self.contract_catalog_delivered = False
        result = self.task_delta()
        result.update({
            "next_call": {
                "tool": "task_contract_submit",
                "arguments": {
                    "task_id": self.store.task_id,
                    "continuation_token": result["continuation_token"],
                    "contract": {},
                },
            },
            "instructions": (
                "Inspect each input once, then use next_call unchanged when only automatic "
                "basics apply. For explicit extra requirements only, fetch "
                "contract_help(structure='task') and the needed validator kinds."
            ),
        })
        if inspections:
            keep = {
                "name", "kind", "valid", "crs", "extent", "feature_count",
                "geometry_type", "bands", "width", "height", "pixel_size", "nodata",
                "geographic", "declared_kind", "kind_corrected",
            }
            result["inspections"] = {
                key: {name: value for name, value in metadata.items() if name in keep}
                for key, metadata in inspections.items()
            }
        return result

    async def run_task(self, a):
        """Inspect and contract a task, then bind its exact safe execution route."""
        a = {
            **a,
            "coordinate_crs": self.coordinate_crs(a.get("coordinate_crs")),
            "map_crs": self.coordinate_crs(a.get("map_crs")),
        }
        await self.validate_coordinate_crs(a["map_crs"], label="map display CRS")
        annotation_crs = await self.validate_coordinate_crs(a["coordinate_crs"])
        annotation_format = (a.get("coordinate_annotations") or {}).get("format", "auto")
        if (
            annotation_format in {"degree_minute", "degree_minute_second"}
            and annotation_crs is not None
            and not annotation_crs.get("geographic", False)
        ):
            raise TaskError(
                "INVALID_COORDINATE_FORMAT",
                "Degree-based coordinate labels require a geographic annotation CRS",
            )
        if (
            (a.get("coordinate_annotations") or {}).get("cardinal_directions") is True
            and annotation_crs is not None
            and not annotation_crs.get("geographic", False)
        ):
            raise TaskError(
                "INVALID_COORDINATE_FORMAT",
                "E/W/N/S coordinate suffixes require a geographic annotation CRS",
            )
        # A rejected compact contract must not leave a durable PREPARING task
        # that the compact tool set cannot amend.
        contract = self.parse_contract(TaskContract, a.get("contract", {}))
        validate_task_contract(
            contract,
            [Deliverable(**item) for item in a["deliverables"]],
            a["inputs"],
        )
        started = await self.create_task(
            {
                "goal": a["goal"],
                "inputs": a["inputs"],
                "deliverables": a["deliverables"],
            },
            inspect_inputs=True,
            coordinate_crs=a["coordinate_crs"],
        )
        if a.get("overwrite_existing_outputs"):
            self.authorize_declared_output_overwrites(
                a["deliverables"], source="explicit_task_start_option"
            )
        elif explicitly_authorizes_output_overwrite(a["goal"]):
            self.authorize_declared_output_overwrites(
                a["deliverables"], source="explicit_task_start_goal"
            )
        contracted = await self.submit_task_contract({
            "task_id": started["task_id"],
            "continuation_token": started["continuation_token"],
            "contract": contract.model_dump(),
            "reason": "Explicit requirements supplied with task_start" if a.get("contract") else "",
        })
        with self.store.transaction():
            self.store.event("TASK_PRESENTATION", {
                "title": a.get("title"),
                "legend_title": a.get("legend_title"),
                "map_language": a.get("map_language", "auto"),
                "show_legend_title": a.get("show_legend_title"),
                "north_arrow": a.get("north_arrow", False),
                "page_orientation": a.get("page_orientation", "auto"),
                "map_crs": a.get("map_crs"),
                "map_frame": a.get("map_frame", {}),
                "coordinate_crs": a["coordinate_crs"],
                "map_elements": a.get("map_elements", {}),
                "coordinate_annotations": a.get("coordinate_annotations", {}),
                "basemap": a.get("basemap"),
                "services": a.get("services", {}),
                "layer_operations": a.get("layer_operations", []),
                "data_operations": a.get("data_operations", []),
                "project_update": a.get("project_update"),
                "project": a.get("project", {}),
                "layers": a.get("layers"),
                "raster_ramp": a["raster_ramp"],
                "raster_styles": a.get("raster_styles", {}),
                "vector_styles": a.get("vector_styles", {}),
                "dpi": a["dpi"],
            })
        next_call = contracted["next_call"]
        map_requested = any(item["kind"] in {"project", "image", "pdf", "layout"}
                            for item in self.store.task()["deliverables"])
        operational_request = bool(
            a.get("inputs") or a.get("basemap") or a.get("services")
            or a.get("layer_operations") or a.get("data_operations") or a.get("project_update")
            or (a.get("project") or {}).get("action", "current") != "current"
        )
        explicit_project_operations = bool(
            a.get("basemap") or a.get("services") or a.get("layer_operations")
            or a.get("project_update")
            or (a.get("project") or {}).get("action", "current") != "current"
        )
        if operational_request and (
            not self.store.task()["deliverables"]
            or bool(a.get("data_operations"))
            or (
                explicit_project_operations
                and all(item["kind"] in {"project", "image", "pdf", "layout"}
                        for item in self.store.task()["deliverables"])
            )
        ):
            next_call = {
                "tool": "workflow_run",
                "arguments": {
                    "task_id": contracted["task_id"],
                    "continuation_token": contracted["continuation_token"],
                    "workflow": (
                        "standard_map_project" if map_requested else "project_layers"
                    ),
                },
            }
        result = {
            "task_id": contracted["task_id"],
            "status": contracted["status"],
        }
        result["inspections"] = started.get("inspections", {})
        if a.get("plan"):
            next_call = {
                "tool": "plan_execute",
                "arguments": {
                    "task_id": contracted["task_id"],
                    "continuation_token": contracted["continuation_token"],
                    "plan": a["plan"],
                },
            }
            result.update({
                "route": "controlled_processing_plan",
                "next_call": self.compact_next_call(next_call),
                "instructions": (
                    "The frozen plan is persisted by the continuation handle. Call task_execute "
                    "once; the service validates and executes each Processing step in order."
                ),
            })
            return result
        if next_call["tool"] != "workflow_run":
            result.update({
                "route": "processing_required",
                "continuation_token": contracted["continuation_token"],
                "next_tool": "prepare_algorithm",
                "instructions": (
                    "Select the exact installed QGIS algorithm ID, then call prepare_algorithm. "
                    "It reads parameter help and asks only for required values that have no "
                    "QGIS default and cannot be bound from the declared inputs and outputs."
                ),
            })
            return result

        workflow_arguments = next_call["arguments"]
        if a.get("layers"):
            workflow_arguments["layers"] = [
                item if item.startswith("asset:") else f"asset:{item}"
                for item in a["layers"]
            ]
        for name in ("title", "legend_title", "map_language", "show_legend_title", "north_arrow", "page_orientation", "map_crs", "map_frame",
                     "coordinate_crs", "map_elements", "coordinate_annotations", "basemap", "services",
                     "layer_operations", "data_operations", "project_update", "vector_styles", "project"):
            if a.get(name) is not None:
                workflow_arguments[name] = a[name]
        workflow_arguments.update({
            "style_layers": a.get("style_layers", True),
            "raster_ramp": a.get("raster_ramp", "Viridis"),
            "raster_styles": a.get("raster_styles", {}),
            "vector_styles": a.get("vector_styles", {}),
            "dpi": a.get("dpi", 150),
        })
        result.update({
            "route": "standard_map_project",
            "next_call": self.compact_next_call(next_call),
            "instructions": (
                "Input inspection and the task contract passed. Call task_execute once with "
                "the returned token; the service owns all approved workflow arguments."
            ),
        })
        return result

    async def execute_plan(self, a):
        """Execute a frozen Processing decomposition without making an agent re-plan it."""
        authorization = self.authorize_continuation(a)
        store = self.require_task(a["task_id"])
        store.require_version(authorization["expected_state_version"])
        completed = []
        continuation = a["continuation_token"]
        for index, raw in enumerate(a["plan"]):
            step = raw if isinstance(raw, dict) else raw.model_dump()
            prepared = await self.prepare_algorithm({
                "task_id": store.task_id,
                "continuation_token": continuation,
                "step_id": step["step_id"],
                "algorithm": step["algorithm"],
                "inputs": step.get("inputs", {}),
                "outputs": step.get("outputs", {}),
                "parameters": step.get("parameters", {}),
                "load_outputs": step.get("load_outputs", True),
            })
            if prepared.get("status") == "WAITING_FOR_USER":
                raise TaskError(
                    "PLAN_PARAMETER_UNRESOLVED",
                    "Frozen plan lacks a required QGIS parameter with no default",
                    evidence={"step_id": step["step_id"], "questions": prepared["questions"]},
                    next_action="Ask the user the returned questions, then amend the plan before retrying",
                )
            outcome = await self.continue_task(prepared["next_call"]["arguments"])
            status = store.task()["status"]
            completed.append({
                "step_id": step["step_id"], "algorithm": step["algorithm"],
                "status": status,
            })
            if status == "COMPLETED":
                if index != len(a["plan"]) - 1:
                    raise TaskError(
                        "PLAN_COMPLETED_EARLY",
                        "Frozen plan contains steps after all deliverables were completed",
                        evidence={"step_id": step["step_id"]},
                    )
                return {**outcome, "workflow": "controlled_processing_plan", "completed_steps": completed}
            continuation = outcome["continuation_token"]
        result = self.task_delta(continuation_token=continuation)
        result.update({
            "workflow": "controlled_processing_plan", "completed_steps": completed,
            "next_call": self.next_call_after_execution(continuation),
        })
        if result["next_call"]["tool"] == "task_finish":
            return await self.finish(result["next_call"]["arguments"])
        raise TaskError(
            "PLAN_INCOMPLETE",
            "Frozen plan ended before every requested deliverable existed",
            evidence={"deliverables": [item["id"] for item in store.task()["deliverables"]]},
            next_action="Add the missing Processing outputs to the frozen plan",
        )

    async def continue_task(self, a):
        """Execute only the next action bound into a signed compact continuation."""
        token = a["continuation_token"]
        self.route_record(token)
        route_id = token
        store = self.require_task()
        for row in store.db.execute(
            "SELECT body FROM events WHERE kind='ROUTE_COMPLETED' ORDER BY sequence DESC"
        ):
            cached = json.loads(row["body"])
            if cached.get("route_id") == route_id:
                return cached["result"]

        target, arguments = self.routed_arguments(token)
        result = await self.dispatch_locked(target, arguments)
        if target == "step_execute":
            presented = await self.auto_present_deliverables(
                result.get("continuation_token")
            )
            if presented is not None:
                result = presented
        completed_steps = result.get("completed_steps", [])
        workflow = result.get("workflow")
        next_call = result.get("next_call")
        if next_call and next_call["tool"] == "task_finish":
            finished = await self.dispatch_locked("task_finish", next_call["arguments"])
            if workflow:
                finished["workflow"] = workflow
            if completed_steps:
                finished["completed_steps"] = completed_steps
            finished.pop("continuation_token", None)
            result = finished
        elif next_call and next_call["tool"] in {
            "task_contract_submit", "workflow_run", "step_execute", "task_finish"
        }:
            result["next_call"] = self.compact_next_call(next_call)
        elif target == "step_execute" and next_call and next_call["tool"] == "step_prepare":
            # ``step_prepare`` is intentionally not part of the compact public
            # surface.  A Processing chain advances by preparing its next live
            # registry algorithm, not by hand-authoring a hidden recipe.
            result.pop("next_call", None)
            result.update({
                "next_tool": "prepare_algorithm",
                "instructions": (
                    "This Processing step committed. If further analysis is needed, "
                    "call prepare_algorithm with the task ID and algorithm parameters; "
                    "the service owns continuation state. "
                    "automatic raster/vector working assets are now available."
                ),
            })
        with store.transaction():
            store.event("ROUTE_COMPLETED", {"route_id": route_id, "result": result})
        return result

    async def continue_presentation(self, a):
        authorization = self.authorize_continuation(a)
        self.require_task(a["task_id"]).require_version(authorization["expected_state_version"])
        result = await self.auto_present_deliverables(a["continuation_token"])
        if result is None:
            raise TaskError("PRESENTATION_NOT_READY", "No requested map is ready for automatic presentation")
        return result

    async def auto_present_deliverables(self, continuation):
        """Create requested map artifacts after generic Processing reaches its data outputs.

        Generic Processing deliberately has no map-specific public tool.  Once all
        requested vector/raster deliverables exist, a requested PNG/PDF/project is
        nevertheless unambiguous: present those final layers with the standard
        required map elements.  This keeps the compact six-tool interface usable
        for analysis-plus-map tasks without forcing an agent to declare or drive
        private layout intermediates.
        """
        if not continuation:
            return None
        task = self.require_task().task()
        maps = [
            item for item in task["deliverables"]
            if item["kind"] in {"image", "pdf", "project", "layout"}
        ]
        presentation = self.presentation_options()
        assets = self.assets()
        data_ids = self.presentation_data_ids(assets)
        if not maps or not data_ids or any(item not in assets for item in data_ids):
            return None
        if all(item["id"] in assets for item in maps):
            return None
        final_ids = data_ids
        if any(not assets[key].get("layer_id") for key in final_ids):
            raise TaskError(
                "PRESENTATION_LAYER_REQUIRED",
                "Map deliverables require final Processing outputs to be loaded into QGIS",
                evidence={"outputs": final_ids,
                          "loaded": [key for key in final_ids if assets[key].get("layer_id")]},
                next_action="Prepare the final Processing algorithm with load_outputs=true",
            )
        layer_ids = [
            item.removeprefix("asset:") for item in (presentation.get("layers") or final_ids)
        ]
        if len(set(layer_ids)) != len(layer_ids):
            raise TaskError("PRESENTATION_LAYER_DUPLICATE", "Map layers must not repeat")
        missing = [key for key in layer_ids if key not in assets or not assets[key].get("layer_id")]
        if missing:
            raise TaskError(
                "PRESENTATION_LAYER_REQUIRED",
                "Requested map context layers must exist and be loaded in QGIS",
                evidence={"missing": missing, "available": sorted(
                    key for key, value in assets.items() if value.get("layer_id"))},
                next_action="Prepare and execute the missing Processing outputs with load_outputs=true",
            )

        step_rows = list(self.store.db.execute("SELECT id,status,body FROM steps ORDER BY rowid"))
        # A repaired presentation can invalidate its logical layout asset while
        # the restored QGIS checkpoint still contains the physical layout.  Keep
        # those names reserved so an automatic layout never collides with state
        # that is intentionally retained for recovery evidence.
        existing_layouts = {
            name
            for name in ((task.get("checkpoint") or {}).get("info") or {}).get("layouts", [])
            if isinstance(name, str) and name
        }
        reserved = (set(assets) | {item["id"] for item in task["deliverables"]}
                    | {row["id"] for row in step_rows} | existing_layouts)

        def unique_id(base):
            candidate, number = base[:128], 2
            while candidate in reserved:
                suffix = f"_{number}"
                candidate = base[:128 - len(suffix)] + suffix
                number += 1
            reserved.add(candidate)
            return candidate

        async def run_step(base_id, contract, token):
            requested_repair = contract.get("repairs_step")
            contract = {key: value for key, value in contract.items() if key != "repairs_step"}
            previous = [
                row for row in step_rows
                if row["id"] == base_id
                or (row["id"].startswith(base_id + "_")
                    and row["id"][len(base_id) + 1:].isdigit())
            ]
            if any(row["status"] == "COMMITTED" for row in previous):
                return {"continuation_token": token}
            latest = previous[-1] if previous else None
            if latest and latest["status"] not in {"FAILED", "INVALIDATED"}:
                raise TaskError(
                    "PRESENTATION_PENDING", "Recover the unfinished presentation step first",
                    evidence={"step_id": latest["id"], "status": latest["status"]},
                )
            step_id = unique_id(base_id)
            repair_step = latest["id"] if latest else requested_repair
            if repair_step is None:
                output_ids = {item["id"] for item in contract.get("outputs", [])}
                repair_step = next((row["id"] for row in reversed(step_rows)
                                    if row["status"] == "INVALIDATED"
                                    and output_ids.intersection(
                                        output.id for output in StepContract.model_validate_json(row["body"]).outputs
                                    )), None)
            prepared = await self.submit_step_contract({
                "task_id": self.store.task_id,
                "continuation_token": token,
                "step_id": step_id,
                "contract": {**contract, "repairs_step": repair_step},
                "inherit_required_checks": bool(repair_step),
            })
            call = prepared["next_call"]["arguments"]
            row = self.store.db.execute(
                "SELECT body FROM steps WHERE id=?", (step_id,)
            ).fetchone()
            step = StepContract.model_validate_json(row["body"])
            authorization = self.authorize_continuation(
                call, purpose="execute", step_id=step_id
            )
            return await self.execute(
                step.operation, step.arguments,
                {"task_id": self.store.task_id, "step_id": step_id, **authorization},
            )

        service_requests = dict(presentation.get("services") or {})
        if presentation.get("basemap"):
            service_requests["presentation_basemap"] = presentation["basemap"]
        for requested_id, requested_service in service_requests.items():
            provider = requested_service.get("provider", "openstreetmap")
            existing = requested_id if requested_id in assets else None
            service_id = existing or unique_id(requested_id)
            if not existing:
                worker_service = {
                    "openstreetmap": "osm", "xyz": "xyz", "wms": "wms",
                    "wmts": "wmts", "wfs": "wfs",
                }[provider]
                token_result = await run_step(
                    "presentation_service_" + service_id,
                    {
                        "operation": "add_basemap",
                        "arguments": {
                            "service": worker_service, "url": requested_service.get("url"),
                            "uri": requested_service.get("uri"),
                            "name": requested_service.get("name"),
                            "attribution": requested_service.get("attribution"),
                            "role": requested_service.get("role"),
                            "layer_name": requested_service.get("layer_name"),
                            "type_name": requested_service.get("type_name"),
                            "style_name": requested_service.get("style_name"),
                            "crs": requested_service.get("crs"),
                            "image_format": requested_service.get("image_format", "image/png"),
                            "version": requested_service.get("version"),
                            "authcfg": requested_service.get("authcfg"),
                            "zmin": requested_service.get("zmin", 0),
                            "zmax": requested_service.get("zmax", 19),
                        },
                        "inputs": [],
                        "outputs": [{
                            "id": service_id,
                            "kind": "vector" if provider == "wfs" else "raster",
                            "binding": "layer",
                        }],
                        "reason": "Add the requested contextual map service",
                    },
                    continuation,
                )
                continuation = token_result["continuation_token"]
                assets = self.assets()
            if service_id not in layer_ids:
                if requested_service.get("role") == "overlay" or provider == "wfs":
                    layer_ids.insert(0, service_id)
                else:
                    layer_ids.append(service_id)

        for key in layer_ids:
            if assets[key].get("remote") and assets[key].get("role") != "overlay":
                continue
            kind = assets[key]["kind"]
            raster_style = {
                name: value for name, value in
                presentation.get("raster_styles", {}).get(key, {}).items()
                if value is not None
            }
            style_arguments = (
                {"layer": f"asset:{key}", "ramp": presentation["raster_ramp"],
                 "band": 1, "classes": 8, "opacity": 1, **raster_style}
                if kind == "raster"
                else {"layer": f"asset:{key}", "color": "#4c78a8", "outline": "#202020",
                      "width": 0.4, "opacity": 1,
                      **{name: value for name, value in
                         presentation.get("vector_styles", {}).get(key, {}).items()
                         if value is not None}}
            )
            style_operation = "style_" + kind
            style_inputs = [key]
            if style_arguments.get("mode") == "qml" or style_arguments.get("renderer") == "qml":
                qml_id = style_arguments.get("qml_asset")
                if not qml_id or qml_id not in assets or assets[qml_id]["kind"] != "style":
                    raise TaskError(
                        "STYLE_ASSET_REQUIRED",
                        "QML presentation requires a declared style input",
                        evidence={"layer": key, "qml_asset": qml_id},
                    )
                style_operation = "style_file"
                style_arguments = {
                    "action": "load", "layer": f"asset:{key}",
                    "path": f"asset:{qml_id}",
                }
                style_inputs.append(qml_id)
            elif kind == "raster" and style_arguments.get("mode") in {"gray", "rgb", "hillshade"}:
                style_operation = "render_raster"
                style_arguments = {
                    name: value for name, value in style_arguments.items()
                    if name in {"layer", "mode", "band", "red", "green", "blue", "azimuth",
                                "altitude", "z_factor", "opacity"}
                }
            elif kind == "vector" and style_arguments.get("renderer") == "graduated":
                style_operation = "style_graduated"
                style_arguments = {
                    "layer": style_arguments["layer"],
                    "field": style_arguments.get("graduated_field"),
                    "ramp": style_arguments.get("ramp", "Viridis"),
                    "classes": style_arguments.get("classes", 5),
                    "method": style_arguments.get("method", "equal_interval"),
                    "opacity": style_arguments.get("opacity", 1),
                    "label_field": style_arguments.get("label_field"),
                }
            token_result = await run_step(
                "presentation_style_" + key,
                {
                    "operation": style_operation,
                    "arguments": style_arguments,
                    "inputs": style_inputs, "outputs": [],
                    "reason": "Apply standard presentation to final Processing output",
                },
                continuation,
            )
            continuation = token_result["continuation_token"]

        layout_outputs = [item for item in maps if item["kind"] == "layout"]
        prior_layout_step = next((row["id"] for row in reversed(step_rows)
                                  if StepContract.model_validate_json(row["body"]).operation == "layout"
                                  and row["status"] in {"COMMITTED", "INVALIDATED"}), None)
        prior_layout = next((
            StepContract.model_validate_json(row["body"]).outputs[0].id
            for row in reversed(step_rows)
            if row["id"].startswith("presentation_layout")
            and row["status"] == "COMMITTED"
        ), None)
        layout_id = (layout_outputs[0]["id"] if layout_outputs
                     else prior_layout or unique_id("map_layout"))
        # An explicitly requested layout ID is part of the task output contract,
        # so it cannot be silently renamed. Rebuilding that task-owned layout is
        # safe; automatically named recovery layouts use a fresh suffix instead.
        overwrite_layout = bool(layout_outputs and layout_id in existing_layouts)
        token_result = await run_step(
            "presentation_layout",
            {
                "operation": "layout",
                "arguments": {
                    "action": "create", "name": layout_id,
                    "title": presentation["title"] or self.default_map_title(task["goal"]),
                    "legend_title": presentation.get("legend_title"),
                    "map_language": presentation.get("map_language", "auto"),
                    "show_legend_title": presentation.get("show_legend_title"),
                    "page_orientation": presentation.get("page_orientation", "auto"),
                    "crs": presentation.get("map_crs"),
                    "map_frame": presentation.get("map_frame", {}),
                    "show_title": True, "layers": [f"asset:{key}" for key in layer_ids],
                    "extent_layer": f"asset:{layer_ids[0]}" if len(layer_ids) == 1 else None,
                    "legend": True,
                    "scalebar": True, "north_arrow": presentation.get("north_arrow", False), "grid": True,
                    "grid_crs": presentation["coordinate_crs"],
                    "map_elements": presentation.get("map_elements", {}),
                    "coordinate_annotations": presentation.get("coordinate_annotations", {}),
                    "overwrite": overwrite_layout,
                },
                "inputs": layer_ids,
                "outputs": [{"id": layout_id, "kind": "layout", "binding": "layout"}],
                "reason": "Create the requested map with basic required elements",
                "repairs_step": prior_layout_step,
            },
            continuation,
        )
        continuation = token_result["continuation_token"]

        for item in maps:
            if item["kind"] not in {"image", "pdf"}:
                continue
            token_result = await run_step(
                "presentation_export_" + item["id"],
                {
                    "operation": "export_map",
                    "arguments": {"layout": f"asset:{layout_id}", "path": f"output:{item['id']}", "dpi": presentation["dpi"]},
                    "inputs": [layout_id],
                    "outputs": [{"id": item["id"], "kind": item["kind"], "binding": "path"}],
                    "reason": "Export the requested map",
                },
                continuation,
            )
            continuation = token_result["continuation_token"]
        for item in maps:
            if item["kind"] != "project":
                continue
            token_result = await run_step(
                "presentation_project_" + item["id"],
                {
                    "operation": "project",
                    "arguments": {"action": "save", "path": f"output:{item['id']}"},
                    "inputs": [],
                    "outputs": [{"id": item["id"], "kind": "project", "binding": "path"}],
                    "reason": "Save the requested editable QGIS project",
                },
                continuation,
            )
            continuation = token_result["continuation_token"]

        result = self.task_delta(continuation_token=continuation)
        result.update({
            "workflow": "processing_result_presentation",
            "assets": self.deliverable_assets(),
            "next_call": self.next_call_after_execution(continuation),
        })
        return result

    async def prepare_algorithm(self, a):
        """Prepare any installed Processing algorithm from its live registry metadata."""
        authorization = self.authorize_continuation(a)
        store = self.require_task(a["task_id"])
        store.require_version(authorization["expected_state_version"])
        info = await self.bridge.call(
            "algorithms", {"action": "help", "algorithm": a["algorithm"]}
        )
        specifications = normalize_algorithm_help(info)
        by_name = {item.name: item for item in specifications}
        name_normalizations = []
        for section in ("inputs", "outputs", "parameters"):
            normalized, changes = canonicalize_parameter_names(
                a.get(section, {}), specifications
            )
            a[section] = normalized
            name_normalizations.extend({**item, "section": section} for item in changes)
        assets = self.assets()
        for name, value in list(a["parameters"].items()):
            spec = by_name.get(name)
            if spec is None or spec.type.casefold() not in {
                "source", "vector", "raster", "maplayer", "multilayer",
            } or name in a["inputs"]:
                continue
            bindings = value if isinstance(value, list) else [value]
            if bindings and all(isinstance(item, str) and item in assets for item in bindings):
                a["inputs"][name] = a["parameters"].pop(name)
                name_normalizations.append({
                    "parameter": name, "from": "parameters", "to": "inputs",
                    "reason": "declared_layer_asset_binding",
                })
        overlap = sorted(
            set(a.get("parameters", {}))
            & (set(a.get("inputs", {})) | set(a.get("outputs", {})))
        )
        if overlap:
            raise TaskError(
                "INVALID_PARAMETERS",
                "A parameter must be supplied in exactly one prepare_algorithm section",
                evidence={"parameters": overlap},
            )
        unknown = sorted(
            (set(a.get("inputs", {})) | set(a.get("outputs", {}))) - set(by_name)
        )
        if unknown:
            raise TaskError(
                "INVALID_PARAMETERS", "Unknown algorithm parameter names",
                evidence={
                    "unknown_parameters": unknown,
                    "allowed_parameters": sorted(by_name),
                    "possible_names": {
                        name: difflib.get_close_matches(name, by_name, n=1, cutoff=0.5)
                        for name in unknown
                    },
                },
            )
        bad_inputs = sorted(
            key for key in a.get("inputs", {}) if by_name[key].destination
        )
        bad_outputs = sorted(
            key for key in a.get("outputs", {}) if not by_name[key].destination
        )
        if bad_inputs or bad_outputs:
            raise TaskError(
                "INVALID_PARAMETERS", "Input and destination parameter mappings are reversed",
                evidence={"not_inputs": bad_inputs, "not_destinations": bad_outputs},
            )
        # Some MCP clients serialize one layer binding as a one-item list.
        # Processing accepts lists only for multilayer parameters; retaining the
        # wrapper makes providers such as GRASS fail while resolving the layer.
        # Normalize this mechanical representation difference, but never choose
        # an item from an ambiguous multi-item value.
        for name, value in list(a.get("inputs", {}).items()):
            if not isinstance(value, list) or by_name[name].type.casefold() == "multilayer":
                continue
            if len(value) != 1:
                raise TaskError(
                    "INVALID_PARAMETERS",
                    "A single-layer Processing input must bind exactly one asset",
                    evidence={"parameter": name, "assets": value},
                )
            a["inputs"][name] = value[0]
            name_normalizations.append({
                "parameter": name, "from": "single_item_list", "to": "single_asset",
                "reason": "single_layer_binding",
            })
        missing_destinations = [
            item.name for item in specifications
            if item.destination and item.required and not item.has_default
            and item.name not in a.get("outputs", {})
        ]
        if missing_destinations:
            raise TaskError(
                "OUTPUT_REQUIRED", "Map every required destination to a declared logical output",
                evidence={"parameters": missing_destinations},
                next_action="Call prepare_algorithm again with outputs={parameter_name: logical_output_id}",
            )

        input_asset_ids = [
            asset_id
            for binding in a.get("inputs", {}).values()
            for asset_id in self.input_binding_ids(binding)
        ]
        unknown_assets = sorted(set(input_asset_ids) - set(assets))
        if unknown_assets:
            raise TaskError(
                "UNKNOWN_ASSET", "Algorithm inputs reference unavailable logical assets",
                evidence={"assets": unknown_assets, "available": sorted(assets)},
            )
        declared_kinds = {
            item["id"]: item["kind"] for item in store.task()["deliverables"]
        }
        declared_kinds.update(self.current_contract().intermediates)
        output_ids = list(a.get("outputs", {}).values())
        inferred_outputs = {}
        for parameter, output_id in a.get("outputs", {}).items():
            if output_id in declared_kinds:
                continue
            kind = destination_asset_kind(by_name[parameter])
            if kind is not None:
                inferred_outputs[output_id] = kind
        unknown_outputs = sorted(
            set(output_ids) - set(declared_kinds) - set(inferred_outputs)
        )
        if unknown_outputs:
            raise TaskError(
                "UNDECLARED_OUTPUT",
                "Ambiguous algorithm outputs must use declared deliverable or intermediate IDs",
                evidence={
                    "outputs": unknown_outputs,
                    "declared": sorted(declared_kinds),
                    "automatic_working_asset_types": ["raster", "vector"],
                },
            )
        if len(set(output_ids)) != len(output_ids):
            raise TaskError("OUTPUT_COLLISION", "Destination parameters must use distinct output IDs")
        existing_outputs = sorted(set(output_ids) & set(assets))
        if existing_outputs:
            raise TaskError(
                "ASSET_EXISTS", "Algorithm outputs cannot overwrite existing logical assets",
                evidence={"assets": existing_outputs},
            )

        supplied = dict(a.get("parameters", {}))
        supplied.update({
            key: (
                [f"asset:{asset_id}" for asset_id in value]
                if isinstance(value, list)
                else f"asset:{value}"
            )
            for key, value in a.get("inputs", {}).items()
        })
        supplied.update({key: f"output:{value}" for key, value in a.get("outputs", {}).items()})
        contextual_choices = {}
        layer_assets = [
            key for key, value in assets.items() if value.get("kind") in {"vector", "raster"}
        ]
        for spec in specifications:
            kind = spec.type.casefold()
            if kind in {"source", "vector", "raster", "maplayer", "multilayer"}:
                contextual_choices[spec.name] = layer_assets
            if "field" not in kind or not spec.parent_parameter:
                continue
            parent_asset = a.get("inputs", {}).get(spec.parent_parameter)
            if not parent_asset or isinstance(parent_asset, list):
                continue
            metadata = await self.bridge.call(
                "_inspect", {
                    "source": assets[parent_asset]["path"],
                    "kind": assets[parent_asset].get("kind"),
                },
            )
            contextual_choices[spec.name] = [
                field["name"] for field in metadata.get("fields", [])
            ]

        plan_id = uuid.uuid4().hex
        resolution = resolve_parameters(
            plan_id=plan_id,
            algorithm=a["algorithm"],
            step_id=a["step_id"],
            specifications=specifications,
            supplied=supplied,
            contextual_choices=contextual_choices,
        )
        resolution.normalizations = [
            *name_normalizations, *resolution.normalizations,
        ]
        payload = {
            **resolution.model_dump(),
            "request": {
                "inputs": a.get("inputs", {}), "outputs": a.get("outputs", {}),
                "parameters": a.get("parameters", {}),
                "load_outputs": a.get("load_outputs", True),
                "inferred_outputs": inferred_outputs,
                "repairs_step": a.get("repairs_step"),
            },
            "specifications": [item.model_dump() for item in specifications],
            "contextual_choices": contextual_choices,
        }
        store.save_algorithm_plan(payload)
        if resolution.questions:
            return self._algorithm_questions(payload)
        return await self._submit_algorithm_resolution(payload, resolution)

    def _algorithm_questions(self, plan):
        return {
            "task_id": self.store.task_id,
            "status": "WAITING_FOR_USER",
            "plan_id": plan["plan_id"],
            "continuation_token": self.issue_continuation(),
            "questions": plan["questions"],
            "instructions": (
                "Ask the user for these required values. Then call task_answer once per "
                "question_id with the user's actual answer; do not guess."
            ),
        }

    async def answer_algorithm(self, a):
        """Persist one typed answer and resume the corresponding preparation plan."""
        store = self.require_task(a["task_id"])
        row = store.db.execute(
            "SELECT plan_id,status,answer FROM questions WHERE id=?", (a["question_id"],)
        ).fetchone()
        if row is None:
            raise TaskError("UNKNOWN_QUESTION", "Structured question does not exist")
        plan = store.algorithm_plan(row["plan_id"])
        if row["status"] == "ANSWERED" and json.loads(row["answer"]) != a["answer"]:
            raise TaskError(
                "ANSWER_IMMUTABLE", "A structured question cannot be answered differently",
                evidence={"question_id": a["question_id"]},
                next_action="Continue with the recorded answer or begin an explicit user-guided revision",
            )
        if row["status"] not in {"PENDING", "ANSWERED"}:
            raise TaskError("QUESTION_STATE", "Structured question is not resumable")
        specifications = [
            AlgorithmParameter.model_validate(item) for item in plan["specifications"]
        ]
        preview_answers = {**plan["answers"], a["question_id"]: a["answer"]}
        resolution = resolve_parameters(
            plan_id=plan["plan_id"], algorithm=plan["algorithm"],
            step_id=plan["step_id"], specifications=specifications,
            supplied=plan["parameters"], answers=preview_answers,
            contextual_choices=plan.get("contextual_choices", {}),
        )
        resolution.normalizations = [
            *plan.get("normalizations", []), *resolution.normalizations,
        ]
        if row["status"] == "PENDING":
            store.answer_question(
                a["question_id"], a["answer"], a["expected_state_version"]
            )
        payload = {
            **plan,
            **resolution.model_dump(),
            "answers": preview_answers,
        }
        if row["status"] == "PENDING":
            store.update_algorithm_plan(payload)
        if resolution.questions:
            return self._algorithm_questions(payload)
        step = store.db.execute(
            "SELECT status FROM steps WHERE id=?", (plan["step_id"],)
        ).fetchone()
        if step is not None:
            if step["status"] != "PLANNED":
                raise TaskError(
                    "STEP_STATE", "The answered algorithm step is no longer pending execution",
                    evidence={"step_id": plan["step_id"], "status": step["status"]},
                )
            result = self.task_delta()
            result.update({
                "step_id": plan["step_id"],
                "step_status": "PLANNED",
                "next_call": self.compact_next_call({
                    "tool": "step_execute",
                    "arguments": {
                        "task_id": store.task_id,
                        "step_id": plan["step_id"],
                        "continuation_token": self.issue_continuation(
                            "execute", plan["step_id"]
                        ),
                    },
                }),
                "plan_id": plan["plan_id"],
                "prepared_algorithm": plan["algorithm"],
            })
            return result
        if store.task()["checkpoint"]:
            await self.restore_committed()
        return await self._submit_algorithm_resolution(payload, resolution)

    async def _submit_algorithm_resolution(self, plan, resolution: AlgorithmResolution):
        store = self.require_task()
        specifications = {
            item["name"]: AlgorithmParameter.model_validate(item)
            for item in plan["specifications"]
        }
        parameters = dict(resolution.parameters)
        validate_processing_expressions(plan["algorithm"], parameters)
        input_ids = [
            asset_id
            for binding in plan["request"]["inputs"].values()
            for asset_id in self.input_binding_ids(binding)
        ]
        for item in resolution.resolutions:
            spec = specifications[item.parameter]
            if item.status != "answered":
                continue
            if spec.type.casefold() in {"source", "vector", "raster", "maplayer"}:
                value = parameters[item.parameter]
                if isinstance(value, str) and not value.startswith("asset:"):
                    parameters[item.parameter] = f"asset:{value}"
                    input_ids.append(value)
            elif spec.type.casefold() == "multilayer":
                values = parameters[item.parameter]
                values = values if isinstance(values, list) else [values]
                parameters[item.parameter] = [
                    value if str(value).startswith("asset:") else f"asset:{value}"
                    for value in values
                ]
                input_ids.extend(str(value).removeprefix("asset:") for value in values)
        input_ids.extend(self.parameter_asset_ids(parameters))
        declared_kinds = {
            item["id"]: item["kind"] for item in store.task()["deliverables"]
        }
        declared_kinds.update(self.current_contract().intermediates)
        declared_kinds.update(plan["request"].get("inferred_outputs", {}))
        outputs = [
            {"id": output_id, "kind": declared_kinds[output_id], "binding": parameter}
            for parameter, output_id in plan["request"]["outputs"].items()
        ]
        result = await self.submit_step_contract({
            "task_id": store.task_id,
            "continuation_token": self.issue_continuation(),
            "step_id": plan["step_id"],
            "contract": {
                "operation": "run_processing",
                "arguments": {
                    "algorithm": plan["algorithm"],
                    "parameters": parameters,
                    "load_outputs": plan["request"].get("load_outputs", True),
                },
                "inputs": list(dict.fromkeys(input_ids)),
                "outputs": outputs,
                "repairs_step": plan["request"].get("repairs_step"),
                "reason": f"Run requested QGIS Processing algorithm {plan['algorithm']}",
            },
            "inherit_required_checks": bool(plan["request"].get("repairs_step")),
        })
        completed = {**plan, **resolution.model_dump(), "status": "PLANNED"}
        store.update_algorithm_plan(completed)
        result.update({
            "plan_id": plan["plan_id"],
            "prepared_algorithm": plan["algorithm"],
            "next_call": self.compact_next_call(result["next_call"]),
        })
        return result

    @staticmethod
    def input_binding_ids(value):
        """Return logical IDs from a single-layer or multilayer binding."""
        return value if isinstance(value, list) else [value]

    @staticmethod
    def parameter_asset_ids(value):
        """Find explicit asset references nested in generic Processing values.

        QGIS accepts layer lists and expression-adjacent layer values inside
        arbitrary parameter objects.  They are still real dependencies, even
        though they are not represented by ``prepare_algorithm.inputs``.
        """
        if isinstance(value, str):
            return [value.removeprefix("asset:")] if value.startswith("asset:") else []
        if isinstance(value, dict):
            return [item for child in value.values() for item in TaskCoordinator.parameter_asset_ids(child)]
        if isinstance(value, list):
            return [item for child in value for item in TaskCoordinator.parameter_asset_ids(child)]
        return []

    async def inspect(self, a):
        source = self.resolve(a["source"], self.assets() if self.store else {}, prefer_path=True)
        if a.get("query"):
            query = a["query"]
            layer_source = self.resolve(
                a["source"], self.assets() if self.store else {}, prefer_path=False
            )
            result = await self.bridge.call("features", {
                "layer": layer_source,
                "action": query.get("action", "sample"),
                "expression": query.get("expression"),
                "limit": query.get("limit", 10),
                "field": query.get("field"),
            })
            if self.store:
                self.store.event("DATA_QUERIED", {
                    "source": a["source"], "query": query,
                    "result_count": len(result.get("features", [])),
                })
            return result
        result = await self.bridge.call("_inspect", {"source": source, "kind": a.get("kind")})
        if a.get("include_fingerprint"):
            result["fingerprint"] = await asyncio.to_thread(fingerprint, source)
        if self.store:
            self.store.event("DATA_INSPECTED", {"source": a["source"], "metadata": result})
        if a.get("include_details"):
            return result
        keep = {
            "source", "name", "kind", "valid", "crs", "extent", "feature_count",
            "geometry_type", "bands", "width", "height", "pixel_size", "nodata",
            "geographic", "fingerprint",
        }
        compact = {key: value for key, value in result.items() if key in keep}
        if result.get("fields") is not None:
            compact["field_count"] = len(result["fields"])
            compact["field_names"] = [item["name"] for item in result["fields"][:20]]
            compact["fields_truncated"] = len(result["fields"]) > 20
        return compact

    async def submit_task_contract(self, a):
        authorization = self.authorize_continuation(a)
        store = self.require_task(a["task_id"])
        expected = authorization["expected_state_version"]
        store.require_version(expected)
        if store.task()["status"] == "BLOCKED":
            await self.restore_committed()
        contract = self.parse_contract(TaskContract, a.get("contract", {}))
        previous = self.current_contract() if store.task()["contract_version"] else None
        validate_task_contract(
            contract,
            [Deliverable(**item) for item in store.task()["deliverables"]],
            store.task()["inputs"],
            previous,
        )
        store.save_contract(contract.model_dump(), a.get("reason", ""), expected)
        result = self.task_delta()
        task = store.task()
        map_kinds = {"project", "image", "pdf", "layout"}
        standard_map = bool(task["inputs"]) and all(
            item["kind"] in map_kinds for item in task["deliverables"]
        )
        next_tool = "workflow_run" if standard_map else "step_prepare"
        next_arguments = {
            "task_id": a["task_id"],
            "continuation_token": result["continuation_token"],
        }
        if standard_map:
            next_arguments["workflow"] = "standard_map_project"
        result.update({
            "next_call": {
                "tool": next_tool,
                "arguments": next_arguments,
            },
            "instructions": (
                "Use the returned standard workflow when it matches the request. Otherwise "
                "prefer step_prepare for common GIS recipes. Use step_contract_submit only "
                "when neither workflow nor recipe fits."
            ),
        })
        return result

    async def prepare_step(self, a):
        """Compile a compact, model-selected recipe into the existing strict step path."""
        contract = self.compile_recipe(a)
        result = await self.submit_step_contract({
            "task_id": a["task_id"],
            "continuation_token": a["continuation_token"],
            "step_id": a["step_id"],
            "contract": contract,
            "inherit_required_checks": False,
        })
        result["prepared_recipe"] = a["action"]
        return result

    async def run_workflow(self, a):
        """Compile and run a bounded workflow through the normal step transaction path."""
        store = self.require_task(a["task_id"])
        self.authorize_continuation(a)
        task = store.task()
        if not task["contract_version"]:
            raise TaskError(
                "CONTRACT_REQUIRED",
                "Submit the task contract before running a workflow",
                next_action="task_contract_submit",
            )
        layout_deliverable = any(
            item["kind"] in {"image", "pdf", "layout"} for item in task["deliverables"]
        )
        project_deliverable = any(item["kind"] == "project" for item in task["deliverables"])
        has_map_content = bool(
            task["inputs"] or a.get("basemap") or a.get("services") or a.get("data_operations")
        )
        map_requested = (
            a.get("workflow") != "project_operations"
            and (layout_deliverable or (project_deliverable and has_map_content))
        )
        data_output_ids = [item["output"] for item in a.get("data_operations", [])]
        data_output_set = set(data_output_ids)
        unsupported = [] if a.get("workflow") == "project_operations" else [
            item for item in task["deliverables"]
            if item["kind"] not in {"project", "image", "pdf", "layout"}
            and item["id"] not in data_output_set
        ]
        if unsupported:
            raise TaskError(
                "WORKFLOW_MISMATCH",
                "The standard map workflow cannot produce every declared deliverable",
                evidence={
                    "unsupported": [
                        {"id": item["id"], "kind": item["kind"]}
                        for item in unsupported
                    ]
                },
                next_action="Use step_prepare recipes for the requested data outputs",
            )

        task_inputs = task["inputs"]
        source_ids = [] if a.get("workflow") == "project_operations" else [
            key
            for key, item in sorted(
                task_inputs.items(), key=lambda pair: pair[1]["kind"] != "vector"
            )
            if item["kind"] in {"vector", "raster"}
        ]
        requested_layers = [ref.removeprefix("asset:") for ref in (a.get("layers") or [])]
        service_requests = dict(a.get("services") or {})
        if a.get("basemap"):
            service_requests["__default_basemap__"] = a["basemap"]
        available_requested = set(task_inputs) | set(service_requests) | data_output_set
        unknown = sorted(set(requested_layers) - available_requested)
        if unknown:
            raise TaskError(
                "UNKNOWN_ASSET", "Workflow map layers reference unknown logical IDs",
                evidence={"assets": unknown, "available": sorted(available_requested)},
            )
        if not (source_ids or service_requests or data_output_ids) and map_requested:
            raise TaskError(
                "LAYERS_REQUIRED",
                "The standard map workflow needs at least one vector or raster task input",
                next_action="Declare map inputs in task_begin or use individual recipes",
            )
        if len(set(source_ids)) != len(source_ids):
            raise TaskError("INVALID_RECIPE", "Workflow layers must not contain duplicates")

        declared_layouts = [
            item["id"] for item in task["deliverables"] if item["kind"] == "layout"
        ]
        declared_layouts.extend(
            key for key, kind in self.current_contract().intermediates.items()
            if kind == "layout"
        )
        if len(declared_layouts) > 1:
            raise TaskError(
                "TASK_AMBIGUOUS",
                "A standard workflow creates one layout but multiple layout IDs are declared",
                evidence={
                    "questions": [
                        "Which declared layout should be the standard map layout?"
                    ],
                    "layouts": declared_layouts,
                },
                next_action="ask_user",
            )

        reserved = set(task_inputs) | {
            item["id"] for item in task["deliverables"]
        } | set(self.current_contract().intermediates) | set(self.assets())

        def unique_id(base):
            candidate = base[:128]
            number = 2
            while candidate in reserved:
                suffix = f"_{number}"
                candidate = base[: 128 - len(suffix)] + suffix
                number += 1
            reserved.add(candidate)
            return candidate

        used_step_ids = {row[0] for row in store.db.execute("SELECT id FROM steps")}

        def step_id(action, key=""):
            base = "workflow_" + action + ("_" + key if key else "")
            if len(base) > 128:
                suffix = hashlib.sha256(base.encode()).hexdigest()[:12]
                base = base[:115] + "_" + suffix
            candidate, number = base, 2
            while candidate in used_step_ids:
                suffix = f"_{number}"
                candidate = base[:128 - len(suffix)] + suffix
                number += 1
            used_step_ids.add(candidate)
            return candidate

        loaded = {key: unique_id("map_" + key) for key in source_ids}
        layout_id = declared_layouts[0] if declared_layouts else unique_id("map_layout")
        service_ids = {
            key: (unique_id("map_basemap") if key == "__default_basemap__" else key)
            for key in service_requests
        }
        data_loaded = {
            key: unique_id("map_" + key) for key in data_output_ids
        } if map_requested else {}
        geometry_types = {}
        for key in source_ids:
            if task_inputs[key]["kind"] == "vector":
                metadata = await self.bridge.call(
                    "_inspect", {"source": task_inputs[key]["path"], "kind": "vector"}
                )
                geometry_types[key] = metadata.get("geometry_type", "")
        recipes = []
        if (a.get("project") or {}).get("action", "current") != "current":
            recipes.append((
                step_id("project"),
                {"action": "project_setup", "project": a["project"]},
            ))
        for key in source_ids:
            recipes.append((
                step_id("load", key),
                {"action": "load", "source": f"asset:{key}", "output": loaded[key]},
            ))
            if a.get("style_layers", True):
                kind = task_inputs[key]["kind"]
                style = {
                    "action": "style_" + kind,
                    "layer": f"asset:{loaded[key]}",
                }
                if kind == "raster":
                    style["ramp"] = a["raster_ramp"]
                    style.update({name: value for name, value in
                                  a.get("raster_styles", {}).get(key, {}).items()
                                  if value is not None})
                elif "Polygon" in geometry_types.get(key, ""):
                    # Keep an enclosing boundary visible without hiding rasters below it.
                    style["color"] = "transparent"
                if kind == "vector":
                    style.update({name: value for name, value in
                                  a.get("vector_styles", {}).get(key, {}).items()
                                  if value is not None})
                recipes.append((step_id("style", key), style))
        for key, service in service_requests.items():
            recipes.append((
                step_id("service", service_ids[key]),
                {"action": "add_basemap", "service": service, "output": service_ids[key]},
            ))
        for index, operation in enumerate(a.get("data_operations", []), 1):
            operation = dict(operation)
            if operation.get("layer") in loaded:
                operation["layer"] = loaded[operation["layer"]]
            recipes.append((
                step_id("data", f"{index}_{operation['output']}"),
                {"action": "vector_data", "data_operation": operation},
            ))
            if operation["output"] in data_loaded:
                recipes.append((
                    step_id("load", operation["output"]),
                    {"action": "load", "source": f"asset:{operation['output']}",
                     "output": data_loaded[operation["output"]]},
                ))
        for key, service in service_requests.items():
            logical = service_ids[key]
            kind = "vector" if service.get("provider") == "wfs" else "raster"
            styles = a.get(f"{kind}_styles", {})
            style = styles.get(key) or styles.get(logical)
            if style:
                recipes.append((
                    step_id("style", logical),
                    {"action": f"style_{kind}", "layer": f"asset:{logical}",
                     **{name: value for name, value in style.items() if value is not None}},
                ))
        for logical in data_output_ids:
            style = a.get("vector_styles", {}).get(logical)
            if style and logical in data_loaded:
                recipes.append((
                    step_id("style", logical),
                    {"action": "style_vector", "layer": f"asset:{data_loaded[logical]}",
                     **{name: value for name, value in style.items() if value is not None}},
                ))
        for index, operation in enumerate(a.get("layer_operations", []), 1):
            operation = dict(operation)

            def workflow_layer_ref(value):
                return loaded.get(value, service_ids.get(value, data_loaded.get(value, value)))

            if operation.get("layer"):
                operation["layer"] = workflow_layer_ref(operation["layer"])
            for field in ("order", "layers"):
                if operation.get(field):
                    operation[field] = [workflow_layer_ref(value) for value in operation[field]]
            recipes.append((
                step_id("layer", str(index)),
                {"action": "layer_manage", "layer_operation": operation},
            ))
        if a.get("project_update"):
            recipes.append((
                step_id("project_update"),
                {"action": "project_update", "project_update": a["project_update"]},
            ))
        if map_requested:
            thematic_ids = [loaded[key] for key in source_ids]
            thematic_ids.extend(data_loaded.values())
            overlay_ids = [
                service_ids[key] for key, service in service_requests.items()
                if service.get("role") == "overlay" or service.get("provider") == "wfs"
            ]
            basemap_ids = [
                service_ids[key] for key, service in service_requests.items()
                if service_ids[key] not in overlay_ids
            ]
            if requested_layers:
                layout_layers = [
                    loaded.get(key, service_ids.get(key, data_loaded.get(key, key)))
                    for key in requested_layers
                ]
            else:
                layout_layers = thematic_ids + overlay_ids + basemap_ids
            recipes.append((
                step_id("layout"),
                {
                "action": "create_layout",
                "layers": [f"asset:{key}" for key in layout_layers],
                "extent_layer": f"asset:{thematic_ids[0]}" if len(thematic_ids) == 1 else None,
                "output": layout_id,
                "title": a.get("title"),
                "legend_title": a.get("legend_title"),
                "map_language": a.get("map_language", "auto"),
                "show_legend_title": a.get("show_legend_title"),
                "north_arrow": a.get("north_arrow", False),
                "page_orientation": a.get("page_orientation", "auto"),
                "map_crs": a.get("map_crs"),
                "map_frame": a.get("map_frame", {}),
                "coordinate_crs": a.get("coordinate_crs"),
                "map_elements": a.get("map_elements", {}),
                "coordinate_annotations": a.get("coordinate_annotations", {}),
                },
            ))
        for item in task["deliverables"]:
            if item["kind"] in {"image", "pdf"}:
                recipes.append((
                    step_id("export", item["id"]),
                    {
                        "action": "export_map",
                        "layout": f"asset:{layout_id}",
                        "output": item["id"],
                        "dpi": a["dpi"],
                    },
                ))
        for item in task["deliverables"]:
            if item["kind"] == "project":
                recipes.append((
                    step_id("save", item["id"]),
                    {"action": "save_project", "output": item["id"]},
                ))

        continuation = a["continuation_token"]
        completed = []
        for current_step, recipe in recipes:
            from .task_tools import StepPrepare

            prepared_arguments = StepPrepare.model_validate({
                "task_id": store.task_id,
                "continuation_token": continuation,
                "step_id": current_step,
                **recipe,
            }).model_dump()
            prepared = await self.prepare_step(prepared_arguments)
            execute_call = prepared["next_call"]["arguments"]
            row = store.db.execute(
                "SELECT body FROM steps WHERE id=?", (current_step,)
            ).fetchone()
            step = StepContract.model_validate_json(row["body"])
            authorization = self.authorize_continuation(
                execute_call, purpose="execute", step_id=current_step
            )
            outcome = await self.execute(
                step.operation,
                step.arguments,
                {"task_id": store.task_id, "step_id": current_step, **authorization},
            )
            continuation = outcome["continuation_token"]
            completed.append({
                "step_id": current_step,
                "action": recipe["action"],
                "status": "COMMITTED",
                "checks": [
                    {"id": report["id"], "status": report["status"]}
                    for report in outcome.get("validation", [])
                ],
            })

        result = self.task_delta(continuation_token=continuation)
        result.update({
            "workflow": a["workflow"],
            "completed_steps": completed,
            "assets": self.deliverable_assets(),
            "next_call": self.next_call_after_execution(continuation),
        })
        return result

    def compile_recipe(self, a):
        store = self.require_task(a["task_id"])
        if not store.task()["contract_version"]:
            raise TaskError(
                "CONTRACT_REQUIRED",
                "Submit the task contract before preparing steps",
                next_action="task_contract_submit",
            )
        action = a["action"]
        required = {
            "project_setup": (),
            "project_update": ("project_update",),
            "load": ("source", "output"),
            "add_basemap": ("output",),
            "layer_manage": ("layer_operation",),
            "vector_data": ("data_operation",),
            "clip_raster": ("raster", "mask", "output"),
            "clip_vector": ("source", "overlay", "output"),
            "reproject_vector": ("source", "target_crs", "output"),
            "style_raster": ("layer",),
            "style_vector": ("layer",),
            "create_layout": ("output",),
            "export_map": ("layout", "output"),
            "save_project": ("output",),
        }[action]
        missing = [field for field in required if not a.get(field)]
        if missing:
            if action == "reproject_vector" and missing == ["target_crs"]:
                raise TaskError(
                    "TASK_AMBIGUOUS",
                    "The requested target CRS is not specified",
                    evidence={"questions": ["Which target CRS should the vector use?"]},
                    next_action="ask_user",
                )
            raise TaskError(
                "INVALID_RECIPE",
                "Recipe fields are missing",
                evidence={"action": action, "missing_fields": missing},
                next_action="Supply the listed fields to step_prepare; do not author a full contract",
            )

        assets = self.assets()

        if action == "project_setup":
            project = a.get("project")
            if not project or project.get("action") == "current":
                raise TaskError("PROJECT_ACTION_REQUIRED", "Choose create or open for project setup")
            return {
                "operation": "project",
                "arguments": {
                    "action": project["action"], "path": project.get("path"),
                    "crs": project.get("crs") or "EPSG:4326",
                    "title": project.get("title") or "Smart-QGIS",
                },
                "inputs": [], "outputs": [],
                "reason": "Create or open the user-requested QGIS project",
            }

        if action == "project_update":
            update = a["project_update"]
            return {
                "operation": "project",
                "arguments": {
                    "action": "update",
                    "crs": update.get("crs"),
                    "title": update.get("title"),
                },
                "inputs": [], "outputs": [],
                "reason": "Apply the user-requested project title or display CRS",
            }

        def asset(field, kinds=None):
            reference = a[field]
            if not isinstance(reference, str) or not reference.startswith("asset:"):
                raise TaskError(
                    "INVALID_RECIPE",
                    "Recipe inputs use asset:<logical_id> references",
                    evidence={"field": field, "expected_format": "asset:<logical_id>"},
                )
            key = reference[6:]
            if key not in assets:
                raise TaskError(
                    "UNKNOWN_ASSET",
                    "Recipe references an unavailable logical asset",
                    evidence={"field": field, "asset": key, "available": sorted(assets)},
                )
            if kinds and assets[key]["kind"] not in kinds:
                raise TaskError(
                    "ASSET_KIND_MISMATCH",
                    "Recipe input has the wrong data kind",
                    evidence={
                        "field": field,
                        "asset": key,
                        "actual": assets[key]["kind"],
                        "expected": sorted(kinds),
                    },
                )
            return key

        def output_kind(key, expected):
            declared = {
                item["id"]: item["kind"] for item in store.task()["deliverables"]
            }
            declared.update(self.current_contract().intermediates)
            actual = declared.get(key)
            if actual not in expected:
                raise TaskError(
                    "OUTPUT_KIND_REQUIRED",
                    "Recipe output must be declared with a compatible kind",
                    evidence={
                        "output": key,
                        "declared_kind": actual,
                        "expected": sorted(expected),
                    },
                    next_action=(
                        "Use a matching task deliverable or declare the intermediate kind in "
                        "the task contract before preparing this step"
                    ),
                )
            return actual

        def layer_reference(reference, kinds=None):
            if not isinstance(reference, str) or not reference:
                raise TaskError("INVALID_RECIPE", "A nonempty layer reference is required")
            key = reference.removeprefix("asset:")
            if key in assets:
                if kinds and assets[key]["kind"] not in kinds:
                    raise TaskError(
                        "ASSET_KIND_MISMATCH", "Layer reference has the wrong data kind",
                        evidence={"asset": key, "actual": assets[key]["kind"], "expected": sorted(kinds)},
                    )
                if not assets[key].get("layer_id"):
                    raise TaskError(
                        "LAYER_NOT_LOADED", "The requested asset is not loaded in the QGIS project",
                        evidence={"asset": key},
                    )
                return f"asset:{key}", key
            # Exact current-project IDs/names are allowed only because the
            # server will resolve them uniquely in the checkpointed worker.
            return reference, None

        if action == "load":
            source = asset("source", {"vector", "raster"})
            kind = assets[source]["kind"]
            return {
                "operation": "load_data",
                "arguments": {
                    "path": f"asset:{source}",
                    "name": a["output"],
                    "kind": kind,
                },
                "inputs": [source],
                "outputs": [{"id": a["output"], "kind": kind, "binding": "layer"}],
                "reason": "Load a declared input into the QGIS project",
            }
        if action == "add_basemap":
            service = a.get("service") or {"provider": "openstreetmap"}
            provider = service.get("provider", "openstreetmap")
            worker_service = {
                "openstreetmap": "osm", "xyz": "xyz", "wms": "wms",
                "wmts": "wmts", "wfs": "wfs",
            }[provider]
            kind = "vector" if provider == "wfs" else "raster"
            return {
                "operation": "add_basemap",
                "arguments": {
                    "service": worker_service, "url": service.get("url"), "uri": service.get("uri"),
                    "name": service.get("name"), "attribution": service.get("attribution"),
                    "role": service.get("role"), "layer_name": service.get("layer_name"),
                    "type_name": service.get("type_name"), "style_name": service.get("style_name"),
                    "crs": service.get("crs"), "image_format": service.get("image_format", "image/png"),
                    "version": service.get("version"), "authcfg": service.get("authcfg"),
                    "zmin": service.get("zmin", 0), "zmax": service.get("zmax", 19),
                },
                "inputs": [],
                "outputs": [{"id": a["output"], "kind": kind, "binding": "layer"}],
                "reason": "Add the requested remote map layer",
            }
        if action == "layer_manage":
            operation = a["layer_operation"]
            arguments = {key: value for key, value in operation.items() if value is not None}
            input_ids = []
            if operation.get("layer"):
                arguments["layer"], key = layer_reference(operation["layer"])
                if key:
                    input_ids.append(key)
            for field in ("order", "layers"):
                if operation.get(field):
                    resolved = [layer_reference(value) for value in operation[field]]
                    arguments[field] = [value for value, _key in resolved]
                    input_ids.extend(key for _value, key in resolved if key)
            return {
                "operation": "layers", "arguments": arguments,
                "inputs": list(dict.fromkeys(input_ids)), "outputs": [],
                "reason": f"Apply requested layer operation: {operation['action']}",
            }
        if action == "vector_data":
            operation = a["data_operation"]
            output = operation["output"]
            output_kind(output, {"vector"})
            arguments = {
                "action": {
                    "export": "export", "create": "create_export", "edit": "edit_export",
                }[operation["action"]],
                "path": f"output:{output}",
                "crs": operation.get("crs"),
                "selected_only": operation.get("selected_only", False),
                "expression": operation.get("expression"),
                "geojson": operation.get("geojson"),
                "geojson_crs": operation.get("geojson_crs", "EPSG:4326"),
                "updates": operation.get("updates", []),
                "name": operation.get("name") or output,
            }
            input_ids = []
            if operation.get("layer"):
                arguments["layer"], key = layer_reference(operation["layer"], {"vector"})
                if key:
                    input_ids.append(key)
            return {
                "operation": "vector_data", "arguments": arguments,
                "inputs": input_ids,
                "outputs": [{"id": output, "kind": "vector", "binding": "path"}],
                "reason": f"Create a managed copy for vector {operation['action']}",
            }
        if action == "clip_raster":
            raster, mask = asset("raster", {"raster"}), asset("mask", {"vector"})
            parameters = {
                "INPUT": f"asset:{raster}",
                "MASK": f"asset:{mask}",
                "CROP_TO_CUTLINE": True,
                "KEEP_RESOLUTION": True,
                "OUTPUT": f"output:{a['output']}",
            }
            if a.get("nodata") is not None:
                parameters["NODATA"] = a["nodata"]
            if a.get("all_touched"):
                parameters["EXTRA"] = "-wo CUTLINE_ALL_TOUCHED=TRUE"
            return {
                "operation": "run_processing",
                "arguments": {
                    "algorithm": "gdal:cliprasterbymasklayer",
                    "parameters": parameters,
                    "load_outputs": True,
                },
                "inputs": [raster, mask],
                "outputs": [{
                    "id": a["output"], "kind": "raster", "binding": "OUTPUT"
                }],
                "reason": "Clip a raster with a declared vector mask",
            }
        if action == "clip_vector":
            source = asset("source", {"vector"})
            overlay = asset("overlay", {"vector"})
            return {
                "operation": "run_processing",
                "arguments": {
                    "algorithm": "native:clip",
                    "parameters": {
                        "INPUT": f"asset:{source}",
                        "OVERLAY": f"asset:{overlay}",
                        "OUTPUT": f"output:{a['output']}",
                    },
                    "load_outputs": True,
                },
                "inputs": [source, overlay],
                "outputs": [{
                    "id": a["output"], "kind": "vector", "binding": "OUTPUT"
                }],
                "reason": "Clip a vector with a declared vector overlay",
            }
        if action == "reproject_vector":
            source = asset("source", {"vector"})
            return {
                "operation": "run_processing",
                "arguments": {
                    "algorithm": "native:reprojectlayer",
                    "parameters": {
                        "INPUT": f"asset:{source}",
                        "TARGET_CRS": a["target_crs"],
                        "OUTPUT": f"output:{a['output']}",
                    },
                    "load_outputs": True,
                },
                "inputs": [source],
                "outputs": [{
                    "id": a["output"], "kind": "vector", "binding": "OUTPUT"
                }],
                "resampling": True,
                "reason": "Reproject a vector to the user-selected CRS",
            }
        if action in {"style_raster", "style_vector"}:
            kind = "raster" if action == "style_raster" else "vector"
            layer = asset("layer", {kind})
            arguments = {"layer": f"asset:{layer}", "opacity": a["opacity"]}
            qml_mode = (
                action == "style_raster" and a.get("mode") == "qml"
            ) or (
                action == "style_vector" and a.get("renderer") == "qml"
            )
            if qml_mode:
                qml_id = a.get("qml_asset")
                if not qml_id or qml_id not in assets or assets[qml_id]["kind"] != "style":
                    raise TaskError(
                        "STYLE_ASSET_REQUIRED",
                        "QML styling requires qml_asset to name a declared style input",
                        evidence={"qml_asset": qml_id, "available": sorted(assets)},
                    )
                return {
                    "operation": "style_file",
                    "arguments": {
                        "action": "load", "layer": f"asset:{layer}",
                        "path": f"asset:{qml_id}",
                    },
                    "inputs": [layer, qml_id],
                    "reason": f"Load the requested QML presentation for the {kind}",
                }
            if action == "style_raster":
                mode = a.get("mode", "continuous")
                if mode in {"gray", "rgb", "hillshade"}:
                    arguments.update(
                        mode=mode, band=a["band"], red=a.get("red") or 1,
                        green=a.get("green") or 2, blue=a.get("blue") or 3,
                        azimuth=a.get("azimuth", 315), altitude=a.get("altitude", 45),
                        z_factor=a.get("z_factor", 1),
                    )
                    operation = "render_raster"
                else:
                    arguments.update(
                        mode=mode, ramp=a["ramp"], band=a["band"], classes=a["classes"],
                        color=a.get("color", "#666666"), label=a.get("label"),
                        minimum=a.get("minimum"), maximum=a.get("maximum"),
                    )
                    operation = action
            else:
                if a.get("renderer") == "graduated":
                    arguments.update(
                        field=a.get("graduated_field"), ramp=a["ramp"], classes=a["classes"],
                        method=a.get("method", "equal_interval"), label_field=a.get("label_field"),
                    )
                    operation = "style_graduated"
                else:
                    arguments.update(
                        renderer=a.get("renderer", "single"), color=a["color"],
                        outline=a["outline"], width=a["width"], size=a["size"],
                        category_field=a.get("category_field"), categories=a.get("categories"),
                        rules=a.get("rules"), marker=a.get("marker", "circle"),
                        line_style=a.get("line_style", "solid"),
                        label_field=a.get("label_field"),
                    )
                    operation = action
            return {
                "operation": operation,
                "arguments": arguments,
                "inputs": [layer],
                "reason": f"Apply the requested {kind} presentation",
            }
        if action == "create_layout":
            layer_refs = a.get("layers")
            if layer_refs:
                invalid = [ref for ref in layer_refs if not ref.startswith("asset:")]
                if invalid:
                    raise TaskError(
                        "INVALID_RECIPE",
                        "Layout layers use asset:<logical_id> references",
                        evidence={"field": "layers", "expected_format": "asset:<logical_id>"},
                    )
                layer_ids = [ref[6:] for ref in layer_refs]
            else:
                layer_ids = [
                key
                for key, item in sorted(
                    assets.items(),
                    key=lambda pair: (
                        pair[1].get("remote", False),
                        pair[1]["kind"] != "vector",
                    ),
                )
                if item.get("layer_id")
                ]
            if not layer_ids:
                raise TaskError(
                    "LAYERS_REQUIRED",
                    "Create-layout recipe needs at least one loaded layer",
                    next_action="Prepare and execute load recipes before the layout",
                )
            for key in layer_ids:
                if key not in assets or not assets[key].get("layer_id"):
                    raise TaskError(
                        "LAYER_NOT_LOADED",
                        "Create-layout recipe references a layer not loaded in QGIS",
                        evidence={"asset": key},
                        next_action="Prepare and execute a load recipe for this asset first",
                    )
            extent = asset("extent_layer") if a.get("extent_layer") else None
            if extent is not None and extent not in layer_ids:
                raise TaskError(
                    "INVALID_RECIPE",
                    "extent_layer must also appear in the ordered layout layers",
                )
            omissions = set(self.current_contract().map_omissions)
            return {
                "operation": "layout",
                "arguments": {
                    "action": "create",
                    "name": a["output"],
                    "title": a.get("title") or self.default_map_title(store.task()["goal"]),
                    "legend_title": a.get("legend_title"),
                    "map_language": a.get("map_language", "auto"),
                    "show_legend_title": a.get("show_legend_title"),
                    "page_orientation": a.get("page_orientation", "auto"),
                    "crs": a.get("map_crs"),
                    "map_frame": a.get("map_frame", {}),
                    "show_title": "title" not in omissions,
                    "layers": [f"asset:{key}" for key in layer_ids],
                    "extent_layer": f"asset:{extent}" if extent else None,
                    "legend": "legend" not in omissions,
                    "scalebar": "scalebar" not in omissions,
                    "north_arrow": a.get("north_arrow", False) and "north_arrow" not in omissions,
                    "grid": "coordinates" not in omissions,
                    "grid_crs": a.get("coordinate_crs"),
                    "map_elements": a.get("map_elements", {}),
                    "coordinate_annotations": a.get("coordinate_annotations", {}),
                },
                "inputs": layer_ids,
                "outputs": [{
                    "id": a["output"], "kind": "layout", "binding": "layout"
                }],
                "reason": "Create a standard map layout with required map elements",
            }
        if action == "export_map":
            layout = asset("layout", {"layout"})
            kind = output_kind(a["output"], {"image", "pdf"})
            return {
                "operation": "export_map",
                "arguments": {
                    "layout": f"asset:{layout}",
                    "path": f"output:{a['output']}",
                    "dpi": a["dpi"],
                },
                "inputs": [layout],
                "outputs": [{"id": a["output"], "kind": kind, "binding": "path"}],
                "reason": "Export the approved map layout",
            }
        if action == "save_project":
            output_kind(a["output"], {"project"})
            return {
                "operation": "project",
                "arguments": {"action": "save", "path": f"output:{a['output']}"},
                "outputs": [{
                    "id": a["output"], "kind": "project", "binding": "path"
                }],
                "reason": "Save the editable QGIS project",
            }
        raise AssertionError(action)

    async def submit_step_contract(self, a):
        authorization = self.authorize_continuation(a)
        store = self.require_task(a["task_id"])
        expected = authorization["expected_state_version"]
        store.require_version(expected)
        if store.task()["status"] == "BLOCKED":
            await self.restore_committed()
        step = self.parse_contract(StepContract, a["contract"])
        if step.unresolved_questions:
            raise TaskError(
                "TASK_AMBIGUOUS", "Resolve input and parameter questions before processing",
                evidence={"questions": step.unresolved_questions}, next_action="ask_user",
            )
        if a.get("inherit_required_checks"):
            self.inherit_repair_checks(step)
        step.arguments = self.normalize(step.operation, step.arguments)
        if is_read(step.operation, step.arguments):
            raise TaskError("READ_ONLY_STEP", "Read-only queries do not require a step contract")
        assets = self.assets()
        unknown = set(step.inputs) - assets.keys()
        if unknown:
            raise TaskError(
                "UNKNOWN_ASSET", "Step inputs are unavailable",
                evidence={"assets": sorted(unknown)},
            )
        if any(output.id in assets for output in step.outputs):
            raise TaskError("ASSET_EXISTS", "Output IDs cannot overwrite existing assets")
        declared_kinds = {item["id"]: item["kind"] for item in store.task()["deliverables"]}
        declared_kinds.update(self.current_contract().intermediates)
        for output in step.outputs:
            if output.id in declared_kinds and output.kind != declared_kinds[output.id]:
                raise TaskError(
                    "ASSET_KIND_MISMATCH", "Step output kind differs from task declaration",
                    evidence={"asset": output.id, "expected": declared_kinds[output.id]},
                )
        self.check_references(step)
        defaults = await self.check_operation(step, assets)
        if step.operation == "run_processing":
            preview = self.resolve(
                step.arguments, assets,
                {output.id: "TEMPORARY_OUTPUT" for output in step.outputs},
            )
            try:
                await self.bridge.call("_processing_preflight", preview)
            except WorkerError as exc:
                if exc.code != "INVALID_PARAMETERS":
                    raise
                raise TaskError(
                    "INVALID_PARAMETERS", "QGIS rejected processing parameters before execution",
                    phase="preflight", evidence={"diagnostic": str(exc)},
                    next_action=(
                        "Correct from algorithm help. If a required value has no default and "
                        "is not uniquely determined, ask the user. No attempt was started."
                    ),
                ) from exc
        system_pre, system_post = family_checks(
            step, defaults, map_omissions=self.current_contract().map_omissions,
        )
        step.preconditions.extend(system_pre)
        step.postconditions.extend(system_post)
        step = self.parse_contract(StepContract, step.model_dump())
        parents = {assets[key].get("step_id") for key in step.inputs} - {None}
        reads_project = any(assets[key].get("layer_id") for key in step.inputs)
        if self.changes_project(step) or reads_project:
            for previous in store.db.execute(
                "SELECT steps.id,steps.body FROM attempts JOIN steps ON steps.id=attempts.step_id "
                "WHERE attempts.status='COMMITTED' AND steps.status='COMMITTED' "
                "ORDER BY attempts.created DESC"
            ):
                if self.changes_project(StepContract.model_validate_json(previous["body"])):
                    parents.add(previous["id"])
                    break
        dependencies = sorted(set(step.dependencies) | parents)
        self.check_repair(step)
        step.dependencies = dependencies
        store.save_step(a["step_id"], step.model_dump(), dependencies, expected)
        result = self.task_delta()
        result.update({
            "step_id": a["step_id"],
            "step_status": "PLANNED",
        })
        result["next_call"] = {
            "tool": "step_execute",
            "arguments": {
                "task_id": a["task_id"],
                "step_id": a["step_id"],
                "continuation_token": self.issue_continuation("execute", a["step_id"]),
            },
        }
        return result

    def normalize(self, operation, arguments):
        from .tools import SPECS, WORKER_OPERATIONS

        schemas = {WORKER_OPERATIONS.get(name, name): schema for name, schema, _ in SPECS}
        if operation not in schemas:
            raise TaskError(
                "UNKNOWN_OPERATION",
                "Step operation must name an MCP GIS tool, not a QGIS algorithm ID",
                evidence={
                    "available_operations": sorted(schemas),
                    "processing_structure": {
                        "operation": "run_processing",
                        "arguments": {"algorithm": "provider:algorithm", "parameters": {}},
                    },
                },
                next_action="For a QGIS algorithm use operation=run_processing, put its ID in arguments.algorithm and its GIS parameters in arguments.parameters; declare inputs and outputs separately",
            )
        try:
            return schemas[operation].model_validate(arguments).model_dump()
        except ValidationError as exc:
            raise TaskError(
                "INVALID_STEP_ARGUMENTS",
                "Step arguments must match the selected MCP tool's input structure",
                evidence={
                    "operation": operation,
                    "errors": [
                        {"field": list(error["loc"]), "message": error["msg"]}
                        for error in exc.errors(include_input=False, include_url=False)[:12]
                    ],
                    "total_errors": exc.error_count(),
                },
                next_action=(
                    "Use arguments={algorithm: <id>, parameters: {<algorithm parameters>}, load_outputs: <bool>}"
                    if operation == "run_processing"
                    else "Read the selected tool's input schema and correct step arguments"
                ),
            ) from exc

    @staticmethod
    def parse_contract(model, body):
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            errors = [
                {"field": list(error["loc"]), "message": error["msg"], "type": error["type"]}
                for error in exc.errors(include_input=False, include_url=False)[:12]
            ]
            raise TaskError(
                "INVALID_CONTRACT",
                "Contract structure or constraints are invalid",
                evidence={"errors": errors, "total_errors": exc.error_count()},
                next_action="Correct the listed fields; use contract_help(kind=...) for validator parameters",
            ) from exc

    @staticmethod
    def changes_project(step):
        return step.operation != "run_processing" or step.arguments.get("load_outputs", True)

    def inherit_repair_checks(self, step):
        """Copy locked obligations only on explicit request; never replace supplied checks."""
        row = self.require_task().db.execute(
            "SELECT body,status FROM steps WHERE id=?", (step.repairs_step,)
        ).fetchone()
        if row is None or row["status"] not in {"FAILED", "INVALIDATED"}:
            raise TaskError("INVALID_REPAIR", "Check inheritance requires a failed or invalidated repairs_step")
        original = StepContract.model_validate_json(row["body"])
        supplied = {check.id for check in [*step.preconditions, *step.postconditions]}
        for phase in ("preconditions", "postconditions"):
            for check in getattr(original, phase):
                if check.required and not check.id.startswith("system_") and check.id not in supplied:
                    getattr(step, phase).append(check.model_copy(deep=True))

    def check_repair(self, step):
        records = {
            row["id"]: StepContract.model_validate_json(row["body"])
            for row in self.store.db.execute("SELECT id,body FROM steps")
        }
        failed_attempts = {
            row[0]
            for row in self.store.db.execute(
                "SELECT DISTINCT attempts.step_id FROM attempts JOIN steps ON steps.id=attempts.step_id "
                "WHERE attempts.status='FAILED' AND steps.status!='COMMITTED'"
            )
        }
        failed = set(failed_attempts)
        # Explicit invalidation affects the whole dependency closure, not only
        # roots named in REPAIR_STARTED. Every affected step needs a replacement.
        failed.update(row[0] for row in self.store.db.execute(
            "SELECT id FROM steps WHERE status='INVALIDATED'"
        ))
        for row in self.store.db.execute("SELECT body FROM events WHERE kind='REPAIR_STARTED'"):
            failed.update(json.loads(row[0])["roots"])
        def root(key):
            while records[key].repairs_step:
                key = records[key].repairs_step
            return key

        planned_at = {}
        input_epochs = {}
        last_user_clarification = 0
        for event in self.store.db.execute("SELECT sequence,kind,body FROM events ORDER BY sequence"):
            body = json.loads(event["body"])
            if event["kind"] == "STEP_PLANNED":
                planned_at[body["step_id"]] = event["sequence"]
            elif event["kind"] == "USER_CLARIFICATION":
                last_user_clarification = event["sequence"]
            elif event["kind"] == "INPUTS_REVISED":
                for key in body["steps"]:
                    input_epochs[root(key)] = event["sequence"]

        output_ids = {output.id for output in step.outputs}
        related = []
        for key in failed:
            old = records[key]
            same_output = bool(output_ids.intersection(output.id for output in old.outputs))
            same_target = bool(step.arguments.get("layer")) and step.arguments.get(
                "layer"
            ) == old.arguments.get("layer")
            same_request = old.operation == step.operation and old.arguments == step.arguments
            if same_output or same_request or (same_target and old.operation == step.operation):
                related.append(key)
        if related and not step.repairs_step:
            raise TaskError(
                "REPAIR_REFERENCE_REQUIRED",
                "Link the revised attempt to a failed step using repairs_step",
                evidence={"failed_steps": sorted(related)},
            )
        if not step.repairs_step:
            return
        if step.repairs_step not in failed:
            raise TaskError("INVALID_REPAIR", "repairs_step must identify a failed or explicitly invalidated step")
        for key in failed_attempts:
            old = records[key]
            current_assets = self.assets()
            upstream_rebuilt = any(
                planned_at.get(current_assets.get(asset, {}).get("step_id"), 0)
                > planned_at.get(key, 0)
                for asset in old.inputs
            )
            if (old.operation == step.operation and old.arguments == step.arguments
                    and planned_at.get(key, 0) >= input_epochs.get(root(key), 0)
                    and not upstream_rebuilt):
                raise TaskError(
                    "UNCHANGED_REPAIR", "The same failed request cannot be retried unchanged"
                )

        ancestor = root(step.repairs_step)
        repairs = [key for key in records if key != ancestor and root(key) == ancestor
                   and planned_at.get(key, 0) >= input_epochs.get(ancestor, 0)
                   and planned_at.get(key, 0) > last_user_clarification]
        if len(repairs) >= self.correction_limit:
            raise TaskError(
                "CLARIFICATION_REQUIRED",
                "Semantic repair limit reached; ask the user before another revised step",
                evidence={"repairs": len(repairs), "limit": self.correction_limit},
                next_action="After the user's actual guidance, call task_update and revise the failed step",
            )
        # Required obligations survive repairs, including a switch of algorithm.
        original = records[step.repairs_step]
        mismatches = []
        for phase in ("preconditions", "postconditions"):
            current = {check.id: check.model_dump() for check in getattr(step, phase)}
            for check in getattr(original, phase):
                if (check.required and not check.id.startswith("system_")
                        and current.get(check.id) != check.model_dump()):
                    mismatches.append({"id": check.id, "phase": phase})
        if mismatches:
            raise TaskError(
                "CONTRACT_WEAKENING", "Repairs must retain required step checks",
                evidence={"original_step": step.repairs_step, "checks": mismatches},
                next_action=(
                    "Use contract_get for original_step; copy the listed required checks unchanged "
                    "in their original preconditions/postconditions. Omit system_ checks, which "
                    "the server regenerates. Change execution parameters without weakening checks."
                    " Alternatively set inherit_required_checks=true on step_contract_submit to copy "
                    "omitted required checks; supplied conflicting checks remain rejected."
                ),
            )

    def check_references(self, step):
        output_ids = {out.id for out in step.outputs}

        def visit(value):
            if isinstance(value, dict):
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, str) and value.startswith("asset:"):
                if value[6:] not in step.inputs:
                    raise TaskError(
                        "UNDECLARED_INPUT", "Every asset reference must be declared as a step input"
                    )
            elif isinstance(value, str) and value.startswith("output:"):
                if value[7:] not in output_ids:
                    raise TaskError(
                        "UNKNOWN_OUTPUT", "Managed output reference must match a declared output ID",
                        evidence={"declared_output_ids": sorted(output_ids)},
                        next_action="Use output:<outputs[].id>; binding is the operation result field, not the logical output ID",
                    )

        visit(step.arguments)
        known = set(step.inputs) | {out.id for out in step.outputs}
        for check in [*step.preconditions, *step.postconditions]:
            refs = (
                {check.target}
                | set(getattr(check, "inputs", []))
                | set(getattr(check, "layers", []))
            )
            if getattr(check, "reference", None):
                refs.add(check.reference)
            if getattr(check, "source_raster", None):
                refs.add(check.source_raster)
            if not refs <= known:
                raise TaskError(
                    "UNKNOWN_ASSET", "Step checks must reference declared inputs/outputs"
                )
        if any(check.target not in step.inputs for check in step.preconditions):
            raise TaskError("INVALID_PRECONDITION", "Preconditions must target available inputs")

    async def check_operation(self, step, assets=None):
        op, args = step.operation, step.arguments
        defaults = {}
        # All stateful layer references must declare their logical dependencies.
        references = [args[key] for key in ("layer", "extent_layer") if args.get(key)]
        references.extend(args.get("layers") or [])
        references.extend(args.get("order") or [])
        exact_project_references = {
            "layers", "vector_data", "style_vector", "style_graduated",
            "style_raster", "render_raster", "features",
        }
        if op not in exact_project_references and any(
            not ref.startswith("asset:") for ref in references
        ):
            raise TaskError("DECLARED_INPUT_REQUIRED", "Layer references must use asset:<id>")
        if assets is not None:
            unloaded = sorted({
                ref[6:]
                for ref in references
                if ref.startswith("asset:")
                and ref[6:] in assets
                and not assets[ref[6:]].get("layer_id")
            })
            if unloaded:
                raise TaskError(
                    "LAYER_NOT_LOADED",
                    "This operation requires QGIS project layers, not input file paths",
                    evidence={"assets": unloaded},
                    next_action=(
                        "First approve and execute load_data for each listed asset with exactly one "
                        "new vector/raster output using binding='layer'; then reference that output."
                    ),
                )
        filenames = [
            out.filename or out.id + EXTENSIONS.get(out.kind, "")
            for out in step.outputs
            if out.kind != "layout" and out.binding != "layer"
        ]
        if len(set(filenames)) != len(filenames):
            raise TaskError("OUTPUT_COLLISION", "Output filenames must be unique within an attempt")
        creates_layer = op in {"load_data", "add_basemap"} or (
            op == "vector_data" and args["action"] == "create"
        )
        creates_layout = op == "layout" and args["action"] == "create"
        if creates_layer and (len(step.outputs) != 1 or step.outputs[0].binding != "layer"):
            raise TaskError("OUTPUT_REQUIRED", "Declare exactly one layer output")
        if creates_layout and (len(step.outputs) != 1 or step.outputs[0].kind != "layout"):
            raise TaskError("OUTPUT_REQUIRED", "Declare exactly one layout output")
        if creates_layout and not args.get("layers"):
            raise TaskError(
                "LAYERS_REQUIRED",
                "Reliable layouts require an explicit ordered list of logical layers",
            )
        if creates_layout:
            omissions = set(self.current_contract().map_omissions)
            disabled = {
                element
                for element, enabled in {
                    "title": args.get("show_title", True),
                    "legend": args.get("legend", True),
                    "scalebar": args.get("scalebar", True),
                    "coordinates": args.get("grid", True),
                }.items()
                if not enabled
            }
            unauthorized = disabled - omissions
            if unauthorized:
                raise TaskError(
                    "MAP_ELEMENTS_REQUIRED",
                    "Maps include a title, legend, scale bar and coordinate annotations by default",
                    evidence={"elements": sorted(unauthorized)},
                    next_action=(
                        "Enable these layout elements. Disable an element only when the user explicitly "
                        "requested its removal and it is listed in task_contract.map_omissions."
                    ),
                )
            not_removed = omissions - disabled
            if not_removed:
                raise TaskError(
                    "MAP_OMISSION_REQUIRED",
                    "The layout still enables map elements the user explicitly asked to remove",
                    evidence={"elements": sorted(not_removed)},
                    next_action="Disable the listed layout elements to match task_contract.map_omissions.",
                )
        if op == "run_processing":
            if args["algorithm"] == "gdal:cliprasterbymasklayer":
                clip_boundary_rule(args["parameters"].get("EXTRA"))
            elif args["algorithm"] == "gdal:translate":
                gdal_translate_extra(args["parameters"].get("EXTRA"))
            elif args["parameters"].get("EXTRA"):
                raise TaskError(
                    "UNSUPPORTED_SIDE_EFFECT",
                    "Additional GDAL command-line arguments are not allowed",
                )
            info = await self.bridge.call(
                "algorithms", {"action": "help", "algorithm": args["algorithm"]}
            )
            defaults = {item["name"]: item["default"] for item in info["parameters"]}
            unknown = sorted(set(args["parameters"]) - defaults.keys())
            if unknown:
                raise TaskError(
                    "INVALID_PARAMETERS", "Unknown algorithm parameter names",
                    evidence={"unknown_parameters": unknown, "allowed_parameters": sorted(defaults)},
                    next_action="Use parameter names from algorithms help; correct and resubmit this step contract. No execution attempt was started.",
                )
            dests = {item["name"] for item in info["parameters"] if item.get("destination")}
            invalid_bindings = sorted(
                output.binding for output in step.outputs if output.binding not in dests
            )
            if invalid_bindings:
                raise TaskError(
                    "OUTPUT_BINDING", "Step output bindings must name algorithm destinations",
                    evidence={"bindings": invalid_bindings, "destinations": sorted(dests)},
                    next_action=MANAGED_OUTPUT_GUIDANCE,
                )
            missing_required = [
                item["name"] for item in info["parameters"]
                if item.get("required", True) and not item.get("has_default", False)
                and item["name"] not in args["parameters"]
            ]
            missing_destinations = sorted(set(missing_required) & dests)
            if missing_destinations:
                raise TaskError(
                    "MANAGED_OUTPUT_REQUIRED",
                    "Required Processing destinations must use declared managed outputs",
                    evidence={"parameters": missing_destinations},
                    next_action=MANAGED_OUTPUT_GUIDANCE,
                )
            if missing_required:
                raise TaskError(
                    "INVALID_PARAMETERS", "Required Processing parameters are missing",
                    evidence={"parameters": missing_required},
                    next_action="Use prepare_algorithm so unresolved required values become structured questions",
                )
            for name in dests:
                if name in args["parameters"] and not str(args["parameters"][name]).startswith(
                    "output:"
                ):
                    raise TaskError(
                        "MANAGED_OUTPUT_REQUIRED", "Processing outputs must use output:<id>",
                        next_action=MANAGED_OUTPUT_GUIDANCE,
                    )
            for definition in info["parameters"]:
                key = definition["name"]
                if (
                    definition.get("type") in {"source", "vector", "raster", "maplayer", "multilayer"}
                    and key in args["parameters"]
                ):
                    value = args["parameters"][key]
                    values = value if isinstance(value, list) else [value]
                    if any(
                        not isinstance(item, str) or not item.startswith("asset:")
                        for item in values
                    ):
                        raise TaskError(
                            "DECLARED_INPUT_REQUIRED",
                            "All processing layers must be declared assets",
                        )
        if op == "load_data":
            if not args["path"].startswith("asset:") or args.get("provider") not in {
                None,
                "ogr",
                "gdal",
            }:
                raise TaskError(
                    "DECLARED_INPUT_REQUIRED", "Load a declared local input using asset:<id>"
                )
        if (
            op == "style_file"
            and args["action"] == "load"
            and not args["path"].startswith("asset:")
        ):
            raise TaskError("DECLARED_INPUT_REQUIRED", "QML input must be a declared local asset")
        writes_path = (
            op == "export_map"
            or (op in {"project", "style_file"} and args["action"] == "save")
            or (op == "vector_data" and args["action"] in {"export", "create_export", "edit_export"})
            or (op == "layout" and args["action"] == "template")
        )
        if writes_path and not (args.get("path") or "").startswith("output:"):
            raise TaskError(
                "MANAGED_OUTPUT_REQUIRED", "Use output:<id> instead of an external output path"
            )
        if op == "project" and args["action"] == "create" and args.get("path"):
            raise TaskError(
                "MANAGED_OUTPUT_REQUIRED", "Create without a path; save using a managed output"
            )
        return defaults

    def resolve(self, value, assets, outputs=None, *, prefer_path=False):
        if isinstance(value, dict):
            return {
                key: self.resolve(item, assets, outputs, prefer_path=prefer_path or key == "path")
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.resolve(item, assets, outputs, prefer_path=prefer_path) for item in value]
        if isinstance(value, str) and value.startswith("asset:"):
            asset = assets.get(value[6:])
            if asset is None:
                raise TaskError(
                    "UNKNOWN_ASSET", "Logical asset is unavailable", evidence={"asset": value}
                )
            return (
                (asset.get("path") if prefer_path else asset.get("layer_id"))
                or asset.get("path")
                or asset.get("layout")
            )
        if isinstance(value, str) and value.startswith("output:"):
            if not outputs or value[7:] not in outputs:
                raise TaskError(
                    "UNKNOWN_OUTPUT", "Declare this managed output in the step contract"
                )
            return outputs[value[7:]]
        return value

    async def verify_inputs(self):
        changed = []
        for key, item in self.store.task()["inputs"].items():
            current = await asyncio.to_thread(fingerprint, item["path"])
            if current != item["fingerprint"]:
                changed.append(key)
        if changed:
            roots = []
            for row in self.store.db.execute("SELECT id,body FROM steps"):
                if set(json.loads(row["body"])["inputs"]).intersection(changed):
                    roots.append(row["id"])
            if roots and not self.store.unresolved():
                self.store.invalidate(roots, "Declared input contents changed")
            raise TaskError(
                "INPUT_CHANGED",
                "Inputs differ from the task's recorded versions",
                evidence={"inputs": changed},
                next_action="Inspect changed inputs with include_fingerprint=true, then task_revise_inputs with current digests and a reason; or restore the original files",
            )

    async def make_checkpoint(self, directory, assets):
        cp = await self.bridge.call("_snapshot", {"directory": str(directory)})
        by_id = {layer["id"]: layer for layer in cp["info"]["layers"]}
        assets = copy.deepcopy(assets)
        for asset in assets.values():
            if asset.get("layer_id") in by_id:
                layer = by_id[asset["layer_id"]]
                if not asset.get("remote") and layer["provider"] != "wms":
                    asset["path"] = layer["source"].split("|")[0]
            elif asset.get("layer_id"):
                # Removing a project layer does not delete its durable dataset.
                # Stop resolving its logical asset to an obsolete runtime ID.
                asset.pop("layer_id")
        cp["assets"] = assets
        remote_layer_ids = {
            item.get("layer_id") for item in assets.values() if item.get("remote")
        }
        paths = {cp["project"], cp["supplemental"]}
        paths.update(
            layer["source"].split("|")[0]
            for layer in cp["info"]["layers"]
            if layer["provider"] != "wms" and layer["id"] not in remote_layer_ids
        )
        paths.update(
            item["path"]
            for item in assets.values()
            if item.get("path") and not item.get("input") and not item.get("remote")
        )
        cp["fingerprints"] = [await asyncio.to_thread(fingerprint, path) for path in sorted(paths)]
        await asyncio.to_thread(sync_tree, directory)
        return cp

    async def verify_checkpoint(self, cp):
        current_environment = await self.bridge.call("_environment", {})
        if cp["environment"] != current_environment:
            recorded = cp["environment"]
            changed = sorted(key for key in set(recorded) | set(current_environment)
                             if recorded.get(key) != current_environment.get(key))
            raise TaskError(
                "ENVIRONMENT_CHANGED", "Checkpoint runtime versions differ",
                evidence={"changed_components": changed,
                          "recorded": {key: recorded.get(key) for key in changed},
                          "current": {key: current_environment.get(key) for key in changed}},
                next_action=(
                    "Restore the recorded runtime and retry task_recover, or recompute in a new "
                    "task under the current runtime with a newly approved contract. Preserve this "
                    "task as recovery evidence; in-place environment migration is not supported."
                ),
            )
        for recorded in cp["fingerprints"]:
            if await asyncio.to_thread(fingerprint, recorded["path"]) != recorded:
                raise TaskError(
                    "ARTIFACT_CHANGED",
                    "Checkpoint artifact is missing or modified",
                    evidence={"path": recorded["path"]},
                )

    async def restore_committed(self):
        await self.fresh_worker()
        cp = self.store.task()["checkpoint"]
        if cp:
            await self.verify_checkpoint(cp)
            await self.bridge.call("_restore", cp)

    @contextmanager
    def trace_attempt(self, phase, step_id, attempt_id):
        started, success = time.monotonic(), False
        try:
            yield
            success = True
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            try:
                self.store.event("PHASE_TIMING", {
                    "phase": phase, "step_id": step_id, "attempt_id": attempt_id,
                    "success": success, "duration_ms": round(duration_ms, 3),
                })
            except (sqlite3.Error, OSError):
                # Measurements are not recovery authority. A full disk must not
                # turn a committed result into an apparent execution failure.
                pass
            self.traces.emit(
                "attempt_" + phase, task_id=self.store.task_id,
                step_id=step_id, attempt_id=attempt_id, success=success,
                duration_ms=duration_ms,
            )

    async def execute(self, operation, arguments, metadata):
        store = self.require_task(metadata["task_id"])
        step_row = store.db.execute(
            "SELECT body FROM steps WHERE id=?", (metadata["step_id"],)
        ).fetchone()
        if step_row is None:
            raise TaskError("STEP_CONTRACT_REQUIRED", "Submit a step contract before execution")
        step = StepContract.model_validate_json(step_row[0])
        if step.operation != operation or step.arguments != arguments:
            raise TaskError(
                "STEP_MISMATCH", "Tool arguments differ from the approved step contract"
            )
        await self.verify_inputs()
        await self.verify_checkpoint(store.task()["checkpoint"])
        attempt = store.begin_attempt(
            metadata["step_id"],
            metadata["idempotency_key"],
            {"operation": operation, "arguments": arguments},
            metadata["contract_version"],
            metadata["expected_state_version"],
        )
        if attempt["cached"]:
            return attempt["result"]
        directory = Path(attempt["directory"])
        phase = "preconditions"
        try:
            assets = self.assets()
            with self.trace_attempt(phase, metadata["step_id"], attempt["attempt_id"]):
                await self.require_checks(step.preconditions, assets)
            phase = "execution"
            with self.trace_attempt(phase, metadata["step_id"], attempt["attempt_id"]):
                result, outputs = await self.run_with_recovery(step, assets, directory)
                self.register_outputs(step, metadata["step_id"], result, outputs, assets)
                # Materialize memory sources before validating durable outputs.
            phase = "checkpoint"
            with self.trace_attempt(phase, metadata["step_id"], attempt["attempt_id"]):
                cp = await self.make_checkpoint(directory / "checkpoint", assets)
                assets = cp["assets"]
            phase = "validation"
            with self.trace_attempt(phase, metadata["step_id"], attempt["attempt_id"]):
                reports = await self.require_checks(step.postconditions, assets)
                reports.extend(await self.basic_outputs(step.outputs, assets))
            response = {
                "result": self.compact_worker_result(operation, result),
                "assets": {
                    out.id: {
                        key: assets[out.id][key]
                        for key in ("kind", "path", "layer_id", "layout")
                        if key in assets[out.id]
                    }
                    for out in step.outputs
                },
                "validation": reports,
                "task_id": store.task_id,
                "step_id": metadata["step_id"],
                "attempt_id": attempt["attempt_id"],
                "continuation_token": self.issue_continuation(
                    state_version=store.task()["state_version"] + 1
                ),
            }
            phase = "persistence"
            with self.trace_attempt(phase, metadata["step_id"], attempt["attempt_id"]):
                store.prepare_commit(attempt["attempt_id"], response, cp)
                await asyncio.to_thread(sync_tree, directory)
            phase = "commit"
            with self.trace_attempt(phase, metadata["step_id"], attempt["attempt_id"]):
                return store.commit(attempt["attempt_id"])
        except BaseException as exc:
            committed = store.db.execute(
                "SELECT result FROM attempts WHERE id=? AND status='COMMITTED'",
                (attempt["attempt_id"],),
            ).fetchone()
            if committed:
                # The transaction is authoritative even if response construction
                # or delivery fails after COMMIT. Never downgrade durable success.
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return json.loads(committed["result"])
            failure = (
                exc.payload
                if isinstance(exc, TaskError)
                else {
                    "code": "CANCELLED"
                    if isinstance(exc, asyncio.CancelledError)
                    else "FILESYSTEM_ERROR" if isinstance(exc, OSError)
                    else (exc.code or "WORKER_UNAVAILABLE") if isinstance(exc, WorkerError)
                    else "EXECUTION_FAILED",
                    "message": str(exc),
                }
            )
            failure["phase"] = failure.get("phase") or phase
            failure["retryable"] = failure.get("retryable", False)
            store.fail(
                attempt["attempt_id"], failure, cancelled=isinstance(exc, asyncio.CancelledError)
            )
            try:
                with self.trace_attempt("recovery", metadata["step_id"], attempt["attempt_id"]):
                    await asyncio.shield(self.restore_committed())
            except Exception as restore_error:
                store.event("RESTORE_FAILED", {"message": str(restore_error)})
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TaskError(
                failure["code"],
                failure.get("message", "Step failed"),
                evidence={
                    "attempt_id": attempt["attempt_id"],
                    "details": failure,
                },
                phase=failure["phase"],
                retryable=failure["retryable"],
                next_action=(
                    "Resolve filesystem availability, then task_recover before retrying"
                    if failure["code"] == "FILESYSTEM_ERROR"
                    else "Use this error evidence directly; submit a revised step contract"
                ),
            ) from exc

    @staticmethod
    def compact_worker_result(operation, result):
        """Keep only fields needed to plan the next step; checkpoints retain full evidence."""
        if not isinstance(result, dict):
            return result
        keep = {
            "algorithm", "path", "bytes", "id", "name", "kind", "crs", "extent",
            "feature_count", "geometry_type", "bands", "width", "height", "minimum",
            "maximum", "band",
            "ramp", "layout", "count", "outputs", "warnings",
        }
        compact = {key: value for key, value in result.items() if key in keep}
        if operation == "load_data" and result.get("fields") is not None:
            compact["field_count"] = len(result["fields"])
            compact["field_names"] = [field["name"] for field in result["fields"][:20]]
            compact["fields_truncated"] = len(result["fields"]) > 20
        if result.get("loaded_layers"):
            compact["loaded_layers"] = [
                {
                    key: layer[key]
                    for key in (
                        "id", "name", "kind", "crs", "extent", "source", "geometry_type",
                        "raster_summary",
                    )
                    if key in layer
                }
                for layer in result["loaded_layers"]
            ]
        return compact

    def register_outputs(self, step, step_id, result, outputs, assets):
        for output in step.outputs:
            asset = {"kind": output.kind, "step_id": step_id, "inputs": step.inputs}
            if output.binding == "layer":
                if "id" not in result:
                    raise TaskError("OUTPUT_BINDING", "Operation did not return a layer ID")
                asset.update(layer_id=result["id"], path=result.get("source"))
                if step.operation == "add_basemap":
                    asset.update(
                        remote=True,
                        service=step.arguments.get("service"),
                        role=step.arguments.get("role"),
                    )
            elif output.kind == "layout":
                asset["layout"] = result["name"]
            else:
                path = outputs[output.id]
                actual = (
                    result.get("outputs", {}).get(output.binding)
                    if step.operation == "run_processing"
                    else result.get("path")
                )
                if actual != path:
                    raise TaskError(
                        "OUTPUT_BINDING",
                        "Operation output does not match the declared managed path",
                    )
                asset["path"] = path
                for layer in result.get("loaded_layers", []):
                    if layer["source"].split("|")[0] == path:
                        asset["layer_id"] = layer["id"]
                        if layer.get("raster_summary") is not None:
                            asset["raster_summary"] = layer["raster_summary"]
            assets[output.id] = asset

    async def run_with_recovery(self, step, assets, directory):
        worker_replays, network_retries = 0, 0
        while True:
            execution_name = f"execution-{worker_replays}-{network_retries}"
            output_dir = temporary_output_root(self.store.task_id) / directory.name / execution_name
            output_dir.mkdir(parents=True, exist_ok=False)
            outputs = {
                out.id: self.output_path(out, output_dir)
                for out in step.outputs
                if out.kind != "layout" and out.binding != "layer"
            }
            resolved = self.resolve(step.arguments, assets, outputs)
            try:
                result = await self.bridge.call(step.operation, resolved)
                missing = {
                    output_id: path for output_id, path in outputs.items()
                    if not Path(path).is_file()
                }
                if missing:
                    raise TaskError(
                        "OUTPUT_MISSING",
                        "Processing did not create a declared output file",
                        phase="execution",
                        evidence={
                            "algorithm": step.arguments.get("algorithm"),
                            "outputs": missing,
                            "worker_log": str(result.get("log", ""))[-12000:],
                        },
                        next_action=(
                            "Inspect worker_log and correct the evidenced Processing parameter; "
                            "do not retry unchanged parameters."
                        ),
                    )
                return result, outputs
            except WorkerError as exc:
                if exc.code == "WORKER_TIMEOUT":
                    raise
                text = str(exc).lower()
                transient = any(
                    message in text
                    for message in (
                        "temporary failure in name resolution",
                        "connection reset",
                        "network timeout",
                        "temporarily unavailable",
                        "connection timed out",
                    )
                )
                if self.bridge.broken:
                    # A stopped worker cannot borrow the network retry budget.
                    if worker_replays >= 1:
                        raise
                    worker_replays += 1
                    self.store.event("WORKER_REPLAY", {"count": worker_replays})
                elif exc.code == "OPERATION_FAILED" and transient and network_retries < 2:
                    delay = (1, 4)[network_retries] + random.uniform(0, 0.25)
                    network_retries += 1
                    self.store.event("NETWORK_RETRY", {"count": network_retries, "delay": delay})
                    await asyncio.sleep(delay)
                else:
                    raise
                # Previous attempt paths are quarantined by never reusing them.
                await self.verify_inputs()
                await self.restore_committed()
                await self.require_checks(step.preconditions, assets)

    def output_path(self, output, temporary_directory):
        """Resolve final destinations only for declared deliverables; intermediates stay temporary."""
        declared = next(
            (item for item in self.store.task()["deliverables"] if item["id"] == output.id), None
        )
        if declared is None:
            return str((temporary_directory / (output.filename or output.id + EXTENSIONS[output.kind])).resolve())
        destination = declared.get("path")
        if destination is None and declared.get("directory"):
            destination = str(
                Path(declared["directory"]) / (output.filename or output.id + EXTENSIONS[output.kind])
            )
        if destination is None:
            return str((temporary_directory / (output.filename or output.id + EXTENSIONS[output.kind])).resolve())
        path = Path(destination).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self.output_conflict_resolution(path) == "overwrite":
                if not path.is_file() or path.is_symlink():
                    raise TaskError(
                        "UNSAFE_OUTPUT", "Only a regular final-output file may be overwritten",
                        evidence={"path": str(path)}, next_action="Choose a different output path",
                    )
                path.unlink()
                self.store.event("OUTPUT_OVERWRITTEN", {"path": str(path)})
                return str(path)
            raise TaskError(
                "OUTPUT_EXISTS", "Requested output path already exists",
                evidence={
                    "path": str(path),
                    "question": {
                        "kind": "output_conflict",
                        "path": str(path),
                        "choices": [
                            {"value": "retry", "description": "I removed the existing file; retry without overwriting."},
                            {"value": "overwrite", "description": "Replace this exact existing file."},
                        ],
                    },
                    "decision_calls": {
                        "retry": {
                            "tool": "task_update",
                            "arguments": {
                                "task_id": self.store.task_id,
                                "instruction": "The user confirmed that this exact file was removed",
                                "output_conflict": {"path": str(path), "action": "retry"},
                            },
                        },
                        "overwrite": {
                            "tool": "task_update",
                            "arguments": {
                                "task_id": self.store.task_id,
                                "instruction": "The user explicitly authorized replacement of this exact file",
                                "output_conflict": {"path": str(path), "action": "overwrite"},
                            },
                        },
                    },
                },
                next_action=(
                    "Ask the user whether this exact file may be replaced. Then call task_update "
                    "once using the matching evidence.decision_calls entry; it will resume only "
                    "the failed operation and return task_execute."
                ),
            )
        return str(path)

    def output_conflict_resolution(self, path):
        """Return the latest explicit user decision for this exact output path."""
        target = str(Path(path).expanduser().resolve())
        rows = self.store.db.execute(
            "SELECT kind,body FROM events WHERE kind IN "
            "('OUTPUT_CONFLICT_RESOLUTION','ATTEMPT_FAILED') ORDER BY sequence DESC"
        )
        for row in rows:
            body = json.loads(row["body"])
            if row["kind"] == "OUTPUT_CONFLICT_RESOLUTION":
                if body.get("path") == target:
                    return body.get("action")
                continue
            failure = body.get("failure") or {}
            if (
                failure.get("code") == "OUTPUT_EXISTS"
                and (failure.get("evidence") or {}).get("path") == target
            ):
                # An older decision cannot authorize a new conflict.  This is
                # especially important for retry after the file remained.
                return None
        return None

    async def check(self, check, assets):
        # Wall time includes Worker transport. Keep it with the local validation
        # evidence even when remote tracing is disabled; cached results retain
        # the original measurement rather than claiming another validation.
        started = time.monotonic()
        report = await self.evaluate_check(check, assets)
        report["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        return report

    async def evaluate_check(self, check, assets):
        body = check.model_dump() if hasattr(check, "model_dump") else check
        if body["target"] not in assets:
            return {
                "id": body["id"],
                "status": "failed",
                "evidence": {"reason": "Target asset missing"},
            }
        kind = body.get("data_kind")
        if body["kind"] == "readable" and kind not in {"vector", "raster"}:
            report = {"id": body["id"], "status": "failed", "scope": "full", "evidence": {}}
            try:
                path = Path(assets[body["target"]]["path"])
                if kind == "pdf":
                    from pypdf import PdfReader

                    if not len(PdfReader(path).pages):
                        raise ValueError("PDF has no pages")
                elif kind == "image":
                    from PIL import Image

                    with Image.open(path) as image:
                        image.verify()
                elif kind == "project":
                    if path.suffix == ".qgz":
                        with zipfile.ZipFile(path) as archive:
                            names = [name for name in archive.namelist() if name.endswith(".qgs")]
                            if len(names) != 1:
                                raise ValueError("Project archive must contain one QGS")
                            ElementTree.fromstring(archive.read(names[0]))
                    else:
                        ElementTree.parse(path)
                else:
                    ElementTree.parse(path)
                report["status"] = "passed"
            except Exception as exc:
                report["evidence"] = {"error": str(exc)}
            return report
        return await self.bridge.call("_validate", {"check": body, "assets": assets})

    async def require_checks(self, checks, assets):
        reports = []
        for check in checks:
            report = await self.check(check, assets)
            reports.append(report)
        failed = [
            report
            for check, report in zip(checks, reports, strict=True)
            if check.required and report["status"] != "passed"
        ]
        if failed:
            raise TaskError(
                "VALIDATION_FAILED", "Required checks did not pass", evidence={"checks": reports}
            )
        return reports

    async def basic_outputs(self, outputs, assets):
        reports = []
        for output in outputs:
            if output.kind == "layout":
                names = (await self.bridge.call("layout", {"action": "list"}))["layouts"]
                if assets[output.id]["layout"] not in names:
                    raise TaskError("INVALID_OUTPUT", "Declared layout was not created")
                continue
            report = await self.check(
                {
                    "id": "system_readable_" + output.id,
                    "kind": "readable",
                    "data_kind": output.kind,
                    "target": output.id,
                },
                assets,
            )
            reports.append(report)
            if report["status"] != "passed":
                raise TaskError(
                    "INVALID_OUTPUT", "System output validity check failed", evidence=report
                )
        return reports

    async def checkpoint(self, a):
        store = self.require_task(a["task_id"])
        store.require_version(a["expected_state_version"])
        if store.task()["status"] not in {"READY", "COMPLETED"}:
            raise TaskError("TASK_STATE", "Checkpoint requires a stable task")
        await self.verify_inputs()
        await self.verify_checkpoint(store.task()["checkpoint"])
        cp = await self.make_checkpoint(
            store.directory / "checkpoints" / uuid.uuid4().hex, self.assets()
        )
        with store.transaction():
            store.db.execute(
                "UPDATE task SET checkpoint=?,state_version=state_version+1", (canonical(cp),)
            )
            store.event("CHECKPOINT_SAVED", {"project": cp["project"]})
        return self.compact_status()

    async def resume(self, a):
        if self.store is None or self.store.task_id != a["task_id"]:
            candidate = TaskStore(self.root, a["task_id"])
            if self.store:
                if self.store.unresolved():
                    candidate.close()
                    raise TaskError("ATTEMPT_UNRESOLVED", "Reconcile the attached task first")
                self.store.close()
            self.store = candidate
        store = self.store
        retry_step = a.get("retry_step")
        if retry_step:
            store.require_version(a.get("expected_state_version"))
            if not a.get("reason", "").strip():
                raise TaskError("RETRY_REASON_REQUIRED", "Explain which infrastructure condition changed")
            row = store.db.execute("SELECT status,contract_version FROM steps WHERE id=?", (retry_step,)).fetchone()
            attempt = store.db.execute(
                "SELECT status,failure FROM attempts WHERE step_id=? ORDER BY created DESC LIMIT 1", (retry_step,)
            ).fetchone()
            allowed = {"FILESYSTEM_ERROR", "WORKER_UNAVAILABLE", "INTERRUPTED", "CANCELLED",
                       "INPUT_REVISION_INTERRUPTED"}
            failure = json.loads(attempt["failure"] or "{}") if attempt else {}
            output_resolution = None
            if failure.get("code") == "OUTPUT_EXISTS":
                output_resolution = self.output_conflict_resolution(
                    (failure.get("evidence") or {}).get("path", "")
                )
            if (not row or row["status"] != "FAILED" or not attempt or attempt["status"] != "FAILED"
                    or (failure.get("code") not in allowed and output_resolution not in {"retry", "overwrite"})):
                raise TaskError("SEMANTIC_REPAIR_REQUIRED", "Only infrastructure failures can retry an unchanged contract",
                                next_action="Submit a revised step with repairs_step for parameter or validation failures; OUTPUT_EXISTS requires recorded user output-conflict guidance")
            if row["contract_version"] != store.task()["contract_version"]:
                raise TaskError("CONTRACT_CONFLICT", "The failed step contract is outdated")
            retries = sum(json.loads(event[0]).get("step_id") == retry_step for event in
                          store.db.execute("SELECT body FROM events WHERE kind='INFRASTRUCTURE_RETRY'"))
            if retries >= 2:
                raise TaskError("RETRY_BUDGET_EXHAUSTED", "Two explicit infrastructure retries were already authorized")
        if store.task()["status"] == "CANCELLED" and not a.get("resume_cancelled"):
            raise TaskError("TASK_CANCELLED", "Explicit resume_cancelled is required")
        await self.verify_inputs()
        await self.restore_committed()
        for pending in store.unresolved():
            if pending["status"] == "VALIDATING":
                try:
                    cp = json.loads(pending["checkpoint"])
                    await self.verify_checkpoint(cp)
                    await self.bridge.call("_restore", cp)
                    step = StepContract.model_validate_json(
                        store.db.execute(
                            "SELECT body FROM steps WHERE id=?", (pending["step_id"],)
                        ).fetchone()[0]
                    )
                    await self.require_checks(step.postconditions, cp["assets"])
                    await self.basic_outputs(step.outputs, cp["assets"])
                    await asyncio.to_thread(sync_tree, store.directory / "attempts" / pending["id"])
                    store.commit(pending["id"])
                    continue
                except Exception as exc:
                    committed = store.db.execute(
                        "SELECT status FROM attempts WHERE id=?", (pending["id"],)
                    ).fetchone()
                    if committed and committed["status"] == "COMMITTED":
                        continue
                    store.fail(
                        pending["id"], {"code": "RECOVERY_VALIDATION_FAILED", "message": str(exc)}
                    )
            else:
                store.fail(
                    pending["id"],
                    {
                        "code": "INTERRUPTED",
                        "message": "Operation did not reach a validated commit",
                    },
                )
            await self.restore_committed()
        with store.transaction():
            status = store.task()["status"]
            target = (
                "COMPLETED"
                if status == "COMPLETED"
                else ("READY" if store.task()["contract_version"] else "PREPARING")
            )
            store.db.execute("UPDATE task SET status=?,state_version=state_version+1", (target,))
            if retry_step:
                store.db.execute("UPDATE steps SET status='PLANNED' WHERE id=?", (retry_step,))
                store.event("INFRASTRUCTURE_RETRY", {"step_id": retry_step, "reason": a["reason"]})
            store.event("TASK_RESUMED", {})
        result = self.compact_status()
        result["next_action"] = "Use contract_get as needed, then continue with the returned continuation_token"
        if result["user_intervention_required"]:
            if result["intervention_reason"] == "tool_timeout":
                result["next_action"] = (
                    "A tool call timed out. Inspect the task and ask the user whether to retry, "
                    "repair, or stop; record the actual response with task_update or task_answer."
                )
            else:
                result["next_action"] = (
                    "The correction limit was reached. Explain the latest failure and ask the user "
                    "how to proceed; record the actual response with task_update or task_answer."
                )
            return result
        if not retry_step and self.presentation_pending():
            result["next_call"] = self.compact_next_call({
                "tool": "presentation_continue",
                "arguments": {"task_id": store.task_id,
                              "continuation_token": result["continuation_token"]},
            })
            result["next_action"] = "Call task_execute with next_call.arguments to resume the pending map"
        if retry_step:
            result["continuation_token"] = self.issue_continuation("execute", retry_step)
            result["next_call"] = {
                "tool": "step_execute",
                "arguments": {
                    "task_id": store.task_id,
                    "step_id": retry_step,
                    "continuation_token": result["continuation_token"],
                },
            }
            result["next_action"] = "Execute the unchanged step using next_call"
        return result

    async def revise_inputs(self, a):
        store = self.require_task(a["task_id"])
        store.require_version(a["expected_state_version"])
        revised = copy.deepcopy(store.task()["inputs"])
        if not a.get("reason", "").strip():
            raise TaskError("REVISION_REASON_REQUIRED", "Explain why new input versions are accepted")
        requested = a["expected_digests"]
        if not requested or not set(requested) <= revised.keys():
            raise TaskError("UNKNOWN_INPUT", "Revision must identify existing declared inputs")
        changed = set()
        for key, item in revised.items():
            current = await asyncio.to_thread(fingerprint, item["path"])
            if current != item["fingerprint"]:
                changed.add(key)
            if key in requested and current["digest"] != requested[key]:
                raise TaskError("INPUT_VERSION_CONFLICT", "Input differs from the inspected version")
            item["fingerprint"] = current
        if changed != set(requested):
            raise TaskError("INPUT_REVISION_MISMATCH", "Explicitly acknowledge exactly the changed inputs",
                            evidence={"changed_inputs": sorted(changed)})
        for key in changed:
            item = revised[key]
            if item["kind"] == "style":
                ElementTree.parse(item["path"])
            else:
                await self.bridge.call("_inspect", {"source": item["path"], "kind": item["kind"]})
        roots = [row["id"] for row in store.db.execute("SELECT id,body FROM steps")
                 if changed.intersection(json.loads(row["body"])["inputs"])]
        return await self.repair({**a, "steps": roots}, revised_inputs=revised)

    async def repair(self, a, *, revised_inputs=None):
        store = self.require_task(a["task_id"])
        store.require_version(a["expected_state_version"])
        allowed_states = {"READY", "BLOCKED", "COMPLETED"}
        if revised_inputs is not None:
            allowed_states.update({"PREPARING", "COMPLETED"})
        discard_pending = revised_inputs is not None and a.get("discard_uncommitted", False)
        if discard_pending:
            allowed_states.add("RUNNING")
        if store.unresolved() and not discard_pending:
            raise TaskError(
                "ATTEMPT_UNRESOLVED", "Uncommitted attempts require reconciliation",
                next_action=(
                    "If inputs changed, inspect their new digests and call task_revise_inputs "
                    "with discard_uncommitted=true to quarantine unresolved results; otherwise task_recover"
                ),
            )
        if store.task()["status"] not in allowed_states:
            raise TaskError(
                "TASK_STATE", "Task state does not permit semantic repair",
                evidence={"status": store.task()["status"], "allowed_states": sorted(allowed_states)},
                next_action="Use task_recover to reconcile or explicitly resume the task before requesting repair",
            )
        if revised_inputs is None:
            await self.verify_inputs()
        affected = set(store.dependency_closure(a["steps"]))
        attempts = list(
            store.db.execute(
                "SELECT attempts.*,steps.status AS step_status,steps.body AS step_body FROM attempts JOIN steps ON steps.id=attempts.step_id "
                "WHERE attempts.status='COMMITTED' ORDER BY attempts.created"
            )
        )
        roots = [
            row
            for row in attempts
            if row["step_id"] in a["steps"] and row["step_status"] == "COMMITTED"
        ]
        if revised_inputs is None and {row["step_id"] for row in roots} != set(a["steps"]):
            committed_roots = {row["step_id"] for row in roots}
            rejected = []
            for step_id in sorted(set(a["steps"]) - committed_roots):
                status = store.db.execute(
                    "SELECT status FROM steps WHERE id=?", (step_id,)
                ).fetchone()[0]
                rejected.append({
                    "step_id": step_id, "status": status,
                    "next_action": (
                        "step_contract_submit: submit a corrected step with repairs_step set to this step_id"
                        if status in {"FAILED", "INVALIDATED"}
                        else "Inspect contract_get and task_diagnose before executing or resuming this step"
                    ),
                })
            raise TaskError(
                "INVALID_REPAIR", "Each root must have a currently committed result",
                evidence={"steps": rejected},
                next_action=(
                    "Follow each step's next_action. task_invalidate rolls back committed results; "
                    "FAILED or INVALIDATED steps do not need another rollback. Read contract_get, "
                    "then submit the corrected step contract without weakening required checks."
                ),
            )
        project_changes = [
            row
            for row in attempts
            if (row["step_status"] == "COMMITTED" or (revised_inputs is not None and row["step_id"] in affected))
            and self.changes_project(StepContract.model_validate_json(row["step_body"]))
        ]
        affected_changes = [row for row in project_changes if row["step_id"] in affected]
        first = min((row["created"] for row in affected_changes), default=float("inf"))
        untracked = [
            row["step_id"]
            for row in project_changes
            if row["created"] >= first and row["step_id"] not in affected
        ]
        if untracked:
            raise TaskError(
                "REPAIR_SCOPE_INCOMPLETE",
                "Historical project mutations are missing dependency links",
                evidence={"steps": untracked},
                next_action="Include these project-mutating steps in the explicit repair scope",
            )
        candidates = [
            (row["created"], json.loads(row["checkpoint"]))
            for row in attempts
            if row["created"] < first and row["step_status"] == "COMMITTED"
        ]
        candidates.extend(
            (row["created"], json.loads(row["body"])["checkpoint"])
            for row in store.db.execute(
                "SELECT created,body FROM events WHERE kind='REPAIR_STARTED'"
            )
            if row["created"] < first
        )
        base = max(candidates, key=lambda item: item[0])[1] if candidates else None
        current_cp = store.task()["checkpoint"]
        retained = {
            key: value
            for key, value in self.assets().items()
            if value.get("step_id") not in affected
        }
        if not affected_changes:
            # A rejected file-only branch never changed the project. Preserve
            # later independent layers/layouts instead of rolling them back.
            base = copy.deepcopy(current_cp)
            required_paths = {base["project"], base["supplemental"]}
            required_paths.update(
                asset["path"]
                for asset in retained.values()
                if asset.get("path") and not asset.get("remote")
            )
            required_paths.update(
                layer["source"].split("|")[0]
                for layer in base["info"]["layers"]
                if layer["provider"] != "wms"
            )
            base["fingerprints"] = [
                item for item in base["fingerprints"] if item["path"] in required_paths
            ]
        recorded = {item["path"]: item for item in current_cp["fingerprints"]}
        # Check independent retained results even if they were produced after the
        # rollback point. Their files can be reused without recreating GIS work.
        for asset in retained.values():
            if asset.get("path") and not asset.get("input") and not asset.get("remote"):
                found = await asyncio.to_thread(fingerprint, asset["path"])
                if recorded.get(found["path"]) != found:
                    raise TaskError(
                        "ARTIFACT_CHANGED", "A retained result is unavailable or changed"
                    )
        try:
            await self.fresh_worker()
            if base:
                await self.verify_checkpoint(base)
                await self.bridge.call("_restore", base)
            elif await self.bridge.call("_environment", {}) != store.task()["environment"]:
                raise TaskError("ENVIRONMENT_CHANGED", "Task runtime versions differ")
            cp = await self.make_checkpoint(
                store.directory / "repairs" / uuid.uuid4().hex, retained
            )
            if revised_inputs is not None:
                for item in revised_inputs.values():
                    if await asyncio.to_thread(fingerprint, item["path"]) != item["fingerprint"]:
                        raise TaskError("INPUT_CHANGED", "Input changed while preparing its revision")
            store.invalidate(a["steps"], a["reason"], checkpoint=cp, revised_inputs=revised_inputs,
                             discard_uncommitted=discard_pending)
        except BaseException:
            if revised_inputs is None:
                await asyncio.shield(self.restore_committed())
            else:
                # Old checkpoint can refer to the changed input; never restore it
                # without verification or replace the original revision error.
                await self.fresh_worker()
            raise
        return {
            **self.compact_status(),
            "invalidated_steps": sorted(affected),
            "next_action": "For Processing, call prepare_algorithm with repairs_step and the original output IDs; then rebuild invalidated downstream steps. Other operations use step_contract_submit with repairs_step.",
        }

    async def validate_task(self, a):
        self.require_task(a["task_id"])
        with self.trace_attempt("final_validation", None, None):
            result = await self.validate_task_contents(a)
        if a.get("include_details"):
            return result
        return {
            "task_id": result["task_id"],
            "passed": result["passed"],
            "checks": [
                {"id": report["id"], "status": report["status"]}
                for report in result["checks"]
            ],
            "continuation_token": result["continuation_token"],
        }

    async def validate_task_contents(self, a):
        store = self.require_task(a["task_id"])
        if store.task()["status"] not in {"READY", "COMPLETED"}:
            raise TaskError("TASK_STATE", "Recover task before final validation")
        await self.verify_inputs()
        await self.verify_checkpoint(store.task()["checkpoint"])
        contract = self.current_contract()
        if a.get("revalidate_steps"):
            store.require_version(a.get("expected_state_version"))
            await self.revalidate_results(a["revalidate_steps"], contract)
        assets = self.assets()
        reports = [await self.check(check, assets) for check in contract.checks]
        passed = all(
            report["status"] == "passed" or not check.required
            for check, report in zip(contract.checks, reports, strict=True)
        )
        for item in store.task()["deliverables"]:
            if item["id"] not in assets:
                passed = False
                reports.append(
                    {
                        "id": "system_deliverable_" + item["id"],
                        "status": "failed",
                        "evidence": {"reason": "Declared deliverable is missing"},
                    }
                )
            else:
                try:
                    reports.extend(
                        await self.basic_outputs(
                            [Output(id=item["id"], kind=item["kind"], binding="path")], assets
                        )
                    )
                except TaskError as exc:
                    passed = False
                    reports.append(
                        {
                            "id": "system_deliverable_" + item["id"],
                            "status": "failed",
                            "evidence": exc.payload,
                        }
                    )
        store.event("TASK_VALIDATED", {"passed": passed, "checks": reports})
        return {
            "task_id": store.task_id,
            "passed": passed,
            "checks": reports,
            "continuation_token": self.issue_continuation(),
        }

    async def revalidate_results(self, step_ids, contract):
        """Revalidate durable outputs or the exact retained state checkpoint."""
        store = self.require_task()
        for step_id in step_ids:
            row = store.db.execute("SELECT * FROM steps WHERE id=?", (step_id,)).fetchone()
            committed = store.db.execute(
                "SELECT id,checkpoint FROM attempts WHERE step_id=? AND status='COMMITTED'", (step_id,)
            ).fetchone()
            if row is None or row["status"] != "INVALIDATED" or committed is None:
                raise TaskError(
                    "RESULT_NOT_REVALIDATABLE",
                    "Select an invalidated step with a committed attempt",
                )
            for dependency in json.loads(row["dependencies"]):
                if (
                    store.db.execute(
                        "SELECT status FROM steps WHERE id=?", (dependency,)
                    ).fetchone()[0]
                    != "COMMITTED"
                ):
                    raise TaskError(
                        "INVALID_DEPENDENCY",
                        "Revalidate upstream results first",
                        evidence={"step_id": step_id, "dependency": dependency},
                    )
            step = StepContract.model_validate_json(row["body"])
            if not step.outputs:
                original_checkpoint = json.loads(committed["checkpoint"])
                current_checkpoint = store.task()["checkpoint"]
                # State-only results have no asset to identify their effect.
                # Reuse only when the exact committed project and supplemental
                # state remain authoritative and have passed fingerprint checks.
                if any(original_checkpoint[key] != current_checkpoint[key]
                       for key in ("project", "supplemental", "fingerprints")):
                    raise TaskError(
                        "RESULT_NOT_REVALIDATABLE",
                        "The state-only step's exact checkpoint is no longer retained",
                        next_action="Submit a new step to establish and validate the intended project state",
                    )
            assets = self.assets()
            saved_assets = store.task()["checkpoint"]["assets"]
            for output in step.outputs:
                saved = saved_assets.get(output.id)
                if saved is None or saved.get("step_id") != step_id:
                    raise TaskError(
                        "RESULT_NOT_REVALIDATABLE", "Output is absent from the retained checkpoint"
                    )
                assets[output.id] = saved
            reports = await self.require_checks(step.postconditions, assets)
            reports.extend(await self.basic_outputs(step.outputs, assets))
            targets = {output.id for output in step.outputs}
            if not step.outputs:
                targets.update(step.inputs)
            reports.extend(
                await self.require_checks(
                    [check for check in contract.checks if check.target in targets], assets
                )
            )
            with store.transaction():
                store.db.execute("UPDATE steps SET status='COMMITTED' WHERE id=?", (step_id,))
                store.db.execute("UPDATE task SET state_version=state_version+1")
                store.event(
                    "RESULT_REVALIDATED",
                    {
                        "step_id": step_id,
                        "attempt_id": committed["id"],
                        "contract_version": store.task()["contract_version"],
                        "checks": reports,
                    },
                )

    async def finish(self, a):
        store = self.require_task(a["task_id"])
        store.require_version(a["expected_state_version"])
        validation = await self.validate_task(a)
        if not validation["passed"]:
            raise TaskError(
                "TASK_NOT_COMPLETE", "Required task checks have not passed", evidence=validation
            )
        with store.transaction():
            store.db.execute("UPDATE task SET status='COMPLETED',state_version=state_version+1")
            store.event("TASK_COMPLETED", validation)
        result = self.task_delta()
        result["assets"] = self.deliverable_assets()
        result["correction_budget"] = {
            "rejected_submissions": self.correction_failures(),
            "limit": self.correction_limit,
            "requires_user_input": self.correction_failures() >= self.correction_limit,
        }
        return result
