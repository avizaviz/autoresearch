"""Real training tests — run actual train.py on MPS/GPU. Very slow (5-20 min each).

Run with:  .venv/bin/python -m pytest tests/test_real_training.py -v -m slow
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")


@pytest.fixture(autouse=True)
def skip_if_no_gpu():
    try:
        import torch

        if not (torch.backends.mps.is_available() or torch.cuda.is_available()):
            pytest.skip("No GPU available")
    except ImportError:
        pytest.skip("torch not installed")


def test_real_train_py_runs_on_mps():
    """Run train.py directly as subprocess. Verify output contains val_bpb."""
    start = time.monotonic()
    result = subprocess.run(
        [PYTHON, "train.py"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, f"train.py failed:\n{result.stderr[-2000:]}"
    assert elapsed > 60, f"Suspiciously fast training: {elapsed:.0f}s"

    val_bpb = None
    for line in result.stdout.splitlines():
        m = re.match(r"^val_bpb:\s+([0-9.]+)", line)
        if m:
            val_bpb = float(m.group(1))
    assert val_bpb is not None, f"No val_bpb line in stdout:\n{result.stdout[-2000:]}"
    assert 0.5 <= val_bpb <= 2.0, f"val_bpb={val_bpb} outside expected range"


def test_worker_parses_real_val_bpb():
    """Import _run_train from worker and run against the real project root."""
    from swarm.worker import _run_train

    start = time.monotonic()
    exit_code, val_bpb, stderr_tail = _run_train(PROJECT_ROOT)
    elapsed = time.monotonic() - start

    assert exit_code == 0, f"train.py failed (exit={exit_code}):\n{stderr_tail}"
    assert val_bpb is not None, "val_bpb was None — worker failed to parse output"
    assert isinstance(val_bpb, float)
    assert elapsed > 60, f"Suspiciously fast: {elapsed:.0f}s"


def test_real_training_duration_realistic():
    """Explicitly check training duration is between 2 min and 30 min."""
    from swarm.worker import _run_train

    start = time.monotonic()
    exit_code, val_bpb, stderr_tail = _run_train(PROJECT_ROOT)
    duration = time.monotonic() - start

    assert exit_code == 0, f"train.py failed (exit={exit_code}):\n{stderr_tail}"
    assert duration > 120, f"Training too fast: {duration:.0f}s (expected >120s)"
    assert duration < 1800, f"Training too slow: {duration:.0f}s (expected <1800s)"
