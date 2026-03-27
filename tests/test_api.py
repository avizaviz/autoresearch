"""Layer 2 integration tests for the swarm orchestrator API."""
from __future__ import annotations

import datetime
import os
import sys
import time
from pathlib import Path

from tests.conftest import (
    create_experiment,
    insert_queued_trial,
    register_worker,
    start_experiment,
    upload_and_start,
    upload_dataset,
    upload_prompt,
)


# ---------------------------------------------------------------
# Health
# ---------------------------------------------------------------
def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------
# Workers
# ---------------------------------------------------------------
def test_worker_register(client):
    data = register_worker(client, hostname="myhost")
    assert "worker_id" in data
    assert "display_name" in data
    assert data["display_name"].startswith("Worker-")


def test_worker_register_reconnect(client, tmp_env):
    first = register_worker(client, hostname="host1")
    second = register_worker(client, hostname="host2", worker_id=first["worker_id"])
    assert second["worker_id"] == first["worker_id"]
    assert second["display_name"] == first["display_name"]

    from swarm.db import get_db
    conn = get_db(tmp_env["db_path"])
    rows = conn.execute("SELECT * FROM workers").fetchall()
    conn.close()
    assert len(rows) == 1


# ---------------------------------------------------------------
# Experiment lifecycle
# ---------------------------------------------------------------
def test_experiment_lifecycle(client):
    exp = create_experiment(client, "lifecycle-test")
    assert exp["status"] == "draft"
    exp_id = exp["id"]

    upload_dataset(client, exp_id)
    upload_prompt(client, exp_id)

    started = start_experiment(client, exp_id)
    assert started["status"] == "running"

    stopped = client.post(f"/api/experiments/{exp_id}/stop").json()
    assert stopped["status"] == "stopped"

    resumed = start_experiment(client, exp_id)
    assert resumed["status"] == "running"


def test_start_conflict(client):
    exp1 = create_experiment(client, "exp1")
    exp2 = create_experiment(client, "exp2")
    upload_and_start(client, exp1["id"])

    upload_dataset(client, exp2["id"])
    upload_prompt(client, exp2["id"])
    resp = client.post(f"/api/experiments/{exp2['id']}/start")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "conflict"
    assert exp1["id"] in body["detail"]


def test_start_missing_dataset(client):
    exp = create_experiment(client, "no-data")
    resp = client.post(f"/api/experiments/{exp['id']}/start")
    assert resp.status_code == 400


