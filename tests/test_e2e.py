"""E2E golden-path test: orchestrator + worker subprocesses, full trial lifecycle."""
from __future__ import annotations

import datetime
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")
TOY_DIR = PROJECT_ROOT / "tests" / "e2e" / "toy_next_number"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = 10.0, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=2)
            if r.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadError):
            pass
        time.sleep(interval)
    raise TimeoutError(f"Orchestrator at {base_url} not healthy within {timeout}s")


def _setup_git_repos(tmpdir: Path) -> tuple[Path, str]:
    """Create an origin repo + working clone with the toy train script.

    Returns (worker_repo_path, head_sha).
    """
    origin = tmpdir / "origin"
    origin.mkdir()
    subprocess.run(["git", "init"], cwd=str(origin), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(origin), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(origin), check=True, capture_output=True,
    )

    shutil.copy(TOY_DIR / "train.py", origin / "train.py")
    subprocess.run(["git", "add", "."], cwd=str(origin), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init toy train"],
        cwd=str(origin), check=True, capture_output=True,
    )
    # Normalise branch name to 'main' regardless of git defaults
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=str(origin), check=True, capture_output=True,
    )

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(origin), capture_output=True, text=True, check=True,
    ).stdout.strip()

    repo = tmpdir / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo), check=True, capture_output=True,
    )

    # Symlink a python interpreter so the worker's _run_train finds .venv/bin/python
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    os.symlink(PYTHON, str(venv_bin / "python"))

    return repo, sha


def _insert_trial(db_path: str, exp_id: str, sha: str) -> str:
    """Insert a queued trial into the DB. Returns trial_id."""
    from swarm.db import get_db

    trial_id = uuid.uuid4().hex[:12]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = get_db(db_path)
    conn.execute(
        """INSERT INTO trials (id, experiment_id, trial_index, status,
                               git_ref, git_commit, created_at)
           VALUES (?, ?, 0, 'queued', 'main', ?, ?)""",
        (trial_id, exp_id, sha, now),
    )
    conn.commit()
    conn.close()
    return trial_id


def _dump_logs(tmpdir: Path):
    print("\n=== E2E TEST FAILED — log dump ===")
    for name in (
        "orch_stdout.log", "orch_stderr.log",
        "worker_stdout.log", "worker_stderr.log",
    ):
        p = tmpdir / name
        if p.exists():
            text = p.read_text()
            print(f"\n--- {name} (last 4000 chars) ---")
            print(text[-4000:])
    print(f"\nTemp dir preserved at: {tmpdir}")


