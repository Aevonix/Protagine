-- Protagine Goal Engine persistence schema

CREATE TABLE IF NOT EXISTS goals (
    goal_id         TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'explicit',
    status          TEXT NOT NULL DEFAULT 'proposed',
    priority        INTEGER NOT NULL DEFAULT 50,
    outcome_json    TEXT,           -- GoalOutcome serialised as JSON
    deadline        TEXT,           -- ISO-8601 datetime
    parent_goal_id  TEXT,           -- self-reference for sub-goals
    tags_json       TEXT NOT NULL DEFAULT '{}',
    context_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    accepted_at     TEXT,
    completed_at    TEXT,
    abandoned_at    TEXT,
    abandon_reason  TEXT,
    replan_count    INTEGER NOT NULL DEFAULT 0,
    estimated_hours REAL,
    progress_pct    REAL NOT NULL DEFAULT 0.0,

    -- Initiative management fields (v0.7.10)
    last_initiative_at   TEXT,           -- ISO-8601: last time an initiative was generated for this goal
    snoozed_until        TEXT,           -- ISO-8601: don't generate initiatives until this time
    snooze_count         INTEGER NOT NULL DEFAULT 0,
    dismissal_reason     TEXT,           -- Why the goal was dismissed (if applicable)

    FOREIGN KEY (parent_goal_id) REFERENCES goals(goal_id)
);

CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE INDEX IF NOT EXISTS idx_goals_priority ON goals(priority DESC);
CREATE INDEX IF NOT EXISTS idx_goals_deadline ON goals(deadline);
CREATE INDEX IF NOT EXISTS idx_goals_last_initiative ON goals(last_initiative_at);
CREATE INDEX IF NOT EXISTS idx_goals_snoozed_until ON goals(snoozed_until);

CREATE TABLE IF NOT EXISTS goal_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         TEXT NOT NULL,
    from_status     TEXT NOT NULL,
    to_status       TEXT NOT NULL,
    trigger         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_goal ON goal_audit_log(goal_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON goal_audit_log(created_at DESC);
