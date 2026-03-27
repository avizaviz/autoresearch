"""Shared fixtures for swarm API tests."""
from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import swarm.orchestrator as orch
from swarm.db import init_db


@pytest.fixture()
def tmp_env(tmp_path):
    """Create a temp DB + runs dir and patch orchestrator globals."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    orch.DB_PATH = db_path
    orch.AUTH_TOKEN = None
    orch.RUNS_DIR = tmp_path
    orch._shutdown_event = __import__("asyncio").Event()
    yield {"db_path": db_path, "tmp_path": tmp_path}


@pytest.fixture()
def client(tmp_env):
    """FastAPI TestClient with background tasks disabled (no lifespan)."""
    with TestClient(orch.app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def register_worker(client: TestClient, hostname: str = "testhost", worker_id: str | None = None) -> dict:
    payload: dict = {"hostname": hostname}
    if worker_id:
        payload["worker_id"] = worker_id
    resp = client.post("/api/workers/register", json=payload)
    assert resp.status_code == 200
    return resp.json()


def create_experiment(client: TestClient, name: str = "test-exp") -> dict:
    resp = client.post("/api/experiments", json={"name": name})
    assert resp.status_code == 200
    return resp.json()


def upload_dataset(client: TestClient, exp_id: str, content: bytes = b"fake,data\n1,2\n") -> dict:
    resp = client.put(
        f"/api/experiments/{exp_id}/dataset",
        files={"file": ("dataset.csv", io.BytesIO(content), "text/csv")},
    )
    assert resp.status_code == 200
    return resp.json()


def upload_prompt(client: TestClient, exp_id: str, text: str = "Train a model") -> dict:
    resp = client.put(
        f"/api/experiments/{exp_id}/prompt",
        content=text.encode(),
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == 200
    return resp.json()


def start_experiment(client: TestClient, exp_id: str) -> dict:
    resp = client.post(f"/api/experiments/{exp_id}/start")
    assert resp.status_code == 200
    return resp.json()


def upload_and_start(client: TestClient, exp_id: str) -> dict:
    upload_dataset(client, exp_id)
    upload_prompt(client, exp_id)
    return start_experiment(client, exp_id)


def insert_queued_trial(tmp_env: dict, exp_id: str, trial_index: int = 0, git_ref: str | None = "main") -> str:
    """Directly insert a queued trial into the DB. Returns trial_id."""
    import datetime
    import uuid
    from swarm.db import get_db
    trial_id = uuid.uuid4().hex[:12]
    conn = get_db(tmp_env["db_path"])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO trials (id, experiment_id, trial_index, status, git_ref, created_at)
           VALUES (?, ?, ?, 'queued', ?, ?)""",
        (trial_id, exp_id, trial_index, git_ref, now),
    )
    conn.commit()
    conn.close()
    return trial_id