# ---------------------------------------------------------------
# Claim + Complete
# ---------------------------------------------------------------
def test_claim_complete_cycle(client, tmp_env):
    worker = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    resp = client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["trial_id"] == trial_id

    resp = client.post(f"/api/trials/{trial_id}/complete", json={
        "exit_code": 0,
        "val_bpb": 1.23,
        "git_commit": "abc123",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"

    exp_resp = client.get(f"/api/experiments/{exp['id']}")
    assert exp_resp.json()["best_val_bpb"] == 1.23
    assert exp_resp.json()["best_commit"] == "abc123"


def test_claim_empty_queue(client, tmp_env):
    worker = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    resp = client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    assert resp.status_code == 204


def test_concurrent_claim(client, tmp_env):
    w1 = register_worker(client, hostname="h1")
    w2 = register_worker(client, hostname="h2")
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    r1 = client.post(f"/api/workers/{w1['worker_id']}/claim", json={})
    r2 = client.post(f"/api/workers/{w2['worker_id']}/claim", json={})

    assert r1.status_code == 200
    assert r2.status_code == 204


def test_best_commit_tracking(client, tmp_env):
    worker = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    t1 = insert_queued_trial(tmp_env, exp["id"], trial_index=0)
    client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    client.post(f"/api/trials/{t1}/complete", json={"exit_code": 0, "val_bpb": 1.0, "git_commit": "aaa"})

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["best_val_bpb"] == 1.0
    assert exp_data["best_commit"] == "aaa"

    t2 = insert_queued_trial(tmp_env, exp["id"], trial_index=1)
    client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    client.post(f"/api/trials/{t2}/complete", json={"exit_code": 0, "val_bpb": 0.9, "git_commit": "bbb"})

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["best_val_bpb"] == 0.9
    assert exp_data["best_commit"] == "bbb"

    t3 = insert_queued_trial(tmp_env, exp["id"], trial_index=2)
    client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    client.post(f"/api/trials/{t3}/complete", json={"exit_code": 0, "val_bpb": 0.95, "git_commit": "ccc"})

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["best_val_bpb"] == 0.9
    assert exp_data["best_commit"] == "bbb"


# ---------------------------------------------------------------
# AT-4: Failure handling
# ---------------------------------------------------------------
def test_failure_handling(client, tmp_env):
    """AT-4: failed trial stores stderr, leaves best_val_bpb untouched, worker returns to idle."""
    from swarm.db import get_db

    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    resp = client.post(f"/api/workers/{wid}/claim", json={})
    assert resp.status_code == 200
    assert resp.json()["trial_id"] == trial_id

    resp = client.post(f"/api/trials/{trial_id}/complete", json={
        "exit_code": 1,
        "val_bpb": None,
        "stderr_tail": "OOM killed",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"

    conn = get_db(tmp_env["db_path"])
    trial = dict(conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone())
    conn.close()
    assert trial["status"] == "failed"
    assert trial["stderr_tail"] == "OOM killed"

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["best_val_bpb"] is None

    resp = client.post(f"/api/workers/{wid}/claim", json={})
    assert resp.status_code == 204

    workers = client.get("/api/workers").json()
    w = next(w for w in workers if w["id"] == wid)
    assert w["state"] == "idle"


# ---------------------------------------------------------------
# AT-11: Worker state derivation
# ---------------------------------------------------------------
def test_worker_state_derivation(client, tmp_env):
    """AT-11: worker state transitions idle -> training -> idle -> offline."""
    from swarm.db import get_db

    worker = register_worker(client)
    wid = worker["worker_id"]

    workers = client.get("/api/workers").json()
    w = next(w for w in workers if w["id"] == wid)
    assert w["state"] == "idle"

    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    client.post(f"/api/workers/{wid}/claim", json={})
    workers = client.get("/api/workers").json()
    w = next(w for w in workers if w["id"] == wid)
    assert w["state"] == "training"

    client.post(f"/api/trials/{trial_id}/complete", json={
        "exit_code": 0, "val_bpb": 1.0, "git_commit": "abc",
    })
    workers = client.get("/api/workers").json()
    w = next(w for w in workers if w["id"] == wid)
    assert w["state"] == "idle"

    five_min_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
    ).isoformat()
    conn = get_db(tmp_env["db_path"])
    conn.execute("UPDATE workers SET last_seen_at = ? WHERE id = ?", (five_min_ago, wid))
    conn.commit()
    conn.close()

    workers = client.get("/api/workers").json()
    w = next(w for w in workers if w["id"] == wid)
    assert w["state"] == "offline"


# ---------------------------------------------------------------
# AT-12: Duration stats
# ---------------------------------------------------------------
def test_duration_stats(client, tmp_env):
    """AT-12: duration_seconds populated and per-worker trial counts correct."""
    from swarm.db import get_db

    w1 = register_worker(client, hostname="h1")
    w2 = register_worker(client, hostname="h2")
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    trial_ids = []
    for i in range(3):
        trial_ids.append(insert_queued_trial(tmp_env, exp["id"], trial_index=i))

    worker_assignments = [w1["worker_id"], w1["worker_id"], w2["worker_id"]]
    known_durations = [120.0, 180.0, 240.0]

    for i, (wid, dur) in enumerate(zip(worker_assignments, known_durations)):
        client.post(f"/api/workers/{wid}/claim", json={})
        client.post(f"/api/trials/{trial_ids[i]}/complete", json={
            "exit_code": 0, "val_bpb": 1.0 - i * 0.1, "git_commit": f"c{i}",
        })

    conn = get_db(tmp_env["db_path"])
    for i, dur in enumerate(known_durations):
        base = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
        started = base + datetime.timedelta(hours=i)
        completed = started + datetime.timedelta(seconds=dur)
        conn.execute(
            "UPDATE trials SET started_at = ?, completed_at = ?, duration_seconds = ? WHERE id = ?",
            (started.isoformat(), completed.isoformat(), dur, trial_ids[i]),
        )
    conn.commit()
    conn.close()

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["trial_counts"]["completed"] == 3

    trials = client.get(f"/api/experiments/{exp['id']}/trials").json()["trials"]
    assert len(trials) == 3
    for t in trials:
        assert t["duration_seconds"] is not None

    w1_trials = [t for t in trials if t["worker_id"] == w1["worker_id"]]
    w2_trials = [t for t in trials if t["worker_id"] == w2["worker_id"]]
    assert len(w1_trials) == 2
    assert len(w2_trials) == 1


# ---------------------------------------------------------------
# AT-17: Trials list with filters
# ---------------------------------------------------------------
def test_trials_list_with_filters(client, tmp_env):
    """AT-17: trials listing with status filters and trial_index ordering."""
    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    t0 = insert_queued_trial(tmp_env, exp["id"], trial_index=0)
    t1 = insert_queued_trial(tmp_env, exp["id"], trial_index=1)
    t2 = insert_queued_trial(tmp_env, exp["id"], trial_index=2)

    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t0}/complete", json={
        "exit_code": 0, "val_bpb": 1.0, "git_commit": "c0",
    })

    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t1}/complete", json={
        "exit_code": 1, "stderr_tail": "segfault",
    })

    all_trials = client.get(f"/api/experiments/{exp['id']}/trials").json()["trials"]
    assert len(all_trials) == 3
    indices = [t["trial_index"] for t in all_trials]
    assert indices == sorted(indices, reverse=True)

    completed = client.get(f"/api/experiments/{exp['id']}/trials?status=completed").json()["trials"]
    assert len(completed) == 1
    assert completed[0]["id"] == t0

    failed = client.get(f"/api/experiments/{exp['id']}/trials?status=failed").json()["trials"]
    assert len(failed) == 1
    assert failed[0]["id"] == t1


