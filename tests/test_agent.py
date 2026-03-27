"""Unit tests for swarm/agent.py — agent runners, git helpers, prompt builder."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from swarm.agent import (
    AgentContext,
    AgentResult,
    CursorAgentRunner,
    ShellAgentRunner,
    TrialResult,
    _NoopAgentRunner,
    _agent_lock,
    _build_prompt,
    _git_checkout,
    _git_commit,
    _git_current_sha,
    _read_file,
    create_agent,
)


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


def _make_context(**overrides) -> AgentContext:
    defaults = dict(
        repo_path=Path("/tmp/fake"),
        experiment_prompt="",
        train_py_content="print('hi')\n",
        last_result=None,
        best_commit=None,
        best_val_bpb=None,
        history=[],
        trial_index=0,
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


# -------------------------------------------------------------------
# _build_prompt
# -------------------------------------------------------------------
class TestBuildPrompt:
    def test_includes_experiment_prompt(self):
        ctx = _make_context(experiment_prompt="Try a cosine schedule")
        prompt = _build_prompt(ctx)
        assert "Try a cosine schedule" in prompt
        assert "Research Instructions" in prompt

    def test_includes_last_result(self):
        last = TrialResult(trial_index=0, val_bpb=1.23, exit_code=0)
        ctx = _make_context(last_result=last)
        prompt = _build_prompt(ctx)
        assert "1.23" in prompt
        assert "Last Trial Result" in prompt

    def test_includes_failure_info(self):
        last = TrialResult(
            trial_index=1, val_bpb=None, exit_code=1,
            stderr_tail="RuntimeError: CUDA OOM", status="failed",
        )
        ctx = _make_context(last_result=last)
        prompt = _build_prompt(ctx)
        assert "FAILED" in prompt
        assert "CUDA OOM" in prompt

    def test_includes_history(self):
        history = [
            TrialResult(trial_index=0, val_bpb=1.5, exit_code=0),
            TrialResult(trial_index=1, val_bpb=1.2, exit_code=0),
            TrialResult(trial_index=2, val_bpb=None, exit_code=1),
        ]
        ctx = _make_context(history=history)
        prompt = _build_prompt(ctx)
        assert "Trial #0" in prompt
        assert "Trial #1" in prompt
        assert "Trial #2" in prompt
        assert "Recent Trial History" in prompt


# -------------------------------------------------------------------
# Git helpers
# -------------------------------------------------------------------
class TestGitHelpers:
    def test_git_commit_creates_sha(self, tmp_git_repo):
        (tmp_git_repo / "train.py").write_text("print('modified')\n")
        sha = _git_commit(tmp_git_repo, "test commit")
        assert sha != ""
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_git_commit_no_changes_returns_empty(self, tmp_git_repo):
        sha = _git_commit(tmp_git_repo, "nothing changed")
        assert sha == ""

    def test_git_checkout_switches_branch(self, tmp_git_repo):
        default_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(tmp_git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        original_sha = _git_current_sha(tmp_git_repo)

        subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=str(tmp_git_repo), capture_output=True, check=True,
        )
        (tmp_git_repo / "train.py").write_text("print('feature')\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_git_repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "feature commit"], cwd=str(tmp_git_repo), capture_output=True, check=True)
        feature_sha = _git_current_sha(tmp_git_repo)
        assert feature_sha != original_sha

        _git_checkout(tmp_git_repo, default_branch)
        assert _git_current_sha(tmp_git_repo) == original_sha


# -------------------------------------------------------------------
# Agent runners
# -------------------------------------------------------------------
class TestAgentRunners:
    def test_noop_agent_returns_failure(self):
        agent = _NoopAgentRunner()
        ctx = _make_context()
        result = agent.run(ctx)
        assert result.success is False
        assert result.error is not None

    def test_create_agent_cursor(self):
        agent = create_agent("cursor")
        assert isinstance(agent, CursorAgentRunner)

    def test_create_agent_none(self):
        agent = create_agent("none")
        assert isinstance(agent, _NoopAgentRunner)

    def test_create_agent_shell_requires_env(self):
        old = os.environ.pop("SWARM_AGENT_SHELL_CMD", None)
        try:
            with pytest.raises(ValueError, match="SWARM_AGENT_SHELL_CMD"):
                create_agent("shell")
        finally:
            if old is not None:
                os.environ["SWARM_AGENT_SHELL_CMD"] = old

    def test_shell_agent_runs_command(self, tmp_git_repo):
        agent = ShellAgentRunner(command="echo '# trial edit' >> train.py")
        ctx = _make_context(repo_path=tmp_git_repo, trial_index=5)
        result = agent.run(ctx)
        assert result.success is True
        assert result.new_commit_sha is not None
        assert len(result.new_commit_sha) == 40
        content = (tmp_git_repo / "train.py").read_text()
        assert "trial edit" in content

    def test_agent_lock_prevents_concurrent(self, tmp_git_repo):
        _agent_lock.acquire()
        try:
            agent = ShellAgentRunner(command="echo 'x' >> train.py")
            ctx = _make_context(repo_path=tmp_git_repo)
            result = agent.run(ctx)
            assert result.success is False
            assert "already running" in result.error
        finally:
            _agent_lock.release()
