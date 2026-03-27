"""Playwright UI tests (Flow B) — browser-based E2E against live orchestrator.

Requires: pytest-playwright, chromium installed.
Run with: .venv/bin/python -m pytest tests/test_ui_playwright.py -v --headed  (to watch)
          .venv/bin/python -m pytest tests/test_ui_playwright.py -v           (headless)
"""
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.ui

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")
TOY_TRAIN = str(PROJECT_ROOT / "tests" / "e2e" / "toy_next_number" / "train.py")
TOY_DATA = str(PROJECT_ROOT / "tests" / "e2e" / "toy_next_number" / "data.jsonl")
TOY_PROMPT = str(PROJECT_ROOT / "tests" / "e2e" / "toy_next_number" / "prompt.txt")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(url, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def server():
    """Start orchestrator + worker as subprocesses for the test module."""
    port = _free_port()
    tmpdir = tempfile.mkdtemp(prefix="swarm_ui_")
    db_path = os.path.join(tmpdir, "swarm.db")
    base_url = f"http://127.0.0.1:{port}"

    git_repo = os.path.join(tmpdir, "repo")
    os.makedirs(git_repo)
    subprocess.run(["git", "init"], cwd=git_repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=git_repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo, capture_output=True, check=True)

    import shutil
    shutil.copy(TOY_TRAIN, os.path.join(git_repo, "train.py"))
    subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial toy train"], cwd=git_repo, capture_output=True, check=True)

    orch_log = open(os.path.join(tmpdir, "orch.log"), "w")
    orch = subprocess.Popen(
        [VENV_PYTHON, "-m", "swarm.orchestrator",
         "--host", "127.0.0.1", "--port", str(port),
         "--db", db_path, "--repo", git_repo, "--agent-type", "none"],
        stdout=orch_log, stderr=subprocess.STDOUT,
    )

    assert _wait_healthy(base_url), f"Orchestrator failed to start on {base_url}"

    worker_log = open(os.path.join(tmpdir, "worker.log"), "w")
    worker_env = os.environ.copy()
    worker_env["SWARM_E2E_FAKE_TRAIN"] = "1"
    worker_env["SWARM_TRAIN_SCRIPT"] = "train.py"
    worker = subprocess.Popen(
        [VENV_PYTHON, "-m", "swarm.worker",
         "--server", base_url, "--repo", git_repo, "--claim-interval", "2"],
        stdout=worker_log, stderr=subprocess.STDOUT,
        env=worker_env,
    )
    time.sleep(2)

    yield {
        "base_url": base_url,
        "port": port,
        "db_path": db_path,
        "tmpdir": tmpdir,
        "git_repo": git_repo,
        "orch_pid": orch.pid,
        "worker_pid": worker.pid,
    }

    worker.terminate()
    orch.terminate()
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        worker.kill()
    try:
        orch.wait(timeout=5)
    except subprocess.TimeoutExpired:
        orch.kill()
    orch_log.close()
    worker_log.close()


@pytest.fixture(scope="module")
def server_url(server):
    return server["base_url"]


def test_home_page_loads(page: Page, server_url):
    """AT-UI-5 partial: home page renders without errors."""
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

    page.goto(server_url)
    expect(page.locator(".brand")).to_contain_text("autoresearch swarm")
    expect(page.locator(".banner")).to_contain_text("One experiment runs at a time")
    expect(page.locator("h2")).to_contain_text("All Experiments")
    assert len(errors) == 0, f"Console errors: {errors}"


def test_create_experiment_via_ui(page: Page, server_url):
    """AT-UI-3 partial: create experiment through the form."""
    page.goto(server_url)

    page.click("text=+ New Experiment")
    page.fill("#name", "ui-created-exp")
    page.set_input_files("#dataset", TOY_DATA)
    page.set_input_files("#prompt", TOY_PROMPT)
    page.click("text=Create Experiment")

    page.wait_for_load_state("networkidle", timeout=15000)
    expect(page.locator("body")).to_contain_text("ui-created-exp")


def test_experiment_detail_shows_data(page: Page, server_url, server):
    """AT-UI-2 + AT-17: detail page shows experiment info matching API."""
    client = httpx.Client(base_url=server_url)
    exp = client.post("/api/experiments", json={"name": "detail-test"}).json()
    exp_id = exp["id"]
    client.put(f"/api/experiments/{exp_id}/prompt",
               content="test prompt", headers={"Content-Type": "text/plain"})
    with open(TOY_DATA, "rb") as f:
        client.put(f"/api/experiments/{exp_id}/dataset",
                   files={"file": ("data.jsonl", f)})

    page.goto(f"{server_url}/experiments/{exp_id}")
    expect(page.locator(".nav-row")).to_contain_text("detail-test")
    expect(page.locator(".ei-v").first).to_contain_text("data.jsonl")


def test_start_stop_resume_flow(page: Page, server_url, server):
    """AT-UI-4 + AT-UI-7: Start/Stop/Resume via UI buttons."""
    client = httpx.Client(base_url=server_url)
    exp = client.post("/api/experiments", json={"name": "startstop-test"}).json()
    exp_id = exp["id"]
    client.put(f"/api/experiments/{exp_id}/prompt",
               content="test", headers={"Content-Type": "text/plain"})
    with open(TOY_DATA, "rb") as f:
        client.put(f"/api/experiments/{exp_id}/dataset",
                   files={"file": ("data.jsonl", f)})

    page.goto(f"{server_url}/experiments/{exp_id}")

    page.click("button:has-text('Start')")
    page.wait_for_load_state("networkidle")
    expect(page.locator(".badge").first).to_contain_text("Running")

    page.click("button:has-text('Stop')")
    page.wait_for_load_state("networkidle")
    expect(page.locator(".badge").first).to_contain_text("Stopped")

    page.click("button:has-text('Resume')")
    page.wait_for_load_state("networkidle")
    expect(page.locator(".badge").first).to_contain_text("Running")

    page.click("button:has-text('Stop')")
    page.wait_for_load_state("networkidle")


def test_trial_completes_in_ui(page: Page, server_url, server):
    """AT-UI-6 partial + AT-26 Playwright: trial completion visible in UI."""
    client = httpx.Client(base_url=server_url)

    running = client.get("/api/experiments?status=running").json()
    for e in running:
        if e["status"] == "running":
            client.post(f"/api/experiments/{e['id']}/stop")

    exp = client.post("/api/experiments", json={"name": "trial-complete-test"}).json()
    exp_id = exp["id"]
    client.put(f"/api/experiments/{exp_id}/prompt",
               content="test", headers={"Content-Type": "text/plain"})
    with open(TOY_DATA, "rb") as f:
        client.put(f"/api/experiments/{exp_id}/dataset",
                   files={"file": ("data.jsonl", f)})
    client.post(f"/api/experiments/{exp_id}/start")

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=server["git_repo"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    from swarm.db import get_db
    import uuid, datetime
    conn = get_db(server["db_path"])
    trial_id = uuid.uuid4().hex[:12]
    conn.execute(
        """INSERT INTO trials (id, experiment_id, trial_index, status, git_commit, created_at)
           VALUES (?, ?, 0, 'queued', ?, ?)""",
        (trial_id, exp_id, sha, datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    page.goto(f"{server_url}/experiments/{exp_id}?tab=experiment")

    deadline = time.monotonic() + 45
    found = False
    while time.monotonic() < deadline:
        page.reload()
        page.wait_for_load_state("networkidle", timeout=5000)
        if "Completed" in page.content() or "completed" in page.content().lower():
            found = True
            break
        time.sleep(3)

    assert found, "No completed trial appeared in UI within 45s"

    client.post(f"/api/experiments/{exp_id}/stop")


def test_pagination_in_ui(page: Page, server_url, server):
    """Trials table pagination: Prev/Next buttons, page counts."""
    client = httpx.Client(base_url=server_url)

    running = client.get("/api/experiments?status=running").json()
    for e in running:
        if e["status"] == "running":
            client.post(f"/api/experiments/{e['id']}/stop")

    exp = client.post("/api/experiments", json={"name": "pagination-test"}).json()
    exp_id = exp["id"]
    client.put(f"/api/experiments/{exp_id}/prompt",
               content="test", headers={"Content-Type": "text/plain"})
    with open(TOY_DATA, "rb") as f:
        client.put(f"/api/experiments/{exp_id}/dataset",
                   files={"file": ("data.jsonl", f)})

    import uuid, datetime
    from swarm.db import get_db
    conn = get_db(server["db_path"])
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=server["git_repo"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    for i in range(5):
        tid = uuid.uuid4().hex[:12]
        conn.execute(
            """INSERT INTO trials (id, experiment_id, trial_index, status,
               git_commit, val_bpb, exit_code, created_at, completed_at, duration_seconds)
               VALUES (?, ?, ?, 'completed', ?, ?, 0, ?, ?, ?)""",
            (tid, exp_id, i, sha, 1.0 - i * 0.1, 
             datetime.datetime.now(datetime.timezone.utc).isoformat(),
             datetime.datetime.now(datetime.timezone.utc).isoformat(),
             300.0 + i),
        )
    conn.commit()
    conn.close()

    page.goto(f"{server_url}/experiments/{exp_id}?tab=experiment")

    resp = client.get(f"/api/experiments/{exp_id}/trials?per_page=2&page=1").json()
    assert resp["total"] == 5
    assert resp["total_pages"] == 3
    assert len(resp["trials"]) == 2

    expect(page.locator("tbody")).to_contain_text("Completed")

    page.goto(f"{server_url}/partials/trials-table/{exp_id}?per_page=2&page=1")
    body_text = page.content()
    assert "page 1 of 3" in body_text.lower() or "Showing 2 of 5" in body_text


def test_workers_tab_visible(page: Page, server_url, server):
    """AT-11 UI: Workers tab shows worker state."""
    client = httpx.Client(base_url=server_url)
    exps = client.get("/api/experiments").json()
    if exps:
        exp_id = exps[0]["id"]
    else:
        exp = client.post("/api/experiments", json={"name": "workers-tab-test"}).json()
        exp_id = exp["id"]
        client.put(f"/api/experiments/{exp_id}/prompt",
                   content="test", headers={"Content-Type": "text/plain"})
        with open(TOY_DATA, "rb") as f:
            client.put(f"/api/experiments/{exp_id}/dataset",
                       files={"file": ("data.jsonl", f)})

    page.goto(f"{server_url}/experiments/{exp_id}?tab=workers")
    expect(page.locator("h2").first).to_contain_text("Workers")

    workers_api = client.get("/api/workers").json()
    if workers_api:
        expect(page.locator("tbody")).to_contain_text(workers_api[0]["display_name"])


def test_no_console_errors_on_golden_path(page: Page, server_url, server):
    """AT-UI-1: No unhandled JS console errors on main pages."""
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

    page.goto(server_url)
    page.wait_for_load_state("networkidle")

    exps = httpx.get(f"{server_url}/api/experiments").json()
    if exps:
        page.goto(f"{server_url}/experiments/{exps[0]['id']}?tab=experiment")
        page.wait_for_load_state("networkidle")
        page.goto(f"{server_url}/experiments/{exps[0]['id']}?tab=workers")
        page.wait_for_load_state("networkidle")

    page.goto(server_url)
    page.wait_for_load_state("networkidle")

    assert len(errors) == 0, f"Console errors found: {errors}"


def test_refresh_preserves_experiment(page: Page, server_url, server):
    """Persistence: refresh doesn't lose experiment data."""
    client = httpx.Client(base_url=server_url)
    exp = client.post("/api/experiments", json={"name": "refresh-test"}).json()
    exp_id = exp["id"]
    client.put(f"/api/experiments/{exp_id}/prompt",
               content="persistence check", headers={"Content-Type": "text/plain"})
    with open(TOY_DATA, "rb") as f:
        client.put(f"/api/experiments/{exp_id}/dataset",
                   files={"file": ("data.jsonl", f)})

    page.goto(f"{server_url}/experiments/{exp_id}")
    expect(page.locator(".nav-row")).to_contain_text("refresh-test")

    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator(".nav-row")).to_contain_text("refresh-test")
    expect(page.locator(".ei-v").first).to_contain_text("data.jsonl")


# ---------------------------------------------------------------
# Model tab helpers
# ---------------------------------------------------------------
def _insert_completed_model(db_path, exp_id, commit="abc123", val_bpb=0.95):
    import uuid, datetime
    from swarm.db import get_db
    conn = get_db(db_path)
    model_id = uuid.uuid4().hex[:12]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO models (id, experiment_id, source_commit, source_val_bpb,
           status, model_path, created_at, completed_at, duration_seconds)
           VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?)""",
        (model_id, exp_id, commit, val_bpb,
         f"runs/experiments/{exp_id}/model/model.pt",
         now, now, 300.0),
    )
    conn.commit()
    conn.close()
    return model_id


# ---------------------------------------------------------------
# Model tab: no model state
# ---------------------------------------------------------------
def test_model_tab_no_model_state(page: Page, server_url, server):
    """Model tab shows 'No model created yet' for a fresh experiment."""
    client = httpx.Client(base_url=server_url)
    exp = client.post("/api/experiments", json={"name": "model-none-test"}).json()
    exp_id = exp["id"]
    client.put(f"/api/experiments/{exp_id}/prompt",
               content="test", headers={"Content-Type": "text/plain"})
    with open(TOY_DATA, "rb") as f:
        client.put(f"/api/experiments/{exp_id}/dataset",
                   files={"file": ("data.jsonl", f)})

    page.goto(f"{server_url}/experiments/{exp_id}?tab=model")
    page.wait_for_load_state("networkidle")

    expect(page.locator("body")).to_contain_text("No model created yet")
    expect(page.locator("body")).to_contain_text("No completed trials yet")


# ---------------------------------------------------------------
# Model tab: completed model shows info card
# ---------------------------------------------------------------
def test_model_tab_after_create(page: Page, server_url, server):
    """Model tab shows model info card when a completed model exists."""
    client = httpx.Client(base_url=server_url)
    exp = client.post("/api/experiments", json={"name": "model-done-test"}).json()
    exp_id = exp["id"]
    client.put(f"/api/experiments/{exp_id}/prompt",
               content="test", headers={"Content-Type": "text/plain"})
    with open(TOY_DATA, "rb") as f:
        client.put(f"/api/experiments/{exp_id}/dataset",
                   files={"file": ("data.jsonl", f)})

    from swarm.db import get_db
    conn = get_db(server["db_path"])
    conn.execute(
        "UPDATE experiments SET best_commit = ?, best_val_bpb = ? WHERE id = ?",
        ("abc123", 0.95, exp_id),
    )
    conn.commit()
    conn.close()

    _insert_completed_model(server["db_path"], exp_id, commit="abc123", val_bpb=0.95)

    page.goto(f"{server_url}/experiments/{exp_id}?tab=model")
    page.wait_for_load_state("networkidle")

    expect(page.locator("body")).to_contain_text("Best Model")
    expect(page.locator("body")).to_contain_text("Completed")
    expect(page.locator(".sha")).to_contain_text("abc123"[:7])
    expect(page.locator(".met")).to_contain_text("0.9500")


def test_delete_experiment_via_ui(page: Page, server_url, server):
    """Delete experiment via UI: confirm dialog, experiment removed from list."""
    client = httpx.Client(base_url=server_url)
    exp = client.post("/api/experiments", json={"name": "delete-me-test"}).json()
    exp_id = exp["id"]

    page.goto(server_url)
    page.wait_for_load_state("networkidle")
    expect(page.locator("body")).to_contain_text("delete-me-test")

    page.on("dialog", lambda dialog: dialog.accept())
    page.click(f"form[action='/experiments/{exp_id}/ui-action/delete'] button")

    page.wait_for_load_state("networkidle")
    expect(page.locator("body")).not_to_contain_text("delete-me-test")
