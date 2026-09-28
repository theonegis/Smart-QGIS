"""Durable task journal. QGIS execution and semantic validation live above this layer.

The task lock is held for the lifetime of the store. A committed attempt is the
only authoritative success record; files alone never imply success.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree


class TaskError(RuntimeError):
    def __init__(self, code, message, *, evidence=None, next_action=None, retryable=False, phase=None):
        super().__init__(message)
        self.payload = {
            "code": code,
            "phase": phase,
            "message": message,
            "evidence": evidence or {},
            "next_action": next_action,
            "retryable": retryable,
        }


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def state_root():
    override = os.getenv("SMART_QGIS_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "smart-qgis"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "smart-qgis"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise TaskError("INVALID_ID", "Identifiers must be 1–128 safe ASCII characters")
    return value


def fingerprint(path, *, _ancestors=()):
    """Hash complete local sources, including shapefile sidecars (never just mtime)."""
    source = Path(path).expanduser()
    if not source.is_absolute() or not source.is_file():
        raise TaskError("INPUT_UNAVAILABLE", "Input must be an existing absolute file")
    source = source.resolve()
    if source in _ancestors or len(_ancestors) >= 32:
        raise TaskError("INPUT_DEPENDENCY_CYCLE", "Source dependency cycle or excessive nesting")
    dependencies = []
    if source.suffix.lower() == ".vrt":
        try:
            xml_bytes = source.read_bytes()
            xml = ElementTree.fromstring(xml_bytes)
        except ElementTree.ParseError as exc:
            raise TaskError("INVALID_INPUT", "VRT is not valid XML") from exc
        if xml.tag not in {"VRTDataset", "OGRVRTDataSource"}:
            raise TaskError("UNSUPPORTED_SOURCE", "Unknown VRT document type")
        for item in xml.iter():
            if item.tag == "PixelFunctionCode":
                raise TaskError("UNSUPPORTED_SOURCE", "Executable VRT pixel functions are not reliable local inputs")
            if item.tag not in {"SourceFilename", "SourceDataset", "SrcDataSource"}:
                continue
            value = (item.text or "").strip()
            child = Path(value)
            if not value or "://" in value or value.startswith("/vsi"):
                raise TaskError("UNSUPPORTED_SOURCE", "VRT dependencies must be local files")
            if item.get("relativeToVRT") == "1":
                child = source.parent / child
            elif not child.is_absolute():
                raise TaskError("UNSUPPORTED_SOURCE", "Relative VRT sources must set relativeToVRT=1")
            dependencies.append(fingerprint(child, _ancestors=(*_ancestors, source)))
    members = [source]
    if source.suffix.lower() == ".shp":
        extensions = {".shp", ".shx", ".dbf", ".prj", ".qpj", ".cpg", ".qix", ".sbn", ".sbx", ".idm", ".ind"}
        members = sorted(
            p
            for p in source.parent.iterdir()
            if p.stem == source.stem and p.suffix.lower() in extensions and p.is_file()
        )
    if source.suffix.lower() in {".tif", ".tiff", ".img", ".png", ".jpg", ".jpeg", ".vrt"}:
        # GDAL can obtain masks, CRS/statistics and overviews from external
        # companions. Hashing only the primary image misses these state changes.
        candidates = [Path(str(source) + suffix) for suffix in (".aux.xml", ".ovr", ".msk")]
        world_extensions = {
            ".tif": (".tfw", ".tifw"),
            ".tiff": (".tfw", ".tiffw"),
            ".jpg": (".jgw", ".jpgw"),
            ".jpeg": (".jgw", ".jpegw"),
            ".png": (".pgw", ".pngw"),
        }
        candidates.extend(
            source.with_suffix(ext)
            for ext in (*world_extensions.get(source.suffix.lower(), ()), ".wld", ".prj")
        )
        members = sorted({source, *(path for path in candidates if path.is_file())})
    # SQLite journals/WAL cannot be treated as an immutable standalone input.
    if source.suffix.lower() in {".gpkg", ".sqlite", ".db"}:
        if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise TaskError(
                "INPUT_BUSY", "Close the input database writer before using this source"
            )
    files = []
    for member in members:
        before = member.stat()
        sha = hashlib.sha256()
        with member.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(chunk)
        after = member.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise TaskError("INPUT_CHANGED", "Input changed while its fingerprint was calculated")
        if member == source and source.suffix.lower() == ".vrt":
            if sha.hexdigest() != hashlib.sha256(xml_bytes).hexdigest():
                raise TaskError("INPUT_CHANGED", "VRT changed while its dependencies were inspected")
        files.append({"path": str(member), "size": after.st_size, "sha256": sha.hexdigest()})
    if dependencies:
        by_path = {item["path"]: item for item in files}
        for dependency in dependencies:
            for item in dependency["files"]:
                previous = by_path.get(item["path"])
                if previous is not None and previous != item:
                    raise TaskError("INPUT_CHANGED", "A shared VRT dependency changed during inspection")
                by_path[item["path"]] = item
        files = [by_path[key] for key in sorted(by_path)]
    return {"path": str(source), "files": files, "digest": digest(files)}


def sync_tree(directory):
    """Flush closed attempt outputs before committing their SQLite references."""
    root = Path(directory)
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            raise TaskError("UNSAFE_ARTIFACT", "Task artifacts must not be symbolic links")
        if item.is_file():
            with item.open("rb") as stream:
                os.fsync(stream.fileno())
    directories = sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True)
    for item in [*directories, root, root.parent]:
        fd = os.open(item, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


SCHEMA = """
CREATE TABLE task (
 id TEXT PRIMARY KEY, goal TEXT NOT NULL, inputs TEXT NOT NULL, deliverables TEXT NOT NULL,
 status TEXT NOT NULL, state_version INTEGER NOT NULL DEFAULT 0,
 contract_version INTEGER NOT NULL DEFAULT 0, checkpoint TEXT, environment TEXT NOT NULL
);
CREATE TABLE contracts (
 version INTEGER PRIMARY KEY, body TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE steps (
 id TEXT PRIMARY KEY, contract_version INTEGER NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL, dependencies TEXT NOT NULL
);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, step_id TEXT NOT NULL REFERENCES steps(id), idempotency_key TEXT UNIQUE NOT NULL,
 request_hash TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
 result TEXT, checkpoint TEXT, failure TEXT, created REAL NOT NULL
);
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE algorithm_plans (
 id TEXT PRIMARY KEY, step_id TEXT NOT NULL, algorithm TEXT NOT NULL, status TEXT NOT NULL,
 body TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE questions (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES algorithm_plans(id),
 parameter TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, answer TEXT,
 created REAL NOT NULL, answered REAL
);
PRAGMA user_version=2;
"""


class TaskStore:
    @classmethod
    def cleanup_expired(cls, root, *, terminal_retention_days=10,
                        failed_retention_days=60, now=None):
        """Delete whole expired finished or blocked tasks without touching active work."""
        if terminal_retention_days < 0 or failed_retention_days < 0:
            raise TaskError("INVALID_CONFIG", "Retention days must be nonnegative integers")
        report = {"removed": [], "skipped_busy": [], "skipped_recoverable": []}
        if terminal_retention_days == 0 and failed_retention_days == 0:
            return report
        root = Path(root).resolve()
        if not root.is_dir() or root.is_symlink():
            return report
        current_time = time.time() if now is None else now
        for directory in sorted(root.iterdir()):
            task_id = directory.name
            if (
                not re.fullmatch(r"[0-9a-f]{32}", task_id)
                or not directory.is_dir()
                or directory.is_symlink()
            ):
                continue
            database = directory / "task.sqlite3"
            lock_path = directory / "owner.lock"
            if (
                not database.is_file() or database.is_symlink()
                or not lock_path.is_file() or lock_path.is_symlink()
            ):
                continue
            try:
                lock = lock_path.open("a+b")
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    report["skipped_busy"].append(task_id)
                    continue
                connection = None
                try:
                    connection = sqlite3.connect(
                        f"file:{database}?mode=ro", uri=True, timeout=1
                    )
                    if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                        continue
                    task = connection.execute("SELECT status FROM task").fetchone()
                    last_event = connection.execute(
                        "SELECT max(created) FROM events"
                    ).fetchone()[0]
                    unresolved = connection.execute(
                        "SELECT count(*) FROM attempts WHERE status IN ('RUNNING','VALIDATING')"
                    ).fetchone()[0]
                    questions = connection.execute(
                        "SELECT count(*) FROM questions WHERE status='PENDING'"
                    ).fetchone()[0]
                    status = task[0] if task else None
                    retention_days = (
                        terminal_retention_days if status in {"COMPLETED", "CANCELLED"}
                        else failed_retention_days if status == "BLOCKED" else 0
                    )
                    if (
                        retention_days == 0
                        or unresolved
                        or questions
                        or last_event is None
                        or last_event >= current_time - retention_days * 86400
                    ):
                        report["skipped_recoverable"].append(task_id)
                        continue
                except sqlite3.Error:
                    continue
                finally:
                    if connection is not None:
                        connection.close()
                quarantine = root / f".cleanup-{task_id}-{uuid.uuid4().hex}"
                try:
                    os.replace(directory, quarantine)
                except OSError:
                    continue
            finally:
                lock.close()
            try:
                shutil.rmtree(quarantine)
            except OSError:
                continue
            report["removed"].append(task_id)
        return report

    def __init__(self, root, task_id, *, create=False):
        self.task_id = identifier(task_id)
        self.directory = Path(root).resolve() / self.task_id
        self.db = None
        self.lock = None
        if create:
            self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise TaskError("TASK_NOT_FOUND", "Task directory does not exist or is unsafe")
        try:
            self.lock = (self.directory / "owner.lock").open("a+b")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TaskError(
                    "TASK_BUSY", "Another process owns this task", retryable=True
                ) from exc
            database = self.directory / "task.sqlite3"
            if not create and not database.is_file():
                raise TaskError("TASK_NOT_FOUND", "Task database does not exist")
            if database.is_symlink():
                raise TaskError("UNSAFE_ARTIFACT", "Task database must not be a symbolic link")
            self.db = sqlite3.connect(database, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if create:
                self.db.executescript(SCHEMA)
            elif self.db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise TaskError("SCHEMA_MISMATCH", "Task database schema is not supported")
            elif self.db.execute("SELECT count(*) FROM task").fetchone()[0] != 1:
                raise TaskError("TASK_INCOMPLETE", "Task creation did not finish")
        except BaseException:
            self.close()
            raise

    @classmethod
    def create(cls, root, goal, inputs, deliverables, environment):
        if not goal.strip() or not deliverables:
            raise TaskError("INVALID_TASK", "A nonempty goal and deliverables are required")
        store = cls(root, uuid.uuid4().hex, create=True)
        try:
            with store.transaction():
                store.db.execute(
                    "INSERT INTO task(id,goal,inputs,deliverables,status,environment) VALUES(?,?,?,?,?,?)",
                    (
                        store.task_id,
                        goal,
                        canonical(inputs),
                        canonical(deliverables),
                        "PREPARING",
                        canonical(environment),
                    ),
                )
                store.event("TASK_CREATED", {"task_id": store.task_id})
            return store
        except BaseException:
            store.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, kind, body):
        self.db.execute(
            "INSERT INTO events(kind,body,created) VALUES(?,?,?)",
            (kind, canonical(body), time.time()),
        )

    def task(self):
        row = dict(self.db.execute("SELECT * FROM task").fetchone())
        for key in ("inputs", "deliverables", "environment", "checkpoint"):
            row[key] = json.loads(row[key]) if row[key] is not None else None
        return row

    def require_version(self, expected):
        current = self.task()["state_version"]
        if expected != current:
            raise TaskError(
                "STATE_CONFLICT", "Task state has changed", evidence={"current": current}
            )

    def save_contract(self, body, reason, expected):
        """Persist an already-validated contract; semantic checks belong to the coordinator."""
        with self.transaction():
            self.require_version(expected)
            task = self.task()
            if task["status"] in {"RUNNING", "RECOVERING", "COMPLETED", "CANCELLED"}:
                raise TaskError("TASK_STATE", "Cannot revise a contract in the current task state")
            version = task["contract_version"] + 1
            if version > 1 and not reason.strip():
                raise TaskError("REVISION_REASON_REQUIRED", "Explain why the contract changed")
            self.db.execute(
                "INSERT INTO contracts VALUES(?,?,?,?)",
                (version, canonical(body), reason, time.time()),
            )
            self.db.execute(
                "UPDATE task SET contract_version=?, state_version=state_version+1, status='READY'",
                (version,),
            )
            # Earlier results remain evidence, but must be revalidated under the new contract.
            self.db.execute("UPDATE steps SET status='INVALIDATED'")
            self.event("CONTRACT_SAVED", {"version": version, "reason": reason})
        return version

    def save_step(self, step_id, body, dependencies, expected):
        identifier(step_id)
        if len(set(dependencies)) != len(dependencies) or step_id in dependencies:
            raise TaskError(
                "INVALID_DEPENDENCY", "Dependencies must be unique and exclude this step"
            )
        with self.transaction():
            self.require_version(expected)
            task = self.task()
            if task["status"] not in {"READY", "BLOCKED"} or task["contract_version"] == 0:
                raise TaskError("CONTRACT_REQUIRED", "Submit a task contract before step contracts")
            if self.db.execute("SELECT 1 FROM steps WHERE id=?", (step_id,)).fetchone():
                raise TaskError("STEP_EXISTS", "Use a new step ID for a revised execution plan")
            for dependency in dependencies:
                row = self.db.execute(
                    "SELECT status FROM steps WHERE id=?", (dependency,)
                ).fetchone()
                if row is None or row[0] != "COMMITTED":
                    raise TaskError(
                        "INVALID_DEPENDENCY", "Each dependency must be a committed step"
                    )
            self.db.execute(
                "INSERT INTO steps VALUES(?,?,?,?,?)",
                (
                    step_id,
                    task["contract_version"],
                    canonical(body),
                    "PLANNED",
                    canonical(dependencies),
                ),
            )
            self.db.execute("UPDATE task SET state_version=state_version+1, status='READY'")
            self.event("STEP_PLANNED", {"step_id": step_id})

    def save_algorithm_plan(self, plan):
        """Persist a normalized plan and its structured unresolved questions."""
        now = time.time()
        with self.transaction():
            if self.db.execute(
                "SELECT 1 FROM algorithm_plans WHERE id=?", (plan["plan_id"],)
            ).fetchone():
                raise TaskError("PLAN_EXISTS", "Algorithm preparation plan already exists")
            self.db.execute(
                "INSERT INTO algorithm_plans VALUES(?,?,?,?,?,?,?)",
                (
                    plan["plan_id"], plan["step_id"], plan["algorithm"], plan["status"],
                    canonical(plan), now, now,
                ),
            )
            for question in plan.get("questions", []):
                self.db.execute(
                    "INSERT INTO questions(id,plan_id,parameter,status,body,created) "
                    "VALUES(?,?,?,'PENDING',?,?)",
                    (
                        question["id"], plan["plan_id"], question["parameter"],
                        canonical(question), now,
                    ),
                )
            self.event("ALGORITHM_PLAN_SAVED", {
                "plan_id": plan["plan_id"], "algorithm": plan["algorithm"],
                "questions": [item["id"] for item in plan.get("questions", [])],
            })

    def algorithm_plan(self, plan_id):
        row = self.db.execute(
            "SELECT body,status FROM algorithm_plans WHERE id=?", (identifier(plan_id),)
        ).fetchone()
        if row is None:
            raise TaskError("UNKNOWN_PLAN", "Algorithm preparation plan does not exist")
        result = json.loads(row["body"])
        result["status"] = row["status"]
        result["answers"] = {
            item["id"]: json.loads(item["answer"])
            for item in self.db.execute(
                "SELECT id,answer FROM questions WHERE plan_id=? AND status='ANSWERED'",
                (plan_id,),
            )
        }
        return result

    def pending_questions(self):
        return [
            {**json.loads(row["body"]), "plan_id": row["plan_id"]}
            for row in self.db.execute(
                "SELECT plan_id,body FROM questions WHERE status='PENDING' ORDER BY created,id"
            )
        ]

    def answer_question(self, question_id, answer, expected):
        """Atomically record one user answer and advance task authorization state."""
        identifier(question_id)
        with self.transaction():
            self.require_version(expected)
            row = self.db.execute(
                "SELECT plan_id,status FROM questions WHERE id=?", (question_id,)
            ).fetchone()
            if row is None:
                raise TaskError("UNKNOWN_QUESTION", "Structured question does not exist")
            if row["status"] != "PENDING":
                raise TaskError("QUESTION_ANSWERED", "Structured question was already answered")
            now = time.time()
            self.db.execute(
                "UPDATE questions SET status='ANSWERED',answer=?,answered=? WHERE id=?",
                (canonical(answer), now, question_id),
            )
            self.db.execute(
                "UPDATE algorithm_plans SET updated=? WHERE id=?", (now, row["plan_id"])
            )
            self.db.execute("UPDATE task SET state_version=state_version+1")
            self.event("QUESTION_ANSWERED", {
                "question_id": question_id, "plan_id": row["plan_id"],
            })
        return row["plan_id"]

    def update_algorithm_plan(self, plan):
        with self.transaction():
            if not self.db.execute(
                "SELECT 1 FROM algorithm_plans WHERE id=?", (plan["plan_id"],)
            ).fetchone():
                raise TaskError("UNKNOWN_PLAN", "Algorithm preparation plan does not exist")
            self.db.execute(
                "UPDATE algorithm_plans SET status=?,body=?,updated=? WHERE id=?",
                (plan["status"], canonical(plan), time.time(), plan["plan_id"]),
            )
            self.event("ALGORITHM_PLAN_UPDATED", {
                "plan_id": plan["plan_id"], "status": plan["status"],
            })

    def begin_attempt(self, step_id, key, request, contract_version, expected):
        identifier(key)
        request_hash = digest(request)
        with self.transaction():
            previous = self.db.execute(
                "SELECT * FROM attempts WHERE idempotency_key=?", (key,)
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash or previous["step_id"] != step_id:
                    raise TaskError(
                        "IDEMPOTENCY_CONFLICT", "This key belongs to a different request"
                    )
                if previous["status"] == "COMMITTED":
                    step = self.db.execute(
                        "SELECT status FROM steps WHERE id=?", (step_id,)
                    ).fetchone()
                    if step[0] != "COMMITTED":
                        raise TaskError(
                            "RESULT_INVALIDATED", "The recorded result is no longer valid"
                        )
                    return {"cached": True, "result": json.loads(previous["result"])}
                raise TaskError(
                    "ATTEMPT_UNRESOLVED",
                    "Inspect or recover this attempt before retrying",
                    evidence={"attempt_id": previous["id"], "status": previous["status"]},
                    next_action="task_recover",
                )
            self.require_version(expected)
            task = self.task()
            if task["status"] != "READY":
                raise TaskError("TASK_STATE", "Task is not ready to execute")
            if contract_version != task["contract_version"] or contract_version == 0:
                raise TaskError("CONTRACT_CONFLICT", "A current task contract is required")
            step = self.db.execute("SELECT * FROM steps WHERE id=?", (step_id,)).fetchone()
            if step is None or step["status"] != "PLANNED":
                raise TaskError(
                    "STEP_CONTRACT_REQUIRED", "Submit a new step contract before execution"
                )
            if step["contract_version"] != contract_version:
                raise TaskError("CONTRACT_CONFLICT", "Step contract is outdated")
            for dependency in json.loads(step["dependencies"]):
                dep = self.db.execute(
                    "SELECT status FROM steps WHERE id=?", (dependency,)
                ).fetchone()
                if dep is None or dep[0] != "COMMITTED":
                    raise TaskError("INVALID_DEPENDENCY", "An upstream result is no longer valid")
            attempt_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO attempts(id,step_id,idempotency_key,request_hash,request,status,created) "
                "VALUES(?,?,?,?,?,'RUNNING',?)",
                (attempt_id, step_id, key, request_hash, canonical(request), time.time()),
            )
            self.db.execute("UPDATE steps SET status='RUNNING' WHERE id=?", (step_id,))
            self.db.execute("UPDATE task SET status='RUNNING', state_version=state_version+1")
            self.event("ATTEMPT_STARTED", {"attempt_id": attempt_id, "step_id": step_id})
        directory = self.directory / "attempts" / attempt_id
        directory.mkdir(parents=True, mode=0o700)
        return {"cached": False, "attempt_id": attempt_id, "directory": str(directory)}

    def prepare_commit(self, attempt_id, result, checkpoint):
        """Journal validated results before the final commit, for crash reconciliation."""
        with self.transaction():
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise TaskError("ATTEMPT_STATE", "Attempt is not running")
            self.db.execute(
                "UPDATE attempts SET status='VALIDATING',result=?,checkpoint=? WHERE id=?",
                (canonical(result), canonical(checkpoint), attempt_id),
            )
            self.db.execute("UPDATE steps SET status='VALIDATING' WHERE id=?", (row["step_id"],))
            self.event("COMMIT_PREPARED", {"attempt_id": attempt_id})

    def commit(self, attempt_id):
        """Caller must verify and fsync all artifacts, including on a recovered prepared commit."""
        with self.transaction():
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row["status"] != "VALIDATING":
                raise TaskError("ATTEMPT_STATE", "Attempt has no prepared validated result")
            self.db.execute("UPDATE attempts SET status='COMMITTED' WHERE id=?", (attempt_id,))
            self.db.execute("UPDATE steps SET status='COMMITTED' WHERE id=?", (row["step_id"],))
            self.db.execute(
                "UPDATE task SET checkpoint=?,status='READY',state_version=state_version+1",
                (row["checkpoint"],),
            )
            self.event("ATTEMPT_COMMITTED", {"attempt_id": attempt_id})
        return json.loads(row["result"])

    def fail(self, attempt_id, failure, *, cancelled=False):
        with self.transaction():
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row["status"] not in {"RUNNING", "VALIDATING"}:
                raise TaskError("ATTEMPT_STATE", "Cannot fail an inactive attempt")
            self.db.execute(
                "UPDATE attempts SET status='FAILED',failure=? WHERE id=?",
                (canonical(failure), attempt_id),
            )
            self.db.execute("UPDATE steps SET status='FAILED' WHERE id=?", (row["step_id"],))
            self.db.execute(
                "UPDATE task SET status=?,state_version=state_version+1",
                ("CANCELLED" if cancelled else "BLOCKED",),
            )
            self.event("ATTEMPT_FAILED", {"attempt_id": attempt_id, "failure": failure})

    def unresolved(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM attempts WHERE status IN ('RUNNING','VALIDATING') ORDER BY created"
            )
        ]

    def dependency_closure(self, step_ids):
        rows = self.db.execute("SELECT id,dependencies FROM steps").fetchall()
        affected = set(step_ids)
        if not affected <= {row["id"] for row in rows}:
            raise TaskError("STEP_NOT_FOUND", "Cannot invalidate an unknown step")
        while True:
            expanded = affected | {
                row["id"] for row in rows if affected.intersection(json.loads(row["dependencies"]))
            }
            if expanded == affected:
                return sorted(affected)
            affected = expanded

    def invalidate(self, step_ids, reason, *, checkpoint=None, revised_inputs=None,
                   discard_uncommitted=False):
        """Invalidate only the requested dependency closure; retain history and files."""
        with self.transaction():
            pending = self.unresolved()
            if discard_uncommitted and (revised_inputs is None or checkpoint is None):
                raise TaskError("INVALID_REVISION", "Discarding attempts requires revised inputs and a verified checkpoint")
            if pending and not discard_uncommitted:
                raise TaskError(
                    "ATTEMPT_UNRESOLVED", "Reconcile active attempts before invalidation"
                )
            affected = self.dependency_closure(step_ids)
            for attempt in pending:
                failure = {"code": "INPUT_REVISION_INTERRUPTED", "phase": "recovery",
                           "retryable": False,
                           "message": "Uncommitted result quarantined during explicit input revision"}
                self.db.execute("UPDATE attempts SET status='FAILED',failure=? WHERE id=?",
                                (canonical(failure), attempt["id"]))
                self.db.execute("UPDATE steps SET status='FAILED' WHERE id=?", (attempt["step_id"],))
                self.event("ATTEMPT_DISCARDED", {"attempt_id": attempt["id"],
                                                "previous_status": attempt["status"], "reason": reason})
            self.db.executemany(
                "UPDATE steps SET status='INVALIDATED' WHERE id=?", ((item,) for item in affected)
            )
            self.db.execute("UPDATE task SET status='BLOCKED',state_version=state_version+1")
            if checkpoint is not None:
                self.db.execute(
                    "UPDATE task SET checkpoint=?,status=?",
                    (canonical(checkpoint), "READY" if self.task()["contract_version"] else "PREPARING"),
                )
                self.event(
                    "REPAIR_STARTED",
                    {"roots": step_ids, "checkpoint": checkpoint, "reason": reason},
                )
            if revised_inputs is not None:
                before = self.task()["inputs"]
                self.db.execute("UPDATE task SET inputs=?", (canonical(revised_inputs),))
                self.event("INPUTS_REVISED", {"before": before, "after": revised_inputs,
                                             "steps": sorted(affected), "reason": reason})
            self.event("STEPS_INVALIDATED", {"steps": sorted(affected), "reason": reason})
        return sorted(affected)
