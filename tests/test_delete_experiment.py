"""Tests for DELETE experiment — full cleanup of trials, models, files."""
import os
import datetime
import pytest
from tests.conftest import (
    register_worker, create_experiment, upload_and_start, insert_queued_trial,
)
from swarm.db import get_db


def test_delete_experiment_removes_all_data(client, tmp_env):
    """DELETE removes experiment row, all trials, all models, and uploaded files."""
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    w = register_worker(client)
    client.post(f"/api/workers/{w['worker_id']}/claim", json={})
    client.post(f"/api/trials/{trial_id}/complete", json={
        "exit_code": 0, "val_bpb": 0.95, "git_commit": "abc123"
    })

    client.post(f"/api/experiments/{exp['id']}/stop")

    resp = client.delete(f"/api/experiments/{exp['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    conn = get_db(tmp_env["db_path"])
    assert conn.execute("SELECT COUNT(*) as c FROM experiments WHERE id = ?",
                        (exp["id"],)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) as c FROM trials WHERE experiment_id = ?",
                        (exp["id"],)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) as c FROM models WHERE experiment_id = ?",
                        (exp["id"],)).fetchone()["c"] == 0
    conn.close()


def test_delete_removes_uploaded_files(client, tmp_env):
    """Uploaded dataset and prompt files are removed from disk."""
    exp = create_experiment(client)
    exp_id = exp["id"]

    resp = client.put(f"/api/experiments/{exp_id}/dataset",
                      files={"file": ("data.jsonl", b"test data", "application/octet-stream")})
    assert resp.status_code == 200
    dataset_uri = resp.json()["dataset_uri"]

    resp = client.put(f"/api/experiments/{exp_id}/prompt",
                      content="test prompt", headers={"Content-Type": "text/plain"})

    assert os.path.exists(dataset_uri)

    resp = client.delete(f"/api/experiments/{exp_id}")
    assert resp.status_code == 200

    assert not os.path.exists(dataset_uri)


def test_cannot_delete_running_experiment(client, tmp_env):
    """Cannot delete an experiment that is currently running."""
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    resp = client.delete(f"/api/experiments/{exp['id']}")
    assert resp.status_code == 409

    exp_check = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_check["status"] == "running"

    client.post(f"/api/experiments/{exp['id']}/stop")


def test_delete_nonexistent_experiment(client, tmp_env):
    """Deleting a nonexistent experiment returns 404."""
    resp = client.delete("/api/experiments/nonexistent123")
    assert resp.status_code == 404


def test_delete_experiment_with_models(client, tmp_env):
    """Models associated with the experiment are also deleted."""
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    client.post(f"/api/experiments/{exp['id']}/stop")

    conn = get_db(tmp_env["db_path"])
    import uuid
    model_id = uuid.uuid4().hex[:12]
    conn.execute(
        """INSERT INTO models (id, experiment_id, source_commit, source_val_bpb,
           status, created_at) VALUES (?, ?, 'abc', 0.95, 'completed', ?)""",
        (model_id, exp["id"], datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    resp = client.delete(f"/api/experiments/{exp['id']}")
    assert resp.status_code == 200

    conn = get_db(tmp_env["db_path"])
    assert conn.execute("SELECT COUNT(*) as c FROM models WHERE experiment_id = ?",
                        (exp["id"],)).fetchone()["c"] == 0
    conn.close()


def test_experiments_list_empty_after_delete(client, tmp_env):
    """After deleting the only experiment, the list is empty."""
    exp = create_experiment(client)
    resp = client.delete(f"/api/experiments/{exp['id']}")
    assert resp.status_code == 200

    exps = client.get("/api/experiments").json()
    assert len(exps) == 0
