# Swarm API Reference

Autoresearch swarm mode: one orchestrator + N workers running parallel `train.py` trials.

## Architecture

```
                ┌──────────────────────────┐
                │  Orchestrator (FastAPI)   │
                │  - Trial queue (SQLite)   │
                │  - Experiment management  │
                │  - Stale trial detection  │
                │  port 8765 (default)      │
                └─────────┬────────────────┘
                          │ HTTP
            ┌─────────────┼─────────────┐
            │             │             │
     ┌──────▼──────┐ ┌───▼──────┐ ┌───▼──────┐
     │  Worker 1   │ │ Worker 2 │ │ Worker N │
     │  (GPU box)  │ │ (GPU box)│ │ (GPU box)│
     │  train.py   │ │ train.py │ │ train.py │
     └─────────────┘ └──────────┘ └──────────┘
```

Each worker registers with the orchestrator, polls for queued trials, checks out the correct git ref, runs `train.py`, and reports `val_bpb` back. The orchestrator tracks the best result per experiment.

## Prerequisites

- Python 3.10+ and [uv](https://docs.astral.sh/uv/) on every machine.
- Same repo cloned on each worker machine (workers do `git fetch` + `git checkout` before each trial).
- Data prepared: `uv run prepare.py` must have been run so `~/.cache/autoresearch/` exists. Either run it on each worker or sync the cache directory.
- Dependencies installed: `uv sync` in the repo on each machine.

## Starting the orchestrator

```bash
uv run python -m swarm.orchestrator --host 0.0.0.0 --port 8765
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8765` | Bind port |
| `--db` | `runs/swarm.db` | SQLite database path |
| `--token` | `None` | Shared secret for Bearer auth on `/api/*` routes. If set, all API requests must include `Authorization: Bearer <token>`. |

The orchestrator creates the database and `runs/` directory automatically on first start.

## Starting a worker

```bash
uv run python -m swarm.worker \
  --server http://orchestrator-ip:8765 \
  --repo /path/to/autoresearch
```

| Flag | Default | Description |
|------|---------|-------------|
| `--server` | *(required)* | Orchestrator URL |
| `--repo` | *(required)* | Path to a local clone of this repo with `train.py` |
| `--token` | `None` | Bearer token (must match orchestrator `--token`) |
| `--heartbeat-interval` | `30` | Seconds between heartbeats |
| `--claim-interval` | `5` | Seconds between poll attempts when idle |

Environment variables:
- `TRAIN_TIMEOUT` — max seconds for a single `train.py` run (default `600`).

The worker persists its ID in `<repo>/.swarm_worker_id` so it reconnects with the same identity after restarts.

## Worker lifecycle

```
register → idle (polling) → claim trial → git checkout → train.py → complete → idle …
              ↑                                                          │
              └──────────────────────────────────────────────────────────┘
```

1. **Register** — worker sends hostname to orchestrator, receives a `worker_id`.
2. **Heartbeat** — background thread pings the orchestrator every `--heartbeat-interval` seconds, reporting which trial (if any) is running.
3. **Claim** — worker polls `POST /api/workers/{id}/claim`. Returns a trial spec or 204 (nothing to do).
4. **Checkout** — worker runs `git fetch origin` then `git checkout <ref>` to get the right `train.py`.
5. **Train** — runs `train.py`, captures stdout for `val_bpb`, stderr tail for diagnostics.
6. **Complete** — posts results back. Orchestrator updates the trial and, if this is a new best, updates the experiment's `best_val_bpb`.
7. **Loop** — back to claim.

## Stale trial detection

The orchestrator runs a background loop every 30 seconds. If a running trial's `last_heartbeat_at` is older than **180 seconds**, it's considered stale:

- **Attempts < 3** — trial is requeued (`status → queued`, `worker_id` cleared).
- **Attempts ≥ 3** — trial is marked `failed` with `stderr_tail = "worker_lost: max attempts exceeded"`.

## One experiment at a time

`POST /api/experiments/{id}/start` returns **409 Conflict** if another experiment is already `running`. Stop the active experiment first.

## Error response format

All API errors follow this shape:

```json
{
  "error": "error_code",
  "detail": "Human-readable explanation"
}
```

Common codes: `not_found`, `conflict`, `missing_dataset`, `missing_prompt`, `unauthorized`.

---

## API Reference

All endpoints are under the orchestrator's base URL (e.g. `http://localhost:8765`). If `--token` is set, include `-H 'Authorization: Bearer TOKEN'` on every `/api/*` request.

The examples below assume:
```bash
HOST=http://localhost:8765
# If using auth:
AUTH="-H 'Authorization: Bearer your-token'"
```

---

### GET /health

Health check (no auth required).

```bash
curl $HOST/health
```

```json
{"status": "ok"}
```

---

### POST /api/workers/register

Register a new worker or reconnect an existing one.

```bash
curl -X POST $HOST/api/workers/register \
  -H 'Content-Type: application/json' \
  -d '{"hostname": "gpu-box-1"}'
```

Reconnect with a known ID:

```bash
curl -X POST $HOST/api/workers/register \
  -H 'Content-Type: application/json' \
  -d '{"hostname": "gpu-box-1", "worker_id": "abc123def456"}'
```

Response:

```json
{"worker_id": "abc123def456", "display_name": "Worker-4821"}
```

---

### POST /api/workers/{worker_id}/heartbeat

Worker heartbeat. Optionally report which trial is running (keeps it from going stale).

```bash
curl -X POST $HOST/api/workers/abc123def456/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{"running_trial_id": "trial789abc"}'
```

```json
{"status": "ok"}
```

---

### GET /api/workers

List all registered workers with inferred state.

```bash
curl $HOST/api/workers
```

```json
[
  {
    "id": "abc123def456",
    "display_name": "Worker-4821",
    "hostname": "gpu-box-1",
    "registered_at": "2026-03-25T10:00:00+00:00",
    "last_seen_at": "2026-03-25T12:34:56+00:00",
    "meta_json": null,
    "state": "training"
  }
]
```

`state` is computed on the fly: `"training"` (has a running trial), `"idle"` (online, no trial), or `"offline"` (last seen >120s ago).

---

### POST /api/experiments

Create a new experiment (starts in `draft` status).

```bash
curl -X POST $HOST/api/experiments \
  -H 'Content-Type: application/json' \
  -d '{"name": "overnight-run"}'
```

```json
{
  "id": "exp123abc456",
  "name": "overnight-run",
  "status": "draft",
  "created_at": "2026-03-25T10:00:00+00:00",
  "git_ref": "main",
  "best_val_bpb": null,
  "best_commit": null,
  "dataset_uri": null,
  "prompt_uri": null
}
```

---

### GET /api/experiments

List all experiments (newest first) with trial count breakdown.

```bash
curl $HOST/api/experiments
```

```json
[
  {
    "id": "exp123abc456",
    "name": "overnight-run",
    "status": "running",
    "best_val_bpb": 0.9812,
    "trial_counts": {"completed": 12, "running": 2, "queued": 1, "failed": 1}
  }
]
```

---

### GET /api/experiments/{exp_id}

Get a single experiment with trial counts.

```bash
curl $HOST/api/experiments/exp123abc456
```

```json
{
  "id": "exp123abc456",
  "name": "overnight-run",
  "status": "running",
  "best_val_bpb": 0.9812,
  "best_commit": "a1b2c3d",
  "trial_counts": {"completed": 12, "running": 2, "queued": 1}
}
```

---

### PUT /api/experiments/{exp_id}/dataset

Upload the training dataset for an experiment (multipart file upload).

```bash
curl -X PUT $HOST/api/experiments/exp123abc456/dataset \
  -F file=@data.zip
```

```json
{"dataset_uri": "runs/experiments/exp123abc456/dataset/data.zip"}
```

---

### PUT /api/experiments/{exp_id}/prompt

Upload the agent prompt. Supports two modes:

**Raw text body** (inline prompt):

```bash
curl -X PUT $HOST/api/experiments/exp123abc456/prompt \
  -H 'Content-Type: text/plain' \
  --data-binary @program.md
```

```json
{"prompt_uri": "inline"}
```

**Multipart file upload**:

```bash
curl -X PUT $HOST/api/experiments/exp123abc456/prompt \
  -F file=@program.md
```

```json
{"prompt_uri": "runs/experiments/exp123abc456/prompt.txt"}
```

---

### POST /api/experiments/{exp_id}/start

Start the experiment. Workers will begin claiming trials from its queue.

```bash
curl -X POST $HOST/api/experiments/exp123abc456/start
```

Success — returns the updated experiment object.

**Fails if:**
- Dataset not uploaded → `400 missing_dataset`
- Prompt not uploaded → `400 missing_prompt`
- Another experiment is running → `409 conflict`

---

### POST /api/experiments/{exp_id}/stop

Stop the experiment. Running trials will finish but no new claims will be issued.

```bash
curl -X POST $HOST/api/experiments/exp123abc456/stop
```

Returns the updated experiment object with `status: "stopped"`.

---

### POST /api/workers/{worker_id}/claim

Worker claims the next queued trial. Returns the trial spec or **204 No Content** if nothing is available.

```bash
curl -X POST $HOST/api/workers/abc123def456/claim \
  -H 'Content-Type: application/json' \
  -d '{}'
```

**200 — trial assigned:**

```json
{
  "trial_id": "trial789abc",
  "git_ref": "main",
  "git_commit": null,
  "experiment_id": "exp123abc456",
  "env_json": null
}
```

**204 — no work available** (empty response body).

---

### POST /api/trials/{trial_id}/complete

Worker reports trial results.

```bash
curl -X POST $HOST/api/trials/trial789abc/complete \
  -H 'Content-Type: application/json' \
  -d '{
    "exit_code": 0,
    "val_bpb": 0.9812,
    "stderr_tail": "",
    "git_commit": "a1b2c3d"
  }'
```

```json
{"status": "completed"}
```

If `exit_code != 0`, the trial is marked `failed`. If `val_bpb` is a new best for the experiment, the orchestrator updates `best_val_bpb` and `best_commit`.

---

### GET /api/experiments/{exp_id}/trials

List trials for an experiment with optional filtering, sorting, and pagination.

```bash
# All trials
curl "$HOST/api/experiments/exp123abc456/trials"

# Only completed, sorted by val_bpb
curl "$HOST/api/experiments/exp123abc456/trials?status=completed&sort=val_bpb"

# Page 2, 10 per page
curl "$HOST/api/experiments/exp123abc456/trials?per_page=10&page=2"
```

| Parameter | Default | Options |
|-----------|---------|---------|
| `status` | *(all)* | `queued`, `running`, `completed`, `failed` |
| `sort` | `trial_index` | `trial_index`, `created_at`, `val_bpb`, `status` |
| `per_page` | `50` | any integer |
| `page` | `1` | any integer |

```json
[
  {
    "id": "trial789abc",
    "experiment_id": "exp123abc456",
    "trial_index": 0,
    "status": "completed",
    "val_bpb": 0.9812,
    "exit_code": 0,
    "worker_id": "abc123def456",
    "duration_seconds": 312.5,
    "git_commit": "a1b2c3d",
    "started_at": "2026-03-25T10:05:00+00:00",
    "completed_at": "2026-03-25T10:10:12+00:00"
  }
]
```
