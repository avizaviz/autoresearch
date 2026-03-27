"""Git operations tests — branch creation, checkout, selective staging."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm.agent import (
    AgentContext,
    ShellAgentRunner,
    _git_commit,
    _git_current_sha,
)
from swarm.db import get_db, init_db
from swarm.worker import _git_fetch_checkout

import swarm.orchestrator as orch
from tests.conftest import upload_and_start


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


def test_branch_created_on_experiment_start(tmp_path, tmp_git_repo):
    """Starting an experiment creates an autoresearch/<name> branch."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    orch.DB_PATH = db_path
    orch.AUTH_TOKEN = None
    orch.RUNS_DIR = tmp_path
    orch.REPO_PATH = tmp_git_repo
    orch._shutdown_event = __import__("asyncio").Event()

    with TestClient(orch.app, raise_server_exceptions=False) as client:
        resp = client.post("/api/experiments", json={"name": "my-cool-exp"})
        exp_id = resp.json()["id"]
        upload_and_start(client, exp_id)

    result = subprocess.run(
        ["git", "branch", "--list", "autoresearch/*"],
        cwd=str(tmp_git_repo), capture_output=True, text=True,
    )
    branches = result.stdout.strip().splitlines()
    branch_names = [b.strip().lstrip("* ") for b in branches]
    assert any("my-cool-exp" in b for b in branch_names), (
        f"Expected autoresearch/my-cool-exp branch, found: {branch_names}"
    )


def test_worker_checkout_sha(tmp_path):
    """Worker's _git_fetch_checkout checks out a specific SHA."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(bare), str(repo)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True, check=True)

    (repo / "train.py").write_text("print('first')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(repo), capture_output=True, check=True)
    sha1 = _git_current_sha(repo)

    (repo / "train.py").write_text("print('second')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "push"], cwd=str(repo), capture_output=True, check=True)
    sha2 = _git_current_sha(repo)
    assert sha1 != sha2

    result_sha = _git_fetch_checkout(repo, None, sha1)
    assert result_sha == sha1
    assert _git_current_sha(repo) == sha1


def test_git_commit_only_train_py(tmp_git_repo):
    """_git_commit only stages train.py — other modified files are left unstaged."""
    (tmp_git_repo / "train.py").write_text("print('new train')\n")
    (tmp_git_repo / "other.txt").write_text("other content\n")
    subprocess.run(["git", "add", "other.txt"], cwd=str(tmp_git_repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add other"], cwd=str(tmp_git_repo), capture_output=True, check=True)
    (tmp_git_repo / "other.txt").write_text("modified other\n")

    sha = _git_commit(tmp_git_repo, "only train.py")
    assert sha != "", "Expected a commit for train.py changes"

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tmp_git_repo), capture_output=True, text=True,
    )
    lines = [l for l in status.stdout.strip().splitlines() if l.strip()]
    modified_files = [l.split()[-1] for l in lines if l.startswith(" M") or l.startswith("M ")]
    assert "other.txt" in modified_files, (
        f"other.txt should still be modified, status: {status.stdout}"
    )


def test_agent_starts_from_best_commit(tmp_git_repo):
    """Agent checks out best_commit before editing — new commit's parent is best_commit."""
    sha_initial = _git_current_sha(tmp_git_repo)

    (tmp_git_repo / "train.py").write_text("print('v2')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_git_repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=str(tmp_git_repo), capture_output=True, check=True)
    sha_v2 = _git_current_sha(tmp_git_repo)

    (tmp_git_repo / "train.py").write_text("print('v3')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_git_repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "v3"], cwd=str(tmp_git_repo), capture_output=True, check=True)
    sha_v3 = _git_current_sha(tmp_git_repo)

    agent = ShellAgentRunner(command="echo '# agent edit' >> train.py")
    ctx = AgentContext(
        repo_path=tmp_git_repo,
        experiment_prompt="",
        train_py_content="",
        last_result=None,
        best_commit=sha_v2,
        best_val_bpb=1.0,
        history=[],
        trial_index=0,
    )
    result = agent.run(ctx)
    assert result.success, f"Agent failed: {result.error}"
    new_sha = result.new_commit_sha

    parent = subprocess.run(
        ["git", "rev-parse", f"{new_sha}^"],
        cwd=str(tmp_git_repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert parent == sha_v2, (
        f"New commit parent should be best_commit ({sha_v2}), got {parent}"
    )


def test_worker_fetch_before_checkout(tmp_path):
    """Worker can fetch+checkout a SHA that only exists on the remote."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)

    clone1 = tmp_path / "clone1"
    subprocess.run(["git", "clone", str(bare), str(clone1)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(clone1), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone1), capture_output=True, check=True)

    (clone1 / "train.py").write_text("print('v1')\n")
    subprocess.run(["git", "add", "."], cwd=str(clone1), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=str(clone1), capture_output=True, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(clone1), capture_output=True, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(clone1), capture_output=True, check=True)
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(clone1),
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    clone2 = tmp_path / "clone2"
    subprocess.run(["git", "clone", str(bare), str(clone2)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(clone2), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone2), capture_output=True, check=True)
    assert _git_current_sha(clone2) == sha1

    (clone1 / "train.py").write_text("print('v2')\n")
    subprocess.run(["git", "add", "."], cwd=str(clone1), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=str(clone1), capture_output=True, check=True)
    subprocess.run(["git", "push"], cwd=str(clone1), capture_output=True, check=True)
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(clone1),
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    fetched_sha = _git_fetch_checkout(clone2, None, sha2)
    assert fetched_sha == sha2
    assert _git_current_sha(clone2) == sha2
