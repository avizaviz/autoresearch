"""FastAPI orchestrator for swarm coordination (Milestones 1-5)."""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import random
import re
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import structlog
import typer
import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from swarm.db import get_db, init_db
from swarm.agent import (
    AgentContext,
    AgentResult,
    TrialResult,
    create_agent,
    _read_file,
)

# ---------------------------------------------------------------------------
# Globals set at startup
# ---------------------------------------------------------------------------
DB_PATH: str = "runs/swarm.db"
AUTH_TOKEN: Optional[str] = None
RUNS_DIR: Path = Path("runs")
REPO_PATH: Optional[Path] = None
STALE_GRACE_SECONDS: int = int(os.environ.get("SWARM_STALE_GRACE", "1800"))
_agent_runner = None

# Model creation state
_model_processes: dict[str, subprocess.Popen] = {}  # model_id -> Popen
_loaded_model = None
_loaded_model_path: Optional[str] = None

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return dict(row)


def _error(status: int, code: str, detail: str):
    raise HTTPException(status_code=status, detail=json.dumps({"error": code, "detail": detail}))


# ---------------------------------------------------------------------------
# Template filters & UI helpers
# ---------------------------------------------------------------------------
_AVATAR_GRADIENTS = [
    "linear-gradient(135deg,#3b82f6,#06b6d4)",
    "linear-gradient(135deg,#8b5cf6,#ec4899)",
    "linear-gradient(135deg,#10b981,#06b6d4)",
    "linear-gradient(135deg,#f59e0b,#ef4444)",
    "linear-gradient(135deg,#6366f1,#8b5cf6)",
    "linear-gradient(135deg,#14b8a6,#22c55e)",
    "linear-gradient(135deg,#ec4899,#f43f5e)",
    "linear-gradient(135deg,#06b6d4,#3b82f6)",
]


def _timeago(iso_str: str | None) -> str:
    if not iso_str:
        return "--"
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return "--"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    delta = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        h = int(delta / 3600)
        m = int((delta % 3600) / 60)
        return f"{h}h {m}m ago"
    return f"{int(delta / 86400)}d ago"


def _format_duration(seconds) -> str:
    if seconds is None:
        return "--"
    seconds = float(seconds)
    if seconds < 1:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m {int(seconds % 60):02d}s"
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    return f"{h}h {m:02d}m"


def _running_duration(iso_str: str | None) -> str:
    """Time elapsed since started_at until now, formatted as duration."""
    if not iso_str:
        return "--"
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return "--"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    delta = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
    return _format_duration(max(0, delta))


def _seconds_ago(seconds) -> str:
    if seconds is None:
        return "--"
    seconds = float(seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def _badge_class(status: str) -> str:
    return {
        "running": "b-run",
        "completed": "b-done",
        "failed": "b-fail",
        "stopped": "b-stop",
        "draft": "b-draft",
        "queued": "b-queue",
        "training": "b-train",
        "idle": "b-idle",
        "offline": "b-off",
    }.get(status, "b-draft")


def _initials(name: str) -> str:
    parts = name.replace("-", " ").split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()


def _avatar_gradient(name: str) -> str:
    h = sum(ord(c) for c in name) % len(_AVATAR_GRADIENTS)
    return _AVATAR_GRADIENTS[h]


def _derive_workers(conn) -> list[dict]:
    """Compute worker state and stats, same logic as GET /api/workers."""
    rows = conn.execute("SELECT * FROM workers").fetchall()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    result = []
    for r in rows:
        w = _row_to_dict(r)
        last_seen = datetime.datetime.fromisoformat(w["last_seen_at"])
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=datetime.timezone.utc)
        age = (now_dt - last_seen).total_seconds()
        if age > 120:
            w["state"] = "offline"
        else:
            running = conn.execute(
                "SELECT id FROM trials WHERE worker_id = ? AND status = 'running' LIMIT 1",
                (w["id"],),
            ).fetchone()
            w["state"] = "training" if running else "idle"
        w["last_seen_seconds"] = age

        stats = conn.execute(
            """SELECT COUNT(*) as cnt, AVG(duration_seconds) as avg_dur,
                      MIN(val_bpb) as best_bpb
               FROM trials WHERE worker_id = ? AND status = 'completed'""",
            (w["id"],),
        ).fetchone()
        w["trials_done"] = stats["cnt"] or 0
        w["avg_duration"] = stats["avg_dur"]
        w["best_val_bpb"] = stats["best_bpb"]

        current = conn.execute(
            """SELECT id, trial_index, git_commit, started_at
               FROM trials WHERE worker_id = ? AND status = 'running' LIMIT 1""",
            (w["id"],),
        ).fetchone()
        w["current_trial"] = _row_to_dict(current) if current else None

        stuck = conn.execute(
            """SELECT id, trial_index FROM trials
               WHERE worker_id = ? AND status = 'running'
               AND last_heartbeat_at < ?""",
            (w["id"], (now_dt - datetime.timedelta(seconds=STALE_GRACE_SECONDS)).isoformat()),
        ).fetchone()
        w["stuck_trial"] = _row_to_dict(stuck) if stuck else None

        if w.get("meta_json"):
            try:
                meta = json.loads(w["meta_json"])
                w["gpu"] = meta.get("gpu", "")
            except (json.JSONDecodeError, TypeError):
                w["gpu"] = ""
        else:
            w["gpu"] = ""

        result.append(w)
    return result


