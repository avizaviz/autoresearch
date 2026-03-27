# autoresearch — swarm mode

Instructions for AI agents working with the swarm orchestrator.

## What's different from single-machine mode

In single-machine mode (`program.md`), you run `uv run train.py` directly and manage results yourself. In swarm mode, an **orchestrator** handles scheduling and a pool of **workers** execute trials. Your job is the same — improve `train.py` to minimize `val_bpb` — but the infrastructure around you is different:

| | Single-machine | Swarm |
|---|----------------|-------|
| Who runs `train.py` | You (`uv run train.py`) | Workers (automatically) |
| Who tracks results | You (`results.tsv`) | Orchestrator (SQLite + API) |
| Parallelism | 1 trial at a time | N workers, N trials at a time |
| Git workflow | You commit and revert | You commit; orchestrator queues the ref |

## What you still do

1. **Edit `train.py` only.** Same constraint as single-machine. Architecture, optimizer, hyperparameters — everything in `train.py` is fair game. Do not modify `prepare.py`.
2. **Commit your changes.** Each commit represents a candidate to be evaluated. Push to the branch the experiment is tracking (typically `main` or an `autoresearch/*` branch).
3. **Check results via the API.** The orchestrator tracks every trial and the best `val_bpb` seen so far.

## What you don't do

- **Don't run `train.py` yourself.** Workers handle execution.
- **Don't manage workers.** The orchestrator handles registration, heartbeats, and stale trial detection.
- **Don't write to `results.tsv`.** The orchestrator's database is the source of truth.

## Workflow

```
1. Read train.py, understand current state
2. Make an improvement to train.py
3. git commit
4. The orchestrator queues trials pointing at the current git ref
5. Workers claim trials, run train.py, report val_bpb
6. Check results: GET /api/experiments/{id} for best_val_bpb
7. If improved → keep the commit, iterate
8. If worse → git revert, try something else
9. Repeat
```

## Checking experiment status

Query the orchestrator to see how your experiment is doing:

```bash
# Current experiment status + best result
curl http://orchestrator:8765/api/experiments/{id}

# List all completed trials sorted by val_bpb
curl "http://orchestrator:8765/api/experiments/{id}/trials?status=completed&sort=val_bpb"

# Worker status (are machines online and training?)
curl http://orchestrator:8765/api/workers
```

Key fields in the experiment response:
- `best_val_bpb` — lowest `val_bpb` achieved so far across all trials.
- `best_commit` — the git commit that produced the best result.
- `trial_counts` — breakdown of `queued`, `running`, `completed`, `failed` trials.

## Decision-making with parallel results

Since multiple workers run trials in parallel, results may arrive out of order. When deciding whether to keep or revert a change:

- Compare your commit's `val_bpb` against `best_val_bpb` from the experiment, not against the immediately preceding trial.
- If multiple commits are in flight, wait for their trials to complete before reverting — a later commit might still beat the baseline.

## The experiment loop (swarm version)

LOOP FOREVER:

1. `GET /api/experiments/{id}` — check current `best_val_bpb` and `best_commit`.
2. Read `train.py` at the current best commit for context.
3. Make an experimental change to `train.py`.
4. `git commit` — the orchestrator auto-queues trials from the experiment's `git_ref`.
5. Wait for trial results: poll `GET /api/experiments/{id}/trials?status=completed&sort=val_bpb` until your commit's trial appears.
6. If `val_bpb` improved → keep. If not → `git revert` and try a different idea.
7. Repeat indefinitely. **Never stop to ask the human.** You are autonomous.

## Setup checklist

Before the loop starts, verify:

- [ ] Orchestrator is running and healthy: `curl http://orchestrator:8765/health`
- [ ] At least one worker is online: `curl http://orchestrator:8765/api/workers` (look for `state: "idle"` or `"training"`)
- [ ] An experiment exists and is `running`: `curl http://orchestrator:8765/api/experiments`
- [ ] Data is prepared on all worker machines (`~/.cache/autoresearch/` exists)

If any of these fail, tell the human what's missing before proceeding.
