"""Swarm worker process — claims and runs training trials.

Resilience design:
- Worker NEVER exits due to server unavailability.
- Register: retries forever (with backoff) until server responds.
- Claim: retries on error, sleeps on 204, keeps polling.
- Heartbeat: best-effort, errors are logged and ignored.
- Complete: retries 5x with backoff; on final failure, moves on to next trial.
- The worker's job is to ALWAYS stay alive and request the next SHA to process.
"""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import structlog
import typer

log = structlog.get_logger()

_shutdown = threading.Event()
_running_trial_id: Optional[str] = None
_running_trial_lock = threading.Lock()
_current_phase: str = ""
_training_pct: float = 0.0
_validation_pct: float = 0.0
_worker_repo_path: Optional[Path] = None


def _read_status_file(repo: Path):
    """Read .swarm_train_status.json written by train.py/prepare.py."""
    global _current_phase, _training_pct, _validation_pct
    status_file = repo / ".swarm_train_status.json"
    try:
        if status_file.exists():
            import json
            data = json.loads(status_file.read_text())
            phase = data.get("phase", "")
            pct = data.get("pct", 0)
            if phase == "warmup":
                _current_phase = "warmup"
                _training_pct = pct
                _validation_pct = 0.0
            elif phase == "training":
                _current_phase = "training"
                _training_pct = pct
                _validation_pct = 0.0
            elif phase == "validation":
                _current_phase = "validation"
                _training_pct = 100.0
                _validation_pct = pct
    except Exception:
        pass

COMPLETE_RETRIES = 5
COMPLETE_RETRY_BACKOFF = 5


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


def _headers(token: Optional[str]) -> dict:
    h: dict = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _register(client: httpx.Client, server: str, token: Optional[str],
              repo: Path) -> tuple[str, str]:
    """Register with the orchestrator. Retries FOREVER with exponential backoff."""
    id_file = repo / ".swarm_worker_id"
    payload: dict = {"hostname": socket.gethostname()}
    if id_file.exists():
        payload["worker_id"] = id_file.read_text().strip()

    backoff = 5
    max_backoff = 120
    attempt = 0

    while not _shutdown.is_set():
        try:
            resp = client.post(
                f"{server}/api/workers/register",
                json=payload,
                headers=_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
            worker_id = data["worker_id"]
            display_name = data["display_name"]
            id_file.write_text(worker_id)
            log.msg("worker.registered", worker_id=worker_id, display_name=display_name)
            return worker_id, display_name
        except Exception as e:
            attempt += 1
            log.warning("worker.register_retry", attempt=attempt, error=str(e),
                        next_retry_in=backoff)
            _shutdown.wait(timeout=backoff)
            backoff = min(backoff * 2, max_backoff)

    raise SystemExit(0)


def _heartbeat_loop(client: httpx.Client, server: str, token: Optional[str],
                     worker_id: str, interval: int):
    """Send heartbeat every interval. Errors are logged and ignored — never crashes."""
    while not _shutdown.is_set():
        try:
            with _running_trial_lock:
                tid = _running_trial_id
            if _worker_repo_path:
                _read_status_file(_worker_repo_path)
            payload: dict = {
                "current_phase": _current_phase,
                "training_pct": _training_pct,
                "validation_pct": _validation_pct,
            }
            if tid:
                payload["running_trial_id"] = tid
            client.post(
                f"{server}/api/workers/{worker_id}/heartbeat",
                json=payload,
                headers=_headers(token),
            )
            log.debug("worker.heartbeat", worker_id=worker_id, running_trial_id=tid,
                       phase=_current_phase, train_pct=_training_pct, val_pct=_validation_pct)
        except Exception as e:
            log.warning("worker.heartbeat_error", error=str(e))
        _shutdown.wait(timeout=interval)


def _git_fetch_checkout(repo: Path, git_ref: Optional[str],
                         git_commit: Optional[str]) -> str:
    """Fetch and checkout. Returns current HEAD sha."""
    try:
        subprocess.run(["git", "fetch", "origin"], cwd=str(repo), check=True,
                        capture_output=True, timeout=60)
    except Exception as e:
        log.warning("worker.git_fetch_failed", error=str(e))

    target = git_commit or git_ref
    if target:
        subprocess.run(["git", "checkout", target], cwd=str(repo), check=True,
                        capture_output=True, timeout=30)
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                             capture_output=True, text=True, check=True, timeout=10)
    return result.stdout.strip()