def test_trials_pagination(client, tmp_env):
    """GET /api/experiments/{id}/trials returns paginated envelope with correct slices."""
    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    for i in range(5):
        tid = insert_queued_trial(tmp_env, exp["id"], trial_index=i)
        client.post(f"/api/workers/{wid}/claim", json={})
        client.post(f"/api/trials/{tid}/complete", json={
            "exit_code": 0,
            "val_bpb": 1.0 - i * 0.01,
            "git_commit": f"c{i}",
        })

    exp_id = exp["id"]
    r1 = client.get(f"/api/experiments/{exp_id}/trials?per_page=2&page=1").json()
    assert len(r1["trials"]) == 2
    assert r1["total"] == 5
    assert r1["total_pages"] == 3
    assert r1["page"] == 1
    assert r1["per_page"] == 2

    r2 = client.get(f"/api/experiments/{exp_id}/trials?per_page=2&page=2").json()
    assert len(r2["trials"]) == 2
    ids_page1 = {t["id"] for t in r1["trials"]}
    ids_page2 = {t["id"] for t in r2["trials"]}
    assert ids_page1.isdisjoint(ids_page2)

    r3 = client.get(f"/api/experiments/{exp_id}/trials?per_page=2&page=3").json()
    assert len(r3["trials"]) == 1

    r4 = client.get(f"/api/experiments/{exp_id}/trials?per_page=2&page=4").json()
    assert len(r4["trials"]) == 0
    assert r4["total"] == 5


