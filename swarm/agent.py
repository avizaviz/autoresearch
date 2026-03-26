"""Agent runner — generates new train.py edits between trials.

v1 uses cursor-agent CLI. The interface is pluggable for Claude API, OpenAI, etc.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

import structlog

log = structlog.get_logger()

_agent_lock = threading.Lock()


@dataclass
class TrialResult:
    trial_index: int
    val_bpb: Optional[float]
    exit_code: int
    stderr_tail: Optional[str] = None
    git_commit: Optional[str] = None
    status: str = "completed"


@dataclass
class AgentContext:
    repo_path: Path
    experiment_prompt: str
    train_py_content: str
    last_result: Optional[TrialResult]
    best_commit: Optional[str]
    best_val_bpb: Optional[float]
    history: list[TrialResult] = field(default_factory=list)
    trial_index: int = 0


@dataclass
class AgentResult:
    success: bool
    new_commit_sha: Optional[str] = None
    description: str = ""
    error: Optional[str] = None


class AgentRunner(Protocol):
    def run(self, context: AgentContext) -> AgentResult: ...


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _git_current_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True, timeout=10,
    )
    return result.stdout.strip()


def _git_checkout(repo: Path, ref: str):
    subprocess.run(
        ["git", "checkout", ref], cwd=str(repo),
        capture_output=True, check=True, timeout=30,
    )


def _git_commit(repo: Path, message: str) -> str:
    subprocess.run(
        ["git", "add", "train.py"], cwd=str(repo),
        capture_output=True, check=True, timeout=10,
    )
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(repo),
        capture_output=True,
    )
    if result.returncode == 0:
        return ""

    subprocess.run(
        ["git", "commit", "-m", message], cwd=str(repo),
        capture_output=True, check=True, timeout=30,
    )
    return _git_current_sha(repo)


def _build_prompt(ctx: AgentContext) -> str:
    parts = []

    parts.append("You are an autonomous AI researcher. Your task is to edit train.py to improve the model's validation performance (val_bpb — lower is better).")
    parts.append("")

    if ctx.experiment_prompt:
        parts.append("## Research Instructions")
        parts.append(ctx.experiment_prompt)
        parts.append("")

    if ctx.last_result:
        parts.append("## Last Trial Result")
        if ctx.last_result.status == "completed" and ctx.last_result.val_bpb is not None:
            parts.append(f"- val_bpb: {ctx.last_result.val_bpb}")
            if ctx.best_val_bpb is not None:
                if ctx.last_result.val_bpb <= ctx.best_val_bpb:
                    parts.append(f"- This WAS an improvement (best so far: {ctx.best_val_bpb})")
                else:
                    parts.append(f"- This was NOT an improvement (best so far: {ctx.best_val_bpb})")
        elif ctx.last_result.status == "failed":
            parts.append(f"- FAILED (exit_code={ctx.last_result.exit_code})")
            if ctx.last_result.stderr_tail:
                parts.append(f"- Error: {ctx.last_result.stderr_tail[:500]}")
        parts.append("")

    if ctx.history:
        parts.append("## Recent Trial History")
        for t in ctx.history[-10:]:
            bpb = f"val_bpb={t.val_bpb}" if t.val_bpb else f"FAILED(exit={t.exit_code})"
            parts.append(f"  Trial #{t.trial_index}: {bpb}")
        parts.append("")

    parts.append("## Current train.py")
    parts.append("The file is at: train.py in the repo root.")
    parts.append("Edit it to try a new approach that might lower val_bpb.")
    parts.append("")
    parts.append("RULES:")
    parts.append("- Only edit train.py")
    parts.append("- The training must complete within the time budget")
    parts.append("- If the last trial crashed, fix the bug or try a different approach")
    parts.append("- Be creative but keep changes focused — one idea per trial")
    parts.append("- Do NOT ask questions or pause — just make the edit")

    return "\n".join(parts)


class CursorAgentRunner:
    """Uses cursor-agent CLI to edit train.py."""

    def __init__(self, cursor_cmd: str = "cursor-agent"):
        self.cursor_cmd = cursor_cmd
        self.timeout = int(os.environ.get("AGENT_TIMEOUT", "300"))

    def run(self, context: AgentContext) -> AgentResult:
        if not _agent_lock.acquire(blocking=False):
            return AgentResult(success=False, error="agent already running")

        try:
            return self._run_locked(context)
        finally:
            _agent_lock.release()

    def _run_locked(self, ctx: AgentContext) -> AgentResult:
        repo = ctx.repo_path

        if ctx.best_commit:
            try:
                _git_checkout(repo, ctx.best_commit)
                log.msg("agent.checkout_best", commit=ctx.best_commit)
            except Exception as e:
                log.error("agent.checkout_failed", error=str(e))
                return AgentResult(success=False, error=f"checkout failed: {e}")

        old_sha = _git_current_sha(repo)
        old_content = _read_file(repo / "train.py")

        prompt = _build_prompt(ctx)

        try:
            log.msg("agent.start", trial_index=ctx.trial_index)
            result = subprocess.run(
                [self.cursor_cmd, "-p", "--output-format", "text", prompt],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            agent_output = result.stdout
            if result.returncode != 0:
                log.error("agent.cursor_failed", returncode=result.returncode,
                          stderr=result.stderr[:500])
                return AgentResult(
                    success=False,
                    error=f"cursor-agent exit {result.returncode}: {result.stderr[:200]}",
                )
        except subprocess.TimeoutExpired:
            log.error("agent.timeout", timeout=self.timeout)
            return AgentResult(success=False, error=f"cursor-agent timed out after {self.timeout}s")
        except FileNotFoundError:
            log.error("agent.not_found", cmd=self.cursor_cmd)
            return AgentResult(
                success=False,
                error=f"cursor-agent command not found: {self.cursor_cmd}",
            )

        new_content = _read_file(repo / "train.py")
        if new_content == old_content:
            log.warning("agent.no_change")
            return AgentResult(success=False, error="agent did not modify train.py")

        description = f"trial {ctx.trial_index}: agent edit"
        first_line = ""
        for line in agent_output.splitlines():
            stripped = line.strip()
            if stripped and len(stripped) > 5:
                first_line = stripped[:80]
                break
        if first_line:
            description = f"trial {ctx.trial_index}: {first_line}"

        try:
            new_sha = _git_commit(repo, description)
            if not new_sha:
                log.warning("agent.commit_empty")
                return AgentResult(success=False, error="git commit produced no diff")
            log.msg("agent.committed", sha=new_sha, description=description)
            return AgentResult(success=True, new_commit_sha=new_sha, description=description)
        except Exception as e:
            log.error("agent.commit_failed", error=str(e))
            return AgentResult(success=False, error=f"git commit failed: {e}")


class ShellAgentRunner:
    """Runs a user-provided shell command to edit train.py."""

    def __init__(self, command: str):
        self.command = command
        self.timeout = int(os.environ.get("AGENT_TIMEOUT", "300"))

    def run(self, context: AgentContext) -> AgentResult:
        if not _agent_lock.acquire(blocking=False):
            return AgentResult(success=False, error="agent already running")

        try:
            return self._run_locked(context)
        finally:
            _agent_lock.release()

    def _run_locked(self, ctx: AgentContext) -> AgentResult:
        repo = ctx.repo_path

        if ctx.best_commit:
            try:
                _git_checkout(repo, ctx.best_commit)
            except Exception as e:
                return AgentResult(success=False, error=f"checkout failed: {e}")

        old_sha = _git_current_sha(repo)
        old_content = _read_file(repo / "train.py")

        env = os.environ.copy()
        env["LAST_VAL_BPB"] = str(ctx.last_result.val_bpb) if ctx.last_result and ctx.last_result.val_bpb else ""
        env["BEST_VAL_BPB"] = str(ctx.best_val_bpb) if ctx.best_val_bpb else ""
        env["TRIAL_INDEX"] = str(ctx.trial_index)
        env["EXPERIMENT_PROMPT"] = ctx.experiment_prompt or ""

        try:
            result = subprocess.run(
                self.command, shell=True, cwd=str(repo),
                capture_output=True, text=True, timeout=self.timeout, env=env,
            )
            if result.returncode != 0:
                return AgentResult(success=False, error=f"shell exit {result.returncode}: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            return AgentResult(success=False, error=f"shell command timed out after {self.timeout}s")

        new_content = _read_file(repo / "train.py")
        if new_content == old_content:
            return AgentResult(success=False, error="shell command did not modify train.py")

        try:
            new_sha = _git_commit(repo, f"trial {ctx.trial_index}: shell agent edit")
            if not new_sha:
                return AgentResult(success=False, error="git commit produced no diff")
            return AgentResult(success=True, new_commit_sha=new_sha, description=f"shell edit for trial {ctx.trial_index}")
        except Exception as e:
            return AgentResult(success=False, error=f"git commit failed: {e}")


def create_agent(agent_type: Optional[str] = None) -> AgentRunner:
    agent_type = agent_type or os.environ.get("SWARM_AGENT_TYPE", "cursor")
    if agent_type == "cursor":
        cmd = os.environ.get("SWARM_CURSOR_CMD", "cursor-agent")
        return CursorAgentRunner(cursor_cmd=cmd)
    elif agent_type == "shell":
        cmd = os.environ.get("SWARM_AGENT_SHELL_CMD")
        if not cmd:
            raise ValueError("SWARM_AGENT_SHELL_CMD env required for shell agent type")
        return ShellAgentRunner(command=cmd)
    elif agent_type == "none":
        return _NoopAgentRunner()
    else:
        raise ValueError(f"Unknown agent type: {agent_type}")


class _NoopAgentRunner:
    """Does nothing — for testing without a real agent."""
    def run(self, context: AgentContext) -> AgentResult:
        return AgentResult(success=False, error="noop agent: no edits produced")
