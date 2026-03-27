"""Full E2E loop tests — agent + orchestrator + worker integration.

These are very slow (agent produces commits, worker runs training).
Run with:  .venv/bin/python -m pytest tests/test_full_loop.py -v -m loop
Fast tests (not marked loop) can be run with:  -k "not loop"
"""
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

from swarm.agent import (
    AgentContext,
    ShellAgentRunner,
    _git_current_sha,
)
from swarm.db import get_db, init_db
from swarm.orchestrator import refill_once

import swarm.orchestrator as orch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")
TOY_DIR = PROJECT_ROOT / "tests" / "e2e" / "toy_next_number"


@pytest.fixture
def tmp_git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    (repo / "train.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo), capture_output=True, check=True)
    return repo


def _make_context(repo, **overrides):
    defaults = dict(
        repo_path=repo,
        experiment_prompt="",
        train_py_content=(repo / "train.py").read_text(),
        last_result=None,
        best_commit=None,
        best_val_bpb=None,
        history=[],
        trial_index=0,
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


# -------------------------------------------------------------------
# Fast tests (not marked loop)
# -------------------------------------------------------------------
def test_agent_produces_different_commits(tmp_git_repo):
    """ShellAgentRunner produces two DIFFERENT commit SHAs for sequential runs."""
    agent = ShellAgentRunner(command="echo '# edit-'$RANDOM >> train.py")

    ctx1 = _make_context(tmp_git_repo, trial_index=0)
    r1 = agent.run(ctx1)
    assert r1.success, f"First agent run failed: {r1.error}"
    sha1 = r1.new_commit_sha

    ctx2 = _make_context(tmp_git_repo, trial_index=1)
    r2 = agent.run(ctx2)
    assert r2.success, f"Second agent run failed: {r2.error}"
    sha2 = r2.new_commit_sha

    assert sha1 != sha2, "Agent produced identical commits for two runs"
    assert len(sha1) == 40
    assert len(sha2) == 40


def test_refill_with_agent_creates_new_sha(tmp_path, tmp_git_repo):
    """refill_once with a ShellAgentRunner creates a trial with a real git_commit."""
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    orch.DB_PATH = db_path
    orch.AUTH_TOKEN = None
    orch.RUNS_DIR = tmp_path
    orch.REPO_PATH = tmp_git_repo
    orch._shutdown_event = __import__("asyncio").Event()

    with TestClient(orch.app, raise_server_exceptions=False) as client:
        resp = client.post("/api/experiments", json={"name": "refill-test"})
        exp_id = resp.json()["id"]

        from tests.conftest import upload_and_start
        upload_and_start(client, exp_id)

        agent = ShellAgentRunner(command="echo '# refill-edit-'$RANDOM >> train.py")
        refill_once(db_path, agent=agent, repo_path=tmp_git_repo)

        conn = get_db(db_path)
        trial = conn.execute(
            "SELECT * FROM trials WHERE experiment_id = ? AND status = 'queued' ORDER BY trial_index",
            (exp_id,),
        ).fetchone()
        conn.close()

        assert trial is not None, "refill_once did not create a queued trial"
        assert trial["git_commit"] is not None, "Trial has no git_commit"
        assert len(trial["git_commit"]) == 40
        first_sha = trial["git_commit"]

        conn = get_db(db_path)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            "UPDATE trials SET status = 'completed', completed_at = ?, val_bpb = 1.0 WHERE id = ?",
            (now, trial["id"]),
        )
        conn.commit()
        conn.close()

        refill_once(db_path, agent=agent, repo_path=tmp_git_repo)

        conn = get_db(db_path)
        trial2 = conn.execute(
            "SELECT * FROM trials WHERE experiment_id = ? AND status = 'queued' ORDER BY trial_index DESC",
            (exp_id,),
        ).fetchone()
        conn.close()

        assert trial2 is not None, "Second refill did not create a trial"
        assert trial2["git_commit"] is not None
        assert trial2["git_commit"] != first_sha, "Second trial has same SHA as first"


# -------------------------------------------------------------------
# Very slow: full loop with real subprocess worker
# -------------------------------------------------------------------
def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _setup_toy_repo(tmpdir: Path) -> tuple[Path, str]:
    """Create origin + clone with toy train script. Returns (clone_path, head_sha)."""
    origin = tmpdir / "origin"
    origin.mkdir()
    subprocess.run(["git", "init"], cwd=str(origin), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(origin), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(origin), capture_output=True, check=True)
    shutil.copy(TOY_DIR / "train.py", origin / "train.py")
    subprocess.run(["git", "add", "."], cwd=str(origin), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(origin), capture_output=True, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(origin), capture_output=True, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(origin),
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    clone = tmpdir / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(clone), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), capture_output=True, check=True)

    venv_bin = clone / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    os.symlink(PYTHON, str(venv_bin / "python"))

    return clone, sha