# ---------------------------------------------------------------
# AT-22: Heartbeat updates trial
# ---------------------------------------------------------------
def test_heartbeat_updates_trial(client, tmp_env):
    """AT-22: heartbeat with running_trial_id updates both worker and trial timestamps."""
    from swarm.db import get_db

    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    client.post(f"/api/workers/{wid}/claim", json={})

    conn = get_db(tmp_env["db_path"])
    w_before = dict(conn.execute("SELECT last_seen_at FROM workers WHERE id = ?", (wid,)).fetchone())
    t_before = dict(conn.execute("SELECT last_heartbeat_at FROM trials WHERE id = ?", (trial_id,)).fetchone())
    conn.close()

    time.sleep(0.05)

    resp = client.post(f"/api/workers/{wid}/heartbeat", json={"running_trial_id": trial_id})
    assert resp.status_code == 200

    conn = get_db(tmp_env["db_path"])
    w_after = dict(conn.execute("SELECT last_seen_at FROM workers WHERE id = ?", (wid,)).fetchone())
    t_after = dict(conn.execute("SELECT last_heartbeat_at FROM trials WHERE id = ?", (trial_id,)).fetchone())
    conn.close()

    assert w_after["last_seen_at"] > w_before["last_seen_at"]
    assert t_after["last_heartbeat_at"] > t_before["last_heartbeat_at"]

    hb_snapshot = t_after["last_heartbeat_at"]

    time.sleep(0.05)

    resp = client.post(f"/api/workers/{wid}/heartbeat", json={})
    assert resp.status_code == 200

    conn = get_db(tmp_env["db_path"])
    w_final = dict(conn.execute("SELECT last_seen_at FROM workers WHERE id = ?", (wid,)).fetchone())
    t_final = dict(conn.execute("SELECT last_heartbeat_at FROM trials WHERE id = ?", (trial_id,)).fetchone())
    conn.close()

    assert w_final["last_seen_at"] > w_after["last_seen_at"]
    assert t_final["last_heartbeat_at"] == hb_snapshot


# ---------------------------------------------------------------
# AT-24: Stuck trial count in experiment API
# ---------------------------------------------------------------
def test_stuck_trial_count_in_api(client, tmp_env):
    """AT-24: GET /api/experiments/{id} includes stuck_count for stale running trials."""
    from swarm.db import get_db

    worker = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    client.post(f"/api/workers/{worker['worker_id']}/claim", json={})

    long_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=45)
    ).isoformat()
    conn = get_db(tmp_env["db_path"])
    conn.execute("UPDATE trials SET last_heartbeat_at = ? WHERE id = ?", (long_ago, trial_id))
    conn.commit()
    conn.close()

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["stuck_count"] >= 1


# ---------------------------------------------------------------
# AT-30: Dataset replacement
# ---------------------------------------------------------------
def test_dataset_replacement(client, tmp_env):
    """AT-30: uploading a second dataset replaces the first on disk."""
    exp = create_experiment(client)

    data_a = upload_dataset(client, exp["id"], content=b"dataset-A-content")
    uri_a = data_a["dataset_uri"]
    assert uri_a
    assert Path(uri_a).read_bytes() == b"dataset-A-content"

    data_b = upload_dataset(client, exp["id"], content=b"dataset-B-content")
    uri_b = data_b["dataset_uri"]
    assert uri_b
    assert Path(uri_b).read_bytes() == b"dataset-B-content"


# ---------------------------------------------------------------
# AT-21: Refill creates trial when running
# ---------------------------------------------------------------
def test_refill_creates_trial_when_running(client, tmp_env):
    """AT-21: refill_once creates a queued trial for a running experiment, skips stopped."""
    from swarm.db import get_db
    from swarm.orchestrator import refill_once

    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    conn = get_db(tmp_env["db_path"])
    before = conn.execute(
        "SELECT COUNT(*) as cnt FROM trials WHERE experiment_id = ? AND status = 'queued'",
        (exp["id"],),
    ).fetchone()["cnt"]
    conn.close()
    assert before == 0

    refill_once(tmp_env["db_path"])

    conn = get_db(tmp_env["db_path"])
    after = conn.execute(
        "SELECT COUNT(*) as cnt FROM trials WHERE experiment_id = ? AND status = 'queued'",
        (exp["id"],),
    ).fetchone()["cnt"]
    conn.close()
    assert after >= 1

    client.post(f"/api/experiments/{exp['id']}/stop")

    refill_once(tmp_env["db_path"])

    conn = get_db(tmp_env["db_path"])
    after_stop = conn.execute(
        "SELECT COUNT(*) as cnt FROM trials WHERE experiment_id = ? AND status = 'queued'",
        (exp["id"],),
    ).fetchone()["cnt"]
    conn.close()
    assert after_stop == after


