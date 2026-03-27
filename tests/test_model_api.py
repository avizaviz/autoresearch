"""CLI/API tests for model tab endpoints (GET/POST model, cancel, generate)."""
from __future__ import annotations

import datetime
import uuid

from swarm.db import get_db
from tests.conftest import (
    create_experiment,
    insert_queued_trial,
    register_worker,
    upload_and_start,
)


def _complete_trial_with_bpb(client, tmp_env, exp_id, val_bpb, trial_index=0, git_commit="abc123"):
    """Insert a queued trial, claim it, and complete it with a given val_bpb."""
    trial_id = insert_queued_trial(tmp_env, exp_id, trial_index=trial_index, git_ref="test")
    worker = register_worker(client)
    client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    client.post(f"/api/trials/{trial_id}/complete", json={
        "exit_code": 0, "val_bpb": val_bpb, "git_commit": git_commit,
    })
    return trial_id


def _set_best_commit(db_path, exp_id, commit="abc123", val_bpb=0.95):
    """Directly set best_commit/best_val_bpb on an experiment."""
    conn = get_db(db_path)
    conn.execute(
        "UPDATE experiments SET best_commit = ?, best_val_bpb = ? WHERE id = ?",
        (commit, val_bpb, exp_id),
    )
    conn.commit()
    conn.close()


def _insert_creating_model(db_path, exp_id, commit="abc123", val_bpb=0.95):
    """Directly insert a model row with status=creating. Returns model_id."""
    conn = get_db(db_path)
    model_id = uuid.uuid4().hex[:12]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO models (id, experiment_id, source_commit, source_val_bpb,
           status, model_path, created_at)
           VALUES (?, ?, ?, ?, 'creating', ?, ?)""",
        (model_id, exp_id, commit, val_bpb,
         f"runs/experiments/{exp_id}/model/model.pt", now),
    )
    conn.commit()
    conn.close()
    return model_id


# ---------------------------------------------------------------
# 1. GET /model on fresh experiment → {"status": "none"}
# ---------------------------------------------------------------
def test_get_model_none(client, tmp_env):
    exp = create_experiment(client)
    resp = client.get(f"/api/experiments/{exp['id']}/model")
    assert resp.status_code == 200
    assert resp.json()["status"] == "none"


# ---------------------------------------------------------------
# 2. POST /model/create with no best_commit → 400
# ---------------------------------------------------------------
def test_create_model_no_best_commit(client, tmp_env):
    """POST /model/create on a draft experiment with no best_commit returns 400."""
    exp = create_experiment(client)
    # Don't start — experiment stays draft with no best_commit
    resp = client.post(f"/api/experiments/{exp['id']}/model/create")
    assert resp.status_code == 400


# ---------------------------------------------------------------
# 3. POST /model/create starts creating
# ---------------------------------------------------------------
def test_create_model_starts_creating(client, tmp_env):
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    _complete_trial_with_bpb(client, tmp_env, exp["id"], val_bpb=0.95, git_commit="abc123")
    _set_best_commit(tmp_env["db_path"], exp["id"], commit="abc123", val_bpb=0.95)

    resp = client.post(f"/api/experiments/{exp['id']}/model/create")
    assert resp.status_code == 200
    model = resp.json()
    assert model["status"] == "creating"
    assert model["source_commit"] == "abc123"
    assert model["source_val_bpb"] == 0.95


# ---------------------------------------------------------------
# 4. GET /model returns model info after creation
# ---------------------------------------------------------------
def test_get_model_status(client, tmp_env):
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    _set_best_commit(tmp_env["db_path"], exp["id"], commit="def456", val_bpb=0.85)

    client.post(f"/api/experiments/{exp['id']}/model/create")

    resp = client.get(f"/api/experiments/{exp['id']}/model")
    assert resp.status_code == 200
    model = resp.json()
    assert model["source_commit"] == "def456"
    assert model["source_val_bpb"] == 0.85
    assert model["status"] == "creating"


# ---------------------------------------------------------------
# 5. POST /model/cancel → status cancelled
# ---------------------------------------------------------------
def test_cancel_model(client, tmp_env):
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    _set_best_commit(tmp_env["db_path"], exp["id"])
    _insert_creating_model(tmp_env["db_path"], exp["id"])

    resp = client.post(f"/api/experiments/{exp['id']}/model/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    model = client.get(f"/api/experiments/{exp['id']}/model").json()
    assert model["status"] == "cancelled"


# ---------------------------------------------------------------
# 6. POST /model/generate without completed model → 400
# ---------------------------------------------------------------
def test_generate_no_model(client, tmp_env):
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    resp = client.post(
        f"/api/experiments/{exp['id']}/model/generate",
        json={"prompt": "hello", "temperature": 0.8, "max_tokens": 10},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "no_model"


# ---------------------------------------------------------------
# 7. POST /model/create conflict → 409
# ---------------------------------------------------------------
def test_create_model_conflict(client, tmp_env):
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    _set_best_commit(tmp_env["db_path"], exp["id"])
    _insert_creating_model(tmp_env["db_path"], exp["id"])

    resp = client.post(f"/api/experiments/{exp['id']}/model/create")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "already_creating"


# ---------------------------------------------------------------
# 8. Model is_outdated when experiment improved
# ---------------------------------------------------------------
def test_model_is_outdated(client, tmp_env):
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    _set_best_commit(tmp_env["db_path"], exp["id"], commit="abc123", val_bpb=1.0)

    conn = get_db(tmp_env["db_path"])
    model_id = uuid.uuid4().hex[:12]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO models (id, experiment_id, source_commit, source_val_bpb,
           status, model_path, created_at, completed_at, duration_seconds)
           VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?)""",
        (model_id, exp["id"], "abc123", 1.0,
         f"runs/experiments/{exp['id']}/model/model.pt",
         now, now, 300.0),
    )
    conn.commit()
    conn.close()

    model = client.get(f"/api/experiments/{exp['id']}/model").json()
    assert model["is_outdated"] is False

    conn = get_db(tmp_env["db_path"])
    conn.execute(
        "UPDATE experiments SET best_val_bpb = ? WHERE id = ?",
        (0.5, exp["id"]),
    )
    conn.commit()
    conn.close()

    model = client.get(f"/api/experiments/{exp['id']}/model").json()
    assert model["is_outdated"] is True


# ---------------------------------------------------------------
# 9. Best trial pinned in trials list
# ---------------------------------------------------------------
def test_best_trial_pinned_in_trials_list(client, tmp_env):
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    _complete_trial_with_bpb(client, tmp_env, exp["id"], val_bpb=1.5, trial_index=0, git_commit="c0")
    _complete_trial_with_bpb(client, tmp_env, exp["id"], val_bpb=0.8, trial_index=1, git_commit="c1")
    _complete_trial_with_bpb(client, tmp_env, exp["id"], val_bpb=1.2, trial_index=2, git_commit="c2")

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["best_val_bpb"] == 0.8
    assert exp_data["best_commit"] == "c1"

    trials_resp = client.get(f"/api/experiments/{exp['id']}/trials").json()
    trials = trials_resp["trials"]
    assert len(trials) == 3
    best_trials = [t for t in trials if t["val_bpb"] == 0.8]
    assert len(best_trials) == 1
    assert best_trials[0]["git_commit"] == "c1"