@pytest.mark.loop
def test_full_loop_agent_edit_train_complete():
    """Full subprocess loop: orchestrator + worker + agent-produced commits."""
    tmpdir = Path(tempfile.mkdtemp(prefix="swarm_loop_"))
    orch_proc = worker_proc = None
    log_fhs: list = []
    success = False

    try:
        worker_repo, sha = _setup_toy_repo(tmpdir)

        agent = ShellAgentRunner(command="echo '# loop-edit-'$RANDOM >> train.py")
        agent_ctx = AgentContext(
            repo_path=worker_repo,
            experiment_prompt="",
            train_py_content=(worker_repo / "train.py").read_text(),
            last_result=None,
            best_commit=None,
            best_val_bpb=None,
            history=[],
            trial_index=0,
        )
        agent_result = agent.run(agent_ctx)
        assert agent_result.success, f"Agent failed: {agent_result.error}"
        agent_sha = agent_result.new_commit_sha

        subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            cwd=str(worker_repo), capture_output=True, check=True,
        )

        port = _find_free_port()
        db_path = str(tmpdir / "swarm.db")

        fh_oo = open(tmpdir / "orch_out.log", "w")
        fh_oe = open(tmpdir / "orch_err.log", "w")
        log_fhs += [fh_oo, fh_oe]

        orch_proc = subprocess.Popen(
            [PYTHON, "-m", "swarm.orchestrator",
             "--host", "127.0.0.1", "--port", str(port),
             "--db", db_path, "--agent-type", "none"],
            stdout=fh_oo, stderr=fh_oe, cwd=str(PROJECT_ROOT),
        )

        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{base_url}/health", timeout=2)
                if r.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.ReadError):
                pass
            time.sleep(0.5)
        else:
            raise TimeoutError("Orchestrator not healthy")

        client = httpx.Client(base_url=base_url, timeout=10)
        resp = client.post("/api/experiments", json={"name": "loop-test"})
        exp_id = resp.json()["id"]

        with open(TOY_DIR / "data.jsonl", "rb") as f:
            client.put(f"/api/experiments/{exp_id}/dataset",
                       files={"file": ("data.jsonl", f, "application/jsonl")})
        with open(TOY_DIR / "prompt.txt", "rb") as f:
            client.put(f"/api/experiments/{exp_id}/prompt",
                       files={"file": ("prompt.txt", f, "text/plain")})
        client.post(f"/api/experiments/{exp_id}/start")

        trial_id = uuid.uuid4().hex[:12]
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = get_db(db_path)
        conn.execute(
            """INSERT INTO trials (id, experiment_id, trial_index, status, git_ref, git_commit, created_at)
               VALUES (?, ?, 0, 'queued', 'main', ?, ?)""",
            (trial_id, exp_id, agent_sha, now),
        )
        conn.commit()
        conn.close()

        fh_wo = open(tmpdir / "worker_out.log", "w")
        fh_we = open(tmpdir / "worker_err.log", "w")
        log_fhs += [fh_wo, fh_we]

        worker_env = os.environ.copy()
        worker_env["SWARM_E2E_FAKE_TRAIN"] = "1"
        worker_env["TRAIN_TIMEOUT"] = "30"

        worker_proc = subprocess.Popen(
            [PYTHON, "-m", "swarm.worker",
             "--server", base_url,
             "--repo", str(worker_repo),
             "--heartbeat-interval", "5",
             "--claim-interval", "2"],
            stdout=fh_wo, stderr=fh_we, cwd=str(PROJECT_ROOT), env=worker_env,
        )

        poll_deadline = time.monotonic() + 60
        completed = None
        while time.monotonic() < poll_deadline:
            resp = client.get(f"/api/experiments/{exp_id}/trials")
            for t in resp.json()["trials"]:
                if t["id"] == trial_id and t["status"] == "completed":
                    completed = t
                    break
            if completed:
                break
            time.sleep(2)

        assert completed is not None, "Trial did not complete within 60s"
        assert isinstance(completed["val_bpb"], float)
        assert completed["val_bpb"] > 0
        assert completed.get("git_commit") is not None

        exp_data = client.get(f"/api/experiments/{exp_id}").json()
        assert exp_data["best_val_bpb"] is not None

        client.close()
        success = True

    finally:
        for proc in (worker_proc, orch_proc):
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        for fh in log_fhs:
            if fh and not fh.closed:
                fh.close()
        if success:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"\nLogs preserved at: {tmpdir}")
            for name in ("orch_out.log", "orch_err.log", "worker_out.log", "worker_err.log"):
                p = tmpdir / name
                if p.exists():
                    print(f"\n--- {name} ---\n{p.read_text()[-3000:]}")