# ---------------------------------------------------------------
# SWARM_TRAIN_SCRIPT env var override
# ---------------------------------------------------------------
def test_swarm_train_script_override(tmp_path):
    """Worker _run_train should respect SWARM_TRAIN_SCRIPT env var."""
    from swarm.worker import _run_train

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)

    custom_script = tmp_path / "my_train.py"
    custom_script.write_text("print('val_bpb: 0.42')\n")

    os.environ["SWARM_TRAIN_SCRIPT"] = "my_train.py"
    try:
        exit_code, val_bpb, stderr_tail = _run_train(tmp_path)
        assert exit_code == 0
        assert val_bpb == 0.42
    finally:
        os.environ.pop("SWARM_TRAIN_SCRIPT", None)

    default_script = tmp_path / "train.py"
    default_script.write_text("print('val_bpb: 0.99')\n")

    exit_code, val_bpb, stderr_tail = _run_train(tmp_path)
    assert exit_code == 0
    assert val_bpb == 0.99


# ---------------------------------------------------------------
# create_experiment accepts git_ref from body
# ---------------------------------------------------------------
def test_create_experiment_with_git_ref(client):
    """create_experiment should accept git_ref from request body, not hardcode 'main'."""
    resp = client.post("/api/experiments", json={"name": "test", "git_ref": "feature/my-branch"})
    assert resp.status_code == 200
    exp = resp.json()
    exp_id = exp["id"]

    detail = client.get(f"/api/experiments/{exp_id}").json()
    assert detail["git_ref"] == "feature/my-branch"

    resp2 = client.post("/api/experiments", json={"name": "no-ref"})
    assert resp2.status_code == 200
    exp2 = resp2.json()

    detail2 = client.get(f"/api/experiments/{exp2['id']}").json()
    assert detail2["git_ref"] is None


# ---------------------------------------------------------------
# Worker handles train failure gracefully
# ---------------------------------------------------------------
def test_worker_graceful_train_failure(client, tmp_env):
    """Failed trial should be recorded; worker should stay available for the next claim."""
    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    t1 = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    resp = client.post(f"/api/workers/{wid}/claim", json={})
    assert resp.status_code == 200
    assert resp.json()["trial_id"] == t1

    resp = client.post(f"/api/trials/{t1}/complete", json={
        "exit_code": 1,
        "val_bpb": None,
        "stderr_tail": "ModuleNotFoundError: No module named 'nonexistent_module'",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"

    trial_detail = client.get(f"/api/experiments/{exp['id']}/trials?status=failed").json()["trials"]
    assert len(trial_detail) == 1
    assert "nonexistent_module" in trial_detail[0]["stderr_tail"]

    assert client.get(f"/api/experiments/{exp['id']}").json()["best_val_bpb"] is None

    t2 = insert_queued_trial(tmp_env, exp["id"], trial_index=1)
    resp = client.post(f"/api/workers/{wid}/claim", json={})
    assert resp.status_code == 200
    assert resp.json()["trial_id"] == t2

    resp = client.post(f"/api/trials/{t2}/complete", json={
        "exit_code": 0, "val_bpb": 1.5, "git_commit": "abc123",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"

    assert client.get(f"/api/experiments/{exp['id']}").json()["best_val_bpb"] == 1.5


# ---------------------------------------------------------------
# Experiment without git_ref → trial has git_ref=None
# ---------------------------------------------------------------
def test_experiment_without_git_ref_skips_checkout(client, tmp_env):
    """When experiment has no git_ref, claimed trial should have git_ref=None."""
    resp = client.post("/api/experiments", json={"name": "no-checkout"})
    assert resp.status_code == 200
    exp = resp.json()
    assert exp["git_ref"] is None

    upload_and_start(client, exp["id"])

    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0, git_ref=None)

    worker = register_worker(client)
    resp = client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["trial_id"] == trial_id
    assert spec["git_ref"] is None


# ---------------------------------------------------------------
# Mixed completed and failed trials — counts + best_val_bpb
# ---------------------------------------------------------------
def test_mixed_completed_and_failed_trials(client, tmp_env):
    """Dashboard should correctly count both completed and failed trials."""
    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    t0 = insert_queued_trial(tmp_env, exp["id"], trial_index=0)
    t1 = insert_queued_trial(tmp_env, exp["id"], trial_index=1)
    t2 = insert_queued_trial(tmp_env, exp["id"], trial_index=2)

    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t0}/complete", json={
        "exit_code": 0, "val_bpb": 1.5, "git_commit": "aaa",
    })

    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t1}/complete", json={
        "exit_code": 0, "val_bpb": 1.2, "git_commit": "bbb",
    })

    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t2}/complete", json={
        "exit_code": 1, "stderr_tail": "segfault in training loop",
    })

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["trial_counts"]["completed"] == 2
    assert exp_data["trial_counts"]["failed"] == 1

    all_trials = client.get(f"/api/experiments/{exp['id']}/trials").json()["trials"]
    assert len(all_trials) == 3
    statuses = {t["id"]: t["status"] for t in all_trials}
    assert statuses[t0] == "completed"
    assert statuses[t1] == "completed"
    assert statuses[t2] == "failed"

    assert exp_data["best_val_bpb"] == 1.2
    assert exp_data["best_commit"] == "bbb"


