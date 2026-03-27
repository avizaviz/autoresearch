---
name: Swarm UI + dashboard
overview: "Phase 2 (after Phase 1 plan): HTML dashboard as a thin client over the existing REST API. Visibility and triggering only; no duplicate business logic. Playwright E2E, charts in browser, worker/experiment tables."
todos:
  - id: ui-thin-client
    content: "Jinja2 + HTMX front-end served by FastAPI; all actions = same REST as CLI; OpenAPI sync"
    status: pending
  - id: ui-pages
    content: "Implement pages: experiments list, experiment detail (trials + chart), workers panel"
    status: pending
  - id: ui-experiment-form
    content: "Upload dataset + prompt, Start/Stop/Resume, disabled Start until inputs valid; mirrors API"
    status: pending
  - id: ui-worker-panel
    content: "Worker list (display_name, offline/idle/training), queue depth, stuck counts from API"
    status: pending
  - id: ui-charts
    content: "Experiment detail val_bpb + best-so-far chart using Chart.js; data from GET /api/experiments/{id}/trials"
    status: pending
  - id: ui-error-empty-states
    content: "Loading skeletons, empty states (no experiments, no trials, no workers), API-down banner"
    status: pending
  - id: ui-polling
    content: "Auto-refresh via HTMX polling (hx-trigger every 5s) or manual refresh button"
    status: pending
  - id: ui-playwright-e2e
    content: "Flow B Playwright — upload, refresh persistence, Start, wait completed, Stop, resume; nightly job"
    status: pending
  - id: ui-at-tests
    content: "AT-UI-1 through AT-UI-4 + Phase 1 AT supplements (AT-11, AT-17, AT-20, AT-24, AT-26 Playwright)"
    status: pending
isProject: false
---

# Phase 2 — Swarm dashboard (UI only)

**Prerequisite:** [Phase 1 plan](fork_viz_+_swarm_f1f60bbf.plan.md) complete — orchestrator **REST API + CLI + worker + CLI E2E** green. **Do not** build dashboard HTML until API contract is stable.

---

## Role of the UI (not logic)

- **Visibility:** Tables and charts over data the API already exposes (`experiments`, `trials`, `workers`, aggregates).
- **Triggering:** Same `POST/GET/PUT` as `curl` — the browser is another client, not a second source of truth.
- **No business logic** beyond validation UX (e.g. disable Start until required fields filled) — server remains authoritative.

---

## Pages and routes

**Reference mockup:** [`docs/ui-mockup.html`](../../docs/ui-mockup.html) — open in browser for the interactive draft with mock data.

**Layout:** Persistent **stat boxes** at top of every view (content swaps based on context). Two levels:

| Level | What user sees |
|-------|----------------|
| **Home (experiments list)** | 5 stat boxes (summary: active experiment, completed trials, best val_bpb, workers active, avg trial time) + info banner ("one experiment at a time") + experiments table. Click a row to drill in. |
| **Inside an experiment** | Same 5 stat boxes (now showing that experiment's data: running time, best val_bpb, trial counts, workers on this, avg trial time) + back arrow ("All experiments") + **Experiment / Workers** sub-tabs. |

**Sub-tabs (inside experiment):**

| Sub-tab | Content |
|---------|---------|
| **Experiment** | Dataset/prompt info, best-commit highlight card, improvement chart (val_bpb + best-so-far), trials table (sortable, filterable by status). |
| **Workers** | Stuck-trial banner with Requeue/Mark Failed actions, workers table (display_name, state badge, current trial + progress bar, trials done, avg time, best val_bpb, GPU), trials-per-worker bar chart, recent activity timeline. |

**One experiment at a time:** If an experiment is `running`, Start/Resume buttons on all other experiments are **disabled** with tooltip "Stop the running experiment first."

**Optional (v2+):**
- Trial detail drill-down (stderr tail, full logs).
- Settings page.

---

## API endpoints the UI consumes (Phase 1 must support)

The UI is a thin client. These `GET` endpoints must return **JSON** with enough structure for tables and charts:

| Endpoint | Used by | Notes |
|----------|---------|-------|
| `GET /api/experiments` | Experiments list | Array of experiment objects with inline aggregate counts. Support `?status=running` filter. |
| `GET /api/experiments/{id}` | Experiment detail | Single object with `best_commit`, `best_val_bpb`, status, counts. |
| `GET /api/experiments/{id}/trials` | Trials table + chart | Array of trial objects. Support `?sort=val_bpb`, `?status=completed`, `?per_page=50&page=1` pagination. |
| `GET /api/workers` | Workers panel | Array of worker objects with derived `state` field (`offline`/`idle`/`training`). |
| `GET /api/health` | Connection indicator | Simple `{"status": "ok"}`. |

**Mutation endpoints** (same as CLI — AT-27 contract):
- `POST /api/experiments` — create
- `PUT /api/experiments/{id}/dataset` — upload dataset
- `PUT /api/experiments/{id}/prompt` — upload prompt
- `POST /api/experiments/{id}/start` — start
- `POST /api/experiments/{id}/stop` — stop

**Pagination:** Default `per_page=50`. UI should show page controls or infinite scroll for trials table.

---

## Stack

### Recommended: **Jinja2 + HTMX** (server-rendered, no build step)

- **Jinja2** templates served by FastAPI (`Jinja2Templates` from `starlette`).
- **HTMX** for dynamic updates without a full SPA: `hx-get`, `hx-trigger="every 5s"`, `hx-swap="innerHTML"`.
- **CSS:** Minimal framework — **Pico CSS** (classless, modern defaults) or **TailwindCSS CDN** (utility classes, no build).
- **Chart:** **Chart.js** via CDN (`<script src="...">`). Lightweight, no npm needed. Renders `val_bpb` + `best_so_far` as line chart.

**Why not React/Vue SPA:** Adds npm, bundler, CORS complexity. HTMX + Jinja gives interactive-feeling pages with zero JS build tooling. Good enough for an internal LAN dashboard.

**Alternative (acceptable):** If you want richer interactivity later, **Preact + htm** (no build step, ESM imports) or **Svelte** (small bundle). But start with HTMX.

### File layout

```
swarm/
  templates/
    base.html          # layout: nav, footer, CSS/JS imports
    experiments.html    # experiments list
    experiment.html     # experiment detail + chart
    workers.html        # workers panel
    partials/
      trials_table.html # HTMX partial for polling updates
      workers_table.html
  static/
    style.css           # custom styles (minimal)
```

FastAPI mounts: `app.mount("/static", StaticFiles(...))` + `templates = Jinja2Templates(directory="swarm/templates")`.

---

## Real-time updates

**v1 approach: HTMX polling.** Each dynamic section (trials table, workers table, queue count) uses `hx-trigger="every 5s"` to re-fetch its partial from the server. Simple, no WebSocket infra.

**Tradeoffs:**
- 5s polling on LAN is fine for <20 workers. Adjust interval if needed.
- Server renders HTML partials (not JSON) for HTMX — fast, cacheable.
- **Manual refresh button** also available (always).

**v2 (optional):** SSE (`EventSource`) from FastAPI for push updates on trial completion. Additive — polling still works as fallback.

---

## Authentication in browser

**v1 default: no auth on LAN (trusted network).** Dashboard is open to anyone who can reach the orchestrator port. Same as the API with `SWARM_TOKEN` — if the token is required, the UI includes it as a cookie or `Authorization` header from a simple "enter token" prompt on first visit (stored in `localStorage`).

**Not v1:** Login form, user accounts, RBAC. Trusted LAN assumption matches Phase 1.

---

## Error and empty states

Every page must handle these gracefully (not blank screen or unhandled JS error):

| State | What the user sees |
|-------|--------------------|
| **API unreachable** | Banner: "Cannot reach orchestrator at `<url>`. Check if the server is running." Retry button. |
| **No experiments** | Empty table with message: "No experiments yet. Create one to get started." + link/button to create. |
| **Experiment has 0 trials** | Trials table placeholder: "Waiting for trials... Refill will create work when the experiment is running." |
| **No workers connected** | Workers table: "No workers connected. Start a worker process on a GPU machine." |
| **Upload in progress** | Progress bar or spinner on the upload button. Disable Start until upload completes. |
| **Trial failed** | Red badge in trials table; expandable `stderr_tail` snippet. |

### Loading states

- Tables show **skeleton rows** (gray placeholder blocks) while fetching.
- Chart shows "Loading..." until data arrives.
- Buttons disable during mutation requests (Start/Stop) with a small spinner.

---

## Playwright / UI end-to-end (Flow B)

**When:** After Flow A (CLI) from Phase 1 is **stable**. Schedule: **`e2e-ui`** nightly or on front-end changes.

| Step | Action (UI) | Assert |
|------|-------------|--------|
| 1 | Open dashboard URL | Page loads, no console errors |
| 2 | Navigate to create experiment | Form visible |
| 3 | Upload dataset + prompt | Files accepted, names shown |
| 4 | Refresh page | Same experiment, same files (persistence) |
| 5 | Click **Start** | Status badge = `running`, Stop button appears |
| 6 | Wait for trial **completed** in table | Row with `completed` status + `val_bpb` value (bounded timeout ~60s with mock train) |
| 7 | Click **Stop** | Status = `stopped`; no stuck spinner |
| 8 | Click **Start** again (resume) | Status = `running` without re-upload prompt |

**Success:** All assertions pass; Playwright `exit 0`; no unhandled console errors captured via `page.on('console')`.

**Failure:** Element not found, timeout waiting for completed, UI shows wrong status vs API, upload lost on refresh.

### Playwright CI setup

- **Tool:** `pytest-playwright` (Python — same ecosystem as Phase 1, no Node dependency).
- **Browser:** Chromium headless (`--headed` for local debug).
- **Install in CI:** `playwright install chromium` in GitHub Actions setup step.
- **Artifacts on failure:** Screenshot + Playwright trace file (`.zip`) uploaded as CI artifacts.
- **Timeout:** 120s max per test (mock train finishes in <10s; rest is navigation).

---

## Acceptance tests owned by this plan

### From Phase 1 (browser-dependent parts)

- **AT-11** — Worker states **as shown in UI** (offline / idle / training badges).
- **AT-17** — Experiments and trials **views in dashboard** (not just JSON API).
- **AT-20** — Improvement chart **visible in UI** (Chart.js renders correctly).
- **AT-24** — Stuck-trial **count banner** in dashboard.
- **AT-26** — Playwright sub-step of golden-path E2E.

### New UI-specific acceptance tests

**AT-UI-1 — No unhandled JS console errors on golden path**

- **When** Playwright runs the full Flow B (steps 1–8).
- **Then** `page.on('console', msg => msg.type() === 'error')` captures **zero** errors.

**AT-UI-2 — UI state matches API on every page**

- **Given** the experiments list page.
- **When** the page renders and Playwright reads the visible status badge and trial counts.
- **Then** they match `GET /api/experiments` response (same status string, same counts within 1 polling cycle tolerance).

**AT-UI-3 — Upload shows progress or no frozen UI**

- **When** uploading a dataset file (even a small one).
- **Then** the upload button is disabled or shows a spinner until the server responds (no double-submit).

**AT-UI-4 — Stop button is immediately responsive**

- **When** user clicks **Stop** on a running experiment.
- **Then** the button disables (or shows spinner) within 500ms; status badge updates to `stopping`/`stopped` within one polling cycle.

**AT-UI-7 — One experiment at a time enforced in UI**

- **Given** one experiment is `running`.
- **When** the user views the experiments list.
- **Then** Start/Resume buttons on all other experiments are **visually disabled** (grayed out, not clickable). Hovering shows tooltip "Stop the running experiment first."
- **When** the running experiment is stopped, the buttons become enabled.

**AT-UI-5 — Empty states render correctly**

- **Given** a fresh orchestrator with no experiments and no workers.
- **When** the user opens `/`, `/workers`.
- **Then** placeholder messages appear (not blank tables or JS errors).

**AT-UI-6 — Chart renders with correct data**

- **Given** an experiment with at least 3 completed trials.
- **When** the user opens the experiment detail page.
- **Then** the Chart.js canvas is visible, has the correct number of data points, and `best_so_far` line is non-increasing.

---

## Link back

Phase 1 plan: [fork_viz_+_swarm_f1f60bbf.plan.md](fork_viz_+_swarm_f1f60bbf.plan.md) — orchestrator process may already mount `/static` or a stub; Phase 2 replaces stub with real pages. **AT-27** (curl parity) remains Phase 1 — UI consumes that API contract.
