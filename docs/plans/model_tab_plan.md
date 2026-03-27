# Model Tab — Create, Store, and Test the Best Model

## What the user wants

The experiment detail page gets a **new tab: "Model"** alongside "Experiment" and "Workers". This tab lets you:

1. **See** if a model checkpoint exists for the current best trial
2. **Create** a model from the best `train.py` (re-runs training + saves checkpoint)
3. **Monitor** model creation progress (in-progress, completed, failed)
4. **Test** the model interactively (text generation / inference)
5. **Recreate** if a better trial appears after the model was built

Also: **pin the best trial** to the top of the trials table.

---

## Architecture

### What "create model" means

Upstream `train.py` does NOT save model weights — the model lives in GPU memory and is lost when the process exits. "Create model" means:

1. Check out `best_commit` (the `train.py` that produced the best `val_bpb`)
2. Run `train.py` again (~5-20 min) but with an **extra step at the end**: `torch.save(model.state_dict(), output_path)`
3. Save the checkpoint to `runs/experiments/<id>/model/model.pt`
4. Record in DB: which commit, which val_bpb, when created

### How to add torch.save without modifying train.py

Option A: **Wrapper script** (`swarm/create_model.py`) that:
- Imports the model class from `train.py` (or exec's it)
- After training + eval, calls `torch.save()`
- This avoids touching `train.py` (which is the agent's domain)

Option B: **Inject a save hook** — set an env var `SWARM_SAVE_MODEL=runs/experiments/<id>/model/model.pt` that `train.py` checks at the end.

**Recommended: Option B** (simpler, one env var, no import hacking). Add ~3 lines to the end of `train.py`:

```python
import os
if os.environ.get("SWARM_SAVE_MODEL"):
    torch.save(model.state_dict(), os.environ["SWARM_SAVE_MODEL"])
```

This keeps `train.py` as the single source of truth for the model architecture.

### Model creation as a "job"

Model creation is a **separate process** from trial training — it runs the same `train.py` but with the save flag. Track it in DB:

**New table: `models`**

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PRIMARY KEY | UUID |
| `experiment_id` | TEXT FK | Which experiment |
| `source_commit` | TEXT | The `git_commit` used (= best_commit at time of creation) |
| `source_val_bpb` | REAL | The val_bpb of that commit's trial |
| `status` | TEXT | `creating` / `completed` / `failed` / `cancelled` |
| `model_path` | TEXT | Path to saved `.pt` file |
| `created_at` | TEXT | When creation started |
| `completed_at` | TEXT | When done |
| `duration_seconds` | REAL | How long creation took |
| `error` | TEXT | Error message if failed |

### Testing the model

For a language model, "test" = **text generation**. The Model tab provides:

- **Text input** (prompt box)
- **Generate button** → sends text to a small inference endpoint
- **Output display** (generated continuation)
- Parameters: temperature, max tokens, top-k

**Inference endpoint:** A lightweight FastAPI route on the orchestrator that:
1. Loads `model.pt` into memory (lazy — load once, keep warm)
2. Uses the tokenizer from `prepare.py`
3. Runs autoregressive generation
4. Returns generated text

This is a **read-only** operation — doesn't affect the experiment or trials.

---

## UI Design

### Trials table: pin best trial

In the trials table (Experiment tab), the **best trial** (matching `best_val_bpb`) gets:
- Pinned to the **top row** (always visible, even with pagination)
- A distinct visual treatment (green highlight / "BEST" badge — already partially there)
- Separated from the rest by a subtle divider

### Model tab layout

```
[Experiment] [Workers] [Model]

┌─────────────────────────────────────────────┐
│ BEST MODEL                                  │
│ Commit: e1d7f1c (val_bpb: 0.1237)         │
│ Status: ● Completed (created 2h ago)        │
│                                             │
│ ⚠ A better trial exists!                   │
│   Current model: 0.1237 (commit e1d7f1c)   │
│   Best trial:    0.0983 (commit a3b4c5d)   │
│   [Recreate Model]                          │
├─────────────────────────────────────────────┤
│ TEST MODEL                                  │
│ ┌─────────────────────────────────────────┐ │
│ │ Enter text prompt...                    │ │
│ └─────────────────────────────────────────┘ │
│ Temperature: [0.8] Max tokens: [200]        │
│ [Generate]                                  │
│                                             │
│ Generated output:                           │
│ ┌─────────────────────────────────────────┐ │
│ │ Once upon a time, there was a small...  │ │
│ └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

### States

| State | What user sees |
|-------|----------------|
| **No model** | "No model created yet. Click Create to build one from the best trial." + [Create Model] button |
| **Creating** | Progress: "Creating model from commit e1d7f1c... (3m 12s elapsed)" + [Cancel] button |
| **Completed** | Model info card + test interface + [Recreate] if better trial exists |
| **Failed** | Error message + [Retry] button |
| **Outdated** | Warning banner: "A better trial exists. Current model: 0.1237, Best trial: 0.0983" + [Recreate Model] |

---

## API Endpoints (new)

| Method | Path | Description |
|--------|------|-------------|
| `POST /api/experiments/{id}/model/create` | Start model creation job |
| `GET /api/experiments/{id}/model` | Get current model status + info |
| `POST /api/experiments/{id}/model/cancel` | Cancel in-progress creation |
| `POST /api/experiments/{id}/model/generate` | Run inference: `{"prompt": "...", "temperature": 0.8, "max_tokens": 200}` → `{"text": "..."}` |

---

## Implementation order

1. **Pin best trial** in trials table (UI only, quick win)
2. **`models` table** in schema.sql
3. **Model creation endpoint** + background job (re-run train.py with save flag)
4. **Model tab UI** (status card, create/recreate buttons, outdated warning)
5. **Inference endpoint** (load model, generate text)
6. **Test interface UI** (prompt box, generate, output)
7. **Tests**: CLI tests for all endpoints + Playwright tests for the Model tab

---

## Tests needed

Per project rules: every feature needs **CLI test + Playwright test**. No mocks for model creation — must run real `train.py` + `torch.save`.

### CLI (API) — 12 tests

| # | Test | What to assert |
|---|------|----------------|
| 1 | Create model from best commit | POST /model/create → status `creating`; poll until `completed`; `model_path` file exists on disk |
| 2 | Get model status | GET /model → correct `source_commit`, `source_val_bpb`, `status`, `model_path` |
| 3 | Cancel model creation | POST /model/cancel during `creating` → status `cancelled` |
| 4 | Create with no best_commit | POST /model/create on experiment with no completed trials → 400 |
| 5 | Generate text with completed model | POST /model/generate with prompt → returns non-empty `text` string |
| 6 | Generate with no model | POST /model/generate when no model exists → 400 |
| 7 | Recreate after better trial | Complete a better trial, POST /model/create again → new model with new `source_commit` |
| 8 | Model creation duration realistic | Assert `duration_seconds` > 60 (real training, not instant) |
| 9 | Model file size reasonable | Assert `model.pt` file > 1MB and < 500MB |
| 10 | Generate with different temperatures | temp=0.1 and temp=1.5 both return text; temp=0.0 is deterministic (same output twice) |
| 11 | Persistence after restart | Create model, restart orchestrator, GET /model still shows `completed` with same path |
| 12 | Concurrent: create model while experiment running | Both model creation and trial training proceed without conflict |

### Playwright (UI) — 7 tests

| # | Test | What to assert |
|---|------|----------------|
| 1 | Model tab shows "no model" state | Fresh experiment → tab shows placeholder + Create button |
| 2 | Create button → progress → completed | Click Create, see "Creating..." status, eventually see "Completed" |
| 3 | Outdated warning | Complete a better trial after model was created → warning banner with "Recreate" button |
| 4 | Test interface: generate text | Type prompt in text box, click Generate, see generated output appear |
| 5 | Recreate button works | Click Recreate → new creation starts → completes with updated commit |
| 6 | Model file size shown in UI | Completed model card shows file size (e.g. "47.2 MB") |
| 7 | Pin best trial at top of trials table | Best trial row is always first in the table regardless of pagination |

### Total: 19 tests for this feature

---

## Open questions

1. **Model size**: The model checkpoint could be 50-200MB. Store locally under `runs/` — no cloud upload for v1.
2. **Inference warm-up**: Loading the model takes a few seconds. Keep it warm in memory after first load? Or load-on-demand per request?
3. **Device for inference**: Same MPS device as training? CPU fallback for smaller models?
4. **torch.save location**: The 3-line addition to `train.py` means the agent might overwrite it in the next trial. We save model from `best_commit` specifically, so we checkout that commit first.