# ---------------------------------------------------------------
# Full experiment lifecycle: stop → resume → stop
# ---------------------------------------------------------------
def test_full_experiment_lifecycle_multi_stop_resume(client, tmp_env):
    """Full cycle: create → start → trial → stop → resume → trial → stop."""
    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])
    exp_id = exp["id"]

    t0 = insert_queued_trial(tmp_env, exp_id, trial_index=0)
    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t0}/complete", json={
        "exit_code": 0, "val_bpb": 2.0, "git_commit": "first",
    })

    exp_data = client.get(f"/api/experiments/{exp_id}").json()
    assert exp_data["best_val_bpb"] == 2.0
    assert exp_data["best_commit"] == "first"

    stopped = client.post(f"/api/experiments/{exp_id}/stop").json()
    assert stopped["status"] == "stopped"

    resumed = client.post(f"/api/experiments/{exp_id}/start").json()
    assert resumed["status"] == "running"

    exp_after_resume = client.get(f"/api/experiments/{exp_id}").json()
    assert exp_after_resume["best_val_bpb"] == 2.0
    assert exp_after_resume["best_commit"] == "first"

    t1 = insert_queued_trial(tmp_env, exp_id, trial_index=1)
    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t1}/complete", json={
        "exit_code": 0, "val_bpb": 1.5, "git_commit": "second",
    })

    exp_data = client.get(f"/api/experiments/{exp_id}").json()
    assert exp_data["best_val_bpb"] == 1.5
    assert exp_data["best_commit"] == "second"

    stopped2 = client.post(f"/api/experiments/{exp_id}/stop").json()
    assert stopped2["status"] == "stopped"

    final = client.get(f"/api/experiments/{exp_id}").json()
    assert final["status"] == "stopped"
    assert final["best_val_bpb"] == 1.5
    assert final["best_commit"] == "second"
    assert final["trial_counts"]["completed"] == 2


# ---------------------------------------------------------------
# Duration formatting unit test
# ---------------------------------------------------------------
def test_duration_formatting():
    from swarm.orchestrator import _format_duration

    assert _format_duration(None) == "--"
    assert _format_duration(0) == "0.0s"
    assert _format_duration(0.2) == "0.2s"
    assert _format_duration(0.62) == "0.6s"
    assert _format_duration(1.5) == "1.5s"
    assert _format_duration(45) == "45.0s"
    assert _format_duration(65) == "1m 05s"
    assert _format_duration(3661) == "1h 01m"


# ---------------------------------------------------------------
# Trial duration is positive and reasonable after completion
# ---------------------------------------------------------------
def test_trial_duration_is_positive_and_reasonable(client, tmp_env):
    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    client.post(f"/api/workers/{wid}/claim", json={})
    resp = client.post(f"/api/trials/{trial_id}/complete", json={
        "exit_code": 0, "val_bpb": 1.0, "git_commit": "dur1",
    })
    assert resp.status_code == 200

    from swarm.db import get_db
    conn = get_db(tmp_env["db_path"])
    row = dict(conn.execute("SELECT duration_seconds FROM trials WHERE id = ?", (trial_id,)).fetchone())
    conn.close()
    assert isinstance(row["duration_seconds"], float)
    assert row["duration_seconds"] > 0

    trials = client.get(f"/api/experiments/{exp['id']}/trials").json()["trials"]
    for t in trials:
        if t["status"] == "completed":
            assert t["duration_seconds"] is not None
            assert t["duration_seconds"] > 0


