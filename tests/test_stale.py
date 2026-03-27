"""Layer 2 tests for stale-trial detection logic."""
from __future__ import annotations

import datetime

from swarm.db import get_db
from tests.conftest import (
    create_experiment,
    insert_queued_trial,
    register_worker,
    upload_and_start,
)


def _run_stale_detection(db_path: str):
    """One-shot replica of the stale-detection logic from orchestrator._stale_detection_loop."""
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=180)
    ).isoformat()
    conn = get_db(db_path)
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
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                conn.execute(
                    """UPDATE trials SET status = 'failed',
                       stderr_tail = 'worker_lost: max attempts exceeded',
                       completed_at = ?, attempt_count = ?
                       WHERE id = ?""",
                    (now, attempts, tid),
                )
            else:
                conn.execute(
                    """UPDATE trials SET status = 'queued',
                       worker_id = NULL, attempt_count = ?
                       WHERE id = ?""",
                    (attempts, tid),
                )
        conn.commit()
    finally:
        conn.close()


def _set_heartbeat_ago(db_path: str, trial_id: str, minutes: int):
    """Set a trial's last_heartbeat_at to N minutes in the past."""
    ts = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=minutes)
    ).isoformat()
    conn = get_db(db_path)
    conn.execute(
        "UPDATE trials SET last_heartbeat_at = ? WHERE id = ?", (ts, trial_id)
    )
    conn.commit()
    conn.close()


def _get_trial(db_path: str, trial_id: str) -> dict:
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


# -------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------
def test_stale_trial_requeued(client, tmp_env):
    """A running trial with stale heartbeat and attempt_count < 3 should be requeued."""
    db_path = tmp_env["db_path"]

    worker = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    # Claim the trial so it transitions to 'running'
    resp = client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    assert resp.status_code == 200
    assert resp.json()["trial_id"] == trial_id

    trial = _get_trial(db_path, trial_id)
    assert trial["status"] == "running"
    assert trial["worker_id"] == worker["worker_id"]

    _set_heartbeat_ago(db_path, trial_id, minutes=45)

    _run_stale_detection(db_path)

    trial = _get_trial(db_path, trial_id)
    assert trial["status"] == "queued"
    assert trial["worker_id"] is None
    assert trial["attempt_count"] == 1


def test_stale_trial_failed_after_max_attempts(client, tmp_env):
    """A running trial that has already been retried twice should be marked failed."""
    db_path = tmp_env["db_path"]

    worker = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)

    resp = client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    assert resp.status_code == 200

    # Simulate two prior requeue cycles: set attempt_count=2
    conn = get_db(db_path)
    conn.execute("UPDATE trials SET attempt_count = 2 WHERE id = ?", (trial_id,))
    conn.commit()
    conn.close()

    _set_heartbeat_ago(db_path, trial_id, minutes=45)

    _run_stale_detection(db_path)

    trial = _get_trial(db_path, trial_id)
    assert trial["status"] == "failed"
    assert trial["attempt_count"] == 3
    assert "max attempts" in trial["stderr_tail"]


def test_fresh_heartbeat_not_reaped(client, tmp_env):
    """A running trial with a recent heartbeat must NOT be touched by stale detection."""
    db_path = tmp_env["db_path"]

    worker = register_worker(client)
    exp = create_experiment(client)
    upload_and_start(client, exp["id"])

    trial_id = insert_queued_trial(tmp_env, exp["id"], trial_index=0)
    resp = client.post(f"/api/workers/{worker['worker_id']}/claim", json={})
    assert resp.status_code == 200

    # Heartbeat is fresh (just claimed → last_heartbeat_at ≈ now)
    _run_stale_detection(db_path)

    trial = _get_trial(db_path, trial_id)
    assert trial["status"] == "running", "Fresh trial should remain running"
    assert trial["worker_id"] == worker["worker_id"]
    assert trial["attempt_count"] == 0
