"""Tests for duplicate worker scenarios — stale requeue, late arrivals, conflicting results.

Covers the edge cases when:
- A trial is requeued (stale) and two workers end up working on it
- The original worker reports back after the trial was requeued
- One worker succeeds, another fails, in either order
- A completed trial should never be overwritten by a failure
"""
import datetime
import pytest
from tests.conftest import (
    register_worker, create_experiment, upload_and_start, insert_queued_trial,
)
from swarm.db import get_db


def _claim_trial(client, worker_id):
    resp = client.post(f"/api/workers/{worker_id}/claim", json={})
    return resp


def _complete_trial(client, trial_id, exit_code=0, val_bpb=None, worker_id=None):
    body = {"exit_code": exit_code}
    if val_bpb is not None:
        body["val_bpb"] = val_bpb
    if worker_id:
        body["worker_id"] = worker_id
    body["git_commit"] = "abc123"
    return client.post(f"/api/trials/{trial_id}/complete", json=body)


def test_completed_trial_not_overwritten_by_failure(client, tmp_env):
    """A trial that was completed successfully must NOT be overwritten by a late failure."""
    w = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    _claim_trial(client, w["worker_id"])

    resp = _complete_trial(client, trial_id, exit_code=0, val_bpb=0.95)
    assert resp.json()["status"] == "completed"

    resp = _complete_trial(client, trial_id, exit_code=1, val_bpb=None, worker_id="late-worker")
    data = resp.json()
    assert data["status"] == "completed"
    assert data.get("ignored") is True

    conn = get_db(tmp_env["db_path"])
    trial = conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone()
    conn.close()
    assert trial["status"] == "completed"
    assert trial["val_bpb"] == 0.95


def test_failed_trial_can_be_overridden_by_success(client, tmp_env):
    """A trial marked failed (e.g. by stale detection) CAN be overridden by a successful completion."""
    w = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    _claim_trial(client, w["worker_id"])

    conn = get_db(tmp_env["db_path"])
    conn.execute("UPDATE trials SET status = 'failed', stderr_tail = 'worker_lost' WHERE id = ?",
                 (trial_id,))
    conn.commit()
    conn.close()

    resp = _complete_trial(client, trial_id, exit_code=0, val_bpb=0.88)
    assert resp.json()["status"] == "completed"

    conn = get_db(tmp_env["db_path"])
    trial = conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone()
    conn.close()
    assert trial["status"] == "completed"
    assert trial["val_bpb"] == 0.88


def test_requeued_trial_accepts_late_completion(client, tmp_env):
    """A trial that was requeued (stale) should accept a late successful completion."""
    w = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    _claim_trial(client, w["worker_id"])

    conn = get_db(tmp_env["db_path"])
    conn.execute("UPDATE trials SET status = 'queued', worker_id = NULL WHERE id = ?",
                 (trial_id,))
    conn.commit()
    conn.close()

    resp = _complete_trial(client, trial_id, exit_code=0, val_bpb=0.92)
    assert resp.json()["status"] == "completed"

    conn = get_db(tmp_env["db_path"])
    trial = conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone()
    conn.close()
    assert trial["status"] == "completed"
    assert trial["val_bpb"] == 0.92


def test_duplicate_failure_ignored(client, tmp_env):
    """A second failure report on an already-failed trial is ignored."""
    w = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    _claim_trial(client, w["worker_id"])

    resp1 = _complete_trial(client, trial_id, exit_code=1, val_bpb=None)
    assert resp1.json()["status"] == "failed"

    resp2 = _complete_trial(client, trial_id, exit_code=1, val_bpb=None)
    assert resp2.json()["status"] == "failed"
    assert resp2.json().get("ignored") is True


def test_success_does_not_regress_best_val_bpb(client, tmp_env):
    """If experiment already has a better val_bpb, a worse completion doesn't downgrade it."""
    w = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    trial1 = insert_queued_trial(tmp_env, exp["id"], trial_index=0)
    _claim_trial(client, w["worker_id"])
    _complete_trial(client, trial1, exit_code=0, val_bpb=0.80)

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["best_val_bpb"] == 0.80

    trial2 = insert_queued_trial(tmp_env, exp["id"], trial_index=1)
    _claim_trial(client, w["worker_id"])
    _complete_trial(client, trial2, exit_code=0, val_bpb=0.95)

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["best_val_bpb"] == 0.80


def test_two_workers_heartbeat_same_trial_after_requeue(client, tmp_env):
    """After stale requeue, both old and new worker can heartbeat without error."""
    w1 = register_worker(client)
    w2 = client.post("/api/workers/register", json={"hostname": "worker2"}).json()

    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    _claim_trial(client, w1["worker_id"])

    conn = get_db(tmp_env["db_path"])
    conn.execute("UPDATE trials SET status = 'queued', worker_id = NULL WHERE id = ?",
                 (trial_id,))
    conn.commit()
    conn.close()

    _claim_trial(client, w2["worker_id"])

    resp1 = client.post(f"/api/workers/{w1['worker_id']}/heartbeat",
                        json={"running_trial_id": trial_id})
    resp2 = client.post(f"/api/workers/{w2['worker_id']}/heartbeat",
                        json={"running_trial_id": trial_id})
    assert resp1.status_code == 200
    assert resp2.status_code == 200


def test_stale_grace_configurable(client, tmp_env):
    """STALE_GRACE_SECONDS should be >= 1800 (30 min) for real training on MPS."""
    from swarm.orchestrator import STALE_GRACE_SECONDS
    assert STALE_GRACE_SECONDS >= 1800, f"STALE_GRACE_SECONDS={STALE_GRACE_SECONDS}, must be >= 1800 for MPS training"
