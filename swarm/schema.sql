CREATE TABLE IF NOT EXISTS workers (
    id              TEXT PRIMARY KEY,
    display_name    TEXT UNIQUE NOT NULL,
    hostname        TEXT,
    registered_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    meta_json       TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    id                      TEXT PRIMARY KEY,
    name                    TEXT,
    dataset_uri             TEXT,
    prompt_uri              TEXT,
    program_prompt_inline   TEXT,
    dataset_ref             TEXT,
    git_ref                 TEXT,
    created_at              TEXT NOT NULL,
    run_mode                TEXT NOT NULL DEFAULT 'open_until_stop',
    scheduling_intent       TEXT NOT NULL DEFAULT 'sequential_autoresearch',
    replica_count           INTEGER,
    duration_hours          REAL,
    ends_at                 TEXT,
    stop_requested_at       TEXT,
    best_commit             TEXT,
    best_val_bpb            REAL,
    status                  TEXT NOT NULL DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS trials (
    id                  TEXT PRIMARY KEY,
    experiment_id       TEXT REFERENCES experiments(id),
    trial_index         INTEGER,
    status              TEXT NOT NULL DEFAULT 'queued',
    duration_seconds    REAL,
    priority            INTEGER DEFAULT 0,
    git_ref             TEXT,
    git_commit          TEXT,
    seed                INTEGER,
    env_json            TEXT,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    worker_id           TEXT REFERENCES workers(id),
    last_heartbeat_at   TEXT,
    current_phase       TEXT,
    training_pct        REAL DEFAULT 0,
    validation_pct      REAL DEFAULT 0,
    attempt_count       INTEGER DEFAULT 0,
    exit_code           INTEGER,
    val_bpb             REAL,
    stderr_tail         TEXT,
    artifact_uri        TEXT
);

CREATE INDEX IF NOT EXISTS idx_trials_status ON trials(status);
CREATE INDEX IF NOT EXISTS idx_trials_experiment ON trials(experiment_id);
CREATE INDEX IF NOT EXISTS idx_trials_val ON trials(val_bpb);
CREATE INDEX IF NOT EXISTS idx_trials_running_heartbeat ON trials(status, last_heartbeat_at);

CREATE TABLE IF NOT EXISTS models (
    id              TEXT PRIMARY KEY,
    experiment_id   TEXT REFERENCES experiments(id),
    source_commit   TEXT NOT NULL,
    source_val_bpb  REAL,
    status          TEXT NOT NULL DEFAULT 'creating',
    model_path      TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    duration_seconds REAL,
    error           TEXT
);