@pytest.mark.e2e
def test_golden_path():
    tmpdir = Path(tempfile.mkdtemp(prefix="swarm_e2e_"))
    orch_proc = None
    worker_proc = None
    log_fhs: list = []
    success = False

    try:
        # ── 1-2: temp git repo ────────────────────────────────────────
        worker_repo, sha = _setup_git_repos(tmpdir)

        # ── 3: start orchestrator ─────────────────────────────────────
        port = _find_free_port()
        db_path = str(tmpdir / "swarm.db")

        fh_orch_out = open(tmpdir / "orch_stdout.log", "w")
        fh_orch_err = open(tmpdir / "orch_stderr.log", "w")
        log_fhs += [fh_orch_out, fh_orch_err]

        orch_proc = subprocess.Popen(
            [
                PYTHON, "-m", "swarm.orchestrator",
                "--host", "127.0.0.1",
                "--port", str(port),
                "--db", db_path,
            ],
            stdout=fh_orch_out,
            stderr=fh_orch_err,
            cwd=str(PROJECT_ROOT),
        )
        base_url = f"http://127.0.0.1:{port}"

        # ── 4: wait for health ────────────────────────────────────────
        _wait_for_health(base_url)

        client = httpx.Client(base_url=base_url, timeout=10)

        # ── 6: create experiment ──────────────────────────────────────
        resp = client.post("/api/experiments", json={"name": "e2e-golden"})
        assert resp.status_code == 200
        exp = resp.json()
        exp_id = exp["id"]
        assert exp["status"] == "draft"

        # ── 7: upload dataset + prompt ────────────────────────────────
        with open(TOY_DIR / "data.jsonl", "rb") as f:
            resp = client.put(
                f"/api/experiments/{exp_id}/dataset",
                files={"file": ("data.jsonl", f, "application/jsonl")},
            )
        assert resp.status_code == 200

        with open(TOY_DIR / "prompt.txt", "rb") as f:
            resp = client.put(
                f"/api/experiments/{exp_id}/prompt",
                files={"file": ("prompt.txt", f, "text/plain")},
            )
        assert resp.status_code == 200

        # ── 8: start experiment ───────────────────────────────────────
        resp = client.post(f"/api/experiments/{exp_id}/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

        # ── 9: insert a queued trial with the known SHA ───────────────
        trial_id = _insert_trial(db_path, exp_id, sha)

        # ── 5: start worker ──────────────────────────────────────────
        fh_wk_out = open(tmpdir / "worker_stdout.log", "w")
        fh_wk_err = open(tmpdir / "worker_stderr.log", "w")
        log_fhs += [fh_wk_out, fh_wk_err]

        worker_env = os.environ.copy()
        worker_env["SWARM_E2E_FAKE_TRAIN"] = "1"
        worker_env["TRAIN_TIMEOUT"] = "30"

        worker_proc = subprocess.Popen(
            [
                PYTHON, "-m", "swarm.worker",
                "--server", base_url,
                "--repo", str(worker_repo),
                "--heartbeat-interval", "5",
                "--claim-interval", "2",
            ],
            stdout=fh_wk_out,
            stderr=fh_wk_err,
            cwd=str(PROJECT_ROOT),
            env=worker_env,
        )

        # ── 10: poll until trial completed ────────────────────────────
        deadline = time.monotonic() + 60
        completed_trial = None
        while time.monotonic() < deadline:
            resp = client.get(f"/api/experiments/{exp_id}/trials")
            assert resp.status_code == 200
            for t in resp.json()["trials"]:
                if t["id"] == trial_id and t["status"] == "completed":
                    completed_trial = t
                    break
            if completed_trial:
                break
            time.sleep(2)

        assert completed_trial is not None, "No trial completed within 60s"

        # ── 11: assertions ────────────────────────────────────────────
        assert completed_trial["status"] == "completed"
        assert isinstance(completed_trial["val_bpb"], float)
        assert completed_trial["val_bpb"] > 0

        exp_data = client.get(f"/api/experiments/{exp_id}").json()
        assert isinstance(exp_data["best_val_bpb"], float)
        assert exp_data["best_val_bpb"] > 0

        # ── 12: stop experiment ───────────────────────────────────────
        resp = client.post(f"/api/experiments/{exp_id}/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

        # ── 13: verify stopped ────────────────────────────────────────
        exp_data = client.get(f"/api/experiments/{exp_id}").json()
        assert exp_data["status"] == "stopped"

        # ── 14: resume without re-upload ──────────────────────────────
        resp = client.post(f"/api/experiments/{exp_id}/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

        # ── 15: stop again ────────────────────────────────────────────
        resp = client.post(f"/api/experiments/{exp_id}/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

        client.close()
        success = True

    finally:
        # ── 16: cleanup ──────────────────────────────────────────────
        if worker_proc and worker_proc.poll() is None:
            worker_proc.send_signal(signal.SIGTERM)
            try:
                worker_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker_proc.kill()
                worker_proc.wait()

        if orch_proc and orch_proc.poll() is None:
            orch_proc.send_signal(signal.SIGTERM)
            try:
                orch_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                orch_proc.kill()
                orch_proc.wait()

        for fh in log_fhs:
            if fh and not fh.closed:
                fh.close()

        if success:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            _dump_logs(tmpdir)