# ---------------------------------------------------------------
# Failed trials have stderr and no val_bpb
# ---------------------------------------------------------------
def test_failed_trials_have_stderr_and_no_val_bpb(client, tmp_env):
    from swarm.db import get_db

    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    t_good = insert_queued_trial(tmp_env, exp["id"], trial_index=0)
    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t_good}/complete", json={
        "exit_code": 0, "val_bpb": 0.8, "git_commit": "good1",
    })

    t_fail = insert_queued_trial(tmp_env, exp["id"], trial_index=1)
    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t_fail}/complete", json={
        "exit_code": 1,
        "val_bpb": None,
        "stderr_tail": "CUDA out of memory",
    })

    conn = get_db(tmp_env["db_path"])
    failed = dict(conn.execute("SELECT * FROM trials WHERE id = ?", (t_fail,)).fetchone())
    conn.close()

    assert failed["exit_code"] != 0
    assert failed["val_bpb"] is None
    assert failed["stderr_tail"] is not None and len(failed["stderr_tail"]) > 0
    assert failed["duration_seconds"] is not None and failed["duration_seconds"] > 0

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["best_val_bpb"] == 0.8
    assert exp_data["best_commit"] == "good1"


# ---------------------------------------------------------------
# Experiment trial counts accurate after mixed operations
# ---------------------------------------------------------------
def test_experiment_trial_counts_accurate(client, tmp_env):
    from swarm.db import get_db

    worker = register_worker(client)
    wid = worker["worker_id"]
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    t0 = insert_queued_trial(tmp_env, exp["id"], trial_index=0)
    t1 = insert_queued_trial(tmp_env, exp["id"], trial_index=1)
    t2 = insert_queued_trial(tmp_env, exp["id"], trial_index=2)

    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t0}/complete", json={
        "exit_code": 0, "val_bpb": 1.0, "git_commit": "c0",
    })

    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t1}/complete", json={
        "exit_code": 0, "val_bpb": 1.5, "git_commit": "c1",
    })

    client.post(f"/api/workers/{wid}/claim", json={})
    client.post(f"/api/trials/{t2}/complete", json={
        "exit_code": 1,
        "stderr_tail": "RuntimeError: training diverged",
    })

    exp_data = client.get(f"/api/experiments/{exp['id']}").json()
    assert exp_data["trial_counts"]["completed"] == 2
    assert exp_data["trial_counts"]["failed"] == 1

    conn = get_db(tmp_env["db_path"])
    total_in_db = conn.execute(
        "SELECT COUNT(*) as cnt FROM trials WHERE experiment_id = ?",
        (exp["id"],),
    ).fetchone()["cnt"]
    conn.close()
    counts = exp_data["trial_counts"]
    assert sum(counts.values()) == total_in_db


# ---------------------------------------------------------------
# API error responses have correct shape
# ---------------------------------------------------------------
def test_api_error_responses_have_correct_shape(client, tmp_env):
    resp = client.get("/api/experiments/nonexistent")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body and "detail" in body

    resp = client.post("/api/experiments/nonexistent/start")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body and "detail" in body

    exp = create_experiment(client, "no-dataset-exp")
    resp = client.post(f"/api/experiments/{exp['id']}/start")
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body and "detail" in body

    running_exp = create_experiment(client, "running-exp")
    upload_and_start(client, running_exp["id"])
    blocked_exp = create_experiment(client, "blocked-exp")
    upload_dataset(client, blocked_exp["id"])
    upload_prompt(client, blocked_exp["id"])
    resp = client.post(f"/api/experiments/{blocked_exp['id']}/start")
    assert resp.status_code == 409
    body = resp.json()
    assert "error" in body and "detail" in body