def _run_train(repo: Path) -> tuple[int, Optional[float], str]:
    """Run train.py. Returns (exit_code, val_bpb, stderr_tail).
    Progress is tracked via .swarm_train_status.json file (read by heartbeat).
    """
    timeout = int(os.environ.get("TRAIN_TIMEOUT", "1800"))
    venv_python = repo / ".venv" / "bin" / "python"
    train_script = os.environ.get("SWARM_TRAIN_SCRIPT", "train.py")
    cmd = [str(venv_python), train_script] if venv_python.exists() else ["python", train_script]

    proc = subprocess.Popen(
        cmd, cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    log.msg("worker.train_start", pid=proc.pid)

    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_bytes, stderr_bytes = proc.communicate()
        log.warning("worker.train_timeout", pid=proc.pid, timeout=timeout)

    exit_code = proc.returncode
    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    stderr_tail = stderr_text[-2000:]

    val_bpb = None
    for line in stdout_text.splitlines():
        m = re.match(r"^val_bpb:\s+([0-9.]+)", line)
        if m:
            val_bpb = float(m.group(1))

    # Clean up status file
    status_file = repo / ".swarm_train_status.json"
    try:
        status_file.unlink(missing_ok=True)
    except Exception:
        pass

    log.msg("worker.train_done", exit_code=exit_code, val_bpb=val_bpb)
    return exit_code, val_bpb, stderr_tail


def _complete_trial(client: httpx.Client, server: str, token: Optional[str],
                     trial_id: str, exit_code: int, val_bpb: Optional[float],
                     stderr_tail: str, git_commit: Optional[str],
                     worker_id: Optional[str] = None) -> bool:
    """Report trial completion. Retries COMPLETE_RETRIES times.
    Returns True if reported successfully, False if all retries failed."""
    payload: dict = {"exit_code": exit_code, "stderr_tail": stderr_tail}
    if val_bpb is not None:
        payload["val_bpb"] = val_bpb
    if git_commit:
        payload["git_commit"] = git_commit
    if worker_id:
        payload["worker_id"] = worker_id

    for attempt in range(COMPLETE_RETRIES):
        try:
            resp = client.post(
                f"{server}/api/trials/{trial_id}/complete",
                json=payload,
                headers=_headers(token),
            )
            resp.raise_for_status()
            log.msg("worker.complete", trial_id=trial_id, exit_code=exit_code,
                    val_bpb=val_bpb)
            return True
        except Exception as e:
            log.warning("worker.complete_retry", attempt=attempt + 1, error=str(e),
                        trial_id=trial_id)
            time.sleep(COMPLETE_RETRY_BACKOFF * (attempt + 1))

    log.error("worker.complete_failed_all_retries", trial_id=trial_id,
              retries=COMPLETE_RETRIES)
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
cli = typer.Typer()


@cli.command()
def main(
    server: str = typer.Option(..., "--server", help="Orchestrator URL"),
    token: Optional[str] = typer.Option(None, "--token"),
    repo: str = typer.Option(..., "--repo", help="Path to git repo with train.py"),
    heartbeat_interval: int = typer.Option(30, "--heartbeat-interval"),
    claim_interval: int = typer.Option(5, "--claim-interval"),
    once: bool = typer.Option(False, "--once", help="Run one trial then exit"),
):
    global _running_trial_id, _worker_repo_path
    configure_logging()
    repo_path = Path(repo).resolve()
    _worker_repo_path = repo_path

    client = httpx.Client(timeout=30)

    worker_id, display_name = _register(client, server, token, repo_path)

    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(client, server, token, worker_id, heartbeat_interval),
        daemon=True,
    )
    hb_thread.start()

    def _signal_handler(signum, frame):
        log.msg("worker.shutdown_requested", signal=signum)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    while not _shutdown.is_set():
        # --- Claim next trial ---
        try:
            resp = client.post(
                f"{server}/api/workers/{worker_id}/claim",
                json={},
                headers=_headers(token),
            )
        except Exception as e:
            log.warning("worker.claim_error", error=str(e))
            _shutdown.wait(timeout=claim_interval)
            continue

        if resp.status_code == 204:
            log.debug("worker.idle")
            _shutdown.wait(timeout=claim_interval)
            continue

        if resp.status_code != 200:
            log.warning("worker.claim_unexpected_status", status=resp.status_code)
            _shutdown.wait(timeout=claim_interval)
            continue

        spec = resp.json()
        trial_id = spec["trial_id"]
        with _running_trial_lock:
            _running_trial_id = trial_id
        log.msg("worker.claimed", trial_id=trial_id,
                experiment_id=spec.get("experiment_id"))

        # --- Git checkout ---
        git_commit = None
        try:
            log.msg("worker.checkout", git_ref=spec.get("git_ref"),
                    git_commit=spec.get("git_commit"))
            git_commit = _git_fetch_checkout(
                repo_path, spec.get("git_ref"), spec.get("git_commit"))
        except Exception as e:
            log.error("worker.checkout_failed", error=str(e))
            _complete_trial(client, server, token, trial_id, 1, None,
                           f"git checkout failed: {e}", None, worker_id)
            with _running_trial_lock:
                _running_trial_id = None
            continue

        # --- Train ---
        exit_code, val_bpb, stderr_tail = _run_train(repo_path)

        # --- Report results (retry with backoff; on total failure, move on) ---
        reported = _complete_trial(client, server, token, trial_id, exit_code,
                                    val_bpb, stderr_tail, git_commit, worker_id)
        if not reported:
            log.warning("worker.moving_on_after_complete_failure", trial_id=trial_id)

        with _running_trial_lock:
            _running_trial_id = None

        if once:
            log.msg("worker.once_mode_done", trial_id=trial_id, val_bpb=val_bpb)
            break

    log.msg("worker.shutdown_complete")
    client.close()


def cli_main():
    cli()


if __name__ == "__main__":
    cli()