def _get_stale_trials(conn) -> list[dict]:
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=STALE_GRACE_SECONDS)
    ).isoformat()
    rows = conn.execute(
        """SELECT t.id, t.trial_index, t.worker_id,
                  w.display_name as worker_name, t.last_heartbeat_at
           FROM trials t LEFT JOIN workers w ON t.worker_id = w.id
           WHERE t.status = 'running' AND t.last_heartbeat_at < ?""",
        (cutoff,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _compute_home_stats(conn) -> list[dict]:
    running = conn.execute(
        "SELECT id, name, best_val_bpb FROM experiments WHERE status = 'running' LIMIT 1"
    ).fetchone()
    active_count = 1 if running else 0
    active_name = running["name"] if running else "none"

    completed = conn.execute(
        "SELECT COUNT(*) as cnt FROM trials WHERE status = 'completed'"
    ).fetchone()["cnt"]

    best_bpb = "--"
    if running and running["best_val_bpb"] is not None:
        best_bpb = f"{running['best_val_bpb']:.4f}"

    workers = _derive_workers(conn)
    online = sum(1 for w in workers if w["state"] != "offline")
    idle = sum(1 for w in workers if w["state"] == "idle")

    avg = conn.execute(
        """SELECT AVG(duration_seconds) as avg_dur
           FROM trials WHERE status = 'completed' AND duration_seconds IS NOT NULL"""
    ).fetchone()

    return [
        {"label": "Active Experiment", "value": str(active_count),
         "sub": active_name, "cls": "sc", "color": "cyan"},
        {"label": "Completed Trials", "value": str(completed),
         "sub": "", "cls": "sg", "color": "green"},
        {"label": "Best val_bpb", "value": best_bpb,
         "sub": "", "cls": "sg", "color": "green"},
        {"label": "Workers Active", "value": str(online),
         "sub": f"{idle} idle", "cls": "sa", "color": "amber"},
        {"label": "Avg Trial Time", "value": _format_duration(avg["avg_dur"]),
         "sub": "", "cls": "sb", "color": ""},
    ]


def _compute_experiment_stats(conn, exp: dict) -> list[dict]:
    tc = exp.get("trial_counts", {})
    total = sum(tc.values()) if tc else 0
    completed = tc.get("completed", 0)
    running_count = tc.get("running", 0)
    queued = tc.get("queued", 0)

    elapsed = "--"
    if exp.get("created_at"):
        try:
            dt = datetime.datetime.fromisoformat(exp["created_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            secs = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
            elapsed = _format_duration(max(0, secs))
        except (ValueError, TypeError):
            pass

    best_bpb = "--"
    if exp.get("best_val_bpb") is not None:
        best_bpb = f"{exp['best_val_bpb']:.4f}"

    workers_on = conn.execute(
        """SELECT COUNT(DISTINCT worker_id) as cnt FROM trials
           WHERE experiment_id = ? AND status = 'running'""",
        (exp["id"],),
    ).fetchone()["cnt"]
    total_workers = conn.execute("SELECT COUNT(*) as cnt FROM workers").fetchone()["cnt"]

    avg = conn.execute(
        """SELECT AVG(duration_seconds) as avg_dur
           FROM trials WHERE experiment_id = ? AND status = 'completed'
           AND duration_seconds IS NOT NULL""",
        (exp["id"],),
    ).fetchone()

    sub_trials = f"{completed} done"
    if running_count:
        sub_trials += f" · {running_count} running"
    if queued:
        sub_trials += f" · {queued} queued"

    return [
        {"label": "Running For", "value": elapsed,
         "sub": "", "cls": "sc", "color": "cyan"},
        {"label": "Best val_bpb", "value": best_bpb,
         "sub": "", "cls": "sg", "color": "green"},
        {"label": "Trials", "value": str(total),
         "sub": sub_trials, "cls": "sg", "color": "green"},
        {"label": "Workers on This", "value": str(workers_on),
         "sub": f"of {total_workers} registered", "cls": "sa", "color": "amber"},
        {"label": "Avg Trial Time", "value": _format_duration(avg["avg_dur"]),
         "sub": "", "cls": "sb", "color": ""},
    ]


def _experiment_elapsed(exp: dict) -> str:
    if not exp.get("created_at"):
        return "--"
    try:
        dt = datetime.datetime.fromisoformat(exp["created_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        secs = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
        return _format_duration(max(0, secs))
    except (ValueError, TypeError):
        return "--"


def configure_logging():
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
_shutdown_event = asyncio.Event()


def refill_once(db_path: str, agent=None, repo_path: Path | None = None):
    """Create a new queued trial by invoking the agent to produce a new commit.
    
    If no agent is provided or agent_type=none, falls back to inserting a trial
    with the experiment's git_ref (legacy/test behavior).
    """
    conn = get_db(db_path)
    try:
        exp = conn.execute(
            "SELECT * FROM experiments WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if exp is None:
            return
        exp_id = exp["id"]
        queued_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM trials WHERE experiment_id = ? AND status = 'queued'",
            (exp_id,),
        ).fetchone()["cnt"]
        if queued_count >= 1:
            return

        running_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM trials WHERE experiment_id = ? AND status = 'running'",
            (exp_id,),
        ).fetchone()["cnt"]
        if running_count > 0:
            return

        max_idx = conn.execute(
            "SELECT COALESCE(MAX(trial_index), -1) as mx FROM trials WHERE experiment_id = ?",
            (exp_id,),
        ).fetchone()["mx"]
        next_index = max_idx + 1

        git_commit = None
        git_ref = exp["git_ref"]

        if agent is not None and repo_path is not None:
            last_trial = conn.execute(
                """SELECT trial_index, val_bpb, exit_code, stderr_tail, git_commit, status
                   FROM trials WHERE experiment_id = ? ORDER BY trial_index DESC LIMIT 1""",
                (exp_id,),
            ).fetchone()

            last_result = None
            if last_trial:
                last_result = TrialResult(
                    trial_index=last_trial["trial_index"],
                    val_bpb=last_trial["val_bpb"],
                    exit_code=last_trial["exit_code"] or 0,
                    stderr_tail=last_trial["stderr_tail"],
                    git_commit=last_trial["git_commit"],
                    status=last_trial["status"],
                )

            history_rows = conn.execute(
                """SELECT trial_index, val_bpb, exit_code, stderr_tail, git_commit, status
                   FROM trials WHERE experiment_id = ? ORDER BY trial_index""",
                (exp_id,),
            ).fetchall()
            history = [
                TrialResult(
                    trial_index=r["trial_index"], val_bpb=r["val_bpb"],
                    exit_code=r["exit_code"] or 0, stderr_tail=r["stderr_tail"],
                    git_commit=r["git_commit"], status=r["status"],
                )
                for r in history_rows
            ]

            prompt_text = exp["program_prompt_inline"] or ""
            if not prompt_text and exp["prompt_uri"]:
                prompt_path = Path(exp["prompt_uri"])
                if prompt_path.exists():
                    prompt_text = _read_file(prompt_path)

            train_content = _read_file(repo_path / "train.py")

            ctx = AgentContext(
                repo_path=repo_path,
                experiment_prompt=prompt_text,
                train_py_content=train_content,
                last_result=last_result,
                best_commit=exp["best_commit"],
                best_val_bpb=exp["best_val_bpb"],
                history=history,
                trial_index=next_index,
            )

            result = agent.run(ctx)
            if result.success and result.new_commit_sha:
                git_commit = result.new_commit_sha
                git_ref = None
                log.msg("agent.produced_commit", sha=git_commit, description=result.description)
            else:
                log.info("agent.no_commit", error=result.error)
                if exp["best_commit"]:
                    git_commit = exp["best_commit"]
                    git_ref = None
                    if not last_trial:
                        log.msg("refill.using_best_commit_for_first_trial",
                                git_commit=git_commit)
                    else:
                        log.msg("refill.using_best_commit_no_agent",
                                git_commit=git_commit)
                else:
                    return

        trial_id = _short_uuid()
        conn.execute(
            """INSERT INTO trials (id, experiment_id, trial_index, status, git_ref, git_commit, created_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
            (trial_id, exp_id, next_index, git_ref, git_commit, _now()),
        )
        conn.commit()
        log.msg("server.trial_queued", experiment_id=exp_id, trial_id=trial_id,
                git_commit=git_commit)
    finally:
        conn.close()


async def _refill_loop():
    """Ensure there are always queued trials for the running experiment."""
    while not _shutdown_event.is_set():
        try:
            await asyncio.sleep(5)
            refill_once(DB_PATH, agent=_agent_runner, repo_path=REPO_PATH)
        except Exception:
            log.exception("refill_error")


async def _stale_detection_loop():
    """Detect and handle stale running trials."""
    while not _shutdown_event.is_set():
        try:
            await asyncio.sleep(30)
            cutoff = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(seconds=STALE_GRACE_SECONDS)
            ).isoformat()
            conn = get_db(DB_PATH)
            try:
                stale = conn.execute(
                    """SELECT id, attempt_count, experiment_id FROM trials
                       WHERE status = 'running' AND last_heartbeat_at < ?""",
                    (cutoff,),
                ).fetchall()
                for t in stale:
                    tid = t["id"]
                    attempts = (t["attempt_count"] or 0) + 1
                    if attempts >= 3:
                        conn.execute(
                            """UPDATE trials SET status = 'failed',
                               stderr_tail = 'worker_lost: max attempts exceeded',
                               completed_at = ?, attempt_count = ?
                               WHERE id = ?""",
                            (_now(), attempts, tid),
                        )
                    else:
                        conn.execute(
                            """UPDATE trials SET status = 'queued',
                               worker_id = NULL, attempt_count = ?
                               WHERE id = ?""",
                            (attempts, tid),
                        )
                    log.msg("server.trial_stale", trial_id=tid, attempt_count=attempts,
                            experiment_id=t["experiment_id"])
                conn.commit()
            finally:
                conn.close()
        except Exception:
            log.exception("stale_detection_error")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)
    refill_task = asyncio.create_task(_refill_loop())
    stale_task = asyncio.create_task(_stale_detection_loop())
    yield
    _shutdown_event.set()
    refill_task.cancel()
    stale_task.cancel()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static files & templates
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
_STATIC_DIR = _HERE / "static"
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR.mkdir(exist_ok=True)
_TEMPLATES_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["timeago"] = _timeago
templates.env.filters["duration"] = _format_duration
templates.env.filters["running_duration"] = _running_duration
templates.env.filters["seconds_ago"] = _seconds_ago
templates.env.filters["badge_class"] = _badge_class
templates.env.filters["initials"] = _initials
templates.env.filters["avatar_gradient"] = _avatar_gradient


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if AUTH_TOKEN and request.url.path.startswith("/api/"):
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {AUTH_TOKEN}":
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "detail": "invalid or missing token"},
            )
    response = await call_next(request)
    return response


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    try:
        body = json.loads(exc.detail)
    except (json.JSONDecodeError, TypeError):
        body = {"error": "error", "detail": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=body)


# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------
def db_conn():
    conn = get_db(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Workers (Milestone 2)
# ---------------------------------------------------------------------------
@app.post("/api/workers/register")
def worker_register(body: dict, conn=Depends(db_conn)):
    hostname = body.get("hostname", "unknown")
    meta_json = body.get("meta_json")
    now = _now()

    existing_worker_id = body.get("worker_id")
    if existing_worker_id:
        existing = conn.execute("SELECT id, display_name FROM workers WHERE id = ?", (existing_worker_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE workers SET hostname = ?, last_seen_at = ?, meta_json = ? WHERE id = ?",
                (hostname, now, meta_json, existing_worker_id),
            )
            conn.commit()
            log.msg("server.worker_registered", worker_id=existing_worker_id,
                    display_name=existing["display_name"], reconnect=True)
            return {"worker_id": existing_worker_id, "display_name": existing["display_name"]}

    worker_id = _short_uuid()
    display_name = f"Worker-{random.randint(1000, 9999)}"
    conn.execute(
        """INSERT INTO workers (id, display_name, hostname, registered_at, last_seen_at, meta_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (worker_id, display_name, hostname, now, now, meta_json),
    )
    conn.commit()
    log.msg("server.worker_registered", worker_id=worker_id, display_name=display_name)
    return {"worker_id": worker_id, "display_name": display_name}


@app.post("/api/workers/{worker_id}/heartbeat")
def worker_heartbeat(worker_id: str, body: dict, conn=Depends(db_conn)):
    now = _now()
    conn.execute("UPDATE workers SET last_seen_at = ? WHERE id = ?", (now, worker_id))
    running_trial_id = body.get("running_trial_id")
    if running_trial_id:
        phase = body.get("current_phase", "")
        train_pct = body.get("training_pct", 0)
        val_pct = body.get("validation_pct", 0)
        conn.execute(
            """UPDATE trials SET last_heartbeat_at = ?, current_phase = ?,
               training_pct = ?, validation_pct = ? WHERE id = ?""",
            (now, phase, train_pct, val_pct, running_trial_id),
        )
    conn.commit()
    return {"status": "ok"}


@app.get("/api/workers")
def list_workers(conn=Depends(db_conn)):
    rows = conn.execute("SELECT * FROM workers").fetchall()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    result = []
    for r in rows:
        w = _row_to_dict(r)
        last_seen = datetime.datetime.fromisoformat(w["last_seen_at"])
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=datetime.timezone.utc)
        age = (now_dt - last_seen).total_seconds()
        if age > 120:
            w["state"] = "offline"
        else:
            running = conn.execute(
                "SELECT id FROM trials WHERE worker_id = ? AND status = 'running' LIMIT 1",
                (w["id"],),
            ).fetchone()
            w["state"] = "training" if running else "idle"
        result.append(w)
    return result


# ---------------------------------------------------------------------------
# Experiments (Milestone 3)
# ---------------------------------------------------------------------------
@app.post("/api/experiments")
def create_experiment(body: dict, conn=Depends(db_conn)):
    exp_id = _short_uuid()
    name = body.get("name", "unnamed")
    git_ref = body.get("git_ref")
    now = _now()
    conn.execute(
        """INSERT INTO experiments (id, name, created_at, status, git_ref)
           VALUES (?, ?, ?, 'draft', ?)""",
        (exp_id, name, now, git_ref),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    log.msg("server.experiment_created", experiment_id=exp_id)
    return _row_to_dict(row)


@app.get("/api/experiments")
def list_experiments(conn=Depends(db_conn)):
    rows = conn.execute("SELECT * FROM experiments ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        e = _row_to_dict(r)
        counts = conn.execute(
            """SELECT status, COUNT(*) as cnt FROM trials
               WHERE experiment_id = ? GROUP BY status""",
            (e["id"],),
        ).fetchall()
        e["trial_counts"] = {c["status"]: c["cnt"] for c in counts}
        running_trial = conn.execute(
            """SELECT current_phase, training_pct, validation_pct FROM trials
               WHERE experiment_id = ? AND status = 'running' LIMIT 1""",
            (e["id"],),
        ).fetchone()
        if running_trial:
            e["running_phase"] = running_trial["current_phase"]
            e["running_train_pct"] = running_trial["training_pct"]
            e["running_val_pct"] = running_trial["validation_pct"]
        result.append(e)
    return result


@app.get("/api/experiments/{exp_id}")
def get_experiment(exp_id: str, conn=Depends(db_conn)):
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        _error(404, "not_found", f"experiment {exp_id} not found")
    e = _row_to_dict(row)
    counts = conn.execute(
        """SELECT status, COUNT(*) as cnt FROM trials
           WHERE experiment_id = ? GROUP BY status""",
        (e["id"],),
    ).fetchall()
    e["trial_counts"] = {c["status"]: c["cnt"] for c in counts}
    stuck_cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=STALE_GRACE_SECONDS)
    ).isoformat()
    stuck = conn.execute(
        """SELECT COUNT(*) as cnt FROM trials
           WHERE experiment_id = ? AND status = 'running' AND last_heartbeat_at < ?""",
        (e["id"], stuck_cutoff),
    ).fetchone()
    e["stuck_count"] = stuck["cnt"]
    return e


@app.put("/api/experiments/{exp_id}/dataset")
async def upload_dataset(exp_id: str, file: UploadFile = File(...), conn=Depends(db_conn)):
    row = conn.execute("SELECT id FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        _error(404, "not_found", f"experiment {exp_id} not found")
    dest_dir = RUNS_DIR / "experiments" / exp_id / "dataset"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (file.filename or "dataset.bin")
    content = await file.read()
    dest.write_bytes(content)
    dataset_uri = str(dest)
    conn.execute("UPDATE experiments SET dataset_uri = ? WHERE id = ?", (dataset_uri, exp_id))
    conn.commit()
    return {"dataset_uri": dataset_uri}


@app.put("/api/experiments/{exp_id}/prompt")
async def upload_prompt(exp_id: str, request: Request, conn=Depends(db_conn)):
    row = conn.execute("SELECT id FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        _error(404, "not_found", f"experiment {exp_id} not found")

    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type:
        form = await request.form()
        f = form.get("file")
        if f:
            dest_dir = RUNS_DIR / "experiments" / exp_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / "prompt.txt"
            data = await f.read()
            dest.write_bytes(data)
            prompt_uri = str(dest)
            conn.execute("UPDATE experiments SET prompt_uri = ? WHERE id = ?", (prompt_uri, exp_id))
            conn.commit()
            return {"prompt_uri": prompt_uri}

    body = await request.body()
    text = body.decode("utf-8")
    conn.execute(
        "UPDATE experiments SET program_prompt_inline = ?, prompt_uri = 'inline' WHERE id = ?",
        (text, exp_id),
    )
    conn.commit()
    return {"prompt_uri": "inline"}


@app.post("/api/experiments/{exp_id}/start")
def start_experiment(exp_id: str, conn=Depends(db_conn)):
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        _error(404, "not_found", f"experiment {exp_id} not found")
    exp = _row_to_dict(row)

    if not exp.get("dataset_uri"):
        _error(400, "missing_dataset", "dataset not uploaded")
    if not exp.get("prompt_uri") and not exp.get("program_prompt_inline"):
        _error(400, "missing_prompt", "prompt not uploaded")

    running = conn.execute(
        "SELECT id FROM experiments WHERE status = 'running' AND id != ?",
        (exp_id,),
    ).fetchone()
    if running:
        log.msg("server.start_rejected", experiment_id=exp_id, conflict_id=running["id"])
        raise HTTPException(
            status_code=409,
            detail=json.dumps({"error": "conflict", "detail": f"experiment {running['id']} is already running"}),
        )

    branch_name = None
    if REPO_PATH and exp.get("status") == "draft":
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", exp.get("name") or exp_id)
        branch_name = f"autoresearch/{safe_name}"
        try:
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=str(REPO_PATH), capture_output=True, timeout=10,
            )
            log.msg("server.branch_created", branch=branch_name)
        except Exception:
            try:
                subprocess.run(
                    ["git", "checkout", branch_name],
                    cwd=str(REPO_PATH), capture_output=True, timeout=10,
                )
                log.msg("server.branch_reused", branch=branch_name)
            except Exception as e:
                log.warning("server.branch_failed", error=str(e))

    update_fields = "status = 'running', stop_requested_at = NULL"
    params: list = []
    if branch_name and not exp.get("git_ref"):
        update_fields += ", git_ref = ?"
        params.append(branch_name)
    params.append(exp_id)
    conn.execute(f"UPDATE experiments SET {update_fields} WHERE id = ?", params)

    if exp.get("status") == "draft" and REPO_PATH:
        head_sha = None
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(REPO_PATH),
                capture_output=True, text=True, check=True, timeout=10,
            )
            head_sha = result.stdout.strip()
        except Exception:
            pass
        if head_sha and not exp.get("best_commit"):
            conn.execute(
                "UPDATE experiments SET best_commit = ? WHERE id = ?",
                (head_sha, exp_id),
            )

    conn.commit()
    log.msg("server.experiment_started", experiment_id=exp_id, branch=branch_name)
    updated = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    return _row_to_dict(updated)


@app.post("/api/experiments/{exp_id}/stop")
def stop_experiment(exp_id: str, conn=Depends(db_conn)):
    row = conn.execute("SELECT id FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        _error(404, "not_found", f"experiment {exp_id} not found")
    now = _now()
    conn.execute(
        "UPDATE experiments SET status = 'stopped', stop_requested_at = ? WHERE id = ?",
        (now, exp_id),
    )
    conn.commit()
    log.msg("server.experiment_stopped", experiment_id=exp_id)
    updated = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    return _row_to_dict(updated)


@app.delete("/api/experiments/{exp_id}")
def delete_experiment(exp_id: str, conn=Depends(db_conn)):
    """Delete an experiment and ALL associated data: trials, models, uploaded files."""
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        _error(404, "not_found", f"experiment {exp_id} not found")

    if row["status"] == "running":
        _error(409, "conflict", "cannot delete a running experiment — stop it first")

    conn.execute("DELETE FROM models WHERE experiment_id = ?", (exp_id,))
    conn.execute("DELETE FROM trials WHERE experiment_id = ?", (exp_id,))
    conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
    conn.commit()

    exp_dir = RUNS_DIR / "experiments" / exp_id
    if exp_dir.exists():
        import shutil
        shutil.rmtree(exp_dir, ignore_errors=True)
        log.msg("server.experiment_files_deleted", path=str(exp_dir))

    log.msg("server.experiment_deleted", experiment_id=exp_id)
    return {"deleted": True, "experiment_id": exp_id}


@app.post("/api/test-experiment")
def create_test_experiment(conn=Depends(db_conn)):
    """Create a default test experiment with toy dataset + prompt, ready to start."""
    exp_id = _short_uuid()
    now = _now()
    conn.execute(
        "INSERT INTO experiments (id, name, created_at, status) VALUES (?, ?, ?, 'draft')",
        (exp_id, "test-experiment", now),
    )

    toy_dir = Path(__file__).parent.parent / "tests" / "e2e" / "toy_next_number"
    dataset_src = toy_dir / "data.jsonl"
    prompt_src = toy_dir / "prompt.txt"

    if dataset_src.exists():
        dest_dir = RUNS_DIR / "experiments" / exp_id / "dataset"
        dest_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        dest = dest_dir / "data.jsonl"
        shutil.copy(dataset_src, dest)
        conn.execute("UPDATE experiments SET dataset_uri = ? WHERE id = ?", (str(dest), exp_id))

    if prompt_src.exists():
        prompt_text = prompt_src.read_text()
        conn.execute(
            "UPDATE experiments SET program_prompt_inline = ?, prompt_uri = 'inline' WHERE id = ?",
            (prompt_text, exp_id),
        )

    conn.commit()
    log.msg("server.test_experiment_created", experiment_id=exp_id)
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Claim + Complete (Milestone 4)
# ---------------------------------------------------------------------------
@app.post("/api/workers/{worker_id}/claim")
def claim_trial(worker_id: str, conn=Depends(db_conn)):
    exp = conn.execute("SELECT id FROM experiments WHERE status = 'running' LIMIT 1").fetchone()
    if not exp:
        return JSONResponse(status_code=204, content=None)

    now = _now()
    trial = conn.execute(
        """SELECT id, git_ref, git_commit, experiment_id, env_json
           FROM trials WHERE experiment_id = ? AND status = 'queued'
           ORDER BY trial_index ASC LIMIT 1""",
        (exp["id"],),
    ).fetchone()
    if not trial:
        return JSONResponse(status_code=204, content=None)

    trial_id = trial["id"]
    conn.execute(
        """UPDATE trials SET status = 'running', worker_id = ?, started_at = ?,
           last_heartbeat_at = ? WHERE id = ? AND status = 'queued'""",
        (worker_id, now, now, trial_id),
    )
    conn.commit()

    verify = conn.execute("SELECT status FROM trials WHERE id = ?", (trial_id,)).fetchone()
    if verify["status"] != "running":
        return JSONResponse(status_code=204, content=None)

    log.msg("server.trial_claimed", trial_id=trial_id, worker_id=worker_id,
            experiment_id=trial["experiment_id"])
    return {
        "trial_id": trial_id,
        "git_ref": trial["git_ref"],
        "git_commit": trial["git_commit"],
        "experiment_id": trial["experiment_id"],
        "env_json": trial["env_json"],
    }


@app.post("/api/trials/{trial_id}/complete")
def complete_trial(trial_id: str, body: dict, conn=Depends(db_conn)):
    trial = conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone()
    if not trial:
        _error(404, "not_found", f"trial {trial_id} not found")

    current_status = trial["status"]
    exit_code = body.get("exit_code", 1)
    val_bpb = body.get("val_bpb")
    stderr_tail = body.get("stderr_tail")
    git_commit = body.get("git_commit")
    worker_id = body.get("worker_id")
    now = _now()

    new_status = "completed" if exit_code == 0 else "failed"

    # --- Edge case: trial already has a terminal status ---
    if current_status == "completed":
        # Already completed successfully — never overwrite with a failure.
        # A late-arriving duplicate worker report is harmless.
        log.warning("server.complete_ignored_already_completed",
                    trial_id=trial_id, incoming_status=new_status,
                    incoming_worker=worker_id)
        return {"status": "completed", "ignored": True,
                "reason": "trial already completed successfully"}

    if current_status == "failed" and new_status == "failed":
        # Already failed, another failure — ignore duplicate
        log.warning("server.complete_ignored_already_failed",
                    trial_id=trial_id, incoming_worker=worker_id)
        return {"status": "failed", "ignored": True,
                "reason": "trial already failed"}

    if current_status == "failed" and new_status == "completed":
        # Previously marked failed (e.g. stale detection), but a worker actually
        # completed successfully. Accept the success — it's real work.
        log.msg("server.complete_overrides_failed",
                trial_id=trial_id, val_bpb=val_bpb, incoming_worker=worker_id)

    if current_status == "queued":
        # Stale detection requeued it, but original worker finished.
        # Accept the result — another worker may have claimed it too,
        # but this result is valid.
        log.msg("server.complete_from_requeued",
                trial_id=trial_id, val_bpb=val_bpb, incoming_worker=worker_id)

    # --- Compute duration ---
    started_at = trial["started_at"]
    duration = None
    if started_at:
        start_dt = datetime.datetime.fromisoformat(started_at)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)
        duration = (datetime.datetime.now(datetime.timezone.utc) - start_dt).total_seconds()

    # --- Update trial ---
    conn.execute(
        """UPDATE trials SET status = ?, completed_at = ?, duration_seconds = ?,
           exit_code = ?, val_bpb = ?, stderr_tail = ?, git_commit = ?
           WHERE id = ?""",
        (new_status, now, duration, exit_code, val_bpb, stderr_tail, git_commit, trial_id),
    )

    # --- Update experiment best if this is a new record ---
    if new_status == "completed" and val_bpb is not None:
        exp = conn.execute(
            "SELECT best_val_bpb FROM experiments WHERE id = ?", (trial["experiment_id"],)
        ).fetchone()
        if exp["best_val_bpb"] is None or val_bpb < exp["best_val_bpb"]:
            conn.execute(
                "UPDATE experiments SET best_val_bpb = ?, best_commit = ? WHERE id = ?",
                (val_bpb, git_commit, trial["experiment_id"]),
            )

    conn.commit()

    event = "server.trial_completed" if new_status == "completed" else "server.trial_failed"
    log.msg(event, trial_id=trial_id, experiment_id=trial["experiment_id"],
            exit_code=exit_code, val_bpb=val_bpb)
    return {"status": new_status}


# ---------------------------------------------------------------------------
# Trials listing
# ---------------------------------------------------------------------------
@app.get("/api/experiments/{exp_id}/trials")
def list_trials(
    exp_id: str,
    status: Optional[str] = Query(None),
    sort: str = Query("trial_index"),
    order: str = Query("desc"),
    per_page: int = Query(50),
    page: int = Query(1),
    conn=Depends(db_conn),
):
    allowed_sorts = {"trial_index", "created_at", "val_bpb", "status"}
    if sort not in allowed_sorts:
        sort = "trial_index"
    direction = "DESC" if order.lower() == "desc" else "ASC"

    base_where = "WHERE experiment_id = ?"
    params_where: list = [exp_id]
    if status:
        base_where += " AND status = ?"
        params_where.append(status)

    total = conn.execute(
        f"SELECT COUNT(*) as cnt FROM trials {base_where}", params_where
    ).fetchone()["cnt"]

    query = f"SELECT * FROM trials {base_where} ORDER BY {sort} {direction} LIMIT ? OFFSET ?"
    params_q = params_where + [per_page, (page - 1) * per_page]

    rows = conn.execute(query, params_q).fetchall()
    total_pages = max(1, (total + per_page - 1) // per_page)

    return {
        "trials": [_row_to_dict(r) for r in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


# ---------------------------------------------------------------------------
# Model endpoints
# ---------------------------------------------------------------------------
def _get_latest_model(conn, exp_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM models WHERE experiment_id = ? ORDER BY created_at DESC LIMIT 1",
        (exp_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def _format_file_size(path: str | None) -> str:
    if not path:
        return "--"
    try:
        size = Path(path).stat().st_size
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / 1024 / 1024:.1f} MB"
        return f"{size / 1024 / 1024 / 1024:.1f} GB"
    except OSError:
        return "--"


@app.get("/api/experiments/{exp_id}/model")
def get_model(exp_id: str, conn=Depends(db_conn)):
    model = _get_latest_model(conn, exp_id)
    if not model:
        return {"status": "none"}
    exp = conn.execute(
        "SELECT best_val_bpb FROM experiments WHERE id = ?", (exp_id,)
    ).fetchone()
    is_outdated = False
    if (
        exp
        and exp["best_val_bpb"] is not None
        and model.get("source_val_bpb") is not None
        and model["status"] == "completed"
        and exp["best_val_bpb"] < model["source_val_bpb"]
    ):
        is_outdated = True
    model["is_outdated"] = is_outdated
    model["file_size"] = _format_file_size(model.get("model_path"))
    return model


@app.post("/api/experiments/{exp_id}/model/create")
async def create_model(exp_id: str, conn=Depends(db_conn)):
    exp = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not exp:
        _error(404, "not_found", f"experiment {exp_id} not found")
    if not exp["best_commit"]:
        _error(400, "no_best_commit", "experiment has no best commit yet")

    existing = conn.execute(
        "SELECT id FROM models WHERE experiment_id = ? AND status = 'creating'",
        (exp_id,),
    ).fetchone()
    if existing:
        _error(409, "already_creating", "a model is already being created for this experiment")

    model_id = _short_uuid()
    now = _now()
    model_path = str(RUNS_DIR / "experiments" / exp_id / "model" / "model.pt")
    conn.execute(
        """INSERT INTO models (id, experiment_id, source_commit, source_val_bpb,
           status, model_path, created_at)
           VALUES (?, ?, ?, ?, 'creating', ?, ?)""",
        (model_id, exp_id, exp["best_commit"], exp["best_val_bpb"], model_path, now),
    )
    conn.commit()
    log.msg("server.model_create_started", model_id=model_id, experiment_id=exp_id,
            source_commit=exp["best_commit"])

    asyncio.create_task(_run_model_creation(model_id, exp_id, exp["best_commit"], model_path))

    row = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    return _row_to_dict(row)


async def _run_model_creation(model_id: str, exp_id: str, source_commit: str, model_path: str):
    """Background task: checkout source_commit, run train.py with SWARM_SAVE_MODEL."""
    t0 = time.monotonic()
    repo = REPO_PATH or Path.cwd()
    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _start_model_training(repo, source_commit, model_path)
        )
        _model_processes[model_id] = proc

        exit_code = await asyncio.get_event_loop().run_in_executor(None, proc.wait)
        duration = time.monotonic() - t0

        conn = get_db(DB_PATH)
        try:
            if exit_code == 0 and Path(model_path).exists():
                conn.execute(
                    """UPDATE models SET status = 'completed', completed_at = ?,
                       duration_seconds = ? WHERE id = ?""",
                    (_now(), duration, model_id),
                )
                log.msg("server.model_completed", model_id=model_id, duration=f"{duration:.1f}s")
            else:
                stderr_text = ""
                if proc.stderr:
                    stderr_text = proc.stderr.read().decode("utf-8", errors="replace")[-500:]
                conn.execute(
                    """UPDATE models SET status = 'failed', completed_at = ?,
                       duration_seconds = ?, error = ? WHERE id = ?""",
                    (_now(), duration, f"exit_code={exit_code}: {stderr_text}", model_id),
                )
                log.msg("server.model_failed", model_id=model_id, exit_code=exit_code)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        duration = time.monotonic() - t0
        conn = get_db(DB_PATH)
        try:
            conn.execute(
                """UPDATE models SET status = 'failed', completed_at = ?,
                   duration_seconds = ?, error = ? WHERE id = ?""",
                (_now(), duration, str(e), model_id),
            )
            conn.commit()
        finally:
            conn.close()
        log.exception("server.model_creation_error", model_id=model_id)
    finally:
        _model_processes.pop(model_id, None)


def _start_model_training(repo: Path, source_commit: str, model_path: str) -> subprocess.Popen:
    """Checkout source_commit and start train.py with SWARM_SAVE_MODEL."""
    subprocess.run(
        ["git", "checkout", source_commit],
        cwd=str(repo), capture_output=True, timeout=30, check=True,
    )
    venv_python = repo / ".venv" / "bin" / "python"
    cmd = [str(venv_python), "train.py"] if venv_python.exists() else ["python", "train.py"]
    env = os.environ.copy()
    env["SWARM_SAVE_MODEL"] = model_path
    return subprocess.Popen(cmd, cwd=str(repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@app.post("/api/experiments/{exp_id}/model/cancel")
def cancel_model(exp_id: str, conn=Depends(db_conn)):
    model = conn.execute(
        "SELECT * FROM models WHERE experiment_id = ? AND status = 'creating' ORDER BY created_at DESC LIMIT 1",
        (exp_id,),
    ).fetchone()
    if not model:
        _error(404, "not_found", "no model currently being created")
    model_id = model["id"]

    proc = _model_processes.get(model_id)
    if proc:
        try:
            proc.kill()
        except OSError:
            pass
        _model_processes.pop(model_id, None)

    conn.execute(
        "UPDATE models SET status = 'cancelled', completed_at = ? WHERE id = ?",
        (_now(), model_id),
    )
    conn.commit()
    log.msg("server.model_cancelled", model_id=model_id)
    return {"status": "cancelled", "model_id": model_id}


@app.post("/api/experiments/{exp_id}/model/generate")
def generate_text(exp_id: str, body: dict, conn=Depends(db_conn)):
    global _loaded_model, _loaded_model_path
    model_row = conn.execute(
        "SELECT * FROM models WHERE experiment_id = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
        (exp_id,),
    ).fetchone()
    if not model_row:
        _error(400, "no_model", "no completed model available for inference")

    model_path = model_row["model_path"]
    if not model_path or not Path(model_path).exists():
        _error(400, "model_missing", "model file not found on disk")

    prompt_text = body.get("prompt", "")
    temperature = float(body.get("temperature", 0.8))
    max_tokens = int(body.get("max_tokens", 200))

    import torch as _torch

    if _loaded_model_path != model_path or _loaded_model is None:
        device = "cpu"
        _loaded_model = _torch.load(model_path, map_location=device, weights_only=False)
        _loaded_model.eval()
        _loaded_model_path = model_path
        log.msg("server.model_loaded", path=model_path)

    repo = REPO_PATH or Path.cwd()
    import sys
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from prepare import Tokenizer as _Tokenizer
    tokenizer = _Tokenizer.from_directory()

    tokens = tokenizer.encode(prompt_text)
    x = _torch.tensor([tokens], dtype=_torch.long)

    _loaded_model.eval()
    with _torch.no_grad():
        for _ in range(max_tokens):
            logits = _loaded_model(x)
            if hasattr(logits, "logits"):
                logits = logits.logits
            next_logits = logits[:, -1, :] / max(temperature, 1e-8)
            probs = _torch.softmax(next_logits, dim=-1)
            next_token = _torch.multinomial(probs, 1)
            x = _torch.cat([x, next_token], dim=1)

    generated_tokens = x[0].tolist()[len(tokens):]
    output_text = tokenizer.decode(generated_tokens)
    return {"text": output_text}


# ---------------------------------------------------------------------------
# UI Routes (Phase 2 Dashboard)
# ---------------------------------------------------------------------------
@app.get("/")
def ui_home(request: Request, conn=Depends(db_conn)):
    rows = conn.execute("SELECT * FROM experiments ORDER BY created_at DESC").fetchall()
    experiments = []
    running_id = None
    for r in rows:
        e = _row_to_dict(r)
        counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM trials WHERE experiment_id = ? GROUP BY status",
            (e["id"],),
        ).fetchall()
        e["trial_counts"] = {c["status"]: c["cnt"] for c in counts}
        e["elapsed"] = _experiment_elapsed(e)
        if e["status"] == "running":
            running_id = e["id"]
        experiments.append(e)

    workers = _derive_workers(conn)
    stats = _compute_home_stats(conn)
    return templates.TemplateResponse(request, "experiments.html", context={
        "experiments": experiments,
        "stats": stats,
        "running_id": running_id,
        "worker_count": sum(1 for w in workers if w["state"] != "offline"),
    })


@app.get("/experiments/{exp_id}")
def ui_experiment_detail(
    exp_id: str,
    request: Request,
    tab: str = Query("experiment"),
    filter: Optional[str] = Query(None),
    conn=Depends(db_conn),
):
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Experiment not found")
    exp = _row_to_dict(row)
    counts = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM trials WHERE experiment_id = ? GROUP BY status",
        (exp["id"],),
    ).fetchall()
    exp["trial_counts"] = {c["status"]: c["cnt"] for c in counts}

    trial_where = "WHERE t.experiment_id = ?"
    trial_params: list = [exp_id]
    if filter and filter in ("completed", "failed", "running", "queued"):
        trial_where += " AND t.status = ?"
        trial_params.append(filter)
    trial_total = conn.execute(
        f"SELECT COUNT(*) as cnt FROM trials t {trial_where}", trial_params
    ).fetchone()["cnt"]
    trial_per_page = 50
    trial_total_pages = max(1, (trial_total + trial_per_page - 1) // trial_per_page)
    trial_query = f"""SELECT t.*, w.display_name as worker_name
                     FROM trials t LEFT JOIN workers w ON t.worker_id = w.id
                     {trial_where}
                     ORDER BY t.trial_index DESC
                     LIMIT ? OFFSET ?"""
    trials = [_row_to_dict(r) for r in conn.execute(
        trial_query, trial_params + [trial_per_page, 0]
    ).fetchall()]

    workers = _derive_workers(conn)
    stale_trials = _get_stale_trials(conn)
    stats = _compute_experiment_stats(conn, exp)

    chart_trials = conn.execute(
        """SELECT trial_index, val_bpb, status FROM trials
           WHERE experiment_id = ? ORDER BY trial_index ASC""",
        (exp_id,),
    ).fetchall()
    chart_data = [
        {"index": r["trial_index"], "val_bpb": r["val_bpb"], "status": r["status"]}
        for r in chart_trials
    ]

    worker_counts_rows = conn.execute(
        """SELECT w.display_name, COUNT(t.id) as cnt
           FROM workers w LEFT JOIN trials t
             ON w.id = t.worker_id AND t.experiment_id = ? AND t.status = 'completed'
           GROUP BY w.id ORDER BY cnt DESC""",
        (exp_id,),
    ).fetchall()

    recent_rows = conn.execute(
        """SELECT t.*, w.display_name as worker_name
           FROM trials t LEFT JOIN workers w ON t.worker_id = w.id
           WHERE t.experiment_id = ? AND t.status IN ('completed', 'failed', 'running')
           ORDER BY COALESCE(t.completed_at, t.started_at) DESC LIMIT 15""",
        (exp_id,),
    ).fetchall()

    # Pin best trial to top of trials list (page 1 only)
    best_trial = None
    if exp.get("best_val_bpb") is not None:
        best_row = conn.execute(
            """SELECT t.*, w.display_name as worker_name
               FROM trials t LEFT JOIN workers w ON t.worker_id = w.id
               WHERE t.experiment_id = ? AND t.val_bpb = ? AND t.status = 'completed'
               ORDER BY t.completed_at DESC LIMIT 1""",
            (exp_id, exp["best_val_bpb"]),
        ).fetchone()
        if best_row:
            best_trial = _row_to_dict(best_row)
            best_trial["is_best"] = True
            trials = [t for t in trials if t["id"] != best_trial["id"]]

    # Model tab context
    model_info = None
    if tab == "model":
        model_info = _get_latest_model(conn, exp_id)
        if model_info:
            is_outdated = (
                exp.get("best_val_bpb") is not None
                and model_info.get("source_val_bpb") is not None
                and model_info["status"] == "completed"
                and exp["best_val_bpb"] < model_info["source_val_bpb"]
            )
            model_info["is_outdated"] = is_outdated
            model_info["file_size"] = _format_file_size(model_info.get("model_path"))

    return templates.TemplateResponse(request, "experiment_detail.html", context={
        "experiment": exp,
        "trials": trials,
        "best_trial": best_trial,
        "best_val_bpb": exp.get("best_val_bpb"),
        "workers": workers,
        "stats": stats,
        "tab": tab,
        "filter": filter,
        "stale_trials": stale_trials,
        "chart_data": json.dumps(chart_data),
        "worker_counts": json.dumps(
            [{"name": r["display_name"], "count": r["cnt"]} for r in worker_counts_rows]
        ),
        "recent_activity": [_row_to_dict(r) for r in recent_rows],
        "worker_count": sum(1 for w in workers if w["state"] != "offline"),
        "page": 1,
        "total_pages": trial_total_pages,
        "total": trial_total,
        "per_page": trial_per_page,
        "exp_id": exp_id,
        "trial_counts": exp.get("trial_counts", {}),
        "model": model_info,
    })


# ---------------------------------------------------------------------------
# HTMX Partials
# ---------------------------------------------------------------------------
@app.get("/partials/stat-boxes")
def partial_stat_boxes_home(request: Request, conn=Depends(db_conn)):
    stats = _compute_home_stats(conn)
    return templates.TemplateResponse(request, "partials/stat_boxes.html", context={
        "stats": stats,
    })


@app.get("/partials/stat-boxes/{exp_id}")
def partial_stat_boxes_exp(exp_id: str, request: Request, conn=Depends(db_conn)):
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404)
    exp = _row_to_dict(row)
    counts = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM trials WHERE experiment_id = ? GROUP BY status",
        (exp["id"],),
    ).fetchall()
    exp["trial_counts"] = {c["status"]: c["cnt"] for c in counts}
    stats = _compute_experiment_stats(conn, exp)
    return templates.TemplateResponse(request, "partials/stat_boxes.html", context={
        "stats": stats,
    })


@app.get("/partials/experiments-table")
def partial_experiments_table(request: Request, conn=Depends(db_conn)):
    rows = conn.execute("SELECT * FROM experiments ORDER BY created_at DESC").fetchall()
    experiments = []
    running_id = None
    for r in rows:
        e = _row_to_dict(r)
        counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM trials WHERE experiment_id = ? GROUP BY status",
            (e["id"],),
        ).fetchall()
        e["trial_counts"] = {c["status"]: c["cnt"] for c in counts}
        e["elapsed"] = _experiment_elapsed(e)
        if e["status"] == "running":
            running_id = e["id"]
        experiments.append(e)
    return templates.TemplateResponse(request, "partials/experiments_table.html", context={
        "experiments": experiments, "running_id": running_id,
    })


@app.get("/partials/trials-table/{exp_id}")
def partial_trials_table(
    exp_id: str,
    request: Request,
    filter: Optional[str] = Query(None),
    page: int = Query(1),
    per_page: int = Query(50),
    conn=Depends(db_conn),
):
    exp = conn.execute("SELECT best_val_bpb FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    best_val_bpb = exp["best_val_bpb"] if exp else None

    base_where = "WHERE t.experiment_id = ?"
    params_w: list = [exp_id]
    if filter and filter in ("completed", "failed", "running", "queued"):
        base_where += " AND t.status = ?"
        params_w.append(filter)

    total = conn.execute(
        f"SELECT COUNT(*) as cnt FROM trials t {base_where}", params_w
    ).fetchone()["cnt"]
    total_pages = max(1, (total + per_page - 1) // per_page)

    trial_query = f"""SELECT t.*, w.display_name as worker_name
                     FROM trials t LEFT JOIN workers w ON t.worker_id = w.id
                     {base_where}
                     ORDER BY t.trial_index DESC
                     LIMIT ? OFFSET ?"""
    params_q = params_w + [per_page, (page - 1) * per_page]
    trials = [_row_to_dict(r) for r in conn.execute(trial_query, params_q).fetchall()]

    # Pin best trial to top (page 1 only)
    best_trial = None
    if page == 1 and best_val_bpb is not None:
        best_row = conn.execute(
            """SELECT t.*, w.display_name as worker_name
               FROM trials t LEFT JOIN workers w ON t.worker_id = w.id
               WHERE t.experiment_id = ? AND t.val_bpb = ? AND t.status = 'completed'
               ORDER BY t.completed_at DESC LIMIT 1""",
            (exp_id, best_val_bpb),
        ).fetchone()
        if best_row:
            best_trial = _row_to_dict(best_row)
            best_trial["is_best"] = True
            trials = [t for t in trials if t["id"] != best_trial["id"]]

    return templates.TemplateResponse(request, "partials/trials_table.html", context={
        "trials": trials, "best_val_bpb": best_val_bpb,
        "best_trial": best_trial,
        "page": page, "total_pages": total_pages, "total": total,
        "per_page": per_page, "exp_id": exp_id, "filter": filter,
    })


@app.get("/partials/workers-table")
def partial_workers_table(
    request: Request,
    experiment_id: Optional[str] = Query(None),
    conn=Depends(db_conn),
):
    workers = _derive_workers(conn)
    return templates.TemplateResponse(request, "partials/workers_table.html", context={
        "workers": workers,
    })


@app.get("/partials/stats-card/{exp_id}")
def partial_stats_card(exp_id: str, request: Request, conn=Depends(db_conn)):
    counts = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM trials WHERE experiment_id = ? GROUP BY status",
        (exp_id,),
    ).fetchall()
    trial_counts = {c["status"]: c["cnt"] for c in counts}
    return templates.TemplateResponse(request, "partials/stats_card.html", context={
        "trial_counts": trial_counts,
    })


# ---------------------------------------------------------------------------
# UI Actions (form posts)
# ---------------------------------------------------------------------------
@app.post("/experiments/new")
async def ui_create_experiment(request: Request, conn=Depends(db_conn)):
    form = await request.form()
    exp_id = _short_uuid()
    name = form.get("name", "unnamed")
    now = _now()

    conn.execute(
        """INSERT INTO experiments (id, name, created_at, status, git_ref)
           VALUES (?, ?, ?, 'draft', 'main')""",
        (exp_id, name, now),
    )

    dataset_file = form.get("dataset")
    if dataset_file and hasattr(dataset_file, "filename") and dataset_file.filename:
        dest_dir = RUNS_DIR / "experiments" / exp_id / "dataset"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / dataset_file.filename
        content = await dataset_file.read()
        dest.write_bytes(content)
        conn.execute("UPDATE experiments SET dataset_uri = ? WHERE id = ?", (str(dest), exp_id))

    prompt_file = form.get("prompt")
    prompt_text = form.get("prompt_text", "")
    if prompt_file and hasattr(prompt_file, "filename") and prompt_file.filename:
        dest_dir = RUNS_DIR / "experiments" / exp_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "prompt.txt"
        content = await prompt_file.read()
        dest.write_bytes(content)
        conn.execute("UPDATE experiments SET prompt_uri = ? WHERE id = ?", (str(dest), exp_id))
    elif prompt_text:
        conn.execute(
            "UPDATE experiments SET program_prompt_inline = ?, prompt_uri = 'inline' WHERE id = ?",
            (prompt_text, exp_id),
        )

    conn.commit()
    log.msg("server.experiment_created", experiment_id=exp_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/experiments/{exp_id}/ui-action/{action}")
async def ui_experiment_action(exp_id: str, action: str, conn=Depends(db_conn)):
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Experiment not found")
    exp = _row_to_dict(row)

    if action == "start":
        if not exp.get("dataset_uri"):
            return RedirectResponse(url="/", status_code=303)
        running = conn.execute(
            "SELECT id FROM experiments WHERE status = 'running' AND id != ?",
            (exp_id,),
        ).fetchone()
        if running:
            return RedirectResponse(url="/", status_code=303)
        conn.execute(
            "UPDATE experiments SET status = 'running', stop_requested_at = NULL WHERE id = ?",
            (exp_id,),
        )
        conn.commit()
        log.msg("server.experiment_started", experiment_id=exp_id)
    elif action == "stop":
        conn.execute(
            "UPDATE experiments SET status = 'stopped', stop_requested_at = ? WHERE id = ?",
            (_now(), exp_id),
        )
        conn.commit()
        log.msg("server.experiment_stopped", experiment_id=exp_id)
        return RedirectResponse(url=f"/experiments/{exp_id}", status_code=303)
    elif action == "create-model":
        return await _ui_create_model_impl(exp_id, exp, conn)
    elif action == "cancel-model":
        return _ui_cancel_model_impl(exp_id, conn)
    elif action == "delete":
        if exp.get("status") == "running":
            return RedirectResponse(url="/", status_code=303)
        conn.execute("DELETE FROM models WHERE experiment_id = ?", (exp_id,))
        conn.execute("DELETE FROM trials WHERE experiment_id = ?", (exp_id,))
        conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
        conn.commit()
        exp_dir = RUNS_DIR / "experiments" / exp_id
        if exp_dir.exists():
            import shutil
            shutil.rmtree(exp_dir, ignore_errors=True)
        log.msg("server.experiment_deleted", experiment_id=exp_id)
        return RedirectResponse(url="/", status_code=303)

    return RedirectResponse(url=f"/experiments/{exp_id}", status_code=303)


async def _ui_create_model_impl(exp_id: str, exp: dict, conn):
    if not exp.get("best_commit"):
        return RedirectResponse(url=f"/experiments/{exp_id}?tab=model", status_code=303)

    existing = conn.execute(
        "SELECT id FROM models WHERE experiment_id = ? AND status = 'creating'",
        (exp_id,),
    ).fetchone()
    if existing:
        return RedirectResponse(url=f"/experiments/{exp_id}?tab=model", status_code=303)

    model_id = _short_uuid()
    now = _now()
    model_path = str(RUNS_DIR / "experiments" / exp_id / "model" / "model.pt")
    conn.execute(
        """INSERT INTO models (id, experiment_id, source_commit, source_val_bpb,
           status, model_path, created_at)
           VALUES (?, ?, ?, ?, 'creating', ?, ?)""",
        (model_id, exp_id, exp["best_commit"], exp["best_val_bpb"], model_path, now),
    )
    conn.commit()
    log.msg("server.model_create_started", model_id=model_id, experiment_id=exp_id,
            source_commit=exp["best_commit"])
    asyncio.create_task(_run_model_creation(model_id, exp_id, exp["best_commit"], model_path))
    return RedirectResponse(url=f"/experiments/{exp_id}?tab=model", status_code=303)


def _ui_cancel_model_impl(exp_id: str, conn):
    model = conn.execute(
        "SELECT * FROM models WHERE experiment_id = ? AND status = 'creating' ORDER BY created_at DESC LIMIT 1",
        (exp_id,),
    ).fetchone()
    if model:
        model_id = model["id"]
        proc = _model_processes.get(model_id)
        if proc:
            try:
                proc.kill()
            except OSError:
                pass
            _model_processes.pop(model_id, None)
        conn.execute(
            "UPDATE models SET status = 'cancelled', completed_at = ? WHERE id = ?",
            (_now(), model_id),
        )
        conn.commit()
        log.msg("server.model_cancelled", model_id=model_id)
    return RedirectResponse(url=f"/experiments/{exp_id}?tab=model", status_code=303)


@app.post("/experiments/{exp_id}/ui-action/generate")
async def ui_generate_text(exp_id: str, request: Request, conn=Depends(db_conn)):
    global _loaded_model, _loaded_model_path
    form = await request.form()
    prompt_text = form.get("prompt", "")
    temperature = float(form.get("temperature", "0.8"))
    max_tokens = int(form.get("max_tokens", "200"))

    model_row = conn.execute(
        "SELECT * FROM models WHERE experiment_id = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
        (exp_id,),
    ).fetchone()
    if not model_row:
        return JSONResponse(content="<div class='dim'>No completed model available.</div>",
                           media_type="text/html")

    model_path = model_row["model_path"]
    if not model_path or not Path(model_path).exists():
        return JSONResponse(content="<div class='dim'>Model file not found on disk.</div>",
                           media_type="text/html")

    try:
        import torch as _torch
        if _loaded_model_path != model_path or _loaded_model is None:
            _loaded_model = _torch.load(model_path, map_location="cpu", weights_only=False)
            _loaded_model.eval()
            _loaded_model_path = model_path

        repo = REPO_PATH or Path.cwd()
        import sys
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from prepare import Tokenizer as _Tokenizer
        tokenizer = _Tokenizer.from_directory()

        tokens = tokenizer.encode(prompt_text)
        x = _torch.tensor([tokens], dtype=_torch.long)
        _loaded_model.eval()
        with _torch.no_grad():
            for _ in range(max_tokens):
                logits = _loaded_model(x)
                if hasattr(logits, "logits"):
                    logits = logits.logits
                next_logits = logits[:, -1, :] / max(temperature, 1e-8)
                probs = _torch.softmax(next_logits, dim=-1)
                next_token = _torch.multinomial(probs, 1)
                x = _torch.cat([x, next_token], dim=1)

        generated_tokens = x[0].tolist()[len(tokens):]
        output_text = tokenizer.decode(generated_tokens)
        import html as _html
        safe_output = _html.escape(output_text)
        return JSONResponse(
            content=f'<pre style="white-space:pre-wrap;font-family:var(--mono);font-size:.8rem;color:var(--t1);padding:.75rem;background:var(--bg);border-radius:var(--rs);border:1px solid var(--brd);max-height:300px;overflow-y:auto">{safe_output}</pre>',
            media_type="text/html",
        )
    except Exception as e:
        import html as _html
        return JSONResponse(
            content=f'<div style="color:var(--red);font-size:.8rem">Error: {_html.escape(str(e))}</div>',
            media_type="text/html",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
cli = typer.Typer()


@cli.command()
def cli_main(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8765, "--port"),
    db: str = typer.Option("runs/swarm.db", "--db"),
    token: Optional[str] = typer.Option(None, "--token"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Path to git repo for agent to edit train.py"),
    agent_type: str = typer.Option("cursor", "--agent-type", help="Agent type: cursor, shell, none"),
):
    global DB_PATH, AUTH_TOKEN, RUNS_DIR, REPO_PATH, _agent_runner
    configure_logging()
    DB_PATH = db
    AUTH_TOKEN = token
    RUNS_DIR = Path(db).parent
    if repo:
        REPO_PATH = Path(repo).resolve()
    else:
        REPO_PATH = Path.cwd()
    try:
        _agent_runner = create_agent(agent_type)
        log.msg("server.agent_loaded", agent_type=agent_type, repo=str(REPO_PATH))
    except Exception as e:
        log.warning("server.agent_load_failed", error=str(e), agent_type=agent_type)
        _agent_runner = None
    log.msg("server.starting", host=host, port=port, db=db, agent_type=agent_type)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    cli()
